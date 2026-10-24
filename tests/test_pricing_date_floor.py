"""A date the programme calls a misread is not a pricing date (real Postgres).

Found 2026-10-24 by retrospective, after three SKU runs had blamed the matcher.
``before_floor`` states the rule outright -- "a date earlier than the
programme's floor is a misread, never a fact" -- and it is enforced for
delivery dates, for booking confirmation, and as the high-severity
DATE_BEFORE_FLOOR verification reason. It was never enforced in
``journey_pricing_date``, the one date every price-master lookup goes through.

So a handwritten 26 read as 24 priced the deal in 2024, found no price book in
force, and the run recorded "no effective price list" -- pointing the fix at
loading historical price books that would never have been used. PRATEEK
PANIGRAHI (2024-08-21) and PUSPANJALI SAHOO (2024-09-04) were both booked in
October 2026. The Journey list applies the floor and hides those dates, so the
screen looked right while the engine used them.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_dates import programme_date_floor, programme_today
from audit_core.uc03_sku_candidates import journey_pricing_date


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="pdfl")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _booking(journey, *, booking_date=None, applied=None):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date, pricing_effective_on) "
                 "VALUES (:t, :j, :b, :a) "
                 "ON CONFLICT (tenant_id, journey_id) DO UPDATE "
                 "SET booking_date = :b, pricing_effective_on = :a"),
            {"t": journey.tenant_id, "j": journey.journey_id, "b": booking_date, "a": applied},
        )


def _priced_on(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return journey_pricing_date(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _floor() -> date:
    floor = programme_date_floor()
    assert floor is not None, "the programme must declare a date floor for this rule to exist"
    return floor


def test_a_booking_date_before_the_programme_is_not_priced_on(journey):
    _booking(journey, booking_date=_floor() - timedelta(days=800))  # a 26 read as 24
    assert _priced_on(journey) is None, (
        "pricing on a misread date finds no price book and reads as a masters gap"
    )


def test_a_date_after_today_is_refused_too(journey):
    """The same misread from the other side: a 26 read as 27.

    Checked directly rather than through ``bookings``, because the database
    already refuses a future booking_date (the validate_actual_booking_date
    trigger), which is worth knowing: that column is guarded, the booking
    form's own reading is not, and the form's reading is what
    ``journey_pricing_date`` prefers."""
    from audit_core.uc03_sku_candidates import _priceable

    ahead = programme_today() + timedelta(days=400)
    assert _priceable(ahead, source="booking_form", tenant_id=journey.tenant_id,
                      journey_id=journey.journey_id) is None
    assert _priceable(programme_today(), source="booking_form", tenant_id=journey.tenant_id,
                      journey_id=journey.journey_id) == programme_today()


def test_the_database_itself_refuses_a_future_booking_date(journey):
    """Recorded because it narrows where the guard is needed: a future date
    cannot reach bookings.booking_date at all."""
    import pytest as _pytest
    from sqlalchemy.exc import ProgrammingError

    with _pytest.raises(ProgrammingError, match="future"):
        _booking(journey, booking_date=programme_today() + timedelta(days=400))


def test_a_booking_date_inside_the_programme_is_used(journey):
    """The fix must not cost a single good date."""
    good = max(_floor(), programme_today() - timedelta(days=30))
    _booking(journey, booking_date=good)
    assert _priced_on(journey) == good


def test_the_date_on_the_floor_itself_is_used(journey):
    _booking(journey, booking_date=_floor())
    assert _priced_on(journey) == _floor()


def test_todays_date_is_used(journey):
    _booking(journey, booking_date=programme_today())
    assert _priced_on(journey) == programme_today()


def test_a_date_a_person_applied_is_never_second_guessed(journey):
    """``pricing_effective_on`` is only ever set to a published price-list
    version's own effective_from by a PC's or Team Lead's explicit decision, so
    it cannot be a misreading and the floor must not override it."""
    applied = _floor() - timedelta(days=5)
    _booking(journey, booking_date=programme_today(), applied=applied)
    assert _priced_on(journey) == applied


def test_no_booking_row_is_still_no_date(journey):
    assert _priced_on(journey) is None
