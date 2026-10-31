"""One accessory invoice requirement, every accessory invoice kept (2026-10-31).

Product owner: "There is no separate Tally Invoice for the Accessories ... We can
remove the Accessory Tally Invoice as separate" and "There could be multiple
accessories invoices and all need to be shown and used for calculations".

The code now treats both accessory invoice types as one requirement ("Accessory
Invoice", template group ACCESSORY_INVOICE) and keeps every copy uploaded
(the requirement is repeatable). This job repairs what the old rules left behind,
per tenant:

1. Accessory invoices and credit notes superseded by a newer upload
   (``void_reason='REPLACED_BY_NEWER_UPLOAD'``) that are a DIFFERENT bill --
   their invoice number is not the number of any active one of the same kind on
   the Journey -- are made active again. A copy with the same number (the same
   bill uploaded twice) or with no readable number stays superseded.
2. The open "Upload the Accessory Invoice (DMS)" task becomes the Journey's one
   "Accessory Invoice" task (its dedupe key moves to the group key, so its
   comments and history stay); the open "(Tally)" task is cancelled as merged
   into it, or moved the same way when it is the Journey's only one.
3. Each Journey touched is re-materialised and recomputed, so the Deal, the
   checklist and the tasks are rebuilt from the documents now active (the
   recompute closes the Accessory Invoice task when one is on file).

Started as a p2_worker_jobs row of kind ACCESSORY_INVOICE_REPAIR. A DRY RUN
(one ``accessory_invoice_repair_item`` log line per Journey, nothing written)
unless the tag ends with "-commit". Idempotent.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from audit_core.db import set_tenant_context
from audit_core.uc03_invoice_materialization import ocr_number_key

logger = structlog.get_logger(__name__)

_ACCESSORY = ("accessory_invoice_dms", "accessory_invoice_tally")
_KINDS = {"accessory_invoice_dms": "ACCESSORY", "accessory_invoice_tally": "ACCESSORY", "credit_note": "CREDIT_NOTE"}
_OPEN = ("READY", "IN_PROGRESS", "ACTION_COMPLETED", "VERIFYING", "AWAITING_REQUESTER_REVIEW", "RETURNED")
_GROUP_KEY = "ACCESSORY_INVOICE"
_REASON = "Accessory invoices are one requirement (product owner, 2026-10-31)"


def _number(value: Any) -> str | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.startswith('"') else value
        except ValueError:
            pass
    return ocr_number_key(str(value)) or None


def _documents(connection: Connection, tenant_id: str, journey_id: UUID | None = None) -> list[dict[str, Any]]:
    """Accessory invoices and credit notes, active or superseded by a newer
    upload, with their invoice number."""
    rows = connection.execute(
        text(
            """
            SELECT e.journey_id, e.evidence_id, e.di_document_id, e.document_type_key, e.association_status,
                   e.void_reason, e.process_area, e.linked_at_utc,
                   (SELECT f.effective_value #>> '{}' FROM auditcore.journey_document_extracted_fields f
                     WHERE f.tenant_id=e.tenant_id AND f.journey_id=e.journey_id
                       AND f.di_document_id=e.di_document_id AND f.field_key='invoice_number'
                     ORDER BY f.updated_at_utc DESC NULLS LAST LIMIT 1) AS invoice_number
            FROM auditcore.evidence e
            WHERE e.tenant_id=:t AND e.document_type_key = ANY(:types)
              AND (e.association_status='ACTIVE'
                   OR (e.association_status='SUPERSEDED' AND e.void_reason='REPLACED_BY_NEWER_UPLOAD'))
              AND (CAST(:j AS uuid) IS NULL OR e.journey_id = CAST(:j AS uuid))
            ORDER BY e.journey_id, e.linked_at_utc, e.evidence_id
            """
        ),
        {"t": tenant_id, "types": list(_KINDS), "j": str(journey_id) if journey_id else None},
    ).mappings().all()
    return [dict(r) for r in rows]


def to_restore(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The superseded copies that are another bill: a number no active
    document of the same kind on the Journey prints, and not a number an
    earlier restored copy already brought back."""
    seen: dict[tuple[str, str], set[str]] = {}
    for d in documents:
        if d["association_status"] == "ACTIVE":
            number = _number(d["invoice_number"])
            if number:
                seen.setdefault((str(d["journey_id"]), _KINDS[d["document_type_key"]]), set()).add(number)
    out = []
    for d in documents:
        if d["association_status"] != "SUPERSEDED":
            continue
        number = _number(d["invoice_number"])
        if not number:
            continue
        numbers = seen.setdefault((str(d["journey_id"]), _KINDS[d["document_type_key"]]), set())
        if number in numbers:
            continue
        numbers.add(number)
        out.append(d)
    return out


