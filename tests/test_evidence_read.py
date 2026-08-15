from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_bearer_token, get_connection, get_engine, get_principal
from audit_core.di_client import DiDocument, DiFact
from audit_core.evidence import get_di_client, get_security_oauth_client
from audit_core.main import app
from audit_core.security import Principal


class FakeSecurityClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, ...]] = []

    def exchange_user_token(self, *, subject_token: str, permissions: list[str]) -> str:
        assert subject_token == "user-token"
        self.requests.append(tuple(permissions))
        return "delegated-read-token"


class FakeDiClient:
    def __init__(self, subject_id: UUID, document_id: UUID) -> None:
        self.subject_id = subject_id
        self.document_id = document_id
        self.document_calls = 0
        self.fact_calls = 0

    def get_document(
        self,
        *,
        token: str,
        tenant_id: str,
        subject_id: str,
        document_id: str,
    ) -> DiDocument:
        assert token == "delegated-read-token"
        assert subject_id == str(self.subject_id)
        assert document_id == str(self.document_id)
        self.document_calls += 1
        return DiDocument(
            document_id=document_id,
            subject_id=subject_id,
            upload_status="FIT",
            processing_status="PROCESSED",
            confirmation_status="CONFIRMED",
            verification_state="NOT_VERIFIED",
            human_verification_status="OPTIONAL",
            confidence_score=96.5,
            correlation_id="di-refresh-1",
        )

    def get_document_facts(
        self,
        *,
        token: str,
        tenant_id: str,
        subject_id: str,
        document_id: str,
    ) -> tuple[DiFact, ...]:
        assert token == "delegated-read-token"
        assert subject_id == str(self.subject_id)
        assert document_id == str(self.document_id)
        self.fact_calls += 1
        return (
            DiFact(
                canonical_field_id="di-field-invoice",
                field_key="invoice_number",
                value="INV-200",
                value_source="MACHINE",
                confidence_score=98.2,
                version_no=1,
            ),
            DiFact(
                canonical_field_id="di-field-amount",
                field_key="amount",
                value=125000.5,
                value_source="MACHINE",
                confidence_score=94.0,
                version_no=1,
            ),
        )


