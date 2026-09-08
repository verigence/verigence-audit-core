"""uc03_async_sync_tasks.py — escalation wrappers around the machine-run,
idempotent, self-healing sync producers (SKU resolution, payment
reconciliation) so a persistent automation failure surfaces to the PC instead
of leaving a panel silently blank forever.

Both producers already never raise, and already return ``{"error": True}`` on
an unexpected internal failure -- as opposed to the *expected* "0 or >1
matches" / "nothing to reconcile yet" outcomes, which they already handle
through their own finding types (``MODEL_NOT_IDENTIFIED``,
``PAYMENT_UNVERIFIED``). These wrappers add exactly one thing: turn a
``{"error": True}`` into an ``AUTOMATED_SYNC_FAILURE`` finding (DATA_GAP / PC),
and resolve it the next time the same step succeeds.

Deliberately NOT a deferred/background task queue: ``sync_model_resolution``
and ``reconcile_payments`` are single-journey, DB-only, and already measured
fast (sub-second even against a large price list). They run inline, exactly
where every other producer in this codebase runs -- at the document-confirm
trigger and again on self-heal-on-read -- matching the existing convention
(``_sync_booking_document`` already calls ``sync_model_resolution`` this way;
``materialize_reviewed_delivery_business_values`` already calls
``reconcile_payments`` this way). A queue would add machinery this workload
doesn't need.
"""
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

import structlog
from sqlalchemy import Connection, text

from audit_core.uc03_delivery_commands import _machine_flag
from audit_core.uc03_manual_verification import _resolve_finding
from audit_core.uc03_model_resolution import sync_model_resolution
from audit_core.uc03_payment_reconciliation import reconcile_payments

logger = structlog.get_logger(__name__)

_FINDING_TYPE = "AUTOMATED_SYNC_FAILURE"
StageCode = Literal["BOOKING", "DELIVERY"]
TaskType = Literal["SKU_RESOLUTION", "PAYMENT_RECONCILIATION"]

_TASK_LABELS: dict[TaskType, str] = {
    "SKU_RESOLUTION": "match the vehicle model against the price masters",
    "PAYMENT_RECONCILIATION": "reconcile payments against the bank statement",
}


def _rule_key(task_type: TaskType, journey_id: UUID) -> str:
    return f"{_FINDING_TYPE}:{task_type}:{journey_id}"


def _flag_failure(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: StageCode,
    task_type: TaskType,
    correlation_id: str,
) -> None:
    label = _TASK_LABELS[task_type]
    _machine_flag(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=stage_code,
        rule_key=_rule_key(task_type, journey_id),
        finding_type=_FINDING_TYPE,
        severity="MEDIUM",
        title=f"Automatic {'model matching' if task_type == 'SKU_RESOLUTION' else 'payment reconciliation'} could not complete",
        description=(
            f"The system tried to automatically {label} and hit an unexpected "
            "error. This does not block the journey, but the result needs a "
            "PC to check directly."
        ),
        correlation_id=correlation_id,
        safe_payload={"taskType": task_type},
        blocking_completion=False,
    )


def _resolve_failure(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: StageCode,
    task_type: TaskType,
    correlation_id: str,
) -> None:
    rule_key = _rule_key(task_type, journey_id)
    finding_id = connection.execute(
        text(
            """
            SELECT audit_finding_id
            FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND rule_key=:rule_key AND finding_status IN ('OPEN','ACKNOWLEDGED')
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "rule_key": rule_key},
    ).scalar_one_or_none()
    if finding_id is None:
        return
    _resolve_finding(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=stage_code,
        finding_id=finding_id,
        actor_id=None,
        correlation_id=correlation_id,
        note="Automatic retry succeeded.",
    )


_DOCUMENT_MISSING_TYPE = "DOCUMENT_MISSING"


def _document_missing_rule_key(stage_code: StageCode, document_id: UUID) -> str:
    return f"{_DOCUMENT_MISSING_TYPE}:{stage_code}:{document_id}"


def sync_document_confirmation_status(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: StageCode,
    document_id: UUID,
    confirmation_status: str | None,
    document_label: str,
    correlation_id: str,
) -> None:
    """Raise DOCUMENT_MISSING the moment DI reports a non-retryable extraction
    failure for a document; resolve it the moment that same requirement's
    active document confirms cleanly (a reupload, or DI succeeding on retry).

    This is deliberately independent of whether facts get pulled/materialized
    -- it only tracks the document's own confirmation outcome, so it applies
    uniformly to both stages regardless of how far each stage's materialization
    pipeline reaches. Idempotent; never raises (best-effort, same as every
    other producer here).
    """
    try:
        rule_key = _document_missing_rule_key(stage_code, document_id)
        status = str(confirmation_status or "").upper()
        if status == "NOT_CONFIRMED":
            _machine_flag(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                stage_code=stage_code,
                rule_key=rule_key,
                finding_type=_DOCUMENT_MISSING_TYPE,
                severity="MEDIUM",
                title=f"Document Not Found: {document_label}",
                description=(
                    f"Document Intelligence could not process {document_label} -- "
                    "the file may be unreadable, corrupt, or the wrong document. "
                    "Re-upload it to replace this one."
                ),
                correlation_id=correlation_id,
                safe_payload={"diDocumentId": str(document_id)},
                blocking_completion=False,
            )
        elif status == "CONFIRMED":
            finding_id = connection.execute(
                text(
                    """
                    SELECT audit_finding_id
                    FROM auditcore.audit_findings
                    WHERE tenant_id=:t AND journey_id=:j AND rule_key=:r
                      AND finding_status IN ('OPEN','ACKNOWLEDGED')
                    """
                ),
                {"t": tenant_id, "j": journey_id, "r": rule_key},
            ).scalar_one_or_none()
            if finding_id is not None:
                _resolve_finding(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    stage_code=stage_code,
                    finding_id=finding_id,
                    actor_id=None,
                    correlation_id=correlation_id,
                    note="Document later processed successfully.",
                )
    except Exception:
        logger.warning("sync_document_confirmation_status failed", exc_info=True)


def sync_model_resolution_with_escalation(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    correlation_id: str,
) -> dict[str, Any]:
    """SKU resolution, escalating a repeated internal failure to the PC."""

    result = sync_model_resolution(
        connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
    )
    if result.get("error"):
        _flag_failure(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code="BOOKING",
            task_type="SKU_RESOLUTION",
            correlation_id=correlation_id,
        )
    else:
        _resolve_failure(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code="BOOKING",
            task_type="SKU_RESOLUTION",
            correlation_id=correlation_id,
        )
    return result


def reconcile_payments_with_escalation(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: StageCode,
    correlation_id: str,
) -> dict[str, Any]:
    """Payment reconciliation, escalating a repeated internal failure to the PC."""

    result = reconcile_payments(
        connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
    )
    if result.get("error"):
        _flag_failure(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage_code,
            task_type="PAYMENT_RECONCILIATION",
            correlation_id=correlation_id,
        )
    else:
        _resolve_failure(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage_code,
            task_type="PAYMENT_RECONCILIATION",
            correlation_id=correlation_id,
        )
    return result


__all__ = [
    "reconcile_payments_with_escalation",
    "sync_document_confirmation_status",
    "sync_model_resolution_with_escalation",
]
