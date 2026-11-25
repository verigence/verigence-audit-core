"""Every document that failed on our side is sent again by itself, once (product owner 22 Nov 2026).

"What happens with the failed journey uploads.. are they being reloaded or they
silently got missed out", then: "create one job at 8:15 am which handles all the
failed transactions".

Found on Mahindra Orissa that day: a page or document that failed kept its
stored file and its file's "Document to resubmit" task, the 9:30 pm clean-up
took it off the booking, and nothing more happened. The jobs that send such
failures again (OUR_SIDE_REUPLOAD, UNREAD_REREAD) ran only when someone started
them -- last on 16 Nov -- and about 414 pages in 105 files of 87 bookings
waited for a PC for reasons that were ours.

This job does it every morning (Jobs page: "Failed documents sent again",
8:15 am India time, P2_FAILED_RESEND) and whenever it is asked for
(p2_worker_jobs kind FAILED_RESEND, tag ``<tenant_id>:<label>``: a dry run
unless the label ends with "-commit"). What failed on our side:

- not read within its 30 minutes (60 for a Drive file): NOT_READ_IN_30_MIN;
- its pages could not be put together: GROUPING_FAILED;
- a system error stopped it after its retries (the code is the error's name);
- the AI's answer was refused by our reader after its retries:
  DOCUMENT_AI_RESPONSE_INVALID;
- the AI service could not take it after its retries (an outage, a rate
  limit, a provider error): DOCUMENT_AI_UNAVAILABLE, DOCUMENT_AI_RATE_LIMITED,
  CLASSIFICATION_PROVIDER_ERROR, EXTRACTION_PROVIDER_ERROR (25 Nov 2026);
- a file never split into pages, or whose upload the browser never confirmed.

A page refused for what it is -- scan quality, a duplicate, content the AI will
not read -- stays with the PC, as before.

Two steps, the first before the second:

1. A document a person made (the PC typed it, sent it again, or put its pages
   together) is read again as that person left it: the same pages, the same
   type, a scan the PC confirmed as good still confirmed. This is "Send again"
   on the PC's task (uc03_p2_api._send_page_again), done for the PC.
2. Every other failure: the whole stored file is uploaded again to the same
   booking (uc03_p2_our_side_reupload). A page already read is not read again,
   the failed ones are replaced, and their task closes.

Once, and only once: a document this job sent again, or a file it uploaded
again, that fails a second time stays with the PC. Nothing is sent for a
booking that is closed or whose documents are marked reviewed, a file taken
out as another customer's, a task the PC closed by hand or a page a person
removed (uc03_p2_retry_stop), or a file the PC has uploaded again since.

At most _DOCUMENTS and _FILES in one run, oldest first, so the reader is never
flooded; what is left over goes the next morning.
"""
from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

JOB = "FAILED_RESEND"
RUN_NAME = "P2_FAILED_RESEND"
DAILY_LABEL = "daily-"
_ACTOR = "p2-worker"
_ROLE = "Verigence"
_SENT_AGAIN_EVENT = "PAGE_SENT_AGAIN"
_DOCUMENTS = int(os.environ.get("AUDIT_CORE_FAILED_RESEND_DOCUMENTS", "60"))
_FILES = int(os.environ.get("AUDIT_CORE_FAILED_RESEND_FILES", "40"))


def daily_tag(tenant_id: str, day: str) -> str:
    """The tag of the morning's own run for ``day`` (a date): it sends, with no dry run before it."""
    return f"{tenant_id}:{DAILY_LABEL}{day}-commit"


def is_daily_tag(tag: str | None) -> bool:
    _tenant, sep, label = str(tag or "").partition(":")
    return bool(sep) and label.strip().startswith(DAILY_LABEL)