@pytest.fixture
def evidence_read_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for evidence read integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-evidence-read-{suffix}"
    actor_id = f"pc-{suffix}"
    di_subject_id = uuid4()
    di_document_id = uuid4()

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"RCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Read OEM') RETURNING oem_id"
            ),
            {"code": f"ROEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Read Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"RP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Read Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"RD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Read Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"RO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Read Customer'
                ) RETURNING customer_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.journeys (
                    tenant_id, dealer_id, outlet_id, customer_id, journey_reference
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'READ-JOURNEY'
                ) RETURNING journey_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
                "customer_id": customer_id,
            },
        ).scalar_one()
        evidence_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.evidence (
                    tenant_id, journey_id, customer_id,
                    di_subject_id, di_document_id,
                    document_type_key, evidence_purpose,
                    processing_status_cache, verification_status_cache,
                    confirmation_status_cache, linked_by_actor_id
                ) VALUES (
                    :tenant_id, :journey_id, :customer_id,
                    :di_subject_id, :di_document_id,
                    'BOOKING_FORM', 'BOOKING_CAPTURE',
                    'PROCESSING', 'NOT_VERIFIED', 'PENDING', :actor_id
                ) RETURNING evidence_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "customer_id": customer_id,
                "di_subject_id": di_subject_id,
                "di_document_id": di_document_id,
                "actor_id": actor_id,
            },
        ).scalar_one()
        old_fact_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.evidence_facts (
                    tenant_id, evidence_id, journey_id, field_key, value_type,
                    value_json, normalized_value, confidence_score,
                    di_field_reference, verification_status
                ) VALUES (
                    :tenant_id, :evidence_id, :journey_id,
                    'invoice_number', 'TEXT', CAST(:value_json AS jsonb),
                    'INV-OLD', 80, 'old-di-field', 'NOT_VERIFIED'
                ) RETURNING evidence_fact_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "evidence_id": evidence_id,
                "journey_id": journey_id,
                "value_json": '"INV-OLD"',
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.business_assignments (
                    tenant_id, security_actor_id, business_role_code,
                    dealer_id, outlet_id
                ) VALUES (
                    :tenant_id, :actor_id, 'PC', :dealer_id, :outlet_id
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
            },
        )

    security_client = FakeSecurityClient()
    di_client = FakeDiClient(di_subject_id, di_document_id)

    def connection_override():
        with engine.begin() as connection:
            yield connection

    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_connection] = connection_override
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject=actor_id,
        tenant_id=tenant_id,
        permissions=(
            "audit.evidence.read",
            "audit.evidence.refresh",
            "di.document.read",
            "di.document.fields.read",
        ),
    )
    app.dependency_overrides[get_bearer_token] = lambda: "user-token"
    app.dependency_overrides[get_security_oauth_client] = lambda: security_client
    app.dependency_overrides[get_di_client] = lambda: di_client
    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "evidence_id": evidence_id,
            "old_fact_id": old_fact_id,
            "actor_id": actor_id,
            "security": security_client,
            "di": di_client,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _assert_no_di_ids(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            assert not key.lower().startswith("di")
            _assert_no_di_ids(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_di_ids(item)


def test_evidence_read_routes_use_only_audit_core_ids(evidence_read_setup) -> None:
    setup = evidence_read_setup
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/v1/tenants/{setup['tenant_id']}/journeys/{setup['journey_id']}/evidence"

    listed = client.get(base)
    detail = client.get(f"{base}/{setup['evidence_id']}")
    facts = client.get(f"{base}/{setup['evidence_id']}/facts")

    assert listed.status_code == 200
    assert detail.status_code == 200
    assert facts.status_code == 200
    assert listed.json()[0]["evidenceId"] == str(setup["evidence_id"])
    assert detail.json()["facts"][0]["value"] == "INV-OLD"
    assert facts.json()[0]["fieldKey"] == "invoice_number"
    _assert_no_di_ids(listed.json())
    _assert_no_di_ids(detail.json())
    _assert_no_di_ids(facts.json())
    assert setup["security"].requests == []
    assert setup["di"].document_calls == 0
    assert setup["di"].fact_calls == 0


def test_refresh_updates_status_and_projects_current_di_facts(evidence_read_setup) -> None:
    setup = evidence_read_setup
    client = TestClient(app, raise_server_exceptions=False)
    url = (
        f"/v1/tenants/{setup['tenant_id']}/journeys/{setup['journey_id']}"
        f"/evidence/{setup['evidence_id']}/refresh"
    )

    response = client.post(url)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["evidenceId"] == str(setup["evidence_id"])
    assert body["processingStatus"] == "PROCESSED"
    assert body["verificationStatus"] == "NOT_VERIFIED"
    assert {fact["fieldKey"] for fact in body["facts"]} == {"invoice_number", "amount"}
    amount = next(fact for fact in body["facts"] if fact["fieldKey"] == "amount")
    assert amount["valueType"] == "NUMBER"
    assert amount["value"] == 125000.5
    assert setup["security"].requests == [
        ("di.document.read", "di.document.fields.read"),
    ]
    assert setup["di"].document_calls == 1
    assert setup["di"].fact_calls == 1
    _assert_no_di_ids(body)

    with setup["engine"].begin() as connection:
        evidence = connection.execute(
            text(
                """
                SELECT processing_status_cache, confirmation_status_cache
                FROM auditcore.evidence
                WHERE tenant_id = :tenant_id AND evidence_id = :evidence_id
                """
            ),
            {"tenant_id": setup["tenant_id"], "evidence_id": setup["evidence_id"]},
        ).mappings().one()
        old_fact = connection.execute(
            text(
                """
                SELECT superseded_at_utc
                FROM auditcore.evidence_facts
                WHERE tenant_id = :tenant_id AND evidence_fact_id = :fact_id
                """
            ),
            {"tenant_id": setup["tenant_id"], "fact_id": setup["old_fact_id"]},
        ).mappings().one()
        current_count = connection.execute(
            text(
                """
                SELECT count(*)
                FROM auditcore.evidence_facts
                WHERE tenant_id = :tenant_id
                  AND evidence_id = :evidence_id
                  AND superseded_at_utc IS NULL
                """
            ),
            {"tenant_id": setup["tenant_id"], "evidence_id": setup["evidence_id"]},
        ).scalar_one()

    assert evidence["processing_status_cache"] == "PROCESSED"
    assert evidence["confirmation_status_cache"] == "CONFIRMED"
    assert old_fact["superseded_at_utc"] is not None
    assert current_count == 2


def test_refresh_requires_audit_refresh_permission(evidence_read_setup) -> None:
    setup = evidence_read_setup
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject=setup["actor_id"],
        tenant_id=setup["tenant_id"],
        permissions=("audit.evidence.read", "di.document.read", "di.document.fields.read"),
    )
    client = TestClient(app, raise_server_exceptions=False)
    url = (
        f"/v1/tenants/{setup['tenant_id']}/journeys/{setup['journey_id']}"
        f"/evidence/{setup['evidence_id']}/refresh"
    )

    response = client.post(url)

    assert response.status_code == 403
    assert response.json()["errorCode"] == "VAC-AUTH-002"
    assert setup["security"].requests == []
    assert setup["di"].document_calls == 0
    assert setup["di"].fact_calls == 0
