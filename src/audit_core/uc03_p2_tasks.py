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


class TaskPermissionError(ValueError):
    """The actor may not take this action on this task (role or assignment)."""


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


def _enable_management_referral(
    connection: Connection, *, tenant_id: str, task: dict[str, Any], actor_id: str, actor_role_code: str,
    comment: str | None, details: dict[str, Any] | None,
) -> dict[str, Any]:
    """The MR task (decision 2026-09-30): a Team Lead completes it with the
    approved amount and the reason, and the journey's Management Referral
    discount is switched on; the task closes as the record of it."""
    from decimal import Decimal, InvalidOperation

    from audit_core.uc03_p2_deal_actions import set_management_referral

    role = str(actor_role_code or "").upper()
    if role not in {"TL", "PM"}:
        raise ValueError("Only a Team Lead can enable the Management Referral discount")
    reference = dict(task["reference"] or {})
    raw = details or {}
    try:
        amount = Decimal(str(raw.get("amount") or reference.get("proposedAmount") or "0"))
    except InvalidOperation as exc:
        raise ValueError("The MR amount must be a number") from exc
    if amount <= 0:
        raise ValueError("Give the approved MR amount")
    reason = str(raw.get("reason") or comment or reference.get("reason") or "").strip()
    if len(reason) < 5:
        raise ValueError("Give a short reason for enabling MR")
    journey_id = UUID(str(task["journey_id"]))
    set_management_referral(
        connection, tenant_id=tenant_id, journey_id=journey_id, opted=True, amount=amount, reason=reason,
        actor_id=actor_id, role=role, correlation_id=None, via=f"TASK:{task['task_id']}",
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks
            SET task_status='VERIFIED_COMPLETE', verified_at_utc=now(),
                completion_result=CAST(:result AS jsonb), updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND task_id=:task_id
            """
        ),
        {"tenant_id": tenant_id, "task_id": task["task_id"],
         "result": _json({"outcome": "MR_ENABLED", "amount": str(amount), "reason": reason, "enabledBy": actor_id,
                          "enabledRole": role, "enabledAt": datetime.now(UTC).isoformat()})},
    )
    record_task_event(
        connection, tenant_id=tenant_id, journey_id=journey_id, task_id=UUID(str(task["task_id"])),
        event_type="COMPLETED", actor_id=actor_id, actor_role_code=role, comment=comment,
        details={"outcome": "MR_ENABLED", "amount": str(amount)},
    )
    return {"taskId": str(task["task_id"]), "status": "VERIFIED_COMPLETE", "outcome": "MR_ENABLED"}


def _vehicle_identity(details: dict[str, Any] | None) -> dict[str, str | None]:
    raw = details or {}
    values: dict[str, str | None] = {}
    for key in ("vin", "chassisNumber", "engineNumber"):
        cleaned = "".join(ch for ch in str(raw.get(key) or "") if ch.isalnum()).upper()
        if cleaned and not 5 <= len(cleaned) <= 25:
            raise ValueError(f"{key} must be 5 to 25 letters or digits")
        values[key] = cleaned or None
    if values["vin"] and len(values["vin"]) != 17:
        raise ValueError("A VIN has 17 characters")
    if not any(values.values()):
        raise ValueError("Enter the VIN, chassis number or engine number")
    return values


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
    # An observation (cash intimated? NDC signed in your presence?) is keyed
    # in by whoever knows: the PC it is assigned to, or a TL / PM.
    observation = task["task_type"] == _OBSERVATION_TASK and actor_role_code in _EXCEPTION_ROLES
    if supervisory:
        if actor_role_code not in _EXCEPTION_ROLES:
            raise TaskPermissionError("Only a Team Lead or Project Manager can accept an exception")
    elif observation:
        pass
    elif assigned_actor_id is not None and str(assigned_actor_id) != actor_id:
        raise TaskPermissionError("This task is assigned to a different actor")
    elif assigned_actor_id is None and assigned_role_code and assigned_role_code != actor_role_code:
        raise TaskPermissionError(
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

    if action in _VERDICT_ACTIONS:
        return _apply_verdict(connection, tenant_id=tenant_id, task=task, action=action, actor_id=actor_id,
                              actor_role_code=actor_role_code, comment=comment, details=details)

    if task["task_type"] == "TL_MANAGEMENT_REFERRAL" and action == "COMPLETE_ACTION":
        return _enable_management_referral(
            connection, tenant_id=tenant_id, task=task, actor_id=actor_id, actor_role_code=actor_role_code,
            comment=comment, details=details,
        )

    if (action == "COMPLETE_ACTION" and str(task.get("dedupe_key") or "").endswith(":INSURANCE_INVOICE_MISSING")
            and str((details or {}).get("answer") or "").upper() == "SELF"):
        # The PC admits the customer arranged their own insurance: the deal
        # drops the premium and the Team Lead gets the flag (its own check).
        from audit_core.uc03_p2_deal_actions import set_insurance_source

        set_insurance_source(
            connection, tenant_id=tenant_id, journey_id=journey_id, source="SELF", actor_id=actor_id,
            reason=(comment or "").strip() or None, correlation_id=None, via="TASK",
        )

    if task["task_type"] == "DELIVERY_REVIEW" and action == "COMPLETE_ACTION":
        # The delivery is reviewed only once every violation has its verdict.
        open_findings = connection.execute(
            text(
                """
                SELECT title FROM auditcore.audit_findings
                WHERE tenant_id=:t AND journey_id=:j AND finding_status IN ('OPEN','ACKNOWLEDGED')
                  AND (finding_class='VIOLATION' OR origin_kind='HUMAN')
                ORDER BY created_at_utc
                """
            ),
            {"t": tenant_id, "j": journey_id},
        ).scalars().all()
        if open_findings:
            raise ValueError(
                f"Give a verdict on {len(open_findings)} open finding(s) first: "
                + "; ".join(str(t) for t in open_findings[:6]) + (" …" if len(open_findings) > 6 else "")
            )

    if action == "PROVIDE_VEHICLE_ID":
        # No pictures of the car: the PC enters the VIN / chassis / engine
        # number instead; the task is then machine-verified like any other
        # and the entry is part of the TL's delivery review.
        entered = _vehicle_identity(details)
        connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_vehicle_identifications (
                    tenant_id, journey_id, vin, chassis_number, engine_number,
                    entered_by_actor_id, entered_by_role, task_id
                ) VALUES (:t, :j, :vin, :chassis, :engine, :actor, :role, :task)
                """
            ),
            {"t": tenant_id, "j": journey_id, "vin": entered["vin"], "chassis": entered["chassisNumber"],
             "engine": entered["engineNumber"], "actor": actor_id, "role": actor_role_code, "task": task_id},
        )
        note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id, reason="VEHICLE_IDENTITY_ENTERED")

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


