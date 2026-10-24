"""Read every Journey's invoices again and re-set the invoice-owned figures
(2026-10-24).

The invoice reader changed how it finds the GST-inclusive vehicle price: an
invoice with no printed GST rate used to lose its ex-showroom line while its
discounts were kept, so the Deal netted a discount against nothing. Invoices
already read keep the old result until they are read again; this does that
for every Journey that has an invoice on file, from the figures already
stored (nothing is sent back to extraction).

Started from outside with AUDIT_CORE_INVOICE_RESCAN_TAG on the worker. A DRY
RUN (one ``invoice_rescan_item`` log line per Journey: ex-showroom now ->
ex-showroom after, nothing written) unless the tag ends with "-commit".
Idempotent: running it again changes nothing that is already right.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from audit_core.db import set_tenant_context
from audit_core.uc03_deal_source_history import record_source_value
from audit_core.uc03_invoice_materialization import (
    CUSTOMER_FAMILIES,
    load_invoice_readings,
    plausible_from,
    select_invoices,
    sync_invoice_actuals,
)

logger = structlog.get_logger(__name__)

_EX_SHOWROOM = "ex_showroom_price"


def _journeys_with_invoices(connection: Connection, tenant_id: str) -> list[UUID]:
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT journey_id FROM auditcore.invoice_review_values
            WHERE tenant_id = :tenant_id ORDER BY journey_id
            """
        ),
        {"tenant_id": tenant_id},
    ).scalars().all()
    return [UUID(str(r)) for r in rows]


def _ex_showroom_now(connection: Connection, tenant_id: str, journey_id: UUID) -> Decimal | None:
    value = connection.execute(
        text(
            """
            SELECT actual_amount FROM auditcore.commercial_lines
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id AND component_key = :key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "key": _EX_SHOWROOM},
    ).scalar()
    return None if value is None else Decimal(str(value))


def _ex_showroom_standard(connection: Connection, tenant_id: str, journey_id: UUID) -> Decimal | None:
    """The price list's ex-showroom for the Journey's resolved SKU and date."""
    value = connection.execute(
        text(
            """
            SELECT standard_amount FROM auditcore.commercial_lines
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id AND component_key = :key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "key": _EX_SHOWROOM},
    ).scalar()
    return None if value is None else Decimal(str(value))


