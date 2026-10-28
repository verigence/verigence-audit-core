"""When a delivery has started (product owner 2026-10-28): a UPI screenshot, an
extra receipt, a cost sheet or a GST certificate uploaded with the booking
papers does not start it; a delivery record an upload opened is not a start."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import add_ready_document, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_delivery_start import (
    delivery_has_started,
    delivery_started_ids,
    starts_delivery,
)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2dst")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _started(journey) -> bool:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        one = delivery_has_started(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        batch = delivery_started_ids(connection, tenant_id=journey.tenant_id, journey_ids=[journey.journey_id])
    assert one == (str(journey.journey_id) in batch)
    return one


def _delivery_record(journey, *, delivered: bool) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text(
            """
            INSERT INTO auditcore.deliveries (tenant_id, journey_id, actual_status_domain,
                actual_delivery_status_code, status_source, actual_delivered_at)
            VALUES (:t, :j, 'DELIVERY', 'DELIVERY_STARTED', 'OPERATIONAL_INPUT',
                    CASE WHEN :delivered THEN now() END)
            """), {"t": journey.tenant_id, "j": journey.journey_id, "delivered": delivered})


def test_pages_that_come_with_the_booking_do_not_start_it(journey):
    for kind in ("dealer_receipt", "payment_receipt", "bank_statement_extract", "cost_sheet",
                 "gst_certificate", "gst_declaration", "purchase_order"):
        add_ready_document(journey, kind, process_area="DELIVERY")
    assert not _started(journey)
    assert not starts_delivery("upi_screenshot") and not starts_delivery(None)


def test_a_delivery_record_an_upload_opened_is_not_a_start(journey):
    _delivery_record(journey, delivered=False)
    assert not _started(journey)


def test_a_delivery_document_starts_it(journey):
    add_ready_document(journey, "rto_challan", process_area="DELIVERY")
    assert _started(journey) and starts_delivery("customer_invoice_dms")


def test_a_delivered_car_has_started_its_delivery(journey):
    _delivery_record(journey, delivered=True)
    assert _started(journey)
