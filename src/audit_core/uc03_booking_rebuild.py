"""Bookings not built again from their documents (2026-11-01).

A booking's values -- its payments, the vehicle, the customer's name and
identity, the commercial lines, finance and insurance -- are built from the
documents on file by the stage recompute (uc03_p2_worker._materialize_journey).
A document held by the old name check is left out of that build. Until
2026-11-01 nothing asked for the booking to be built again when the check
released a hold (the fact fingerprint does not read a document's hold, and in
P2 the check runs after the build), so a released receipt stayed without a
payment: UTK-SB-261028-001's ₹30,36,490 receipt, ADI-BM-261026-005's
₹11,00,000, with the booking still waiting for its minimum payment.

This job, per Project, recomputes every booking with documents on file in a
savepoint that is ALWAYS rolled back (the stage recompute itself,
uc03_p2_worker.settle_journey) and compares what it would write with what is
stored: payments, the vehicle, the customer, the booking, the commercial
lines, discounts, add-ons, finance, insurance, registration, vehicle and
delivery records, and the booking's open tasks (raised and closed). When and
by whom a row was written is not a change, nor a resolved vehicle's names
(the SKU check writes them after the recompute). It logs per booking what
would change. With a
tag ending in "-commit" it requests the stage recompute of the bookings that
would change -- the worker's own recompute then builds them, exactly as after
any document change. The job never writes a booking's values itself, and a
booking whose build would change nothing is not touched.

Started as worker job ``BOOKING_REBUILD``, over every booking or only the
journey references the job lists in ``reports``. A DRY RUN unless the tag ends
with "-commit". Idempotent: once the recomputes have run, a second run finds
nothing to change.
"""
from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

JOB = "BOOKING_REBUILD"

# The tables the build writes, keyed by their row id.
_TABLES: dict[str, str] = {
    "payments": "payment_id",
    "journey_products": "journey_product_id",
    "bookings": "booking_id",
    "commercial_lines": "commercial_line_id",
    "discount_applications": "discount_application_id",
    "journey_addons": "journey_addon_id",
    "finance_records": "finance_record_id",
    "insurance_records": "insurance_record_id",
    "registration_records": "registration_record_id",
    "vehicle_records": "vehicle_record_id",
    "deliveries": "delivery_id",
}
# When a row was written is not what it says.
_STAMPS = ("updated_at_utc", "version_no", "created_at_utc")


