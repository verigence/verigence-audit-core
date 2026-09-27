"""Phase 2 task lifecycle.

New task semantics live only in p2_* tables. Machine-raised work is deduped by
a deterministic dedupe_key and cannot be finally closed by a human click:
ACTION_COMPLETED schedules TASK_VERIFY. Human-raised work round-trips to the
original requester, who accepts or rejects; reject requires a comment.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_document_field_corrections import _apply_field_value
from audit_core.uc03_p2_runtime import note_facts_changed

# Phase 2-only task code. Deliberately not named *TASK_TYPE: the legacy
# UC03 review-queue classifier must not treat isolated p2_tasks as legacy
# workflow_tasks.
_REQUESTER_CONFIRMATION_CODE = "REQUESTER_CONFIRMATION"
_FIELD_CORRECTION_REVIEW_CODE = "FIELD_CORRECTION_REVIEW_P2"

# Terminal states accept commentary only; nothing can re-open them by a click.
_TERMINAL_STATUSES = frozenset({"VERIFIED_COMPLETE", "CANCELLED", "FAILED", "DEAD_LETTER"})
# Work has been handed back to the system/requester; the assignee must wait.
_AWAITING_STATUSES = frozenset({"VERIFYING", "AWAITING_REQUESTER_REVIEW", "ACTION_COMPLETED"})
_COMMENTARY_ACTIONS = frozenset({"ADD_COMMENT", "PROVIDE_FEEDBACK"})
# Accepting a machine finding as a legitimate business exception is a
# supervisory decision, regardless of which role the task is assigned to.
_EXCEPTION_ROLES = frozenset({"TL", "PM"})


class TaskStateError(ValueError):
    """The task's current status does not allow this action."""


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def create_p2_task(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    task_type: str,
    category: str,
    origin_kind: str,
    source_type: str,
    source_code: str | None,
    dedupe_key: str,
    title: str,
    description: str,
    reference: dict[str, Any],
    severity: str,
    priority: str,
    assigned_role_code: str,
    assigned_actor_id: str | None,
    raised_by_actor_id: str | None,
    raised_by_role_code: str | None,
    allowed_actions: list[str],
    completion_protocol: str,
    due_at_utc: datetime | None = None,
    root_task_id: UUID | None = None,
    parent_task_id: UUID | None = None,
    round_number: int = 1,
) -> UUID:
    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_tasks (
                tenant_id, journey_id, root_task_id, parent_task_id, round_number,
                task_type, category, origin_kind, source_type, source_code,
                dedupe_key, title, description, reference,
                severity, priority, assigned_role_code, assigned_actor_id,
                raised_by_actor_id, raised_by_role_code, allowed_actions,
                completion_protocol, due_at_utc
            ) VALUES (
                :tenant_id, :journey_id, :root_task_id, :parent_task_id, :round_number,
                :task_type, :category, :origin_kind, :source_type, :source_code,
                :dedupe_key, :title, :description, CAST(:reference AS jsonb),
                :severity, :priority, :assigned_role_code, :assigned_actor_id,
                :raised_by_actor_id, :raised_by_role_code, CAST(:allowed_actions AS jsonb),
                :completion_protocol, :due_at_utc
            )
            ON CONFLICT (tenant_id, dedupe_key)
            DO UPDATE SET
                title=EXCLUDED.title,
                description=EXCLUDED.description,
                reference=EXCLUDED.reference,
                severity=EXCLUDED.severity,
                priority=EXCLUDED.priority,
                due_at_utc=EXCLUDED.due_at_utc,
                updated_at_utc=now()
            RETURNING task_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "root_task_id": root_task_id,
            "parent_task_id": parent_task_id,
            "round_number": round_number,
            "task_type": task_type,
            "category": category,
            "origin_kind": origin_kind,
            "source_type": source_type,
            "source_code": source_code,
            "dedupe_key": dedupe_key,
            "title": title,
            "description": description,
            "reference": _json(reference),
            "severity": severity,
            "priority": priority,
            "assigned_role_code": assigned_role_code,
            "assigned_actor_id": assigned_actor_id,
            "raised_by_actor_id": raised_by_actor_id,
            "raised_by_role_code": raised_by_role_code,
            "allowed_actions": _json(allowed_actions),
            "completion_protocol": completion_protocol,
            "due_at_utc": due_at_utc,
        },
    ).scalar_one()
    task_id = UUID(str(row))
    if root_task_id is None:
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_tasks
                SET root_task_id=task_id
                WHERE tenant_id=:tenant_id AND task_id=:task_id AND root_task_id IS NULL
                """
            ),
            {"tenant_id": tenant_id, "task_id": task_id},
        )
    return task_id


def record_task_event(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    task_id: UUID,
    event_type: str,
    actor_id: str | None,
    actor_role_code: str | None,
    comment: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_task_events (
                tenant_id, task_id, journey_id, event_type,
                actor_id, actor_role_code, comment, details
            ) VALUES (
                :tenant_id, :task_id, :journey_id, :event_type,
                :actor_id, :actor_role_code, :comment, CAST(:details AS jsonb)
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": task_id,
            "journey_id": journey_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "actor_role_code": actor_role_code,
            "comment": comment,
            "details": _json(details or {}),
        },
    )


def _task(connection: Connection, *, tenant_id: str, task_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT *
            FROM auditcore.p2_tasks
            WHERE tenant_id=:tenant_id AND task_id=:task_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "task_id": task_id},
    ).mappings().one()
    return dict(row)


def _enqueue_verification(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    task_id: UUID,
    reference: dict[str, Any],
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_work_queue (
                tenant_id, journey_id, work_type, work_key, payload, work_status
            ) VALUES (
                :tenant_id, :journey_id, 'TASK_VERIFY', :work_key,
                CAST(:payload AS jsonb), 'PENDING'
            )
            ON CONFLICT (tenant_id, work_type, work_key)
            DO UPDATE SET payload=EXCLUDED.payload,
                          work_status='PENDING',
                          next_attempt_at_utc=NULL,
                          last_error=NULL,
                          updated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "work_key": str(task_id),
            "payload": _json({"taskId": str(task_id), "reference": reference}),
        },
    )


def _create_requester_review(
    connection: Connection,
    *,
    tenant_id: str,
    task: dict[str, Any],
    actor_id: str,
    actor_role_code: str,
) -> UUID:
    root_id = UUID(str(task["root_task_id"] or task["task_id"]))
    review_dedupe = f"requester-review:{root_id}:round:{task['round_number']}"
    reference = dict(task["reference"] or {})
    reference["actionTaskId"] = str(task["task_id"])
    reference["actionCompletedBy"] = actor_id
    return create_p2_task(
        connection,
        tenant_id=tenant_id,
        journey_id=UUID(str(task["journey_id"])),
        task_type=_REQUESTER_CONFIRMATION_CODE,
        category="TASK_CONFIRMATION",
        origin_kind="SYSTEM",
        source_type="HUMAN_ACTION",
        source_code=task["task_type"],
        dedupe_key=review_dedupe,
        title=f"Confirm: {task['title']}",
        description=(
            "The requested action was completed. Review the response and either "
            "accept it or reject it with a comment."
        ),
        reference=reference,
        severity=str(task["severity"]),
        priority=str(task["priority"]),
        assigned_role_code=str(task["raised_by_role_code"] or actor_role_code),
        assigned_actor_id=task["raised_by_actor_id"],
        raised_by_actor_id=actor_id,
        raised_by_role_code=actor_role_code,
        allowed_actions=["ACCEPT", "REJECT", "ADD_COMMENT"],
        completion_protocol="REQUESTER_CONFIRMED",
        due_at_utc=task["due_at_utc"],
        root_task_id=root_id,
        parent_task_id=UUID(str(task["task_id"])),
        round_number=int(task["round_number"]),
    )


def submit_action(
    connection: Connection,
    *,
    tenant_id: str,
    task_id: UUID,
    action: str,
    actor_id: str,
    actor_role_code: str,
    comment: str | None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task = _task(connection, tenant_id=tenant_id, task_id=task_id)
    allowed = list(task["allowed_actions"] or [])
    if action not in allowed:
        raise ValueError(f"Action {action} is not allowed for this task")
    status = str(task["task_status"])
    if action not in _COMMENTARY_ACTIONS:
        if status in _TERMINAL_STATUSES:
            raise TaskStateError(f"This task is already {status.replace('_', ' ').lower()}.")
        if status in _AWAITING_STATUSES:
            raise TaskStateError(
                "This task is waiting for verification; no further action is needed right now."
            )

    assigned_actor_id = task.get("assigned_actor_id")
    assigned_role_code = str(task.get("assigned_role_code") or "")
    supervisory = action == "ACCEPT_EXCEPTION"
    if supervisory:
        if actor_role_code not in _EXCEPTION_ROLES:
            raise ValueError("Only a Team Lead or Project Manager can accept an exception")
    elif assigned_actor_id is not None and str(assigned_actor_id) != actor_id:
        raise ValueError("This task is assigned to a different actor")
    elif assigned_actor_id is None and assigned_role_code and assigned_role_code != actor_role_code:
        raise ValueError(
            f"This task is assigned to role {assigned_role_code}, not {actor_role_code}"
        )

    journey_id = UUID(str(task["journey_id"]))
    record_task_event(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        task_id=task_id,
        event_type=action,
        actor_id=actor_id,
        actor_role_code=actor_role_code,
        comment=comment,
        details=details,
    )

    if action in {"ADD_COMMENT", "PROVIDE_FEEDBACK"}:
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_tasks
                SET task_status=CASE WHEN task_status='READY' THEN 'IN_PROGRESS' ELSE task_status END,
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND task_id=:task_id
                """
            ),
            {"tenant_id": tenant_id, "task_id": task_id},
        )
        return {"taskId": str(task_id), "status": "IN_PROGRESS"}

    if supervisory:
        if not (comment or "").strip():
            raise ValueError("Accepting an exception requires a comment explaining why")
        reference = dict(task["reference"] or {})
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_tasks
                SET task_status='VERIFIED_COMPLETE', verified_at_utc=now(),
                    completion_result=CAST(:result AS jsonb), updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND task_id=:task_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "task_id": task_id,
                "result": _json({
                    "outcome": "EXCEPTION_ACCEPTED",
                    "acceptedIssueHash": reference.get("issueHash"),
                    "acceptedBy": actor_id,
                    "acceptedRole": actor_role_code,
                    "acceptedAt": datetime.now(UTC).isoformat(),
                    "comment": comment.strip(),
                }),
            },
        )
        return {"taskId": str(task_id), "status": "VERIFIED_COMPLETE", "outcome": "EXCEPTION_ACCEPTED"}

    if task["task_type"] == _FIELD_CORRECTION_REVIEW_CODE:
        reference = dict(task["reference"] or {})
        if action not in {"APPROVE_CORRECTION", "REJECT_CORRECTION"}:
            raise ValueError(f"Action {action} is not valid for a correction review task")

        if action == "REJECT_CORRECTION":
            if not (comment or "").strip():
                raise ValueError("Reject correction requires a comment")
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_tasks
                    SET task_status='VERIFIED_COMPLETE',
                        verified_at_utc=now(),
                        completion_result=CAST(:result AS jsonb),
                        updated_at_utc=now()
                    WHERE tenant_id=:tenant_id AND task_id=:task_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "task_id": task_id,
                    "result": _json({
                        "outcome": "REJECT_CORRECTION",
                        "verifiedBy": actor_id,
                        "verifiedAt": datetime.now(UTC).isoformat(),
                        "comment": comment.strip(),
                    }),
                },
            )
            return {
                "taskId": str(task_id),
                "status": "VERIFIED_COMPLETE",
                "outcome": "REJECT_CORRECTION",
            }

        required = (
            "documentId",
            "stage",
            "fieldKey",
            "canonicalFieldId",
            "sourceFactVersion",
            "proposedValue",
        )
        missing = [key for key in required if key not in reference]
        if missing:
            raise ValueError(
                "Correction review task is missing required reference data: "
                + ", ".join(missing)
            )

        # Re-read the field under lock: the machine value is always taken from
        # the durable store (never from the task payload), and a proposal made
        # against a value that has since changed must be re-proposed.
        current = connection.execute(
            text(
                """
                SELECT extracted_value, effective_value
                FROM auditcore.journey_document_extracted_fields
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND di_document_id=:document_id
                  AND source_canonical_field_id=:canonical_field_id
                  AND source_fact_version=:source_fact_version
                  AND field_key=:field_key
                ORDER BY updated_at_utc DESC
                LIMIT 1
                FOR UPDATE
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "document_id": UUID(str(reference["documentId"])),
                "canonical_field_id": str(reference["canonicalFieldId"]),
                "source_fact_version": int(reference["sourceFactVersion"]),
                "field_key": str(reference["fieldKey"]),
            },
        ).mappings().one_or_none()
        if current is None:
            raise TaskStateError("The field no longer exists on this document.")
        current_effective = (
            current["effective_value"]
            if current["effective_value"] is not None
            else current["extracted_value"]
        )
        if (
            "currentEffectiveValue" in reference
            and _json(current_effective) != _json(reference["currentEffectiveValue"])
        ):
            raise TaskStateError(
                "The field changed after this correction was proposed. Reject it and propose again."
            )

        evidence_id = reference.get("evidenceId")
        confidence = reference.get("confidenceScore")
        _apply_field_value(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=str(reference["stage"]),
            document_id=UUID(str(reference["documentId"])),
            evidence_id=UUID(str(evidence_id)) if evidence_id else None,
            document_type_key=(
                str(reference["documentTypeKey"])
                if reference.get("documentTypeKey")
                else None
            ),
            field_key=str(reference["fieldKey"]),
            canonical_field_id=str(reference["canonicalFieldId"]),
            source_fact_version=int(reference["sourceFactVersion"]),
            confidence_score=float(confidence) if confidence is not None else None,
            original_value=current["extracted_value"],
            new_value=reference["proposedValue"],
            actor_id=actor_id,
        )
        note_facts_changed(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            reason="FIELD_CORRECTION_APPROVED",
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_tasks
                SET task_status='VERIFIED_COMPLETE',
                    verified_at_utc=now(),
                    completion_result=CAST(:result AS jsonb),
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND task_id=:task_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "task_id": task_id,
                "result": _json({
                    "outcome": "APPROVE_CORRECTION",
                    "verifiedBy": actor_id,
                    "verifiedAt": datetime.now(UTC).isoformat(),
                    "comment": comment,
                }),
            },
        )
        return {
            "taskId": str(task_id),
            "status": "VERIFIED_COMPLETE",
            "outcome": "APPROVE_CORRECTION",
        }

    if task["task_type"] == _REQUESTER_CONFIRMATION_CODE:
        root_id = UUID(str(task["root_task_id"]))
        if action == "ACCEPT":
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_tasks
                    SET task_status='VERIFIED_COMPLETE',
                        verified_at_utc=now(),
                        completion_result=CAST(:result AS jsonb),
                        updated_at_utc=now()
                    WHERE tenant_id=:tenant_id
                      AND task_id IN (:review_task_id, :root_task_id)
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "review_task_id": task_id,
                    "root_task_id": root_id,
                    "result": _json({
                        "outcome": "ACCEPT",
                        "verifiedBy": actor_id,
                        "verifiedAt": datetime.now(UTC).isoformat(),
                        "comment": comment,
                    }),
                },
            )
            return {"taskId": str(task_id), "status": "VERIFIED_COMPLETE", "rootTaskId": str(root_id)}

        if action == "REJECT":
            if not (comment or "").strip():
                raise ValueError("Reject requires a comment")
            root = _task(connection, tenant_id=tenant_id, task_id=root_id)
            next_round = int(root["round_number"] or 1) + 1
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_tasks
                    SET task_status='VERIFIED_COMPLETE', verified_at_utc=now(), updated_at_utc=now()
                    WHERE tenant_id=:tenant_id AND task_id=:review_task_id
                    """
                ),
                {"tenant_id": tenant_id, "review_task_id": task_id},
            )
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_tasks
                    SET task_status='RETURNED',
                        round_number=:round_number,
                        updated_at_utc=now()
                    WHERE tenant_id=:tenant_id AND task_id=:root_task_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "root_task_id": root_id,
                    "round_number": next_round,
                },
            )
            record_task_event(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                task_id=root_id,
                event_type="RETURNED",
                actor_id=actor_id,
                actor_role_code=actor_role_code,
                comment=comment,
                details={"reviewTaskId": str(task_id), "roundNumber": next_round},
            )
            return {"taskId": str(root_id), "status": "RETURNED", "roundNumber": next_round}

    # All other substantive actions represent completion of the requested
    # human action, not final verification of the task.
    if task["completion_protocol"] == "MACHINE_VERIFIED":
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_tasks
                SET task_status='VERIFYING',
                    completion_result=CAST(:result AS jsonb),
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND task_id=:task_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "task_id": task_id,
                "result": _json({
                    "action": action,
                    "actorId": actor_id,
                    "comment": comment,
                    "details": details or {},
                    # Only evaluations that start after this instant may
                    # return the task: results computed earlier are stale.
                    "submittedAt": datetime.now(UTC).isoformat(),
                }),
            },
        )
        _enqueue_verification(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            task_id=task_id,
            reference=dict(task["reference"] or {}),
        )
        return {"taskId": str(task_id), "status": "VERIFYING"}

    connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks
            SET task_status='AWAITING_REQUESTER_REVIEW',
                completion_result=CAST(:result AS jsonb),
                updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND task_id=:task_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": task_id,
            "result": _json({
                "action": action,
                "actorId": actor_id,
                "comment": comment,
                "details": details or {},
            }),
        },
    )
    review_task_id = _create_requester_review(
        connection,
        tenant_id=tenant_id,
        task=task,
        actor_id=actor_id,
        actor_role_code=actor_role_code,
    )
    return {
        "taskId": str(task_id),
        "status": "AWAITING_REQUESTER_REVIEW",
        "reviewTaskId": str(review_task_id),
    }
