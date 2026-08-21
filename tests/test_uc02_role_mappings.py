from __future__ import annotations

import os
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core import role_mappings
from audit_core.dependencies import HumanAdminRequest, require_super_admin_request
from audit_core.main import app
from audit_core.security_integration import (
    SecurityAdminContext,
    SecurityAdminError,
    SecurityOperatingRoleMutation,
)


@dataclass
class ControlledSecurityAdmin:
    calls: list[tuple[str, str, str, str | None]] = field(default_factory=list)
    fail_set: bool = False
    fail_remove: bool = False

    def client_class(self):
        controller = self

        class Client:
            def __init__(self, *, base_url: str) -> None:
                assert base_url == "https://security.test"

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                return None

            def set_operating_role(
                self,
                *,
                human_bearer_token: str,
                tenant_id: str,
                user_id: str,
                role_key: str,
            ) -> SecurityOperatingRoleMutation:
                controller.calls.append(("PUT", human_bearer_token, user_id, role_key))
                if controller.fail_set:
                    raise SecurityAdminError("Security unavailable")
                return SecurityOperatingRoleMutation(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    changed=True,
                    assignment_id=str(uuid4()),
                    role_key=role_key,
                )

            def remove_operating_role(
                self,
                *,
                human_bearer_token: str,
                tenant_id: str,
                user_id: str,
            ) -> SecurityOperatingRoleMutation:
                controller.calls.append(("DELETE", human_bearer_token, user_id, None))
                if controller.fail_remove:
                    raise SecurityAdminError("Security unavailable")
                return SecurityOperatingRoleMutation(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    changed=True,
                    assignment_id=str(uuid4()),
                    role_key="PC",
                )

        return Client


