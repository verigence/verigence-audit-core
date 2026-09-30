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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_p2_names import display_name, same_organisation, same_person
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
    # A question for the PC (its answers), and what the check looked at.
    for key in ("question", "answers", "paymentIds", "documentIds", "components", "discounts"):
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


def _date_problem(field: dict[str, Any], floor: date | None) -> str:
    """"Receipt date read as 12/03/2019, before July 2026": what the PC
    checks on the page and why it is almost certainly misread."""
    shown = format_value(field["fieldKey"], field.get("value"))
    if "DATE_UNREADABLE" in field.get("reasons", ()):
        return f"{_label(field['fieldKey'])} read as {shown!r}, which is not a date"
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
                           keep: set[str], evidence: dict[str, Any]) -> int:
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
            resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key, evidence=evidence)
            closed += 1
    return closed


# A file still being worked on this long after upload gets a status task:
# "check back after 1 hour". A page is never failed for taking long (a burst
# or a quota is waited out); after an hour the PC is simply told how far the
# file is, and the task updates itself until the file is done.
_UPLOAD_STATUS_AFTER_SECONDS = int(os.environ.get("P2_UPLOAD_STATUS_AFTER_SECONDS", str(60 * 60)))
_SETTLED_PAGE_STATES = ("READY", "SUPPORTING", "NEEDS_REVIEW", "CANCELLED", "MERGED")
_FAILED_PAGE_STATES = ("FAILED", "DEAD_LETTER")


def _pages_text(pages: list[int]) -> str:
    if len(pages) == 1:
        return f"page {pages[0]}"
    return "pages " + ", ".join(str(p) for p in pages[:-1]) + f" and {pages[-1]}"


