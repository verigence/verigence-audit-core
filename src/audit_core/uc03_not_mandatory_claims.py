"""A PC's "this document is not mandatory" claim, accepted or rejected by the Reviewer.

Product owner, 2026-11-04: "For PC for mandatory documents, they should be given
an option to close the task by selecting the option that document is not
mandatory and a comment should tell why it is not mandatory. All such tasks for
mandatory documents needs to be combined with PC feedback and submitted to
Reviewer for acceptance or reject. If a task is rejected, then PC will see the
same task again."

Until now "Document not required to be uploaded" closed a missing-document task
(DOCUMENT_MISSING / BOOKING_FORM_MISSING) at once, with nobody checking it. Now:

1. The PC's claim (the choice plus their own reason) parks the task in
   AWAITING_REQUESTER_REVIEW for the REVIEWER role -- the same waiting status
   and reviewer columns the Task Manager uses. The task is not closed.
2. Every pending claim on a journey is listed on ONE Reviewer task
   (NOT_MANDATORY_REVIEW), with each claim's document, stage and the PC's reason.
3. The Reviewer decides each claim:
   - accept: the PC's task closes (VERIFIED_COMPLETE, outcome
     NOT_MANDATORY_ACCEPTED, the issue hash kept so the next sync does not
     reopen it); the claim is the answer for that document from then on;
   - reject (a reason is required): the PC's task is RETURNED to the PC, next
     round, carrying the Reviewer's reason. The PC sees the same task again.
   The combined task closes itself when no claim is left pending.

The "Document uploaded" choice still closes at once, as before.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import authorize_p2

NOT_MANDATORY_CHOICE = "Document not required to be uploaded"
CLAIM_REVIEW_TASK = "NOT_MANDATORY_REVIEW"
CLAIMED = "NOT_MANDATORY_CLAIMED"
ACCEPTED = "NOT_MANDATORY_ACCEPTED"
REJECTED = "NOT_MANDATORY_REJECTED"
# Who decides a claim. The Reviewer is the role it goes to; a Team Lead or PM
# holds the same authority over a PC's work.
DECIDING_ROLES = frozenset({"REVIEWER", "TL", "PM"})
MIN_REASON = 10

_WORK_UPDATE = "audit.work.update"
_READ = "audit.journey.read"

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-not-mandatory"])


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def review_key(journey_id: UUID | str) -> str:
    return f"not-mandatory-review:{journey_id}"


def is_claim(choice: str | None) -> bool:
    return (choice or "").strip().lower() == NOT_MANDATORY_CHOICE.lower()


def claim_reason(comment: str | None) -> str:
    """The PC's own words: the comment without the choice the button put first."""
    text_ = (comment or "").strip()
    if text_.lower().startswith(NOT_MANDATORY_CHOICE.lower()):
        text_ = text_[len(NOT_MANDATORY_CHOICE):]
    return text_.lstrip(" .:-–—\n").strip()


# --------------------------------------------------------------------------- the PC's claim

