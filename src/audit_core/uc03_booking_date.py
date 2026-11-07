"""The booking date of a Journey: one rule, one stored copy, its source.

The rule (product owner R5 2026-10-20; 2026-11-07 20:47 IST: "there should be
date attached ... if booking form date is missing fall back is the audit
opening date", the audit opening date being the day the PC uploaded the
booking documents), first that applies:

1. BOOKING_FORM / BOOKING_DOCKET  the booking date on the Journey's ACTIVE
   booking form -- else its booking docket -- as read, or as the PC
   corrected it, when it can be right (not before the programme's floor, not
   after today; a misread counts as missing);
2. ENTERED                 a date a person entered on the booking record,
   unless it lies before the floor;
3. BOOKING_DOCS_UPLOADED   the day (IST) the PC first uploaded the Journey's
   booking documents: the earliest upload (as the Project Tracker counts one)
   holding a booking-stage page;
4. JOURNEY_CREATED         the day (IST) the Journey was started.

So every Journey has a booking date. Steps 1-2 are the date the deal is
priced on (``pricing_booking_date``; ``uc03_sku_candidates.journey_pricing_date``)
and the only ones a check may read as a booking date: steps 3-4 are a
placeholder for lists and reports, never a pricing basis and never evidence
for a finding (``PRICING_BOOKING_DATE_SQL``).

The stored copy is ``auditcore.bookings.booking_date`` with
``booking_date_source``. ``sync_booking_date`` writes it on every recompute;
``fill_missing_booking_dates`` (worker start) brings every other record to the
rule. A booking form date that arrives later replaces a fallback date on the
next recompute.

History (2026-11-07): the column used to be written only by the generic
machine-materialization pass, from the one winning reading across every copy
ever read; a superseded docket (refused as not ACTIVE) or a Delivery-stage
ledger (never written) could win, and 208 of 1,204 Mahindra Orissa journeys
had no booking date. The customer ledger is not a source here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

logger = structlog.get_logger(__name__)

# The attribute (uc03_attribute_mapping) the generic materialization pass must
# leave to this module.
BOOKING_DATE_ATTRIBUTE = "actual_booking_date"

BOOKING_FORM = "BOOKING_FORM"
BOOKING_DOCKET = "BOOKING_DOCKET"
ENTERED = "ENTERED"
BOOKING_DOCS_UPLOADED = "BOOKING_DOCS_UPLOADED"
JOURNEY_CREATED = "JOURNEY_CREATED"
FALLBACK_SOURCES = frozenset({BOOKING_DOCS_UPLOADED, JOURNEY_CREATED})
SOURCE_LABELS = {
    BOOKING_FORM: "Booking form",
    BOOKING_DOCKET: "Booking docket",
    ENTERED: "Entered",
    BOOKING_DOCS_UPLOADED: "Day booking documents were uploaded",
    JOURNEY_CREATED: "Day the journey was started",
}


def pricing_booking_date_sql(alias: str = "b") -> str:
    """SQL for the booking record's date as a pricing date or a check may use
    it: NULL when it is only a fallback (the day documents were uploaded or
    the journey started). ``alias`` is the bookings table's alias."""
    return (f"CASE WHEN {alias}.booking_date_source IN ('{BOOKING_DOCS_UPLOADED}', '{JOURNEY_CREATED}') "
            f"THEN NULL ELSE {alias}.booking_date END")


PRICING_BOOKING_DATE_SQL = pricing_booking_date_sql("b")


@dataclass(frozen=True)
class BookingDate:
    day: date
    source: str

    @property
    def is_fallback(self) -> bool:
        return self.source in FALLBACK_SOURCES


def _form_reading(connection: Connection, *, tenant_id: str, journey_id: Any) -> BookingDate | None:
    from audit_core.uc03_p2_dates import booking_form_date, unusable_pricing_date_reason

    form = booking_form_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    if form is None or form["date"] is None or unusable_pricing_date_reason(form["raw"]) is not None:
        return None
    return BookingDate(form["date"], BOOKING_FORM if form["documentType"] == "booking_form" else BOOKING_DOCKET)


