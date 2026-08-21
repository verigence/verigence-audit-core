import os
from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_connection, get_principal
from audit_core.main import app
from audit_core.security import Principal


@pytest.fixture
def project_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for project integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_a = f"tenant-project-a-{suffix}"
    tenant_b = f"tenant-project-b-{suffix}"
    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, :name)
                RETURNING product_category_id
                """
            ),
            {"code": f"PCAT-{suffix}", "name": f"Category {suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, :name)
                RETURNING oem_id
                """
            ),
            {"code": f"POEM-{suffix}", "name": f"OEM {suffix}"},
        ).scalar_one()
        for tenant_id, project_name in (
            (tenant_a, "Project A"),
            (tenant_b, "Project B"),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.projects (
                        tenant_id, project_code, project_name, oem_id,
                        product_category_id, effective_start_date, project_status
                    ) VALUES (
                        :tenant_id, :project_code, :project_name, :oem_id,
                        :category_id, CURRENT_DATE, 'CONFIGURING'
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "project_code": f"P-{tenant_id}",
                    "project_name": project_name,
                    "oem_id": oem_id,
                    "category_id": category_id,
                },
            )

    def connection_override():
        with engine.begin() as connection:
            yield connection

    app.dependency_overrides[get_connection] = connection_override
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject="user-project",
        tenant_id=tenant_a,
        permissions=(),
    )
    try:
        yield engine, tenant_a, tenant_b
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_project_get_and_patch_are_tenant_bound_and_versioned(project_setup) -> None:
    engine, tenant_a, tenant_b = project_setup
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(f"/v1/tenants/{tenant_a}/project")
    assert response.status_code == 200
    assert response.headers["etag"] == '"1"'
    payload = response.json()
    assert payload["tenantId"] == tenant_a
    assert payload["projectCode"] == f"P-{tenant_a}"
    assert payload["projectName"] == "Project A"
    assert payload["projectStatus"] == "CONFIGURING"
    assert payload["timezoneName"] == "Asia/Kolkata"
    assert payload["versionNo"] == 1

    mismatch = client.get(f"/v1/tenants/{tenant_b}/project")
    assert mismatch.status_code == 403
    assert mismatch.json()["errorCode"] == "VAC-AUTH-003"

    updated = client.patch(
        f"/v1/tenants/{tenant_a}/project",
        headers={"If-Match": response.headers["etag"]},
        json={
            "projectName": "Project A Updated",
            "effectiveEndDate": (date.today() + timedelta(days=30)).isoformat(),
            "timezoneName": "Asia/Kolkata",
            "regionCode": "WEST",
        },
    )
    assert updated.status_code == 200
    assert updated.headers["etag"] == '"2"'
    assert updated.json()["projectName"] == "Project A Updated"
    assert updated.json()["regionCode"] == "WEST"
    assert updated.json()["versionNo"] == 2

    stale = client.patch(
        f"/v1/tenants/{tenant_a}/project",
        headers={"If-Match": '"1"'},
        json={"projectName": "Stale Update"},
    )
    assert stale.status_code == 409
    assert stale.json()["errorCode"] == "VAC-CONFLICT-001"

    with engine.connect() as connection:
        names = dict(
            connection.execute(
                text(
                    "SELECT tenant_id, project_name FROM auditcore.projects "
                    "WHERE tenant_id IN (:tenant_a, :tenant_b)"
                ),
                {"tenant_a": tenant_a, "tenant_b": tenant_b},
            ).all()
        )
    assert names[tenant_a] == "Project A Updated"
    assert names[tenant_b] == "Project B"


def test_project_restricted_fields_lock_after_published_master_exists(project_setup) -> None:
    engine, tenant_a, _ = project_setup
    client = TestClient(app, raise_server_exceptions=False)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.project_policy_versions (
                    tenant_id, version_no, lifecycle_status, effective_from
                ) VALUES (:tenant_id, 1, 'PUBLISHED', CURRENT_DATE)
                """
            ),
            {"tenant_id": tenant_a},
        )

    current = client.get(f"/v1/tenants/{tenant_a}/project")
    assert current.status_code == 200
    original_start = date.fromisoformat(current.json()["effectiveStartDate"])

    blocked = client.patch(
        f"/v1/tenants/{tenant_a}/project",
        headers={"If-Match": current.headers["etag"]},
        json={"effectiveStartDate": (original_start + timedelta(days=1)).isoformat()},
    )
    assert blocked.status_code == 422
    assert blocked.json()["errorCode"] == "VAC-VAL-002"

    allowed = client.patch(
        f"/v1/tenants/{tenant_a}/project",
        headers={"If-Match": current.headers["etag"]},
        json={"projectName": "Still Editable"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["projectName"] == "Still Editable"


def test_uc02_project_status_accepts_configuring(project_setup) -> None:
    engine, tenant_a, _ = project_setup
    with engine.connect() as connection:
        status = connection.execute(
            text(
                "SELECT project_status FROM auditcore.projects "
                "WHERE tenant_id=:tenant_id"
            ),
            {"tenant_id": tenant_a},
        ).scalar_one()
    assert status == "CONFIGURING"
