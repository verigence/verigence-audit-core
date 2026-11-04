"""A Reviewer closes a journey with a closure summary (product owner, 2026-11-04).

"Reviewer should be given a privilege to close a journey with comments. It
should be covered which all documents which were mandatory has been skipped
for this journey. Any violations which are reported has been not considered
reason for it. It should be very deterministic report why any of the
exceptions were not continued and specific ans for each. it will be kind of
closure summary report and it will act as add to our existing audit report."

What is open on the journey is computed here, the same way every time, from
what the system already holds -- nothing is chosen by the Reviewer:

- MISSING_DOCUMENT: every required document (mandatory, or conditional and
  made mandatory by the deal) not on file, Booking and -- once started --
  Delivery (``uc03_p2_audit_summary.required_documents``, the checklist the
  stage gates use). A PC's "not mandatory" claim the Reviewer accepted
  pre-fills its answer; one still waiting for the Reviewer blocks the close.
- OPEN_FINDING: every audit finding still OPEN or ACKNOWLEDGED (violations,
  data gaps, document gaps).
- OPEN_TASK: every task still open on the journey that the two lists above do
  not already cover (a finding's own task, a missing document's own task), and
  not the closure's own follow-up (the Team Lead's delivery review) or an
  information task.

Closing needs a specific answer (10 characters or more) for EVERY item and an
overall comment. It then completes the Delivery stage exactly as the
Reviewer's "complete delivery" does, so the Team Lead's review follows as for
any completed delivery, and writes the summary -- each item with its answer --
to ``p2_journey_closures``. The latest closure is part of the journey's
compliance report.
"""
from __future__ import annotations

import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import authorize_p2

MIN_ANSWER = 10
CLOSING_ROLES = frozenset({"REVIEWER"})
_OPEN_TASK_STATUSES = ("READY", "IN_PROGRESS", "RETURNED", "ACTION_COMPLETED", "VERIFYING",
                       "AWAITING_REQUESTER_REVIEW")
# Tasks that are not open work on the deal: the closure's own follow-up, the
# Reviewer's combined claims task (its claims are read directly) and
# information-only notices.
_NOT_ITEMS = frozenset({"DELIVERY_REVIEW", "NOT_MANDATORY_REVIEW", "REVIEWER_CORRECTIONS_INFO"})

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-reviewer-closure"])


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


# --------------------------------------------------------------------------- what is open

