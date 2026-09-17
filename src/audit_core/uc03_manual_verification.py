"""uc03_manual_verification.py — the "manual verification required" worklist.

When a booking / delivery document is extracted and one or more of its
machine-read values are below the 90% confidence bar, this raises **one
MANUAL_VERIFICATION_REVIEW Task per (stage, document)** carrying the field
list, assigned to PC. A low-confidence field isn't a rule violation or a
compliance gap on its own -- it's DI saying "I'm not sure I read this
right, please double-check" -- so this is a Task Queue item, not a
rule-classified audit finding (direct user correction, same shape as
uc03_model_selection_corrections.py / uc03_document_field_corrections.py's
own fixes: Audit Review stays reserved for what a rule actually found
wrong with the business process).

There is deliberately no dedicated resolve action for this Task: a PC
already fixes exactly this from the standalone Documents page (the same
per-field correction flow `uc03_document_field_corrections.py` exposes,
already routed to from the Task Queue and Journey 360 for this exact
category) -- submitting any correction there writes ``reviewed_at_utc``
unconditionally (see ``persist_reviewed_di_fields``), which is all this
module's own re-sync needs to see to close the Task on its own, the same
"go fix it, it clears itself" shape ``AUTO_SELF_SERVE`` tasks already use.
No separate "mark done" step exists or is needed.

``sync_manual_verification_findings`` is the producer (name kept for its
two real callers -- uc03_confidence_review_policy.py's per-document sync,
uc03_run_all_rules.py's manual trigger -- which only care about its return
counts): it raises a Task for every document that still has unreviewed
low-confidence fields and completes the Task for every document that no
longer does.

This module also still hosts ``_resolve_finding`` -- a generic "resolve
this audit_findings row" helper with no MANUAL_VERIFICATION-specific logic
at all, reused by roughly a dozen other rule producers across the codebase
(WRONG_DOCUMENT, DUPLICATE_BOOKING, DUPLICATE_RECEIPT, the booking
checkpoint rules, and others). Moving it to a shared-utilities module would
be a real, valuable cleanup, but is a separate, larger refactor than this
one (it would touch every one of those importers) -- kept in place here,
unchanged, specifically so this fix doesn't silently break unrelated rules
that have nothing to do with manual verification.
"""
from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_review_confidence import REVIEW_THRESHOLD_PERCENT
from audit_core.workflow import complete_workflow_task, create_workflow_task

TASK_TYPE = "MANUAL_VERIFICATION_REVIEW"
_WORKFLOW_TYPE = "UC03_MANUAL_VERIFICATION"
_RULE_PREFIX = "MANUAL_VERIFICATION"
_OPEN_TASK_STATUSES = {"PENDING", "READY", "CLAIMED", "IN_PROGRESS", "RETRY_WAIT"}

StageCode = Literal["BOOKING", "DELIVERY"]


def _rule_key(stage_code: str, document_id: UUID) -> str:
    # Kept in this exact "MANUAL_VERIFICATION:<stage>:<doc>" shape -- not
    # just an internal label. uc03_review_queue.py's summary counts a Task
    # toward manualVerification the same way it already counts a Finding
    # (item.ruleKey.startswith("MANUAL_VERIFICATION:")), and
    # ReviewQueuePage.tsx's isManualVerificationRule() does the identical
    # check client-side -- both keep working unchanged as long as this
    # Task's own payload carries the same ruleKey shape a
    # MANUAL_VERIFICATION finding always did.
    return f"{_RULE_PREFIX}:{stage_code}:{document_id}"


def _effect_key(tenant_id: str, journey_id: UUID, stage_code: str, document_id: UUID) -> str:
    return f"task:manual-verification:{tenant_id}:{journey_id}:{stage_code}:{document_id}"


