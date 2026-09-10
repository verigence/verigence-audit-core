"""Regression test for a real, live bug: Delivery's canonical materialization
(registration/finance/invoices/insurance/receipts/payments/scrappage) was
gated behind `changed` -- whether a document's newly-fetched DI facts
differed from what was already durably stored -- inside
uc03_confidence_review_policy._sync_booking_document's DELIVERY branch.

`changed` answers a question about the SOURCE DATA, not about whether the
CURRENT materializer code has ever actually run against it. A document
confirmed once, whose facts have not moved since, shows changed=False on
every later sync -- a duplicate DI webhook redelivery, or the PC's own
`/resync` (uc03_delivery_capture_v2.resync_delivery_capture_v2) -- even
after a materializer fix ships or a whole new canonical table is added.
Symptom actually reported live: documents show Classified/Extracted, but
Journey 360's Invoices/Registration/Payments panels stay empty, and
clicking "Recheck documents" does not fix it either, because resync hits
this exact same gate.

Proves the fix: sync a Delivery RTO Challan document once (creates a
`registration_records` row), delete that row to simulate materialization
never having actually run for it, then sync the SAME document again with
byte-identical facts (changed=False) and assert the row reappears --
materialization must still run.
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core import uc03_confidence_review_policy as confidence_policy
from audit_core.di_client import DiDocument, DiFact


class _FakeSecurityClient:
    def get_service_token(self, *, audience: str) -> str:
        return "fake-token"


class _FakeDiClient:
    def __init__(self) -> None:
        self._documents: dict[str, DiDocument] = {}
        self._facts: dict[str, list[DiFact]] = {}

    def add(self, document: DiDocument, facts: list[DiFact]) -> None:
        self._documents[document.document_id] = document
        self._facts[document.document_id] = facts

    def get_audit_document(self, *, document_id: str, **kwargs) -> DiDocument:
        return self._documents[document_id]

    def get_audit_document_facts(self, *, document_id: str, **kwargs) -> list[DiFact]:
        return self._facts[document_id]


def _fact(canonical_field_id, field_key, value, *, confidence=99.0, version_no=1):
    return DiFact(
        canonical_field_id=canonical_field_id,
        field_key=field_key,
        value=value,
        value_source="EXTRACTION",
        confidence_score=confidence,
        version_no=version_no,
    )


def _confirmed_document(document_id, document_type_key):
    return DiDocument(
        document_id=str(document_id),
        upload_status="COMPLETE",
        processing_status="COMPLETED",
        confirmation_status="CONFIRMED",
        document_type_key=document_type_key,
        verification_state="NOT_VERIFIED",
    )


@pytest.fixture
def delivery_journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-dsg-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"DSG-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"DSG-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'DSG', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"DSG-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"DSG-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"DSG-O-{suffix}"},
        ).scalar_one()
        customer_id = c.execute(
            text("""INSERT INTO auditcore.customers
                (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = c.execute(
            text("""INSERT INTO auditcore.journeys
                (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"DSG-J-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.journey_stage_states
                (tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                 first_started_at_utc, latest_activity_at_utc, version_no)
                VALUES (:t, :j, 'DELIVERY', 'DELIVERY_IN_PROGRESS', 'IN_PROGRESS', 'NOT_EVALUATED',
                        now(), now(), 1)"""),
            {"t": tenant_id, "j": journey_id},
        )
        document_id = uuid4()
        c.execute(
            text(
                """INSERT INTO auditcore.evidence (
                    tenant_id, journey_id, customer_id,
                    di_subject_id, di_document_id,
                    document_type_key, evidence_purpose
                ) VALUES (
                    :t, :j, :cu, :subject, :doc, 'rto_challan', 'DELIVERY'
                )"""
            ),
            {
                "t": tenant_id, "j": journey_id, "cu": customer_id,
                "subject": uuid4(), "doc": document_id,
            },
        )
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_id = journey_id  # type: ignore[attr-defined]
        c.document_id = document_id  # type: ignore[attr-defined]
        yield c
    engine.dispose()


def test_delivery_materialization_reruns_even_when_facts_are_unchanged(delivery_journey) -> None:
    c = delivery_journey
    tenant_id, journey_id, document_id = c.tenant_id, c.journey_id, c.document_id

    di_client = _FakeDiClient()
    di_client.add(
        _confirmed_document(document_id, "rto_challan"),
        [
            _fact(f"regno-{document_id}", "registration_number", "DL7C5573"),
        ],
    )

    def _sync_once():
        confidence_policy._sync_booking_document(
            c,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document_id,
            service_id="di-service",
            security_client=_FakeSecurityClient(),
            di_client=di_client,
            bump_version=True,
            stage_code="DELIVERY",
        )

    def _registration_row():
        return c.execute(
            text(
                """SELECT registration_number FROM auditcore.registration_records
                   WHERE tenant_id=:t AND journey_id=:j"""
            ),
            {"t": tenant_id, "j": journey_id},
        ).mappings().one_or_none()

    # First sync: brand-new facts, changed=True under both old and new code
    # -- materialization runs, a registration_records row appears.
    _sync_once()
    row = _registration_row()
    assert row is not None
    assert row["registration_number"] == "DL7C5573"

    # Simulate "materialization never actually ran for this document" (e.g.
    # it was confirmed before a materializer fix shipped): delete the row
    # the first sync created, without touching the durably-stored facts
    # that fed it.
    c.execute(
        text("DELETE FROM auditcore.registration_records WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    )
    assert _registration_row() is None

    # Second sync: byte-identical facts already durably stored ->
    # changed=False. Materialization must still run -- this is exactly the
    # PC's "Recheck documents" resync scenario, and the fix this test
    # guards: previously, `if stage_code == "DELIVERY" and changed:` meant
    # this second call skipped materialization entirely and the
    # registration_records row would never come back.
    _sync_once()
    row = _registration_row()
    assert row is not None, (
        "Delivery materialization must re-run even when the source facts "
        "are unchanged from the last sync -- gating it on `changed` strands "
        "already-confirmed documents' data out of the canonical tables "
        "forever, with no resync able to reach it."
    )
    assert row["registration_number"] == "DL7C5573"