def submit_claim(
    connection: Connection, *, tenant_id: str, task: dict[str, Any], actor_id: str, actor_role_code: str,
    comment: str | None,
) -> dict[str, Any]:
    """Park a missing-document task as the PC's "not mandatory" claim, for the Reviewer.

    Called by ``submit_action`` after it wrote the CLOSE_TASK event."""
    reason = claim_reason(comment)
    if len(reason) < MIN_REASON:
        raise ValueError("Say why this document is not mandatory for this journey (at least 10 characters).")
    result = dict(task.get("completion_result") or {})
    result.update({
        "outcome": CLAIMED, "choice": NOT_MANDATORY_CHOICE, "claimReason": reason,
        "claimedBy": actor_id, "claimedRole": actor_role_code, "claimedAt": datetime.now(UTC).isoformat(),
        "claimRound": int(task.get("round_number") or 1),
    })
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks
            SET task_status='AWAITING_REQUESTER_REVIEW', reviewer_role_code='REVIEWER', reviewer_actor_id=NULL,
                action_completed_at_utc=now(), completed_by_actor_id=:actor,
                completion_result=CAST(:result AS jsonb), updated_at_utc=now()
            WHERE tenant_id=:t AND task_id=:id
            """
        ),
        {"t": tenant_id, "id": task["task_id"], "actor": actor_id, "result": _json(result)},
    )
    from audit_core.uc03_document_waivers import record_legacy_answer

    record_legacy_answer(connection, tenant_id=tenant_id, task=task, choice=NOT_MANDATORY_CHOICE, status="CLAIMED",
                         reason=reason, actor_id=actor_id, actor_role_code=actor_role_code)
    journey_id = UUID(str(task["journey_id"]))
    refresh_review_task(connection, tenant_id=tenant_id, journey_id=journey_id)
    return {"taskId": str(task["task_id"]), "status": "AWAITING_REQUESTER_REVIEW", "outcome": CLAIMED}


# --------------------------------------------------------------------------- reading claims

def claims(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """Every "not mandatory" claim on the journey, pending or decided, oldest first.

    Since 2026-11-15 a claim is a row of p2_document_waivers (one missing-documents
    task per journey, uc03_document_waivers); its id is the waiver id, kept under
    "taskId" so the Reviewer's panel and the decision endpoint did not change."""
    rows = connection.execute(
        text(
            """
            SELECT waiver_id, requirement_key, label, stage, status, round, reason, claimed_by, claimed_at_utc,
                   decided_by, decided_role, decided_at_utc, decision_reason
            FROM auditcore.p2_document_waivers
            WHERE tenant_id=:t AND journey_id=:j AND choice=:choice
            ORDER BY claimed_at_utc, waiver_id
            """
        ),
        {"t": tenant_id, "j": journey_id, "choice": NOT_MANDATORY_CHOICE},
    ).mappings().all()
    state = {"CLAIMED": "PENDING", "ACCEPTED": "ACCEPTED", "REJECTED": "REJECTED"}
    return [*_task_claims(connection, tenant_id=tenant_id, journey_id=journey_id), *[{
        "taskId": str(r["waiver_id"]),
        "requirementKey": r["requirement_key"],
        "document": r["label"] or r["requirement_key"],
        "stage": r["stage"],
        "state": state[r["status"]],
        "round": int(r["round"]),
        "pcReason": r["reason"],
        "claimedBy": r["claimed_by"],
        "claimedAt": r["claimed_at_utc"].isoformat() if r["claimed_at_utc"] else None,
        "decidedBy": r["decided_by"],
        "decidedRole": r["decided_role"],
        "decidedAt": r["decided_at_utc"].isoformat() if r["decided_at_utc"] else None,
        "decisionReason": r["decision_reason"],
    } for r in rows]]


