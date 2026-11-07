"""The booking date of a Journey: one rule, one stored copy, its source.

Product owner 2026-11-07 20:47 IST: every journey carries a booking date --
the booking form's, else the docket's, else one a person entered, else the
day the PC uploaded the booking documents, else the day the journey was
started. The last two are for lists and reports only: no deal is priced on
them and no check reads them.

History: the column used to be written only by the generic machine pass,
whose one winning reading could be a superseded copy (refused) or a
Delivery-stage ledger (never written); 208 of 1,204 Mahindra Orissa journeys
had no booking date.
"""
from __future__ import annotations

import io
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from openpyxl import load_workbook
from p2_support import (
    add_evidence,
    add_extracted_field,
    add_page,
    add_ready_document,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_booking_date import (
    BOOKING_DOCKET,
    BOOKING_DOCS_UPLOADED,
    BOOKING_FORM,
    ENTERED,
    JOURNEY_CREATED,
    fill_missing_booking_dates,
    journey_booking_date,
    pricing_booking_date,
    sync_booking_date,
)
from audit_core.uc03_p2_dates import programme_date_floor, programme_today
from audit_core.uc03_p2_deal_actions import booking_date_for_pricing
from audit_core.uc03_p2_journey_export import (
    columns,
    journeys_in_scope,
    run_pending_exports,
)
from audit_core.uc03_post_extraction_materialization import (
    materialize_machine_booking_values,
)
from audit_core.uc03_sku_candidates import journey_pricing_date

STARTED = date(2026, 10, 25)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="bkdt")
    with engine.begin() as connection:
        set_tenant_context(connection, created.tenant_id)
        connection.execute(
            text("UPDATE auditcore.journeys SET created_at_utc=:c WHERE tenant_id=:t AND journey_id=:j"),
            {"c": datetime(STARTED.year, STARTED.month, STARTED.day, 6, 0, tzinfo=UTC),
             "t": created.tenant_id, "j": created.journey_id},
        )
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _a_good_day() -> date:
    """A day the rule accepts on a form: on or after the floor, not after today."""
    floor = programme_date_floor() or date(2026, 7, 1)
    return max(floor, programme_today() - timedelta(days=5))


def _booking(journey, booking_date: date | None = None, source: str | None = None) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date, booking_date_source) "
                 "VALUES (:t, :j, :b, :s) ON CONFLICT (tenant_id, journey_id) DO UPDATE "
                 "SET booking_date=:b, booking_date_source=:s"),
            {"t": journey.tenant_id, "j": journey.journey_id, "b": booking_date, "s": source},
        )


def _held(journey) -> tuple[date | None, str | None]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        row = connection.execute(
            text("SELECT booking_date, booking_date_source FROM auditcore.bookings "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).one_or_none()
    return (row[0], row[1]) if row else (None, None)


def _call(journey, fn, **kwargs):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return fn(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, **kwargs)


def _rule(journey) -> tuple[date, str]:
    found = _call(journey, journey_booking_date)
    return found.day, found.source


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


def _upload(journey, on: date, *, stage: str = "BOOKING", page_status: str = "READY") -> None:
    batch_id, queue_id, _ = add_page(journey)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_upload_batches SET created_at_utc=:c WHERE tenant_id=:t AND batch_id=:b"),
            {"c": datetime(on.year, on.month, on.day, 5, 0, tzinfo=UTC), "t": journey.tenant_id, "b": batch_id},
        )
        connection.execute(
            text("UPDATE auditcore.p2_document_queue SET business_stage=:s, queue_status=:q "
                 "WHERE tenant_id=:t AND queue_id=:id"),
            {"s": stage, "q": page_status, "t": journey.tenant_id, "id": queue_id},
        )


# ── the rule, step by step ──────────────────────────────────────────────────


def test_with_nothing_on_file_the_booking_date_is_the_day_the_journey_was_started(journey) -> None:
    assert _rule(journey) == (STARTED, JOURNEY_CREATED)


def test_the_first_booking_upload_day_comes_before_the_start_day(journey) -> None:
    _upload(journey, STARTED + timedelta(days=4), stage="DELIVERY")  # not a booking upload
    _upload(journey, STARTED + timedelta(days=3), stage="BOOKING", page_status="CANCELLED")  # dropped
    assert _rule(journey) == (STARTED, JOURNEY_CREATED)
    _upload(journey, STARTED + timedelta(days=2))
    _upload(journey, STARTED + timedelta(days=1))
    assert _rule(journey) == (STARTED + timedelta(days=1), BOOKING_DOCS_UPLOADED)


