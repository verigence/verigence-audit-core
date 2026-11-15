"""The answer for one document of a journey's missing-documents task (product owner 2026-11-15).

A journey has ONE missing-documents task (uc03_p2_task_producer.sync_document_missing_tasks).
Each document on it can be answered for on its own line:

- "Document uploaded"            the PC says it is on file though it was not read: counts as received;
- "Dealer not provided"          EW / RSA invoice the dealership does not issue (PC): received;
- "Temporary registration"       RTO challan, by a TL / PM / Reviewer with the number: received;
- "Document not required ..."    the PC's claim with a reason (never for the NDC / cost sheet):
                                 waits for the Reviewer (uc03_not_mandatory_claims), who accepts
                                 (received) or rejects (asked again, with the Reviewer's reason).

The answer is one row of p2_document_waivers per journey and document; the task is then
synced again, so it lists only what is still missing and closes itself when nothing is.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

UPLOADED = "Document uploaded"
CHOICES = frozenset({UPLOADED, "Dealer not provided", "Temporary registration",
                     "Document not required to be uploaded"})


# Documents that became one either-of line on 2026-11-15 (document_templates.yaml groups).
GROUPED_KEYS = {"customer_invoice_dms": "VEHICLE_INVOICE", "tax_invoice_tally": "VEHICLE_INVOICE",
                "gate_pass": "GATE_PASS", "delivery_receipt": "GATE_PASS"}


def line_key(requirement_key: str | None) -> str:
    key = str(requirement_key or "")
    return GROUPED_KEYS.get(key, key)


def record_legacy_answer(connection: Connection, *, tenant_id: str, task: dict[str, Any], choice: str, status: str,
                         reason: str, actor_id: str, actor_role_code: str) -> None:
    """An answer on a per-document task of before 2026-11-15 is a waiver row too, so the
    Reviewer sees a claim, remarks see the answer, and the one task does not ask again."""
    if task.get("source_code") != "DOCUMENT_MISSING":
        return  # the booking form task keeps its claim on the task (uc03_not_mandatory_claims)
    reference = task.get("reference") or {}
    if isinstance(reference, str):
        reference = json.loads(reference or "{}")
    key = line_key(reference.get("requirementKey"))
    if not key:
        return
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_document_waivers (tenant_id, journey_id, requirement_key, label, stage, choice,
                                                        status, reason, claimed_by, claimed_role, source_task_id)
            VALUES (:t, :j, :k, :label, :stage, :choice, :status, :reason, :actor, :role, :task)
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO UPDATE
              SET choice=EXCLUDED.choice, status=EXCLUDED.status, reason=EXCLUDED.reason, claimed_by=EXCLUDED.claimed_by,
                  claimed_role=EXCLUDED.claimed_role, claimed_at_utc=now(), decided_by=NULL, decided_role=NULL,
                  decided_at_utc=NULL, decision_reason=NULL, round=p2_document_waivers.round + 1,
                  source_task_id=EXCLUDED.source_task_id, updated_at_utc=now()
            """
        ),
        {"t": tenant_id, "j": UUID(str(task["journey_id"])), "k": key, "label": task.get("title"),
         "stage": reference.get("stage"), "choice": choice, "status": status, "reason": reason or None,
         "actor": actor_id, "role": actor_role_code, "task": task["task_id"]},
    )


def is_consolidated(task: dict[str, Any]) -> bool:
    reference = task.get("reference") or {}
    return isinstance(reference, dict) and isinstance(reference.get("items"), list)


def answer(connection: Connection, *, tenant_id: str, task: dict[str, Any], details: dict[str, Any] | None,
           comment: str | None, actor_id: str, actor_role_code: str) -> dict[str, Any]:
    """Record the answer for one document of the task, then sync the task."""
    from audit_core import uc03_registration_closures as registration_closures
    from audit_core.uc03_not_mandatory_claims import (
        MIN_REASON,
        claim_reason,
        is_claim,
        refresh_review_task,
    )
    from audit_core.uc03_p2_task_producer import (
        NEVER_WAIVED_AS_NOT_REQUIRED,
        sync_document_missing_tasks,
    )

    details = details or {}
    choice = str(details.get("choice") or "")
    key = str(details.get("requirementKey") or "")
    items = {str(i.get("key")): i for i in (task.get("reference") or {}).get("items") or []}
    if key not in items:
        raise ValueError("Choose the document this answer is for.")
    if choice not in CHOICES:
        raise ValueError("Choose what to say about this document.")
    line = {"reference": {"requirementKey": key}}
    registration_closures.check_close_choice(line, choice=choice, actor_role_code=actor_role_code, comment=comment)
    claim = is_claim(choice)
    reason = claim_reason(comment) if claim else (comment or "").strip()
    if claim:
        if key in NEVER_WAIVED_AS_NOT_REQUIRED:
            raise ValueError("The NDC / cost sheet is mandatory for every delivery and cannot be marked as not "
                             "required. Upload it; the Deal waits for it.")
        if len(reason) < MIN_REASON:
            raise ValueError("Say why this document is not mandatory for this journey (at least 10 characters).")
    journey_id = UUID(str(task["journey_id"]))
    item = items[key]
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_document_waivers (tenant_id, journey_id, requirement_key, label, stage, choice,
                                                        status, reason, claimed_by, claimed_role, source_task_id)
            VALUES (:t, :j, :k, :label, :stage, :choice, :status, :reason, :actor, :role, :task)
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO UPDATE
              SET choice=EXCLUDED.choice, status=EXCLUDED.status, reason=EXCLUDED.reason,
                  claimed_by=EXCLUDED.claimed_by, claimed_role=EXCLUDED.claimed_role, claimed_at_utc=now(),
                  decided_by=NULL, decided_role=NULL, decided_at_utc=NULL, decision_reason=NULL,
                  round=p2_document_waivers.round + 1, source_task_id=EXCLUDED.source_task_id, updated_at_utc=now()
            """
        ),
        {"t": tenant_id, "j": journey_id, "k": key, "label": item.get("label"), "stage": item.get("stage"),
         "choice": choice, "status": "CLAIMED" if claim else "ACCEPTED", "reason": reason or None,
         "actor": actor_id, "role": actor_role_code, "task": task["task_id"]},
    )
    if claim:
        refresh_review_task(connection, tenant_id=tenant_id, journey_id=journey_id)
    sync_document_missing_tasks(connection, tenant_id=tenant_id, journey_id=journey_id,
                                evaluation_started_at=datetime.now(UTC))
    status = connection.execute(
        text("SELECT task_status FROM auditcore.p2_tasks WHERE tenant_id=:t AND task_id=:id"),
        {"t": tenant_id, "id": task["task_id"]},
    ).scalar_one()
    return {"taskId": str(task["task_id"]), "status": status,
            "outcome": "NOT_MANDATORY_CLAIMED" if claim else "DOCUMENT_ANSWERED", "requirementKey": key}