def sync_processing_failure_tasks(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    registry: Registry | None = None,
    evaluation_started_at: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """One task (PC) per uploaded file that is not done, never one per page.

    Info, an hour after upload, while pages are still being identified or
    waiting for an automatic retry: how far the file is and "check back
    after 1 hour". High once the retries are spent (or the document service
    refused a page): which pages to upload on their own. The task refreshes
    its counts as they change and closes itself when the file is done or
    removed."""
    registry = registry or get_registry()
    now = now or datetime.now(UTC)
    wanted: set[str] = set()
    counts: dict[str, int] = {}
    batches = connection.execute(
        text(
            """
            SELECT b.batch_id, b.original_filename, b.page_count, b.batch_status, b.created_at_utc,
                   COUNT(q.queue_id) FILTER (WHERE q.queue_status NOT IN ('CANCELLED','MERGED')) AS total,
                   COUNT(q.queue_id) FILTER (WHERE q.queue_status IN ('READY','SUPPORTING','NEEDS_REVIEW')) AS done,
                   COUNT(q.queue_id) FILTER (WHERE q.queue_status='RETRY_WAIT') AS retrying,
                   COUNT(q.queue_id) FILTER (WHERE q.queue_status NOT IN
                     ('READY','SUPPORTING','NEEDS_REVIEW','CANCELLED','MERGED','RETRY_WAIT','FAILED','DEAD_LETTER'))
                     AS working,
                   COALESCE(array_agg(DISTINCT p.page ORDER BY p.page)
                            FILTER (WHERE q.queue_status IN ('FAILED','DEAD_LETTER')), ARRAY[]::int[]) AS failed_pages,
                   MIN(q.status_reason) FILTER (WHERE q.queue_status IN ('FAILED','DEAD_LETTER')) AS failed_reason
            FROM auditcore.p2_upload_batches b
            LEFT JOIN auditcore.p2_document_queue q ON q.tenant_id=b.tenant_id AND q.batch_id=b.batch_id
            LEFT JOIN LATERAL unnest(COALESCE(q.page_numbers, ARRAY[q.page_number])) AS p(page) ON true
            WHERE b.tenant_id=:t AND b.journey_id=:j
              AND b.batch_status NOT IN ('CANCELLED','COMPLETED')
            GROUP BY b.batch_id, b.original_filename, b.page_count, b.batch_status, b.created_at_utc
            ORDER BY b.created_at_utc
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    for row in batches:
        filename = row["original_filename"]
        total, done = int(row["total"] or 0), int(row["done"] or 0)
        retrying, working = int(row["retrying"] or 0), int(row["working"] or 0)
        failed_pages = [int(p) for p in (row["failed_pages"] or [])]
        split_failed = row["batch_status"] == "FAILED" and int(row["page_count"] or 0) == 0
        in_progress = retrying + working > 0 or row["batch_status"] in ("AWAITING_UPLOAD", "UPLOADED", "SPLITTING")
        age = (now - row["created_at_utc"]).total_seconds() if row["created_at_utc"] else 0.0
        if split_failed or (failed_pages and not in_progress):
            severity = "HIGH"
            if split_failed:
                title = f"{filename} could not be processed"
                description = (f"{row['failed_reason'] or 'The file could not be split into pages.'} Upload the file "
                               "again from Upload / Edit Documents, or delete this booking if nothing on it can be "
                               "used. This task closes itself once the file is processed.")
            else:
                n = len(failed_pages)
                title = f"{filename}: {n} page{'' if n == 1 else 's'} could not be processed"
                reason = (row["failed_reason"] or "").strip()
                description = ((f"{reason} " if reason else "")
                               + f"Upload {_pages_text(failed_pages)} of {filename} on "
                               f"{'its' if n == 1 else 'their'} own as {'a separate file' if n == 1 else 'separate files'} "
                               "from Upload / Edit Documents (or retry each from its card). This task closes itself "
                               "once the pages are processed; delete this booking if nothing on it can be used.")
        elif in_progress and age >= _UPLOAD_STATUS_AFTER_SECONDS:
            severity = "INFO"
            title = f"{filename}: {done} of {total} pages read, still working"
            parts = [f"{done} of {total} pages read"]
            if working:
                parts.append(f"{working} being identified or read")
            if retrying:
                parts.append(f"{retrying} waiting for an automatic retry")
            if failed_pages:
                parts.append(f"{_pages_text(failed_pages)} could not be processed so far")
            description = (", ".join(parts) + ". Nothing to do yet: check back after 1 hour. This task updates "
                           "itself, and closes when the file is done or tells you which pages to upload on "
                           "their own if any cannot be processed.")
        else:
            continue
        key = f"upload-status:{journey_id}:{row['batch_id']}"
        wanted.add(key)
        _, outcome = raise_or_refresh(
            connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
            task_type="PC_UPLOAD_STATUS", source_type="DOCUMENT",
            source_code="DOCUMENT_PROCESSING_FAILED" if severity == "HIGH" else "DOCUMENT_PROCESSING_SLOW",
            title=title, description=description,
            reference={"generatedBy": "SYSTEM", "sourceType": "DOCUMENT",
                       "sourceCode": "DOCUMENT_PROCESSING_FAILED" if severity == "HIGH" else "DOCUMENT_PROCESSING_SLOW",
                       "batchId": str(row["batch_id"]), "filename": filename, "pageNumbers": failed_pages,
                       "pages": {"total": total, "read": done, "working": working, "retrying": retrying,
                                 "failed": len(failed_pages)},
                       "retrying": severity != "HIGH"},
            severity=severity, registry=registry, evaluation_started_at=evaluation_started_at,
        )
        counts[outcome] = counts.get(outcome, 0) + 1
    closed = _resolve_prefix_except(
        connection, tenant_id=tenant_id, journey_id=journey_id, prefix=f"upload-status:{journey_id}:",
        keep=wanted, evidence={"reason": "The file was processed or removed."},
    )
    # The per-page tasks this replaced (one per failed page) close too.
    closed += _resolve_prefix_except(
        connection, tenant_id=tenant_id, journey_id=journey_id, prefix=f"processing-failed:{journey_id}:",
        keep=set(), evidence={"reason": "Replaced by the file's own status task."},
    )
    if closed:
        counts["VERIFIED"] = counts.get("VERIFIED", 0) + closed
    return counts


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
    longer applies."""
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
        for document in checked:
            if same_person(document["name"], kyc["name"]):
                continue
            template = registry.template_for_di_type(document["diType"], stage=document["stage"])
            raise_task(
                document, kind="customer",
                title=f"Replace the {template.display_name}: it is not in the customer's name",
                description=(
                    f"The {template.display_name} is in the name of {display_name(document['name'])}, but the "
                    f"customer per the {kyc_template.display_name} is {display_name(kyc['name'])}. Wrong document "
                    f"uploaded: please delete it and upload the {template.display_name} in the customer's name as "
                    f"per PAN/Aadhaar. If the name was only misread, correct it on the document instead."
                ),
                reference={"documentName": display_name(document["name"]), "customerName": display_name(kyc["name"]),
                           "referenceDocumentId": kyc["documentId"], "referenceTemplateKey": kyc_template.key},
            )

    # -- the dealership, as the booking form names it
    dealers = _named_documents(connection, tenant_id=tenant_id, journey_id=journey_id,
                               pairs=rules.dealer_name_checked)
    anchor = next((d for d in reversed(dealers) if d["pair"].startswith("booking_form.")), None)
    if anchor is not None:
        for document in dealers:
            if document["documentId"] == anchor["documentId"] or same_organisation(document["name"], anchor["name"]):
                continue
            template = registry.template_for_di_type(document["diType"], stage=document["stage"])
            raise_task(
                document, kind="dealer",
                title=f"Replace the {template.display_name}: it names another dealership",
                description=(
                    f"The {template.display_name} names the dealership {display_name(document['name'])}, but the "
                    f"Booking Docket names {display_name(anchor['name'])}. Wrong document uploaded: please delete "
                    f"it and upload the {template.display_name} issued by this dealership. If the name was only "
                    f"misread, correct it on the document instead."
                ),
                reference={"documentName": display_name(document["name"]), "dealerName": display_name(anchor["name"]),
                           "referenceDocumentId": anchor["documentId"], "referenceTemplateKey": "booking_docket"},
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
