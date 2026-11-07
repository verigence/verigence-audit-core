"""The booking date of a Journey: one rule, one stored copy (2026-11-07).

Found on Mahindra Orissa: 208 of 1,204 Journeys had no booking date on their
booking record although 184 had documents and most an active booking docket
carrying one, so "Export to Excel" by booking date dropped them (223 listed
where about 430 were expected). The generic machine pass wrote the ONE
winning reading across every copy ever read; a superseded copy (refused as no
longer ACTIVE) or a Delivery-stage ledger (never written) could win, and the
active docket's date was never stored.
"""
from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_evidence,
    add_extracted_field,
    add_ready_document,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_booking_date import (
    fill_missing_booking_dates,
    journey_booking_date,
    read_booking_date,
    sync_booking_date,
)
from audit_core.uc03_p2_dates import programme_date_floor, programme_today
from audit_core.uc03_post_extraction_materialization import (
    materialize_machine_booking_values,
)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="bkdt")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _a_good_day() -> date:
    """A day the rule accepts: on or after the floor, not after today."""
    floor = programme_date_floor() or date(2026, 7, 1)
    return max(floor, programme_today() - timedelta(days=5))


def _booking(journey, booking_date: date | None = None) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date) VALUES (:t, :j, :b) "
                 "ON CONFLICT (tenant_id, journey_id) DO UPDATE SET booking_date=:b"),
            {"t": journey.tenant_id, "j": journey.journey_id, "b": booking_date},
        )


def _held(journey) -> date | None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT booking_date FROM auditcore.bookings WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one_or_none()


def _call(journey, fn, **kwargs):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return fn(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, **kwargs)


def _docket(journey, booking_date: str | None, *, status: str = "ACTIVE", doc_type: str = "booking_docket"):
    di_document_id = uuid4()
    evidence_id = add_evidence(journey, di_document_id=di_document_id, document_type_key=doc_type, status=status)
    add_extracted_field(journey, di_document_id=di_document_id, field_key="booking_date", value=booking_date,
                        document_type="booking_form")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.journey_document_extracted_fields SET evidence_id=:e "
                 "WHERE tenant_id=:t AND di_document_id=:d"),
            {"e": evidence_id, "t": journey.tenant_id, "d": di_document_id},
        )
    return di_document_id


# ── the rule ────────────────────────────────────────────────────────────────


def test_the_booking_date_is_the_one_on_the_booking_form_else_the_one_entered(journey) -> None:
    day = _a_good_day()
    _booking(journey, day - timedelta(days=1))
    # Nothing read yet: the entered date.
    assert _call(journey, read_booking_date) is None
    assert _call(journey, journey_booking_date) == day - timedelta(days=1)
    # The booking form's date, as read, is the Journey's booking date (R5).
    add_ready_document(journey, "booking_form", booking_date=day.strftime("%d/%m/%Y"))
    assert _call(journey, read_booking_date) == day
    assert _call(journey, journey_booking_date) == day


def test_a_misread_date_on_the_form_is_not_the_booking_date(journey) -> None:
    floor = programme_date_floor()
    assert floor is not None
    _booking(journey, None)
    add_ready_document(journey, "booking_form", booking_date=(floor - timedelta(days=730)).isoformat())
    assert _call(journey, read_booking_date) is None
    assert _call(journey, journey_booking_date) is None


def test_only_an_active_copy_counts(journey) -> None:
    """A superseded or voided copy of the docket never sets the booking date."""
    day = _a_good_day()
    _booking(journey, None)
    _docket(journey, (day - timedelta(days=3)).isoformat(), status="SUPERSEDED")
    _docket(journey, (day - timedelta(days=4)).isoformat(), status="VOIDED")
    assert _call(journey, read_booking_date) is None
    _docket(journey, day.isoformat())
    assert _call(journey, read_booking_date) == day


# ── the stored copy ─────────────────────────────────────────────────────────


