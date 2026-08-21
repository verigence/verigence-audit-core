from __future__ import annotations

import os
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_super_admin_request,
)
from audit_core.di_project_master_proxy import get_di_admin_client
from audit_core.main import app
from audit_core.security_integration import SecurityAdminContext


@dataclass
class FakeDiAdminClient:
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    upload_calls: int = 0

    def list_project_masters(
        self,
        *,
        human_token: str,
        tenant_id: str,
    ) -> list[dict]:
        self.calls.append(("catalogue", human_token, tenant_id))
        return [
            {
                "masterKey": "DOCUMENT_TYPES",
                "displayName": "Document Types",
                "administrationModes": ["FORM", "EXCEL"],
                "requiresWEF": False,
            },
            {
                "masterKey": "EXTRACTION_PROFILES",
                "displayName": "Extraction Profiles",
                "administrationModes": ["FORM", "EXCEL"],
                "requiresWEF": False,
            },
            {
                "masterKey": "REQUIREMENT_PROFILES",
                "displayName": "Requirement Profiles",
                "administrationModes": ["FORM", "EXCEL"],
                "requiresWEF": False,
            },
        ]

    def list_project_master_versions(
        self,
        *,
        human_token: str,
        tenant_id: str,
        master_key: str,
    ) -> dict:
        self.calls.append((master_key, human_token, tenant_id))
        state = "ACTIVE" if master_key == "DOCUMENT_TYPES" else "PUBLISHED"
        return {
            "masterKey": master_key,
            "versions": [
                {
                    "versionId": str(uuid4()),
                    "businessKey": f"{master_key.lower()}-key",
                    "displayName": master_key.replace("_", " ").title(),
                    "status": state,
                    "versionNo": 1,
                    "publishedAtUtc": None,
                }
            ],
        }

    def get_project_master_template(
        self,
        *,
        human_token: str,
        tenant_id: str,
        master_key: str,
    ) -> tuple[bytes, str]:
        self.calls.append((f"template:{master_key}", human_token, tenant_id))
        return (
            b"DI-XLSX-TEMPLATE",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def upload_project_master_import(
        self,
        *,
        human_token: str,
        tenant_id: str,
        master_key: str,
        idempotency_key: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> dict:
        self.upload_calls += 1
        self.calls.append((f"upload:{master_key}", human_token, tenant_id))
        assert idempotency_key == "di-master-import-0001"
        assert filename == "document-types.xlsx"
        assert content == b"DI-WORKBOOK"
        assert "spreadsheetml" in content_type
        return {
            "importId": str(uuid4()),
            "tenantId": tenant_id,
            "masterKey": master_key,
            "fileName": filename,
            "fileHashSha256": "a" * 64,
            "templateVersion": "1.0",
            "status": "PREVIEW_READY",
            "rowsParsed": 1,
            "validRows": 1,
            "warningRows": 0,
            "errorRows": 0,
            "createdByUserId": "superadmin-di",
            "confirmedByUserId": None,
            "confirmedAtUtc": None,
            "rows": [
                {
                    "rowNumber": 2,
                    "parsedData": {
                        "documentTypeKey": "booking_form",
                        "displayName": "Booking Form",
                    },
                    "validationStatus": "VALID",
                    "validationMessages": [],
                }
            ],
        }


@pytest.fixture
def di_proxy_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for DI Project Master proxy tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-di-master-{suffix}"
    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, :name)
                RETURNING product_category_id
                """
            ),
            {"code": f"DI-CAT-{suffix}", "name": f"DI Category {suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, :name)
                RETURNING oem_id
                """
            ),
            {"code": f"DI-OEM-{suffix}", "name": f"DI OEM {suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date, project_status
                ) VALUES (
                    :tenant_id, :code, 'DI Master Project', :oem_id,
                    :category_id, CURRENT_DATE, 'CONFIGURING'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"DI-PROJ-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )

    def connection_override():
        with engine.begin() as connection:
            yield connection

    fake_di = FakeDiAdminClient()
    admin_request = HumanAdminRequest(
        user_id="superadmin-di",
        bearer_token="same-human-superadmin-token",
        admin_context=SecurityAdminContext(
            user_id="superadmin-di",
            is_super_admin=True,
            admin_scopes=(),
        ),
    )
    app.dependency_overrides[get_connection] = connection_override
    app.dependency_overrides[require_super_admin_request] = lambda: admin_request
    app.dependency_overrides[get_di_admin_client] = lambda: fake_di
    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "di": fake_di,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_catalogue_combines_audit_core_and_di_and_forwards_human_token(
    di_proxy_setup,
) -> None:
    setup = di_proxy_setup
    response = TestClient(app, raise_server_exceptions=False).get(
        f"/v1/tenants/{setup['tenant_id']}/project-masters"
    )

    assert response.status_code == 200, response.text
    items = response.json()
    by_key = {(item["ownerModule"], item["masterKey"]): item for item in items}
    assert ("AUDIT_CORE", "PRODUCT_MASTER") in by_key
    assert by_key[("DI", "DOCUMENT_TYPES")]["lifecycleStatus"] == "ACTIVE"
    assert by_key[("DI", "EXTRACTION_PROFILES")]["lifecycleStatus"] == "PUBLISHED"
    assert by_key[("DI", "REQUIREMENT_PROFILES")]["lifecycleStatus"] == "PUBLISHED"
    assert all(call[1] == "same-human-superadmin-token" for call in setup["di"].calls)


def test_di_template_is_proxied_without_browser_calling_di_directly(di_proxy_setup) -> None:
    setup = di_proxy_setup
    response = TestClient(app, raise_server_exceptions=False).get(
        f"/v1/tenants/{setup['tenant_id']}/project-masters/DI/DOCUMENT_TYPES/template"
    )

    assert response.status_code == 200
    assert response.content == b"DI-XLSX-TEMPLATE"
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert setup["di"].calls[-1] == (
        "template:DOCUMENT_TYPES",
        "same-human-superadmin-token",
        setup["tenant_id"],
    )


def test_di_import_is_staged_in_di_and_mirrored_into_existing_audit_core_operation_model(
    di_proxy_setup,
) -> None:
    setup = di_proxy_setup
    response = TestClient(app, raise_server_exceptions=False).post(
        f"/v1/tenants/{setup['tenant_id']}/project-masters/DI/DOCUMENT_TYPES/imports",
        headers={"Idempotency-Key": "di-master-import-0001"},
        files={
            "file": (
                "document-types.xlsx",
                b"DI-WORKBOOK",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["ownerModule"] == "DI"
    assert payload["masterKey"] == "DOCUMENT_TYPES"
    assert payload["status"] == "PREVIEW_READY"
    assert payload["rowsParsed"] == 1
    assert setup["di"].upload_calls == 1

    with setup["engine"].begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT owner_module, master_key, status, rows_parsed,
                       valid_rows, warning_rows, error_rows
                FROM auditcore.project_master_imports
                WHERE tenant_id=:tenant_id AND import_id=:import_id
                """
            ),
            {
                "tenant_id": setup["tenant_id"],
                "import_id": payload["importId"],
            },
        ).mappings().one()
        staged_row = connection.execute(
            text(
                """
                SELECT validation_status, parsed_data
                FROM auditcore.project_master_import_rows
                WHERE tenant_id=:tenant_id AND import_id=:import_id
                """
            ),
            {
                "tenant_id": setup["tenant_id"],
                "import_id": payload["importId"],
            },
        ).mappings().one()

    assert row["owner_module"] == "DI"
    assert row["master_key"] == "DOCUMENT_TYPES"
    assert row["status"] == "PREVIEW_READY"
    assert row["rows_parsed"] == 1
    assert row["valid_rows"] == 1
    assert staged_row["validation_status"] == "VALID"
    assert staged_row["parsed_data"]["documentTypeKey"] == "booking_form"


def test_di_import_rejects_wef_because_di_lifecycle_has_no_approved_wef(
    di_proxy_setup,
) -> None:
    setup = di_proxy_setup
    response = TestClient(app, raise_server_exceptions=False).post(
        f"/v1/tenants/{setup['tenant_id']}/project-masters/DI/DOCUMENT_TYPES/imports",
        headers={"Idempotency-Key": "di-master-import-0001"},
        data={"effectiveFrom": "2026-08-21"},
        files={
            "file": (
                "document-types.xlsx",
                b"DI-WORKBOOK",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 400
    assert response.json()["errorCode"] == "VAC-VAL-001"
    assert setup["di"].upload_calls == 0
