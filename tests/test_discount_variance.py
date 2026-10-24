"""Discount variance, tallied from the invoice only (product owner, 2026-10-24)."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_discount_variance import (
    OVER,
    UNDER,
    UNKNOWN,
    discount_variance_report,
    discount_variance_rows,
)


def _view(discounts, *, invoice=True, price="1000000"):
    return {
        "summary": {"vehicleInvoiceOnFile": invoice},
        "categories": [{"code": "VEHICLE", "components": [
            {"key": "ex_showroom_price", "standard": "1000000", "current": price}]}],
        "discounts": discounts,
    }


def _d(key, entitled, billed, billed_from="invoice", booking=None):
    return {"key": key, "label": key.title(), "entitled": entitled, "billed": billed, "billedFrom": billed_from,
            "booking": booking}


def test_a_discount_given_above_its_entitlement_is_over():
    rows = discount_variance_rows(_view([_d("CORPORATE", "20000", "35000.00")]))
    assert rows == [{"key": "CORPORATE", "label": "Corporate", "status": OVER, "entitled": "20000",
                     "invoice": "35000.00", "variance": "15000.00"}]


def test_a_discount_the_invoice_does_not_carry_is_under():
    rows = discount_variance_rows(_view([_d("CORPORATE", "20000", None, billed_from=None)]))
    assert rows[0]["status"] == UNDER and rows[0]["invoice"] == "0" and rows[0]["variance"] == "-20000"


def test_within_rupees_100_is_not_a_variance():
    assert discount_variance_rows(_view([_d("CORPORATE", "20000", "20099.99")])) == []


def test_the_booking_figure_is_never_the_tally():
    # the booking says 20000, the invoice carries nothing: Rs 0 on the invoice
    rows = discount_variance_rows(_view([_d("CORPORATE", "20000", "20000", billed_from="document",
                                            booking="20000")]))
    assert rows[0]["status"] == UNDER and rows[0]["invoice"] == "0"


def test_an_unknown_entitlement_is_reported_not_taken_as_zero():
    rows = discount_variance_rows(_view([_d("EXCHANGE", None, "10000")]))
    assert rows[0]["status"] == UNKNOWN and rows[0]["variance"] is None
    assert discount_variance_rows(_view([_d("EXCHANGE", None, None, billed_from=None)])) == []


def test_no_invoice_or_an_invoice_with_no_price_is_not_tallied():
    assert discount_variance_rows(_view([_d("CORPORATE", "20000", "0")], invoice=False)) == []
    assert discount_variance_rows(_view([_d("CORPORATE", "20000", "0")], price=None)) == []


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="discv")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def test_the_report_carries_category_pc_outlet_customer_and_model(journey):
    rows = [{"key": "CORPORATE", "label": "Corporate", "status": OVER, "entitled": "20000",
             "invoice": "35000", "variance": "15000"}]
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_journey_runtime SET discount_variance = CAST(:d AS jsonb) "
                 "WHERE tenant_id = :t AND journey_id = :j"),
            {"d": json.dumps(rows), "t": journey.tenant_id, "j": journey.journey_id},
        )
        report = discount_variance_report(connection, tenant_id=journey.tenant_id)
    assert report["counts"]["over"] == 1 and report["counts"]["journeys"] == 1
    item = report["items"][0]
    assert {"discount", "pc", "outlet", "customer", "model", "variant", "variance"} <= set(item)
    assert item["discountKey"] == "CORPORATE" and Decimal(item["variance"]) == 15000
    assert UNDER and report["toleranceRupees"] == 100