def _task_claims(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """A claim kept on its task: the booking form task (BOOKING_FORM_MISSING). The
    per-document tasks of before 2026-11-15 are waiver rows (migration 0229)."""
    rows = connection.execute(
        text(
            """
            SELECT task_id, title, task_status, round_number, reference, completion_result
            FROM auditcore.p2_tasks
            WHERE tenant_id=:t AND journey_id=:j AND task_type='DOCUMENT_MISSING' AND source_code <> 'DOCUMENT_MISSING'
              AND completion_result->>'outcome' IN ('NOT_MANDATORY_CLAIMED','NOT_MANDATORY_ACCEPTED',
                                                    'NOT_MANDATORY_REJECTED')
            ORDER BY created_at_utc, task_id
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    out = []
    for r in rows:
        ref, res = dict(r["reference"] or {}), dict(r["completion_result"] or {})
        state = {"NOT_MANDATORY_CLAIMED": "PENDING", "NOT_MANDATORY_ACCEPTED": "ACCEPTED",
                 "NOT_MANDATORY_REJECTED": "REJECTED"}[res.get("outcome")]
        if state == "PENDING" and r["task_status"] != "AWAITING_REQUESTER_REVIEW":
            continue
        out.append({
            "taskId": str(r["task_id"]), "requirementKey": ref.get("requirementKey"), "document": r["title"],
            "stage": ref.get("stage"), "state": state, "round": int(r["round_number"]),
            "pcReason": res.get("claimReason"), "claimedBy": res.get("claimedBy"), "claimedAt": res.get("claimedAt"),
            "decidedBy": res.get("decidedBy"), "decidedRole": res.get("decidedRole"),
            "decidedAt": res.get("decidedAt"), "decisionReason": res.get("decisionReason"),
        })
    return out


def refresh_review_task(connection: Connection, *, tenant_id: str, journey_id: UUID) -> UUID | None:
    """The journey's one Reviewer task listing every pending claim; closed when none is left."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh, resolve_if_open

    pending = [c for c in claims(connection, tenant_id=tenant_id, journey_id=journey_id) if c["state"] == "PENDING"]
    if not pending:
        resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=review_key(journey_id),
                        evidence={"pendingClaims": 0})
        return None
    listed = "; ".join(f"{c['document']}: {c['pcReason']}" for c in pending)
    count = len(pending)
    task_id, _ = raise_or_refresh(
        connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=review_key(journey_id),
        task_type=CLAIM_REVIEW_TASK, source_type="HUMAN_ACTION", source_code=CLAIM_REVIEW_TASK,
        title=f"PC says {count} document{'s are' if count != 1 else ' is'} not mandatory",
        description=(f"The PC answered {count} missing document{'s' if count != 1 else ''} as not mandatory: "
                     f"{listed}. Accept or reject each one; a rejected one goes back to the PC with your reason."),
        reference={"sourceCode": CLAIM_REVIEW_TASK, "claims": pending,
                   # In the issue hash: a new claim reopens a task already closed.
                   "fields": [f"{c['taskId']}:{c['round']}" for c in pending]},
        severity="HIGH", registry=get_registry(),
    )
    return task_id


# --------------------------------------------------------------------------- the Reviewer's decision

def decide_claim(
    connection: Connection, *, tenant_id: str, journey_id: UUID, task_id: UUID, decision: str,
    reason: str | None, actor_id: str, actor_role_code: str,
) -> dict[str, Any]:
    from audit_core.uc03_p2_tasks import record_task_event

    role = (actor_role_code or "").upper()
    if role not in DECIDING_ROLES:
        raise HTTPException(status_code=403, detail="Only a Reviewer, Team Lead or Project Manager decides this claim.")
    decision = decision.upper()
    reason = (reason or "").strip()
    if decision == "REJECT" and len(reason) < MIN_REASON:
        raise HTTPException(status_code=422, detail="Say why the document is mandatory (at least 10 characters).")
    if decision not in ("ACCEPT", "REJECT"):
        raise HTTPException(status_code=422, detail="decision is ACCEPT or REJECT.")
    waiver = connection.execute(
        text("SELECT * FROM auditcore.p2_document_waivers WHERE tenant_id=:t AND waiver_id=:id AND journey_id=:j "
             "FOR UPDATE"),
        {"t": tenant_id, "id": task_id, "j": journey_id},
    ).mappings().one_or_none()
    if waiver is not None:
        return _decide_waiver(connection, tenant_id=tenant_id, journey_id=journey_id, waiver=waiver,
                              decision=decision, reason=reason, actor_id=actor_id, role=role)
    task = connection.execute(
        text("SELECT * FROM auditcore.p2_tasks WHERE tenant_id=:t AND task_id=:id AND journey_id=:j FOR UPDATE"),
        {"t": tenant_id, "id": task_id, "j": journey_id},
    ).mappings().one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Claim not found on this journey.")
    result = dict(task["completion_result"] or {})
    if task["task_status"] != "AWAITING_REQUESTER_REVIEW" or result.get("outcome") != CLAIMED:
        raise HTTPException(status_code=409, detail="This claim is not waiting for a decision.")
    result.update({"decidedBy": actor_id, "decidedRole": role, "decidedAt": datetime.now(UTC).isoformat(),
                   "decisionReason": reason or None})
    if decision == "ACCEPT":
        result.update({"outcome": ACCEPTED,
                       "acceptedIssueHash": (task["reference"] or {}).get("issueHash")})
        connection.execute(
            text("UPDATE auditcore.p2_tasks SET task_status='VERIFIED_COMPLETE', verified_at_utc=now(), "
                 "completion_result=CAST(:r AS jsonb), updated_at_utc=now() WHERE tenant_id=:t AND task_id=:id"),
            {"t": tenant_id, "id": task_id, "r": _json(result)},
        )
        event, status = "ACCEPT", "VERIFIED_COMPLETE"
    elif decision == "REJECT":
        result["outcome"] = REJECTED
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_tasks
                SET task_status='RETURNED', round_number=round_number + 1, completion_result=CAST(:r AS jsonb),
                    action_completed_at_utc=NULL, completed_by_actor_id=NULL, updated_at_utc=now()
                WHERE tenant_id=:t AND task_id=:id
                """
            ),
            {"t": tenant_id, "id": task_id, "r": _json(result)},
        )
        event, status = "RETURNED", "RETURNED"
    else:
        raise HTTPException(status_code=422, detail="decision is ACCEPT or REJECT.")
    connection.execute(
        text("UPDATE auditcore.p2_document_waivers SET status=:s, decided_by=:a, decided_role=:r, "
             "decided_at_utc=now(), decision_reason=:why, updated_at_utc=now() "
             "WHERE tenant_id=:t AND source_task_id=:id AND status='CLAIMED'"),
        {"t": tenant_id, "id": task_id, "s": "ACCEPTED" if decision == "ACCEPT" else "REJECTED", "a": actor_id,
         "r": role, "why": reason or None},
    )
    record_task_event(connection, tenant_id=tenant_id, journey_id=journey_id, task_id=task_id, event_type=event,
                      actor_id=actor_id, actor_role_code=role, comment=reason or None,
                      details={"claim": result["outcome"]})
    refresh_review_task(connection, tenant_id=tenant_id, journey_id=journey_id)
    return {"taskId": str(task_id), "status": status, "outcome": result["outcome"]}


def _decide_waiver(connection: Connection, *, tenant_id: str, journey_id: UUID, waiver: Any, decision: str,
                   reason: str, actor_id: str, role: str) -> dict[str, Any]:
    """Accept: the document counts as received. Reject: it is asked for again on the
    journey's missing-documents task, with the Reviewer's reason."""
    from audit_core.uc03_p2_task_producer import sync_document_missing_tasks
    from audit_core.uc03_p2_tasks import record_task_event

    if waiver["status"] != "CLAIMED":
        raise HTTPException(status_code=409, detail="This claim is not waiting for a decision.")
    status = "ACCEPTED" if decision == "ACCEPT" else "REJECTED"
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_waivers
            SET status=:status, decided_by=:actor, decided_role=:role, decided_at_utc=now(),
                decision_reason=:reason, updated_at_utc=now()
            WHERE tenant_id=:t AND waiver_id=:id
            """
        ),
        {"t": tenant_id, "id": waiver["waiver_id"], "status": status, "actor": actor_id, "role": role,
         "reason": reason or None},
    )
    outcome = ACCEPTED if status == "ACCEPTED" else REJECTED
    if waiver["source_task_id"] is not None:
        # A claim made on a per-document task of before 2026-11-15 parked that task
        # with the Reviewer; it closes with the decision (a rejected document is asked
        # for again on the journey's one missing-documents task, not on this one).
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_tasks
                SET task_status='VERIFIED_COMPLETE', verified_at_utc=now(), updated_at_utc=now(),
                    completion_result=COALESCE(completion_result, '{}'::jsonb) || CAST(:r AS jsonb)
                WHERE tenant_id=:t AND task_id=:id AND task_status='AWAITING_REQUESTER_REVIEW'
                  AND reference ? 'requirementKey'
                """
            ),
            {"t": tenant_id, "id": waiver["source_task_id"],
             "r": _json({"outcome": outcome, "decidedBy": actor_id, "decidedRole": role,
                         "decisionReason": reason or None, "mergedInto": f"document-missing:{journey_id}"})},
        )
        record_task_event(connection, tenant_id=tenant_id, journey_id=journey_id, task_id=waiver["source_task_id"],
                          event_type="ACCEPT" if status == "ACCEPTED" else "RETURNED", actor_id=actor_id,
                          actor_role_code=role, comment=reason or None,
                          details={"claim": outcome, "requirementKey": waiver["requirement_key"]})
    refresh_review_task(connection, tenant_id=tenant_id, journey_id=journey_id)
    sync_document_missing_tasks(connection, tenant_id=tenant_id, journey_id=journey_id,
                                evaluation_started_at=datetime.now(UTC))
    return {"taskId": str(waiver["waiver_id"]), "status": status, "outcome": outcome}


# --------------------------------------------------------------------------- endpoints

class ClaimDecision(BaseModel):
    decision: Literal["ACCEPT", "REJECT"]
    reason: str | None = Field(default=None, max_length=2000)


@router.get("/journeys/{journey_id}/not-mandatory-claims")
def list_claims(
    tenant_id: str, journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    authorize_p2(connection, tenant_id=tenant_id, journey_id=journey_id, human_principal=human_principal,
                 authorization_client=authorization_client, permission_key=_READ)
    return {"journeyId": str(journey_id), "claims": claims(connection, tenant_id=tenant_id, journey_id=journey_id)}


@router.post("/journeys/{journey_id}/not-mandatory-claims/{task_id}/decision")
def post_decision(
    tenant_id: str, journey_id: UUID, task_id: UUID, command: ClaimDecision,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    access = authorize_p2(connection, tenant_id=tenant_id, journey_id=journey_id, human_principal=human_principal,
                          authorization_client=authorization_client, permission_key=_WORK_UPDATE)
    return decide_claim(connection, tenant_id=tenant_id, journey_id=journey_id, task_id=task_id,
                        decision=command.decision, reason=command.reason, actor_id=access.actor_id,
                        actor_role_code=access.operating_role or access.functional_role or "")
