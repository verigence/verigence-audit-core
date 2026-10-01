"""The Journey list's price variance is the Deal tab's own number.

The Deal build (``uc03_p2_journey360.deal``) is the single calculation of
"current vs standard". This module stores its result on the Journey's runtime
row so the list reads one value instead of repeating the calculation in SQL.
It only ever writes ``p2_journey_runtime.price_variance``; any failure leaves
the previous value untouched and is logged, so it can never break a recompute.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_journey360 import deal

logger = structlog.get_logger(__name__)

# Journeys a worker process has already tried to backfill: each is tried once
# per process, so a Journey whose Deal cannot be built is not retried every sweep.
_backfill_tried: set[tuple[str, UUID]] = set()
_BACKFILL_BATCH = 25


def refresh_price_variance(connection: Connection, *, tenant_id: str, journey_id: UUID) -> Decimal | None:
    """Store the Deal's current-vs-standard variance (0 when nothing can be
    compared, as the list always showed). Returns the stored value, or None
    if it could not be computed."""
    try:
        with connection.begin_nested():
            view = deal(connection, tenant_id=tenant_id, journey_id=journey_id)
            raw = view["summary"]["variance"]["currentVsStandard"]
            value = Decimal(raw) if raw is not None else Decimal(0)
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_journey_runtime SET price_variance=:value
                    WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                      AND price_variance IS DISTINCT FROM :value
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id, "value": value},
            )
            return value
    except Exception:
        logger.warning("p2_price_variance_refresh_failed", tenant_id=tenant_id,
                       journey_id=str(journey_id), exc_info=True)
        return None


def backfill_price_variance(engine: Engine, *, tenant_id: str, limit: int = _BACKFILL_BATCH) -> int:
    """Compute the variance for Journeys that have none yet (existing ones
    when this was introduced). Writes only that column; each Journey is
    attempted once per process."""
    done = 0
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        journeys = connection.execute(
            text(
                """
                SELECT journey_id FROM auditcore.p2_journey_runtime
                WHERE tenant_id=:tenant_id AND price_variance IS NULL
                ORDER BY updated_at_utc DESC LIMIT :limit
                """
            ),
            {"tenant_id": tenant_id, "limit": limit + len(_backfill_tried)},
        ).scalars().all()
        for journey_id in journeys:
            key = (tenant_id, journey_id)
            if key in _backfill_tried:
                continue
            _backfill_tried.add(key)
            if refresh_price_variance(connection, tenant_id=tenant_id, journey_id=journey_id) is not None:
                done += 1
            if done >= limit:
                break
    return done