def person_made(connection: Connection, tenant_id: str, references: list[str] | None = None) -> list[dict[str, Any]]:
    """Documents a person made that failed on our side and still wait for the PC, oldest first."""
    from audit_core.uc03_p2_our_side_reupload import OUR_SIDE_CODES, OUR_SIDE_ERROR
    from audit_core.uc03_p2_page_removal import _OPEN_JOURNEY
    from audit_core.uc03_p2_runtime import swept_pending_sql

    rows = connection.execute(
        text(
            f"""
            SELECT q.queue_id, q.journey_id, j.journey_reference, q.batch_id, b.original_filename,
                   q.page_number, q.page_numbers, q.unit_kind, q.queue_status, q.last_error, q.page_sha256,
                   q.page_object_key, q.template_key, q.classified_document_type, q.quality_confirmed,
                   EXISTS (SELECT 1 FROM auditcore.p2_stage_document_reviews rv
                           WHERE rv.tenant_id=q.tenant_id AND rv.journey_id=q.journey_id) AS reviewed,
                   -- made by this job (or by UNREAD_REREAD) from a failed one: it had its second read
                   EXISTS (SELECT 1 FROM auditcore.p2_activity_events e
                           WHERE e.tenant_id=q.tenant_id AND e.journey_id=q.journey_id
                             AND e.event_type=:sent_again AND e.details->>'by'=:actor
                             AND e.details->>'newQueueId'=q.queue_id::text) AS sent_again_before
            FROM auditcore.p2_document_queue q
            JOIN auditcore.journeys j ON j.tenant_id=q.tenant_id AND j.journey_id=q.journey_id
            JOIN auditcore.p2_upload_batches b ON b.tenant_id=q.tenant_id AND b.batch_id=q.batch_id
            WHERE q.tenant_id=:t AND q.unit_kind='GROUP' AND q.group_source='PC'
              AND (q.last_error = ANY(:codes) OR q.last_error ~ :system_error)
              AND (q.queue_status IN ('FAILED','DEAD_LETTER') OR {swept_pending_sql("q")})
              AND {_OPEN_JOURNEY}
              AND b.identity_enforced_at_utc IS NULL AND COALESCE(b.identity_status, '') <> 'FOREIGN'
              AND (cardinality(CAST(:refs AS text[])) = 0 OR j.journey_reference = ANY(CAST(:refs AS text[])))
            ORDER BY q.created_at_utc, q.queue_id
            """
        ),
        {"t": tenant_id, "codes": list(OUR_SIDE_CODES), "system_error": OUR_SIDE_ERROR,
         "sent_again": _SENT_AGAIN_EVENT, "actor": _ACTOR, "refs": list(references or [])},
    ).mappings().all()
    return [dict(r) for r in rows]


def _why_not(connection: Connection, tenant_id: str, unit: dict[str, Any]) -> str | None:
    from audit_core.uc03_p2_retry_stop import automatic_retry_stop_reason

    if unit["reviewed"]:
        return "the booking's documents are marked reviewed"
    if unit["sent_again_before"]:
        return "it was sent again once already and failed again: it stays with the PC"
    if not unit["page_object_key"]:
        return "no stored page is recorded"
    return automatic_retry_stop_reason(connection, tenant_id=tenant_id, journey_id=unit["journey_id"],
                                       queue_id=unit["queue_id"])


def _as_the_person_left_it(unit: dict[str, Any]) -> tuple[Any, str | None]:
    """The document type the person gave it (None when it has none of its own) and how DI is told."""
    from audit_core.uc03_p2_api import _OTHER_DOCUMENT_TEMPLATE
    from audit_core.uc03_p2_registry import get_registry

    template = get_registry().documents.get(str(unit.get("template_key") or ""))
    if template is None or template.key == _OTHER_DOCUMENT_TEMPLATE or not template.di_types:
        return None, None
    classified = str(unit.get("classified_document_type") or "")
    return template, (classified if classified in template.di_types else template.di_types[0])


def _send(connection: Connection, tenant_id: str, unit: dict[str, Any], tag: str) -> UUID:
    from audit_core.uc03_p2_api import _send_page_again
    from audit_core.uc03_p2_runtime import swept_pending_sql

    locked = connection.execute(
        text(f"SELECT q.queue_status, q.last_error, {swept_pending_sql('q')} AS swept "
             "FROM auditcore.p2_document_queue q WHERE q.tenant_id=:t AND q.queue_id=:q FOR UPDATE"),
        {"t": tenant_id, "q": unit["queue_id"]},
    ).mappings().one()
    if locked["last_error"] != unit["last_error"] or not (
            locked["swept"] or locked["queue_status"] in ("FAILED", "DEAD_LETTER")):
        raise RuntimeError("the document changed since it was listed")
    template, di_type = _as_the_person_left_it(unit)
    return _send_page_again(
        connection, tenant_id=tenant_id, journey_id=UUID(str(unit["journey_id"])), unit=unit, template=template,
        di_type=di_type, actor_id=_ACTOR, role=_ROLE, swept=bool(locked["swept"]), event_type=_SENT_AGAIN_EVENT,
        override_reason=None, correlation_id=f"failed-resend:{tag}",
        quality_confirmed=bool(unit.get("quality_confirmed")),
    )