_OBSERVATION_TASK = "PC_CONFIRMATION"
_VERDICT_ACTIONS = frozenset({"CONFIRM_BREACH", "MARK_FALSE_POSITIVE"})


def _apply_verdict(
    connection: Connection, *, tenant_id: str, task: dict[str, Any], action: str, actor_id: str,
    actor_role_code: str, comment: str | None, details: dict[str, Any] | None,
) -> dict[str, Any]:
    """The Team Lead's verdict on the finding behind this task, through the
    same finding state machine as the Findings screen; the finding closes
    with the verdict and so does every task raised for it."""
    from audit_core.uc03_audit_flags import FlagLifecycleCommand, apply_finding_verdict
    from audit_core.uc03_finding_routing import class_profile, classify_finding
    from audit_core.uc03_p2_task_producer import close_tasks_for_finding

    if actor_role_code not in _EXCEPTION_ROLES:
        raise ValueError("Only a Team Lead or Project Manager can give a verdict on a finding")
    reason = (comment or "").strip()
    if not reason:
        raise ValueError("A verdict needs a short reason")
    if len(reason.split()) > 50:
        raise ValueError("Keep the reason to 50 words or fewer")
    reference = dict(task["reference"] or {})
    if not reference.get("findingId"):
        raise ValueError("This task is not linked to a finding")
    finding_id = UUID(str(reference["findingId"]))
    row = connection.execute(
        text(
            """
            SELECT journey_id, stage_code, finding_status, finding_class, finding_type_code, rule_key, owner_role_code
            FROM auditcore.audit_findings WHERE tenant_id=:t AND audit_finding_id=:f FOR UPDATE
            """
        ),
        {"t": tenant_id, "f": finding_id},
    ).mappings().one_or_none()
    if row is None:
        raise ValueError("The finding behind this task no longer exists")
    if row["finding_status"] in {"OPEN", "ACKNOWLEDGED"}:
        finding_class = row["finding_class"] or classify_finding(row["rule_key"], row["finding_type_code"])
        category = str((details or {}).get("rejectionCategory") or "OTHER").upper()
        apply_finding_verdict(
            connection, tenant_id=tenant_id, flag_id=finding_id, journey_id=UUID(str(row["journey_id"])),
            daily_ops_run_id=None, process_area=str(row["stage_code"]), finding_class=finding_class,
            resolution_mode=class_profile(finding_class).resolution_mode, current_status=str(row["finding_status"]),
            current_owner_role=row["owner_role_code"],
            payload=FlagLifecycleCommand(
                action=action, remarks=reason, resolutionReason=reason,
                rejectionCategory=category if action == "MARK_FALSE_POSITIVE" else None,
            ),
            operating_role=actor_role_code, actor_id=actor_id, correlation_id=None,
        )
    verdict = "CONFIRMED_BREACH" if action == "CONFIRM_BREACH" else "FALSE_POSITIVE"
    close_tasks_for_finding(connection, tenant_id=tenant_id, finding_id=finding_id, verdict=verdict,
                            actor_id=actor_id, actor_role=actor_role_code, comment=reason)
    return {"taskId": str(task["task_id"]), "status": "VERIFIED_COMPLETE", "outcome": verdict,
            "findingId": str(finding_id)}
