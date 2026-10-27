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
   already right;
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


def _rebuild(engine: Engine, tenant_id: str, journey_id: UUID) -> None:
    from audit_core.uc03_delivery_post_extraction_materialization import (
        materialize_booking_documents_from_durable_store,
        materialize_delivery_documents_from_durable_store,
    )

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        materialize_booking_documents_from_durable_store(connection, tenant_id=tenant_id, journey_id=journey_id)
        materialize_delivery_documents_from_durable_store(connection, tenant_id=tenant_id, journey_id=journey_id)


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
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                still = len(affected_journeys(connection, tenant_id))
        logger.info("insurance_code_repair_finished", tag=tag, tenant_id=tenant_id, commit=commit,
                    journeys=len(before), fixed_by_rebuild=rebuilt, pages_to_reread=len(to_reread),
                    still_phone_shaped=still if commit else None,
                    **{f"skipped_{k.lower()}": v for k, v in skipped.items()},
                    **{f"reread_{k}": v for k, v in results.items()})