def test_an_entered_date_comes_before_the_upload_day(journey) -> None:
    _upload(journey, STARTED + timedelta(days=1))
    entered = _a_good_day() - timedelta(days=2)
    _booking(journey, entered, ENTERED)
    assert _rule(journey) == (entered, ENTERED)


def test_an_entered_date_before_the_floor_is_not_a_booking_date(journey) -> None:
    floor = programme_date_floor()
    assert floor is not None
    _booking(journey, floor - timedelta(days=700), ENTERED)
    assert _rule(journey) == (STARTED, JOURNEY_CREATED)


def test_the_booking_form_comes_first_and_the_docket_without_one(journey) -> None:
    day = _a_good_day()
    _upload(journey, STARTED + timedelta(days=1))
    _booking(journey, day - timedelta(days=3), ENTERED)
    _docket(journey, (day - timedelta(days=1)).isoformat())
    assert _rule(journey) == (day - timedelta(days=1), BOOKING_DOCKET)
    add_ready_document(journey, "booking_form", booking_date=day.strftime("%d/%m/%Y"))
    assert _rule(journey) == (day, BOOKING_FORM)


def test_a_misread_or_inactive_form_counts_as_missing(journey) -> None:
    floor = programme_date_floor()
    assert floor is not None
    _upload(journey, STARTED + timedelta(days=1))
    _docket(journey, _a_good_day().isoformat(), status="SUPERSEDED")
    _docket(journey, (floor - timedelta(days=730)).isoformat())
    assert _rule(journey) == (STARTED + timedelta(days=1), BOOKING_DOCS_UPLOADED)


def test_a_record_written_before_sources_were_kept_is_entered_only_when_no_document_carries_it(journey) -> None:
    day = _a_good_day()
    _booking(journey, day - timedelta(days=2), None)
    assert _rule(journey) == (day - timedelta(days=2), ENTERED)
    # A copy of a superseded docket's reading is no entered date.
    _docket(journey, (day - timedelta(days=2)).isoformat(), status="SUPERSEDED")
    assert _rule(journey) == (STARTED, JOURNEY_CREATED)


# ── the stored copy ─────────────────────────────────────────────────────────


def test_the_machine_pass_stores_the_rule_s_date_and_source(journey) -> None:
    day = _a_good_day()
    _booking(journey, None)
    _docket(journey, (day - timedelta(days=2)).isoformat(), status="SUPERSEDED")
    _docket(journey, day.isoformat())
    _call(journey, materialize_machine_booking_values)
    assert _held(journey) == (day, BOOKING_DOCKET)
    assert _call(journey, sync_booking_date) == "UNCHANGED"


def test_a_form_date_that_arrives_later_replaces_the_fallback(journey) -> None:
    _upload(journey, STARTED + timedelta(days=1))
    assert _call(journey, sync_booking_date) == "FILLED"
    assert _held(journey) == (STARTED + timedelta(days=1), BOOKING_DOCS_UPLOADED)
    day = _a_good_day()
    _docket(journey, day.isoformat())
    assert _call(journey, sync_booking_date) == "UPDATED"
    assert _held(journey) == (day, BOOKING_DOCKET)


def test_a_person_entering_a_date_records_it_as_entered(journey) -> None:
    from audit_core import uc03_booking_capture as booking_capture

    entered = _a_good_day() - timedelta(days=1)
    _call(journey, booking_capture._write_typed_capture, field_key="BOOKING_DATE", value=entered,
          source_evidence_id=None)
    assert _held(journey) == (entered, ENTERED)
    assert _call(journey, sync_booking_date) == "UNCHANGED"


def test_the_backfill_brings_every_record_to_the_rule_and_is_idempotent(journey) -> None:
    floor = programme_date_floor()
    assert floor is not None
    # A pre-floor misread held from an old copy; booking documents uploaded.
    misread = floor - timedelta(days=3650)
    _docket(journey, misread.isoformat(), status="VOIDED")
    _booking(journey, misread, None)
    _upload(journey, STARTED + timedelta(days=2))
    assert fill_missing_booking_dates(journey.engine, journey.tenant_id) == {"UPDATED": 1}
    assert _held(journey) == (STARTED + timedelta(days=2), BOOKING_DOCS_UPLOADED)
    # Again: the fallback is re-checked and stays.
    assert fill_missing_booking_dates(journey.engine, journey.tenant_id) == {"UNCHANGED": 1}
    assert _held(journey) == (STARTED + timedelta(days=2), BOOKING_DOCS_UPLOADED)


def test_the_backfill_gives_a_journey_with_no_booking_record_its_start_day(journey) -> None:
    assert _held(journey) == (None, None)
    assert fill_missing_booking_dates(journey.engine, journey.tenant_id) == {"FILLED": 1}
    assert _held(journey) == (STARTED, JOURNEY_CREATED)


