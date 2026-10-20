"""SKU resolution is retried every night for a journey with a model read but
no confirmed SKU (2026-10-20). Before, nothing re-ran it once the Booking
documents were copied: a price master loaded or corrected later never
reached the journeys waiting on it."""
from __future__ import annotations

from datetime import date

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_audit_rules import queue_nightly_sku_retry


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="nightsku")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _product(journey, *, model: str | None, status: str | None) -> None:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(
            text("INSERT INTO auditcore.journey_products (tenant_id, journey_id, model_name_snapshot, selection_status) "
                 "VALUES (:t, :j, :m, :s)"),
            {"t": journey.tenant_id, "j": journey.journey_id, "m": model, "s": status},
        )


def _queue(journey, day: date) -> int:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return queue_nightly_sku_retry(c, tenant_id=journey.tenant_id, today=day)


def _work(journey) -> list[dict]:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return [dict(r) for r in c.execute(
            text("SELECT work_key, payload FROM auditcore.p2_work_queue "
                 "WHERE tenant_id=:t AND journey_id=:j AND work_type='CONTROL_EVALUATE'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).mappings()]


def test_a_journey_with_a_model_but_no_confirmed_sku_is_retried_once_a_night(journey):
    _product(journey, model="XUV 3XO", status=None)
    assert _queue(journey, date(2026, 10, 20)) == 1
    assert _queue(journey, date(2026, 10, 20)) == 0  # once a night
    work = _work(journey)
    assert [w["work_key"] for w in work] == [f"nightly-sku:2026-10-20:{journey.journey_id}"]
    assert work[0]["payload"]["unit"] == "NATIVE:BOOKING"
    assert work[0]["payload"]["force"] is True


def test_a_confirmed_sku_is_left_alone(journey):
    _product(journey, model="XUV 3XO", status="CONFIRMED")
    assert _queue(journey, date(2026, 10, 20)) == 0


def test_no_model_read_is_not_retried(journey):
    """Nothing to match: resolution would only skip again."""
    _product(journey, model=None, status=None)
    assert _queue(journey, date(2026, 10, 20)) == 0


def test_no_booking_model_but_a_vehicle_invoice_model_is_retried(journey):
    """Rule 2026-10-20: the resolution reads the vehicle invoice when the
    Booking Form named no model, so such a Journey is retried too -- with or
    without a journey_products row."""
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(
            text(
                """
                INSERT INTO auditcore.journey_document_extracted_fields (
                    tenant_id, journey_id, di_document_id, source_fact_version, stage_code,
                    source_document_type_key, field_key, extracted_value, effective_value, is_modified
                ) VALUES (:t, :j, gen_random_uuid(), 1, 'DELIVERY', 'customer_invoice_dms', 'model_name_raw',
                          '"XUV 3XO"'::jsonb, '"XUV 3XO"'::jsonb, false)
                """
            ),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
    assert _queue(journey, date(2026, 10, 20)) == 1
