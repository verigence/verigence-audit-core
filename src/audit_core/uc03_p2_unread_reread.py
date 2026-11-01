"""Documents whose read never finished are read again from their stored pages (product owner 2026-11-01).

23 to 26 October, documents waited longer than their 30 minutes for Document
Intelligence and were cancelled: "Not read within 30 minutes. Please upload
this page again." (NOT_READ_IN_30_MIN). Each was a document cut from the PC's
file -- one or more pages grouped as one unit -- and its pages had been read
one by one before the grouping, then voided as merged into it. With the
document cancelled, nothing of it is on the booking, and the booking shows
it missing (ADI-RD-261026-013's GST certificate, GSTIN 21ASIPB8600M1ZY, so
the GSTIN rule cannot name the customer).

Product owner: read them again from the stored pages, as whole documents (not
the page-by-page reads). This job does what "Send again" on the PC's
"Document to resubmit" task does (uc03_p2_api._send_page_again): a new unit
of the same pages, read as the same document type, its 30 minutes starting
now; the cancelled unit is marked replaced ("Sent again as ... by
Verigence"). When it is read it is linked like any document, and replaces a
copy on file the usual way.

A unit is read again only when all of these hold:
- it is a document unit (GROUP) cancelled or given up (CANCELLED, DEAD_LETTER)
  and never replaced since (last_error SUPERSEDED_PAGE is a PC's own resend
  or re-upload), and not refused for scan quality (DI_QUALITY_*);
- its file is the booking's own (not taken out as another customer's);
- the document it holds is missing from the booking: a page of it belonged to
  a requirement with nothing active now;
- its type is known (a template with a document type);
- the worker may still try it by itself (uc03_p2_retry_stop: a task about it
  closed by the PC, or the page removed, stops it).

p2_worker_jobs kind UNREAD_REREAD: a dry run (one ``unread_reread_item`` line
per document) unless the tag ends with "-commit"; at most _PER_RUN documents
are sent per run, oldest booking first, so the reader is never flooded --
run it again for the rest (a document sent is no longer a candidate).
"""
from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

_ACTOR = "p2-worker"
_ROLE = "Verigence"
_PER_RUN = int(os.environ.get("AUDIT_CORE_UNREAD_REREAD_PER_RUN", "40"))
_GIVEN_UP = ("CANCELLED", "DEAD_LETTER")


