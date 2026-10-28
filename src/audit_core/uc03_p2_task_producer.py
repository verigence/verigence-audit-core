"""Machine-raised Phase 2 tasks: produced, refreshed, verified and reopened
from the control ledger and from field-review state.

Every task is self-contained (what is wrong, where, with the values, what to
do) and deduplicated per logical issue:

    control:<journey>:<control code>        one task per failing control
    field-review:<journey>:<document id>    one task per document with
                                            unreviewed low-confidence fields

Completion is machine-verified (blueprint 12.1): a task closes only when the
condition that raised it no longer holds. A task under verification is
returned to the assignee only by an evaluation that started after the
assignee acted. A closed task reopens if its issue comes back, unless a TL
accepted it as an exception for exactly the same values.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_p2_document_health import TL_AFTER_SECONDS
from audit_core.uc03_p2_names import (
    display_name,
    is_masked,
    masked_matches,
    same_organisation,
    same_person,
)
from audit_core.uc03_p2_registry import ControlTemplate, Registry, get_registry
from audit_core.uc03_p2_runtime import (
    LARGE_UPLOAD_PAGES,
    QUALITY_CLOSE_INSTRUCTION,
    dayend_removal_notice,
    swept_pending_sql,
)
from audit_core.uc03_p2_stage import unreviewed_fields
from audit_core.uc03_p2_tasks import record_task_event

_OPEN = ("READY", "IN_PROGRESS", "RETURNED", "ACTION_COMPLETED", "VERIFYING")
_PRIORITY_BY_SEVERITY = {"CRITICAL": "URGENT", "HIGH": "HIGH", "MEDIUM": "NORMAL", "LOW": "LOW", "INFO": "LOW"}
_SLA_HOURS = {"URGENT": 4, "HIGH": 24, "NORMAL": 48, "LOW": 72}
_RULE_ENGINE_SEVERITY = {"CRITICAL": "CRITICAL", "HIGH": "HIGH", "MAJOR": "HIGH", "MEDIUM": "MEDIUM",
                         "MINOR": "LOW", "LOW": "LOW", "INFO": "INFO"}
_MONEY_HINTS = ("amount", "price", "total", "cost", "premium", "value", "paid", "discount", "credited", "debited")


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def _label(key: str) -> str:
    text_ = key.replace("_", " ").strip()
    return text_[:1].upper() + text_[1:]


def format_value(field_key: str, value: Any) -> str:
    if value is None or value == "":
        return "blank"
    if any(hint in field_key.lower() for hint in _MONEY_HINTS):
        try:
            amount = Decimal(str(value).replace(",", "").replace("₹", "").strip())
        except (InvalidOperation, ValueError):
            return str(value)
        whole, _, fraction = f"{amount:.2f}".partition(".")
        sign = "-" if whole.startswith("-") else ""
        digits = whole.lstrip("-")
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        indian = ",".join([*groups, tail]) if groups else tail
        return f"₹{sign}{indian}" + ("" if fraction == "00" else f".{fraction}")
    return str(value)


def _operand(registry: Registry, operand: str | None) -> tuple[str, str] | None:
    if not operand or operand == "-" or "." not in operand:
        return None
    document, field_key = operand.split(".", 1)
    if document.startswith("_"):
        label = {"_reconciliation": "Standard / master", "_derived": "Calculated"}.get(document, "Reference")
        return label, _label(field_key.split(":")[-2] if ":" in field_key else field_key)
    key = "booking_docket" if document in {"booking_form", "booking_docket"} else document
    template = registry.documents.get(key)
    return (template.display_name if template else _label(document)), _label(field_key)


def _document_ids(connection: Connection, registry: Registry, *, tenant_id: str, journey_id: UUID,
                  template_keys: list[str]) -> list[str]:
    types: list[str] = []
    for key in template_keys:
        template = registry.documents.get(key)
        if template:
            types.extend([template.key, *template.di_types])
    if not types:
        return []
    return [
        str(value) for value in connection.execute(
            text(
                """
                SELECT di_document_id FROM auditcore.evidence
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND association_status='ACTIVE' AND document_type_key = ANY(:types)
                ORDER BY linked_at_utc
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "types": types},
        ).scalars().all()
    ]


def describe_failure(
    connection: Connection,
    registry: Registry,
    *,
    tenant_id: str,
    journey_id: UUID,
    control: ControlTemplate,
    reason: str | None,
    details: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """(title, description, reference) a PC can act on without knowing rule codes."""
    reference: dict[str, Any] = {
        "generatedBy": "SYSTEM",
        "sourceType": "RULE",
        "sourceCode": control.code,
        "controlMode": control.mode,
        "category": control.category,
    }
    if control.mode == "EXTERNAL":
        left = _operand(registry, (control.operands or {}).get("left"))
        right = _operand(registry, (control.operands or {}).get("right"))
        left_field = str((control.operands or {}).get("left") or "").split(".")[-1]
        right_field = str((control.operands or {}).get("right") or "").split(".")[-1]
        reference.update({
            "operands": control.operands,
            "leftValue": details.get("leftValue"),
            "rightValue": details.get("rightValue"),
            "documentTypes": list(control.depends_on_documents),
            "documentIds": _document_ids(connection, registry, tenant_id=tenant_id, journey_id=journey_id,
                                         template_keys=list(control.depends_on_documents)),
            "fieldKeys": [f for f in (left_field, right_field) if f],
        })
        if left and right:
            title = f"Review {left[1].lower()}: {left[0]} vs {right[0]}"
            description = (
                f"{left[0]} shows {left[1]} of {format_value(left_field, details.get('leftValue'))}, "
                f"while {right[0]} shows {right[1]} of {format_value(right_field, details.get('rightValue'))}. "
                "Check both documents: correct the extraction if a value was misread, "
                "or add evidence explaining the difference."
            )
        elif left:
            title = f"Review {left[1].lower()} on {left[0]}"
            description = (
                f"{left[0]} shows {left[1]} of {format_value(left_field, details.get('leftValue'))}. "
                f"{reason or 'This does not meet the audit rule.'}"
            )
        else:
            title = _label(control.code.lower())
            description = reason or "The audit rule found a problem. Review the related documents."
        return title, description, reference

    if control.mode == "GATE":
        gates = details.get("gates") or {}
        payment = gates.get("MINIMUM_BOOKING_PAYMENT")
        if payment:
            reference["documentTypes"] = ["dealer_receipt"]
            return (
                "Collect the remaining booking amount",
                (
                    f"Receipts total {format_value('amount', payment.get('receiptTotal'))} against the minimum "
                    f"booking amount of {format_value('amount', payment.get('minimumAmount'))} "
                    f"({format_value('amount', payment.get('shortfall'))} short). Upload the remaining "
                    "receipt(s), or correct a receipt amount that was misread."
                ),
                reference,
            )
        return (_label(control.code.lower()), reason or "A required checkpoint is not met.", reference)

    if control.mode == "PAGES":
        return (
            "Fix pages that could not be processed",
            (
                f"{details.get('pageCount', 'Some')} page(s) could not be processed by document intelligence. "
                "Retry them, or re-upload a clearer scan."
            ),
            reference,
        )

    # Native / sync / event findings carry their own business wording.
    reference["findingTitle"] = details.get("findingTitle")
    # A question for the PC (its answers), and what the check looked at.
    for key in ("question", "answers", "paymentIds", "documentIds", "components", "discounts",
                "uploadFirst", "documentTypes"):
        if details.get(key):
            reference[key] = details[key]
    title = str(details.get("findingTitle") or _label(control.code.lower()))[:300]
    parts = [str(p) for p in (details.get("findingDescription"), reason) if p]
    expected, observed = details.get("expected"), details.get("observed")
    if expected or observed:
        parts.append(f"Expected: {expected or '-'}. Found: {observed or '-'}.")
    description = " ".join(dict.fromkeys(parts)) or "The audit check failed. Review the related documents."
    return title, description, reference


def _issue_hash(reference: dict[str, Any]) -> str:
    material = {k: reference.get(k) for k in ("sourceCode", "leftValue", "rightValue", "fields", "findingTitle")}
    return hashlib.sha256(_json(material).encode()).hexdigest()[:24]


def _existing(connection: Connection, *, tenant_id: str, dedupe_key: str) -> dict[str, Any] | None:
    row = connection.execute(
        text("SELECT * FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k FOR UPDATE"),
        {"t": tenant_id, "k": dedupe_key},
    ).mappings().one_or_none()
    return dict(row) if row else None


def raise_or_refresh(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    dedupe_key: str,
    task_type: str,
    source_type: str,
    source_code: str,
    title: str,
    description: str,
    reference: dict[str, Any],
    severity: str,
    registry: Registry,
    evaluation_started_at: datetime | None = None,
) -> tuple[UUID, str]:
    """Create, refresh, return or reopen the one task for a logical issue.
    Returns (task_id, what happened)."""
    template = registry.tasks[task_type]
    priority = _PRIORITY_BY_SEVERITY.get(severity, "NORMAL")
    reference = {**reference, "issueHash": _issue_hash(reference)}
    existing = _existing(connection, tenant_id=tenant_id, dedupe_key=dedupe_key)
    now = datetime.now(UTC)

    if existing is None:
        task_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_tasks (
                    tenant_id, journey_id, task_type, category, origin_kind, source_type, source_code,
                    dedupe_key, title, description, reference, severity, priority,
                    assigned_role_code, allowed_actions, completion_protocol, due_at_utc
                ) VALUES (
                    :t, :j, :type, :category, 'SYSTEM', :source_type, :source_code,
                    :key, :title, :description, CAST(:reference AS jsonb), :severity, :priority,
                    :owner, CAST(:actions AS jsonb), 'MACHINE_VERIFIED', :due
                ) RETURNING task_id
                """
            ),
            {
                "t": tenant_id, "j": journey_id, "type": task_type, "category": template.category,
                "source_type": source_type, "source_code": source_code, "key": dedupe_key,
                "title": title[:300], "description": description, "reference": _json(reference),
                "severity": severity, "priority": priority, "owner": template.owner,
                "actions": _json(list(template.actions)), "due": now + timedelta(hours=_SLA_HOURS[priority]),
            },
        ).scalar_one()
        task_id = UUID(str(task_id))
        connection.execute(
            text("UPDATE auditcore.p2_tasks SET root_task_id=task_id WHERE tenant_id=:t AND task_id=:id"),
            {"t": tenant_id, "id": task_id},
        )
        record_task_event(connection, tenant_id=tenant_id, journey_id=journey_id, task_id=task_id,
                          event_type="RAISED", actor_id="SYSTEM", actor_role_code="SYSTEM",
                          details={"reason": description})
        return task_id, "RAISED"

    task_id = UUID(str(existing["task_id"]))
    status = str(existing["task_status"])
    result = dict(existing["completion_result"] or {})
    new_status, outcome = status, "UNCHANGED"
    if status == "VERIFIED_COMPLETE":
        if result.get("acceptedIssueHash") == reference["issueHash"]:
            return task_id, "ACCEPTED_EXCEPTION"
        if result.get("verdict"):
            # A Team Lead's verdict on this check's finding stands for the
            # Journey; the finding is reopened from Findings if ever needed.
            return task_id, "ACCEPTED_EXCEPTION"
        new_status, outcome = "READY", "REOPENED"
    elif status == "VERIFYING":
        submitted = result.get("submittedAt")
        submitted_at = datetime.fromisoformat(submitted) if submitted else None
        if evaluation_started_at is not None and (submitted_at is None or evaluation_started_at > submitted_at):
            new_status, outcome = "RETURNED", "RETURNED"
    elif status == "CANCELLED":
        return task_id, "CANCELLED"

    content_changed = (
        existing["title"] != title[:300]
        or existing["description"] != description
        or (existing["reference"] or {}).get("issueHash") != reference["issueHash"]
    )
    if outcome == "UNCHANGED" and not content_changed:
        return task_id, "UNCHANGED"
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks
            SET title=:title, description=:description, reference=CAST(:reference AS jsonb),
                severity=:severity, priority=:priority, task_status=:status,
                round_number=round_number + CASE WHEN :reopened THEN 1 ELSE 0 END,
                due_at_utc=CASE WHEN :reopened THEN :due ELSE due_at_utc END,
                verified_at_utc=CASE WHEN :reopened THEN NULL ELSE verified_at_utc END,
                updated_at_utc=now()
            WHERE tenant_id=:t AND task_id=:id
            """
        ),
        {
            "t": tenant_id, "id": task_id, "title": title[:300], "description": description,
            "reference": _json(reference), "severity": severity, "priority": priority,
            "status": new_status, "reopened": outcome == "REOPENED",
            "due": now + timedelta(hours=_SLA_HOURS[priority]),
        },
    )
    record_task_event(
        connection, tenant_id=tenant_id, journey_id=journey_id, task_id=task_id,
        event_type=("MACHINE_VERIFICATION_FAIL" if outcome == "RETURNED" else outcome if outcome != "UNCHANGED"
                    else "UPDATED"),
        actor_id="SYSTEM", actor_role_code="SYSTEM", details={"reason": description},
    )
    return task_id, outcome if outcome != "UNCHANGED" else "UPDATED"


