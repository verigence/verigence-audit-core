"""A receipt sits under Booking or Delivery on the document checklist by the
minimum booking amount, never by the area it was uploaded under (product owner
2026-11-12: "any payment more than the minimum payment has to be considered as
part of the delivery and there are no two buckets ... why same issue again")."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import (
    AllowAllAuthorization,
    add_ready_document,
    add_receipt_payment,
    create_p2_journey,
    database_engine,
    principal,
    set_minimum_booking_amount,
)

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_api import list_documents


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2rc")
    set_minimum_booking_amount(created, "11000")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _receipt(journey, amount, number, on, *, area="BOOKING"):
    document_id = add_ready_document(journey, "dealer_receipt", process_area=area, amount_paid=amount,
                                     receipt_number=number, receipt_date=on)
    add_receipt_payment(journey, amount=amount, receipt_number=number, receipt_date=on, di_document_id=document_id)
    return str(document_id)


def _listing(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return list_documents(tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                              human_principal=principal(journey), authorization_client=AllowAllAuthorization(),
                              connection=connection)


def test_receipts_all_uploaded_under_booking_are_placed_by_the_minimum_booking_amount(journey) -> None:
    """Live, SHI-YK-261111-001: six receipts in one delivery file, all filed under Booking. The first
    (Rs 11,001) reaches the minimum of Rs 11,000; the other five are the delivery's -- yet all six sat on
    the Booking Payment Receipt card and the Delivery Payment Receipt card read "Missing"."""
    first = _receipt(journey, "11001", "KH3S/26-27/0763", "2026-09-30")
    later = [_receipt(journey, amount, number, on) for amount, number, on in (
        ("80000", "KH3S/26-27/1149", "2026-10-19"), ("100000", "KH3S/26-27/1170", "2026-10-22"),
        ("832222", "KH3S/26-27/1219", "2026-10-23"))]
    listing = _listing(journey)
    checklist = {(c["stage"], c["templateKey"]): c for c in listing["checklist"]}
    booking, delivery = checklist[("BOOKING", "dealer_receipt")], checklist[("DELIVERY", "payment_receipt")]
    assert booking["documentIds"] == [first] and booking["status"] == "RECEIVED"
    assert sorted(delivery["documentIds"]) == sorted(later) and delivery["status"] == "RECEIVED"
    assert delivery["readyCount"] == 3 and booking["readyCount"] == 1
    by_id = {d["documentId"]: d for d in listing["documents"]}
    assert (by_id[first]["templateKey"], by_id[first]["process_area"]) == ("dealer_receipt", "BOOKING")
    for document_id in later:
        assert (by_id[document_id]["templateKey"], by_id[document_id]["process_area"]) == ("payment_receipt", "DELIVERY")


def test_a_receipt_uploaded_under_delivery_that_makes_the_minimum_is_the_booking_payment(journey) -> None:
    only = _receipt(journey, "21000", "R1", "2026-10-01", area="DELIVERY")
    listing = _listing(journey)
    checklist = {(c["stage"], c["templateKey"]): c for c in listing["checklist"]}
    assert checklist[("BOOKING", "dealer_receipt")]["documentIds"] == [only]
    assert checklist[("DELIVERY", "payment_receipt")]["documentIds"] == []