@pytest.fixture
def role_mapping_setup(monkeypatch):
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC02 Role Mapping integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_a = f"tenant-role-a-{suffix}"
    tenant_b = f"tenant-role-b-{suffix}"
    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, :name)
                RETURNING product_category_id
                """
            ),
            {"code": f"RCAT-{suffix}", "name": f"Role Category {suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, :name)
                RETURNING oem_id
                """
            ),
            {"code": f"ROEM-{suffix}", "name": f"Role OEM {suffix}"},
        ).scalar_one()
        for tenant_id in (tenant_a, tenant_b):
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.projects (
                        tenant_id, project_code, project_name, oem_id,
                        product_category_id, effective_start_date, project_status
                    ) VALUES (
                        :tenant_id, :code, :name, :oem_id,
                        :category_id, CURRENT_DATE, 'CONFIGURING'
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "code": f"P-{tenant_id}",
                    "name": f"Project {tenant_id}",
                    "oem_id": oem_id,
                    "category_id": category_id,
                },
            )

        dealer_a = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Dealer A')
                RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_a, "code": f"DA-{suffix}"},
        ).scalar_one()
        dealer_a2 = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Dealer A2')
                RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_a, "code": f"DA2-{suffix}"},
        ).scalar_one()
        dealer_b = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Dealer B')
                RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_b, "code": f"DB-{suffix}"},
        ).scalar_one()
        outlet_a = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (:tenant_id, :dealer_id, :code, 'Outlet A')
                RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_a, "dealer_id": dealer_a, "code": f"OA-{suffix}"},
        ).scalar_one()
        outlet_a2 = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (:tenant_id, :dealer_id, :code, 'Outlet A2')
                RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_a, "dealer_id": dealer_a2, "code": f"OA2-{suffix}"},
        ).scalar_one()
        outlet_b = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (:tenant_id, :dealer_id, :code, 'Outlet B')
                RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_b, "dealer_id": dealer_b, "code": f"OB-{suffix}"},
        ).scalar_one()

    controller = ControlledSecurityAdmin()
    monkeypatch.setenv("SECURITY_BASE_URL", "https://security.test")
    monkeypatch.setattr(role_mappings, "SecurityAdminClient", controller.client_class())
    admin_request = HumanAdminRequest(
        user_id="superadmin-role",
        bearer_token="same-human-superadmin-token",
        admin_context=SecurityAdminContext(
            user_id="superadmin-role",
            is_super_admin=True,
            admin_scopes=(),
        ),
    )
    app.dependency_overrides[require_super_admin_request] = lambda: admin_request
    try:
        yield {
            "engine": engine,
            "tenant_a": tenant_a,
            "tenant_b": tenant_b,
            "dealer_a": str(dealer_a),
            "dealer_a2": str(dealer_a2),
            "outlet_a": str(outlet_a),
            "outlet_a2": str(outlet_a2),
            "outlet_b": str(outlet_b),
            "security": controller,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_pc_role_mapping_forwards_same_human_token_and_is_idempotent(
    role_mapping_setup,
) -> None:
    setup = role_mapping_setup
    client = TestClient(app, raise_server_exceptions=False)
    user_id = "user-pc-1"
    path = f"/v1/tenants/{setup['tenant_a']}/role-mappings/{user_id}"
    body = {
        "operatingRole": "PC",
        "dealerIds": [],
        "outletIds": [setup["outlet_a"], setup["outlet_a2"]],
    }

    first = client.put(path, headers={"Idempotency-Key": "pc-map-1"}, json=body)
    assert first.status_code == 200
    assert first.json()["operationStatus"] == "COMPLETED"
    assert first.json()["mapping"] == {
        "userId": user_id,
        "operatingRole": "PC",
        "dealerIds": [],
        "outletIds": sorted([setup["outlet_a"], setup["outlet_a2"]]),
    }
    assert setup["security"].calls == [
        ("PUT", "same-human-superadmin-token", user_id, "PC")
    ]

    replay = client.put(path, headers={"Idempotency-Key": "pc-map-1"}, json=body)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert len(setup["security"].calls) == 1

    current = client.get(path)
    assert current.status_code == 200
    assert current.json() == first.json()["mapping"]

    with setup["engine"].connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT dealer_id::text, outlet_id::text, business_role_code
                FROM auditcore.business_assignments
                WHERE tenant_id=:tenant_id AND security_actor_id=:user_id
                  AND assignment_status='ACTIVE'
                ORDER BY outlet_id::text
                """
            ),
            {"tenant_id": setup["tenant_a"], "user_id": user_id},
        ).all()
        operation = connection.execute(
            text(
                """
                SELECT status, safe_request_summary::text, security_receipt::text
                FROM auditcore.administrative_operations
                WHERE operation_type='ROLE_MAPPING'
                  AND tenant_id=:tenant_id AND idempotency_key='pc-map-1'
                """
            ),
            {"tenant_id": setup["tenant_a"]},
        ).one()

    assert len(rows) == 2
    assert {row[2] for row in rows} == {"PC"}
    assert {row[1] for row in rows} == {setup["outlet_a"], setup["outlet_a2"]}
    assert operation[0] == "COMPLETED"
    assert "same-human-superadmin-token" not in (operation[1] or "")
    assert "same-human-superadmin-token" not in (operation[2] or "")


def test_role_mapping_scope_rules_and_tenant_isolation(role_mapping_setup) -> None:
    setup = role_mapping_setup
    client = TestClient(app, raise_server_exceptions=False)
    tenant = setup["tenant_a"]

    cases = [
        ("PC", [], [], 422),
        ("TL", [setup["dealer_a"]], [setup["outlet_a"]], 422),
        ("PM", [setup["dealer_a"]], [], 422),
        ("Executive", [], [setup["outlet_a"]], 422),
        ("CRM", [], [setup["outlet_a"]], 422),
        ("PC", [], [setup["outlet_b"]], 422),
    ]
    for index, (role, dealers, outlets, expected) in enumerate(cases):
        response = client.put(
            f"/v1/tenants/{tenant}/role-mappings/user-invalid-{index}",
            headers={"Idempotency-Key": f"invalid-{index}"},
            json={
                "operatingRole": role,
                "dealerIds": dealers,
                "outletIds": outlets,
            },
        )
        assert response.status_code == expected
        assert response.json()["errorCode"] == "VAC-VAL-002"

    assert setup["security"].calls == []


def test_tl_pm_crm_and_executive_scope_shapes(role_mapping_setup) -> None:
    setup = role_mapping_setup
    client = TestClient(app, raise_server_exceptions=False)
    tenant = setup["tenant_a"]
    cases = [
        (
            "TL",
            [setup["dealer_a"], setup["dealer_a2"]],
            [],
            [setup["dealer_a"], setup["dealer_a2"]],
            [],
        ),
        ("PM", [], [], [], []),
        ("CRM", [setup["dealer_a"]], [], [setup["dealer_a"]], []),
        ("CRM", [], [], [], []),
        ("Executive", [], [], [], []),
    ]
    for index, (role, dealers, outlets, expected_dealers, expected_outlets) in enumerate(cases):
        user_id = f"user-scope-{index}"
        response = client.put(
            f"/v1/tenants/{tenant}/role-mappings/{user_id}",
            headers={"Idempotency-Key": f"scope-{index}"},
            json={
                "operatingRole": role,
                "dealerIds": dealers,
                "outletIds": outlets,
            },
        )
        assert response.status_code == 200
        mapping = response.json()["mapping"]
        assert mapping["operatingRole"] == role
        assert mapping["dealerIds"] == sorted(expected_dealers)
        assert mapping["outletIds"] == expected_outlets


def test_role_mapping_security_failure_is_recoverable_with_same_key(role_mapping_setup) -> None:
    setup = role_mapping_setup
    client = TestClient(app, raise_server_exceptions=False)
    setup["security"].fail_set = True
    user_id = "user-recovery"
    path = f"/v1/tenants/{setup['tenant_a']}/role-mappings/{user_id}"
    body = {
        "operatingRole": "TL",
        "dealerIds": [setup["dealer_a"]],
        "outletIds": [],
    }

    failed = client.put(path, headers={"Idempotency-Key": "recover-map"}, json=body)
    assert failed.status_code == 202
    assert failed.json()["operationStatus"] == "RECOVERY_REQUIRED"
    assert failed.json()["mapping"] is None

    setup["security"].fail_set = False
    recovered = client.put(path, headers={"Idempotency-Key": "recover-map"}, json=body)
    assert recovered.status_code == 200
    assert recovered.json()["operationStatus"] == "COMPLETED"
    assert recovered.json()["mapping"]["operatingRole"] == "TL"
    assert len(setup["security"].calls) == 2


def test_role_mapping_delete_is_project_assignment_removal_and_retry_safe(
    role_mapping_setup,
) -> None:
    setup = role_mapping_setup
    client = TestClient(app, raise_server_exceptions=False)
    user_id = "user-delete-role"
    path = f"/v1/tenants/{setup['tenant_a']}/role-mappings/{user_id}"
    created = client.put(
        path,
        headers={"Idempotency-Key": "create-before-delete"},
        json={
            "operatingRole": "PC",
            "dealerIds": [],
            "outletIds": [setup["outlet_a"]],
        },
    )
    assert created.status_code == 200

    removed = client.delete(path, headers={"Idempotency-Key": "remove-role-1"})
    assert removed.status_code == 200
    assert removed.json()["operationStatus"] == "COMPLETED"
    assert removed.json()["mapping"] is None
    assert setup["security"].calls[-1] == (
        "DELETE",
        "same-human-superadmin-token",
        user_id,
        None,
    )

    replay = client.delete(path, headers={"Idempotency-Key": "remove-role-1"})
    assert replay.status_code == 200
    assert len([call for call in setup["security"].calls if call[0] == "DELETE"]) == 1
    assert client.get(path).json() is None

    with setup["engine"].connect() as connection:
        active = connection.execute(
            text(
                """
                SELECT count(*) FROM auditcore.business_assignments
                WHERE tenant_id=:tenant_id AND security_actor_id=:user_id
                  AND assignment_status='ACTIVE'
                """
            ),
            {"tenant_id": setup["tenant_a"], "user_id": user_id},
        ).scalar_one()
    assert active == 0


def test_role_mapping_idempotency_conflict_and_operation_table_has_no_delete_privilege(
    role_mapping_setup,
) -> None:
    setup = role_mapping_setup
    client = TestClient(app, raise_server_exceptions=False)
    user_id = "user-idem-conflict"
    path = f"/v1/tenants/{setup['tenant_a']}/role-mappings/{user_id}"
    first = client.put(
        path,
        headers={"Idempotency-Key": "mapping-conflict"},
        json={"operatingRole": "PM", "dealerIds": [], "outletIds": []},
    )
    assert first.status_code == 200
    conflict = client.put(
        path,
        headers={"Idempotency-Key": "mapping-conflict"},
        json={
            "operatingRole": "TL",
            "dealerIds": [setup["dealer_a"]],
            "outletIds": [],
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["errorCode"] == "VAC-CONFLICT-003"

    with setup["engine"].connect() as connection:
        can_delete = connection.execute(
            text(
                "SELECT has_table_privilege(" 
                "'audit_core_runtime', "
                "'auditcore.administrative_operations', 'DELETE')"
            )
        ).scalar_one()
    assert can_delete is False
