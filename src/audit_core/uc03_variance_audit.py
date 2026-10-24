"""One look at every Journey's price variance, from both places it is shown
(2026-10-24).

The Journey list reads a stored copy of the Deal's current-vs-standard figure;
the Deal tab computes it. This builds the Deal for every Journey, as the Deal
tab does, and logs it beside the stored copy, with the components that differ
from the price list, so a variance a reviewer cannot explain can be traced to
its line (the invoice's price, a discount, a missing standard) instead of
argued about. Read-only. Started from outside with
AUDIT_CORE_VARIANCE_AUDIT_TAG on the worker.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_journey360 import deal

logger = structlog.get_logger(__name__)
_TOLERANCE = Decimal(100)


def _dec(value: Any) -> Decimal | None:
    try:
        return None if value in (None, "") else Decimal(str(value))
    except InvalidOperation:
        return None


def _journeys(connection: Connection, tenant_id: str) -> list[UUID]:
    rows = connection.execute(
        text("SELECT journey_id FROM auditcore.p2_journey_runtime WHERE tenant_id = :t ORDER BY journey_id"),
        {"t": tenant_id},
    ).scalars().all()
    return [UUID(str(r)) for r in rows]


def _stored_copy(connection: Connection, tenant_id: str, journey_id: UUID) -> tuple[Decimal | None, bool]:
    row = connection.execute(
        text(
            """
            SELECT price_variance, price_variance_fact_version = fact_version AS current
            FROM auditcore.p2_journey_runtime WHERE tenant_id = :t AND journey_id = :j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one_or_none()
    if row is None:
        return None, False
    return _dec(row["price_variance"]), bool(row["current"])


def audit_journey(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    view = deal(connection, tenant_id=tenant_id, journey_id=journey_id)
    summary = view["summary"]
    rows = [r for group in view.get("categories") or [] for r in group.get("components") or []]
    rows += view.get("discounts") or []
    off = []
    for row in rows:
        std, cur = _dec(row.get("standard")), _dec(row.get("current"))
        if std is None and cur is None:
            continue
        if std is None or cur is None or abs(cur - std) > _TOLERANCE:
            off.append({"key": row.get("key"), "standard": None if std is None else str(std),
                        "current": None if cur is None else str(cur), "flags": row.get("flags") or []})
    stored, stored_current = _stored_copy(connection, tenant_id, journey_id)
    deal_variance = _dec((summary.get("variance") or {}).get("currentVsStandard"))
    return {
        "dealVariance": deal_variance, "storedVariance": stored, "storedIsCurrent": stored_current,
        "listAgreesWithDeal": (stored or Decimal(0)) == (deal_variance or Decimal(0)),
        "invoiceIsSourceOfTruth": bool(summary.get("invoiceIsSourceOfTruth")),
        "vehicleInvoiceOnFile": bool(summary.get("vehicleInvoiceOnFile")),
        "standardUnavailable": view.get("standardUnavailable"),
        "netStandard": (summary.get("net") or {}).get("standard"),
        "netCurrent": (summary.get("net") or {}).get("current"),
        "differing": off,
    }


def run_variance_audit(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    for tenant_id in tenant_ids:
        totals = {"journeys": 0, "failed": 0, "list_disagrees": 0, "stored_stale": 0, "over_100": 0}
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                journeys = _journeys(connection, tenant_id)
        except Exception:
            logger.warning("variance_audit_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        for journey_id in journeys:
            totals["journeys"] += 1
            try:
                with engine.begin() as connection:
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    set_tenant_context(connection, tenant_id)
                    item = audit_journey(connection, tenant_id=tenant_id, journey_id=journey_id)
            except Exception:
                totals["failed"] += 1
                logger.warning("variance_audit_item_failed", tag=tag, tenant_id=tenant_id,
                               journey_id=str(journey_id), exc_info=True)
                continue
            totals["list_disagrees"] += not item["listAgreesWithDeal"]
            totals["stored_stale"] += not item["storedIsCurrent"]
            totals["over_100"] += abs(item["dealVariance"] or Decimal(0)) > _TOLERANCE
            logger.info("variance_audit_item", tag=tag, tenant_id=tenant_id, journey_id=str(journey_id),
                        deal_variance=str(item["dealVariance"]), stored_variance=str(item["storedVariance"]),
                        stored_is_current=item["storedIsCurrent"], list_agrees=item["listAgreesWithDeal"],
                        invoice_on_file=item["vehicleInvoiceOnFile"], net_standard=str(item["netStandard"]),
                        net_current=str(item["netCurrent"]), standard_unavailable=str(item["standardUnavailable"]),
                        differing=str(item["differing"])[:1800])
        logger.info("variance_audit_finished", tag=tag, tenant_id=tenant_id, **totals)
