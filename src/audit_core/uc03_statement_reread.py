"""Read the account statements again, once (2026-10-26).

customer_ledger 1.1 added the statement's printed net value ("Net Value" in
the ledger's total row). The statements read before it have none, so the
receipts-against-statement check skips them. Document Intelligence has been
taught to read an already-read document again on request (reprocess force);
this asks it to, for the statements that need it, and copies the new reading
into Audit Core.

Only a statement form with no printed net value is picked. A statement is
left alone, and listed by Journey, when
- a person corrected one of its values here (the copy would overwrite it),
- its stage's documents are marked reviewed (a re-copy would undo the
  figures the review attests to), or
- its page is not settled (still being read).
Document Intelligence refuses, too, a document a person verified or
corrected there (``has_corrections``).

A tag containing "-copy-" does not ask DI again: it only copies the pages DI
has already read (after a worker restart cut a run short).

Started from outside with AUDIT_CORE_STATEMENT_REREAD_TAG on the worker. A DRY
RUN (one ``statement_reread_item`` log line per statement, nothing asked)
unless the tag ends with "-commit". On commit each picked statement is asked
for, then polled until Document Intelligence has finished, and the document is
synced into the Journey's fields. The Deal is
then set from the statements by the account-statement rebuild
(AUDIT_CORE_ACCOUNT_STATEMENT_TAG), which is run separately. Safe to run
again: a statement that has its printed net value is no longer picked.
"""
from __future__ import annotations

import os
import time
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from audit_core.db import set_tenant_context
from audit_core.di_capture_v2_client import DiCaptureV2Error
from audit_core.uc03_account_statement import (
    ACCOUNT_STATEMENT_TYPE,
    KIND_FORM,
    line_rows,
    load_ledger_documents,
    read_statement,
    statement_kind,
    statement_total,
)
from audit_core.uc03_deal_source_policy import account_statement_is_source_of_truth

logger = structlog.get_logger(__name__)

_WAIT_SECONDS = int(os.environ.get("AUDIT_CORE_STATEMENT_REREAD_WAIT_SECONDS", "2400"))
_POLL_SECONDS = int(os.environ.get("AUDIT_CORE_STATEMENT_REREAD_POLL_SECONDS", "15"))
# DI's listing does not say that a PROCESSED document has a new reading queued
# (nextAttemptAtUtc is set only for a failing one), so a statement is copied
# only after this long since the request: DI read each one in about ten seconds
# on DEV (2026-10-26).
_SETTLE_SECONDS = int(os.environ.get("AUDIT_CORE_STATEMENT_REREAD_SETTLE_SECONDS", "300"))
_SETTLED_PAGE_STATES = ("READY", "NEEDS_REVIEW")


def _page(connection: Connection, tenant_id: str, journey_id: UUID, di_document_id: Any) -> dict[str, Any] | None:
    row = connection.execute(
        text(
            """
            SELECT queue_id, queue_status FROM auditcore.p2_document_queue
            WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d
            ORDER BY created_at_utc DESC LIMIT 1
            """
        ),
        {"t": tenant_id, "j": journey_id, "d": di_document_id},
    ).mappings().one_or_none()
    return dict(row) if row else None


def _corrected_fields(connection: Connection, tenant_id: str, journey_id: UUID, di_document_id: Any) -> int:
    return int(connection.execute(
        text(
            """
            SELECT count(*) FROM auditcore.journey_document_extracted_fields
            WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d
              AND (is_modified OR modified_value IS NOT NULL)
            """
        ),
        {"t": tenant_id, "j": journey_id, "d": di_document_id},
    ).scalar() or 0)


