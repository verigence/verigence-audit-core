from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_bearer_token, get_engine, get_principal
from audit_core.di_client import DiDocument, DiSubject
from audit_core.evidence import get_di_client, get_security_oauth_client
from audit_core.main import app
from audit_core.security import Principal


class FakeSecurityClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, ...]] = []

    def exchange_user_token(self, *, subject_token: str, permissions: list[str]) -> str:
        assert subject_token == "user-token"
        self.requests.append(tuple(permissions))
        return f"delegated:{permissions[0]}"


class FakeDiClient:
    def __init__(self) -> None:
        self.subject_id = uuid4()
        self.document_id = uuid4()
        self.uploaded_content: bytes | None = None
        self.upload_token: str | None = None
        self.upload_subject_id: str | None = None
        self.upload_calls = 0
        self.read_calls = 0

    def _document(self, subject_id: str) -> DiDocument:
        return DiDocument(
            document_id=str(self.document_id),
            subject_id=subject_id,
            upload_status="FIT",
            processing_status="PROCESSING",
            confirmation_status="PENDING",
            verification_state="NOT_VERIFIED",
            human_verification_status=None,
            confidence_score=None,
            correlation_id="di-corr-1",
        )

    def create_subject(
        self,
        *,
        token: str,
        tenant_id: str,
        subject_type: str,
        display_name: str | None = None,
    ) -> DiSubject:
        assert token == "delegated:di.subject.create"
        assert tenant_id.startswith("tenant-evidence-")
        assert subject_type == "OTHER"
        assert display_name == "Evidence Customer"
        return DiSubject(subject_id=str(self.subject_id), status="ACTIVE")

    def upload_document(
        self,
        *,
        token: str,
        tenant_id: str,
        subject_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        source_channel: str,
        document_type_key: str | None = None,
        captured_at: str | None = None,
        source_reference: str | None = None,
        replaces_document_id: str | None = None,
    ) -> DiDocument:
        assert tenant_id.startswith("tenant-evidence-")
        assert filename == "booking.pdf"
        assert content_type == "application/pdf"
        assert source_channel == "API"
        assert document_type_key == "BOOKING_FORM"
        assert captured_at is None
        assert source_reference is None
        assert replaces_document_id is None
        self.upload_calls += 1
        self.uploaded_content = content
        self.upload_token = token
        self.upload_subject_id = subject_id
        return self._document(subject_id)

    def get_document(
        self,
        *,
        token: str,
        tenant_id: str,
        subject_id: str,
        document_id: str,
    ) -> DiDocument:
        assert token == "delegated:di.document.read"
        assert tenant_id.startswith("tenant-evidence-")
        assert document_id == str(self.document_id)
        self.read_calls += 1
        return self._document(subject_id)


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
        permissions=(
            "audit.evidence.upload",
            "di.subject.create",
            "di.document.upload",
            "di.document.read",
        ),
    )
    app.dependency_overrides[get_bearer_token] = lambda: "user-token"
    app.dependency_overrides[get_security_oauth_client] = lambda: security_client
    app.dependency_overrides[get_di_client] = lambda: di_client
    try:
        yield engine, tenant_id, journey_id, customer_id, actor_id, security_client, di_client
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