def close_tasks_for_finding(connection: Connection, *, tenant_id: str, finding_id: UUID, verdict: str,
                            actor_id: str | None, actor_role: str | None, comment: str | None) -> int:
    """A Team Lead's verdict on a finding (confirmed breach, false positive,
    resolved) closes every open task raised for it; the verdict is kept on
    the task so the check does not raise it again."""
    rows = connection.execute(
        text(
            """
            SELECT task_id, journey_id, reference FROM auditcore.p2_tasks
            WHERE tenant_id=:t AND reference->>'findingId'=:f AND task_status = ANY(:open)
            FOR UPDATE
            """
        ),
        {"t": tenant_id, "f": str(finding_id), "open": list(_OPEN)},
    ).mappings().all()
    now = datetime.now(UTC).isoformat()
    for row in rows:
        reference = dict(row["reference"] or {})
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_tasks
                SET task_status='VERIFIED_COMPLETE', verified_at_utc=now(),
                    completion_result=COALESCE(completion_result, '{}'::jsonb) || CAST(:result AS jsonb),
                    updated_at_utc=now()
                WHERE tenant_id=:t AND task_id=:id
                """
            ),
            {"t": tenant_id, "id": row["task_id"], "result": _json({
                "outcome": verdict, "verdict": verdict, "acceptedIssueHash": reference.get("issueHash"),
                "verdictBy": actor_id, "verdictRole": actor_role, "verdictAt": now, "comment": comment,
            })},
        )
        record_task_event(connection, tenant_id=tenant_id, journey_id=UUID(str(row["journey_id"])),
                          task_id=UUID(str(row["task_id"])), event_type="VERDICT", actor_id=actor_id or "SYSTEM",
                          actor_role_code=actor_role or "SYSTEM", comment=comment,
                          details={"verdict": verdict, "findingId": str(finding_id)})
    return len(rows)


def resolve_if_open(connection: Connection, *, tenant_id: str, journey_id: UUID, dedupe_key: str,
                    evidence: dict[str, Any]) -> UUID | None:
    existing = _existing(connection, tenant_id=tenant_id, dedupe_key=dedupe_key)
    if existing is None or str(existing["task_status"]) not in _OPEN:
        return None
    task_id = UUID(str(existing["task_id"]))
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks
            SET task_status='VERIFIED_COMPLETE', verified_at_utc=now(),
                completion_result=completion_result || CAST(:result AS jsonb), updated_at_utc=now()
            WHERE tenant_id=:t AND task_id=:id
            """
        ),
        {"t": tenant_id, "id": task_id, "result": _json({"machineVerification": "PASS", **evidence})},
    )
    record_task_event(connection, tenant_id=tenant_id, journey_id=journey_id, task_id=task_id,
                      event_type="MACHINE_VERIFICATION_PASS", actor_id="SYSTEM", actor_role_code="SYSTEM",
                      details=evidence)
    return task_id


# Task types that ask the PC a question rather than for a fix: asked only once
# delivery has started (2026-10-28).
QUESTION_TASK_TYPES = frozenset({"PC_CONFIRMATION"})


def apply_control_transitions(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    transitions: list,
    evaluation_started_at: datetime | None,
    registry: Registry | None = None,
) -> dict[str, int]:
    registry = registry or get_registry()
    counts: dict[str, int] = {}
    delivery_started: bool | None = None
    for transition in transitions:
        control = registry.controls.get(transition.control_code)
        if control is None:
            continue
        key = f"control:{journey_id}:{control.code}"
        if transition.current == "FAIL" and control.task_type in QUESTION_TASK_TYPES:
            # The PC's questions (cash intimated? accessories fitted? who paid?)
            # are asked once delivery has started (product owner 2026-10-28:
            # fewer tasks); until then the check stays unanswered on the ledger
            # and an open question closes, to come back at delivery.
            if delivery_started is None:
                from audit_core.uc03_p2_stage import delivery_has_started

                delivery_started = delivery_has_started(connection, tenant_id=tenant_id, journey_id=journey_id)
            if not delivery_started:
                outcome = "VERIFIED" if resolve_if_open(
                    connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                    evidence={"controlCode": control.code, "deferredUntil": "DELIVERY_STARTED",
                              "verifiedAt": datetime.now(UTC).isoformat()},
                ) else "DEFERRED"
                counts[outcome] = counts.get(outcome, 0) + 1
                continue
        if transition.current == "FAIL":
            reason = connection.execute(
                text("SELECT status_reason FROM auditcore.p2_control_state "
                     "WHERE tenant_id=:t AND journey_id=:j AND control_code=:c"),
                {"t": tenant_id, "j": journey_id, "c": control.code},
            ).scalar_one_or_none()
            title, description, reference = describe_failure(
                connection, registry, tenant_id=tenant_id, journey_id=journey_id,
                control=control, reason=reason, details=transition.details,
            )
            if transition.finding_id:
                reference["findingId"] = str(transition.finding_id)
            reference["stage"] = transition.stage
            severity = str(
                control.severity
                or _RULE_ENGINE_SEVERITY.get(str(transition.details.get("ruleEngineSeverity") or "").upper())
                or "MEDIUM"
            )
            _, outcome = raise_or_refresh(
                connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                task_type=control.task_type, source_type="RULE", source_code=control.code,
                title=title, description=description, reference=reference, severity=severity,
                registry=registry, evaluation_started_at=evaluation_started_at,
            )
        elif transition.current in {"PASS", "NOT_APPLICABLE"}:
            outcome = "VERIFIED" if resolve_if_open(
                connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                evidence={"controlCode": control.code, "controlStatus": transition.current,
                          "verifiedAt": datetime.now(UTC).isoformat()},
            ) else "NONE"
        else:
            continue  # waiting / technical states keep verification pending
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def _field_with_confidence(field: dict[str, Any]) -> str:
    confidence = field["confidence"]
    shown = "no confidence" if confidence is None else f"{confidence:.0f}%"
    return f"{_label(field['fieldKey'])} ({shown})"


def _date_problem(field: dict[str, Any], floor: date | None) -> str:
    """"Receipt date read as 12/03/2019, before July 2026": what the PC
    checks on the page and why it is almost certainly misread."""
    shown = format_value(field["fieldKey"], field.get("value"))
    if "DATE_UNREADABLE" in field.get("reasons", ()):
        return f"{_label(field['fieldKey'])} read as {shown!r}, which is not a date"
    if "DATE_IN_FUTURE" in field.get("reasons", ()):
        return f"{_label(field['fieldKey'])} read as {shown}, which is after today"
    when = f", before {floor.strftime('%B %Y')}" if floor else ""
    return f"{_label(field['fieldKey'])} read as {shown}{when}"