def statement_items(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    """Every customer_ledger statement of the tenant, with whether it needs
    reading again and why not when it is left alone."""
    from audit_core.uc03_p2_reviewer import page_stage_code, stage_is_reviewed

    journeys = connection.execute(
        text(
            """
            SELECT DISTINCT e.journey_id, j.journey_reference
            FROM auditcore.evidence e
            JOIN auditcore.journeys j ON j.tenant_id=e.tenant_id AND j.journey_id=e.journey_id
            WHERE e.tenant_id=:t AND e.association_status='ACTIVE' AND e.document_type_key=:type
            ORDER BY j.journey_reference
            """
        ),
        {"t": tenant_id, "type": ACCOUNT_STATEMENT_TYPE},
    ).mappings().all()
    items: list[dict[str, Any]] = []
    for journey in journeys:
        journey_id = UUID(str(journey["journey_id"]))
        for document in load_ledger_documents(connection, tenant_id=tenant_id, journey_id=journey_id):
            rows = line_rows(document["fields"].get("line_items"))
            kind = statement_kind(rows)
            read = read_statement(rows) if kind == KIND_FORM else None
            has_total = bool(read) and statement_total(document["fields"], read) is not None
            di_document_id = document["documentId"]
            page = _page(connection, tenant_id, journey_id, di_document_id)
            stage = page_stage_code(connection, tenant_id=tenant_id, journey_id=journey_id,
                                    di_document_id=UUID(str(di_document_id)))
            corrected = _corrected_fields(connection, tenant_id, journey_id, di_document_id)
            reviewed = bool(stage) and stage_is_reviewed(
                connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage)
            if kind != KIND_FORM:
                decision = "SKIP_NOT_A_STATEMENT_FORM"
            elif has_total:
                decision = "SKIP_HAS_PRINTED_TOTAL"
            elif page is None or stage is None:
                decision = "SKIP_PAGE_UNKNOWN"
            elif page["queue_status"] not in _SETTLED_PAGE_STATES:
                decision = "SKIP_PAGE_NOT_SETTLED"
            elif corrected:
                decision = "SKIP_CORRECTED_BY_A_PERSON"
            elif reviewed:
                decision = "SKIP_STAGE_REVIEWED"
            else:
                decision = "REREAD"
            items.append({
                "journeyId": journey_id, "journeyReference": journey["journey_reference"],
                "diDocumentId": di_document_id, "queueId": page["queue_id"] if page else None,
                "queueStatus": page["queue_status"] if page else None, "stage": stage, "kind": kind,
                "hasPrintedTotal": has_total, "correctedFields": corrected, "stageReviewed": reviewed,
                "decision": decision,
            })
    return items


def _di_status(v2: Any, *, token: str, tenant_id: str, context_ref: str, di_document_id: str) -> dict[str, Any] | None:
    """The document's listing entry. The stage a statement belongs to is not
    always the phase it was uploaded in, so both phases are looked at."""
    for phase in ("BOOKING", "DELIVERY"):
        listing = v2.list_documents(token=token, tenant_id=tenant_id, external_context_ref=context_ref, phase=phase)
        for item in listing.get("documents") or []:
            if str(item.get("documentId")) == di_document_id:
                return item
    return None


def _copy_into_audit_core(engine: Engine, tenant_id: str, item: dict[str, Any], tag: str) -> None:
    """Copy what Document Intelligence now holds for the statement into the
    Journey's fields: the document sync itself (the one a link callback runs),
    not the page reconcile, whose copy step leaves a page alone once it holds
    any value and so never writes the new reading."""
    from audit_core.uc03_confidence_review_policy import (
        _prefetch_document_for_sync,
        _sync_booking_document_once,
    )
    from audit_core.uc03_document_capture_v2 import (
        get_di_client,
        get_security_oauth_client,
    )
    from audit_core.uc03_p2_runtime import record_activity

    security, di = get_security_oauth_client(), get_di_client()
    document_id = UUID(str(item["diDocumentId"]))
    prefetched = _prefetch_document_for_sync(
        engine, tenant_id=tenant_id, journey_id=item["journeyId"], document_id=document_id,
        security_client=security, di_client=di)
    if prefetched is None:
        raise RuntimeError("the statement's evidence link is no longer active")
    _sync_booking_document_once(
        engine, tenant_id=tenant_id, journey_id=item["journeyId"], document_id=document_id,
        service_id="p2-worker", stage_code=str(item["stage"]).upper(),
        security_client=security, di_client=di, prefetched=prefetched)
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        record_activity(
            connection, tenant_id=tenant_id, journey_id=item["journeyId"], event_type="PAGE_RESYNC_REQUESTED",
            subject_type="DOCUMENT_PAGE", subject_id=str(item["queueId"]),
            details={"by": f"system:{tag}", "stageCode": item["stage"], "reason": "STATEMENT_REREAD"},
            correlation_id=f"statement-reread:{tag}",
        )


def _reread(engine: Engine, tenant_id: str, items: list[dict[str, Any]], tag: str, *, ask: bool = True) -> dict[str, int]:
    from audit_core.uc03_document_capture_v2 import (
        _DI_AUDIENCE,
        _ensure_di_context,
        get_di_capture_v2_client,
        get_di_client,
        get_security_oauth_client,
    )

    security, di, v2 = get_security_oauth_client(), get_di_client(), get_di_capture_v2_client()
    counts = {"asked": 0, "refused": 0, "finished": 0, "failed": 0, "timed_out": 0}
    pending: list[dict[str, Any]] = []
    for item in items:
        try:
            security.get_service_token(audience=_DI_AUDIENCE)
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                context_ref, token = _ensure_di_context(
                    connection=connection, engine=engine, tenant_id=tenant_id,
                    journey_id=item["journeyId"], security_client=security, di_client=di)
            if not ask:
                # "-copy-" tag: DI already read these; only copy what it holds.
                pending.append({**item, "contextRef": context_ref})
                continue
            outcome = v2.reprocess_document(
                token=token, tenant_id=tenant_id, external_context_ref=context_ref,
                document_id=str(item["diDocumentId"]), force=True)
        except (DiCaptureV2Error, RuntimeError):
            counts["failed"] += 1
            logger.warning("statement_reread_request_failed", tag=tag, tenant_id=tenant_id,
                           journey=item["journeyReference"], exc_info=True)
            continue
        result = str(outcome.get("outcome") or "")
        logger.info("statement_reread_requested", tag=tag, tenant_id=tenant_id, journey=item["journeyReference"],
                    di_document_id=str(item["diDocumentId"]), di_outcome=result,
                    processing_job_id=outcome.get("processingJobId"))
        if result in ("queued", "in_progress"):
            counts["asked"] += 1
            pending.append({**item, "contextRef": context_ref})
        else:
            counts["refused"] += 1
    started = time.monotonic()
    deadline = started + _WAIT_SECONDS
    while pending and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
        if ask and time.monotonic() - started < _SETTLE_SECONDS:
            continue
        token = security.get_service_token(audience=_DI_AUDIENCE)
        still: list[dict[str, Any]] = []
        for item in pending:
            try:
                status = _di_status(v2, token=token, tenant_id=tenant_id, context_ref=item["contextRef"],
                                    di_document_id=str(item["diDocumentId"]))
            except DiCaptureV2Error:
                still.append(item)
                continue
            if status is None or status.get("nextAttemptAtUtc"):
                still.append(item)  # DI still holds the reading
                continue
            processing = str(status.get("processingStatus") or "")
            if processing == "PROCESSED":
                try:
                    _copy_into_audit_core(engine, tenant_id, item, tag)
                except Exception:
                    counts["failed"] += 1
                    logger.warning("statement_reread_copy_failed", tag=tag, tenant_id=tenant_id,
                                   journey=item["journeyReference"], exc_info=True)
                    continue
                counts["finished"] += 1
                logger.info("statement_reread_copied", tag=tag, tenant_id=tenant_id, journey=item["journeyReference"])
            elif processing == "FAILED":
                counts["failed"] += 1
                logger.warning("statement_reread_failed", tag=tag, tenant_id=tenant_id, journey=item["journeyReference"],
                               failure_code=status.get("processingFailureCode"))
            else:
                still.append(item)
        pending = still
    counts["timed_out"] = len(pending)
    for item in pending:
        logger.warning("statement_reread_timed_out", tag=tag, tenant_id=tenant_id, journey=item["journeyReference"])
    return counts


def run_statement_reread(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    commit = tag.endswith("-commit")
    for tenant_id in tenant_ids:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                if not account_statement_is_source_of_truth(connection, tenant_id=tenant_id):
                    continue
                items = statement_items(connection, tenant_id)
        except Exception:
            logger.warning("statement_reread_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        totals: dict[str, int] = {}
        for item in items:
            totals[item["decision"]] = totals.get(item["decision"], 0) + 1
            logger.info("statement_reread_item", tag=tag, tenant_id=tenant_id, commit=commit,
                        **{k: str(v) for k, v in item.items()})
        results: dict[str, int] = {}
        if commit:
            try:
                results = _reread(engine, tenant_id, [i for i in items if i["decision"] == "REREAD"], tag,
                                  ask="-copy-" not in tag)
            except Exception:
                logger.warning("statement_reread_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
        logger.info("statement_reread_finished", tag=tag, tenant_id=tenant_id, commit=commit,
                    statements=len(items), **{f"decision_{k.lower()}": v for k, v in totals.items()}, **results)