def _restore(connection: Connection, tenant_id: str, document: dict[str, Any]) -> None:
    connection.execute(
        text(
            """
            UPDATE auditcore.evidence
            SET association_status='ACTIVE', void_reason=NULL, voided_by_actor_id=NULL, voided_at_utc=NULL
            WHERE tenant_id=:t AND evidence_id=:e AND association_status='SUPERSEDED'
            """
        ),
        {"t": tenant_id, "e": document["evidence_id"]},
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.document_capture_v2_documents
            SET capture_status='CLASSIFIED', updated_at_utc=now()
            WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d AND capture_status='SUPERSEDED'
            """
        ),
        {"t": tenant_id, "j": document["journey_id"], "d": document["di_document_id"]},
    )


def _open_missing_tasks(connection: Connection, tenant_id: str, journey_id: UUID | None = None
                        ) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT task_id, journey_id, dedupe_key, task_status
            FROM auditcore.p2_tasks
            WHERE tenant_id=:t AND source_code='DOCUMENT_MISSING' AND task_status = ANY(:open)
              AND (dedupe_key LIKE '%:accessory_invoice_dms' OR dedupe_key LIKE '%:accessory_invoice_tally')
              AND (CAST(:j AS uuid) IS NULL OR journey_id = CAST(:j AS uuid))
            ORDER BY journey_id, dedupe_key
            """
        ),
        {"t": tenant_id, "open": list(_OPEN), "j": str(journey_id) if journey_id else None},
    ).mappings().all()
    return [dict(r) for r in rows]


def task_plan(tasks: list[dict[str, Any]], *, group_key_taken: bool) -> list[tuple[dict[str, Any], str]]:
    """Per open task of one Journey: ("MOVE", task) keeps it as the group's
    task, ("CANCEL", task) retires it. The DMS task is kept before the Tally
    one; nothing moves when the group key already has a task."""
    ordered = sorted(tasks, key=lambda t: (not str(t["dedupe_key"]).endswith(":accessory_invoice_dms"),
                                           str(t["task_id"])))
    plan = []
    for index, task in enumerate(ordered):
        plan.append((task, "MOVE" if index == 0 and not group_key_taken else "CANCEL"))
    return plan


def _apply_task(connection: Connection, tenant_id: str, task: dict[str, Any], action: str) -> None:
    from audit_core.uc03_p2_tasks import record_task_event

    journey_id = UUID(str(task["journey_id"]))
    if action == "MOVE":
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_tasks
                SET dedupe_key=:key, title='Upload the Accessory Invoice',
                    reference = reference || jsonb_build_object('requirementKey', CAST(:group AS text)),
                    updated_at_utc=now()
                WHERE tenant_id=:t AND task_id=:id
                """
            ),
            {"t": tenant_id, "id": task["task_id"], "key": f"document-missing:{journey_id}:{_GROUP_KEY}",
             "group": _GROUP_KEY},
        )
        record_task_event(connection, tenant_id=tenant_id, journey_id=journey_id, task_id=task["task_id"],
                          event_type="UPDATED", actor_id="SYSTEM", actor_role_code="SYSTEM",
                          comment="Now the one Accessory Invoice task (DMS or Tally format).",
                          details={"previousDedupeKey": task["dedupe_key"], "reason": _REASON})
        return
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks
            SET task_status='CANCELLED', updated_at_utc=now(),
                completion_result = COALESCE(completion_result, '{}'::jsonb)
                    || jsonb_build_object('outcome', 'CHECK_RETIRED', 'reason', CAST(:reason AS text))
            WHERE tenant_id=:t AND task_id=:id AND task_status = ANY(:open)
            """
        ),
        {"t": tenant_id, "id": task["task_id"], "open": list(_OPEN), "reason": _REASON},
    )
    record_task_event(connection, tenant_id=tenant_id, journey_id=journey_id, task_id=task["task_id"],
                      event_type="CANCELLED", actor_id="SYSTEM", actor_role_code="SYSTEM",
                      comment="Merged into the one Accessory Invoice task.", details={"reason": _REASON})


