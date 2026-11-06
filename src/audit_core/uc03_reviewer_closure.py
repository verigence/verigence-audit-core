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
overall comment. Product owner, 2026-11-04 (second part): "Reviewer's closure
to replace the TL review ... TL should be now given consolidated informational
task on what all things have been closed by Reviewer. TL needs to accept it or
reject it. If TL doesnt take any action within 96 hours, then by default task
gets accepted ... auto approve ... if it get rejected, TL select which
specific tasks to be rejected, which gets open up again for Reviewer." So a
closure:

1. completes the Delivery (as the Reviewer's "complete delivery" does) and
   marks the journey reviewed (``mark_delivery_reviewed``, actor the
   Reviewer) -- the Team Lead's "Review the completed delivery" task closes;
2. writes the summary -- each item with its answer -- to
   ``p2_journey_closures``; the latest closure is in the compliance report;
3. raises ONE information task for the Team Lead (CLOSURE_ACCEPTANCE) listing
   every item and answer, due in 96 hours.

The Team Lead accepts the closure, or rejects specific items, each with a
reason. A rejection reopens the journey's review (SENT_BACK) and gives the
Reviewer one task (CLOSURE_REWORK) listing the rejected items; on the next
close the accepted items keep their answers and only the rejected ones need a
new one. A closure nobody decides within 96 hours is accepted automatically
(AUTO_ACCEPTED) by the worker's pass (``auto_accept_due``).
"""
from __future__ import annotations

import json
from typing import Annotated, Any, Literal
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
_NOT_ITEMS = frozenset({"DELIVERY_REVIEW", "NOT_MANDATORY_REVIEW", "REVIEWER_CORRECTIONS_INFO",
                        "CLOSURE_ACCEPTANCE", "CLOSURE_REWORK"})
ACCEPTANCE_TASK = "CLOSURE_ACCEPTANCE"
REWORK_TASK = "CLOSURE_REWORK"
ACCEPTANCE_WINDOW_HOURS = 96
DECIDING_ROLES = frozenset({"TL", "PM"})

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

    # After the Team Lead rejected items of the last closure: an item they
    # accepted keeps its answer; a rejected one shows why and needs a new one.
    last = latest_closure(connection, tenant_id=tenant_id, journey_id=journey_id)
    if last and last["acceptanceStatus"] == "REJECTED":
        previous = {i["key"]: i for i in last["items"]}
        rejected = {r["key"]: r for r in last["rejections"]}
        for item in items:
            if item["key"] in rejected:
                item["prefilledAnswer"] = None
                item["rejection"] = {"reason": rejected[item["key"]]["reason"],
                                     "previousAnswer": (previous.get(item["key"]) or {}).get("answer")}
            elif item["key"] in previous and previous[item["key"]].get("answer"):
                item["prefilledAnswer"] = previous[item["key"]]["answer"]
                item["carried"] = True

    blockers = [f"The PC's claim that “{c['document']}” is not mandatory is waiting for a decision."
                for c in pending]
    return {"items": items, "blockers": blockers}


def _closure_view(row: Any) -> dict[str, Any]:
    summary = row["summary"] or {}
    return {
        "closureId": str(row["closure_id"]), "journeyId": str(row["journey_id"]),
        "closedBy": row["closed_by_actor_id"], "closedRole": row["closed_by_role"],
        "comment": row["comment"], "itemCount": int(row["item_count"]), "items": summary.get("items", []),
        "counts": summary.get("counts", {}), "deliveryOutcome": row["delivery_outcome"],
        "closedAtUtc": row["closed_at_utc"], "acceptanceStatus": row["acceptance_status"],
        "acceptanceDueUtc": row["acceptance_due_utc"], "acceptanceTaskId": (str(row["acceptance_task_id"])
                                                                           if row["acceptance_task_id"] else None),
        "decidedBy": row["decided_by_actor_id"], "decidedRole": row["decided_by_role"],
        "decidedAtUtc": row["decided_at_utc"], "rejections": row["rejections"] or [],
    }


_CLOSURE_COLUMNS = ("closure_id, journey_id, closed_by_actor_id, closed_by_role, comment, item_count, summary, "
                    "delivery_outcome, closed_at_utc, acceptance_status, acceptance_due_utc, acceptance_task_id, "
                    "decided_by_actor_id, decided_by_role, decided_at_utc, rejections")


def latest_closure(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any] | None:
    row = connection.execute(
        text(f"SELECT {_CLOSURE_COLUMNS} FROM auditcore.p2_journey_closures WHERE tenant_id=:t AND journey_id=:j "
             "ORDER BY closed_at_utc DESC, closure_id DESC LIMIT 1"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one_or_none()
    return _closure_view(row) if row else None


def has_reviewer_closure(connection: Connection, *, tenant_id: str, journey_id: UUID) -> bool:
    """A journey a Reviewer closed: its review is the closure's, not the Team Lead's delivery review."""
    return connection.execute(
        text("SELECT EXISTS (SELECT 1 FROM auditcore.p2_journey_closures WHERE tenant_id=:t AND journey_id=:j)"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()


# --------------------------------------------------------------------------- closing

CLOSED_BY_CLOSURE = "CLOSED_BY_REVIEWER_CLOSURE"


def close_answered_tasks(
    connection: Connection, *, tenant_id: str, journey_id: UUID, closure_id: UUID | str,
    items: list[dict[str, Any]], actor_id: str,
) -> list[str]:
    """The open tasks the closure answered, closed with it (2026-11-06: a closed
    journey still showed "Fix pages that could not be processed" to the PC).

    - an open task item: that task;
    - an open finding: the finding's own task(s);
    - a missing document: its Document Missing task.

    Each keeps the answer, the closure and its issue hash (``acceptedIssueHash``),
    so the next sync does not reopen it for the same problem; a new problem (a
    different hash) still raises it again. The closure's own follow-up tasks
    are never touched. Returns the ids closed."""
    from audit_core.uc03_p2_tasks import record_task_event

    task_answers: dict[str, str] = {}
    finding_answers: dict[str, str] = {}
    document_answers: dict[str, str] = {}
    for item in items:
        kind, _, ref = str(item.get("key") or "").partition(":")
        answer = str(item.get("answer") or "")
        if kind == "task":
            task_answers[ref] = answer
        elif kind == "finding":
            finding_answers[ref] = answer
        elif kind == "document":
            document_answers[ref] = answer
    if not (task_answers or finding_answers or document_answers):
        return []
    rows = connection.execute(
        text(
            """
            SELECT task_id, task_type, reference
            FROM auditcore.p2_tasks
            WHERE tenant_id=:t AND journey_id=:j AND task_status = ANY(CAST(:open AS varchar[]))
              AND task_type <> ALL(CAST(:skip AS varchar[]))
              AND (CAST(task_id AS text) = ANY(CAST(:tasks AS text[]))
                   OR reference->>'findingId' = ANY(CAST(:findings AS text[]))
                   OR (task_type = 'DOCUMENT_MISSING'
                       AND reference->>'requirementKey' = ANY(CAST(:documents AS text[]))))
            FOR UPDATE
            """
        ),
        {"t": tenant_id, "j": journey_id, "open": list(_OPEN_TASK_STATUSES), "skip": list(_NOT_ITEMS),
         "tasks": list(task_answers), "findings": list(finding_answers), "documents": list(document_answers)},
    ).mappings().all()
    closed: list[str] = []
    for row in rows:
        task_id = str(row["task_id"])
        reference = dict(row["reference"] or {})
        answer = (task_answers.get(task_id)
                  or finding_answers.get(str(reference.get("findingId") or ""))
                  or document_answers.get(str(reference.get("requirementKey") or "")) or "")
        result = {"outcome": CLOSED_BY_CLOSURE, "closureId": str(closure_id), "answer": answer,
                  "closedBy": actor_id, "closedRole": "REVIEWER",
                  "acceptedIssueHash": reference.get("issueHash")}
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_tasks
                SET task_status='VERIFIED_COMPLETE', verified_at_utc=now(), updated_at_utc=now(),
                    completed_by_actor_id=:actor,
                    completion_result=COALESCE(completion_result, '{}'::jsonb) || CAST(:result AS jsonb)
                WHERE tenant_id=:t AND task_id=:id
                """
            ),
            {"t": tenant_id, "id": row["task_id"], "actor": actor_id, "result": _json(result)},
        )
        record_task_event(connection, tenant_id=tenant_id, journey_id=journey_id, task_id=row["task_id"],
                          event_type=CLOSED_BY_CLOSURE, actor_id=actor_id, actor_role_code="REVIEWER",
                          comment=answer or None, details={"closureId": str(closure_id)})
        closed.append(task_id)
    return closed


