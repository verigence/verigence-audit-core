"""Audit tasks that named no document, and documents the old name check held
out of their bookings (2026-11-01).

993 open tasks read only "The audit check failed. Review the related
documents." -- the check's own task, raised with nothing to show because what
the check found is written elsewhere. And the old name check
(uc03_customer_identity_consistency) held 741 documents' values out of their
bookings, about 650 of them documents the current name rule accepts -- all 346
held receipts among them, 79 with no payment recorded.

This job, per Project:

1. cancels the open check tasks of WRONG_DOCUMENT, MANUAL_VERIFICATION and
   INVOICE_SKU_MISMATCH (``uc03_p2_task_producer.OWN_TASK_CODES``): each
   problem has its own task naming the documents. Only the check's own task
   (control:<journey>:<code>) -- never the PC's field review or name tasks;
2. runs the native checks again (forced) on the bookings whose DUPLICATE_RECEIPT
   or DUPLICATE_BOOKING task reads the fallback sentence, or whose duplicate
   booking task does not yet name the other booking: the receipt task then
   names the receipt, the pair is raised again with the booking it matches
   and on what;
3. checks every booking holding a document again by the current name rule;
   what is released reaches the booking on its recompute, which is requested.
   The dry run checks each booking in a savepoint and rolls it back, logging
   what would be released and the released receipts with no payment yet.

Started as worker job ``AUDIT_TASK_CLEANUP``. A DRY RUN (logged, nothing
written) unless the tag ends with "-commit". Idempotent: a second run finds
no task to cancel, no fallback task, and no hold to release.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_task_producer import (
    _OPEN,
    OWN_TASK_CODES,
    OWN_TASK_RESULT,
    _json,
)
from audit_core.uc03_p2_tasks import record_task_event

logger = structlog.get_logger(__name__)

JOB = "AUDIT_TASK_CLEANUP"
FALLBACK = "The audit check failed. Review the related documents."
_RERUN_CODES = ("DUPLICATE_RECEIPT", "DUPLICATE_BOOKING")
_RECEIPTS = ("dealer_receipt", "payment_receipt")


def _own_task_shells(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(
        text(
            """
            SELECT task_id, journey_id, source_code, task_status FROM auditcore.p2_tasks
            WHERE tenant_id=:t AND dedupe_key LIKE 'control:%' AND source_code = ANY(:codes)
              AND task_status = ANY(:open)
            ORDER BY created_at_utc
            FOR UPDATE
            """
        ),
        {"t": tenant_id, "codes": sorted(OWN_TASK_CODES), "open": list(_OPEN)},
    ).mappings().all()]


def _cancel_shells(connection: Connection, tenant_id: str, tasks: list[dict[str, Any]], tag: str) -> int:
    if not tasks:
        return 0
    cancelled = connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks
            SET task_status='CANCELLED',
                completion_result=COALESCE(completion_result, '{}'::jsonb) || CAST(:result AS jsonb),
                updated_at_utc=now()
            WHERE tenant_id=:t AND task_id = ANY(:ids) AND task_status = ANY(:open)
              AND dedupe_key LIKE 'control:%' AND source_code = ANY(:codes)
            RETURNING task_id, journey_id, source_code
            """
        ),
        {"t": tenant_id, "ids": [t["task_id"] for t in tasks], "open": list(_OPEN),
         "codes": sorted(OWN_TASK_CODES), "result": _json(OWN_TASK_RESULT)},
    ).mappings().all()
    before = {str(t["task_id"]): str(t["task_status"]) for t in tasks}
    for row in cancelled:
        record_task_event(
            connection, tenant_id=tenant_id, journey_id=UUID(str(row["journey_id"])),
            task_id=UUID(str(row["task_id"])), event_type="CANCELLED", actor_id="SYSTEM",
            actor_role_code="SYSTEM",
            details={**OWN_TASK_RESULT, "sourceCode": row["source_code"],
                     "previousStatus": before.get(str(row["task_id"])), "job": JOB, "tag": tag},
        )
    return len(cancelled)


