import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal


@pytest.fixture
def feedback_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for feedback integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-feedback-{suffix}"
    actor_id = f"feedback-user-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"FCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Feedback OEM') RETURNING oem_id"
            ),
            {"code": f"FOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Feedback Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"FP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.business_assignments (
                    tenant_id, security_actor_id, business_role_code
                ) VALUES (:tenant_id, :actor_id, 'PC')
                """
            ),
            {"tenant_id": tenant_id, "actor_id": actor_id},
        )

    def connection_override():
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
            yield connection

    app.dependency_overrides[get_connection] = connection_override
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=actor_id)
    try:
        yield tenant_id, actor_id, engine
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_pc_can_submit_feedback_without_feedback_select_access(feedback_setup) -> None:
    tenant_id, actor_id, engine = feedback_setup
    client = TestClient(app, raise_server_exceptions=False)
    screenshot = b"\x89PNG\r\n\x1a\nfeedback-regression"

    response = client.post(
        f"/v1/tenants/{tenant_id}/feedback",
        data={
            "feedbackText": "Website is slow. Please look into the issue.",
            "submittedByDisplayName": "Feedback Tester",
            "pagePath": "/bookings/example",
        },
        files={"screenshot": ("feedback.png", screenshot, "image/png")},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["feedbackId"]
    assert body["createdAtUtc"]

    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.platform_super_admin', 'true', true)"))
        stored = connection.execute(
            text(
                """
                SELECT submitted_by_user_id, submitted_by_role, feedback_text,
                       page_path, screenshot_file_name, screenshot_content_type,
                       screenshot_data
                FROM auditcore.user_feedback
                WHERE feedback_id = :feedback_id
                """
            ),
            {"feedback_id": body["feedbackId"]},
        ).mappings().one()

    assert stored["submitted_by_user_id"] == actor_id
    assert stored["submitted_by_role"] == "PC"
    assert stored["feedback_text"] == "Website is slow. Please look into the issue."
    assert stored["page_path"] == "/bookings/example"
    assert stored["screenshot_file_name"] == "feedback.png"
    assert stored["screenshot_content_type"] == "image/png"
    assert bytes(stored["screenshot_data"]) == screenshot