def repair_journey(connection: Connection, *, tenant_id: str, journey_id: UUID, tag: str, commit: bool
                   ) -> dict[str, Any]:
    documents = _documents(connection, tenant_id, journey_id)
    restore = to_restore(documents)
    tasks = _open_missing_tasks(connection, tenant_id, journey_id)
    taken = connection.execute(
        text("SELECT EXISTS (SELECT 1 FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k)"),
        {"t": tenant_id, "k": f"document-missing:{journey_id}:{_GROUP_KEY}"},
    ).scalar_one()
    plan = task_plan(tasks, group_key_taken=bool(taken))
    item = {
        "restore": [{"type": d["document_type_key"], "invoiceNumber": d["invoice_number"],
                     "documentId": str(d["di_document_id"])} for d in restore],
        "tasks": [{"key": str(t["dedupe_key"]).rsplit(":", 1)[-1], "status": t["task_status"], "action": a}
                  for t, a in plan],
    }
    if not commit or (not restore and not plan):
        return item
    for document in restore:
        _restore(connection, tenant_id, document)
    for task, action in plan:
        _apply_task(connection, tenant_id, task, action)
    if restore:
        from audit_core.uc03_p2_worker import _rematerialize_stage

        for stage in sorted({str(d["process_area"] or "DELIVERY").upper() for d in restore}):
            _rematerialize_stage(connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage)
    from audit_core.uc03_p2_runtime import note_facts_changed

    note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id,
                       reason="ACCESSORY_INVOICE_REPAIR", correlation_id=f"accessory-invoice-repair:{tag}")
    return item


def _journeys(connection: Connection, tenant_id: str) -> list[UUID]:
    restore = {UUID(str(d["journey_id"])) for d in to_restore(_documents(connection, tenant_id))}
    tasks = {UUID(str(t["journey_id"])) for t in _open_missing_tasks(connection, tenant_id)}
    return sorted(restore | tasks, key=str)


def run_accessory_invoice_repair(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    commit = tag.endswith("-commit")
    for tenant_id in tenant_ids:
        totals = {"journeys": 0, "restored": 0, "tasks_moved": 0, "tasks_cancelled": 0, "failed": 0}
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                journeys = _journeys(connection, tenant_id)
        except Exception:
            logger.warning("accessory_invoice_repair_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        for journey_id in journeys:
            totals["journeys"] += 1
            try:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    item = repair_journey(connection, tenant_id=tenant_id, journey_id=journey_id, tag=tag,
                                          commit=commit)
            except Exception:
                totals["failed"] += 1
                logger.warning("accessory_invoice_repair_item_failed", tag=tag, tenant_id=tenant_id,
                               journey_id=str(journey_id), exc_info=True)
                continue
            totals["restored"] += len(item["restore"])
            totals["tasks_moved"] += sum(t["action"] == "MOVE" for t in item["tasks"])
            totals["tasks_cancelled"] += sum(t["action"] == "CANCEL" for t in item["tasks"])
            logger.info("accessory_invoice_repair_item", tag=tag, tenant_id=tenant_id, commit=commit,
                        journey_id=str(journey_id), restore=item["restore"], tasks=item["tasks"])
        logger.info("accessory_invoice_repair_finished", tag=tag, tenant_id=tenant_id, commit=commit, **totals)