def sync_field_review_tasks(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    registry: Registry | None = None,
    evaluation_started_at: datetime | None = None,
) -> dict[str, int]:
    """One MANUAL_VERIFICATION_REVIEW task per document with unreviewed
    low-confidence fields; tasks close themselves when none remain."""
    registry = registry or get_registry()
    pending = unreviewed_fields(connection, registry, tenant_id=tenant_id, journey_id=journey_id)
    by_document: dict[str, list[dict[str, Any]]] = {}
    for item in pending:
        by_document.setdefault(item["documentId"], []).append(item)
    counts: dict[str, int] = {}
    floor = registry.extraction_rules.date_floor
    for document_id, fields in by_document.items():
        name = fields[0]["documentName"]
        # Misread dates first (they are wrong, not merely uncertain), then the
        # least certain reading first.
        fields.sort(key=lambda f: (f["severity"] != "HIGH", f["confidence"] is not None, f["confidence"] or 0))
        dated = [f for f in fields if f["severity"] == "HIGH"]
        uncertain = [f for f in fields if f["severity"] != "HIGH"]
        sentences = []
        if dated:
            sentences.append(
                f"These dates on the {name} cannot be right as read: "
                + "; ".join(_date_problem(f, floor) for f in dated[:4])
                + (f" and {len(dated) - 4} more" if len(dated) > 4 else "") + "."
            )
        if uncertain:
            listed = ", ".join(_field_with_confidence(f) for f in uncertain[:6])
            more = f" and {len(uncertain) - 6} more" if len(uncertain) > 6 else ""
            sentences.append(f"These values on the {name} were read with low confidence: {listed}{more}.")
        sentences.append("Open the document, compare each value with the page and confirm or correct it.")
        severity = "HIGH" if dated else "MEDIUM"
        count = len(fields)
        title = (
            f"Check {count} date{'s' if count != 1 else ''} on {name}" if dated and not uncertain
            else f"Verify {count} field{'s' if count != 1 else ''} on {name}"
        )
        reference = {
            "generatedBy": "SYSTEM", "sourceType": "DOCUMENT_FIELD", "sourceCode": "MANUAL_VERIFICATION",
            "documentId": document_id, "documentIds": [document_id], "templateKey": fields[0]["templateKey"],
            "fields": [{"fieldKey": f["fieldKey"], "canonicalFieldId": f["canonicalFieldId"],
                        "sourceFactVersion": f["sourceFactVersion"], "confidence": f["confidence"],
                        "threshold": f["threshold"], "reasons": f["reasons"],
                        "value": f.get("value") if f["severity"] == "HIGH" else None} for f in fields],
            "fieldKeys": [f["fieldKey"] for f in fields],
            "dateFloor": floor.isoformat() if floor and dated else None,
        }
        _, outcome = raise_or_refresh(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            dedupe_key=f"field-review:{journey_id}:{document_id}",
            task_type="MANUAL_VERIFICATION_REVIEW", source_type="DOCUMENT_FIELD",
            source_code="MANUAL_VERIFICATION",
            title=title, description=" ".join(sentences),
            reference=reference, severity=severity, registry=registry,
            evaluation_started_at=evaluation_started_at,
        )
        counts[outcome] = counts.get(outcome, 0) + 1
    open_keys = connection.execute(
        text(
            """
            SELECT dedupe_key FROM auditcore.p2_tasks
            WHERE tenant_id=:t AND journey_id=:j AND task_type='MANUAL_VERIFICATION_REVIEW'
              AND dedupe_key LIKE 'field-review:%' AND task_status = ANY(:open)
            """
        ),
        {"t": tenant_id, "j": journey_id, "open": list(_OPEN)},
    ).scalars().all()
    for key in open_keys:
        if key.rsplit(":", 1)[-1] not in by_document:
            resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                            evidence={"allFieldsReviewed": True, "verifiedAt": datetime.now(UTC).isoformat()})
            counts["VERIFIED"] = counts.get("VERIFIED", 0) + 1
    return counts


