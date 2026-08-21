from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_engine, get_principal
from audit_core.di_client import DiDocument, DiSubject
from audit_core.evidence import get_di_client, get_security_oauth_client
from audit_core.main import app
from audit_core.security import Principal


class FakeSecurityClient:
    def __init__(self) -> None:
        self.audiences: list[str] = []

    def get_service_token(self, *, audience: str) -> str:
        self.audiences.append(audience)
        return "service-integration-di-token"


class FakeDiClient:
    def __init__(self) -> None:
        self.subject_id = uuid4()
        self.document_id = uuid4()
        self.subject_calls = 0
        self.context_calls: list[dict[str, str]] = []
        self.upload_calls = 0
        self.read_calls = 0
        self.uploaded_content: bytes | None = None

    def _document(self) -> DiDocument:
        return DiDocument(
            document_id=str(self.document_id),
            upload_status="FIT",
            processing_status="PROCESSING",
            confirmation_status="PENDING",
            confidence_score=None,
            document_type_key="BOOKING_FORM",
            registered_at=None,
            verification_state="NOT_VERIFIED",
        )

    def create_subject(
        self,
        *,
        token: str,
        tenant_id: str,
        subject_type: str,
        display_name: str | None = None,
    ) -> DiSubject:
        assert token == "service-integration-di-token"
        assert tenant_id.startswith("tenant-evidence-")
        assert subject_type == "OTHER"
        assert display_name == "Evidence Customer"
        self.subject_calls += 1
        return DiSubject(subject_id=str(self.subject_id), status="ACTIVE")

    def ensure_audit_storage_context(
        self,
        *,
        token: str,
        tenant_id: str,
        external_context_ref: str,
        subject_id: str,
        dealer_id: str,
        outlet_id: str,
        customer_id: str,
        project_name: str,
        dealer_name: str,
        outlet_name: str,
        customer_name: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        assert token == "service-integration-di-token"
        self.context_calls.append(
            {
                "tenantId": tenant_id,
                "externalContextRef": external_context_ref,
                "subjectId": subject_id,
                "dealerId": dealer_id,
                "outletId": outlet_id,
                "customerId": customer_id,
                "projectName": project_name,
                "dealerName": dealer_name,
                "outletName": outlet_name,
                "customerName": customer_name,
                "idempotencyKey": idempotency_key,
            }
        )
        return {"storageContextId": str(uuid4())}

    def upload_audit_document(
        self,
        *,
        token: str,
        tenant_id: str,
        external_context_ref: str,
        filename: str,
        content: bytes,
        content_type: str,
        document_type_key: str | None = None,
    ) -> DiDocument:
        assert token == "service-integration-di-token"
        assert tenant_id.startswith("tenant-evidence-")
        assert external_context_ref.startswith("audit-")
        assert filename == "booking.pdf"
        assert content_type == "application/pdf"
        assert document_type_key == "BOOKING_FORM"
        self.upload_calls += 1
        self.uploaded_content = content
        return self._document()

    def get_audit_document(
        self,
        *,
        token: str,
        tenant_id: str,
        external_context_ref: str,
        document_id: str,
    ) -> DiDocument:
        assert token == "service-integration-di-token"
        assert tenant_id.startswith("tenant-evidence-")
        assert external_context_ref.startswith("audit-")
        assert document_id == str(self.document_id)
        self.read_calls += 1
        return self._document()


@pytest.fixture
def evidence_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for evidence integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-evidence-{suffix}"
    actor_id = f"pc-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"ECAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Evidence OEM') RETURNING oem_id"
            ),
            {"code": f"EOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Evidence Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"EP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Evidence Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"ED-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Evidence Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"EO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Evidence Customer'
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
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'EVIDENCE-JOURNEY'
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
    di_client = FakeDiClient()

    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject=actor_id,
        tenant_id=tenant_id,
        permissions=("audit.evidence.upload",),
    )
    app.dependency_overrides[get_security_oauth_client] = lambda: security_client
    app.dependency_overrides[get_di_client] = lambda: di_client
    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "customer_id": customer_id,
            "dealer_id": dealer_id,
            "outlet_id": outlet_id,
            "actor_id": actor_id,
            "security": security_client,
            "di": di_client,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _post_evidence(
    client: TestClient,
    *,
    tenant_id: str,
    journey_id: UUID,
    key: str,
    content: bytes,
):
    return client.post(
        f"/v1/tenants/{tenant_id}/journeys/{journey_id}/evidence",
        headers={"Idempotency-Key": key},
        data={
            "evidencePurpose": "BOOKING_CAPTURE",
            "documentTypeKey": "BOOKING_FORM",
        },
        files={"file": ("booking.pdf", content, "application/pdf")},
    )


