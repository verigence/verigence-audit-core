"""Open tasks that link to a replaced copy of a document (2026-11-16).

Pages read one by one and then read again with the pages around them are
merged into the grouped copy; the page copy is superseded and removed from
Document Intelligence. Tasks raised from the page copy kept pointing at it,
because a refresh changed a task only when its issue changed and the document
a task opens is not part of the issue: "Enter the booking date on the Booking
Docket" for UTK-SA-261103-044 opened "Document was not found" (VAC-NF-000).
On 16 Nov 50 open tasks on Mahindra Orissa (36 booking-date tasks) were in
that state.

``raise_or_refresh`` now follows the document (``document_pointers``). This
job, per Project, runs the document rules again (``_sync_rule_tasks``) on each
Journey holding an open task that points at a copy no longer in use, so each
such task is refreshed onto the copy in use (or closed, if the rule no longer
fails). Links the rules do not refresh still open the copy in use: the review
endpoint answers VAC-NF-034 with ``currentDocumentId``.

Started as worker job ``TASK_DOCUMENT_REPOINT``. A DRY RUN (each Journey run
in a savepoint and rolled back, counts logged) unless the tag ends with
"-commit". Idempotent: a second run finds no task pointing at a replaced copy.
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_task_producer import _OPEN

logger = structlog.get_logger(__name__)

JOB = "TASK_DOCUMENT_REPOINT"

# An open task whose reference names a document that is neither a live
# capture of the Journey nor ACTIVE evidence on it: what the review endpoint
# cannot open (uc03_p2_documents._document_context).
_STALE_TASKS_SQL = """
    SELECT DISTINCT t.task_id, t.journey_id, t.source_code
    FROM auditcore.p2_tasks t
    CROSS JOIN LATERAL (
        SELECT t.reference->>'documentId' AS doc
        UNION SELECT t.reference->>'referenceDocumentId'
        UNION SELECT jsonb_array_elements_text(
            CASE WHEN jsonb_typeof(t.reference->'documentIds') = 'array'
                 THEN t.reference->'documentIds' ELSE '[]'::jsonb END)
    ) d
    WHERE t.tenant_id = :t AND t.task_status = ANY(:open)
      AND d.doc ~ '^[0-9a-fA-F-]{36}$'
      AND NOT EXISTS (
        SELECT 1 FROM auditcore.document_capture_v2_documents x
        WHERE x.tenant_id = t.tenant_id AND x.journey_id = t.journey_id
          AND x.di_document_id = d.doc::uuid AND x.capture_status <> 'SUPERSEDED')
      AND NOT EXISTS (
        SELECT 1 FROM auditcore.evidence e
        WHERE e.tenant_id = t.tenant_id AND e.journey_id = t.journey_id
          AND e.di_document_id = d.doc::uuid AND e.association_status = 'ACTIVE')
"""


def stale_tasks(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(text(_STALE_TASKS_SQL), {"t": tenant_id, "open": list(_OPEN)}).mappings().all()
    return [dict(row) for row in rows]


def repoint_tenant(connection: Connection, *, tenant_id: str, commit: bool) -> dict[str, Any]:
    from audit_core.uc03_p2_worker import _sync_rule_tasks

    before = stale_tasks(connection, tenant_id)
    journeys = sorted({str(row["journey_id"]) for row in before})
    summary: dict[str, Any] = {
        "staleTasks": len(before),
        "byCode": dict(Counter(str(row["source_code"]) for row in before)),
        "journeys": len(journeys),
        "failedJourneys": 0,
    }
    for journey_id in journeys:
        savepoint = connection.begin_nested()
        try:
            _sync_rule_tasks(connection, tenant_id=tenant_id, journey_id=UUID(journey_id),
                             started_at=datetime.now(UTC))
        except Exception:
            savepoint.rollback()
            summary["failedJourneys"] += 1
            logger.warning("task_document_repoint_journey_failed", tenant_id=tenant_id, journey_id=journey_id,
                           exc_info=True)
            continue
        if commit:
            savepoint.commit()
            continue
        # Dry run: count what this Journey would leave stale, then undo it.
        left = [row for row in stale_tasks(connection, tenant_id) if str(row["journey_id"]) == journey_id]
        summary["dryRunStillStale"] = summary.get("dryRunStillStale", 0) + len(left)
        savepoint.rollback()
    if commit:
        after = stale_tasks(connection, tenant_id)
        summary["stillStale"] = len(after)
        summary["stillStaleByCode"] = dict(Counter(str(row["source_code"]) for row in after))
    return summary


def run_task_document_repoint(engine: Engine, tag: str, tenant_ids: list[str]) -> dict[str, Any]:
    commit = tag.endswith("-commit")
    results: dict[str, Any] = {"commit": commit}
    for tenant_id in tenant_ids:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                summary = repoint_tenant(connection, tenant_id=tenant_id, commit=commit)
        except Exception as exc:
            logger.warning("task_document_repoint_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            results[tenant_id] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            continue
        if summary["staleTasks"]:
            results[tenant_id] = summary
        logger.info("task_document_repoint_finished", tag=tag, tenant_id=tenant_id, commit=commit,
                    **{k: (v if isinstance(v, int) else str(v)) for k, v in summary.items()})
    return results
