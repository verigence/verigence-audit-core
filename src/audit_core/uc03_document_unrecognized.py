"""uc03_document_unrecognized.py — surface documents DI could not classify.

DI's Capture V2 classifier sets a document's state to ``UNKNOWN`` when its
best-confidence guess against the tenant's known document types still falls
below the acceptance threshold -- it genuinely doesn't know what the
document is (as opposed to CLASSIFIED-but-not-yet-extracted, or FAILED).
That state was previously invisible: it never gets an ``evidence`` link (DI
only arms the audit-link webhook for a *successful* classification), so
Audit Core's document-sync pipeline (uc03_confidence_review_policy.py's
_sync_booking_document) never even sees it, and the capture screen's own
card status only distinguishes PROCESSED/FAILED -- everything else, UNKNOWN
included, renders as plain "Uploaded". A PC had no way to tell "still being
classified" from "DI gave up, a human needs to look at this."

DI simply not recognizing a document isn't a rule violation or a compliance
gap on its own -- it's a "someone please look at this and say what it
actually is" -- so this raises a standalone PC_VERIFY_UNRECOGNIZED_DOCUMENT
Task (no backing Audit Finding at all: workflow_tasks.related_finding_id is
left NULL) per such document, the moment a capture-screen read reconciles
Delivery's or Booking's live DI document list (uc03_delivery_capture_v2.py /
uc03_document_capture_v2.py -- the same place that state already gets
written durably into document_capture_v2_documents.capture_status). Carries
the document's filename and a direct view link so the PC can open it without
hunting through the capture list themselves. The Task auto-cancels once the
document is reclassified (a later DI pass changes its mind) or removed; a
PC completing it records one of two outcomes (tasks_api.py's complete_task):
CORRECT (dismiss, nothing to fix) or INCORRECT (soft-deletes the document by
marking it SUPERSEDED, same terminal status a re-upload already uses, so a
later upload can take its place against the same requirement).
"""
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.workflow import cancel_workflow_task, create_workflow_task

TASK_TYPE = "PC_VERIFY_UNRECOGNIZED_DOCUMENT"
_WORKFLOW_TYPE = "UC03_DOCUMENT_VERIFICATION"
_OPEN_TASK_STATUSES = {"PENDING", "READY", "CLAIMED", "IN_PROGRESS", "RETRY_WAIT"}

StageCode = Literal["BOOKING", "DELIVERY"]


def _effect_key(tenant_id: str, journey_id: UUID, stage_code: str, document_id: UUID) -> str:
    return f"task:document-unrecognized:{tenant_id}:{journey_id}:{stage_code}:{document_id}"


def _friendly_filename(raw: Any, document_id: UUID) -> str:
    text_value = str(raw).strip() if raw else ""
    return text_value or f"document {str(document_id)[:8]}"


def sync_document_unrecognized_findings(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: StageCode,
    di_documents: list[dict[str, Any]],
    correlation_id: str,
) -> dict[str, Any]:
    """Raise a PC_VERIFY_UNRECOGNIZED_DOCUMENT task for each currently-
    UNKNOWN document in this read's live DI list; cancel one whose document
    is no longer UNKNOWN (reclassified, or gone).

    Best-effort and idempotent -- safe to call on every capture-screen read.
    Never raises; a failure here must never break the read it's piggybacking on.
    """
    try:
        unrecognized = [
            item for item in di_documents
            if str(item.get("state") or "").upper() == "UNKNOWN"
        ]
    except Exception:  # noqa: BLE001 - producer must never break the caller
        return {"raised": 0, "resolved": 0, "error": True}

    raised = 0
    live_effect_keys: set[str] = set()
    for item in unrecognized:
        try:
            document_id = UUID(str(item["documentId"]))
        except (KeyError, ValueError):
            continue
        effect_key = _effect_key(tenant_id, journey_id, stage_code, document_id)
        live_effect_keys.add(effect_key)
        existing = connection.execute(
            text(
                "SELECT 1 FROM auditcore.workflow_tasks "
                "WHERE tenant_id = :tenant_id AND effect_key = :effect_key"
            ),
            {"tenant_id": tenant_id, "effect_key": effect_key},
        ).scalar_one_or_none()
        if existing is not None:
            continue
        filename = _friendly_filename(item.get("originalFilename"), document_id)
        create_workflow_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            workflow_type=_WORKFLOW_TYPE,
            process_area=stage_code,
            task_type=TASK_TYPE,
            assigned_role_code="PC",
            task_payload={
                "diDocumentId": str(document_id),
                "originalFilename": filename,
                "contentUrl": item.get("contentUrl"),
                "stageCode": stage_code,
                "comment": (
                    f'"{filename}" was uploaded but Document Intelligence could not '
                    "confidently identify it as any known document type. Open it, "
                    "confirm what it actually is, and record whether it's the "
                    "correct document (DI just couldn't classify it) or the wrong "
                    "one (it gets removed so you can upload the right one)."
                ),
            },
            effect_key=effect_key,
            correlation_id=correlation_id,
        )
        raised += 1

    open_tasks = connection.execute(
        text(
            """
            SELECT workflow_task_id, effect_key
            FROM auditcore.workflow_tasks
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND task_type = :task_type AND process_area = :stage_code
              AND task_status IN ('PENDING','READY','CLAIMED','IN_PROGRESS','RETRY_WAIT')
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "task_type": TASK_TYPE,
            "stage_code": stage_code,
        },
    ).mappings().all()

    resolved = 0
    for task in open_tasks:
        if task["effect_key"] in live_effect_keys:
            continue
        cancel_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task["workflow_task_id"],
            actor_id="SYSTEM",
            reason="Document reclassified or removed.",
        )
        resolved += 1

    return {"raised": raised, "resolved": resolved}


def apply_unrecognized_document_verification(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    di_document_id: UUID,
    outcome: str,
) -> None:
    """Completion side effect for a PC_VERIFY_UNRECOGNIZED_DOCUMENT task
    (tasks_api.py::complete_task). CORRECT is a plain dismissal -- DI just
    couldn't classify it, nothing to change. INCORRECT soft-deletes the
    document: capture_status becomes SUPERSEDED, the same terminal status a
    re-upload already leaves behind, so the row and its audit trail survive
    but it's no longer live, and a fresh upload can take its place. No hard
    delete and no outbound DI call, unlike the capture screen's own
    still-mid-review "remove" action -- this can fire well after that
    window (Booking/Delivery need not still be open), so it only ever
    touches Audit Core's own durable record.
    """
    if outcome != "INCORRECT":
        return
    connection.execute(
        text(
            """
            UPDATE auditcore.document_capture_v2_documents
            SET capture_status = 'SUPERSEDED', updated_at_utc = now()
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND stage_code = :stage_code AND di_document_id = :document_id
              AND capture_status <> 'SUPERSEDED'
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
            "document_id": di_document_id,
        },
    )