def _person_made_step(engine: Engine, tenant_id: str, *, tag: str, commit: bool,
                      references: list[str] | None) -> dict[str, Any]:
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        units = person_made(connection, tenant_id, references)
    totals = {"documents": len(units), "would_send": 0, "sent": 0, "skipped": 0, "left_for_next_run": 0, "failed": 0}
    items: list[dict[str, Any]] = []
    for unit in units:
        item = {"reference": unit["journey_reference"], "file": unit["original_filename"],
                "pages": list(unit["page_numbers"] or [unit["page_number"]]), "queueId": str(unit["queue_id"]),
                "failure": str(unit["last_error"])[:80], "type": unit["template_key"],
                "scanConfirmedByPc": bool(unit["quality_confirmed"])}
        why = None
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                why = _why_not(connection, tenant_id, unit)
                if why:
                    outcome = "SKIPPED"
                elif not commit:
                    outcome = "WOULD_SEND"
                elif totals["sent"] >= _DOCUMENTS:
                    outcome, why = "NEXT_RUN", f"more than {_DOCUMENTS} in one run"
                else:
                    item["newQueueId"] = str(_send(connection, tenant_id, unit, tag))
                    outcome = "SENT"
        except Exception as exc:  # one document never stops the others
            outcome, why = "FAILED", f"{type(exc).__name__}: {getattr(exc, 'detail', None) or exc}"[:300]
            logger.warning("failed_resend_item_failed", tag=tag, queue_id=str(unit["queue_id"]), exc_info=True)
        item["outcome"], item["why"] = outcome, why
        totals[{"WOULD_SEND": "would_send", "SENT": "sent", "SKIPPED": "skipped", "NEXT_RUN": "left_for_next_run",
                "FAILED": "failed"}[outcome]] += 1
        items.append(item)
        logger.info("failed_resend_item", tag=tag, tenant_id=tenant_id, commit=commit, **item)
    return {**totals, "items": items}


def run_failed_resend(engine: Engine, tag: str, references: list[str] | None = None) -> dict[str, Any]:
    """Both steps for one Project. What was found and done is returned (kept on the job row)."""
    from audit_core.uc03_p2_our_side_reupload import parse_tag, run_our_side_reupload

    tenant_id, commit = parse_tag(tag)
    documents = _person_made_step(engine, tenant_id, tag=tag, commit=commit, references=references)
    files = run_our_side_reupload(engine, tag, references, per_run=_FILES)
    logger.info("failed_resend_finished", tag=tag, tenant_id=tenant_id, commit=commit,
                documents_sent=documents["sent"], documents_would_send=documents["would_send"],
                documents_skipped=documents["skipped"], documents_failed=documents["failed"],
                files_sent=files["sent"], files_would_send=files["would_send"], files_skipped=files["skipped"],
                files_not_in_storage=files["not_in_storage"], files_failed=files["failed"],
                left_for_next_run=documents["left_for_next_run"] + files["left_for_next_run"])
    return {"commit": commit, "documents": documents, "files": files}


def request_daily(connection: Connection, *, tenant_id: str, day: str) -> bool:
    """Ask the worker for the morning's run of ``day``; a second ask for the same day does nothing."""
    tag = daily_tag(tenant_id, day)
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_worker_jobs (job_kind, tag, requested_by)
            SELECT CAST(:k AS varchar), CAST(:tag AS varchar), 'system'
            WHERE NOT EXISTS (SELECT 1 FROM auditcore.p2_worker_jobs
                              WHERE job_kind = CAST(:k AS varchar) AND tag = CAST(:tag AS varchar))
            RETURNING job_id
            """
        ),
        {"k": JOB, "tag": tag},
    ).scalar_one_or_none() is not None