_MANUAL_VERIFICATION_TASKS = frozenset({
    "MANUAL_VERIFICATION_REVIEW", "FIELD_CORRECTION_REVIEW", "FIELD_CORRECTION_REVIEW_P2", "PC_CORRECTION",
    "DELIVERY_VIN_MANUAL_ENTRY_REVIEW", "MODEL_SELECTION_CORRECTION_REVIEW", "PC_CONFIRMATION",
    "TL_MANUAL_VERIFICATION",
})
_DOCUMENT_TASKS = frozenset({
    "PC_DOCUMENT_REUPLOAD", "DOCUMENT_REMEDIATION", "WRONG_DOCUMENT_REVIEW", "WRONG_DOCUMENT_DEALER_NOTICE",
    "PC_VERIFY_UNRECOGNIZED_DOCUMENT", "PC_RESOLVE_DOCUMENT_PROCESSING_FAILURE", "PC_UPLOAD_STATUS",
    "PC_BOOKING_DATE_MISSING", "TL_DOCUMENT_STUCK",
    "DUPLICATE_RECEIPT_NOTICE",
    "DELIVERY_VEHICLE_PHOTOS_MISSING", "DOCUMENT_MISSING",
    "TL_DOCUMENT_UPLOAD",
})
_DOCUMENT_CATEGORIES = frozenset({"DOCUMENT_REUPLOAD", "DOCUMENT_EXCEPTION", "EVIDENCE_GAP", "DOCUMENT_VERIFICATION"})


def task_queue_tab(task_type: str, category: str) -> str:
    """Which Task Queue tab a task belongs to: MANUAL_VERIFICATION (values to
    confirm or corrections to approve), DOCUMENTS (something to upload,
    re-upload or re-type) or OTHER (checks and findings; listed under All)."""
    if task_type in _MANUAL_VERIFICATION_TASKS or category == "CORRECTION_APPROVAL":
        return "MANUAL_VERIFICATION"
    if task_type in _DOCUMENT_TASKS or category in _DOCUMENT_CATEGORIES:
        return "DOCUMENTS"
    return "OTHER"
