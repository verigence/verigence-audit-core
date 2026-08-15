from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_connection, get_engine, get_principal
from audit_core.di_client import DiDocument, DiSubject
from audit_core.evidence import get_di_client, get_security_oauth_client
from audit_core.main import app
from audit_core.security import Principal


@dataclass
class FakeSecurityClient:
    calls: list[tuple[str, ...]]

    def exchange_user_token(self, *, subject_token: str, permissions: list[str]) -> str:
        assert subject_token == "user-token"
        self.calls.append(tuple(permissions))
        return "service-token"


@dataclass
class FakeDiClient:
    tenant_id: str
    subject_id: str
    document_id: str
    create_calls: int = 0
    upload_calls: int = 0

    def create_subject(self, **kwargs) -> DiSubject:
        assert kwargs["token"] == "service-token"
        assert kwargs["tenant_id"] == self.tenant_id
        self.create_calls += 1
        return DiSubject(subject_id=self.subject_id, status="ACTIVE")

    def upload_document(self, **kwargs) -> DiDocument:
        assert kwargs["token"] == "service-token"
        assert kwargs["tenant_id"] == self.tenant_id
        assert kwargs["subject_id"] == self.subject_id
        self.upload_calls += 1
        return DiDocument(
            document_id=self.document_id,
            subject_id=self.subject_id,
            upload_status="ACCEPTED",
            processing_status="COMPLETED",
            confirmation_status="CONFIRMED",
            verification_state="NOT_VERIFIED",
            human_verification_status=None,
            confidence_score=0.97,
            correlation_id="di-e2e",
        )

    def get_document(self, **kwargs) -> DiDocument:
        return self.upload_document(**kwargs)


