"""Rebuild every Journey's cached Excess Discount rows (2026-10-31).

The Excess Discount tab reads ``p2_journey_runtime.discount_variance``, which
the stage recompute and the price-variance sweep refresh only when a Journey's
facts move (``price_variance_fact_version``). The rows now read the given
figure, the entitlement and the tolerance as the EXCESS_DISCOUNT rule does
(``uc03_discount_given``); a cached copy built before that keeps the old
meaning until its Journey's facts change. This rebuilds them all at once,
through the same ``refresh_price_variance`` the sweep calls -- it writes only
the cache, never a fact, task or finding.

It is also the check that the tab and the rule agree: every Journey (not
cancelled, not a duplicate) with an open "Discount given beyond the
entitlement" task must have an OVER_ENTITLEMENT row. Each one that does not is
logged as ``discount_variance_task_without_row``.

Started as worker job ``DISCOUNT_VARIANCE_RECOMPUTE``. A DRY RUN (rows
computed and compared, one ``discount_variance_recompute_item`` log line per
Journey whose rows would change, nothing written) unless the tag ends with
"-commit". Idempotent.

2026-11-26 (product owner: "excess discount given Rs 2000, not in the excess
discount list"). The list and the "Discount given beyond the entitlement" task
must move together, and so must the dealer-grid tier stored with the deal. A
Journey still under review whose excess rows change, or whose stored grid tier
is no longer the tier its insurance decision gives
(``uc03_deal_reconciliation._grid_tier``), is given one stage recompute on
commit: its deal is reconciled again and EXCESS_DISCOUNT runs again. The dry
run counts them (``recheck``, ``tierMoved``) and names each.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_discount_variance import OVER, discount_variance_rows
from audit_core.uc03_p2_journey360 import deal
from audit_core.uc03_p2_price_variance import refresh_price_variance

logger = structlog.get_logger(__name__)

_CLOSED_TASK = ("VERIFIED_COMPLETE", "CANCELLED", "FAILED", "DEAD_LETTER")
_MISSING_LOGGED = 200


def _journeys(engine: Engine, tenant_id: str) -> list[dict[str, Any]]:
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        rows = connection.execute(
            text(
                """
                SELECT r.journey_id, j.journey_reference, r.discount_variance,
                       j.review_completed_at_utc IS NULL AS under_review,
                       (SELECT COALESCE(da.details->'reconciliation'->'grid', da.details->'grid')->>'tier'
                          FROM auditcore.discount_applications da
                         WHERE da.tenant_id = r.tenant_id AND da.journey_id = r.journey_id
                           AND da.discount_key = 'ADDITIONAL_DISCOUNT'
                         ORDER BY da.updated_at_utc DESC LIMIT 1) AS grid_tier,
                       EXISTS (SELECT 1 FROM auditcore.journey_stage_states bs
                               WHERE bs.tenant_id = j.tenant_id AND bs.journey_id = j.journey_id
                                 AND bs.stage_code = 'BOOKING'
                                 AND (bs.business_status IN ('BOOKING_CANCELLED', 'DUPLICATE_BOOKING')
                                      OR bs.closure_disposition = 'NO_DELIVERY')) AS cancelled,
                       EXISTS (SELECT 1 FROM auditcore.p2_tasks t
                               WHERE t.tenant_id = r.tenant_id AND t.journey_id = r.journey_id
                                 AND t.source_code = 'EXCESS_DISCOUNT'
                                 AND t.task_status <> ALL(:closed)) AS open_task
                FROM auditcore.p2_journey_runtime r
                JOIN auditcore.journeys j ON j.tenant_id = r.tenant_id AND j.journey_id = r.journey_id
                WHERE r.tenant_id = :t
                ORDER BY j.journey_reference, r.journey_id
                """
            ),
            {"t": tenant_id, "closed": list(_CLOSED_TASK)},
        ).mappings().all()
    return [dict(r) for r in rows]


def _over(rows: Any) -> int:
    return sum(1 for r in rows or [] if isinstance(r, dict) and r.get("status") == OVER)


def _excess(rows: Any) -> list[tuple[Any, Any]]:
    """The excess rows as the EXCESS_DISCOUNT check would raise them: (line, amount)."""
    return sorted((str(r.get("key")), str(r.get("variance"))) for r in rows or []
                  if isinstance(r, dict) and r.get("status") == OVER)


def tier_moved(engine: Engine, *, tenant_id: str, journey_id: UUID, stored_tier: str | None) -> str | None:
    """The dealer-grid tier the Journey's insurance decision gives now, when it is
    not the tier stored with its dealer discount; else None."""
    if not stored_tier:
        return None
    from audit_core.uc03_deal_reconciliation import _grid_tier

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        now = _grid_tier(connection, tenant_id=tenant_id, journey_id=journey_id)
    return now if now != stored_tier else None


def request_recheck(engine: Engine, *, tenant_id: str, journey_id: UUID) -> None:
    """One stage recompute: the deal is reconciled again and its checks run again."""
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        connection.execute(text("SELECT auditcore.p2_request_stage_recompute(:t, :j)"),
                           {"t": tenant_id, "j": journey_id})


def recompute_journey(engine: Engine, *, tenant_id: str, journey_id: UUID, commit: bool) -> list[dict[str, Any]]:
    """The Journey's rows as they are computed now; with ``commit``, stored."""
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        if not commit:
            from audit_core.uc03_statement_totals import charges_in_doubt

            return discount_variance_rows(deal(connection, tenant_id=tenant_id, journey_id=journey_id),
                                          charges_in_doubt(connection, tenant_id=tenant_id, journey_id=journey_id))
        if refresh_price_variance(connection, tenant_id=tenant_id, journey_id=journey_id) is None:
            raise RuntimeError("the Deal could not be built")
        stored = connection.execute(
            text("SELECT discount_variance FROM auditcore.p2_journey_runtime WHERE tenant_id = :t AND journey_id = :j"),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one()
    return list(stored or [])


def run_discount_variance_recompute(engine: Engine, tag: str, tenant_ids: list[str]) -> dict[str, Any]:
    """Returns the counts per Project and the bookings rechecked (kept on the job row,
    so a dry run can be read before its commit)."""
    commit = tag.endswith("-commit")
    report: dict[str, Any] = {"commit": commit, "tenants": {}}
    for tenant_id in tenant_ids:
        rechecked: list[dict[str, Any]] = []
        totals = {"journeys": 0, "changed": 0, "failed": 0, "overBefore": 0, "overAfter": 0,
                  "openTasks": 0, "openTasksWithoutRow": 0, "tierMoved": 0, "recheck": 0}
        try:
            journeys = _journeys(engine, tenant_id)
        except Exception:
            logger.warning("discount_variance_recompute_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        for journey in journeys:
            totals["journeys"] += 1
            journey_id = UUID(str(journey["journey_id"]))
            before = list(journey["discount_variance"] or [])
            try:
                after = recompute_journey(engine, tenant_id=tenant_id, journey_id=journey_id, commit=commit)
            except Exception:
                totals["failed"] += 1
                logger.warning("discount_variance_recompute_item_failed", tag=tag, tenant_id=tenant_id,
                               journey_id=str(journey_id), exc_info=True)
                continue
            totals["overBefore"] += _over(before)
            totals["overAfter"] += _over(after)
            if json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True):
                totals["changed"] += 1
                logger.info("discount_variance_recompute_item", tag=tag, tenant_id=tenant_id, commit=commit,
                            journey_id=str(journey_id), journey_reference=journey["journey_reference"],
                            over_before=_over(before), over_after=_over(after), rows=after)
            # The task follows the list, and the stored tier the insurance decision (2026-11-26).
            try:
                tier_now = tier_moved(engine, tenant_id=tenant_id, journey_id=journey_id,
                                      stored_tier=journey["grid_tier"])
            except Exception:
                tier_now = None
                logger.warning("discount_variance_recompute_tier_failed", tag=tag, tenant_id=tenant_id,
                               journey_id=str(journey_id), exc_info=True)
            if tier_now is not None:
                totals["tierMoved"] += 1
            if journey["under_review"] and not journey["cancelled"] and (
                    tier_now is not None or _excess(before) != _excess(after)):
                totals["recheck"] += 1
                logger.info("discount_variance_recompute_recheck", tag=tag, tenant_id=tenant_id, commit=commit,
                            journey_id=str(journey_id), journey_reference=journey["journey_reference"],
                            tier_stored=journey["grid_tier"], tier_now=tier_now,
                            excess_before=_excess(before), excess_after=_excess(after))
                if len(rechecked) < _MISSING_LOGGED:
                    rechecked.append({"booking": journey["journey_reference"], "tierStored": journey["grid_tier"],
                                      "tierNow": tier_now or journey["grid_tier"],
                                      "excessBefore": _excess(before), "excessAfter": _excess(after)})
                if commit:
                    try:
                        request_recheck(engine, tenant_id=tenant_id, journey_id=journey_id)
                    except Exception:
                        totals["failed"] += 1
                        logger.warning("discount_variance_recompute_recheck_failed", tag=tag, tenant_id=tenant_id,
                                       journey_id=str(journey_id), exc_info=True)
            if journey["open_task"] and not journey["cancelled"]:
                totals["openTasks"] += 1
                if not _over(after):
                    totals["openTasksWithoutRow"] += 1
                    if totals["openTasksWithoutRow"] <= _MISSING_LOGGED:
                        logger.warning("discount_variance_task_without_row", tag=tag, tenant_id=tenant_id,
                                       journey_id=str(journey_id), journey_reference=journey["journey_reference"],
                                       rows=after)
        logger.info("discount_variance_recompute_finished", tag=tag, tenant_id=tenant_id, commit=commit, **totals)
        report["tenants"][tenant_id] = {**totals, "rechecked": rechecked}
    return report
