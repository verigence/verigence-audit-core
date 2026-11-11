"""The journey export's booking date: every journey has one (product owner 2026-11-07).

"you fix the download of the journey": in the export only, a journey's booking
date is the one on file when it can be right (on or after the programme's
floor, not after today), else the day the PC first uploaded booking documents,
else the day the journey was started. "Dates by: Booking date" filters on it,
and the last column says which of the three it is.
"""
from __future__ import annotations

import io
from datetime import UTC, date, datetime, timedelta

import pytest
from conftest import delete_tenant_data
from openpyxl import load_workbook
from p2_support import add_page, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_dates import programme_date_floor, programme_today
from audit_core.uc03_p2_journey_export import (
    columns,
    journeys_in_scope,
    run_pending_exports,
)

STARTED = date(2026, 10, 25)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="exbd")
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


def _booking(journey, booking_date: date | None) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date) VALUES (:t, :j, :b) "
                 "ON CONFLICT (tenant_id, journey_id) DO UPDATE SET booking_date=:b"),
            {"t": journey.tenant_id, "j": journey.journey_id, "b": booking_date},
        )


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


def _scope(journey, start: date | None = None, end: date | None = None) -> list[dict]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return journeys_in_scope(connection, tenant_id=journey.tenant_id,
                                 filters={"dateBasis": "BOOKING", "dateFrom": start, "dateTo": end},
                                 outlet_ids=None)


def _one(journey) -> tuple[date, str]:
    [row] = _scope(journey)
    return row["booking_date"], row["booking_date_source"]


def _floor() -> date:
    floor = programme_date_floor()
    assert floor is not None
    return floor


# ── the three sources ───────────────────────────────────────────────────────


def test_the_booking_date_on_file_is_used_when_it_can_be_right(journey) -> None:
    on_file = max(_floor(), programme_today() - timedelta(days=40))
    _booking(journey, on_file)
    _upload(journey, STARTED + timedelta(days=1))
    assert _one(journey) == (on_file, "BOOKING_DOCKET")


def test_without_one_the_day_booking_documents_were_first_uploaded(journey) -> None:
    _booking(journey, None)
    _upload(journey, STARTED + timedelta(days=4), stage="DELIVERY")  # not a booking upload
    _upload(journey, STARTED + timedelta(days=3), page_status="CANCELLED")  # dropped since
    _upload(journey, STARTED + timedelta(days=2))
    _upload(journey, STARTED + timedelta(days=1))
    assert _one(journey) == (STARTED + timedelta(days=1), "BOOKING_DOCS_UPLOADED")


def test_without_booking_uploads_the_day_the_journey_was_started(journey) -> None:
    _upload(journey, STARTED + timedelta(days=2), stage="DELIVERY")
    assert _one(journey) == (STARTED, "JOURNEY_CREATED")  # no booking record at all
    _booking(journey, None)
    assert _one(journey) == (STARTED, "JOURNEY_CREATED")


# ── the floor and today ─────────────────────────────────────────────────────


def test_a_date_on_file_before_the_floor_is_not_used(journey) -> None:
    _booking(journey, _floor() - timedelta(days=3650))  # 2016 for 2026
    _upload(journey, STARTED + timedelta(days=1))
    assert _one(journey) == (STARTED + timedelta(days=1), "BOOKING_DOCS_UPLOADED")
    _booking(journey, _floor())  # the floor itself is a usable date
    assert _one(journey) == (_floor(), "BOOKING_DOCKET")


def test_a_date_on_file_after_today_is_not_used(journey) -> None:
    # The database refuses a future booking date (trigger
    # validate_actual_booking_date, on the project's own clock); the export
    # does not rely on it. Held here with triggers skipped for this session.
    tomorrow = programme_today() + timedelta(days=1)
    _booking(journey, None)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        connection.execute(
            text("UPDATE auditcore.bookings SET booking_date=:d WHERE tenant_id=:t AND journey_id=:j"),
            {"d": tomorrow, "t": journey.tenant_id, "j": journey.journey_id},
        )
    assert _one(journey) == (STARTED, "JOURNEY_CREATED")


# ── the filter ──────────────────────────────────────────────────────────────


def test_the_booking_date_filter_uses_that_date(journey) -> None:
    _booking(journey, date(2026, 9, 30))
    assert _scope(journey, date(2026, 10, 20), date(2026, 11, 7)) == []
    assert len(_scope(journey, date(2026, 9, 1), date(2026, 9, 30))) == 1
    # A misread on file: the upload day decides, not the misread, not the start day.
    _booking(journey, _floor() - timedelta(days=700))
    _upload(journey, date(2026, 11, 2))
    assert _scope(journey, date(2026, 10, 20), date(2026, 10, 31)) == []
    assert len(_scope(journey, date(2026, 11, 1), date(2026, 11, 7))) == 1


# ── the column ──────────────────────────────────────────────────────────────