def _stage_rows(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[tuple[str, str], dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT stage_code, gate_key, gate_status, details FROM auditcore.p2_stage_gate_state
            WHERE tenant_id=:t AND journey_id=:j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    return {(str(r["stage_code"]), str(r["gate_key"])): {**dict(r["details"] or {}), "passed": r["gate_status"] == "PASS"}
            for r in rows}


def _resolve_prefix_except(connection: Connection, *, tenant_id: str, journey_id: UUID, prefix: str,
                           keep: set[str], evidence: dict[str, Any] | Callable[[str], dict[str, Any]]) -> int:
    open_keys = connection.execute(
        text(
            """
            SELECT dedupe_key FROM auditcore.p2_tasks
            WHERE tenant_id=:t AND journey_id=:j AND dedupe_key LIKE :prefix AND task_status = ANY(:open)
            """
        ),
        {"t": tenant_id, "j": journey_id, "prefix": f"{prefix}%", "open": list(_OPEN)},
    ).scalars().all()
    closed = 0
    for key in open_keys:
        if key not in keep:
            resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                            evidence=evidence(key) if callable(evidence) else evidence)
            closed += 1
    return closed


# A file still being worked on this long after upload gets an Info status
# task. Since 2026-10-21 no page takes more than 30 minutes (the worker fails
# it: uc03_p2_worker.fail_unsettled_pages), so this is a safety net for a
# file whose read pages are still being copied, or a worker that was down.
_UPLOAD_STATUS_AFTER_SECONDS = int(os.environ.get("P2_UPLOAD_STATUS_AFTER_SECONDS", str(60 * 60)))
_SETTLED_PAGE_STATES = ("READY", "SUPPORTING", "NEEDS_REVIEW", "CANCELLED", "MERGED")
_FAILED_PAGE_STATES = ("FAILED", "DEAD_LETTER")


def _pages_text(pages: list[int]) -> str:
    if len(pages) == 1:
        return f"page {pages[0]}"
    return "pages " + ", ".join(str(p) for p in pages[:-1]) + f" and {pages[-1]}"


_REASON_WORDS = {"faded": "too faded", "blurred": "blurred", "tooSmall": "too small",
                 "unreadable": "unreadable", "other": "not clear enough"}


def resubmit_page_reason(code: str | None, status_reason: str | None) -> str:
    """Why a failed page is to be resubmitted, in plain words, for the
    file's "Document to resubmit" task. A page the scan-quality check
    rejected says so and why (product owner 2026-10-20: the task must say
    the document was rejected, not leave the PC to find out on the card);
    a page our setup refused says there is no need to re-upload; any other
    page carries the reason its card shows."""
    from audit_core.uc03_p2_daily_report import rejection_reason

    if str(code or "") == SETUP_FAILURE_CODE:
        return SETUP_PAGE_TEXT
    rejected = rejection_reason(code)
    if rejected is not None:
        return (f"Rejected by the scan-quality check: {_REASON_WORDS.get(rejected, 'not clear enough')}. Scan it "
                "again in good light, flat and in focus, and upload it again.")
    return (status_reason or "").strip() or "It could not be read. Upload it again."


# Product owner 2026-10-21: every FAILED page raises a "Document to resubmit"
# task (category key DOCUMENT_RESUBMIT), one per uploaded file, listing its
# failed pages, each with the reason in plain words and a link that opens the
# page from storage (a short-lived link issued when it is opened, never a raw
# bucket address).
RESUBMIT_TASK_TYPE = "DOCUMENT_RESUBMIT"
RESUBMIT_CATEGORY = "DOCUMENT_RESUBMIT"
# Gemini refused our key or model: our setup, not the page (2026-10-21).
# Since 2026-10-22 nothing re-reads such a page at night, so it is failed and
# resubmitted like any other; the text still says whose problem it is.
SETUP_FAILURE_CODE = "DOCUMENT_AI_NOT_CONFIGURED"
SETUP_PAGE_TEXT = "Our setup problem, not a problem with your file. Please upload this page again."
# Why a file failed as a whole under the 30-minute ceiling
# (uc03_p2_worker.fail_unsettled_pages); stored on the upload batch.
FILE_NOT_UPLOADED_IN_TIME_REASON = ("The file did not finish uploading within 30 minutes. "
                                    "Please upload the file again.")
FILE_NOT_SPLIT_IN_TIME_REASON = ("The file was not split into pages within 30 minutes. "
                                 "Please upload the file again.")


def page_open_path(tenant_id: str, journey_id: UUID, queue_id: UUID | str) -> str:
    """The app endpoint that issues a short-lived link to a page's file
    (uc03_p2_api.get_page_file): stored in the resubmit task as filePath,
    since a storage link itself would expire."""
    return f"/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/pages/{queue_id}/file"



def upload_open_path(tenant_id: str, journey_id: UUID, batch_id: UUID | str) -> str:
    """The app endpoint that issues a short-lived link to the file as the PC
    uploaded it (uc03_p2_api.get_upload_file): the Daily Processing report
    carries it as filePath, since a storage link itself would expire."""
    return f"/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/uploads/{batch_id}/file"


# Why the quality task closed by itself. There is no time rule: an upload of
# some other file never answers a refused page (review of #479, 2026-10-21).
# The PC closes the task by hand, with a comment, every time (2026-10-22;
# CLOSE_TASK in uc03_p2_tasks).
_QUALITY_CLOSED_EVIDENCE = {
    "reason": "No page of this file is refused any more: the refused pages were replaced by the same scan, "
              "read, or removed, or the file was deleted.",
}


def _removal_evidence(removal: dict[str, Any]) -> dict[str, Any]:
    """Closing evidence naming a page a person removed from the Journey."""
    pages = [int(p) for p in removal.get("pages") or []]
    where = (_pages_text(pages).capitalize() if pages else "A page") + f" of {removal.get('filename') or 'the file'}"
    who = str(removal.get("removedBy") or "someone") + (f" ({removal['removedRole']})" if removal.get("removedRole") else "")
    return {
        "reason": f"{where} was removed from the journey by {who}: {removal.get('reason') or 'no reason given'}",
        "removedPage": {k: removal.get(k) for k in ("queueId", "batchId", "pages", "removedBy", "removedRole", "reason")},
        "verifiedAt": datetime.now(UTC).isoformat(),
    }


def _sync_quality_task(
    connection: Connection, *, tenant_id: str, journey_id: UUID, batch_id: Any, filename: str,
    quality: list[dict[str, Any]], registry: Registry, evaluation_started_at: datetime | None,
) -> str:
    """One PC task per uploaded file with pages refused by the scan-quality
    check (product owner 2026-10-21: blur, low contrast, too small and every
    other DI_QUALITY_* refusal are document tasks of their own, "Image
    quality"). It names the pages and why, and asks for a re-scan (a retry
    cannot help). It stays open, and blocks the delivery gate, for as long
    as any refused page of the file is still refused -- exactly as the
    file's status task did for those pages before -- and closes itself once
    they are replaced by the same scan, read or removed, or the file is
    deleted. The day-end clean-up never closes it. Another file uploaded on
    the booking never closes it either: nothing says which document a
    refused page was (it was refused before it was classified), so the PC
    closes the task by hand with a comment (2026-10-22). A task closed that
    way is not reopened by this sync (its ``acceptedIssueHash`` is kept).
    Returns what happened to the task."""
    from audit_core.uc03_p2_daily_report import rejection_reason
    from audit_core.uc03_p2_document_task_groups import rejection_words

    by_reason: dict[str, list[int]] = {}
    codes: dict[str, list[str]] = {}
    # Each refused page's unit, so the task can offer "Remove this page".
    page_queues: dict[int, str] = {}
    for page in quality:
        if page.get("queueId"):
            page_queues.setdefault(int(page["page"]), str(page["queueId"]))
        reason = rejection_reason(page.get("code")) or "other"
        number = int(page["page"])
        if number not in by_reason.setdefault(reason, []):
            by_reason[reason].append(number)
        code = str(page.get("code") or "")
        if code not in codes.setdefault(reason, []):
            codes[reason].append(code)
    pages = sorted({p for group in by_reason.values() for p in group})
    why = "; ".join(f"{_pages_text(sorted(group)).capitalize()} {'is' if len(group) == 1 else 'are'} "
                    f"{rejection_words(reason)}" for reason, group in by_reason.items())
    count = len(pages)
    title = f"{filename}: {count} page{'' if count == 1 else 's'} refused for image quality"
    description = (f"Refused by the scan-quality check and not read: {why}. A retry will not help. "
                   f"{dayend_removal_notice(count)} Scan "
                   f"{'it' if count == 1 else 'them'} again in good light, flat and in focus, and upload from "
                   f"Upload / Edit Documents. {QUALITY_CLOSE_INSTRUCTION} This task also closes itself once "
                   "the refused pages are processed or removed (the day-end clean-up only takes them off the "
                   "journey; it does not close this task). Delete this booking if nothing on it can be used.")
    _, outcome = raise_or_refresh(
        connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=f"upload-quality:{journey_id}:{batch_id}",
        task_type="PC_PAGE_QUALITY_REJECTED", source_type="DOCUMENT", source_code="DOCUMENT_QUALITY_REJECTED",
        title=title, description=description,
        reference={"generatedBy": "SYSTEM", "sourceType": "DOCUMENT", "sourceCode": "DOCUMENT_QUALITY_REJECTED",
                   "batchId": str(batch_id), "filename": filename, "pageNumbers": pages,
                   "rejections": [{"reason": reason, "pages": sorted(group), "codes": codes[reason]}
                                  for reason, group in by_reason.items()],
                   "pageQueues": [{"page": page, "queueId": queue} for page, queue in sorted(page_queues.items())]},
        severity="HIGH", registry=registry, evaluation_started_at=evaluation_started_at,
    )
    # A task raised before "Close task" existed has not got the
    # action: give it to the ones still open (this task type only).
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks SET allowed_actions=CAST(:actions AS jsonb), updated_at_utc=now()
            WHERE tenant_id=:t AND dedupe_key=:k
              AND task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')
              AND allowed_actions IS DISTINCT FROM CAST(:actions AS jsonb)
            """
        ),
        {"t": tenant_id, "k": f"upload-quality:{journey_id}:{batch_id}",
         "actions": _json(list(registry.tasks["PC_PAGE_QUALITY_REJECTED"].actions))},
    )
    return outcome


def sync_processing_failure_tasks(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    registry: Registry | None = None,
    evaluation_started_at: datetime | None = None,
    now: datetime | None = None,
    removal: dict[str, Any] | None = None,
) -> dict[str, int]:
    """One task (PC) per uploaded file that is not done, never one per page.

    A file with failed pages (or one that could not be split) raises the
    PC's "Document to resubmit" task (DOCUMENT_RESUBMIT, product owner
    2026-10-21): each failed page, its reason and a link to open it. A page
    that failed only because of our setup (Gemini refused our key or model)
    says so -- no need to re-upload -- and the Team Lead is alerted. A file
    still being worked on an hour after upload gets the Info status task
    (rare now that no file takes more than 30 minutes). Every task refreshes
    as the pages change and closes itself when the file is done or
    removed.

    ``removal`` (a page a person just removed from the Journey: batchId,
    pages, filename, removedBy, removedRole, reason) is named in the closing
    evidence of that file's tasks."""
    registry = registry or get_registry()
    now = now or datetime.now(UTC)
    wanted: set[str] = set()
    tl_wanted: set[str] = set()
    cfg_wanted: set[str] = set()
    quality_wanted: set[str] = set()
    counts: dict[str, int] = {}
    # A page the day-end sweep cleared from the Journey (CANCELLED, PAGE_SWEPT)
    # is still a failed page of its file until it is resubmitted: it keeps the
    # file's "Document to resubmit" task open (product owner 2026-10-22). Its
    # row is read as FAILED here; pages in every other state are as they are.
    batches = connection.execute(
        text(
            f"""
            SELECT b.batch_id, b.original_filename, b.page_count, b.batch_status, b.created_at_utc,
                   b.failure_reason,
                   COUNT(q.queue_id) FILTER (WHERE q.queue_status NOT IN ('CANCELLED','MERGED')) AS total,
                   COUNT(q.queue_id) FILTER (WHERE q.queue_status IN ('READY','SUPPORTING','NEEDS_REVIEW')) AS done,
                   COUNT(q.queue_id) FILTER (WHERE q.queue_status='RETRY_WAIT') AS retrying,
                   COUNT(q.queue_id) FILTER (WHERE q.queue_status NOT IN
                     ('READY','SUPPORTING','NEEDS_REVIEW','CANCELLED','MERGED','RETRY_WAIT','FAILED','DEAD_LETTER'))
                     AS working,
                   COALESCE(jsonb_agg(jsonb_build_object(
                       'queueId', q.queue_id,
                       'pages', COALESCE(q.page_numbers, ARRAY[q.page_number]),
                       'reason', q.status_reason,
                       'code', q.last_error,
                       'templateKey', NULLIF(q.template_key, 'supporting_document')) ORDER BY q.page_number)
                     FILTER (WHERE q.queue_status IN ('FAILED','DEAD_LETTER')
                                     AND COALESCE(q.last_error, '') NOT LIKE 'DI_QUALITY_%'), '[]'::jsonb)
                     AS failed_units,
                   -- Pages the scan-quality check refused have their own task
                   -- (#479, PC_PAGE_QUALITY_REJECTED), never the resubmit task.
                   COALESCE(jsonb_agg(jsonb_build_object('page', q.page_number, 'code', q.last_error,
                                                         'queueId', q.queue_id) ORDER BY q.page_number)
                            FILTER (WHERE q.queue_status IN ('FAILED','DEAD_LETTER')
                                      AND q.last_error LIKE 'DI_QUALITY_%'), '[]'::jsonb) AS quality_pages
            FROM auditcore.p2_upload_batches b
            LEFT JOIN (
              SELECT p.tenant_id, p.batch_id, p.queue_id, p.page_number, p.page_numbers, p.status_reason,
                     p.last_error, p.template_key,
                     CASE WHEN {swept_pending_sql("p")} THEN 'FAILED' ELSE p.queue_status END AS queue_status
              FROM auditcore.p2_document_queue p
              WHERE p.tenant_id=:t AND p.journey_id=:j
            ) q ON q.tenant_id=b.tenant_id AND q.batch_id=b.batch_id
            WHERE b.tenant_id=:t AND b.journey_id=:j
              AND b.batch_status NOT IN ('CANCELLED','COMPLETED')
            GROUP BY b.batch_id, b.original_filename, b.page_count, b.batch_status, b.created_at_utc,
                     b.failure_reason
            ORDER BY b.created_at_utc
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    for row in batches:
        filename = row["original_filename"]
        total, done = int(row["total"] or 0), int(row["done"] or 0)
        retrying, working = int(row["retrying"] or 0), int(row["working"] or 0)
        failed_units = list(row["failed_units"] or [])
        failed_pages = sorted({int(p) for unit in failed_units for p in (unit.get("pages") or [])})
        quality = [dict(q) for q in (row["quality_pages"] or [])]
        if quality:
            # A page refused for image quality has the file's quality task
            # only (product owner 2026-10-21): one task per page, never two.
            outcome = _sync_quality_task(
                connection, tenant_id=tenant_id, journey_id=journey_id, batch_id=row["batch_id"],
                filename=filename, quality=quality, registry=registry,
                evaluation_started_at=evaluation_started_at,
            )
            quality_wanted.add(f"upload-quality:{journey_id}:{row['batch_id']}")
            counts[outcome] = counts.get(outcome, 0) + 1
        split_failed = row["batch_status"] == "FAILED" and int(row["page_count"] or 0) == 0
        in_progress = retrying + working > 0 or row["batch_status"] in ("AWAITING_UPLOAD", "UPLOADED", "SPLITTING")
        age = (now - row["created_at_utc"]).total_seconds() if row["created_at_utc"] else 0.0
        setup_units = [u for u in failed_units if str(u.get("code") or "") == SETUP_FAILURE_CODE]
        if setup_units:
            cfg_key = f"ai-config-tl:{journey_id}:{row['batch_id']}"
            cfg_wanted.add(cfg_key)
            _, cfg_outcome = raise_or_refresh(
                connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=cfg_key,
                task_type="TL_DOCUMENT_STUCK", source_type="DOCUMENT", source_code=SETUP_FAILURE_CODE,
                title=f"{filename}: the AI service refused our credentials",
                description=(f"The AI service (Gemini) refused Document Intelligence's key or model while reading "
                             f"{filename}. This is a configuration problem on our side, not the PC's: raise it "
                             "with support now. Nothing reads these pages again by itself: once it is fixed the PC "
                             "uploads them again. This task closes itself when the pages are read, uploaded "
                             "again or removed."),
                reference={"generatedBy": "SYSTEM", "sourceType": "DOCUMENT", "sourceCode": SETUP_FAILURE_CODE,
                           "batchId": str(row["batch_id"]), "filename": filename},
                severity="HIGH", registry=registry, evaluation_started_at=evaluation_started_at,
            )
            counts[cfg_outcome] = counts.get(cfg_outcome, 0) + 1
        if split_failed or failed_units:
            key = f"resubmit:{journey_id}:{row['batch_id']}"
            wanted.add(key)
            pages = [
                {"queueId": str(u["queueId"]), "pageNumbers": [int(p) for p in (u.get("pages") or [])],
                 "reason": resubmit_page_reason(u.get("code"), u.get("reason")),
                 "code": u.get("code"),
                 # The type the page was identified as, when it is known:
                 # the task offers "Send again" for such a page and "Set type
                 # and send" for one that was never identified (2026-10-27).
                 "templateKey": u.get("templateKey"),
                 "filePath": page_open_path(tenant_id, journey_id, u["queueId"])}
                for u in failed_units
            ]
            only_setup = bool(pages) and len(setup_units) == len(failed_units) and not split_failed
            if split_failed:
                title = f"{filename}: upload the file again"
                # Not uploaded, or not split into pages, within 30 minutes
                # (fail_unsettled_pages), or the split itself failed.
                why = (row["failure_reason"] or "").strip() or "It could not be split into pages."
                lines = [f"{filename}: {why}" + ("" if "upload the file again" in why.lower()
                                                     else " Upload the file again from Upload / Edit Documents.")]
            elif only_setup:
                title = f"{filename}: {_pages_text(failed_pages)} not read — our setup problem, upload again"
                lines = [(f"{_pages_text(failed_pages).capitalize()}: {SETUP_PAGE_TEXT} Your Team Lead has been "
                          "alerted. This task closes itself once the page is uploaded again.")]
            else:
                n = len(failed_pages)
                title = f"{filename}: {n} page{'' if n == 1 else 's'} to resubmit"
                lines = [f"{_pages_text(p['pageNumbers']).capitalize()}: {p['reason']}" for p in pages]
                lines.append("Upload the failed pages again from Upload / Edit Documents (on their own, or the "
                             "whole file again; each page's link opens it). This task closes itself once the "
                             "pages are read again or uploaded again.")
            _, outcome = raise_or_refresh(
                connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                task_type=RESUBMIT_TASK_TYPE, source_type="DOCUMENT", source_code="DOCUMENT_PROCESSING_FAILED",
                title=title, description=" ".join(lines),
                reference={"generatedBy": "SYSTEM", "sourceType": "DOCUMENT",
                           "sourceCode": "DOCUMENT_PROCESSING_FAILED", "category": RESUBMIT_CATEGORY,
                           "batchId": str(row["batch_id"]), "filename": filename, "pageNumbers": failed_pages,
                           "pages": pages, "setupOnly": only_setup},
                severity="HIGH", registry=registry,
                evaluation_started_at=evaluation_started_at,
            )
            # The pages' type (2026-10-27) is not part of the issue, so a
            # task already open is not refreshed for it: give it the list.
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_tasks
                    SET reference=jsonb_set(reference, '{pages}', CAST(:pages AS jsonb)), updated_at_utc=now()
                    WHERE tenant_id=:t AND dedupe_key=:k
                      AND task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')
                      AND reference->'pages' IS DISTINCT FROM CAST(:pages AS jsonb)
                    """
                ),
                {"t": tenant_id, "k": key, "pages": _json(pages)},
            )
            counts[outcome] = counts.get(outcome, 0) + 1
            continue
        if not (in_progress and age >= _UPLOAD_STATUS_AFTER_SECONDS):
            continue
        parts = [f"{done} of {total} pages read"]
        if working:
            parts.append(f"{working} being identified or read")
        if retrying:
            parts.append(f"{retrying} waiting for an automatic retry")
        if age >= TL_AFTER_SECONDS:
            # Decision 2026-10-01: a file still not read after a day is the
            # Team Lead's to know about, with what the PC has already been told.
            tl_key = f"upload-stuck-tl:{journey_id}:{row['batch_id']}"
            tl_wanted.add(tl_key)
            _, tl_outcome = raise_or_refresh(
                connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=tl_key,
                task_type="TL_DOCUMENT_STUCK", source_type="DOCUMENT", source_code="DOCUMENT_PROCESSING_STUCK",
                title=f"{filename}: not read for more than a day ({done} of {total} pages read)",
                description=(f"{filename} was uploaded more than 24 hours ago and {total - done} of its pages "
                             "are still not read. The PC has the file's own status task. If it stays unread, "
                             "raise it with support. This task closes itself when the file is done."),
                reference={"generatedBy": "SYSTEM", "sourceType": "DOCUMENT",
                           "sourceCode": "DOCUMENT_PROCESSING_STUCK", "batchId": str(row["batch_id"]),
                           "filename": filename,
                           "pages": {"total": total, "read": done, "working": working, "retrying": retrying}},
                severity="HIGH", registry=registry, evaluation_started_at=evaluation_started_at,
            )
            counts[tl_outcome] = counts.get(tl_outcome, 0) + 1
        key = f"upload-status:{journey_id}:{row['batch_id']}"
        wanted.add(key)
        _, outcome = raise_or_refresh(
            connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
            task_type="PC_UPLOAD_STATUS", source_type="DOCUMENT", source_code="DOCUMENT_PROCESSING_SLOW",
            title=f"{filename}: {done} of {total} pages read, still working",
            description=(", ".join(parts) + ". Nothing to do yet. This task updates itself, and closes when the "
                         "file is done or turns into a resubmit task for any page that cannot be read."),
            reference={"generatedBy": "SYSTEM", "sourceType": "DOCUMENT", "sourceCode": "DOCUMENT_PROCESSING_SLOW",
                       "batchId": str(row["batch_id"]), "filename": filename, "pageNumbers": [],
                       "pages": {"total": total, "read": done, "working": working, "retrying": retrying},
                       "retrying": True},
            severity="INFO", registry=registry, evaluation_started_at=evaluation_started_at,
        )
        counts[outcome] = counts.get(outcome, 0) + 1
    quality_batches = {key.rsplit(":", 1)[-1] for key in quality_wanted}
    removal_evidence = _removal_evidence(removal) if removal else None

    def status_evidence(key: str) -> dict[str, Any]:
        batch = key.rsplit(":", 1)[-1]
        if removal_evidence and batch == str(removal["batchId"]):
            return removal_evidence
        if batch in quality_batches:
            # Nothing on the file failed otherwise: what is left was refused
            # for image quality and is on the file's own quality task.
            return {"reason": "The pages still not read were refused for image quality; they are on the file's "
                              "image-quality task."}
        return {"reason": "The file was processed or removed."}

    def removal_or(key: str, reason: str) -> dict[str, Any]:
        if removal_evidence and key.rsplit(":", 1)[-1] == str(removal["batchId"]):
            return removal_evidence
        return {"reason": reason}

    def quality_evidence(key: str) -> dict[str, Any]:
        if removal_evidence and key.rsplit(":", 1)[-1] == str(removal["batchId"]):
            return removal_evidence
        return _QUALITY_CLOSED_EVIDENCE

    closed = 0
    for prefix, keep, evidence in (
        (f"upload-status:{journey_id}:", wanted, status_evidence),
        (f"resubmit:{journey_id}:", wanted,
         lambda k: removal_or(k, "The pages were read again, uploaded again or removed.")),
        (f"upload-stuck-tl:{journey_id}:", tl_wanted, {"reason": "The file was processed or removed."}),
        (f"ai-config-tl:{journey_id}:", cfg_wanted,
         lambda k: removal_or(k, "The pages were read, or are no longer waiting.")),
        (f"upload-quality:{journey_id}:", quality_wanted, quality_evidence),
        # The per-page tasks the file's task replaced close too.
        (f"processing-failed:{journey_id}:", set(), {"reason": "Replaced by the file's own task."}),
    ):
        closed += _resolve_prefix_except(connection, tenant_id=tenant_id, journey_id=journey_id, prefix=prefix,
                                         keep=keep, evidence=evidence)
    closed += sync_dead_work_tasks(connection, tenant_id=tenant_id, journey_id=journey_id, registry=registry)
    if closed:
        counts["VERIFIED"] = counts.get("VERIFIED", 0) + closed
    return counts