def test_upload_facade_returns_only_audit_core_evidence_id_and_persists_no_binary(
    evidence_setup,
) -> None:
    engine, tenant_id, journey_id, customer_id, actor_id, security_client, di_client = (
        evidence_setup
    )
    raw_document = b"UNIQUE-RAW-DOCUMENT-BYTES-DO-NOT-PERSIST"
    client = TestClient(app, raise_server_exceptions=False)

    response = _post_evidence(
        client,
        tenant_id=tenant_id,
        journey_id=journey_id,
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
    assert body["journeyId"] == str(journey_id)
    assert "diSubjectId" not in body
    assert "diDocumentId" not in body
    assert security_client.requests == [
        ("di.subject.create",),
        ("di.document.upload",),
    ]
    assert di_client.uploaded_content == raw_document
    assert di_client.upload_token == "delegated:di.document.upload"
    assert di_client.upload_subject_id == str(di_client.subject_id)

    with engine.begin() as connection:
        evidence = connection.execute(
            text(
                """
                SELECT evidence_id, customer_id, di_subject_id, di_document_id,
                       evidence_purpose, document_type_key,
                       processing_status_cache, verification_status_cache
                FROM auditcore.evidence
                WHERE tenant_id = :tenant_id AND evidence_id = :evidence_id
                """
            ),
            {"tenant_id": tenant_id, "evidence_id": evidence_id},
        ).mappings().one()
        mapping = connection.execute(
            text(
                """
                SELECT customer_id, di_subject_id, di_subject_type
                FROM auditcore.di_subject_mappings
                WHERE tenant_id = :tenant_id AND customer_id = :customer_id
                  AND mapping_status = 'ACTIVE'
                """
            ),
            {"tenant_id": tenant_id, "customer_id": customer_id},
        ).mappings().one()
        operation = connection.execute(
            text(
                """
                SELECT operation_status, evidence_id, di_subject_id, di_document_id
                FROM auditcore.evidence_ingestion_operations
                WHERE tenant_id = :tenant_id AND idempotency_key = 'evidence-key-0001'
                """
            ),
            {"tenant_id": tenant_id},
        ).mappings().one()

    assert evidence["customer_id"] == customer_id
    assert evidence["di_subject_id"] == di_client.subject_id
    assert evidence["di_document_id"] == di_client.document_id
    assert evidence["evidence_purpose"] == "BOOKING_CAPTURE"
    assert evidence["document_type_key"] == "BOOKING_FORM"
    assert evidence["processing_status_cache"] == "PROCESSING"
    assert evidence["verification_status_cache"] == "NOT_VERIFIED"
    assert mapping["customer_id"] == customer_id
    assert mapping["di_subject_id"] == di_client.subject_id
    assert mapping["di_subject_type"] == "OTHER"
    assert operation["operation_status"] == "LINKED"
    assert operation["evidence_id"] == evidence_id
    assert operation["di_subject_id"] == di_client.subject_id
    assert operation["di_document_id"] == di_client.document_id
    assert actor_id


def test_replay_returns_same_evidence_without_duplicate_di_upload(evidence_setup) -> None:
    engine, tenant_id, journey_id, _, _, security_client, di_client = evidence_setup
    client = TestClient(app, raise_server_exceptions=False)
    content = b"IDEMPOTENT-DOCUMENT"

    first = _post_evidence(
        client,
        tenant_id=tenant_id,
        journey_id=journey_id,
        key="evidence-replay-0001",
        content=content,
    )
    requests_after_first = list(security_client.requests)
    second = _post_evidence(
        client,
        tenant_id=tenant_id,
        journey_id=journey_id,
        key="evidence-replay-0001",
        content=content,
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()
    assert di_client.upload_calls == 1
    assert di_client.read_calls == 0
    assert security_client.requests == requests_after_first
    with engine.begin() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM auditcore.evidence "
                "WHERE tenant_id = :tenant_id AND journey_id = :journey_id"
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
    assert count == 1


def test_di_accepted_outer_failure_is_recovered_without_second_upload(evidence_setup) -> None:
    engine, tenant_id, journey_id, _, _, security_client, di_client = evidence_setup
    client = TestClient(app, raise_server_exceptions=False)
    content = b"RECOVERY-DOCUMENT"

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE OR REPLACE FUNCTION auditcore.test_fail_evidence_link()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.tenant_id = :tenant_id THEN
                        RAISE EXCEPTION 'simulated outer evidence link failure';
                    END IF;
                    RETURN NEW;
                END;
                $$
                """
            ),
            {"tenant_id": tenant_id},
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
            tenant_id=tenant_id,
            journey_id=journey_id,
            key="evidence-recovery-0001",
            content=content,
        )
        assert failed.status_code == 500
        assert di_client.upload_calls == 1

        with engine.begin() as connection:
            operation = connection.execute(
                text(
                    """
                    SELECT operation_status, di_subject_id, di_document_id, evidence_id
                    FROM auditcore.evidence_ingestion_operations
                    WHERE tenant_id = :tenant_id
                      AND idempotency_key = 'evidence-recovery-0001'
                    """
                ),
                {"tenant_id": tenant_id},
            ).mappings().one()
        assert operation["operation_status"] == "DI_ACCEPTED"
        assert operation["di_subject_id"] == di_client.subject_id
        assert operation["di_document_id"] == di_client.document_id
        assert operation["evidence_id"] is None
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP TRIGGER IF EXISTS test_fail_evidence_link ON auditcore.evidence"))
            connection.execute(text("DROP FUNCTION IF EXISTS auditcore.test_fail_evidence_link()"))

    recovered = _post_evidence(
        client,
        tenant_id=tenant_id,
        journey_id=journey_id,
        key="evidence-recovery-0001",
        content=content,
    )

    assert recovered.status_code == 201, recovered.text
    assert di_client.upload_calls == 1
    assert di_client.read_calls == 1
    assert security_client.requests[-1] == ("di.document.read",)
    with engine.begin() as connection:
        operation = connection.execute(
            text(
                """
                SELECT operation_status, evidence_id
                FROM auditcore.evidence_ingestion_operations
                WHERE tenant_id = :tenant_id
                  AND idempotency_key = 'evidence-recovery-0001'
                """
            ),
            {"tenant_id": tenant_id},
        ).mappings().one()
        evidence_count = connection.execute(
            text(
                "SELECT count(*) FROM auditcore.evidence "
                "WHERE tenant_id = :tenant_id AND journey_id = :journey_id"
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
    assert operation["operation_status"] == "LINKED"
    assert operation["evidence_id"] == UUID(recovered.json()["evidenceId"])
    assert evidence_count == 1


def test_same_idempotency_key_with_different_document_is_conflict(evidence_setup) -> None:
    _, tenant_id, journey_id, _, _, _, di_client = evidence_setup
    client = TestClient(app, raise_server_exceptions=False)

    first = _post_evidence(
        client,
        tenant_id=tenant_id,
        journey_id=journey_id,
        key="evidence-conflict-0001",
        content=b"FIRST-DOCUMENT",
    )
    second = _post_evidence(
        client,
        tenant_id=tenant_id,
        journey_id=journey_id,
        key="evidence-conflict-0001",
        content=b"DIFFERENT-DOCUMENT",
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["errorCode"] == "VAC-CONFLICT-003"
    assert di_client.upload_calls == 1


def test_upload_facade_requires_audit_evidence_permission(evidence_setup) -> None:
    _, tenant_id, journey_id, _, actor_id, security_client, di_client = evidence_setup
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject=actor_id,
        tenant_id=tenant_id,
        permissions=("di.subject.create", "di.document.upload"),
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        f"/v1/tenants/{tenant_id}/journeys/{journey_id}/evidence",
        headers={"Idempotency-Key": "evidence-key-0002"},
        data={"evidencePurpose": "BOOKING_CAPTURE"},
        files={"file": ("booking.pdf", b"bytes", "application/pdf")},
    )

    assert response.status_code == 403
    assert response.json()["errorCode"] == "VAC-AUTH-002"
    assert security_client.requests == []
    assert di_client.uploaded_content is None