# ── low-confidence field query ─────────────────────────────────────────────────
# Bug fix: this used to compare confidence_score < 0.90 unconditionally, but
# verigence-di computes and stores confidence on a 0.00-100.00 scale
# (confidence_scale='PERCENT' on every row _machine_upsert_fact writes) --
# so a real, non-degenerate low-confidence extraction (45, 60, 85) was never
# < 0.90 and this worklist essentially never populated in production. Fixed
# to reuse the SAME normalize-then-compare-to-REVIEW_THRESHOLD_PERCENT logic
# uc03_confidence_review_policy.py::_unreviewed_low_confidence_count already
# gets right for the pre-submit blocking gate, rather than a third,
# independent (and wrong) copy of the same 90% threshold.
_LOW_CONFIDENCE_SQL = f"""
    SELECT f.extracted_field_id,
           f.di_document_id,
           f.field_key,
           f.source_canonical_field_id,
           f.source_document_type_key,
           f.source_fact_ref,
           f.source_fact_version,
           f.extracted_value,
           f.effective_value,
           f.confidence_score,
           COALESCE(e.document_type_key, f.source_document_type_key) AS document_label
    FROM auditcore.journey_document_extracted_fields f
    LEFT JOIN auditcore.evidence e
      ON e.tenant_id = f.tenant_id AND e.di_document_id = f.di_document_id
    WHERE f.tenant_id = :tenant_id
      AND f.journey_id = :journey_id
      AND f.stage_code = :stage_code
      AND f.reviewed_at_utc IS NULL
      AND f.extracted_value IS NOT NULL
      AND f.extracted_value <> 'null'::jsonb
      AND (
          f.confidence_score IS NULL
          OR CASE
              WHEN f.confidence_scale='UNIT_INTERVAL' THEN f.confidence_score * 100
              ELSE f.confidence_score
          END < {REVIEW_THRESHOLD_PERCENT}
      )
    ORDER BY f.di_document_id, f.field_key
"""


def _unreviewed_low_confidence(
    connection: Connection, *, tenant_id: str, journey_id: UUID, stage_code: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(_LOW_CONFIDENCE_SQL),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
        },
    ).mappings().all()
    return [dict(row) for row in rows]


def _by_document(rows: list[dict[str, Any]]) -> dict[UUID, list[dict[str, Any]]]:
    grouped: dict[UUID, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["di_document_id"], []).append(row)
    return grouped


