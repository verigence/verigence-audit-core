"""SKU_SET (2026-11-03): the SKU the product owner decides for a booking.

A PC's confirmed SKU changes only through a Team Lead's decision
(uc03_model_selection_corrections). When the product owner decides it -- live
2026-11-02: SHI-SS-261027-007 confirmed on 28 Oct as a BE 6 while its
documents name an XUV 3XO AX5 PM MT -- this job applies that decision the
same way a Team Lead's approved correction is applied: the SKU is checked
against the price list in force on the booking's pricing date, pinned as a
Team Lead decision with who decided it in its evidence, recorded on the
booking's activity, the deal re-priced and the booking recomputed.

Worker job ``SKU_SET``: tag ``<tenant_id>:<label>``, reports
``["<journey reference>=<sku code>", ...]``. A DRY RUN unless the label ends
with "-commit": the dry run reports, per booking, the SKU now and the SKU it
would get, or why it cannot. Idempotent: a booking already on that SKU is
left as it is.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

JOB = "SKU_SET"


def _parse(tag: str) -> tuple[str, bool]:
    tenant_id, sep, label = (tag or "").partition(":")
    if not sep or not tenant_id.strip() or not label.strip():
        raise ValueError(f"{JOB}: tag must be '<tenant_id>:<label>'")
    return tenant_id.strip(), label.strip().endswith("-commit")


def set_sku(connection: Any, *, tenant_id: str, reference: str, sku_code: str, tag: str,
            commit: bool) -> dict[str, Any]:
    """One booking's SKU set to ``sku_code`` (see the module docstring)."""
    from audit_core.uc03_model_resolution import (
        _pin_state,
        _run_deal_reconciliation,
        reassign_confirmed_sku,
    )
    from audit_core.uc03_model_selection_corrections import _current_effective_rows
    from audit_core.uc03_p2_runtime import note_facts_changed, record_activity

    entry: dict[str, Any] = {"reference": reference, "to": sku_code}
    journey_id = connection.execute(
        text("SELECT journey_id FROM auditcore.journeys WHERE tenant_id=:t AND journey_reference=:r"),
        {"t": tenant_id, "r": reference},
    ).scalar_one_or_none()
    if journey_id is None:
        return {**entry, "error": "No booking with this reference."}
    journey_id = UUID(str(journey_id))
    state = _pin_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    entry["from"] = state.get("sku_code")
    rows = _current_effective_rows(connection, tenant_id=tenant_id, journey_id=journey_id)
    row = next((r for r in rows if str(r["sku_code"]) == sku_code), None)
    if row is None:
        return {**entry, "error": "The SKU is not in the price list in force on the booking's pricing date."}
    if str(state.get("product_sku_id") or "") == str(row["product_sku_id"]):
        return {**entry, "unchanged": True}
    if not commit:
        return {**entry, "wouldApply": True}
    correlation_id = f"{JOB.lower()}:{tag}"
    reassign_confirmed_sku(
        connection, tenant_id=tenant_id, journey_id=journey_id, product_sku_id=UUID(str(row["product_sku_id"])),
        evidence={"source": "PRODUCT_OWNER", "job": tag, "skuCode": sku_code,
                  "replaced": {"skuCode": state.get("sku_code"), "basis": state.get("match_basis")},
                  "decidedAtUtc": datetime.now(UTC).isoformat()},
    )
    record_activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type="SKU_SET_BY_PRODUCT_OWNER",
        subject_type="JOURNEY", subject_id=str(journey_id),
        details={"from": state.get("sku_code"), "to": sku_code, "job": tag}, correlation_id=correlation_id,
    )
    _run_deal_reconciliation(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id)
    note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id, reason=JOB,
                       correlation_id=correlation_id)
    return {**entry, "applied": True}


def run_sku_set(engine: Engine, tag: str, reports: list[str]) -> dict[str, Any]:
    tenant_id, commit = _parse(tag)
    items = []
    for item in reports or []:
        reference, sep, sku_code = str(item).partition("=")
        if not sep or not reference.strip() or not sku_code.strip():
            items.append({"item": item, "error": "Expected '<journey reference>=<sku code>'."})
            continue
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                items.append(set_sku(connection, tenant_id=tenant_id, reference=reference.strip(),
                                     sku_code=sku_code.strip(), tag=tag, commit=commit))
        except Exception as exc:  # one booking's failure never stops the others
            logger.warning("sku_set_failed", tag=tag, reference=reference, exc_info=True)
            items.append({"reference": reference.strip(), "to": sku_code.strip(),
                          "error": f"{type(exc).__name__}: {exc}"[:300]})
    result = {"commit": commit, "items": items}
    logger.info("sku_set_finished", tag=tag, **{k: v for k, v in result.items() if k != "items"},
                applied=sum(1 for i in items if i.get("applied")))
    return result


__all__ = ["JOB", "run_sku_set", "set_sku"]