def close_journey(
    connection: Connection, *, tenant_id: str, journey_id: UUID, actor_id: str, actor_role: str,
    comment: str, answers: dict[str, str], correlation_id: str | None = None,
) -> dict[str, Any]:
    from audit_core.uc03_p2_runtime import note_facts_changed, record_activity
    from audit_core.uc03_p2_task_producer import (
        resolve_if_open,
        sync_delivery_review_task,
    )
    from audit_core.uc03_p2_tasks import create_p2_task, record_task_event
    from audit_core.uc03_p2_workflow import (
        complete_delivery_by_reviewer,
        mark_delivery_reviewed,
    )

    if actor_role.upper() not in CLOSING_ROLES:
        raise HTTPException(status_code=403, detail="Only a Reviewer closes a journey this way.")
    comment = (comment or "").strip()
    if len(comment) < MIN_ANSWER:
        raise HTTPException(status_code=422, detail="Write the closing comment (at least 10 characters).")
    reviewed = connection.execute(
        text("SELECT review_completed_at_utc IS NOT NULL FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j "
             "FOR UPDATE"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one_or_none()
    if reviewed is None:
        raise HTTPException(status_code=404, detail="Journey not found.")
    if reviewed:
        raise HTTPException(status_code=409, detail="This journey is already reviewed and closed.")
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
        source = "REVIEWER" if typed else ("PREVIOUS_CLOSURE" if item.get("carried") else "ACCEPTED_CLAIM")
        answered.append({**item, "answer": answer, "answerSource": source})
    if unanswered:
        # Named, so the Reviewer sees which item -- usually one that opened after
        # the form was loaded (a task reopened by a re-check, a new finding).
        raise HTTPException(
            status_code=422,
            detail=(f"Give a specific reason (at least {MIN_ANSWER} characters) for every open item. "
                    f"Still without one: {'; '.join(unanswered)}."),
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
    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_journey_closures (
                tenant_id, journey_id, closed_by_actor_id, closed_by_role, comment, item_count, summary,
                delivery_outcome, correlation_id, acceptance_due_utc
            ) VALUES (:t, :j, :actor, :role, :comment, :n, CAST(:summary AS jsonb), :outcome, :cid,
                      now() + make_interval(hours => :hours))
            RETURNING closure_id, acceptance_due_utc
            """
        ),
        {"t": tenant_id, "j": journey_id, "actor": actor_id, "role": actor_role.upper(), "comment": comment,
         "n": len(answered), "summary": _json(summary), "outcome": delivery["outcome"], "cid": correlation_id,
         "hours": ACCEPTANCE_WINDOW_HOURS},
    ).mappings().one()
    closure_id, due = row["closure_id"], row["acceptance_due_utc"]
    # The tasks those answers cover are closed with the journey: nothing is
    # left open for the PC or the Team Lead that the closure already answered.
    close_answered_tasks(connection, tenant_id=tenant_id, journey_id=journey_id, closure_id=closure_id,
                         items=answered, actor_id=actor_id)

    # The Reviewer's closure is the journey's review (it replaces the Team Lead's).
    mark_delivery_reviewed(connection, tenant_id=tenant_id, journey_id=journey_id, actor_id=actor_id,
                           actor_role="REVIEWER", correlation_id=correlation_id)
    sync_delivery_review_task(connection, tenant_id=tenant_id, journey_id=journey_id)
    resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=_rework_key(journey_id),
                    evidence={"closedAgain": str(closure_id)})

    # One information task for the Team Lead: everything the Reviewer closed, with the answers.
    count = len(answered)
    listed = "; ".join(f"{i['label']}: {i['answer']}" for i in answered[:12])
    more = f" (first 12 of {count} shown)" if count > 12 else ""
    task_id = create_p2_task(
        connection, tenant_id=tenant_id, journey_id=journey_id, task_type=ACCEPTANCE_TASK,
        category="TASK_CONFIRMATION", origin_kind="SYSTEM", source_type="REVIEW", source_code=ACCEPTANCE_TASK,
        dedupe_key=f"closure-acceptance:{closure_id}",
        title=f"Reviewer closed the journey with {count} open item{'s' if count != 1 else ''} answered",
        description=(f"The Reviewer closed this journey: “{comment}”. "
                     + (f"Items closed{more}: {listed}. " if count else "Nothing was open. ")
                     + f"Accept the closure, or reject specific items with a reason; it is accepted automatically "
                       f"if not decided within {ACCEPTANCE_WINDOW_HOURS} hours."),
        reference={"sourceCode": ACCEPTANCE_TASK, "closureId": str(closure_id), "comment": comment,
                   "closedBy": actor_id, "items": answered, "counts": counts,
                   "autoAcceptAtUtc": due.isoformat()},
        severity="MEDIUM", priority="NORMAL", assigned_role_code="TL", assigned_actor_id=None,
        raised_by_actor_id=actor_id, raised_by_role_code="REVIEWER",
        allowed_actions=["REVIEW_DOCUMENT", "ADD_COMMENT"], completion_protocol="MACHINE_VERIFIED",
        due_at_utc=due,
    )
    connection.execute(
        text("UPDATE auditcore.p2_journey_closures SET acceptance_task_id=:task WHERE tenant_id=:t AND closure_id=:c"),
        {"t": tenant_id, "c": closure_id, "task": task_id},
    )
    record_task_event(connection, tenant_id=tenant_id, journey_id=journey_id, task_id=task_id, event_type="RAISED",
                      actor_id=actor_id, actor_role_code="REVIEWER", details={"closureId": str(closure_id)})
    record_activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type="JOURNEY_CLOSED_BY_REVIEWER",
        subject_type="JOURNEY", subject_id=str(journey_id),
        details={"closureId": str(closure_id), "by": actor_id, "comment": comment, "counts": counts},
        correlation_id=correlation_id,
    )
    note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id, reason="JOURNEY_CLOSED_BY_REVIEWER")
    return {"closureId": str(closure_id), "deliveryOutcome": delivery["outcome"], "itemCount": count,
            "counts": counts, "acceptanceTaskId": str(task_id), "autoAcceptAtUtc": due}


def _rework_key(journey_id: UUID | str) -> str:
    return f"closure-rework:{journey_id}"


# --------------------------------------------------------------------------- the Team Lead's decision

def _close_acceptance_task(connection: Connection, *, tenant_id: str, closure: dict[str, Any], outcome: str,
                           actor_id: str, actor_role: str, comment: str | None,
                           details: dict[str, Any]) -> None:
    from audit_core.uc03_p2_tasks import record_task_event

    task_id = closure["acceptanceTaskId"]
    if not task_id:
        return
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks
            SET task_status='VERIFIED_COMPLETE', verified_at_utc=now(), updated_at_utc=now(),
                completed_by_actor_id=:actor,
                completion_result=COALESCE(completion_result, '{}'::jsonb) || CAST(:result AS jsonb)
            WHERE tenant_id=:t AND task_id=:id AND task_status <> 'VERIFIED_COMPLETE'
            """
        ),
        {"t": tenant_id, "id": task_id, "actor": actor_id,
         "result": _json({"outcome": outcome, "decidedBy": actor_id, "decidedRole": actor_role, **details})},
    )
    record_task_event(connection, tenant_id=tenant_id, journey_id=UUID(closure["journeyId"]), task_id=UUID(task_id),
                      event_type=outcome, actor_id=actor_id, actor_role_code=actor_role, comment=comment,
                      details=details)


def decide_closure(
    connection: Connection, *, tenant_id: str, journey_id: UUID, closure_id: UUID, decision: str,
    rejections: list[dict[str, str]] | None, comment: str | None, actor_id: str, actor_role: str,
) -> dict[str, Any]:
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_runtime import record_activity
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    role = (actor_role or "").upper()
    if role not in DECIDING_ROLES:
        raise HTTPException(status_code=403, detail="Only a Team Lead or Project Manager decides a closure.")
    row = connection.execute(
        text(f"SELECT {_CLOSURE_COLUMNS} FROM auditcore.p2_journey_closures WHERE tenant_id=:t AND journey_id=:j "
             "AND closure_id=:c FOR UPDATE"),
        {"t": tenant_id, "j": journey_id, "c": closure_id},
    ).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Closure not found on this journey.")
    closure = _closure_view(row)
    if closure["acceptanceStatus"] != "PENDING":
        raise HTTPException(status_code=409, detail=f"This closure is already {closure['acceptanceStatus'].lower()}.")
    decision = decision.upper()

    if decision == "ACCEPT":
        connection.execute(
            text("UPDATE auditcore.p2_journey_closures SET acceptance_status='ACCEPTED', decided_by_actor_id=:a, "
                 "decided_by_role=:r, decided_at_utc=now() WHERE tenant_id=:t AND closure_id=:c"),
            {"t": tenant_id, "c": closure_id, "a": actor_id, "r": role},
        )
        _close_acceptance_task(connection, tenant_id=tenant_id, closure=closure, outcome="ACCEPTED",
                               actor_id=actor_id, actor_role=role, comment=(comment or "").strip() or None,
                               details={"closureId": str(closure_id)})
        record_activity(connection, tenant_id=tenant_id, journey_id=journey_id, event_type="CLOSURE_ACCEPTED",
                        subject_type="JOURNEY", subject_id=str(journey_id),
                        details={"closureId": str(closure_id), "by": actor_id, "role": role})
        return {"closureId": str(closure_id), "acceptanceStatus": "ACCEPTED"}

    if decision != "REJECT":
        raise HTTPException(status_code=422, detail="decision is ACCEPT or REJECT.")
    by_key = {i["key"]: i for i in closure["items"]}
    chosen: list[dict[str, str]] = []
    problems: list[str] = []
    for entry in rejections or []:
        key, reason = str(entry.get("key") or ""), (entry.get("reason") or "").strip()
        if key not in by_key:
            problems.append(f"{key or '(no item)'} is not an item of this closure")
        elif len(reason) < MIN_ANSWER:
            problems.append(f"Say why “{by_key[key]['label']}” is rejected (at least 10 characters)")
        else:
            chosen.append({"key": key, "label": by_key[key]["label"], "kind": by_key[key]["kind"], "reason": reason})
    if not chosen and not problems:
        problems.append("Choose the items to reject, each with a reason")
    if problems:
        raise HTTPException(status_code=422, detail=f"The rejection is incomplete: {'; '.join(problems)}.")

    connection.execute(
        text("UPDATE auditcore.p2_journey_closures SET acceptance_status='REJECTED', decided_by_actor_id=:a, "
             "decided_by_role=:r, decided_at_utc=now(), rejections=CAST(:rej AS jsonb) "
             "WHERE tenant_id=:t AND closure_id=:c"),
        {"t": tenant_id, "c": closure_id, "a": actor_id, "r": role, "rej": _json(chosen)},
    )
    # The review is reopened: the Reviewer answers the rejected items again and closes again.
    connection.execute(
        text("UPDATE auditcore.journeys SET review_completed_at_utc=NULL, audit_state='SENT_BACK', "
             "audit_outcome='PENDING', updated_at_utc=now() WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    )
    _close_acceptance_task(connection, tenant_id=tenant_id, closure=closure, outcome="REJECTED", actor_id=actor_id,
                           actor_role=role, comment=(comment or "").strip() or None,
                           details={"closureId": str(closure_id), "rejections": chosen})
    listed = "; ".join(f"{r['label']}: {r['reason']}" for r in chosen)
    raise_or_refresh(
        connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=_rework_key(journey_id),
        task_type=REWORK_TASK, source_type="REVIEW", source_code=REWORK_TASK,
        title=f"Team Lead rejected {len(chosen)} item{'s' if len(chosen) != 1 else ''} of your closure",
        description=(f"The {('Team Lead' if role == 'TL' else 'Project Manager')} rejected: {listed}. Act on them, "
                     "then close the journey again: the accepted items keep their answers."),
        reference={"sourceCode": REWORK_TASK, "closureId": str(closure_id), "rejections": chosen,
                   "fields": [str(closure_id)]},
        severity="HIGH", registry=get_registry(),
    )
    record_activity(connection, tenant_id=tenant_id, journey_id=journey_id, event_type="CLOSURE_REJECTED",
                    subject_type="JOURNEY", subject_id=str(journey_id),
                    details={"closureId": str(closure_id), "by": actor_id, "role": role, "rejections": chosen})
    return {"closureId": str(closure_id), "acceptanceStatus": "REJECTED", "rejected": len(chosen)}


def auto_accept_due(connection: Connection, *, tenant_id: str) -> int:
    """Accept every closure nobody decided within 96 hours (AUTO_ACCEPTED). Run by the worker."""
    from audit_core.uc03_p2_runtime import record_activity

    rows = connection.execute(
        text(f"SELECT {_CLOSURE_COLUMNS} FROM auditcore.p2_journey_closures WHERE tenant_id=:t "
             "AND acceptance_status='PENDING' AND acceptance_due_utc <= now() "
             "ORDER BY acceptance_due_utc FOR UPDATE SKIP LOCKED LIMIT 200"),
        {"t": tenant_id},
    ).mappings().all()
    for row in rows:
        closure = _closure_view(row)
        connection.execute(
            text("UPDATE auditcore.p2_journey_closures SET acceptance_status='AUTO_ACCEPTED', "
                 "decided_by_actor_id='SYSTEM', decided_by_role='SYSTEM', decided_at_utc=now() "
                 "WHERE tenant_id=:t AND closure_id=:c"),
            {"t": tenant_id, "c": row["closure_id"]},
        )
        _close_acceptance_task(connection, tenant_id=tenant_id, closure=closure, outcome="AUTO_ACCEPTED",
                               actor_id="SYSTEM", actor_role="SYSTEM",
                               comment=f"Not decided within {ACCEPTANCE_WINDOW_HOURS} hours: accepted automatically.",
                               details={"closureId": closure["closureId"]})
        record_activity(connection, tenant_id=tenant_id, journey_id=UUID(closure["journeyId"]),
                        event_type="CLOSURE_AUTO_ACCEPTED", subject_type="JOURNEY", subject_id=closure["journeyId"],
                        details={"closureId": closure["closureId"], "dueUtc": closure["acceptanceDueUtc"]})
    return len(rows)


# --------------------------------------------------------------------------- endpoints

class ClosureCommand(BaseModel):
    comment: str = Field(min_length=1, max_length=4000)
    # Item key (as the preview lists it) -> the specific answer for it.
    answers: dict[str, str] = Field(default_factory=dict)


class ClosureRejection(BaseModel):
    key: str = Field(min_length=1, max_length=300)
    reason: str = Field(default="", max_length=2000)


class ClosureDecision(BaseModel):
    decision: Literal["ACCEPT", "REJECT"]
    rejections: list[ClosureRejection] = Field(default_factory=list)
    comment: str | None = Field(default=None, max_length=4000)


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
    reviewed = connection.execute(
        text("SELECT review_completed_at_utc IS NOT NULL FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()
    return {
        "journeyId": str(journey_id),
        "canClose": (access.operating_role or "").upper() in CLOSING_ROLES and not current["blockers"] and not reviewed,
        "reviewed": bool(reviewed),
        "minAnswerLength": MIN_ANSWER,
        "acceptanceWindowHours": ACCEPTANCE_WINDOW_HOURS,
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


@router.post("/journeys/{journey_id}/reviewer-closure/{closure_id}/decision")
def post_closure_decision(
    tenant_id: str, journey_id: UUID, closure_id: UUID, command: ClosureDecision,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    access = authorize_p2(connection, tenant_id=tenant_id, journey_id=journey_id, human_principal=human_principal,
                          authorization_client=authorization_client, permission_key="audit.work.update")
    set_tenant_context(connection, tenant_id)
    return decide_closure(connection, tenant_id=tenant_id, journey_id=journey_id, closure_id=closure_id,
                          decision=command.decision, rejections=[r.model_dump() for r in command.rejections],
                          comment=command.comment, actor_id=access.actor_id,
                          actor_role=access.operating_role or access.functional_role or "")


_CLOSURE_STATUSES = frozenset({"PENDING", "ACCEPTED", "AUTO_ACCEPTED", "REJECTED"})


@router.get("/reviewer-closures")
def list_closures(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    status: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Reviewer closures in the caller's outlets, newest first: the Team Lead's
    pending decisions and the ones accepted automatically, which TL, PM and
    Executive see (product owner 2026-11-04)."""
    access = authorize_p2(connection, tenant_id=tenant_id, journey_id=None, human_principal=human_principal,
                          authorization_client=authorization_client, permission_key="audit.journey.read")
    set_tenant_context(connection, tenant_id)
    wanted = (status or "").upper() or None
    if wanted and wanted not in _CLOSURE_STATUSES:
        raise HTTPException(status_code=422, detail="status is PENDING, ACCEPTED, AUTO_ACCEPTED or REJECTED.")
    cols = ", ".join(f"c.{c.strip()}" for c in _CLOSURE_COLUMNS.split(","))
    rows = connection.execute(
        text(
            f"""
            SELECT {cols}, j.journey_reference, COALESCE(cu.legal_name, cu.display_name) AS customer_name,
                   o.outlet_name, d.dealer_name
            FROM auditcore.p2_journey_closures c
            JOIN auditcore.journeys j ON j.tenant_id=c.tenant_id AND j.journey_id=c.journey_id
            JOIN auditcore.customers cu ON cu.tenant_id=j.tenant_id AND cu.customer_id=j.customer_id
            JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
            JOIN auditcore.dealer_outlets o ON o.tenant_id=j.tenant_id AND o.dealer_id=j.dealer_id
                                           AND o.outlet_id=j.outlet_id
            WHERE c.tenant_id=:t
              AND (CAST(:status AS varchar) IS NULL OR c.acceptance_status=CAST(:status AS varchar))
              AND EXISTS (
                SELECT 1 FROM auditcore.business_assignments ba
                WHERE ba.tenant_id=j.tenant_id AND ba.security_actor_id=:actor AND ba.assignment_status='ACTIVE'
                  AND ba.effective_from <= now() AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                  AND (ba.dealer_id IS NULL OR (ba.dealer_id=j.dealer_id
                       AND (ba.outlet_id IS NULL OR ba.outlet_id=j.outlet_id))))
            ORDER BY c.closed_at_utc DESC, c.closure_id DESC
            LIMIT :limit
            """
        ),
        {"t": tenant_id, "status": wanted, "actor": access.actor_id, "limit": max(1, min(limit, 500))},
    ).mappings().all()
    counts = {r["acceptance_status"]: int(r["n"]) for r in connection.execute(
        text(
            """
            SELECT c.acceptance_status, count(*) AS n
            FROM auditcore.p2_journey_closures c
            JOIN auditcore.journeys j ON j.tenant_id=c.tenant_id AND j.journey_id=c.journey_id
            WHERE c.tenant_id=:t AND EXISTS (
                SELECT 1 FROM auditcore.business_assignments ba
                WHERE ba.tenant_id=j.tenant_id AND ba.security_actor_id=:actor AND ba.assignment_status='ACTIVE'
                  AND ba.effective_from <= now() AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                  AND (ba.dealer_id IS NULL OR (ba.dealer_id=j.dealer_id
                       AND (ba.outlet_id IS NULL OR ba.outlet_id=j.outlet_id))))
            GROUP BY 1
            """
        ),
        {"t": tenant_id, "actor": access.actor_id},
    ).mappings().all()}
    return {
        "status": wanted,
        "counts": {s: counts.get(s, 0) for s in sorted(_CLOSURE_STATUSES)},
        "closures": [
            {**_closure_view(r), "journeyReference": r["journey_reference"], "customerName": r["customer_name"],
             "outletName": r["outlet_name"], "dealerName": r["dealer_name"]}
            for r in rows
        ],
    }


# --------------------------------------------------------------------------- shown on every journey view

def closure_columns_sql(journey_alias: str = "j") -> str:
    """Two columns any journey query adds so every view shows a Reviewer-closed
    journey the same way (product owner 2026-11-04: "show in all the views
    across the PC/TL/PM/Executives, journey which get closed"):
    ``reviewer_closure_status`` (PENDING / ACCEPTED / AUTO_ACCEPTED / REJECTED of
    the latest closure, or NULL) and ``reviewer_closed_at_utc``."""
    a = journey_alias
    latest = (f"FROM auditcore.p2_journey_closures rc WHERE rc.tenant_id={a}.tenant_id "
              f"AND rc.journey_id={a}.journey_id ORDER BY rc.closed_at_utc DESC, rc.closure_id DESC LIMIT 1")
    return (f"(SELECT rc.acceptance_status {latest}) AS reviewer_closure_status, "
            f"(SELECT rc.closed_at_utc {latest}) AS reviewer_closed_at_utc")


def closure_fields(row: Any) -> dict[str, Any]:
    """The API shape of those two columns: ``reviewerClosure`` or None."""
    status = row.get("reviewer_closure_status") if hasattr(row, "get") else None
    if not status:
        return {"reviewerClosure": None}
    return {"reviewerClosure": {"status": status, "closedAtUtc": row.get("reviewer_closed_at_utc")}}


def journey_closed_sql(journey_alias: str = "j") -> str:
    """The one "closed" rule every view uses: Delivery complete, or the booking
    cancelled / a duplicate / closed without delivery (as the Bookings list)."""
    a = journey_alias
    return (f"EXISTS (SELECT 1 FROM auditcore.journey_stage_states cst WHERE cst.tenant_id={a}.tenant_id "
            f"AND cst.journey_id={a}.journey_id AND ((cst.stage_code='DELIVERY' AND cst.business_completed_at_utc "
            f"IS NOT NULL) OR (cst.stage_code='BOOKING' AND (cst.business_status IN ('BOOKING_CANCELLED',"
            f"'DUPLICATE_BOOKING') OR cst.closure_disposition='NO_DELIVERY'))))")


def journey_closure_state(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """For a single-journey screen: closed or not, and the Reviewer's closure."""
    row = connection.execute(
        text(f"SELECT {journey_closed_sql('j')} AS closed, j.review_completed_at_utc, {closure_columns_sql('j')} "
             "FROM auditcore.journeys j WHERE j.tenant_id=:t AND j.journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one_or_none()
    if row is None:
        return {"closed": False, "reviewedAtUtc": None, "reviewerClosure": None}
    return {"closed": bool(row["closed"]), "reviewedAtUtc": row["review_completed_at_utc"], **closure_fields(row)}


def closure_states(connection: Connection, *, tenant_id: str, journey_ids: list[Any]) -> dict[str, dict[str, Any]]:
    """The closed marker for many journeys at once, keyed by journey id (str):
    {"closed", "reviewedAtUtc", "reviewerClosure"}. For list endpoints whose own
    query is left as it is; one query whatever the page size."""
    ids = sorted({str(j) for j in journey_ids if j})
    if not ids:
        return {}
    rows = connection.execute(
        text(f"SELECT j.journey_id, {journey_closed_sql('j')} AS closed, j.review_completed_at_utc, "
             f"{closure_columns_sql('j')} FROM auditcore.journeys j "
             "WHERE j.tenant_id=:t AND j.journey_id = ANY(CAST(:ids AS uuid[]))"),
        {"t": tenant_id, "ids": ids},
    ).mappings().all()
    return {str(r["journey_id"]): {"closed": bool(r["closed"]), "reviewedAtUtc": r["review_completed_at_utc"],
                                   **closure_fields(r)} for r in rows}
