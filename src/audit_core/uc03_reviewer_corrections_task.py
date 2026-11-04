"""The Team Lead's one information task for a Reviewer's corrections on a journey.

Product owner, 2026-11-04: "reviewer should be able to edit the scanned
documents and correct the data. So all the data which is corrected by Reviewer
should go as one information task to TL combined."

A Reviewer's correction applies at once and nobody approves it (owner decision
2026-10-18); the field change log is the trail. This module turns that trail
into ONE task per journey for the Team Lead, listing every value the Reviewer
corrected on it: the document, the field, the value before and after, who and
when, and the Reviewer's remark.

- One task per journey (a stable dedupe key), refreshed with every correction.
- The Team Lead closes it with a comment once seen (CLOSE_TASK). A later
  correction reopens it, because the list -- and so the issue hash -- changed
  (raise_or_refresh); the comment that closed it stays in its history.
- The Reviewer does not close it: it informs the Team Lead about the
  Reviewer's own changes (``submit_action`` refuses, and the Reviewer's queue
  leaves it out).

Built from ``journey_field_change_log`` alone, so it is the same answer however
often it is rebuilt.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from audit_core.uc03_p2_tasks import REVIEWER_CORRECTIONS_TASK

# The description lists this many; the reference carries all of them.
_LISTED = 12
_KEPT = 200


def task_key(journey_id: UUID | str) -> str:
    return f"reviewer-corrections:{journey_id}"


def _shown(value: Any) -> str:
    if value is None:
        return "(blank)"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if value is None or value == "":
        return "(blank)"
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def reviewer_corrections(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """Every value a Reviewer corrected on the journey, oldest first."""
    rows = connection.execute(
        text(
            """
            SELECT l.change_id, l.stage_code, l.di_document_id, l.field_key,
                   l.previous_value, l.new_value, l.confidence_score,
                   l.actor_id, l.remarks, l.changed_at_utc,
                   d.classified_document_type_key AS document_type, d.original_filename
            FROM auditcore.journey_field_change_log l
            LEFT JOIN LATERAL (
                SELECT dc.classified_document_type_key, dc.original_filename
                FROM auditcore.document_capture_v2_documents dc
                WHERE dc.tenant_id = l.tenant_id AND dc.journey_id = l.journey_id
                  AND dc.di_document_id = l.di_document_id
                LIMIT 1
            ) d ON true
            WHERE l.tenant_id = :t AND l.journey_id = :j
              AND upper(l.actor_role_code) = 'REVIEWER'
              AND l.change_reason = 'CORRECTION'
            ORDER BY l.changed_at_utc, l.change_id
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    return [
        {
            "changeId": str(r["change_id"]),
            "stageCode": r["stage_code"],
            "documentId": str(r["di_document_id"]) if r["di_document_id"] else None,
            "documentType": r["document_type"],
            "fileName": r["original_filename"],
            "fieldKey": r["field_key"],
            "previousValue": _shown(r["previous_value"]),
            "newValue": _shown(r["new_value"]),
            "confidence": float(r["confidence_score"]) if r["confidence_score"] is not None else None,
            "reviewerActorId": r["actor_id"],
            "remarks": r["remarks"],
            "changedAtUtc": r["changed_at_utc"].isoformat() if r["changed_at_utc"] else None,
        }
        for r in rows
    ]


def _line(item: dict[str, Any]) -> str:
    where = item["documentType"] or item["fileName"] or "document"
    text_ = f"{item['fieldKey']} on {where}: {item['previousValue']} → {item['newValue']}"
    return f"{text_} ({item['remarks']})" if item.get("remarks") else text_


def refresh_reviewer_corrections_task(connection: Connection, *, tenant_id: str, journey_id: UUID) -> UUID | None:
    """Raise, refresh or reopen the journey's one Reviewer-corrections task."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    items = reviewer_corrections(connection, tenant_id=tenant_id, journey_id=journey_id)
    if not items:
        return None
    count = len(items)
    listed = "; ".join(_line(item) for item in items[-_LISTED:])
    more = f" (the latest {_LISTED} of {count} shown; all are in the task's detail)" if count > _LISTED else ""
    description = (
        f"For information: the Reviewer corrected {count} value{'s' if count != 1 else ''} read from this "
        f"journey's documents{more}: {listed}. The corrections are already applied. Close this task with a "
        "comment once seen; a later correction opens it again."
    )
    task_id, _ = raise_or_refresh(
        connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=task_key(journey_id),
        task_type=REVIEWER_CORRECTIONS_TASK, source_type="DOCUMENT_FIELD", source_code=REVIEWER_CORRECTIONS_TASK,
        title=f"Reviewer corrected {count} value{'s' if count != 1 else ''}",
        description=description,
        # ``fields`` is part of the issue hash (uc03_p2_task_producer._issue_hash):
        # a new correction is a new issue, so a task the TL closed opens again.
        reference={"sourceCode": REVIEWER_CORRECTIONS_TASK, "correctionCount": count,
                   "fields": [item["changeId"] for item in items], "corrections": items[-_KEPT:]},
        severity="MEDIUM", registry=get_registry(),
    )
    return task_id


def run_reviewer_corrections_backfill(engine: Any, tag: str, tenant_ids: list[str]) -> dict[str, Any]:
    """Raise the Team Lead's one information task for every journey whose
    Reviewer corrections predate the task (product owner 2026-11-04: "yes" to
    the backfill of the corrections made before it existed).

    Worker job ``REVIEWER_CORRECTIONS_BACKFILL``. A DRY RUN -- counts only,
    nothing written -- unless the tag ends with "-commit". Idempotent: a
    journey's task is raised or refreshed by its stable key, so a second run
    changes nothing."""
    import structlog

    from audit_core.db import set_tenant_context

    logger = structlog.get_logger(__name__)
    commit = tag.endswith("-commit")
    report: dict[str, Any] = {"tag": tag, "commit": commit, "tenants": {}}
    for tenant_id in tenant_ids:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            journeys = [r[0] for r in connection.execute(
                text("SELECT DISTINCT journey_id FROM auditcore.journey_field_change_log WHERE tenant_id=:t "
                     "AND upper(actor_role_code)='REVIEWER' AND change_reason='CORRECTION'"),
                {"t": tenant_id},
            ).all()]
        totals = {"journeys": len(journeys), "raised": 0, "failed": 0}
        if commit:
            for journey_id in journeys:
                try:
                    with engine.begin() as connection:
                        set_tenant_context(connection, tenant_id)
                        if refresh_reviewer_corrections_task(connection, tenant_id=tenant_id,
                                                             journey_id=UUID(str(journey_id))):
                            totals["raised"] += 1
                except Exception:
                    totals["failed"] += 1
                    logger.warning("reviewer_corrections_backfill_item_failed", tag=tag, tenant_id=tenant_id,
                                   journey_id=str(journey_id), exc_info=True)
        if journeys:
            report["tenants"][tenant_id] = totals
        logger.info("reviewer_corrections_backfill_finished", tag=tag, tenant_id=tenant_id, commit=commit, **totals)
    return report