def read_booking_date(connection: Connection, *, tenant_id: str, journey_id: Any) -> date | None:
    """Step 1: the booking date read from the Journey's ACTIVE booking form
    (else its booking docket), the PC's correction winning, when it can be
    right; None otherwise."""
    reading = _form_reading(connection, tenant_id=tenant_id, journey_id=journey_id)
    return reading.day if reading else None


def _held(connection: Connection, *, tenant_id: str, journey_id: Any) -> dict[str, Any] | None:
    row = connection.execute(
        text("SELECT booking_date, booking_date_source FROM auditcore.bookings WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one_or_none()
    return dict(row) if row else None


def _carried_by_a_document(connection: Connection, *, tenant_id: str, journey_id: Any, day: date) -> bool:
    """Whether any document reading of this Journey (any copy, any status)
    carries this booking date -- i.e. it was copied from a document, not
    entered by a person."""
    return bool(connection.execute(
        text(
            """
            SELECT EXISTS (SELECT 1 FROM auditcore.journey_document_extracted_fields f
                           WHERE f.tenant_id=:t AND f.journey_id=:j AND f.field_key='booking_date'
                             AND f.effective_value #>> '{}' = :d)
            """
        ),
        {"t": tenant_id, "j": journey_id, "d": day.isoformat()},
    ).scalar_one())


def entered_booking_date(connection: Connection, *, tenant_id: str, journey_id: Any,
                         held: dict[str, Any] | None = None) -> date | None:
    """Step 2: the date a person entered on the booking record, unless it is
    before the floor. A record whose source is not known yet (written before
    the source was kept) holds an entered date when no document reading
    carries it; one a document carries was copied from that document."""
    from audit_core.uc03_p2_dates import before_floor

    if held is None:
        held = _held(connection, tenant_id=tenant_id, journey_id=journey_id)
    if not held or held["booking_date"] is None or before_floor(held["booking_date"]):
        return None
    source = held["booking_date_source"]
    if source == ENTERED:
        return held["booking_date"]
    if source is None and not _carried_by_a_document(
            connection, tenant_id=tenant_id, journey_id=journey_id, day=held["booking_date"]):
        return held["booking_date"]
    return None


def pricing_booking_date(connection: Connection, *, tenant_id: str, journey_id: Any) -> date | None:
    """Steps 1-2 only: the booking date a deal may be priced on and a check
    may read. None when the Journey's booking date is only a fallback."""
    read = read_booking_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    if read is not None:
        return read
    return entered_booking_date(connection, tenant_id=tenant_id, journey_id=journey_id)


def booking_docs_uploaded_on(connection: Connection, *, tenant_id: str, journey_id: Any) -> date | None:
    """Step 3: the IST day of the Journey's first upload holding a
    booking-stage page. An upload as the Project Tracker counts one: not one
    still awaiting its file, nor a duplicate cancelled on arrival with no
    pages; a page cancelled since is not counted."""
    return connection.execute(
        text(
            """
            SELECT MIN(CAST(ub.created_at_utc AT TIME ZONE 'Asia/Kolkata' AS date))
            FROM auditcore.p2_upload_batches ub
            WHERE ub.tenant_id=:t AND ub.journey_id=:j
              AND ub.batch_status <> 'AWAITING_UPLOAD'
              AND NOT (ub.batch_status = 'CANCELLED' AND COALESCE(ub.page_count, 0) = 0)
              AND EXISTS (SELECT 1 FROM auditcore.p2_document_queue q
                          WHERE q.tenant_id=ub.tenant_id AND q.batch_id=ub.batch_id
                            AND q.business_stage='BOOKING' AND q.queue_status <> 'CANCELLED')
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one_or_none()


def journey_booking_date(connection: Connection, *, tenant_id: str, journey_id: Any) -> BookingDate:
    """THE booking date of a Journey and where it came from (steps 1-4)."""
    reading = _form_reading(connection, tenant_id=tenant_id, journey_id=journey_id)
    if reading is not None:
        return reading
    entered = entered_booking_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    if entered is not None:
        return BookingDate(entered, ENTERED)
    uploaded = booking_docs_uploaded_on(connection, tenant_id=tenant_id, journey_id=journey_id)
    if uploaded is not None:
        return BookingDate(uploaded, BOOKING_DOCS_UPLOADED)
    started = connection.execute(
        text("SELECT CAST(created_at_utc AT TIME ZONE 'Asia/Kolkata' AS date) FROM auditcore.journeys "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()
    return BookingDate(started, JOURNEY_CREATED)


def sync_booking_date(connection: Connection, *, tenant_id: str, journey_id: Any) -> str:
    """Store the Journey's booking date and its source on its booking record.

    A booking form / docket date or an entered one goes through the typed
    writer (its effective configuration and the "no price master in force"
    task follow); a fallback date is written as it is, with no pricing side
    effect. Returns "UNCHANGED", "FILLED" (the record held no date) or
    "UPDATED"."""
    from audit_core import uc03_booking_capture as booking_capture

    rule = journey_booking_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    held = _held(connection, tenant_id=tenant_id, journey_id=journey_id)
    held_day = held["booking_date"] if held else None
    if held and held_day == rule.day and held["booking_date_source"] == rule.source:
        return "UNCHANGED"
    if held_day != rule.day and not rule.is_fallback:
        booking_capture._write_typed_capture(
            connection, tenant_id=tenant_id, journey_id=UUID(str(journey_id)),
            field_key="BOOKING_DATE", value=rule.day, source_evidence_id=None,
        )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date, booking_date_source)
            VALUES (:t, :j, :d, :s)
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                booking_date = EXCLUDED.booking_date,
                booking_date_source = EXCLUDED.booking_date_source,
                updated_at_utc = now(),
                version_no = auditcore.bookings.version_no + 1
            """
        ),
        {"t": tenant_id, "j": journey_id, "d": rule.day, "s": rule.source},
    )
    return "FILLED" if held_day is None else "UPDATED"


def fill_missing_booking_dates(engine: Engine, tenant_id: str) -> dict[str, int]:
    """Worker start: bring to the rule every booking record that has no date,
    no source yet (written before the source was kept), a date before the
    programme's floor, or only a fallback date; and give a Journey with no
    booking record one. Running it again changes nothing. Each Journey in its
    own transaction, so one failure does not stop the rest. Returns the
    outcome counts."""
    from audit_core.db import set_tenant_context
    from audit_core.uc03_p2_dates import programme_date_floor

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        journeys = connection.execute(
            text(
                f"""
                SELECT j.journey_id
                FROM auditcore.journeys j
                LEFT JOIN auditcore.bookings b ON b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id
                WHERE j.tenant_id=:t AND (
                    b.booking_date IS NULL OR b.booking_date_source IS NULL
                    OR b.booking_date < COALESCE(CAST(:floor AS date), b.booking_date)
                    OR b.booking_date_source IN ('{BOOKING_DOCS_UPLOADED}', '{JOURNEY_CREATED}'))
                ORDER BY j.created_at_utc
                """
            ),
            {"t": tenant_id, "floor": programme_date_floor()},
        ).scalars().all()
    counts: dict[str, int] = {}
    for journey_id in journeys:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                outcome = sync_booking_date(connection, tenant_id=tenant_id, journey_id=journey_id)
        except Exception:
            outcome = "FAILED"
            logger.warning("booking_date_fill_failed", tenant_id=tenant_id, journey_id=str(journey_id),
                           exc_info=True)
        counts[outcome] = counts.get(outcome, 0) + 1
    logger.info("booking_dates_filled", tenant_id=tenant_id, checked=len(journeys), **counts)
    return counts
