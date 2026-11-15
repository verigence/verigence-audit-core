"""Every task has an end the PC can reach (product owner 2026-11-15).

    "task should be closed by PC, they shouldnt remain open despite they
    updating the docs or comments where ever as applicable"

On 15 Nov, 412 tasks a PC, TL or Reviewer had acted on were still open, almost
all for more than a day. Four causes, four rules:

1. A check the PC cannot fix by uploading (the names are the same person, the
   document is right as it is) looped: upload -> re-check fails -> returned.
   ESCALATE_TO_TL: on a task returned after the re-check, or on a wrong-name
   task at any time, the PC writes why and sends it to the Team Lead. The task
   goes to the TL (or a Reviewer of the outlet) with "accept as exception";
   it never comes back to the PC unless the TL returns it.
2. "Checking your fix" could hang: a task VERIFYING for more than an hour with
   nothing of the journey still being read is returned to the PC saying it was
   checked again and is still open, and how to close it.
3. A TL's accepted exception reopened whenever another PC task opened or
   closed: "delivery not completed in time" carried the pending list in its
   title, which is part of the accepted issue. Its title is now stable
   (uc03_p2_audit_rules); an exception still reopens when the compared values
   themselves change.
4. "Not required" claims waited for a Reviewer for days: the combined review
   task left for 48 hours goes to the Team Lead too.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

ESCALATE_ACTION = "ESCALATE_TO_TL"
# Tasks a PC can never close by uploading: the name on a document is not the
# booking's (the PC can only say why).
NAME_TASK_SOURCES = frozenset({"WRONG_CUSTOMER_NAME", "WRONG_DEALER_NAME"})
MIN_REASON = 10
VERIFYING_MINUTES = 60
CLAIM_REVIEW_HOURS = 48
_OPEN = ("READY", "IN_PROGRESS", "RETURNED")


def can_escalate(task: dict[str, Any], actor_role_code: str | None) -> bool:
    """The PC (or whoever the task is with) may send it to the Team Lead."""
    if str(task.get("assigned_role_code") or "").upper() != "PC":
        return False
    if str(task.get("task_status")) not in _OPEN:
        return False
    if str(actor_role_code or "").upper() not in {"PC", "REVIEWER"}:
        return False
    return str(task.get("task_status")) == "RETURNED" or str(task.get("source_code") or "") in NAME_TASK_SOURCES


def escalate(connection: Connection, *, tenant_id: str, task: dict[str, Any], actor_id: str,
             actor_role_code: str, comment: str | None) -> dict[str, Any]:
    reason = (comment or "").strip()
    if len(reason) < MIN_REASON:
        raise ValueError("Write why this is right as it is (at least 10 characters) before sending it to the "
                         "Team Lead.")
    reference = dict(task.get("reference") or {})
    reference["escalated"] = {"by": actor_id, "role": actor_role_code, "reason": reason,
                              "at": datetime.now(UTC).isoformat()}
    actions = list(task.get("allowed_actions") or [])
    for action in ("ACCEPT_EXCEPTION", "ADD_COMMENT"):
        if action not in actions:
            actions.append(action)
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks
            SET assigned_role_code='TL', assigned_actor_id=NULL, task_status='READY',
                reference=CAST(:reference AS jsonb), allowed_actions=CAST(:actions AS jsonb), updated_at_utc=now()
            WHERE tenant_id=:t AND task_id=:id
            """
        ),
        {"t": tenant_id, "id": task["task_id"], "reference": json.dumps(reference, default=str),
         "actions": json.dumps(actions)},
    )
    return {"taskId": str(task["task_id"]), "status": "READY", "outcome": "ESCALATED_TO_TL"}


def return_stale_verifying(connection: Connection, *, tenant_id: str) -> int:
    """VERIFYING for more than an hour, nothing of the journey being read, no
    verification queued: back to the PC with what to do."""
    from audit_core.uc03_p2_journey_views import _IN_FLIGHT
    from audit_core.uc03_p2_tasks import record_task_event

    rows = connection.execute(
        text(
            """
            SELECT t.task_id, t.journey_id FROM auditcore.p2_tasks t
            WHERE t.tenant_id=:t AND t.task_status='VERIFYING'
              AND t.updated_at_utc < now() - make_interval(mins => :minutes)
              AND NOT EXISTS (SELECT 1 FROM auditcore.p2_document_queue q
                              WHERE q.tenant_id=t.tenant_id AND q.journey_id=t.journey_id
                                AND q.queue_status = ANY(CAST(:in_flight AS text[])))
              AND NOT EXISTS (SELECT 1 FROM auditcore.p2_work_queue w
                              WHERE w.tenant_id=t.tenant_id AND w.work_type='TASK_VERIFY'
                                AND w.work_key=t.task_id::text AND w.work_status IN ('PENDING','RETRY_WAIT','CLAIMED'))
            FOR UPDATE OF t SKIP LOCKED
            """
        ),
        {"t": tenant_id, "minutes": VERIFYING_MINUTES, "in_flight": list(_IN_FLIGHT)},
    ).mappings().all()
    reason = ("Checked again: the issue is still there. If the document is right as it is, close the task with "
              "a comment, or explain and send it to the Team Lead.")
    for row in rows:
        connection.execute(
            text("UPDATE auditcore.p2_tasks SET task_status='RETURNED', updated_at_utc=now() "
                 "WHERE tenant_id=:t AND task_id=:id AND task_status='VERIFYING'"),
            {"t": tenant_id, "id": row["task_id"]},
        )
        record_task_event(connection, tenant_id=tenant_id, journey_id=UUID(str(row["journey_id"])),
                          task_id=UUID(str(row["task_id"])), event_type="MACHINE_VERIFICATION_FAIL",
                          actor_id="SYSTEM", actor_role_code="SYSTEM", details={"reason": reason})
    return len(rows)


def escalate_waiting_claims(connection: Connection, *, tenant_id: str) -> int:
    """The combined "not required" review left with the Reviewer for 48 hours goes to the TL too."""
    from audit_core.uc03_not_mandatory_claims import CLAIM_REVIEW_TASK

    return len(connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks SET assigned_role_code='TL', updated_at_utc=now()
            WHERE tenant_id=:t AND task_type=:type AND assigned_role_code='REVIEWER'
              AND task_status IN ('READY','IN_PROGRESS','RETURNED')
              AND created_at_utc < now() - make_interval(hours => :hours)
            RETURNING task_id
            """
        ),
        {"t": tenant_id, "type": CLAIM_REVIEW_TASK, "hours": CLAIM_REVIEW_HOURS},
    ).scalars().all())


def run_closure_watchdog(connection: Connection, *, tenant_id: str) -> dict[str, int]:
    return {"verifyingReturned": return_stale_verifying(connection, tenant_id=tenant_id),
            "claimReviewsEscalated": escalate_waiting_claims(connection, tenant_id=tenant_id)}
