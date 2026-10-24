"""Anything the deal needs and does not have raises a task to fix, and a deal
with no price raises a critical one naming the vehicle (product owner,
2026-10-24: "for dates for which price is not available, pls raise critical
task that the given model doesnt has price available"; "any thing not
available raise an task as fix").

Before this, all three read MEDIUM and sat unread among hundreds of others,
and a booking date that was present but misread raised nothing at all -- the
resolver refused it and no task asked anyone to correct it.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_dates import (
    programme_date_floor,
    programme_today,
    unusable_pricing_date_reason,
)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="pucr")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def test_a_date_before_the_programme_is_called_a_misread_year(journey):
    floor = programme_date_floor()
    assert floor is not None
    why = unusable_pricing_date_reason(floor - timedelta(days=800))
    assert why is not None
    assert "misread" in why


def test_a_date_after_today_is_called_a_misread(journey):
    why = unusable_pricing_date_reason(programme_today() + timedelta(days=400))
    assert why is not None
    assert "after today" in why


def test_an_unreadable_value_says_it_could_not_be_read(journey):
    why = unusable_pricing_date_reason("as per booking")
    assert why is not None
    assert "could not be read" in why


def test_a_missing_value_says_none_was_found(journey):
    for empty in (None, "", "   "):
        why = unusable_pricing_date_reason(empty)
        assert why is not None
        assert "no booking date was found" in why


def test_a_date_inside_the_programme_is_usable(journey):
    good = max(programme_date_floor(), programme_today() - timedelta(days=20))
    assert unusable_pricing_date_reason(good) is None
    assert unusable_pricing_date_reason(good.isoformat()) is None


def test_the_resolver_and_the_task_agree_on_what_is_unusable(journey):
    """One definition, two users. If these ever drift, a date the engine
    refuses raises no task and the Journey is stuck with nobody told."""
    from audit_core.uc03_sku_candidates import _priceable

    floor = programme_date_floor()
    for day in (floor - timedelta(days=800), programme_today() + timedelta(days=400)):
        assert unusable_pricing_date_reason(day) is not None
        assert _priceable(day, source="t", tenant_id=journey.tenant_id,
                          journey_id=journey.journey_id) is None
    good = max(floor, programme_today() - timedelta(days=20))
    assert unusable_pricing_date_reason(good) is None
    assert _priceable(good, source="t", tenant_id=journey.tenant_id,
                      journey_id=journey.journey_id) == good


def test_no_price_master_in_force_is_critical_and_names_the_vehicle(journey):
    from audit_core.uc03_sku_candidates import sync_price_master_in_force_task

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date) "
                 "VALUES (:t, :j, CURRENT_DATE - 5)"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
        connection.execute(
            text("INSERT INTO auditcore.journey_products (tenant_id, journey_id, model_name_snapshot, "
                 "variant_name_snapshot, selection_source) VALUES (:t, :j, 'THAR ROXX', 'MX1 D MT RWD', "
                 "'EVIDENCE')"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
        # no price list exists for this tenant at all
        sync_price_master_in_force_task(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        [task] = connection.execute(
            text("SELECT title, severity, priority FROM auditcore.p2_tasks "
                 "WHERE tenant_id=:t AND journey_id=:j AND task_type='PRICE_MASTER_NOT_IN_FORCE'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).mappings().all()
    assert task["severity"] == "CRITICAL"
    assert task["priority"] == "URGENT"
    assert "No price available for THAR ROXX MX1 D MT RWD" in task["title"]


def test_the_no_sku_task_is_critical(journey):
    from audit_core.uc03_sku_tasks import raise_not_set_task

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        raise_not_set_task(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                           why="no booking form on file names the vehicle", reason="no_vehicle_model_read")
        [task] = connection.execute(
            text("SELECT severity, priority FROM auditcore.p2_tasks "
                 "WHERE tenant_id=:t AND journey_id=:j AND task_type='SKU_NOT_SET'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).mappings().all()
    assert (task["severity"], task["priority"]) == ("CRITICAL", "URGENT")