def _ex_showroom_source(connection: Connection, tenant_id: str, journey_id: UUID) -> str | None:
    value = connection.execute(
        text(
            """
            SELECT source_reference FROM auditcore.commercial_lines
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id AND component_key = :key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "key": _EX_SHOWROOM},
    ).scalar()
    return None if value is None else str(value).split(":", 1)[0]


def _detail(selection: dict[str, Any], before_source: str | None) -> dict[str, Any]:
    """What the new ex-showroom was worked from, for reading the log."""
    holders = (selection.get("lineDocuments") or {}).get(_EX_SHOWROOM) or []
    rep = holders[0] if holders else None
    vehicle = (rep or {}).get("vehicle") or {}
    raw = (rep or {}).get("_raw") or {}
    return {
        "beforeSource": before_source,
        "basis": vehicle.get("gstBasis"), "rate": vehicle.get("gstRate"),
        "selling": vehicle.get("sellingPrice"), "sellingFrom": vehicle.get("sellingPriceSource"),
        "taxable": vehicle.get("taxable"), "discountPrinted": vehicle.get("discountPrinted"),
        "grandTotal": raw.get("grand_total_amount"), "tcs": raw.get("tcs_amount"),
        "addOnLines": sorted(k for k in (rep or {}).get("lines", {}) if k != _EX_SHOWROOM),
        "issues": [i["code"] for i in (rep or {}).get("issues", [])],
    }


def _on_phase_two(connection: Connection, tenant_id: str, journey_id: UUID) -> bool:
    """Only a Journey that already has its Phase 2 runtime row is told its
    facts changed; asking would otherwise create one."""
    return connection.execute(
        text("SELECT 1 FROM auditcore.p2_journey_runtime WHERE tenant_id = :t AND journey_id = :j"),
        {"t": tenant_id, "j": journey_id},
    ).first() is not None


def rescan_journey(
    connection: Connection, *, tenant_id: str, journey_id: UUID, tag: str, commit: bool,
) -> dict[str, Any]:
    """One Journey: what its ex-showroom is now, what the invoices on file
    give, and (``commit``) the re-set figures with downstream told."""
    readings = load_invoice_readings(connection, tenant_id=tenant_id, journey_id=journey_id)
    selection = select_invoices(readings, plausible_from=plausible_from(
        connection, tenant_id=tenant_id, journey_id=journey_id))
    before = _ex_showroom_now(connection, tenant_id, journey_id)
    after = selection["lines"].get(_EX_SHOWROOM)
    item = {"invoices": len(readings), "before": before, "after": after,
            "changed": after is not None and (before is None or abs(before - after) > Decimal("0.5")),
            "standard": _ex_showroom_standard(connection, tenant_id, journey_id),
            "detail": _detail(selection, _ex_showroom_source(connection, tenant_id, journey_id))}
    if not commit:
        return item
    for reading in readings:
        if reading["family"] not in CUSTOMER_FAMILIES:
            continue
        for component_key, amount in reading["lines"].items():
            record_source_value(
                connection, tenant_id=tenant_id, journey_id=journey_id, line_kind="COMMERCIAL",
                component_key=component_key, source_document_type=reading["documentType"], amount=amount,
                evidence_id=reading["evidenceId"], document_id=reading["documentId"],
            )
        for discount_key, entry in reading["discounts"].items():
            record_source_value(
                connection, tenant_id=tenant_id, journey_id=journey_id, line_kind="DISCOUNT",
                component_key=discount_key, source_document_type=reading["documentType"], amount=entry["amount"],
                evidence_id=reading["evidenceId"], document_id=reading["documentId"],
            )
    sync_invoice_actuals(connection, tenant_id=tenant_id, journey_id=journey_id,
                         correlation_id=f"invoice-rescan:{tag}")
    if _on_phase_two(connection, tenant_id, journey_id):
        from audit_core.uc03_p2_runtime import note_facts_changed

        note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id,
                           reason="invoice_rescan", correlation_id=f"invoice-rescan:{tag}")
    return item


def run_invoice_rescan(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    commit = tag.endswith("-commit")
    for tenant_id in tenant_ids:
        totals = {"journeys": 0, "had_no_ex_showroom": 0, "has_ex_showroom_after": 0, "changed": 0, "failed": 0}
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                journeys = _journeys_with_invoices(connection, tenant_id)
        except Exception:
            logger.warning("invoice_rescan_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        for journey_id in journeys:
            totals["journeys"] += 1
            try:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    item = rescan_journey(connection, tenant_id=tenant_id, journey_id=journey_id,
                                          tag=tag, commit=commit)
            except Exception:
                totals["failed"] += 1
                logger.warning("invoice_rescan_item_failed", tag=tag, tenant_id=tenant_id,
                               journey_id=str(journey_id), exc_info=True)
                continue
            totals["had_no_ex_showroom"] += item["before"] is None
            totals["has_ex_showroom_after"] += item["after"] is not None
            totals["changed"] += bool(item["changed"])
            logger.info("invoice_rescan_item", tag=tag, tenant_id=tenant_id, commit=commit,
                        journey_id=str(journey_id), invoices=item["invoices"],
                        ex_showroom_before=str(item["before"]), ex_showroom_after=str(item["after"]),
                        changed=item["changed"], ex_showroom_standard=str(item["standard"]), detail=str(item["detail"]))
        logger.info("invoice_rescan_finished", tag=tag, tenant_id=tenant_id, commit=commit, **totals)
