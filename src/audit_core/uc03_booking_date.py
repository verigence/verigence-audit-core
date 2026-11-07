"""The booking date of a Journey: one rule, one stored copy (2026-11-07).

The rule (product owner rules R5, 2026-10-20, and decision 2026-09-30): the
booking date printed on the Journey's booking form -- or, without one, its
booking docket -- as read, or as the PC corrected it, provided it can be
right (not before the programme's date floor, not after today); else the
booking date held on the booking record (entered by hand). Never guessed and
never today. It is the date the deal is priced on
(``uc03_sku_candidates.journey_pricing_date``) and the date the "Enter the
booking date" task asks for (``uc03_p2_task_producer.sync_booking_date_task``).

The stored copy is ``auditcore.bookings.booking_date``. Every list, filter and
report reads that column in SQL (Journey list and search, work items, the
Team Lead's supervisory view, the compliance report, the journey export), so
the column has to hold the rule's answer. ``sync_booking_date`` writes it
from the documents on every recompute; ``fill_missing_booking_dates`` (on
every worker start) fills the records left empty before it existed and
repairs those holding a date only a superseded / voided copy carries.

Why the column was empty (found 2026-11-07, Mahindra Orissa: 208 of 1,204
Journeys, 207 created 20 Oct - 7 Nov, dropped from a booking-date export): the
column was only written by the generic machine-materialization pass
(``uc03_post_extraction_materialization``), which writes the ONE winning
reading per field across every document ever read on the Journey. Two
things made the winner unusable while the active booking docket carried a
good date:

* a superseded, voided or another customer's copy of the booking docket
  could win the tie-break, and the typed writer then refuses it ("Unsupported
  evidence": the evidence is no longer ACTIVE) -- the pass logs and skips,
  and the active copy is never tried;
* a Delivery-stage document carrying a "booking date" (the customer ledger)
  outranks every Booking-stage one, and the pass writes only Booking-stage
  winners -- so again nothing is written, even when the ledger read nothing.

The booking date is therefore taken out of that pass and written here, from
the same reading the deal is priced on.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

logger = structlog.get_logger(__name__)

# The attribute (uc03_attribute_mapping) the generic materialization pass must
# leave to this module.
BOOKING_DATE_ATTRIBUTE = "actual_booking_date"


def read_booking_date(connection: Connection, *, tenant_id: str, journey_id: Any) -> date | None:
    """The booking date read from the Journey's ACTIVE booking form (else its
    booking docket), the PC's correction winning over the reading, when it
    can be right; None when there is no such document, it carries no date,
    or the date is a misread (before the floor, after today, not a date)."""
    from audit_core.uc03_p2_dates import booking_form_date, unusable_pricing_date_reason

    form = booking_form_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    if form is None or form["date"] is None or unusable_pricing_date_reason(form["raw"]) is not None:
        return None
    return form["date"]


def journey_booking_date(connection: Connection, *, tenant_id: str, journey_id: Any) -> date | None:
    """THE booking date of a Journey: the booking date read from its booking
    form / docket when it can be right, else the one held on the booking
    record (entered by hand) unless it lies before the programme's floor (a
    misread, never a fact -- ``uc03_p2_dates.before_floor``), else None. The
    database already refuses a held date after today."""
    from audit_core.uc03_p2_dates import before_floor

    read = read_booking_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    if read is not None:
        return read
    held = connection.execute(
        text("SELECT booking_date FROM auditcore.bookings WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one_or_none()
    return None if held is None or before_floor(held) else held


# A held date that only a copy no longer on file carries: the booking docket's
# reading copied onto the record while that copy was ACTIVE, then superseded or
# voided while the generic pass could no longer write the active copy's date
# (2026-11-07: 58 records on Mahindra Orissa, many a misread year -- 2016-10-10
# held while the active docket reads 2026-10-18). A date a person entered is
# carried by no document reading, or by an active one, and is never stale.
_STALE_COPY_SQL = """
SELECT EXISTS (
         SELECT 1 FROM auditcore.journey_document_extracted_fields f
         WHERE f.tenant_id=:t AND f.journey_id=:j AND f.field_key='booking_date'
           AND f.effective_value #>> '{}' = CAST(:held AS text)
           AND NOT EXISTS (SELECT 1 FROM auditcore.evidence e
                           WHERE e.tenant_id=f.tenant_id AND e.journey_id=f.journey_id
                             AND e.di_document_id=f.di_document_id AND e.association_status='ACTIVE'))
   AND NOT EXISTS (
         SELECT 1 FROM auditcore.journey_document_extracted_fields f
         JOIN auditcore.evidence e ON e.tenant_id=f.tenant_id AND e.journey_id=f.journey_id
          AND e.di_document_id=f.di_document_id AND e.association_status='ACTIVE'
         WHERE f.tenant_id=:t AND f.journey_id=:j AND f.field_key='booking_date'
           AND f.effective_value #>> '{}' = CAST(:held AS text))
"""

Replace = Literal["ANY", "STALE_COPY", "NONE"]


def sync_booking_date(
    connection: Connection, *, tenant_id: str, journey_id: Any, replace: Replace = "ANY",
) -> str:
    """Store the Journey's booking date on its booking record.

    Writes the booking date read from the booking form / docket into an empty
    record. A held, different date is replaced according to ``replace``:
    "ANY" (the recompute: the record follows the documents, as the deal's
    pricing date does), "STALE_COPY" (only a date that just a superseded /
    voided copy carries, see ``_STALE_COPY_SQL``), "NONE" (never). The record
    is never cleared: a Journey whose form carries no usable date keeps what
    is held.

    Returns "FILLED" (was empty), "UPDATED" (replaced), "UNCHANGED" (already
    the same), "NOT_READ" (nothing usable on the documents) or "KEPT" (a held
    date ``replace`` does not allow replacing)."""
    from audit_core import uc03_booking_capture as booking_capture

    read = read_booking_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    if read is None:
        return "NOT_READ"
    held = connection.execute(
        text("SELECT booking_date FROM auditcore.bookings WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one_or_none()
    if held == read:
        return "UNCHANGED"
    if held is not None and replace != "ANY":
        stale = replace == "STALE_COPY" and connection.execute(
            text(_STALE_COPY_SQL), {"t": tenant_id, "j": journey_id, "held": held.isoformat()},
        ).scalar_one()
        if not stale:
            return "KEPT"
    # Through the typed writer (and the wrappers installed on it): the date's
    # effective configuration and the "no price master in force" task follow.
    booking_capture._write_typed_capture(
        connection, tenant_id=tenant_id, journey_id=UUID(str(journey_id)),
        field_key="BOOKING_DATE", value=read, source_evidence_id=None,
    )
    return "FILLED" if held is None else "UPDATED"


def fill_missing_booking_dates(engine: Engine, tenant_id: str) -> dict[str, int]:
    """Backfill, on every worker start: each booking record of the tenant
    that holds no booking date gets the one read from its booking form /
    docket, and one holding a date only a superseded / voided copy carries
    gets the active copy's. Never replaces any other held value (a date a
    person entered stays); running it again changes nothing. Each Journey in
    its own transaction, so one failure does not stop the rest. Returns the
    outcome counts."""
    from audit_core.db import set_tenant_context

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        journeys = connection.execute(
            text(
                """
                SELECT j.journey_id
                FROM auditcore.journeys j
                LEFT JOIN auditcore.bookings b ON b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id
                WHERE j.tenant_id=:t AND (
                    b.booking_date IS NULL
                    OR NOT EXISTS (
                        SELECT 1 FROM auditcore.journey_document_extracted_fields f
                        JOIN auditcore.evidence e ON e.tenant_id=f.tenant_id AND e.journey_id=f.journey_id
                         AND e.di_document_id=f.di_document_id AND e.association_status='ACTIVE'
                        WHERE f.tenant_id=j.tenant_id AND f.journey_id=j.journey_id
                          AND f.field_key='booking_date'
                          AND f.effective_value #>> '{}' = CAST(b.booking_date AS text)))
                ORDER BY j.created_at_utc
                """
            ),
            {"t": tenant_id},
        ).scalars().all()
    counts: dict[str, int] = {}
    for journey_id in journeys:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                outcome = sync_booking_date(connection, tenant_id=tenant_id, journey_id=journey_id,
                                            replace="STALE_COPY")
        except Exception:
            outcome = "FAILED"
            logger.warning("booking_date_fill_failed", tenant_id=tenant_id, journey_id=str(journey_id),
                           exc_info=True)
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome in ("FILLED", "UPDATED"):
            logger.info("booking_date_filled", tenant_id=tenant_id, journey_id=str(journey_id), outcome=outcome)
    logger.info("booking_dates_filled", tenant_id=tenant_id, checked=len(journeys), **counts)
    return counts