def test_the_workbook_shows_the_booking_date_and_its_source(journey, monkeypatch) -> None:
    class _Storage:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def put_object(self, key: str, data: bytes, *, content_type: str) -> None:
            self.objects[key] = data

    storage = _Storage()
    monkeypatch.setattr("audit_core.uc03_p2_storage.get_p2_document_storage", lambda: storage)
    _booking(journey, None)
    _upload(journey, STARTED + timedelta(days=1))
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
    book = load_workbook(io.BytesIO(data))
    sheet = book["Deal Audit"]
    header = [c.value for c in next(sheet.iter_rows(max_row=1))]
    assert header == columns() and "Booking Date Source" in header and "Booking Date Note" not in header
    [row] = list(sheet.iter_rows(min_row=2, values_only=True))
    values = dict(zip(header, row, strict=True))
    assert values["Booking Date"] == (STARTED + timedelta(days=1)).isoformat()
    assert values["Booking Date Source"] == "Day booking documents were uploaded"
    notes = {r[0]: r[1] for r in book["Columns"].iter_rows(min_row=2, values_only=True)}
    assert "Day booking documents were uploaded" in notes["Booking Date Source"]


# ── the job's statement limit (9 Nov 2026) ──────────────────────────────────


def test_export_statements_get_the_job_time_limit_on_a_short_timeout_engine(monkeypatch) -> None:
    """9 Nov 2026: the export ran on the P2 worker's 10-second engine and the
    Mahindra Orissa scope query was cancelled. Every export job transaction
    sets its own limit; SET LOCAL ends with it."""
    from sqlalchemy import create_engine

    from audit_core import uc03_p2_journey_export as export

    short = create_engine(database_engine().url, connect_args={"options": "-c statement_timeout=10000"})
    try:
        with short.begin() as connection:
            assert connection.execute(text("SHOW statement_timeout")).scalar_one() == "10s"
            export._allow_long(connection)
            assert connection.execute(text("SHOW statement_timeout")).scalar_one() == "5min"
        with short.begin() as connection:
            assert connection.execute(text("SHOW statement_timeout")).scalar_one() == "10s"
        monkeypatch.setenv("AUDIT_CORE_EXPORT_STATEMENT_TIMEOUT_MS", "120000")
        with short.begin() as connection:
            export._allow_long(connection)
            assert connection.execute(text("SHOW statement_timeout")).scalar_one() == "2min"
    finally:
        short.dispose()


def test_the_export_job_runs_its_scope_query_under_the_job_limit(journey, monkeypatch) -> None:
    from sqlalchemy import create_engine

    from audit_core import uc03_p2_journey_export as export

    seen: list[str] = []
    real_scope = export.journeys_in_scope

    def spy(connection, **kwargs):
        seen.append(connection.execute(text("SHOW statement_timeout")).scalar_one())
        return real_scope(connection, **kwargs)

    class _Storage:
        def put_object(self, key: str, data: bytes, *, content_type: str) -> None:
            pass

    monkeypatch.setattr(export, "journeys_in_scope", spy)
    short = create_engine(database_engine().url, connect_args={"options": "-c statement_timeout=10000"})
    try:
        with short.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            connection.execute(
                text("INSERT INTO auditcore.p2_journey_exports (tenant_id, export_id, requested_by_actor_id, "
                     "requested_by_role, filters, outlet_ids, names, file_name, status) "
                     "VALUES (:t, gen_random_uuid(), 'pm', 'PM', CAST(:f AS jsonb), NULL, '{}'::jsonb, 'x.xlsx', "
                     "'PENDING')"),
                {"t": journey.tenant_id, "f": '{"dateBasis": "BOOKING"}'},
            )
        job = export.claim_export(short, journey.tenant_id, "test-worker")
        export.run_export(short, job, storage=_Storage())
        with short.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            status = connection.execute(
                text("SELECT status FROM auditcore.p2_journey_exports WHERE tenant_id=:t"),
                {"t": journey.tenant_id}).scalar_one()
    finally:
        short.dispose()
    assert seen == ["5min"] and status == "READY"


def test_first_booking_uploads_are_worked_out_per_journey_across_many(journey) -> None:
    """The grouped pass gives each journey its own first booking upload, with
    other journeys' uploads and pages around it."""
    others = []
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        for n in range(30):
            others.append(connection.execute(
                text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, journey_reference, "
                     "created_at_utc) VALUES (:t, :d, :o, :c, :r, :at) RETURNING journey_id"),
                {"t": journey.tenant_id, "d": journey.dealer_id, "o": journey.outlet_id, "c": journey.customer_id,
                 "r": f"EXBD-MANY-{n:02d}", "at": datetime(2026, 10, 1, 6, 0, tzinfo=UTC)},
            ).scalar_one())
    from p2_support import P2Journey

    for n, journey_id in enumerate(others):
        other = P2Journey(engine=journey.engine, tenant_id=journey.tenant_id, journey_id=journey_id,
                          actor_id=journey.actor_id, dealer_id=journey.dealer_id, outlet_id=journey.outlet_id,
                          customer_id=journey.customer_id)
        if n % 3 == 0:
            continue  # no upload: the day it was started
        _upload(other, date(2026, 10, 10) + timedelta(days=n % 7))
        _upload(other, date(2026, 10, 20) + timedelta(days=n % 5))
        if n % 3 == 2:
            _upload(other, date(2026, 10, 2), stage="DELIVERY")
    rows = {r["journey_id"]: (r["booking_date"], r["booking_date_source"]) for r in _scope(journey)}
    for n, journey_id in enumerate(others):
        expected = ((date(2026, 10, 1), "JOURNEY_CREATED") if n % 3 == 0
                    else (date(2026, 10, 10) + timedelta(days=n % 7), "BOOKING_DOCS_UPLOADED"))
        assert rows[journey_id] == expected, n
