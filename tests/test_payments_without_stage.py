"""Receipts carry no Booking or Delivery stage (decision 2026-10-19).

Found live 2026-10-19: the Booking receipt writer stamped
payment_stage='BOOKING' and the Delivery receipt writer 'DELIVERY' (which also
linked a delivery_id). A receipt both writers saw was flipped between the two,
and the flip back to 'BOOKING' was refused by the database ("Only a
Delivery-stage payment may reference a Delivery"). That failed the whole
Journey's document sync on every retry, and the P2 reconcile rescheduled it
every five minutes for days.

The rules now (product owner): Booking is confirmed once the receipts, taken
first by receipt date, reach the minimum booking amount; which receipts make
up the Booking payment is derived (uc03_p2_stage.receipt_split), never stored.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_evidence,
    add_receipt_payment,
    create_p2_journey,
    database_engine,
    set_minimum_booking_amount,
)
from sqlalchemy import text

from audit_core import uc03_p2_journey360 as journey360
from audit_core.db import set_tenant_context
from audit_core.uc03_booking_confirmation_rules import evaluate_minimum_booking_payment
from audit_core.uc03_delivery_review_materialization import (
    materialize_delivery_receipts,
)
from audit_core.uc03_v2_review_materialization import (
    materialize_reviewed_booking_receipts,
)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="nostage")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _receipt_document(document_id: UUID, evidence_id: UUID, **fields: str) -> SimpleNamespace:
    return SimpleNamespace(
        documentId=document_id,
        evidenceId=evidence_id,
        documentTypeKey="dealer_receipt",
        extractionState="READY",
        fields=[SimpleNamespace(fieldKey=k, value=v, confidenceScore=99.0) for k, v in fields.items()],
    )


def _add_delivery(journey) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.deliveries (tenant_id, journey_id) VALUES (:t, :j)"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )


def _payments(journey) -> list[dict]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [dict(r) for r in connection.execute(
            text("SELECT payment_id, amount, payment_stage, delivery_id, source_di_document_id "
                 "FROM auditcore.payments WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).mappings().all()]


def test_a_receipt_both_writers_see_is_one_payment_and_never_fails(journey):
    """The live failure: the Delivery writer then the Booking writer on the
    same receipt document, on a Journey that has a Delivery."""
    _add_delivery(journey)
    document_id = uuid4()
    evidence_id = add_evidence(journey, di_document_id=document_id, document_type_key="dealer_receipt")
    receipt = _receipt_document(
        document_id, evidence_id,
        amount_paid="25000", receipt_number="RC-77", receipt_date="2026-10-01",
    )

    for _ in range(2):  # the reconcile ran both, over and over
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            materialize_delivery_receipts(
                connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                documents=[receipt], actor_id="tester",
            )
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            materialize_reviewed_booking_receipts(
                connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                documents=[receipt], rejected_review_keys=set(), actor_id=None,
            )

    rows = _payments(journey)
    assert len(rows) == 1
    assert rows[0]["amount"] == Decimal("25000.00")
    assert rows[0]["source_di_document_id"] == document_id
    assert rows[0]["payment_stage"] == "UNSPECIFIED"
    assert rows[0]["delivery_id"] is None


def test_second_pass_with_the_same_values_changes_nothing(journey):
    document_id = uuid4()
    evidence_id = add_evidence(journey, di_document_id=document_id, document_type_key="dealer_receipt")
    receipt = _receipt_document(document_id, evidence_id, amount_paid="11000", receipt_date="2026-10-02")
    results = []
    for writer in ("booking", "delivery", "booking"):
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            if writer == "booking":
                results.append(materialize_reviewed_booking_receipts(
                    connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                    documents=[receipt], rejected_review_keys=set(), actor_id=None,
                ))
            else:
                results.append(materialize_delivery_receipts(
                    connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                    documents=[receipt], actor_id="tester",
                ))
    assert [r["created"] for r in results] == [1, 0, 0]
    assert [r["updated"] for r in results] == [0, 0, 0]
    assert [r["unchanged"] for r in results] == [0, 1, 1]


def _booking_confirm_date(journey) -> date | None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT booking_confirm_date FROM auditcore.journey_stage_states "
                 "WHERE tenant_id=:t AND journey_id=:j AND stage_code='BOOKING'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one()


def _stamp(journey, document_id: UUID, stage: str) -> None:
    """Legacy data: a stamp left by the old writers (now ignored)."""
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.payments SET payment_stage=:s "
                 "WHERE tenant_id=:t AND source_di_document_id=:d"),
            {"s": stage, "t": journey.tenant_id, "d": document_id},
        )


def test_minimum_booking_amount_counts_every_receipt_first_by_date(journey):
    set_minimum_booking_amount(journey, "21000")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code) "
                 "VALUES (:t, :j, 'BOOKING')"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
    # Uploaded in this order, but receipt-date order is 1 Oct, 5 Oct, 9 Oct.
    later = add_receipt_payment(journey, amount="50000", receipt_number="R-3", receipt_date="2026-10-09")
    add_receipt_payment(journey, amount="11000", receipt_number="R-1", receipt_date="2026-10-01")
    second = add_receipt_payment(journey, amount="10000", receipt_number="R-2", receipt_date="2026-10-05")
    # Stamps the old writers left must not matter any more.
    _stamp(journey, second, "DELIVERY")
    _stamp(journey, later, "DELIVERY")

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        evaluate_minimum_booking_payment(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, correlation_id="t",
        )
    # 11,000 on 1 Oct, then 21,000 on 5 Oct reaches the minimum.
    assert _booking_confirm_date(journey) == date(2026, 10, 5)


def test_journey360_splits_receipts_by_minimum_not_by_stamp(journey):
    set_minimum_booking_amount(journey, "21000")
    first = add_receipt_payment(journey, amount="11000", receipt_number="R-1", receipt_date="2026-10-01")
    add_receipt_payment(journey, amount="10000", receipt_number="R-2", receipt_date="2026-10-05")
    third = add_receipt_payment(journey, amount="50000", receipt_number="R-3", receipt_date="2026-10-09")
    # Old stamps, deliberately the wrong way round.
    _stamp(journey, first, "DELIVERY")
    _stamp(journey, third, "BOOKING")

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        view = journey360.payments(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)

    assert view["byStage"] == {"BOOKING": "21000.00", "DELIVERY": "50000.00"}
    stage_by_number = {item["receiptNumber"]: item["stage"] for item in view["items"]}
    assert stage_by_number == {"R-1": "BOOKING", "R-2": "BOOKING", "R-3": "DELIVERY"}
    assert view["receiptsTotal"] == "71000.00"
