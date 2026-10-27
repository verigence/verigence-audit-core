"""One-off re-send of pages that failed because of us, not because of the page.

2026-10-21: 25 pages in the last days failed for reasons on our side -- the
document service refused the upload slot (fixed on 20 Oct evening, but those
pages were never sent again), a document-service error, and AI answers our
reader refused (fixed on 21 Oct morning). Pages refused for scan quality
(DI_QUALITY_*) are the page's own problem and are never re-sent here.

Started from outside by setting AUDIT_CORE_PAGE_RESEND_TAG on the worker. A dry
run unless the tag ends with "-commit": it lists what it would re-send. On
commit each page goes through the same recovery a PC's Retry uses
(request_page_recovery: look at what Document Intelligence holds first, send
again only if DI gave it up). Only FAILED pages are picked, so a second run
finds nothing to do.

A page whose task the PC has closed is never re-sent (product owner
2026-10-27, uc03_p2_retry_stop): "if the task is closed, then automatic
retries shouldn't happen".
"""
from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_retry_stop import automatic_retry_stop_reason
from audit_core.uc03_p2_runtime import request_page_recovery

logger = structlog.get_logger(__name__)

_DAYS = int(os.environ.get("AUDIT_CORE_PAGE_RESEND_DAYS", "4"))

# last_error prefixes that mean the page itself is the problem, or a failure
# a re-send cannot fix until its own bug is fixed.
# AUDIT_CORE_PAGE_RESEND_SKIP overrides the list (comma-separated), e.g. to
# re-send facts-copy failures once the bug behind them is fixed.
_NOT_RESENT = tuple(
    prefix.strip()
    for prefix in os.environ.get("AUDIT_CORE_PAGE_RESEND_SKIP", "DI_QUALITY_,FACTS_COPY_FAILED").split(",")
    if prefix.strip()
)


def _candidates(connection, tenant_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT q.queue_id, q.journey_id, j.journey_reference, b.original_filename, q.page_number,
                   q.di_document_id, coalesce(q.last_error, '') AS last_error
            FROM auditcore.p2_document_queue q
            JOIN auditcore.journeys j ON j.tenant_id = q.tenant_id AND j.journey_id = q.journey_id
            JOIN auditcore.p2_upload_batches b ON b.tenant_id = q.tenant_id AND b.batch_id = q.batch_id
            WHERE q.tenant_id = :t AND q.queue_status = 'FAILED'
              AND q.created_at_utc > now() - make_interval(days => :days)
            ORDER BY q.created_at_utc
            """
        ),
        {"t": tenant_id, "days": _DAYS},
    ).mappings().all()
    picked = []
    for row in rows:
        if str(row["last_error"]).startswith(_NOT_RESENT):
            continue
        why = automatic_retry_stop_reason(connection, tenant_id=tenant_id, journey_id=row["journey_id"],
                                          queue_id=row["queue_id"])
        if why:
            logger.info("page_resend_skipped", tenant_id=tenant_id, queue_id=str(row["queue_id"]), why=why)
            continue
        picked.append(dict(row))
    return picked


def run_page_resend(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    commit = tag.endswith("-commit")
    for tenant_id in tenant_ids:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                pages = _candidates(connection, tenant_id)
                for page in pages:
                    logger.info(
                        "page_resend_item", tag=tag, tenant_id=tenant_id, commit=commit,
                        journey=page["journey_reference"], file=str(page["original_filename"])[:80],
                        page=page["page_number"], had_di_document=page["di_document_id"] is not None,
                        reason=str(page["last_error"])[:160],
                    )
                    if commit:
                        request_page_recovery(
                            connection, tenant_id=tenant_id, journey_id=UUID(str(page["journey_id"])),
                            queue_id=UUID(str(page["queue_id"])), marker="r",
                            requested_by=f"system:{tag}", correlation_id=f"page-resend:{tag}",
                        )
                logger.info("page_resend_finished", tag=tag, tenant_id=tenant_id, commit=commit, pages=len(pages))
        except Exception:
            logger.warning("page_resend_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