LARGE_UPLOAD_TASK_TYPE = "PC_LARGE_UPLOAD_REVIEW"
_LARGE_UPLOAD_PREFIX = "large-upload:"


def sync_large_upload_tasks(
    connection: Connection, *, tenant_id: str, journey_id: UUID, registry: Registry | None = None,
    evaluation_started_at: datetime | None = None, threshold: int | None = None,
) -> dict[str, int]:
    """One Info notice (PC) per uploaded file of more than ``threshold`` pages
    (25 by default; product owner 2026-10-26): please review the file and
    confirm that this many pages are really needed. It never blocks anything:
    the upload is already read, the task sits outside the Documents and
    Manual Verification tabs the delivery gate looks at, and the PC closes it
    with a comment (Close task). A task closed that way is not reopened for
    the same file. It closes itself if the file is cancelled, fails before it
    was split or is deleted. The Daily Processing report highlights the file."""
    registry = registry or get_registry()
    limit = LARGE_UPLOAD_PAGES if threshold is None else threshold
    batches = connection.execute(
        text(
            """
            SELECT batch_id, original_filename, page_count
            FROM auditcore.p2_upload_batches
            WHERE tenant_id=:t AND journey_id=:j AND page_count > :limit
              AND batch_status NOT IN ('CANCELLED','FAILED','AWAITING_UPLOAD')
            ORDER BY created_at_utc
            """
        ),
        {"t": tenant_id, "j": journey_id, "limit": limit},
    ).mappings().all()
    counts: dict[str, int] = {}
    wanted: set[str] = set()
    for row in batches:
        pages, filename = int(row["page_count"]), row["original_filename"]
        key = f"{_LARGE_UPLOAD_PREFIX}{journey_id}:{row['batch_id']}"
        wanted.add(key)
        _, outcome = raise_or_refresh(
            connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
            task_type=LARGE_UPLOAD_TASK_TYPE, source_type="DOCUMENT", source_code="LARGE_UPLOAD",
            title=f"{filename}: {pages} pages uploaded, please review",
            description=(f"{filename} has {pages} pages, more than {limit}. Please review the file and check "
                         f"that all {pages} pages are really needed on this booking; remove or leave out the "
                         "pages that are not. Nothing is blocked and you can keep uploading. Close this task "
                         "with a comment when you have reviewed it."),
            reference={"generatedBy": "SYSTEM", "sourceType": "DOCUMENT", "sourceCode": "LARGE_UPLOAD",
                       "batchId": str(row["batch_id"]), "filename": filename, "pageCount": pages,
                       "threshold": limit},
            severity="INFO", registry=registry, evaluation_started_at=evaluation_started_at,
        )
        counts[outcome] = counts.get(outcome, 0) + 1
    closed = _resolve_prefix_except(
        connection, tenant_id=tenant_id, journey_id=journey_id, prefix=f"{_LARGE_UPLOAD_PREFIX}{journey_id}:",
        keep=wanted, evidence={"reason": "The file is no longer on the journey, or has no more than "
                                         f"{limit} pages."},
    )
    if closed:
        counts["VERIFIED"] = counts.get("VERIFIED", 0) + closed
    return counts