def test_upload_uses_one_di_service_token_and_trusted_storage_context(evidence_setup) -> None:
    setup = evidence_setup
    raw_document = b"UNIQUE-RAW-DOCUMENT-BYTES-DO-NOT-PERSIST"
    response = _post_evidence(
        TestClient(app, raise_server_exceptions=False),
        tenant_id=setup["tenant_id"],
        journey_id=setup["journey_id"],
        key="evidence-key-0001",
        content=raw_document,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    evidence_id = UUID(body["evidenceId"])
    assert set(body) == {
        "evidenceId",
        "journeyId",
        "documentTypeKey",
        "evidencePurpose",
        "processingStatus",
        "verificationStatus",
        "createdAtUtc",
    }
    assert "diSubjectId" not in body
    assert "diDocumentId" not in body
    assert setup["security"].audiences == ["di"]
    assert setup["di"].subject_calls == 1
    assert setup["di"].upload_calls == 1
    assert setup["di"].uploaded_content == raw_document
    assert len(setup["di"].context_calls) == 1

    context = setup["di"].context_calls[0]
    assert context["tenantId"] == setup["tenant_id"]
    assert context["dealerId"] == str(setup["dealer_id"])
    assert context["outletId"] == str(setup["outlet_id"])
    assert context["customerId"] == str(setup["customer_id"])
    assert context["projectName"] == "Evidence Project"
    assert context["dealerName"] == "Evidence Dealer"
    assert context["outletName"] == "Evidence Outlet"
    assert context["customerName"] == "Evidence Customer"
    assert context["idempotencyKey"] == "evidence-key-0001:context"

    with setup["engine"].begin() as connection:
        evidence = connection.execute(
            text(
                """
                SELECT customer_id, di_subject_id, di_document_id,
                       processing_status_cache, verification_status_cache
                FROM auditcore.evidence
                WHERE tenant_id=:tenant_id AND evidence_id=:evidence_id
                """
            ),
            {"tenant_id": setup["tenant_id"], "evidence_id": evidence_id},
        ).mappings().one()
        operation = connection.execute(
            text(
                """
                SELECT operation_status, evidence_id, di_subject_id, di_document_id
                FROM auditcore.evidence_ingestion_operations
                WHERE tenant_id=:tenant_id AND idempotency_key='evidence-key-0001'
                """
            ),
            {"tenant_id": setup["tenant_id"]},
        ).mappings().one()

    assert evidence["customer_id"] == setup["customer_id"]
    assert evidence["di_subject_id"] == setup["di"].subject_id
    assert evidence["di_document_id"] == setup["di"].document_id
    assert evidence["processing_status_cache"] == "PROCESSING"
    assert evidence["verification_status_cache"] == "NOT_VERIFIED"
    assert operation["operation_status"] == "LINKED"
    assert operation["evidence_id"] == evidence_id


def test_replay_returns_cached_evidence_without_second_token_or_upload(evidence_setup) -> None:
    setup = evidence_setup
    client = TestClient(app, raise_server_exceptions=False)
    content = b"IDEMPOTENT-DOCUMENT"

    first = _post_evidence(
        client,
        tenant_id=setup["tenant_id"],
        journey_id=setup["journey_id"],
        key="evidence-replay-0001",
        content=content,
    )
    second = _post_evidence(
        client,
        tenant_id=setup["tenant_id"],
        journey_id=setup["journey_id"],
        key="evidence-replay-0001",
        content=content,
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()
    assert setup["security"].audiences == ["di"]
    assert setup["di"].subject_calls == 1
    assert len(setup["di"].context_calls) == 1
    assert setup["di"].upload_calls == 1
    assert setup["di"].read_calls == 0


def test_di_accepted_outer_failure_recovers_through_same_context_without_reupload(
    evidence_setup,
) -> None:
    setup = evidence_setup
    client = TestClient(app, raise_server_exceptions=False)
    content = b"RECOVERY-DOCUMENT"

    with setup["engine"].begin() as connection:
        connection.execute(
            text(
                """
                CREATE OR REPLACE FUNCTION auditcore.test_fail_evidence_link()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'simulated outer evidence link failure';
                END;
                $$
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TRIGGER test_fail_evidence_link
                BEFORE INSERT ON auditcore.evidence
                FOR EACH ROW EXECUTE FUNCTION auditcore.test_fail_evidence_link()
                """
            )
        )

    try:
        failed = _post_evidence(
            client,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            key="evidence-recovery-0001",
            content=content,
        )
        assert failed.status_code == 500
        assert setup["di"].upload_calls == 1
        with setup["engine"].begin() as connection:
            operation = connection.execute(
                text(
                    """
                    SELECT operation_status, di_subject_id, di_document_id, evidence_id
                    FROM auditcore.evidence_ingestion_operations
                    WHERE tenant_id=:tenant_id
                      AND idempotency_key='evidence-recovery-0001'
                    """
                ),
                {"tenant_id": setup["tenant_id"]},
            ).mappings().one()
        assert operation["operation_status"] == "DI_ACCEPTED"
        assert operation["di_document_id"] == setup["di"].document_id
        assert operation["evidence_id"] is None
    finally:
        with setup["engine"].begin() as connection:
            connection.execute(
                text("DROP TRIGGER IF EXISTS test_fail_evidence_link ON auditcore.evidence")
            )
            connection.execute(text("DROP FUNCTION IF EXISTS auditcore.test_fail_evidence_link()"))

    recovered = _post_evidence(
        client,
        tenant_id=setup["tenant_id"],
        journey_id=setup["journey_id"],
        key="evidence-recovery-0001",
        content=content,
    )

    assert recovered.status_code == 201, recovered.text
    assert setup["security"].audiences == ["di", "di"]
    assert setup["di"].upload_calls == 1
    assert setup["di"].read_calls == 1
    assert len(setup["di"].context_calls) == 2
    assert (
        setup["di"].context_calls[0]["externalContextRef"]
        == setup["di"].context_calls[1]["externalContextRef"]
    )
    assert (
        setup["di"].context_calls[0]["customerId"]
        == setup["di"].context_calls[1]["customerId"]
    )