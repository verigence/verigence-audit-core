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
   not the number of any active one of the same kind on the Journey, and not
   its total dated within a day either (a misread number) -- are made active
   again. The same bill uploaded twice, or a copy whose number or total was
   not read, stays superseded.
2. Each Journey touched -- restored, or with an open "(DMS)" / "(Tally)"
   missing-document task -- is re-materialised and recomputed, so the Deal, the
   checklist and the tasks are rebuilt from the documents now active: the open
   member tasks fold into the one "Accessory Invoice" task (the first keeps its
   comments and history, another is cancelled as merged), or close when an
   accessory invoice is on file.

Started as a p2_worker_jobs row of kind ACCESSORY_INVOICE_REPAIR. A DRY RUN
(one ``accessory_invoice_repair_item`` log line per Journey, nothing written)
unless the tag ends with "-commit". Idempotent.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from audit_core.db import set_tenant_context
from audit_core.uc03_invoice_materialization import ocr_number_key

logger = structlog.get_logger(__name__)

_KINDS = {"accessory_invoice_dms": "ACCESSORY", "accessory_invoice_tally": "ACCESSORY", "credit_note": "CREDIT_NOTE"}
_OPEN = ("READY", "IN_PROGRESS", "ACTION_COMPLETED", "VERIFYING", "AWAITING_REQUESTER_REVIEW", "RETURNED")


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
                     ORDER BY f.updated_at_utc DESC NULLS LAST LIMIT 1) AS invoice_number,
                   (SELECT f.effective_value #>> '{}' FROM auditcore.journey_document_extracted_fields f
                     WHERE f.tenant_id=e.tenant_id AND f.journey_id=e.journey_id
                       AND f.di_document_id=e.di_document_id AND f.field_key='grand_total_amount'
                     ORDER BY f.updated_at_utc DESC NULLS LAST LIMIT 1) AS grand_total,
                   (SELECT f.effective_value #>> '{}' FROM auditcore.journey_document_extracted_fields f
                     WHERE f.tenant_id=e.tenant_id AND f.journey_id=e.journey_id
                       AND f.di_document_id=e.di_document_id AND f.field_key='invoice_date'
                     ORDER BY f.updated_at_utc DESC NULLS LAST LIMIT 1) AS invoice_date
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


def _total(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "")) if value not in (None, "", "null") else None
    except InvalidOperation:
        return None


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value not in (None, "", "null") else None
    except ValueError:
        return None


def _same_bill(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """One bill: one invoice number, or one grand total to the rupee dated
    within a day (a misread number, or the DMS and Tally copy of one bill) --
    the Deal's own rule (uc03_invoice_materialization._accessory_bills)."""
    if a["number"] and a["number"] == b["number"]:
        return True
    if a["total"] is None or b["total"] is None or abs(a["total"] - b["total"]) > 1:
        return False
    return a["day"] is None or b["day"] is None or abs((a["day"] - b["day"]).days) <= 1


def to_restore(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The superseded copies that are another bill: one with a readable
    number and total that is not the same bill as any active document of the
    same kind on the Journey, nor as a copy restored before it. A copy whose
    number or total was not read stays superseded (it cannot be told apart)."""
    def facts(d: dict[str, Any]) -> dict[str, Any]:
        return {"number": _number(d["invoice_number"]), "total": _total(d.get("grand_total")),
                "day": _day(d.get("invoice_date"))}

    kept: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for d in documents:
        if d["association_status"] == "ACTIVE":
            kept.setdefault((str(d["journey_id"]), _KINDS[d["document_type_key"]]), []).append(facts(d))
    out = []
    for d in documents:
        if d["association_status"] != "SUPERSEDED":
            continue
        mine = facts(d)
        if not mine["number"] or mine["total"] is None:
            continue
        bills = kept.setdefault((str(d["journey_id"]), _KINDS[d["document_type_key"]]), [])
        if any(_same_bill(mine, other) for other in bills):
            continue
        bills.append(mine)
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


def repair_journey(connection: Connection, *, tenant_id: str, journey_id: UUID, tag: str, commit: bool
                   ) -> dict[str, Any]:
    """One Journey: the copies to restore and its open member tasks; with
    ``commit``, restored, re-materialised and recomputed -- the recompute
    folds the open member tasks into the one Accessory Invoice task
    (uc03_p2_task_producer._adopt_member_tasks) or closes them."""
    restore = to_restore(_documents(connection, tenant_id, journey_id))
    tasks = _open_missing_tasks(connection, tenant_id, journey_id)
    item = {
        "restore": [{"type": d["document_type_key"], "invoiceNumber": d["invoice_number"],
                     "total": d.get("grand_total"), "date": d.get("invoice_date"),
                     "documentId": str(d["di_document_id"])} for d in restore],
        "openTasks": [str(t["dedupe_key"]).rsplit(":", 1)[-1] for t in tasks],
    }
    if not commit or (not restore and not tasks):
        return item
    for document in restore:
        _restore(connection, tenant_id, document)
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
        totals = {"journeys": 0, "restored": 0, "open_tasks": 0, "failed": 0}
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
            totals["open_tasks"] += len(item["openTasks"])
            logger.info("accessory_invoice_repair_item", tag=tag, tenant_id=tenant_id, commit=commit,
                        journey_id=str(journey_id), restore=item["restore"], open_tasks=item["openTasks"])
        logger.info("accessory_invoice_repair_finished", tag=tag, tenant_id=tenant_id, commit=commit, **totals)