def test_the_backfill_keeps_an_entered_date_and_only_names_its_source(journey) -> None:
    entered = _a_good_day() - timedelta(days=4)
    _booking(journey, entered, None)
    _upload(journey, STARTED + timedelta(days=1))
    assert fill_missing_booking_dates(journey.engine, journey.tenant_id) == {"UPDATED": 1}
    assert _held(journey) == (entered, ENTERED)
    assert fill_missing_booking_dates(journey.engine, journey.tenant_id) == {}


# ── pricing and checks never use a fallback ─────────────────────────────────


def test_the_deal_is_never_priced_on_a_fallback_date(journey) -> None:
    _upload(journey, STARTED + timedelta(days=1))
    _call(journey, sync_booking_date)
    assert _held(journey)[1] == BOOKING_DOCS_UPLOADED
    assert _call(journey, pricing_booking_date) is None
    assert _call(journey, journey_pricing_date) is None
    assert _call(journey, booking_date_for_pricing) is None
    # A booking form date prices it, exactly as before.
    day = _a_good_day()
    add_ready_document(journey, "booking_form", booking_date=day.isoformat())
    assert _call(journey, journey_pricing_date) == day
    assert _call(journey, booking_date_for_pricing) == day


def test_an_entered_date_still_prices_the_deal(journey) -> None:
    entered = _a_good_day() - timedelta(days=1)
    _booking(journey, entered, ENTERED)
    assert _call(journey, journey_pricing_date) == entered
    assert _call(journey, booking_date_for_pricing) == entered


def test_a_delivery_before_a_fallback_booking_date_is_no_finding(journey) -> None:
    from audit_core.uc03_delivery_review_materialization import (
        _sync_delivery_date_sanity_finding,
    )

    delivered = datetime(2026, 10, 26, 6, 0, tzinfo=UTC)
    _booking(journey, STARTED + timedelta(days=5), BOOKING_DOCS_UPLOADED)
    assert _call(journey, _sync_delivery_date_sanity_finding, actual_delivered_at=delivered)["raised"] == 0
    # The same date entered by a person is a booking date, and the check runs.
    _booking(journey, STARTED + timedelta(days=5), ENTERED)
    assert _call(journey, _sync_delivery_date_sanity_finding, actual_delivered_at=delivered)["raised"] == 1


# ── the export ──────────────────────────────────────────────────────────────


def _scope(journey, start: date, end: date) -> list[dict]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return journeys_in_scope(connection, tenant_id=journey.tenant_id,
                                 filters={"dateBasis": "BOOKING", "dateFrom": start, "dateTo": end},
                                 outlet_ids=None)


def test_the_export_filters_on_the_stored_booking_date(journey) -> None:
    _booking(journey, date(2026, 9, 30), ENTERED)
    assert _scope(journey, date(2026, 10, 20), date(2026, 11, 7)) == []
    [row] = _scope(journey, date(2026, 9, 1), date(2026, 9, 30))
    assert row["booking_date_source"] == ENTERED


def test_the_workbook_shows_the_booking_date_and_its_source(journey, monkeypatch) -> None:
    class _Storage:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def put_object(self, key: str, data: bytes, *, content_type: str) -> None:
            self.objects[key] = data

    storage = _Storage()
    monkeypatch.setattr("audit_core.uc03_p2_storage.get_p2_document_storage", lambda: storage)
    _upload(journey, STARTED + timedelta(days=1))
    _call(journey, sync_booking_date)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.p2_journey_exports (tenant_id, export_id, requested_by_actor_id, "
                 "requested_by_role, filters, outlet_ids, names, file_name, status) "
                 "VALUES (:t, gen_random_uuid(), 'pm', 'PM', CAST(:f AS jsonb), NULL, '{}'::jsonb, 'x.xlsx', "
                 "'PENDING')"),
            {"t": journey.tenant_id,
             "f": '{"dateBasis": "BOOKING", "dateFrom": "2026-10-20", "dateTo": "2026-11-07"}'},
        )
    assert run_pending_exports(journey.engine, [journey.tenant_id], "test-worker") == 1
    [data] = storage.objects.values()
    sheet = load_workbook(io.BytesIO(data))["Deal Audit"]
    header = [c.value for c in next(sheet.iter_rows(max_row=1))]
    assert header == columns() and header[-1] == "Booking Date Source"
    [row] = list(sheet.iter_rows(min_row=2, values_only=True))
    values = dict(zip(header, row, strict=True))
    assert values["Booking Date"] == (STARTED + timedelta(days=1)).isoformat()
    assert values["Booking Date Source"] == "Day booking documents were uploaded"
