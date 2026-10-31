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


def recompute_journey(engine: Engine, *, tenant_id: str, journey_id: UUID, commit: bool) -> list[dict[str, Any]]:
    """The Journey's rows as they are computed now; with ``commit``, stored."""
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        if not commit:
            return discount_variance_rows(deal(connection, tenant_id=tenant_id, journey_id=journey_id))
        if refresh_price_variance(connection, tenant_id=tenant_id, journey_id=journey_id) is None:
            raise RuntimeError("the Deal could not be built")
        stored = connection.execute(
            text("SELECT discount_variance FROM auditcore.p2_journey_runtime WHERE tenant_id = :t AND journey_id = :j"),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one()
    return list(stored or [])


def run_discount_variance_recompute(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    commit = tag.endswith("-commit")
    for tenant_id in tenant_ids:
        totals = {"journeys": 0, "changed": 0, "failed": 0, "overBefore": 0, "overAfter": 0,
                  "openTasks": 0, "openTasksWithoutRow": 0}
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
            if journey["open_task"] and not journey["cancelled"]:
                totals["openTasks"] += 1
                if not _over(after):
                    totals["openTasksWithoutRow"] += 1
                    if totals["openTasksWithoutRow"] <= _MISSING_LOGGED:
                        logger.warning("discount_variance_task_without_row", tag=tag, tenant_id=tenant_id,
                                       journey_id=str(journey_id), journey_reference=journey["journey_reference"],
                                       rows=after)
        logger.info("discount_variance_recompute_finished", tag=tag, tenant_id=tenant_id, commit=commit, **totals)