def candidates(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (g.queue_id)
                   g.queue_id, g.journey_id, j.journey_reference, b.original_filename, g.batch_id,
                   g.page_number, g.page_numbers, g.unit_kind, g.queue_status, g.last_error, g.client_upload_id,
                   g.di_document_id, g.di_state, g.page_object_key, g.page_sha256, g.business_stage,
                   g.template_key, g.classified_document_type, g.created_at_utc,
                   EXISTS (SELECT 1 FROM auditcore.p2_stage_document_reviews rv
                           WHERE rv.tenant_id=g.tenant_id AND rv.journey_id=g.journey_id) AS reviewed
            FROM auditcore.p2_document_queue g
            JOIN auditcore.journeys j ON j.tenant_id=g.tenant_id AND j.journey_id=g.journey_id
            JOIN auditcore.p2_upload_batches b ON b.tenant_id=g.tenant_id AND b.batch_id=g.batch_id
            JOIN auditcore.p2_document_queue pq
              ON pq.tenant_id=g.tenant_id AND pq.merged_into_queue_id=g.queue_id AND pq.unit_kind='PAGE'
            JOIN auditcore.evidence pe
              ON pe.tenant_id=pq.tenant_id AND pe.journey_id=pq.journey_id AND pe.di_document_id=pq.di_document_id
             AND pe.association_status IN ('SUPERSEDED', 'VOIDED')
            WHERE g.tenant_id=:t AND g.unit_kind='GROUP' AND g.queue_status = ANY(:given_up)
              AND COALESCE(g.last_error, '') <> 'SUPERSEDED_PAGE'
              AND COALESCE(g.last_error, '') NOT LIKE 'DI_QUALITY%'
              AND b.identity_enforced_at_utc IS NULL AND COALESCE(b.identity_status, '') <> 'FOREIGN'
              AND NOT EXISTS (SELECT 1 FROM auditcore.evidence a
                              WHERE a.tenant_id=pe.tenant_id AND a.journey_id=pe.journey_id
                                AND a.journey_document_requirement_id=pe.journey_document_requirement_id
                                AND a.association_status='ACTIVE')
              -- not already being read again (a resend of the same pages in flight or read)
              AND NOT EXISTS (SELECT 1 FROM auditcore.p2_document_queue n
                              WHERE n.tenant_id=g.tenant_id AND n.batch_id=g.batch_id AND n.queue_id<>g.queue_id
                                AND n.page_numbers=g.page_numbers AND n.created_at_utc > g.created_at_utc
                                AND n.queue_status NOT IN ('CANCELLED', 'MERGED'))
            ORDER BY g.queue_id
            """
        ),
        {"t": tenant_id, "given_up": list(_GIVEN_UP)},
    ).mappings().all()
    return sorted((dict(r) for r in rows), key=lambda r: (str(r["journey_reference"]), list(r["page_numbers"] or [])))


def _why_not(connection: Connection, tenant_id: str, unit: dict[str, Any]) -> tuple[str | None, Any]:
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_retry_stop import automatic_retry_stop_reason

    template = get_registry().documents.get(str(unit.get("template_key") or ""))
    if template is None or not template.di_types:
        return "its document type is not known", None
    stop = automatic_retry_stop_reason(connection, tenant_id=tenant_id, journey_id=unit["journey_id"],
                                       queue_id=unit["queue_id"])
    if stop:
        return stop, template
    return None, template


def run_unread_reread(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    commit = tag.endswith("-commit")
    for tenant_id in tenant_ids:
        totals = {"documents": 0, "would_send": 0, "sent": 0, "skipped": 0, "failed": 0, "left_for_next_run": 0}
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                units = candidates(connection, tenant_id)
        except Exception:
            logger.warning("unread_reread_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        for unit in units:
            totals["documents"] += 1
            try:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    why, template = _why_not(connection, tenant_id, unit)
                    outcome = "SKIPPED" if why else ("WOULD_SEND" if not commit else "SENT")
                    if why is None and commit and totals["sent"] >= _PER_RUN:
                        outcome, why = "NEXT_RUN", f"more than {_PER_RUN} in one run"
                    new_id = None
                    if outcome == "SENT":
                        new_id = _send(connection, tenant_id, unit, template, tag)
            except Exception:
                totals["failed"] += 1
                logger.warning("unread_reread_item_failed", tag=tag, tenant_id=tenant_id,
                               queue_id=str(unit["queue_id"]), exc_info=True)
                continue
            totals[{"SENT": "sent", "SKIPPED": "skipped", "NEXT_RUN": "left_for_next_run",
                    "WOULD_SEND": "would_send"}[outcome]] += 1
            logger.info("unread_reread_item", tag=tag, tenant_id=tenant_id, commit=commit, outcome=outcome,
                        reason=why, reference=unit["journey_reference"], file_name=unit["original_filename"],
                        pages=list(unit["page_numbers"] or [unit["page_number"]]),
                        document=template.display_name if template else unit.get("template_key"),
                        reviewed=bool(unit["reviewed"]), queue_id=str(unit["queue_id"]),
                        new_queue_id=str(new_id) if new_id else None, last_error=unit["last_error"])
        logger.info("unread_reread_finished", tag=tag, tenant_id=tenant_id, commit=commit, per_run=_PER_RUN, **totals)


def _send(connection: Connection, tenant_id: str, unit: dict[str, Any], template: Any, tag: str) -> UUID:
    from audit_core.uc03_p2_api import _send_page_again

    locked = connection.execute(
        text("SELECT queue_status, last_error FROM auditcore.p2_document_queue WHERE tenant_id=:t AND queue_id=:q "
             "FOR UPDATE"),
        {"t": tenant_id, "q": unit["queue_id"]},
    ).mappings().one()
    if locked["queue_status"] not in _GIVEN_UP or locked["last_error"] == "SUPERSEDED_PAGE":
        raise RuntimeError("the unit changed since it was listed")
    classified = str(unit.get("classified_document_type") or "")
    di_type = classified if classified in template.di_types else template.di_types[0]
    return _send_page_again(
        connection, tenant_id=tenant_id, journey_id=UUID(str(unit["journey_id"])), unit=unit, template=template,
        di_type=di_type, actor_id=_ACTOR, role=_ROLE, swept=True, event_type="PAGE_SENT_AGAIN",
        override_reason=None, correlation_id=f"unread-reread:{tag}",
    )