def _fallback_journeys(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(
        text(
            """
            SELECT journey_id, array_agg(DISTINCT source_code ORDER BY source_code) AS codes
            FROM auditcore.p2_tasks
            WHERE tenant_id=:t AND dedupe_key LIKE 'control:%' AND source_code = ANY(:codes)
              AND task_status = ANY(:open)
              AND (description = :fallback
                   -- a duplicate booking task that does not yet name the other booking (2026-11-01)
                   OR (source_code = 'DUPLICATE_BOOKING' AND description NOT LIKE 'This booking matches the earlier booking%'))
            GROUP BY journey_id ORDER BY journey_id
            """
        ),
        {"t": tenant_id, "codes": list(_RERUN_CODES), "open": list(_OPEN), "fallback": FALLBACK},
    ).mappings().all()]


def _rerun_native(connection: Connection, tenant_id: str, journey_id: UUID, tag: str) -> list[str]:
    from audit_core.uc03_p2_controls import delivery_active
    from audit_core.uc03_p2_runtime import enqueue_work

    units = ["NATIVE:BOOKING"]
    if delivery_active(connection, tenant_id=tenant_id, journey_id=journey_id):
        units.append("NATIVE:DELIVERY")
    for unit in units:
        enqueue_work(connection, tenant_id=tenant_id, journey_id=journey_id, work_type="CONTROL_EVALUATE",
                     work_key=f"unit:{journey_id}:{unit}", payload={"unit": unit, "force": True},
                     correlation_id=f"{JOB.lower()}:{tag}")
    return units


def _held_journeys(connection: Connection, tenant_id: str) -> list[UUID]:
    return [UUID(str(j)) for j in connection.execute(
        text(
            """
            SELECT DISTINCT journey_id FROM auditcore.evidence
            WHERE tenant_id=:t AND association_status='ACTIVE' AND identity_check_status='HELD'
            ORDER BY journey_id
            """
        ),
        {"t": tenant_id},
    ).scalars().all()]