def test_critical_journey_uses_audit_core_only_and_keeps_delivery_independent() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for critical end-to-end audit journey")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-e2e-{suffix}"
    pc_id = f"pc-{suffix}"
    tl_id = f"tl-{suffix}"
    subject_id = str(uuid4())
    document_id = str(uuid4())
    security_client = FakeSecurityClient(calls=[])
    di_client = FakeDiClient(
        tenant_id=tenant_id,
        subject_id=subject_id,
        document_id=document_id,
    )

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"E2ECAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'E2E OEM') RETURNING oem_id"
            ),
            {"code": f"E2EOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'E2E Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"E2EP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.business_status_codes (
                    tenant_id, domain_key, status_code, status_label
                ) VALUES (:tenant_id, 'DELIVERY', 'DELIVERED', 'Delivered')
                """
            ),
            {"tenant_id": tenant_id},
        )

    def connection_override():
        with engine.begin() as connection:
            yield connection

    app.dependency_overrides[get_connection] = connection_override
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_security_oauth_client] = lambda: security_client
    app.dependency_overrides[get_di_client] = lambda: di_client
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject=pc_id,
        tenant_id=tenant_id,
        permissions=(
            "audit.evidence.upload",
            "audit.delivery.read",
            "audit.delivery.write",
            "audit.journey.read",
            "audit.journey.update",
            "audit.journey.submit",
        ),
    )
    client = TestClient(app, raise_server_exceptions=False)
    auth_headers = {"Authorization": "Bearer user-token"}

    try:
        dealer = client.post(
            f"/v1/tenants/{tenant_id}/dealers",
            json={"dealerCode": f"D-{suffix}", "dealerName": "E2E Dealer"},
        )
        assert dealer.status_code == 201, dealer.text
        dealer_id = dealer.json()["dealerId"]

        outlet = client.post(
            f"/v1/tenants/{tenant_id}/dealers/{dealer_id}/outlets",
            json={"outletCode": f"O-{suffix}", "outletName": "E2E Outlet"},
        )
        assert outlet.status_code == 201, outlet.text
        outlet_id = outlet.json()["outletId"]

        customer = client.post(
            f"/v1/tenants/{tenant_id}/outlets/{outlet_id}/customers",
            json={"customerTypeCode": "RETAIL", "displayName": "E2E Customer"},
        )
        assert customer.status_code == 201, customer.text
        customer_id = customer.json()["customerId"]

        journey = client.post(
            f"/v1/tenants/{tenant_id}/customers/{customer_id}/journeys",
            json={"journeyReference": f"J-{suffix}"},
        )
        assert journey.status_code == 201, journey.text
        journey_id = journey.json()["journeyId"]

        with engine.begin() as connection:
            for actor_id, role_code in ((pc_id, "PC"), (tl_id, "TL")):
                connection.execute(
                    text(
                        """
                        INSERT INTO auditcore.business_assignments (
                            tenant_id, security_actor_id, business_role_code,
                            dealer_id, outlet_id
                        ) VALUES (
                            :tenant_id, :actor_id, :role_code,
                            :dealer_id, :outlet_id
                        )
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "actor_id": actor_id,
                        "role_code": role_code,
                        "dealer_id": dealer_id,
                        "outlet_id": outlet_id,
                    },
                )

        evidence = client.post(
            f"/v1/tenants/{tenant_id}/journeys/{journey_id}/evidence",
            headers={
                **auth_headers,
                "Idempotency-Key": f"evidence-{suffix}",
                "X-Correlation-ID": f"e2e-{suffix}",
            },
            data={
                "evidencePurpose": "DELIVERY_AUDIT",
                "documentTypeKey": "GATE_PASS",
            },
            files={"file": ("gate-pass.pdf", b"e2e-document", "application/pdf")},
        )
        assert evidence.status_code == 201, evidence.text
        evidence_id = evidence.json()["evidenceId"]
        assert "di" not in " ".join(evidence.json().keys()).lower()
        assert di_client.create_calls == 1
        assert di_client.upload_calls == 1

        delivery = client.put(
            f"/v1/tenants/{tenant_id}/journeys/{journey_id}/delivery",
            json={
                "actualDeliveryStatusCode": "DELIVERED",
                "actualDeliveredAt": "2026-08-15T12:00:00Z",
                "statusSource": "EVIDENCE",
                "sourceEvidenceId": evidence_id,
            },
        )
        assert delivery.status_code == 200, delivery.text
        assert delivery.json()["actualDeliveryStatusCode"] == "DELIVERED"
        assert delivery.json()["statusLabel"] == "Delivered"

        started = client.post(f"/v1/tenants/{tenant_id}/journeys/{journey_id}/audit/start")
        assert started.status_code == 200, started.text
        assert started.json()["auditState"] == "IN_PROGRESS"

        submitted = client.post(
            f"/v1/tenants/{tenant_id}/journeys/{journey_id}/audit/submit",
            headers={"Idempotency-Key": f"submit-{suffix}"},
        )
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["auditState"] == "PC_SUBMITTED"

        app.dependency_overrides[get_principal] = lambda: Principal(
            subject=tl_id,
            tenant_id=tenant_id,
            permissions=("audit.review.read", "audit.review.decide", "audit.delivery.read"),
        )
        reviewed = client.post(
            f"/v1/tenants/{tenant_id}/journeys/{journey_id}/review-decisions",
            headers={"Idempotency-Key": f"review-{suffix}"},
            json={
                "decision": "NO_BREACH",
                "reviewerRoleCode": "TL",
                "remarks": "Evidence and observed delivery fact reviewed.",
            },
        )
        assert reviewed.status_code == 201, reviewed.text
        assert reviewed.json()["decision"] == "NO_BREACH"

        final_delivery = client.get(
            f"/v1/tenants/{tenant_id}/journeys/{journey_id}/delivery"
        )
        assert final_delivery.status_code == 200, final_delivery.text
        assert final_delivery.json()["actualDeliveryStatusCode"] == "DELIVERED"

        with engine.begin() as connection:
            final_state = connection.execute(
                text(
                    """
                    SELECT audit_state, audit_outcome
                    FROM auditcore.journeys
                    WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id},
            ).mappings().one()
        assert final_state["audit_state"] == "REVIEW_COMPLETE"
        assert final_state["audit_outcome"] == "NO_BREACH"
        assert security_client.calls == [
            ("di.subject.create",),
            ("di.document.upload",),
        ]
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