def sync_dead_work_tasks(
    connection: Connection, *, tenant_id: str, journey_id: UUID, registry: Registry | None = None,
) -> int:
    """Close the tasks a stopped step raised once their condition is gone
    (2026-10-20 re-review, R2), and hand back one a person marked done while
    the condition still holds -- never left waiting in VERIFYING:
    - work-dead:{journey}:{work type} (Team Lead): that step of the journey
      is no longer dead-lettered (Re-sync ran it, or it was asked again);
    - group-failed:{journey}:{batch} (PC): the upload was grouped, removed,
      or uploaded again (a later upload of the same file).
    Returns the number of tasks closed."""
    registry = registry or get_registry()
    tasks = connection.execute(
        text(
            """
            SELECT dedupe_key, task_status, task_type, source_type, source_code, title, description,
                   reference, severity
            FROM auditcore.p2_tasks
            WHERE tenant_id=:t AND journey_id=:j AND task_status = ANY(:open)
              AND (dedupe_key LIKE :dead OR dedupe_key LIKE :group)
            """
        ),
        {"t": tenant_id, "j": journey_id, "open": list(_OPEN), "dead": f"work-dead:{journey_id}:%",
         "group": f"group-failed:{journey_id}:%"},
    ).mappings().all()
    closed = 0
    for task in tasks:
        key = str(task["dedupe_key"])
        kind, _, rest = key.partition(f":{journey_id}:")
        if kind == "work-dead":
            holds = connection.execute(
                text(
                    """
                    SELECT EXISTS (SELECT 1 FROM auditcore.p2_work_queue
                                   WHERE tenant_id=:t AND journey_id=:j AND work_type=:w
                                     AND work_status='DEAD_LETTER')
                    """
                ),
                {"t": tenant_id, "j": journey_id, "w": rest},
            ).scalar_one()
            reason = "The step ran again."
        else:
            holds = connection.execute(
                text(
                    """
                    SELECT EXISTS (
                      SELECT 1 FROM auditcore.p2_upload_batches b
                      WHERE b.tenant_id=:t AND b.batch_id=CAST(:b AS uuid) AND b.grouping_status='FAILED'
                        AND b.batch_status <> 'CANCELLED'
                        AND NOT EXISTS (
                          SELECT 1 FROM auditcore.p2_upload_batches later
                          WHERE later.tenant_id=b.tenant_id AND later.journey_id=b.journey_id
                            AND later.original_filename=b.original_filename
                            AND later.created_at_utc > b.created_at_utc
                            AND later.batch_status <> 'CANCELLED'))
                    """
                ),
                {"t": tenant_id, "b": rest},
            ).scalar_one()
            reason = "The upload was grouped, removed or uploaded again."
        if not holds:
            resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                            evidence={"reason": reason})
            closed += 1
        elif task["task_status"] == "VERIFYING":
            # Marked done while the step is still stopped: back to the assignee.
            raise_or_refresh(
                connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                task_type=str(task["task_type"]), source_type=str(task["source_type"]),
                source_code=str(task["source_code"]), title=str(task["title"]),
                description=str(task["description"]),
                reference={k: v for k, v in dict(task["reference"] or {}).items() if k != "issueHash"},
                severity=str(task["severity"]), registry=registry, evaluation_started_at=datetime.now(UTC),
            )
    return closed