def _held(connection: Connection, tenant_id: str, journey_id: UUID) -> dict[str, dict[str, Any]]:
    return {str(r["evidence_id"]): dict(r) for r in connection.execute(
        text(
            """
            SELECT evidence_id, di_document_id, document_type_key FROM auditcore.evidence
            WHERE tenant_id=:t AND journey_id=:j AND association_status='ACTIVE' AND identity_check_status='HELD'
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()}


def _receipt_without_payment(connection: Connection, tenant_id: str, journey_id: UUID,
                             document_id: Any) -> dict[str, Any] | None:
    """A released receipt with no payment recorded: its amount and payer as read."""
    row = connection.execute(
        text(
            """
            SELECT
              (SELECT f.effective_value #>> '{}' FROM auditcore.journey_document_extracted_fields f
                WHERE f.tenant_id=:t AND f.di_document_id=:d AND f.field_key='amount_paid'
                ORDER BY f.confidence_score DESC NULLS LAST LIMIT 1) AS amount,
              (SELECT f.effective_value #>> '{}' FROM auditcore.journey_document_extracted_fields f
                WHERE f.tenant_id=:t AND f.di_document_id=:d AND f.field_key='customer_name'
                ORDER BY f.confidence_score DESC NULLS LAST LIMIT 1) AS payer,
              EXISTS (SELECT 1 FROM auditcore.payments p
                       WHERE p.tenant_id=:t AND p.journey_id=:j AND p.source_di_document_id=:d) AS paid
            """
        ),
        {"t": tenant_id, "j": journey_id, "d": document_id},
    ).mappings().one()
    if row["paid"]:
        return None
    return {"documentId": str(document_id), "amount": row["amount"], "payer": row["payer"]}


def _amount(value: Any) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "").replace("₹", "").strip())
    except (InvalidOperation, ValueError):
        return Decimal(0)


def recheck_holds(connection: Connection, *, tenant_id: str, journey_id: UUID, commit: bool) -> dict[str, Any]:
    """Check one booking's holds again by the current name rule. Without
    ``commit`` the check runs in a savepoint that is rolled back."""
    from audit_core.uc03_customer_identity_consistency import (
        sync_customer_identity_consistency,
    )

    before = _held(connection, tenant_id, journey_id)
    savepoint = connection.begin_nested()
    try:
        result = sync_customer_identity_consistency(connection, tenant_id=tenant_id, journey_id=journey_id,
                                                    correlation_id=f"{JOB.lower()}")
        after = _held(connection, tenant_id, journey_id)
    except Exception:
        savepoint.rollback()
        raise
    if result.get("error"):
        savepoint.rollback()
        return {"error": True}
    released = [before[e] for e in before if e not in after]
    newly_held = [after[e] for e in after if e not in before]
    unpaid = [r for r in (
        _receipt_without_payment(connection, tenant_id, journey_id, d["di_document_id"])
        for d in released if d["document_type_key"] in _RECEIPTS) if r]
    if commit:
        savepoint.commit()
    else:
        savepoint.rollback()
    return {
        "heldBefore": len(before),
        "released": len(released),
        "releasedByType": dict(Counter(str(d["document_type_key"]) for d in released)),
        "stillHeld": len(after) - len(newly_held),
        "newlyHeld": len(newly_held),
        "unpaidReceipts": unpaid,
    }


def clean_tenant(engine: Engine, *, tenant_id: str, tag: str, commit: bool) -> dict[str, Any]:
    totals: dict[str, Any] = {"shells": 0, "shellsByCode": {}, "cancelled": 0, "rerunJourneys": 0,
                              "rerunByCode": {}, "heldJourneys": 0, "heldBefore": 0, "released": 0,
                              "releasedByType": Counter(), "stillHeld": 0, "newlyHeld": 0, "unpaidReceipts": 0,
                              "unpaidAmount": Decimal(0), "recomputed": 0, "failed": 0}
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        shells = _own_task_shells(connection, tenant_id)
        totals["shells"] = len(shells)
        totals["shellsByCode"] = dict(Counter(str(t["source_code"]) for t in shells))
        if commit:
            totals["cancelled"] = _cancel_shells(connection, tenant_id, shells, tag)

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        fallback = _fallback_journeys(connection, tenant_id)
        totals["rerunJourneys"] = len(fallback)
        totals["rerunByCode"] = dict(Counter(c for row in fallback for c in row["codes"]))
        for row in fallback:
            if commit:
                _rerun_native(connection, tenant_id, UUID(str(row["journey_id"])), tag)
        held_journeys = _held_journeys(connection, tenant_id)
    totals["heldJourneys"] = len(held_journeys)

    from audit_core.uc03_p2_runtime import note_facts_changed

    for journey_id in held_journeys:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                outcome = recheck_holds(connection, tenant_id=tenant_id, journey_id=journey_id, commit=commit)
                if outcome.get("error"):
                    totals["failed"] += 1
                    logger.warning("audit_task_cleanup_hold_failed", tag=tag, tenant_id=tenant_id,
                                   journey_id=str(journey_id))
                    continue
                reference = connection.execute(
                    text("SELECT journey_reference FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
                    {"t": tenant_id, "j": journey_id},
                ).scalar_one_or_none()
                changed = outcome["released"] or outcome["newlyHeld"]
                if commit and changed:
                    note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id,
                                       reason="DOCUMENT_HOLD_RECHECKED", correlation_id=f"{JOB.lower()}:{tag}")
                    totals["recomputed"] += 1
        except Exception:
            totals["failed"] += 1
            logger.warning("audit_task_cleanup_hold_failed", tag=tag, tenant_id=tenant_id,
                           journey_id=str(journey_id), exc_info=True)
            continue
        for key in ("heldBefore", "released", "stillHeld", "newlyHeld"):
            totals[key] += outcome[key]
        totals["releasedByType"].update(outcome["releasedByType"])
        totals["unpaidReceipts"] += len(outcome["unpaidReceipts"])
        totals["unpaidAmount"] += sum((_amount(r["amount"]) for r in outcome["unpaidReceipts"]), Decimal(0))
        if changed:
            logger.info("audit_task_cleanup_hold", tag=tag, tenant_id=tenant_id, commit=commit,
                        journey_id=str(journey_id), reference=reference, released=outcome["released"],
                        released_by_type=outcome["releasedByType"], still_held=outcome["stillHeld"],
                        newly_held=outcome["newlyHeld"], unpaid_receipts=outcome["unpaidReceipts"])
    totals["releasedByType"] = dict(totals["releasedByType"])
    totals["unpaidAmount"] = str(totals["unpaidAmount"])
    return totals


def run_audit_task_cleanup(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    commit = tag.endswith("-commit")
    for tenant_id in tenant_ids:
        try:
            totals = clean_tenant(engine, tenant_id=tenant_id, tag=tag, commit=commit)
        except Exception:
            logger.warning("audit_task_cleanup_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        logger.info("audit_task_cleanup_finished", tag=tag, tenant_id=tenant_id, commit=commit, **totals)