def _rows(connection: Connection, sql: str, params: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in connection.execute(text(sql), params).mappings().all():
        body = row["body"]
        out[str(row["id"])] = body if isinstance(body, dict) else json.loads(body)
    return out


def snapshot(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, dict[str, dict[str, Any]]]:
    """What the booking's tables say now, less the write stamps."""
    strip = "".join(f" - '{column}'" for column in _STAMPS)
    params = {"t": tenant_id, "j": journey_id}
    tables = {
        table: _rows(connection, f"SELECT {key}::text AS id, (to_jsonb(x){strip}) AS body "
                                 f"FROM auditcore.{table} x WHERE x.tenant_id=:t AND x.journey_id=:j", params)
        for table, key in _TABLES.items()
    }
    # The vehicle's SKU match records when it was decided; the build does not decide it.
    for body in tables["journey_products"].values():
        evidence = body.get("match_evidence")
        if isinstance(evidence, dict):
            evidence.pop("decidedAtUtc", None)
    tables["customers"] = _rows(
        connection,
        f"""
        SELECT c.customer_id::text AS id, (to_jsonb(c){strip}) AS body
        FROM auditcore.customers c JOIN auditcore.journeys j
          ON j.tenant_id=c.tenant_id AND j.customer_id=c.customer_id
        WHERE j.tenant_id=:t AND j.journey_id=:j
        """,
        params,
    )
    return tables


def _amount(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def _payment(body: dict[str, Any]) -> dict[str, Any]:
    return {"amount": body.get("amount"), "receiptNumber": body.get("receipt_number"),
            "receiptDate": body.get("receipt_date"), "documentId": body.get("source_di_document_id")}


# The vehicle's names, once its SKU is resolved, are the price list's: the SKU
# check (a control unit, after the recompute) writes them again, so the
# recompute writing the booking docket's own names first is not a change.
_SKU_OWNED = frozenset({
    "model_code_snapshot", "model_name_snapshot", "variant_code_snapshot", "variant_name_snapshot",
    "colour_code_snapshot", "colour_name_snapshot", "selection_source", "selection_status", "selection_method",
    "selection_score", "sku_resolution_remarks", "vehicle_category", "match_basis", "match_evidence",
})


def _counts(table: str, column: str, row: dict[str, Any]) -> bool:
    """Whether a column's change is a change of what the booking says: not when
    or by whom it was written, nor a vehicle name the SKU check owns."""
    if column == "version_no" or column.endswith(("_at_utc", "_at", "_actor_id")):
        return False
    return not (table == "journey_products" and row.get("product_sku_id") and column in _SKU_OWNED)


def compare(before: dict[str, dict[str, dict[str, Any]]],
            after: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """Per table, the rows the build would add, remove or change (and which
    columns). Payments and the vehicle are listed with their values."""
    changes: dict[str, Any] = {}
    for table, new in after.items():
        old = before.get(table, {})
        added = [new[i] for i in new if i not in old]
        removed = [old[i] for i in old if i not in new]
        changed = {}
        for i in new:
            if i not in old or old[i] == new[i]:
                continue
            columns = sorted(k for k in set(old[i]) | set(new[i])
                             if old[i].get(k) != new[i].get(k) and _counts(table, k, old[i]))
            if columns:
                changed[i] = columns
        if not (added or removed or changed):
            continue
        entry: dict[str, Any] = {"added": len(added), "removed": len(removed), "changed": len(changed)}
        if table == "payments":
            entry["addedPayments"] = [_payment(b) for b in added]
            entry["removedPayments"] = [_payment(b) for b in removed]
            entry["changedPayments"] = [
                {**_payment(new[i]), "columns": columns, "amountBefore": old[i].get("amount")}
                for i, columns in changed.items()
            ]
        elif table == "journey_products":
            entry["vehicle"] = [
                {"before": {k: old.get(i, {}).get(k) for k in ("model_name_snapshot", "variant_name_snapshot",
                                                               "colour_name_snapshot")},
                 "after": {k: new[i].get(k) for k in ("model_name_snapshot", "variant_name_snapshot",
                                                      "colour_name_snapshot")}}
                for i in new if i not in old or i in changed
            ]
        else:
            entry["columns"] = sorted({c for columns in changed.values() for c in columns})
            # What each changed row says before and after (2026-11-01): a change
            # is judged on its values, not on a column name.
            entry["values"] = [
                {"key": _row_label(old[i]), **{c: {"before": _shown(old[i].get(c)), "after": _shown(new[i].get(c))}
                                               for c in columns}}
                for i, columns in list(changed.items())[:5]
            ]
            if added:
                entry["addedRows"] = [_row_label(b) for b in added[:5]]
        changes[table] = entry
    return changes


def _row_label(body: dict[str, Any]) -> str | None:
    """The row's own name, when it has one (a discount's key, a line's component)."""
    for key in ("discount_key", "component_key", "addon_type_code", "finance_type_code", "insurer_name"):
        if body.get(key):
            return str(body[key])
    return None


def _shown(value: Any) -> Any:
    text_value = json.dumps(value, default=str) if isinstance(value, (dict, list)) else value
    return text_value[:160] if isinstance(text_value, str) else text_value


_OPEN_TASKS = ("READY", "IN_PROGRESS", "RETURNED", "ACTION_COMPLETED", "VERIFYING")


def _open_tasks(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, tuple[str, str]]:
    return {str(r[0]): (str(r[1]), str(r[2] or "")) for r in connection.execute(
        text("SELECT dedupe_key, title, source_code FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j "
             "AND task_status = ANY(:open)"),
        {"t": tenant_id, "j": journey_id, "open": list(_OPEN_TASKS)},
    ).all()}


def rebuild_check(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """Recompute the booking in a savepoint, always rolled back -- the stage
    recompute itself (uc03_p2_worker.settle_journey: the build, the booking
    date, the customer's name, the stage and the tasks that follow) -- and say
    what it would change, the tasks it would raise and close included (a
    receipt whose amount was not read gets its task, 2026-11-01). The checks
    the recompute then asks for (control units) are not run here."""
    from audit_core.uc03_p2_worker import settle_journey

    before = snapshot(connection, tenant_id=tenant_id, journey_id=journey_id)
    tasks_before = _open_tasks(connection, tenant_id=tenant_id, journey_id=journey_id)
    savepoint = connection.begin_nested()
    try:
        settle_journey(connection, tenant_id=tenant_id, journey_id=journey_id)
        after = snapshot(connection, tenant_id=tenant_id, journey_id=journey_id)
        tasks_after = _open_tasks(connection, tenant_id=tenant_id, journey_id=journey_id)
    finally:
        savepoint.rollback()
    changes = compare(before, after)
    raised = sorted(f"{code}: {title}" for key, (title, code) in tasks_after.items() if key not in tasks_before)
    closed = sorted(f"{code}: {title}" for key, (title, code) in tasks_before.items() if key not in tasks_after)
    if raised or closed:
        changes["tasks"] = {"raised": raised, "closed": closed}
    return changes


def _journeys(connection: Connection, tenant_id: str, references: list[str] | None = None) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(
        text(
            """
            SELECT j.journey_id, j.journey_reference FROM auditcore.journeys j
            WHERE j.tenant_id=:t AND EXISTS (
                SELECT 1 FROM auditcore.evidence e
                WHERE e.tenant_id=j.tenant_id AND e.journey_id=j.journey_id AND e.association_status='ACTIVE')
              AND (CAST(:refs AS text[]) IS NULL OR j.journey_reference = ANY(CAST(:refs AS text[])))
            ORDER BY j.journey_reference
            """
        ),
        {"t": tenant_id, "refs": list(references) if references else None},
    ).mappings().all()]


def rebuild_tenant(engine: Engine, *, tenant_id: str, tag: str, commit: bool,
                   references: list[str] | None = None) -> dict[str, Any]:
    """Every booking of the Project with documents on file, or only those of
    ``references`` (journey references)."""
    from audit_core.uc03_p2_runtime import note_facts_changed

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        journeys = _journeys(connection, tenant_id, references)
    totals: dict[str, Any] = {"journeys": len(journeys), "wouldChange": 0, "byTable": Counter(),
                              "paymentsAdded": 0, "paymentsAddedAmount": Decimal("0.00"), "paymentsRemoved": 0,
                              "paymentsRemovedAmount": Decimal("0.00"), "paymentsChanged": 0, "vehicleChanged": 0,
                              "tasksRaised": 0, "tasksClosed": 0, "raisedByCode": Counter(),
                              "closedByCode": Counter(),
                              "recomputeRequested": 0, "failed": 0}
    for journey in journeys:
        journey_id = UUID(str(journey["journey_id"]))
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                # Never wait on the worker building the same booking; that one is retried here next run.
                connection.execute(text("SET LOCAL lock_timeout = '5s'"))
                connection.execute(text("SET LOCAL statement_timeout = '60s'"))
                changes = rebuild_check(connection, tenant_id=tenant_id, journey_id=journey_id)
                if commit and changes:
                    note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id,
                                       reason="BOOKING_REBUILD", correlation_id=f"{JOB.lower()}:{tag}")
                    totals["recomputeRequested"] += 1
        except Exception:
            totals["failed"] += 1
            logger.warning("booking_rebuild_failed", tag=tag, tenant_id=tenant_id, journey_id=str(journey_id),
                           reference=journey["journey_reference"], exc_info=True)
            continue
        if not changes:
            continue
        totals["wouldChange"] += 1
        totals["byTable"].update(changes.keys())
        payments = changes.get("payments", {})
        totals["paymentsAdded"] += len(payments.get("addedPayments", []))
        totals["paymentsAddedAmount"] += sum((_amount(p["amount"]) for p in payments.get("addedPayments", [])),
                                             Decimal(0))
        totals["paymentsRemoved"] += len(payments.get("removedPayments", []))
        totals["paymentsRemovedAmount"] += sum((_amount(p["amount"]) for p in payments.get("removedPayments", [])),
                                               Decimal(0))
        totals["paymentsChanged"] += len(payments.get("changedPayments", []))
        totals["vehicleChanged"] += 1 if "journey_products" in changes else 0
        tasks = changes.get("tasks", {})
        totals["tasksRaised"] += len(tasks.get("raised", []))
        totals["tasksClosed"] += len(tasks.get("closed", []))
        totals["raisedByCode"].update(t.split(":", 1)[0] for t in tasks.get("raised", []))
        totals["closedByCode"].update(t.split(":", 1)[0] for t in tasks.get("closed", []))
        logger.info("booking_rebuild_item", tag=tag, tenant_id=tenant_id, commit=commit, journey_id=str(journey_id),
                    reference=journey["journey_reference"], changes=changes)
    totals["byTable"] = dict(totals["byTable"])
    totals["raisedByCode"] = dict(totals["raisedByCode"])
    totals["closedByCode"] = dict(totals["closedByCode"])
    totals["paymentsAddedAmount"] = str(totals["paymentsAddedAmount"])
    totals["paymentsRemovedAmount"] = str(totals["paymentsRemovedAmount"])
    return totals


def run_booking_rebuild(engine: Engine, tag: str, tenant_ids: list[str], references: list[str] | None = None) -> None:
    """``references``: journey references to limit the run to (a job's
    ``reports``); none means every booking."""
    commit = tag.endswith("-commit")
    for tenant_id in tenant_ids:
        try:
            totals = rebuild_tenant(engine, tenant_id=tenant_id, tag=tag, commit=commit, references=references)
        except Exception:
            logger.warning("booking_rebuild_tenant_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        logger.info("booking_rebuild_finished", tag=tag, tenant_id=tenant_id, commit=commit, **totals)
