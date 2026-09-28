"""Phase 2 document submission and upload status.

Submitting a stage's documents is the PC's "I have uploaded what I have"
checkpoint. It is allowed as soon as every uploaded page has been
classified, or 4 minutes after the stage's current upload window opened --
whichever comes first -- so a slow document never blocks the PC, and a PC
never submits before Document Intelligence has seen what they uploaded.

Submission is recorded on the existing journey workflow (capture complete
on journey_stage_states, a who/role/when event on journey_workflow_events),
runs the existing booking-submission rules (missing conditional documents,
discount evidence) and re-runs every check.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_engine, get_human_principal
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import check_p2_permission, resolve_p2_scope
from audit_core.uc03_p2_controls import request_control_evaluation
from audit_core.uc03_p2_runtime import note_facts_changed, record_activity
from audit_core.uc03_p2_workflow import last_submission, mark_documents_submitted

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-submission"])

SUBMIT_AFTER_SECONDS = 240
_NOT_CLASSIFIED = (
    "QUEUED", "PREPARING_PAGE", "DI_UPLOAD_PREPARING", "DI_UPLOADING", "DI_FINALIZING", "CLASSIFYING", "RETRY_WAIT",
)
_BATCH_IN_FLIGHT = ("AWAITING_UPLOAD", "UPLOADED", "SPLITTING")
_NOT_EXTRACTED = ("FAILED", "DEAD_LETTER", "NEEDS_REVIEW")


def upload_status(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """Counts a PC or TL watches while documents are processed, plus whether
    the stage's documents can be submitted yet."""
    row = connection.execute(
        text(
            f"""
            WITH units AS (
              SELECT q.queue_id, q.unit_kind, q.queue_status, q.page_sha256, q.created_at_utc,
                     q.merged_into_queue_id
              FROM auditcore.p2_document_queue q
              WHERE q.tenant_id=:t AND q.journey_id=:j AND q.queue_status <> 'CANCELLED'
            ),
            docs AS (SELECT * FROM units WHERE queue_status <> 'MERGED'),
            runtime AS (
              SELECT current_stage FROM auditcore.p2_journey_runtime WHERE tenant_id=:t AND journey_id=:j
            )
            SELECT
              (SELECT COUNT(*) FROM docs) AS documents,
              (SELECT COUNT(*) FROM units WHERE unit_kind='PAGE') AS pages,
              (SELECT COUNT(*) FROM docs WHERE queue_status NOT IN {_NOT_CLASSIFIED}) AS classified,
              (SELECT COUNT(*) FROM docs WHERE queue_status='READY') AS extracted,
              (SELECT COUNT(*) FROM docs WHERE queue_status='SUPPORTING') AS supporting,
              (SELECT COUNT(*) FROM docs WHERE queue_status IN {_NOT_EXTRACTED}) AS not_extracted,
              (SELECT COUNT(*) FROM docs WHERE queue_status IN {_NOT_CLASSIFIED}) AS not_classified,
              (SELECT COUNT(*) FROM (
                 SELECT page_sha256 FROM units
                 WHERE unit_kind='PAGE' AND page_sha256 IS NOT NULL
                 GROUP BY page_sha256 HAVING COUNT(*) > 1
               ) d) AS duplicate_groups,
              (SELECT COALESCE(SUM(n - 1), 0) FROM (
                 SELECT COUNT(*) AS n FROM units
                 WHERE unit_kind='PAGE' AND page_sha256 IS NOT NULL
                 GROUP BY page_sha256 HAVING COUNT(*) > 1
               ) d) AS duplicates,
              (SELECT COUNT(*) FROM auditcore.p2_upload_batches b
                WHERE b.tenant_id=:t AND b.journey_id=:j AND b.batch_status IN {_BATCH_IN_FLIGHT}) AS batches_in_flight,
              (SELECT current_stage FROM runtime) AS stage
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    stage = "DELIVERY" if str(row["stage"] or "").startswith("DELIVERY") else "BOOKING"
    submitted_at = last_submission(connection, tenant_id=tenant_id, journey_id=journey_id, stage=stage)
    # The window opens with the first upload after the stage's last submission.
    window_start = connection.execute(
        text(
            """
            SELECT MIN(created_at_utc) FROM auditcore.p2_upload_batches
            WHERE tenant_id=:t AND journey_id=:j
              AND (CAST(:since AS timestamptz) IS NULL OR created_at_utc > CAST(:since AS timestamptz))
            """
        ),
        {"t": tenant_id, "j": journey_id, "since": submitted_at},
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    elapsed = int((now - window_start).total_seconds()) if window_start else 0
    waiting = int(row["not_classified"] or 0) + int(row["batches_in_flight"] or 0)
    has_new_uploads = window_start is not None
    all_classified = has_new_uploads and waiting == 0
    timer_done = has_new_uploads and elapsed >= SUBMIT_AFTER_SECONDS
    can_submit = bool(all_classified or timer_done)
    if not has_new_uploads:
        reason = "Submitted. Upload more documents to submit again." if submitted_at else "Upload the documents first."
    elif all_classified:
        reason = "Every document has been identified."
    elif timer_done:
        reason = f"{waiting} document(s) are still being identified; you can submit now and they will be checked when ready."
    else:
        reason = f"Submit unlocks when every document is identified, or in {SUBMIT_AFTER_SECONDS - elapsed} seconds."
    return {
        "counts": {
            "documents": int(row["documents"] or 0),
            "pages": int(row["pages"] or 0),
            "uploading": int(row["batches_in_flight"] or 0),
            "classified": int(row["classified"] or 0),
            "extracted": int(row["extracted"] or 0),
            "supporting": int(row["supporting"] or 0),
            "notExtracted": int(row["not_extracted"] or 0),
            "notClassified": int(row["not_classified"] or 0),
            "duplicates": int(row["duplicates"] or 0),
        },
        "submission": {
            "stage": stage,
            "canSubmit": can_submit,
            "reason": reason,
            "windowStartedAtUtc": window_start,
            "secondsElapsed": elapsed,
            "unlockAfterSeconds": SUBMIT_AFTER_SECONDS,
            "secondsRemaining": 0 if can_submit else max(0, SUBMIT_AFTER_SECONDS - elapsed),
            "submittedAtUtc": submitted_at,
        },
    }


class SubmitCommand(BaseModel):
    stage: Literal["BOOKING", "DELIVERY"] | None = None


def _after_submit(engine: Engine, *, tenant_id: str, journey_id: UUID, stage: str, correlation_id: str) -> None:
    """Runs after the submission committed: the existing submission rules
    first, then every Phase 2 check against their results."""
    try:
        if stage == "BOOKING":
            from audit_core.uc03_booking_rule_trigger import (
                schedule_booking_checkpoint_rules,
            )

            schedule_booking_checkpoint_rules(
                engine, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
                trigger="P2_BOOKING_SUBMITTED", raise_new=True,
            )
        else:
            from audit_core.uc03_delivery_capture_v2 import (
                schedule_delivery_document_checkpoint,
            )

            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                schedule_delivery_document_checkpoint(
                    connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
                )
    except Exception:
        logger.warning("p2_submission_rules_failed", tenant_id=tenant_id, journey_id=str(journey_id),
                       stage=stage, exc_info=True)
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        request_control_evaluation(
            connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
            delay_seconds=0, force=True,
        )


@router.post("/journeys/{journey_id}:submit")
def submit_documents(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    background: BackgroundTasks,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    engine: Annotated[Engine, Depends(get_engine)],
    command: SubmitCommand | None = None,
) -> dict[str, Any]:
    decision = check_p2_permission(
        tenant_id=tenant_id, human_principal=human_principal,
        authorization_client=authorization_client, permission_key="audit.journey.update",
    )
    correlation_id = get_correlation_id(request)
    with engine.begin() as connection:
        access = resolve_p2_scope(
            connection, tenant_id=tenant_id, journey_id=journey_id, human_principal=human_principal, decision=decision,
        )
        status = upload_status(connection, tenant_id=tenant_id, journey_id=journey_id)
        submission = status["submission"]
        stage = (command.stage if command and command.stage else None) or submission["stage"]
        if not submission["canSubmit"]:
            raise HTTPException(status_code=409, detail=submission["reason"])
        mark_documents_submitted(
            connection, tenant_id=tenant_id, journey_id=journey_id, stage=stage, actor_id=human_principal.subject,
            actor_role=access.operating_role or access.functional_role, counts=status["counts"],
            correlation_id=correlation_id,
        )
        record_activity(
            connection, tenant_id=tenant_id, journey_id=journey_id, event_type="DOCUMENTS_SUBMITTED",
            subject_type="JOURNEY", subject_id=str(journey_id),
            details={"stage": stage, "role": access.operating_role or access.functional_role,
                     "counts": status["counts"]},
            correlation_id=correlation_id,
        )
        note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id,
                           reason=f"{stage}_SUBMITTED", correlation_id=correlation_id)
    background.add_task(_after_submit, engine, tenant_id=tenant_id, journey_id=journey_id, stage=stage,
                        correlation_id=correlation_id)
    return {"journeyId": str(journey_id), "stage": stage, "submittedAtUtc": datetime.now(UTC).isoformat()}