def _extracted_field_count(
    connection: Connection, *, tenant_id: str, journey_id: UUID, stage_code: str
) -> int:
    """How many extracted fields exist at all for this stage, regardless of
    confidence or review status -- distinguishes "nothing extracted for this
    stage yet" (SKIPPED) from "checked, nothing currently below threshold"
    (PASS) for the Execution Log, since _LOW_CONFIDENCE_SQL only ever
    returns the rows that already have a problem."""
    return connection.execute(
        text(
            """
            SELECT count(*) FROM auditcore.journey_document_extracted_fields
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND stage_code = :stage_code
              AND extracted_value IS NOT NULL AND extracted_value <> 'null'::jsonb
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "stage_code": stage_code},
    ).scalar_one()


def _friendly_label(raw: Any, document_id: UUID) -> str:
    text_value = str(raw).strip() if raw else ""
    if not text_value:
        return f"document {str(document_id)[:8]}"
    return text_value.replace("_", " ").title()


# ── producer ──────────────────────────────────────────────────────────────────
def sync_manual_verification_findings(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: StageCode,
    correlation_id: str,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Raise a MANUAL_VERIFICATION_REVIEW task for each document that still
    has unreviewed low-confidence fields; complete it for each that no
    longer does.

    Best-effort and idempotent — safe to call after every extraction, review or
    correction. Never raises.
    """
    try:
        pending = _by_document(
            _unreviewed_low_confidence(
                connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage_code
            )
        )
    except Exception:  # noqa: BLE001 - producer must never break the caller
        return {"raised": 0, "resolved": 0, "error": True}

    # raised counts every currently-outstanding pending document, not just
    # ones that got a brand new Task this call -- uc03_run_all_rules.py
    # derives FAIL from this being non-zero, on every evaluation run, for
    # as long as the underlying gap stays open (matching this producer's
    # own pre-Task-Queue behavior, which always incremented raised per
    # pending item regardless of whether the finding it idempotently
    # touched already existed).
    raised = 0
    live_effect_keys: set[str] = set()
    for document_id, fields in pending.items():
        raised += 1
        label = _friendly_label(fields[0].get("document_label"), document_id)
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
        create_workflow_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            workflow_type=_WORKFLOW_TYPE,
            process_area=stage_code,
            task_type=TASK_TYPE,
            assigned_role_code="PC",
            task_payload={
                "ruleKey": _rule_key(stage_code, document_id),
                "diDocumentId": str(document_id),
                "documentLabel": label,
                "fieldKeys": [row["field_key"] for row in fields],
                "comment": (
                    f"{len(fields)} machine-read value"
                    f"{'s' if len(fields) != 1 else ''} on {label} are below the 90% "
                    "confidence threshold — confirm or correct each against the document."
                ),
            },
            effect_key=effect_key,
            correlation_id=correlation_id,
        )

    # Complete a Task whose document is now clear -- the only way a field
    # ever leaves _unreviewed_low_confidence is a PC actually reviewing it
    # (via the Documents page's own field-correction flow, which stamps
    # reviewed_at_utc unconditionally), so this is always a genuine
    # completion, never an ambiguous "went away on its own" case.
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
        complete_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task["workflow_task_id"],
            actor_id=actor_id or "SYSTEM",
        )
        resolved += 1

    # Compatibility for a MANUAL_VERIFICATION finding raised before this
    # producer moved onto the Task Queue: nothing else in the codebase
    # resolves it any more, so without this it would sit open forever even
    # after a PC actually reviews the fields it was about -- resolve it the
    # same moment the equivalent Task would close. Safe to delete once no
    # tenant has one of these findings open any longer.
    live_rule_keys = {_rule_key(stage_code, doc_id) for doc_id in pending}
    legacy_findings = connection.execute(
        text(
            """
            SELECT audit_finding_id, rule_key FROM auditcore.audit_findings
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND stage_code = :stage_code AND finding_type_code = 'MANUAL_VERIFICATION'
              AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "stage_code": stage_code},
    ).mappings().all()
    for finding in legacy_findings:
        if finding["rule_key"] in live_rule_keys:
            continue
        _resolve_finding(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage_code,
            finding_id=finding["audit_finding_id"],
            actor_id=actor_id,
            correlation_id=correlation_id,
            note="All flagged values verified.",
        )
        resolved += 1

    examined = _extracted_field_count(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage_code
    )
    return {"raised": raised, "resolved": resolved, "examined": examined}


def _resolve_finding(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    finding_id: UUID,
    actor_id: str | None,
    correlation_id: str,
    note: str,
) -> None:
    """Generic audit_findings resolver -- see module docstring: no
    MANUAL_VERIFICATION-specific logic, shared by many other rule
    producers. Not used by this module's own producer any more (see
    sync_manual_verification_findings above), kept here unchanged."""
    updated = connection.execute(
        text(
            """
            UPDATE auditcore.audit_findings
            SET finding_status = 'RESOLVED',
                disposition = 'FIXED',
                resolved_at_utc = now(),
                resolved_by_actor_id = :actor_id,
                updated_at_utc = now()
            WHERE tenant_id = :tenant_id AND audit_finding_id = :finding_id
              AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
            """
        ),
        {"tenant_id": tenant_id, "finding_id": finding_id, "actor_id": actor_id},
    )
    if updated.rowcount != 1:
        return
    connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_finding_events (
                tenant_id, audit_finding_id, journey_id, stage_code,
                event_type, actor_id, actor_role_snapshot, safe_payload, correlation_id
            ) VALUES (
                :tenant_id, :finding_id, :journey_id, :stage_code,
                'RESOLVED', :actor_id, :role, CAST(:payload AS jsonb), :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "finding_id": finding_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
            "actor_id": actor_id,
            "role": "PC" if actor_id else "SYSTEM",
            "payload": json.dumps({"disposition": "FIXED", "note": note}),
            "correlation_id": correlation_id,
        },
    )


__all__ = ["TASK_TYPE", "_resolve_finding", "sync_manual_verification_findings"]
