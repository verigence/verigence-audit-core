"""Repair Insurance agent / MISP codes that hold a phone number (2026-10-27).

Confirmed live: an insurance policy's MISP line reads "Name: ADITYA MOTORS
Contact Number: 7873999101 Code: MIBL/M&M/AAECA5786A/000", and Journeys showed
the contact number as the Agent code -- 20 on Mahindra Orissa. Two causes, both
fixed: Document Intelligence's instructions (insurance_cover 1.2 now says a
contact number is never a code, and its parser drops a phone-shaped code), and
Audit Core taking a retired or older reading over the newest live one.

This job repairs what is already stored, per Journey whose agent or MISP code
looks like a phone number:

1. rebuild the Journey's values from the readings Audit Core holds -- the
   newest live reading now wins, which fixes a Journey whose newest reading was
   already right. The rebuild only fills empty columns, so a phone-shaped code
   is cleared first. The change is kept when a proper code takes its place, or
   when the column stays empty because no current reading holds the phone
   number any more (the policy prints only a contact number there, and the
   new reading rightly has no code). If a current reading still holds it (not
   read again yet, or its stage reviewed), the Journey is rolled back and
   listed (live 2026-10-27: the rebuild alone changed nothing);
2. if the code still looks like a phone number, ask Document Intelligence to
   read the Journey's live insurance pages again under the new instructions
   (reprocess force) and copy the new reading in -- the same request, wait and
   copy the statement re-read uses.

A page is left alone, and listed, when a person corrected one of its values
here, its stage is marked reviewed, or it is still being read. Document
Intelligence refuses, too, a document a person verified or corrected there.

Started as a worker job (kind INSURANCE_CODE_REPAIR). A DRY RUN -- one
``insurance_code_repair_item`` log line per Journey, nothing changed -- unless
the tag ends with "-commit". Safe to run again: a Journey whose codes are no
longer phone-shaped is not picked.
"""
from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

_INSURANCE_TYPES = ("insurance_cover", "insurance_cover_note", "insurance_policy")
_SETTLED_PAGE_STATES = ("READY", "NEEDS_REVIEW")
_PHONE = re.compile(r"(?:\+?91|0)?[6-9]\d{9}|1800\d{6,7}|0\d{2,4}\d{6,8}")


def looks_like_phone_number(value: Any) -> bool:
    """An Indian mobile, landline or toll-free number, with or without spaces,
    dashes or a +91/0 prefix. A real code (MIBL/M&M/AAECA5786A/000) never matches."""
    if value is None:
        return False
    return bool(_PHONE.fullmatch(re.sub(r"[\s\-().]", "", str(value))))