def _open_findings(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT audit_finding_id, title, description, finding_class, severity, finding_status, stage_code,
                   rule_key, origin_kind, created_at_utc
            FROM auditcore.audit_findings
            WHERE tenant_id=:t AND journey_id=:j AND finding_status IN ('OPEN','ACKNOWLEDGED')
            ORDER BY created_at_utc, audit_finding_id
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def _open_tasks(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT task_id, task_type, title, description, task_status, assigned_role_code, severity, reference,
                   completion_result, created_at_utc
            FROM auditcore.p2_tasks
            WHERE tenant_id=:t AND journey_id=:j AND task_status = ANY(CAST(:open AS varchar[]))
            ORDER BY created_at_utc, task_id
            """
        ),
        {"t": tenant_id, "j": journey_id, "open": list(_OPEN_TASK_STATUSES)},
    ).mappings().all()
    return [dict(r) for r in rows]


def closure_items(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """Everything open on the journey that a closure has to answer for, in a fixed order:
    missing documents (missing first by stage), then findings, then tasks (oldest first)."""
    from audit_core.uc03_not_mandatory_claims import claims
    from audit_core.uc03_p2_audit_summary import required_documents

    all_claims = claims(connection, tenant_id=tenant_id, journey_id=journey_id)
    accepted = {c["requirementKey"]: c for c in all_claims if c["state"] == "ACCEPTED" and c["requirementKey"]}
    pending = [c for c in all_claims if c["state"] == "PENDING"]

    items: list[dict[str, Any]] = []
    missing_keys: set[str] = set()
    for doc in required_documents(connection, tenant_id=tenant_id, journey_id=journey_id):
        if doc["onFile"]:
            continue
        missing_keys.add(str(doc["key"]))
        claim = accepted.get(str(doc["key"]))
        prefill = None
        if claim:
            prefill = (f"Not mandatory for this journey. PC: {claim['pcReason']}. Accepted by the "
                       f"{(claim['decidedRole'] or 'Reviewer').title()}"
                       + (f": {claim['decisionReason']}" if claim.get("decisionReason") else "") + ".")
        items.append({
            "key": f"document:{doc['key']}", "kind": "MISSING_DOCUMENT", "stage": doc["stage"],
            "label": doc["label"], "detail": doc.get("reason"), "requirement": doc["requirement"],
            "severity": None, "prefilledAnswer": prefill,
            "claim": ({"taskId": claim["taskId"], "pcReason": claim["pcReason"],
                       "decidedBy": claim["decidedBy"], "decidedAt": claim["decidedAt"]} if claim else None),
        })

    findings = _open_findings(connection, tenant_id=tenant_id, journey_id=journey_id)
    finding_ids = {str(f["audit_finding_id"]) for f in findings}
    for f in findings:
        items.append({
            "key": f"finding:{f['audit_finding_id']}", "kind": "OPEN_FINDING", "stage": f["stage_code"],
            "label": f["title"], "detail": f["description"], "findingClass": f["finding_class"],
            "severity": f["severity"], "status": f["finding_status"], "ruleKey": f["rule_key"],
            "prefilledAnswer": None, "claim": None,
        })

    for t in _open_tasks(connection, tenant_id=tenant_id, journey_id=journey_id):
        ref = dict(t["reference"] or {})
        if t["task_type"] in _NOT_ITEMS:
            continue
        if str(ref.get("findingId") or "") in finding_ids:
            continue  # answered with its finding
        if t["task_type"] == "DOCUMENT_MISSING" and str(ref.get("requirementKey") or "") in missing_keys:
            continue  # answered with its document
        if (t["completion_result"] or {}).get("outcome") == "NOT_MANDATORY_CLAIMED":
            continue  # a pending claim: listed as a blocker below
        items.append({
            "key": f"task:{t['task_id']}", "kind": "OPEN_TASK", "stage": ref.get("stage"),
            "label": t["title"], "detail": t["description"], "severity": t["severity"],
            "status": t["task_status"], "assignedRole": t["assigned_role_code"], "taskType": t["task_type"],
            "prefilledAnswer": None, "claim": None,
        })

    blockers = [f"The PC's claim that “{c['document']}” is not mandatory is waiting for a decision."
                for c in pending]
    return {"items": items, "blockers": blockers}


def latest_closure(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any] | None:
    row = connection.execute(
        text("SELECT closure_id, closed_by_actor_id, closed_by_role, comment, item_count, summary, delivery_outcome, "
             "closed_at_utc FROM auditcore.p2_journey_closures WHERE tenant_id=:t AND journey_id=:j "
             "ORDER BY closed_at_utc DESC LIMIT 1"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one_or_none()
    if row is None:
        return None
    return {
        "closureId": str(row["closure_id"]), "closedBy": row["closed_by_actor_id"], "closedRole": row["closed_by_role"],
        "comment": row["comment"], "itemCount": int(row["item_count"]), "items": (row["summary"] or {}).get("items", []),
        "counts": (row["summary"] or {}).get("counts", {}), "deliveryOutcome": row["delivery_outcome"],
        "closedAtUtc": row["closed_at_utc"],
    }


# --------------------------------------------------------------------------- closing

def close_journey(
    connection: Connection, *, tenant_id: str, journey_id: UUID, actor_id: str, actor_role: str,
    comment: str, answers: dict[str, str], correlation_id: str | None = None,
) -> dict[str, Any]:
    from audit_core.uc03_p2_runtime import note_facts_changed, record_activity
    from audit_core.uc03_p2_workflow import complete_delivery_by_reviewer

    if actor_role.upper() not in CLOSING_ROLES:
        raise HTTPException(status_code=403, detail="Only a Reviewer closes a journey this way.")
    comment = (comment or "").strip()
    if len(comment) < MIN_ANSWER:
        raise HTTPException(status_code=422, detail="Write the closing comment (at least 10 characters).")
    current = closure_items(connection, tenant_id=tenant_id, journey_id=journey_id)
    if current["blockers"]:
        raise HTTPException(status_code=409, detail=" ".join(current["blockers"]))
    unanswered, answered = [], []
    for item in current["items"]:
        typed = (answers.get(item["key"]) or "").strip()
        answer = typed or (item.get("prefilledAnswer") or "")
        if len(answer) < MIN_ANSWER:
            unanswered.append(item["label"])
            continue
        answered.append({**item, "answer": answer, "answerSource": "REVIEWER" if typed else "ACCEPTED_CLAIM"})
    if unanswered:
        raise HTTPException(
            status_code=422,
            detail={"message": "Give a specific reason (at least 10 characters) for every open item.",
                    "unanswered": unanswered},
        )

    delivery = complete_delivery_by_reviewer(
        connection, tenant_id=tenant_id, journey_id=journey_id, actor_id=actor_id, actor_role="REVIEWER",
        reason=f"Closed by the Reviewer: {comment}", correlation_id=correlation_id,
    )
    refused = {
        "BOOKING_NOT_COMPLETE": "The booking is not complete yet: a journey is closed only after it.",
        "DELIVERY_NOT_STARTED": "The delivery has not started: no delivery document is on file yet.",
    }
    if delivery["outcome"] in refused:
        raise HTTPException(status_code=409, detail=refused[delivery["outcome"]])

    counts: dict[str, int] = {}
    for item in answered:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    summary = {"items": answered, "counts": counts, "pendingAtCompletion": delivery.get("pending", [])}
    closure_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_journey_closures (
                tenant_id, journey_id, closed_by_actor_id, closed_by_role, comment, item_count, summary,
                delivery_outcome, correlation_id
            ) VALUES (:t, :j, :actor, :role, :comment, :n, CAST(:summary AS jsonb), :outcome, :cid)
            RETURNING closure_id
            """
        ),
        {"t": tenant_id, "j": journey_id, "actor": actor_id, "role": actor_role.upper(), "comment": comment,
         "n": len(answered), "summary": _json(summary), "outcome": delivery["outcome"], "cid": correlation_id},
    ).scalar_one()
    record_activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type="JOURNEY_CLOSED_BY_REVIEWER",
        subject_type="JOURNEY", subject_id=str(journey_id),
        details={"closureId": str(closure_id), "by": actor_id, "comment": comment, "counts": counts},
        correlation_id=correlation_id,
    )
    if delivery["outcome"] == "COMPLETED":
        note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id, reason="JOURNEY_CLOSED_BY_REVIEWER")
    return {"closureId": str(closure_id), "deliveryOutcome": delivery["outcome"], "itemCount": len(answered),
            "counts": counts}


