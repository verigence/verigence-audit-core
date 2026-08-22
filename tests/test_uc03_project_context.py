from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.db import set_security_actor_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal


@pytest.fixture
def uc03_project_context_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 Project Context integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    actor_id = f"uc03-user-{suffix}"
    other_actor_id = f"uc03-other-{suffix}"
    tenant_pc = f"tenant-uc03-pc-{suffix}"
    tenant_pm = f"tenant-uc03-pm-{suffix}"
    tenant_other = f"tenant-uc03-other-{suffix}"
    tenant_inactive = f"tenant-uc03-inactive-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, :name)
                RETURNING product_category_id
                """
            ),
            {"code": f"UC03-CAT-{suffix}", "name": f"UC03 Category {suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, :name)
                RETURNING oem_id
                """
            ),
            {"code": f"UC03-OEM-{suffix}", "name": f"UC03 OEM {suffix}"},
        ).scalar_one()

        projects = (
            (tenant_pc, "Alpha Project", "ACTIVE"),
            (tenant_pm, "Beta Project", "ACTIVE"),
            (tenant_other, "Other User Project", "ACTIVE"),
            (tenant_inactive, "Inactive Project", "INACTIVE"),
        )
        for tenant_id, project_name, status in projects:
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.projects (
                        tenant_id, project_code, project_name, oem_id,
                        product_category_id, effective_start_date,
                        timezone_name, project_status
                    ) VALUES (
                        :tenant_id, :project_code, :project_name, :oem_id,
                        :category_id, CURRENT_DATE - 1,
                        'Asia/Kolkata', :project_status
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "project_code": f"P-{tenant_id}",
                    "project_name": project_name,
                    "oem_id": oem_id,
                    "category_id": category_id,
                    "project_status": status,
                },
            )

        dealer_one = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Dealer One')
                RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_pc, "code": f"D1-{suffix}"},
        ).scalar_one()
        dealer_two = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Dealer Two')
                RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_pc, "code": f"D2-{suffix}"},
        ).scalar_one()

        outlet_one = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (:tenant_id, :dealer_id, :code, 'Outlet One')
                RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_pc, "dealer_id": dealer_one, "code": f"O1-{suffix}"},
        ).scalar_one()
        outlet_two = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (:tenant_id, :dealer_id, :code, 'Outlet Two')
                RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_pc, "dealer_id": dealer_two, "code": f"O2-{suffix}"},
        ).scalar_one()

        for dealer_id, outlet_id in ((dealer_one, outlet_one), (dealer_two, outlet_two)):
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.business_assignments (
                        tenant_id, security_actor_id, business_role_code,
                        dealer_id, outlet_id
                    ) VALUES (:tenant_id, :actor_id, 'PC', :dealer_id, :outlet_id)
                    """
                ),
                {
                    "tenant_id": tenant_pc,
                    "actor_id": actor_id,
                    "dealer_id": dealer_id,
                    "outlet_id": outlet_id,
                },
            )

        connection.execute(
            text(
                """
                INSERT INTO auditcore.business_assignments (
                    tenant_id, security_actor_id, business_role_code
                ) VALUES (:tenant_id, :actor_id, 'PM')
                """
            ),
            {"tenant_id": tenant_pm, "actor_id": actor_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.business_assignments (
                    tenant_id, security_actor_id, business_role_code
                ) VALUES (:tenant_id, :actor_id, 'PM')
                """
            ),
            {"tenant_id": tenant_other, "actor_id": other_actor_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.business_assignments (
                    tenant_id, security_actor_id, business_role_code
                ) VALUES (:tenant_id, :actor_id, 'PM')
                """
            ),
            {"tenant_id": tenant_inactive, "actor_id": actor_id},
        )

    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=actor_id)
    try:
        yield {
            "engine": engine,
            "actor_id": actor_id,
            "other_actor_id": other_actor_id,
            "tenant_pc": tenant_pc,
            "tenant_pm": tenant_pm,
            "tenant_other": tenant_other,
            "tenant_inactive": tenant_inactive,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_me_projects_returns_only_active_projects_for_current_actor(uc03_project_context_setup) -> None:
    setup = uc03_project_context_setup
    response = TestClient(app, raise_server_exceptions=False).get("/v1/me/projects")

    assert response.status_code == 200
    payload = response.json()
    assert [project["tenantId"] for project in payload["projects"]] == [
        setup["tenant_pc"],
        setup["tenant_pm"],
    ]
    assert payload["projects"][0] == {
        "tenantId": setup["tenant_pc"],
        "projectCode": f"P-{setup['tenant_pc']}",
        "projectName": "Alpha Project",
        "projectStatus": "ACTIVE",
        "timezoneName": "Asia/Kolkata",
        "operatingRole": "PC",
        "scope": {"allDealers": False, "dealerCount": 2, "outletCount": 2},
    }
    assert payload["projects"][1] == {
        "tenantId": setup["tenant_pm"],
        "projectCode": f"P-{setup['tenant_pm']}",
        "projectName": "Beta Project",
        "projectStatus": "ACTIVE",
        "timezoneName": "Asia/Kolkata",
        "operatingRole": "PM",
        "scope": {"allDealers": True, "dealerCount": 0, "outletCount": 0},
    }


def test_project_discovery_rls_is_actor_read_only(uc03_project_context_setup) -> None:
    setup = uc03_project_context_setup
    with setup["engine"].begin() as connection:
        connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
        set_security_actor_context(connection, setup["actor_id"])

        assignment_tenants = set(
            connection.execute(
                text(
                    """
                    SELECT tenant_id
                    FROM auditcore.business_assignments
                    WHERE assignment_status='ACTIVE'
                    """
                )
            ).scalars()
        )
        visible_projects = set(
            connection.execute(text("SELECT tenant_id FROM auditcore.projects")).scalars()
        )

        assert assignment_tenants == {
            setup["tenant_pc"],
            setup["tenant_pm"],
            setup["tenant_inactive"],
        }
        assert visible_projects == {
            setup["tenant_pc"],
            setup["tenant_pm"],
            setup["tenant_inactive"],
        }
        assert setup["tenant_other"] not in visible_projects

        update_result = connection.execute(
            text(
                """
                UPDATE auditcore.projects
                SET project_name='SHOULD NOT CHANGE'
                WHERE tenant_id=:tenant_id
                """
            ),
            {"tenant_id": setup["tenant_pc"]},
        )
        assert update_result.rowcount == 0


def test_c0_foundation_tables_and_append_only_trigger_exist(uc03_project_context_setup) -> None:
    engine = uc03_project_context_setup["engine"]
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT to_regclass('auditcore.journey_stage_states')")
        ).scalar_one() == "auditcore.journey_stage_states"
        assert connection.execute(
            text("SELECT to_regclass('auditcore.journey_workflow_events')")
        ).scalar_one() == "auditcore.journey_workflow_events"
        trigger_count = connection.execute(
            text(
                """
                SELECT count(*)
                FROM pg_trigger
                WHERE tgrelid='auditcore.journey_workflow_events'::regclass
                  AND tgname='trg_journey_workflow_events_append_only'
                  AND NOT tgisinternal
                """
            )
        ).scalar_one()
        assert trigger_count == 1