def affected_journeys(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT r.journey_id, j.journey_reference, r.agent_intermediary_code, r.misp_code
            FROM auditcore.insurance_records r
            JOIN auditcore.journeys j ON j.tenant_id = r.tenant_id AND j.journey_id = r.journey_id
            WHERE r.tenant_id = :t
            ORDER BY j.journey_reference
            """
        ),
        {"t": tenant_id},
    ).mappings().all()
    return [dict(r) for r in rows
            if looks_like_phone_number(r["agent_intermediary_code"]) or looks_like_phone_number(r["misp_code"])]


def insurance_pages(connection: Connection, tenant_id: str, journey: dict[str, Any]) -> list[dict[str, Any]]:
    """The Journey's live insurance pages, with whether each may be read again."""
    from audit_core.uc03_p2_reviewer import page_stage_code, stage_is_reviewed

    journey_id = UUID(str(journey["journey_id"]))
    pages = connection.execute(
        text(
            """
            SELECT DISTINCT ON (q.di_document_id) q.queue_id, q.queue_status, q.di_document_id
            FROM auditcore.p2_document_queue q
            WHERE q.tenant_id = :t AND q.journey_id = :j AND q.di_document_id IS NOT NULL
              AND q.retired_at_utc IS NULL AND q.queue_status NOT IN ('MERGED', 'CANCELLED')
              AND q.classified_document_type = ANY(CAST(:types AS text[]))
            ORDER BY q.di_document_id, q.created_at_utc DESC
            """
        ),
        {"t": tenant_id, "j": journey_id, "types": list(_INSURANCE_TYPES)},
    ).mappings().all()
    items: list[dict[str, Any]] = []
    for page in pages:
        di_document_id = UUID(str(page["di_document_id"]))
        stage = page_stage_code(connection, tenant_id=tenant_id, journey_id=journey_id, di_document_id=di_document_id)
        corrected = int(connection.execute(
            text(
                """
                SELECT count(*) FROM auditcore.journey_document_extracted_fields
                WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d
                  AND (is_modified OR modified_value IS NOT NULL)
                """
            ),
            {"t": tenant_id, "j": journey_id, "d": di_document_id},
        ).scalar() or 0)
        reviewed = bool(stage) and stage_is_reviewed(connection, tenant_id=tenant_id, journey_id=journey_id,
                                                     stage_code=stage)
        if stage is None:
            decision = "SKIP_PAGE_UNKNOWN"
        elif page["queue_status"] not in _SETTLED_PAGE_STATES:
            decision = "SKIP_PAGE_NOT_SETTLED"
        elif corrected:
            decision = "SKIP_CORRECTED_BY_A_PERSON"
        elif reviewed:
            decision = "SKIP_STAGE_REVIEWED"
        else:
            decision = "REREAD"
        items.append({"journeyId": journey_id, "journeyReference": journey["journey_reference"],
                      "diDocumentId": di_document_id, "queueId": page["queue_id"],
                      "queueStatus": page["queue_status"], "stage": stage, "decision": decision})
    return items


_CODE_COLUMNS = ("agent_intermediary_code", "misp_code")


class _KeepAsIs(Exception):
    """No proper code replaced the phone number: roll the Journey back."""


def _codes(connection: Connection, tenant_id: str, journey_id: UUID) -> dict[str, Any] | None:
    row = connection.execute(
        text(
            """
            SELECT agent_intermediary_code, misp_code, source_kind
            FROM auditcore.insurance_records WHERE tenant_id = :t AND journey_id = :j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def _current_readings_say_no_code(connection: Connection, tenant_id: str, journey_id: UUID, field_key: str) -> bool:
    """The readings that still speak for the Journey -- active evidence, a page
    not merged, cancelled or retired -- exist for this field and none of them
    holds a phone number: the policy was read and has no code there."""
    values = connection.execute(
        text(
            """
            SELECT f.effective_value
            FROM auditcore.journey_document_extracted_fields f
            JOIN auditcore.evidence e ON e.tenant_id = f.tenant_id AND e.di_document_id = f.di_document_id
            WHERE f.tenant_id = :t AND f.journey_id = :j AND f.field_key = :k
              AND e.association_status = 'ACTIVE'
              AND f.source_document_type_key = ANY(CAST(:types AS text[]))
              AND NOT EXISTS (
                  SELECT 1 FROM auditcore.p2_document_queue q
                  WHERE q.tenant_id = f.tenant_id AND q.di_document_id = f.di_document_id
                  HAVING bool_and(q.retired_at_utc IS NOT NULL OR q.queue_status IN ('MERGED', 'CANCELLED'))
              )
            """
        ),
        {"t": tenant_id, "j": journey_id, "k": field_key, "types": list(_INSURANCE_TYPES)},
    ).scalars().all()
    return bool(values) and not any(looks_like_phone_number(v) for v in values)


def _rebuild(engine: Engine, tenant_id: str, journey_id: UUID) -> list[str]:
    """Clear the phone-shaped codes and rebuild from the newest live readings.
    Returns the columns that now hold a proper code; anything else is rolled back."""
    from audit_core.uc03_delivery_post_extraction_materialization import (
        materialize_booking_documents_from_durable_store,
        materialize_delivery_documents_from_durable_store,
    )

    try:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            before = _codes(connection, tenant_id, journey_id)
            # Only values Audit Core copied from documents; never one a person entered.
            if before is None or before["source_kind"] != "EVIDENCE":
                return []
            wrong = [c for c in _CODE_COLUMNS if looks_like_phone_number(before[c])]
            if not wrong:
                return []
            connection.execute(
                text(
                    f"""
                    UPDATE auditcore.insurance_records SET {', '.join(f'{c} = NULL' for c in wrong)}
                    WHERE tenant_id = :t AND journey_id = :j
                    """
                ),
                {"t": tenant_id, "j": journey_id},
            )
            materialize_booking_documents_from_durable_store(connection, tenant_id=tenant_id, journey_id=journey_id)
            materialize_delivery_documents_from_durable_store(connection, tenant_id=tenant_id, journey_id=journey_id)
            after = _codes(connection, tenant_id, journey_id) or {}
            fixed = [
                c for c in wrong
                if (after.get(c) and not looks_like_phone_number(after.get(c)))
                or (after.get(c) is None and _current_readings_say_no_code(connection, tenant_id, journey_id, c))
            ]
            if fixed != wrong:
                raise _KeepAsIs
            return fixed
    except _KeepAsIs:
        return []


def run_insurance_code_repair(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    from audit_core.uc03_statement_reread import _reread

    commit = tag.endswith("-commit")
    for tenant_id in tenant_ids:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                before = affected_journeys(connection, tenant_id)
        except Exception:
            logger.warning("insurance_code_repair_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        rebuilt = 0
        still = 0
        to_reread: list[dict[str, Any]] = []
        skipped: dict[str, int] = {}
        for journey in before:
            journey_id = UUID(str(journey["journey_id"]))
            if commit:
                try:
                    _rebuild(engine, tenant_id, journey_id)
                except Exception:
                    logger.warning("insurance_code_repair_rebuild_failed", tag=tag, tenant_id=tenant_id,
                                   journey=journey["journey_reference"], exc_info=True)
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                now_wrong = any(j["journey_id"] == journey["journey_id"] for j in affected_journeys(connection, tenant_id))
                pages = insurance_pages(connection, tenant_id, journey) if now_wrong else []
            if commit and not now_wrong:
                rebuilt += 1
            for page in pages:
                if page["decision"] == "REREAD":
                    to_reread.append(page)
                else:
                    skipped[page["decision"]] = skipped.get(page["decision"], 0) + 1
            logger.info("insurance_code_repair_item", tag=tag, tenant_id=tenant_id, commit=commit,
                        journey=journey["journey_reference"],
                        agent_code_phone=looks_like_phone_number(journey["agent_intermediary_code"]),
                        misp_code_phone=looks_like_phone_number(journey["misp_code"]),
                        fixed_by_rebuild=bool(commit and not now_wrong),
                        pages=[{k: str(v) for k, v in p.items() if k != "journeyId"} for p in pages])
        results: dict[str, int] = {}
        if commit and to_reread:
            try:
                results = _reread(engine, tenant_id, to_reread, tag)
            except Exception:
                logger.warning("insurance_code_repair_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
        if commit:
            # The re-read copies the new reading in, but the rebuild only fills
            # empty columns: clear and rebuild once more with the new readings.
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                left = affected_journeys(connection, tenant_id)
            for journey in left:
                try:
                    _rebuild(engine, tenant_id, UUID(str(journey["journey_id"])))
                except Exception:
                    logger.warning("insurance_code_repair_rebuild_failed", tag=tag, tenant_id=tenant_id,
                                   journey=journey["journey_reference"], exc_info=True)
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                remaining = affected_journeys(connection, tenant_id)
                still = len(remaining)
            for journey in remaining:
                logger.info("insurance_code_repair_left", tag=tag, tenant_id=tenant_id,
                            journey=journey["journey_reference"],
                            agent_code_phone=looks_like_phone_number(journey["agent_intermediary_code"]),
                            misp_code_phone=looks_like_phone_number(journey["misp_code"]))
        logger.info("insurance_code_repair_finished", tag=tag, tenant_id=tenant_id, commit=commit,
                    journeys=len(before), fixed_by_rebuild=rebuilt, pages_to_reread=len(to_reread),
                    still_phone_shaped=still if commit else None,
                    **{f"skipped_{k.lower()}": v for k, v in skipped.items()},
                    **{f"reread_{k}": v for k, v in results.items()})
