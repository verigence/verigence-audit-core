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
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_p2_registry import ControlTemplate, Registry, get_registry
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
    for transition in transitions:
        control = registry.controls.get(transition.control_code)
        if control is None:
            continue
        key = f"control:{journey_id}:{control.code}"
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
    for document_id, fields in by_document.items():
        name = fields[0]["documentName"]
        fields.sort(key=lambda f: (f["confidence"] is not None, f["confidence"] or 0))
        listed = ", ".join(_field_with_confidence(f) for f in fields[:6])
        more = f" and {len(fields) - 6} more" if len(fields) > 6 else ""
        reference = {
            "generatedBy": "SYSTEM", "sourceType": "DOCUMENT_FIELD", "sourceCode": "MANUAL_VERIFICATION",
            "documentId": document_id, "documentIds": [document_id], "templateKey": fields[0]["templateKey"],
            "fields": [{"fieldKey": f["fieldKey"], "canonicalFieldId": f["canonicalFieldId"],
                        "sourceFactVersion": f["sourceFactVersion"], "confidence": f["confidence"],
                        "threshold": f["threshold"]} for f in fields],
            "fieldKeys": [f["fieldKey"] for f in fields],
        }
        _, outcome = raise_or_refresh(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            dedupe_key=f"field-review:{journey_id}:{document_id}",
            task_type="MANUAL_VERIFICATION_REVIEW", source_type="DOCUMENT_FIELD",
            source_code="MANUAL_VERIFICATION",
            title=f"Verify {len(fields)} field{'s' if len(fields) != 1 else ''} on {name}",
            description=(
                f"These values on the {name} were read with low confidence: {listed}{more}. "
                "Open the document, compare each value with the page and confirm or correct it."
            ),
            reference=reference, severity="MEDIUM", registry=registry,
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


def sync_vehicle_photo_task(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    registry: Registry | None = None,
    evaluation_started_at: datetime | None = None,
) -> str | None:
    """DELIVERY_VEHICLE_PHOTOS_MISSING while Delivery is under way and no
    vehicle photo is on file; the task closes itself on the first photo."""
    registry = registry or get_registry()
    row = connection.execute(
        text(
            """
            SELECT
              (SELECT current_stage FROM auditcore.p2_journey_runtime
                WHERE tenant_id=:t AND journey_id=:j) AS stage,
              (SELECT COUNT(*) FROM auditcore.delivery_vehicle_photos
                WHERE tenant_id=:t AND journey_id=:j AND deleted_at_utc IS NULL) AS photos
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    dedupe_key = f"vehicle-photos:{journey_id}"
    in_delivery = str(row["stage"] or "").startswith("DELIVERY")
    if in_delivery and not row["photos"]:
        _, outcome = raise_or_refresh(
            connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=dedupe_key,
            task_type="DELIVERY_VEHICLE_PHOTOS_MISSING", source_type="EVIDENCE",
            source_code="VEHICLE_PHOTOS",
            title="Add vehicle photos",
            description=(
                "Delivery has started but no photo of the vehicle is on file. Take or upload photos "
                "of the delivered vehicle (front, rear, sides, odometer) from the booking's Photos tab."
            ),
            reference={"generatedBy": "SYSTEM", "sourceType": "EVIDENCE", "sourceCode": "VEHICLE_PHOTOS",
                       "photoCount": 0},
            severity="MEDIUM", registry=registry, evaluation_started_at=evaluation_started_at,
        )
        return outcome
    if row["photos"]:
        resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=dedupe_key,
                        evidence={"photoCount": int(row["photos"]), "verifiedAt": datetime.now(UTC).isoformat()})
        return "VERIFIED"
    return None
