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


def _seed_journey(engine, *, tenant_id: str, suffix: str):
    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:c, 'V') RETURNING product_category_id"
            ),
            {"c": f"BCRCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"
            ),
            {"c": f"BCROEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date)
                VALUES (:t, :pc, 'BCR', :o, :cat, CURRENT_DATE)"""
            ),
            {"t": tenant_id, "pc": f"BCR-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:t, :c, 'D') RETURNING dealer_id"
            ),
            {"t": tenant_id, "c": f"BCR-D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"
            ),
            {"t": tenant_id, "d": dealer_id, "c": f"BCR-O-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """INSERT INTO auditcore.customers
                (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"""
            ),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text(
                """INSERT INTO auditcore.journeys
                (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""
            ),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"BCR-J-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """INSERT INTO auditcore.journey_stage_states
                (tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                 first_started_at_utc, latest_activity_at_utc, version_no)
                VALUES (:t, :j, 'BOOKING', 'BOOKING_IN_PROGRESS', 'IN_PROGRESS', 'NOT_EVALUATED',
                        now(), now(), 1)"""
            ),
            {"t": tenant_id, "j": journey_id},
        )
    return journey_id


def _link_evidence(engine, *, tenant_id: str, journey_id, customer_id, document_type_key: str):
    document_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """INSERT INTO auditcore.evidence (
                    tenant_id, journey_id, customer_id,
                    di_subject_id, di_document_id,
                    document_type_key, evidence_purpose
                ) VALUES (
                    :t, :j, :cu, :subject, :doc, :doc_type, 'BOOKING'
                )"""
            ),
            {
                "t": tenant_id,
                "j": journey_id,
                "cu": customer_id,
                "subject": uuid4(),
                "doc": document_id,
                "doc_type": document_type_key,
            },
        )
    return document_id


def _customer_id(engine, *, tenant_id: str, journey_id):
    with engine.begin() as connection:
        return connection.execute(
            text(
                "SELECT customer_id FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"
            ),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one()


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


def test_booking_form_discount_evidence_and_intimation_date() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-bcr-{suffix}"
    journey_id = _seed_journey(engine, tenant_id=tenant_id, suffix=suffix)
    customer_id = _customer_id(engine, tenant_id=tenant_id, journey_id=journey_id)

    booking_form_id = _link_evidence(
        engine, tenant_id=tenant_id, journey_id=journey_id, customer_id=customer_id,
        document_type_key="booking_form",
    )
    di_client = _FakeDiClient()
    di_client.add(
        _confirmed_document(booking_form_id, "booking_form"),
        [
            _fact(f"booking_date-{suffix}", "booking_date", "2026-08-15"),
            _fact(f"corporate_discount-{suffix}", "corporate_discount_amount", "5000"),
            _fact(f"exchange_discount-{suffix}", "exchange_discount_amount", "8000"),
            _fact(f"scrappage_discount-{suffix}", "scrappage_discount_amount", "3000"),
        ],
    )

    with engine.begin() as connection:
        confidence_policy._sync_booking_document(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=booking_form_id,
            service_id="di-service",
            security_client=_FakeSecurityClient(),
            di_client=di_client,
            bump_version=True,
        )

    with engine.begin() as connection:
        state = connection.execute(
            text(
                "SELECT intimation_date FROM auditcore.journey_stage_states "
                "WHERE tenant_id=:t AND journey_id=:j AND stage_code='BOOKING'"
            ),
            {"t": tenant_id, "j": journey_id},
        ).mappings().one()
        findings = connection.execute(
            text(
                "SELECT rule_key, severity, finding_status FROM auditcore.audit_findings "
                "WHERE tenant_id=:t AND journey_id=:j ORDER BY rule_key"
            ),
            {"t": tenant_id, "j": journey_id},
        ).mappings().all()

    assert str(state["intimation_date"]) == "2026-08-15"
    rule_keys = {row["rule_key"] for row in findings}
    assert "BK_DISCOUNT_EVIDENCE_MISSING:corporate_discount" in rule_keys
    assert "BK_DISCOUNT_EVIDENCE_MISSING:exchange_bonus" in rule_keys
    assert "BK_DISCOUNT_EVIDENCE_MISSING:scrappage_discount" in rule_keys
    for row in findings:
        assert row["severity"] == "HIGH"
        assert row["finding_status"] == "OPEN"

    # Corporate ID and a Scrappage Certificate of Deposit now arrive: both
    # self-heal; exchange (no vehicle_rc ever linked) stays open.
    corporate_id_doc = _link_evidence(
        engine, tenant_id=tenant_id, journey_id=journey_id, customer_id=customer_id,
        document_type_key="corporate_id",
    )
    di_client.add(_confirmed_document(corporate_id_doc, "corporate_id"), [])
    scrappage_certificate_doc = _link_evidence(
        engine, tenant_id=tenant_id, journey_id=journey_id, customer_id=customer_id,
        document_type_key="scrappage_certificate_of_deposit",
    )
    di_client.add(_confirmed_document(scrappage_certificate_doc, "scrappage_certificate_of_deposit"), [])
    with engine.begin() as connection:
        confidence_policy._sync_booking_document(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=corporate_id_doc,
            service_id="di-service",
            security_client=_FakeSecurityClient(),
            di_client=di_client,
            bump_version=True,
        )
        from audit_core.uc03_booking_confirmation_rules import (
            record_booking_form_intimation_and_discount_evidence,
        )

        record_booking_form_intimation_and_discount_evidence(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=booking_form_id,
            correlation_id="test",
        )

    with engine.begin() as connection:
        findings = connection.execute(
            text(
                "SELECT rule_key, finding_status FROM auditcore.audit_findings "
                "WHERE tenant_id=:t AND journey_id=:j"
            ),
            {"t": tenant_id, "j": journey_id},
        ).mappings().all()
    status_by_key = {row["rule_key"]: row["finding_status"] for row in findings}
    assert status_by_key["BK_DISCOUNT_EVIDENCE_MISSING:corporate_discount"] == "RESOLVED"
    assert status_by_key["BK_DISCOUNT_EVIDENCE_MISSING:exchange_bonus"] == "OPEN"
    assert status_by_key["BK_DISCOUNT_EVIDENCE_MISSING:scrappage_discount"] == "RESOLVED"

    engine.dispose()


def test_minimum_booking_amount_sequential_aggregation() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-bcr-min-{suffix}"
    journey_id = _seed_journey(engine, tenant_id=tenant_id, suffix=suffix)
    customer_id = _customer_id(engine, tenant_id=tenant_id, journey_id=journey_id)

    di_client = _FakeDiClient()

    def _sync_receipt(*, amount: str, receipt_date: str):
        document_id = _link_evidence(
            engine, tenant_id=tenant_id, journey_id=journey_id, customer_id=customer_id,
            document_type_key="dealer_receipt",
        )
        di_client.add(
            _confirmed_document(document_id, "dealer_receipt"),
            [
                _fact(f"amount-{document_id}", "amount_paid", amount),
                _fact(f"receipt_date-{document_id}", "receipt_date", receipt_date),
                _fact(f"receipt_number-{document_id}", "receipt_number", f"RC-{document_id.hex[:6]}"),
            ],
        )
        with engine.begin() as connection:
            confidence_policy._sync_booking_document(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                document_id=document_id,
                service_id="di-service",
                security_client=_FakeSecurityClient(),
                di_client=di_client,
                bump_version=True,
            )

    def _state():
        with engine.begin() as connection:
            return connection.execute(
                text(
                    "SELECT booking_confirm_date, booking_confirmed_at_utc "
                    "FROM auditcore.journey_stage_states "
                    "WHERE tenant_id=:t AND journey_id=:j AND stage_code='BOOKING'"
                ),
                {"t": tenant_id, "j": journey_id},
            ).mappings().one()

    def _violation_status():
        with engine.begin() as connection:
            return connection.execute(
                text(
                    "SELECT finding_status FROM auditcore.audit_findings "
                    "WHERE tenant_id=:t AND journey_id=:j "
                    "AND rule_key='BK_MIN_BOOKING_AMOUNT_NOT_MET'"
                ),
                {"t": tenant_id, "j": journey_id},
            ).mappings().one_or_none()

    # First receipt alone (5000) is below the default 11000 minimum.
    _sync_receipt(amount="5000", receipt_date="2026-08-20")
    state = _state()
    assert state["booking_confirm_date"] is None
    assert state["booking_confirmed_at_utc"] is None
    violation = _violation_status()
    assert violation is not None
    assert violation["finding_status"] == "OPEN"

    # A second, EARLIER-dated receipt arrives (out of upload order) that
    # pushes the chronological running total (3000 on 08-10, then 5000 on
    # 08-20 => 8000) still short of 11000.
    _sync_receipt(amount="3000", receipt_date="2026-08-10")
    state = _state()
    assert state["booking_confirm_date"] is None
    assert _violation_status()["finding_status"] == "OPEN"

    # A third receipt crosses the threshold: 3000 (08-10) + 5000 (08-20) +
    # 4000 (08-25) = 12000 >= 11000. Confirming date is the 08-25 receipt --
    # the one the chronological running total actually crosses on.
    _sync_receipt(amount="4000", receipt_date="2026-08-25")
    state = _state()
    assert str(state["booking_confirm_date"]) == "2026-08-25"
    assert state["booking_confirmed_at_utc"] is not None
    assert _violation_status()["finding_status"] == "RESOLVED"

    engine.dispose()