def test_the_machine_pass_stores_the_active_docket_s_date_whatever_wins_the_tie_break(journey) -> None:
    """The DEV case: a superseded copy and a Delivery-stage ledger next to the
    active docket. Before, the pass skipped ("Unsupported evidence" / not a
    Booking-stage winner) and the record stayed empty."""
    day = _a_good_day()
    _booking(journey, None)
    _docket(journey, (day - timedelta(days=2)).isoformat(), status="SUPERSEDED")
    _docket(journey, day.isoformat())
    ledger = uuid4()
    add_evidence(journey, di_document_id=ledger, document_type_key="customer_ledger", process_area="DELIVERY")
    add_extracted_field(journey, di_document_id=ledger, field_key="booking_date", value=None,
                        document_type="customer_ledger")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.journey_document_extracted_fields SET stage_code='DELIVERY' "
                 "WHERE tenant_id=:t AND di_document_id=:d"),
            {"t": journey.tenant_id, "d": ledger},
        )
    _call(journey, materialize_machine_booking_values)
    assert _held(journey) == day
    # Running it again changes nothing.
    assert _call(journey, sync_booking_date) == "UNCHANGED"


def test_the_record_follows_a_corrected_reading_and_is_never_cleared(journey) -> None:
    day = _a_good_day()
    _booking(journey, None)
    document = _docket(journey, (day - timedelta(days=1)).isoformat())
    assert _call(journey, sync_booking_date) == "FILLED"
    # The PC corrects the date on the docket: the record follows.
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.journey_document_extracted_fields SET effective_value=to_jsonb(CAST(:v AS text)) "
                 "WHERE tenant_id=:t AND di_document_id=:d"),
            {"v": day.isoformat(), "t": journey.tenant_id, "d": document},
        )
    assert _call(journey, sync_booking_date) == "UPDATED"
    assert _held(journey) == day
    # The docket is removed: nothing usable is read, the held date stays.
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.evidence SET association_status='VOIDED' WHERE tenant_id=:t AND di_document_id=:d"),
            {"t": journey.tenant_id, "d": document},
        )
    assert _call(journey, sync_booking_date) == "NOT_READ"
    assert _held(journey) == day


def test_the_backfill_fills_empty_records_only_and_is_idempotent(journey) -> None:
    day = _a_good_day()
    _booking(journey, None)
    _docket(journey, day.isoformat())
    assert fill_missing_booking_dates(journey.engine, journey.tenant_id) == {"FILLED": 1}
    assert _held(journey) == day
    # Again: nothing left to fill.
    assert fill_missing_booking_dates(journey.engine, journey.tenant_id) == {}
    assert _held(journey) == day


def test_the_backfill_never_replaces_a_date_a_person_entered(journey) -> None:
    day = _a_good_day()
    entered = day - timedelta(days=3)
    _booking(journey, entered)
    _docket(journey, day.isoformat())
    assert fill_missing_booking_dates(journey.engine, journey.tenant_id) == {"KEPT": 1}
    assert _call(journey, sync_booking_date, replace="NONE") == "KEPT"
    assert _held(journey) == entered


def test_the_backfill_replaces_a_date_only_a_superseded_copy_carries(journey) -> None:
    """The other face of the bug on DEV: 58 records held a reading of a copy
    since superseded or voided -- often a misread year (2016-10-10 held while
    the active docket reads 2026-10-18)."""
    day = _a_good_day()
    misread = day.replace(year=day.year - 10)
    _docket(journey, misread.isoformat(), status="SUPERSEDED")
    _booking(journey, misread)
    _docket(journey, day.isoformat())
    assert fill_missing_booking_dates(journey.engine, journey.tenant_id) == {"UPDATED": 1}
    assert _held(journey) == day
    assert fill_missing_booking_dates(journey.engine, journey.tenant_id) == {}


def test_a_date_an_active_document_carries_is_not_a_stale_copy(journey) -> None:
    day = _a_good_day()
    other = day - timedelta(days=2)
    # Two active booking documents: the form (which the rule reads) and a
    # docket carrying the held date. The held date is no stale copy.
    add_ready_document(journey, "booking_form", booking_date=day.isoformat())
    _docket(journey, other.isoformat())
    _booking(journey, other)
    assert _call(journey, sync_booking_date, replace="STALE_COPY") == "KEPT"
    assert _held(journey) == other


def test_the_backfill_leaves_a_journey_with_nothing_usable_empty(journey) -> None:
    floor = programme_date_floor()
    assert floor is not None
    _booking(journey, None)
    _docket(journey, (floor - timedelta(days=400)).isoformat())
    assert fill_missing_booking_dates(journey.engine, journey.tenant_id) == {"NOT_READ": 1}
    assert _held(journey) is None
