"""Apply the field corrections still waiting for a Team Lead (2026-10-30).

Until 2026-10-30 a PC's correction of a value read at 90% confidence or more
was not applied: it raised "Review correction" (``FIELD_CORRECTION_REVIEW_P2``)
for a Team Lead, and the PC's own task stayed open until it was approved.
Product owner 2026-10-30: the PC's change goes through and the Team Lead and
Reviewer are informed only. New corrections now apply at once
(``correct_p2_document_field``); this applies the ones raised before, through
the same approval the Team Lead would have given, so the PC's tasks re-verify
and close.

A proposal whose field changed after it was made is left open (the approval
refuses it) and logged, for a person to decide.

Started as worker job ``WAITING_CORRECTIONS``. A DRY RUN (one
``waiting_correction_item`` log line per proposal, nothing written) unless the
tag ends with "-commit". Idempotent: an applied proposal is closed.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_runtime import record_activity
from audit_core.uc03_p2_tasks import TaskStateError, submit_action

logger = structlog.get_logger(__name__)

_COMMENT = ("Applied as entered by the PC: from 2026-10-30 PC corrections go through "
            "and the Team Lead and Reviewer are informed.")


def _waiting(engine: Engine, tenant_id: str) -> list[dict[str, Any]]:
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        rows = connection.execute(
            text(
                """
                SELECT task_id, journey_id, reference, raised_by_actor_id, raised_by_role_code
                FROM auditcore.p2_tasks
                WHERE tenant_id = :t AND task_type = 'FIELD_CORRECTION_REVIEW_P2'
                  AND task_status NOT IN ('VERIFIED_COMPLETE', 'CANCELLED', 'FAILED', 'DEAD_LETTER')
                ORDER BY created_at_utc
                """
            ),
            {"t": tenant_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def apply_waiting_correction(connection, *, tenant_id: str, task: dict[str, Any], tag: str) -> str:
    """Approve one waiting proposal; returns the outcome."""
    reference = dict(task["reference"] or {})
    try:
        submit_action(
            connection, tenant_id=tenant_id, task_id=UUID(str(task["task_id"])),
            action="APPROVE_CORRECTION", actor_id="SYSTEM", actor_role_code="TL", comment=_COMMENT,
            details={"job": "WAITING_CORRECTIONS", "tag": tag},
        )
    except TaskStateError as exc:
        return f"LEFT_OPEN: {exc}"
    record_activity(
        connection, tenant_id=tenant_id, journey_id=UUID(str(task["journey_id"])),
        event_type="FIELD_CORRECTED_FOR_INFORMATION", subject_type="DOCUMENT",
        subject_id=str(reference.get("documentId")),
        details={
            "fieldKey": reference.get("fieldKey"),
            "previousValue": reference.get("currentEffectiveValue"),
            "newValue": reference.get("proposedValue"),
            "confidence": reference.get("confidenceScore"),
            "documentTypeKey": reference.get("documentTypeKey"),
            "actorId": task.get("raised_by_actor_id"),
            "actorRole": task.get("raised_by_role_code") or "PC",
            "remarks": reference.get("proposalRemarks"),
            "informed": ["TL", "REVIEWER"],
            "appliedFromTask": str(task["task_id"]),
        },
    )
    return "APPLIED"


def run_waiting_corrections(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    commit = tag.endswith("-commit")
    for tenant_id in tenant_ids:
        totals = {"waiting": 0, "applied": 0, "left_open": 0, "failed": 0}
        journeys: set[str] = set()
        try:
            waiting = _waiting(engine, tenant_id)
        except Exception:
            logger.warning("waiting_corrections_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        for task in waiting:
            totals["waiting"] += 1
            reference = dict(task["reference"] or {})
            outcome = "DRY_RUN"
            if commit:
                try:
                    with engine.begin() as connection:
                        set_tenant_context(connection, tenant_id)
                        outcome = apply_waiting_correction(connection, tenant_id=tenant_id, task=task, tag=tag)
                except Exception:
                    totals["failed"] += 1
                    logger.warning("waiting_correction_item_failed", tag=tag, tenant_id=tenant_id,
                                   task_id=str(task["task_id"]), exc_info=True)
                    continue
                if outcome == "APPLIED":
                    totals["applied"] += 1
                    journeys.add(str(task["journey_id"]))
                else:
                    totals["left_open"] += 1
            logger.info("waiting_correction_item", tag=tag, tenant_id=tenant_id, commit=commit,
                        task_id=str(task["task_id"]), journey_id=str(task["journey_id"]),
                        field=str(reference.get("fieldKey")), outcome=outcome)
        logger.info("waiting_corrections_finished", tag=tag, tenant_id=tenant_id, commit=commit,
                    journeys=len(journeys), **totals)