def sync_unclassified_page_tasks(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    registry: Registry | None = None,
    evaluation_started_at: datetime | None = None,
) -> dict[str, int]:
    """One High task (PC) per page Document Intelligence could not classify.
    The page is kept as supporting evidence, but nobody has said what it is:
    the PC sets its type on the Upload / Edit Documents card (it is then
    read as that document), or marks it Others (kept for the record, never
    read). Either closes the task; so does removing the page."""
    registry = registry or get_registry()
    wanted: set[str] = set()
    counts: dict[str, int] = {}
    rows = connection.execute(
        text(
            """
            SELECT q.queue_id, b.batch_id, b.original_filename, q.page_numbers, q.page_number
            FROM auditcore.p2_document_queue q
            JOIN auditcore.p2_upload_batches b ON b.tenant_id=q.tenant_id AND b.batch_id=q.batch_id
            WHERE q.tenant_id=:t AND q.journey_id=:j AND q.queue_status='SUPPORTING'
              AND COALESCE(q.template_key, 'supporting_document') = 'supporting_document'
              AND q.type_overridden_by_actor_id IS NULL
            ORDER BY b.created_at_utc, q.page_number
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    for row in rows:
        key = f"unclassified:{journey_id}:{row['queue_id']}"
        wanted.add(key)
        pages = list(row["page_numbers"] or [row["page_number"]])
        where = f"Page {pages[0]}" if len(pages) == 1 else f"Pages {'-'.join(str(p) for p in pages)}"
        _, outcome = raise_or_refresh(
            connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
            task_type="PC_VERIFY_UNRECOGNIZED_DOCUMENT", source_type="DOCUMENT",
            source_code="DOCUMENT_UNCLASSIFIED",
            title=f"Set the document type: {where.lower()} of {row['original_filename']}",
            description=(f"Document Intelligence could not tell what {where.lower()} of {row['original_filename']} "
                         "is, so nothing was read from it. Open Upload / Edit Documents, find its card and "
                         "choose the document type; it is then read as that document. Choose Others if it is "
                         "not a checklist document: it stays on file without being read."),
            reference={"generatedBy": "SYSTEM", "sourceType": "DOCUMENT", "sourceCode": "DOCUMENT_UNCLASSIFIED",
                       "batchId": str(row["batch_id"]), "queueId": str(row["queue_id"]),
                       "filename": row["original_filename"], "pageNumbers": pages},
            severity="HIGH", registry=registry, evaluation_started_at=evaluation_started_at,
        )
        counts[outcome] = counts.get(outcome, 0) + 1
    closed = _resolve_prefix_except(
        connection, tenant_id=tenant_id, journey_id=journey_id, prefix=f"unclassified:{journey_id}:",
        keep=wanted, evidence={"reason": "The document type was set, the page was kept as Others, or it was removed."},
    )
    if closed:
        counts["VERIFIED"] = counts.get("VERIFIED", 0) + closed
    return counts


def sync_booking_date_task(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    registry: Registry | None = None,
    evaluation_started_at: datetime | None = None,
) -> dict[str, int]:
    """One Medium task (PC) while the booking form on file carries no
    booking date: the deal is priced on that date, and it is never guessed
    (decision 2026-09-30). The task opens the booking form on its booking
    date field and closes itself once a date is in. A date that was read
    but cannot be right (before the floor, unreadable) is the field review
    task's business, not this one's."""
    from audit_core.uc03_p2_dates import booking_form_date, unusable_pricing_date_reason

    registry = registry or get_registry()
    key = f"booking-date:{journey_id}"
    form = booking_form_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    counts: dict[str, int] = {}
    # A date that is present but cannot price a deal gets the same task as a
    # date that is missing (2026-10-24). Before this it only fired on an empty
    # value, so a booking date misread as 2024 left the resolver with nothing
    # to price on and nobody asked to fix it: 30 Journeys on Mahindra Orissa
    # stuck with no price and no task. Anything not available raises a task.
    unusable = (unusable_pricing_date_reason(form["raw"]) if form is not None else None)
    if form is not None and unusable is not None:
        name = "Booking Form" if form["documentType"] == "booking_form" else "Booking Docket"
        missing = form["raw"] in (None, "")
        _, outcome = raise_or_refresh(
            connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
            task_type="PC_BOOKING_DATE_MISSING", source_type="DOCUMENT_FIELD", source_code="BOOKING_DATE_MISSING",
            title=(f"Enter the booking date on the {name}" if missing
                   else f"Correct the booking date on the {name}"),
            description=(f"The {name} was read, but {unusable}. The deal is priced on the booking date, so "
                         "nothing is priced until it is right -- no standard price, no SKU check, no "
                         f"commercial finding. Open the document, {'enter' if missing else 'correct'} the "
                         "booking date printed on it and save. This task closes itself once the date is in."),
            reference={"generatedBy": "SYSTEM", "sourceType": "DOCUMENT_FIELD", "sourceCode": "BOOKING_DATE_MISSING",
                       "documentId": form["documentId"], "documentIds": [form["documentId"]],
                       "templateKey": form["documentType"], "fieldKey": "booking_date",
                       "fieldKeys": ["booking_date"], "readAs": form["raw"], "reason": unusable},
            severity="CRITICAL", registry=registry, evaluation_started_at=evaluation_started_at,
        )
        counts[outcome] = counts.get(outcome, 0) + 1
    elif resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                         evidence={"bookingDate": (form or {}).get("raw"),
                                   "verifiedAt": datetime.now(UTC).isoformat()}):
        counts["VERIFIED"] = 1
    return counts


def sync_document_missing_tasks(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    registry: Registry | None = None,
    evaluation_started_at: datetime | None = None,
) -> dict[str, int]:
    """One Document Missing task (PC) per conditional document that
    evidence made mandatory and that is not in yet -- e.g. a corporate
    discount on the booking form or invoice asks for the Corporate ID. The
    task closes itself when the document is read, or when the evidence no
    longer applies.

    A template may set ``raises_missing_task: false`` to stay on the stage
    checklist as expected without ever becoming a task -- for a document the
    dealership may legitimately not have raised, whose absence is not itself
    a finding."""
    from audit_core.uc03_p2_stage import condition_reasons, requirement_items

    registry = registry or get_registry()
    reasons = condition_reasons(connection, tenant_id=tenant_id, journey_id=journey_id)
    wanted: set[str] = set()
    counts: dict[str, int] = {}
    for stage in ("BOOKING", "DELIVERY"):
        for item in requirement_items(connection, registry, tenant_id=tenant_id, journey_id=journey_id,
                                      stage=stage, reasons=reasons):
            if item["requirement"] != "CONDITIONAL" or not item["required"] or item["received"]:
                continue
            if not item.get("raisesMissingTask", True):
                continue
            key = f"document-missing:{journey_id}:{item['key']}"
            wanted.add(key)
            conditions = ", ".join(CONDITION_TEXT.get(c, c) for c in item["conditions"])
            _, outcome = raise_or_refresh(
                connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                task_type="DOCUMENT_MISSING", source_type="DOCUMENT", source_code="DOCUMENT_MISSING",
                title=f"Upload the {item['label']}",
                description=(f"{item['reason']} The {item['label']} is therefore mandatory for this "
                             f"{'booking' if stage == 'BOOKING' else 'delivery'} ({conditions}). "
                             "Upload it; this task closes itself once it is read."),
                reference={"generatedBy": "SYSTEM", "sourceType": "DOCUMENT", "sourceCode": "DOCUMENT_MISSING",
                           "requirementKey": item["key"], "templateKeys": item["templates"],
                           "conditions": item["conditions"], "stage": stage},
                severity="HIGH", registry=registry, evaluation_started_at=evaluation_started_at,
            )
            counts[outcome] = counts.get(outcome, 0) + 1
    closed = _resolve_prefix_except(
        connection, tenant_id=tenant_id, journey_id=journey_id, prefix=f"document-missing:{journey_id}:",
        keep=wanted, evidence={"documentReceivedOrNotRequired": True, "verifiedAt": datetime.now(UTC).isoformat()},
    )
    if closed:
        counts["VERIFIED"] = counts.get("VERIFIED", 0) + closed
    return counts


def _named_documents(connection: Connection, *, tenant_id: str, journey_id: UUID,
                     pairs: tuple[str, ...]) -> list[dict[str, Any]]:
    """Every ACTIVE document whose ``<di type>.<field>`` is one of ``pairs``,
    with the name it carries (as corrected by the PC, if it was)."""
    from audit_core.uc03_p2_stage import _LEGACY_TYPE_ALIASES

    wanted: dict[str, str] = {}
    for pair in pairs:
        di_type, _, field_key = pair.partition(".")
        wanted[pair] = pair
        for alias in _LEGACY_TYPE_ALIASES.get(di_type, ()):
            wanted[f"{alias}.{field_key}"] = pair
    if not wanted:
        return []
    rows = connection.execute(
        text(
            """
            SELECT e.di_document_id, e.document_type_key, e.process_area, e.linked_at_utc,
                   f.field_key, f.effective_value
            FROM auditcore.evidence e
            JOIN auditcore.journey_document_extracted_fields f
              ON f.tenant_id=e.tenant_id AND f.journey_id=e.journey_id AND f.di_document_id=e.di_document_id
            WHERE e.tenant_id=:t AND e.journey_id=:j AND e.association_status='ACTIVE'
              AND (e.document_type_key || '.' || f.field_key) = ANY(:pairs)
              AND f.effective_value IS NOT NULL
              AND f.effective_value <> 'null'::jsonb AND f.effective_value <> '""'::jsonb
            ORDER BY e.linked_at_utc ASC, e.di_document_id ASC
            """
        ),
        {"t": tenant_id, "j": journey_id, "pairs": list(wanted)},
    ).mappings().all()
    return [
        {
            "documentId": str(row["di_document_id"]), "diType": str(row["document_type_key"]),
            "stage": str(row["process_area"] or "").upper() or None, "fieldKey": str(row["field_key"]),
            "pair": wanted[f"{row['document_type_key']}.{row['field_key']}"], "name": row["effective_value"],
        }
        for row in rows
        if isinstance(row["effective_value"], str) and row["effective_value"].strip()
    ]


def _gst_trade_names(connection: Connection, *, tenant_id: str, journey_id: UUID, document_id: str | None) -> list[str]:
    """The trade name the GST certificate prints, if any."""
    if not document_id:
        return []
    return [str(v) for v in connection.execute(
        text(
            """
            SELECT DISTINCT f.effective_value #>> '{}' FROM auditcore.journey_document_extracted_fields f
            WHERE f.tenant_id=:t AND f.journey_id=:j AND f.di_document_id=CAST(:d AS uuid)
              AND f.field_key IN ('organisation_trade_name', 'trade_name')
              AND f.effective_value IS NOT NULL AND f.effective_value <> 'null'::jsonb
            """
        ),
        {"t": tenant_id, "j": journey_id, "d": document_id},
    ).scalars().all() if v and str(v).strip()]


def sync_name_consistency_tasks(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    registry: Registry | None = None,
    evaluation_started_at: datetime | None = None,
) -> dict[str, int]:
    """One customer and one dealership per Journey.

    Every customer document (booking form, invoices, receipts, insurance
    cover...) must carry the customer's name as the KYC gives it, and every
    dealer document must carry the dealership the booking form names. A
    document in another name is the wrong document: a High severity Document
    Missing task (PC) says which document, whose name it carries and whose it
    should, and asks for it to be deleted and the right one uploaded. The
    task closes itself when that document goes, or when a corrected name
    matches after all."""
    registry = registry or get_registry()
    rules = registry.extraction_rules
    wanted: set[str] = set()
    counts: dict[str, int] = {}

    def raise_task(document: dict[str, Any], *, kind: str, title: str, description: str,
                   reference: dict[str, Any]) -> None:
        key = f"wrong-document:{journey_id}:{kind}:{document['documentId']}"
        wanted.add(key)
        template = registry.template_for_di_type(document["diType"], stage=document["stage"])
        _, outcome = raise_or_refresh(
            connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
            task_type="DOCUMENT_MISSING", source_type="DOCUMENT", source_code=f"WRONG_{kind.upper()}_NAME",
            title=title, description=description,
            reference={"generatedBy": "SYSTEM", "sourceType": "DOCUMENT", "sourceCode": f"WRONG_{kind.upper()}_NAME",
                       "documentId": document["documentId"], "documentIds": [document["documentId"]],
                       "templateKey": template.key, "fieldKey": document["fieldKey"],
                       "fieldKeys": [document["fieldKey"]], "stage": document["stage"], **reference},
            severity="HIGH", registry=registry, evaluation_started_at=evaluation_started_at,
        )
        counts[outcome] = counts.get(outcome, 0) + 1

    # -- the customer, as the KYC names them: the verified legal name on the
    # customer record (the first KYC read) is the reference; before one is
    # verified, the latest KYC document is. Every other document naming the
    # customer, a second KYC document included, must be the same person.
    references = _named_documents(connection, tenant_id=tenant_id, journey_id=journey_id,
                                  pairs=rules.customer_name_reference)
    verified = connection.execute(
        text(
            """
            SELECT c.legal_name, e.di_document_id, e.document_type_key, e.process_area
            FROM auditcore.journeys j
            JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
            LEFT JOIN auditcore.evidence e
              ON e.tenant_id=c.tenant_id AND e.evidence_id=c.legal_name_source_evidence_id
            WHERE j.tenant_id=:t AND j.journey_id=:j AND c.legal_name_status='VERIFIED'
              AND c.legal_name IS NOT NULL AND btrim(c.legal_name) <> ''
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().first()
    if verified is not None:
        kyc = {
            "name": verified["legal_name"],
            "documentId": str(verified["di_document_id"]) if verified["di_document_id"] else None,
            "diType": str(verified["document_type_key"] or "pan_card"),
            "stage": str(verified["process_area"] or "BOOKING").upper(),
        }
    else:
        by_pair = {doc["pair"]: doc for doc in reversed(references)}  # latest document of each kind
        kyc = next((by_pair[pair] for pair in rules.customer_name_reference if pair in by_pair), None)
    if kyc is not None:
        kyc_template = registry.template_for_di_type(kyc["diType"], stage=kyc["stage"])
        checked = _named_documents(connection, tenant_id=tenant_id, journey_id=journey_id,
                                   pairs=rules.customer_name_checked)
        checked += [doc for doc in references if doc["documentId"] != kyc["documentId"]]
        # The customer named by a GST certificate (product owner 2026-10-27):
        # a document may carry the GSTIN's legal or trade name, or the name of
        # the person whose KYC is on file (the proprietor / signatory). The
        # KYC documents are that person's own, so they only have to agree
        # with each other, not with a firm's name.
        gst_names: list[str] = []
        person: dict[str, Any] | None = None
        if kyc["diType"] == "gst_certificate":
            gst_names = [str(kyc["name"]), *_gst_trade_names(connection, tenant_id=tenant_id,
                                                             journey_id=journey_id, document_id=kyc["documentId"])]
            by_pair = {doc["pair"]: doc for doc in reversed(references)}
            person = next((by_pair[pair] for pair in rules.customer_name_reference if pair in by_pair), None)

        def matches(document: dict[str, Any]) -> bool:
            # A masked PAN ("PXXXXJ KXXXR NXXXX") is the customer's when its
            # visible letters fit the customer's name (2026-10-28).
            if is_masked(document["name"]):
                return any(masked_matches(document["name"], n)
                           for n in [kyc["name"], *gst_names, *([person["name"]] if person else [])])
            if is_masked(kyc["name"]) and masked_matches(kyc["name"], document["name"]):
                return True  # only a masked PAN names the customer: a full name that fits it is theirs
            if not gst_names:
                return same_person(document["name"], kyc["name"])
            if document["pair"] in rules.customer_name_reference:
                return person is None or document["documentId"] == person["documentId"] or same_person(
                    document["name"], person["name"])
            return (any(same_person(document["name"], n) or same_organisation(document["name"], n) for n in gst_names)
                    or (person is not None and same_person(document["name"], person["name"])))

        for document in checked:
            if matches(document):
                continue
            template = registry.template_for_di_type(document["diType"], stage=document["stage"])
            raise_task(
                document, kind="customer",
                title=f"Replace the {template.display_name}: it is not in the customer's name",
                description=(
                    f"The {template.display_name} is in the name of {display_name(document['name'])}, but the "
                    f"customer per the {kyc_template.display_name} is {display_name(kyc['name'])}. Wrong document "
                    f"uploaded: please delete it and upload the {template.display_name} in the customer's name as "
                    f"per {'the GST certificate' if gst_names else 'PAN/Aadhaar'}. If the name was only misread, "
                    f"correct it on the document instead."
                ),
                reference={"documentName": display_name(document["name"]), "customerName": display_name(kyc["name"]),
                           "referenceDocumentId": kyc["documentId"], "referenceTemplateKey": kyc_template.key},
            )

    # -- the dealership. The booking form is the OEM's form and often names
    # the OEM (Mahindra), not the dealership (Aditya Motors), so a document
    # naming the OEM is not compared (2026-09-30). The reference is the
    # booking form when it names a dealership, else the earliest dealer
    # document; every other dealer document must name the same dealership.
    oem = connection.execute(
        text("SELECT o.oem_code, o.oem_name FROM auditcore.projects p JOIN auditcore.oems o ON o.oem_id=p.oem_id "
             "WHERE p.tenant_id=:t LIMIT 1"),
        {"t": tenant_id},
    ).mappings().first()
    oem_names = [n for n in ((oem or {}).get("oem_code"), (oem or {}).get("oem_name")) if n]
    dealers = [
        d for d in _named_documents(connection, tenant_id=tenant_id, journey_id=journey_id, pairs=rules.dealer_name_checked)
        if not any(same_organisation(d["name"], name) for name in oem_names)
    ]
    anchor = next((d for d in reversed(dealers) if d["pair"].startswith("booking_form.")), dealers[0] if dealers else None)
    if anchor is not None:
        anchor_template = registry.template_for_di_type(anchor["diType"], stage=anchor["stage"])
        for document in dealers:
            if document["documentId"] == anchor["documentId"] or same_organisation(document["name"], anchor["name"]):
                continue
            template = registry.template_for_di_type(document["diType"], stage=document["stage"])
            raise_task(
                document, kind="dealer",
                title=f"Replace the {template.display_name}: it names another dealership",
                description=(
                    f"The {template.display_name} names the dealership {display_name(document['name'])}, but the "
                    f"{anchor_template.display_name} names {display_name(anchor['name'])}. Wrong document uploaded: "
                    f"please delete it and upload the {template.display_name} issued by this dealership. If the name "
                    f"was only misread, correct it on the document instead."
                ),
                reference={"documentName": display_name(document["name"]), "dealerName": display_name(anchor["name"]),
                           "referenceDocumentId": anchor["documentId"], "referenceTemplateKey": anchor_template.key},
            )

    closed = _resolve_prefix_except(
        connection, tenant_id=tenant_id, journey_id=journey_id, prefix=f"wrong-document:{journey_id}:",
        keep=wanted, evidence={"namesConsistent": True, "verifiedAt": datetime.now(UTC).isoformat()},
    )
    if closed:
        counts["VERIFIED"] = counts.get("VERIFIED", 0) + closed
    return counts


CONDITION_TEXT = {
    "financeCase": "finance case", "corporateDiscount": "corporate discount", "corporateCustomer": "corporate customer",
    "exchangeBenefit": "exchange benefit", "insuranceByDealer": "insurance through the dealership",
    "registrationByDealer": "registration through the dealership", "rsaSold": "RSA sold",
    "ewSold": "extended warranty sold", "accessoriesSold": "accessories sold", "scrappageClaimed": "scrappage benefit",
}


def sync_vehicle_photo_task(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    registry: Registry | None = None,
    evaluation_started_at: datetime | None = None,
) -> str | None:
    """Once every other Delivery document is in but there is no proof of the
    vehicle, the PC is asked to upload pictures of the car being delivered
    (VIN, sides, interior) or enter the VIN / chassis / engine number. The
    task closes itself on the first picture or entered identity."""
    from audit_core.uc03_p2_stage import vehicle_proof

    registry = registry or get_registry()
    gates = _stage_rows(connection, tenant_id=tenant_id, journey_id=journey_id)
    docs = gates.get(("DELIVERY", "REQUIRED_DOCUMENTS")) or {}
    proof = vehicle_proof(connection, tenant_id=tenant_id, journey_id=journey_id)
    stage = connection.execute(
        text("SELECT current_stage FROM auditcore.p2_journey_runtime WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one_or_none()
    dedupe_key = f"vehicle-photos:{journey_id}"
    if proof.get("passed"):
        resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=dedupe_key,
                        evidence={"photos": proof.get("photos"), "manualIdentity": proof.get("manualIdentity"),
                                  "verifiedAt": datetime.now(UTC).isoformat()})
        return "VERIFIED"
    if str(stage or "").startswith("DELIVERY") and docs.get("passed"):
        _, outcome = raise_or_refresh(
            connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=dedupe_key,
            task_type="DELIVERY_VEHICLE_PHOTOS_MISSING", source_type="EVIDENCE", source_code="VEHICLE_PHOTOS",
            title="Add pictures of the car, or enter the VIN / engine number",
            description=(
                "Every other delivery document is in, but there is no proof of the vehicle. Upload pictures of the "
                "car being delivered (VIN plate, sides, interior) from the booking's Vehicle photos tab, or enter "
                "the VIN / chassis / engine number here. The Team Lead sees the entry in the delivery review."
            ),
            reference={"generatedBy": "SYSTEM", "sourceType": "EVIDENCE", "sourceCode": "VEHICLE_PHOTOS"},
            severity="HIGH", registry=registry, evaluation_started_at=evaluation_started_at,
        )
        return outcome
    return None


def sync_delivery_review_task(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    registry: Registry | None = None,
    evaluation_started_at: datetime | None = None,
) -> str | None:
    """A completed Delivery is marked for TL review: one Review Delivery
    task for the Team Lead, closed when the TL marks the review done."""
    from audit_core.uc03_p2_workflow import delivery_reviewed

    registry = registry or get_registry()
    state = connection.execute(
        text("SELECT delivery_completion_state FROM auditcore.p2_journey_runtime WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one_or_none()
    dedupe_key = f"delivery-review:{journey_id}"
    if state != "COMPLETE":
        return None
    if delivery_reviewed(connection, tenant_id=tenant_id, journey_id=journey_id):
        resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=dedupe_key,
                        evidence={"reviewed": True, "verifiedAt": datetime.now(UTC).isoformat()})
        return "VERIFIED"
    _, outcome = raise_or_refresh(
        connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=dedupe_key,
        task_type="DELIVERY_REVIEW", source_type="REVIEW", source_code="DELIVERY_REVIEW",
        title="Review the completed delivery",
        description=("Every delivery document is in, the vehicle is proven and the PC's tasks are closed. All "
                     "compliance checks have run. Review the Journey 360 and its compliance report, then mark the "
                     "review done."),
        reference={"generatedBy": "SYSTEM", "sourceType": "REVIEW", "sourceCode": "DELIVERY_REVIEW"},
        severity="MEDIUM", registry=registry, evaluation_started_at=evaluation_started_at,
    )
    return outcome