# --------------------------------------------------------------------------- endpoints

class ClosureCommand(BaseModel):
    comment: str = Field(min_length=1, max_length=4000)
    # Item key (as the preview lists it) -> the specific answer for it.
    answers: dict[str, str] = Field(default_factory=dict)


@router.get("/journeys/{journey_id}/reviewer-closure")
def preview_closure(
    tenant_id: str, journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    access = authorize_p2(connection, tenant_id=tenant_id, journey_id=journey_id, human_principal=human_principal,
                          authorization_client=authorization_client, permission_key="audit.journey.read")
    set_tenant_context(connection, tenant_id)
    current = closure_items(connection, tenant_id=tenant_id, journey_id=journey_id)
    return {
        "journeyId": str(journey_id),
        "canClose": (access.operating_role or "").upper() in CLOSING_ROLES and not current["blockers"],
        "minAnswerLength": MIN_ANSWER,
        **current,
        "lastClosure": latest_closure(connection, tenant_id=tenant_id, journey_id=journey_id),
    }


@router.post("/journeys/{journey_id}/reviewer-closure")
def post_closure(
    tenant_id: str, journey_id: UUID, command: ClosureCommand,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    access = authorize_p2(connection, tenant_id=tenant_id, journey_id=journey_id, human_principal=human_principal,
                          authorization_client=authorization_client, permission_key="audit.journey.update")
    set_tenant_context(connection, tenant_id)
    return close_journey(connection, tenant_id=tenant_id, journey_id=journey_id, actor_id=access.actor_id,
                         actor_role=access.operating_role or "", comment=command.comment, answers=command.answers)
