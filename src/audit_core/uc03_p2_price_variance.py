"""The Journey list's price variance is the Deal tab's own number.

The cache records the ``fact_version`` it was computed at (migration 0159).
Every fact change bumps that version through ``note_facts_changed``, so a copy
left behind by any write path -- a SKU pinned by a resolution run, a pricing
date moved, an invoice materialised -- is one integer comparison away from
being recognised as stale, and the sweep recomputes it. Chasing each write
path instead would miss the next one (2026-10-24).

The Deal build (``uc03_p2_journey360.deal``) is the single calculation of
"current vs standard". This module stores its result on the Journey's runtime
row so the list reads one value instead of repeating the calculation in SQL.
It only ever writes ``p2_journey_runtime.price_variance``; any failure leaves
the previous value untouched and is logged, so it can never break a recompute.
"""
from __future__ import annotations

import json
from decimal import Decimal
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_discount_variance import discount_variance_rows
from audit_core.uc03_p2_journey360 import deal

logger = structlog.get_logger(__name__)

# Journeys whose Deal a worker process failed to build, with the fact version it
# failed at: held back until those facts move, so a broken Deal is not retried
# every sweep while a changed one is always picked up.
_backfill_tried: set[tuple[str, UUID, int | None]] = set()
_BACKFILL_BATCH = 25
_TRIED_CAP = 500


def refresh_price_variance(connection: Connection, *, tenant_id: str, journey_id: UUID) -> Decimal | None:
    """Store the Deal's current-vs-standard variance (0 when nothing can be
    compared, as the list always showed). Returns the stored value, or None
    if it could not be computed."""
    try:
        with connection.begin_nested():
            view = deal(connection, tenant_id=tenant_id, journey_id=journey_id)
            raw = view["summary"]["variance"]["currentVsStandard"]
            value = Decimal(raw) if raw is not None else Decimal(0)
            discounts = json.dumps(discount_variance_rows(view))
            # The version is stamped even when the figure itself has not moved:
            # that is what marks the cache current for the facts as they stand.
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_journey_runtime
                    SET price_variance=:value, price_variance_fact_version=fact_version,
                        discount_variance=CAST(:discounts AS jsonb)
                    WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                      AND (price_variance IS DISTINCT FROM :value
                           OR price_variance_fact_version IS DISTINCT FROM fact_version
                           OR discount_variance IS DISTINCT FROM CAST(:discounts AS jsonb))
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id, "value": value, "discounts": discounts},
            )
            return value
    except Exception:
        logger.warning("p2_price_variance_refresh_failed", tenant_id=tenant_id,
                       journey_id=str(journey_id), exc_info=True)
        return None


def backfill_price_variance(engine: Engine, *, tenant_id: str, limit: int = _BACKFILL_BATCH) -> int:
    """Recompute the variance of every Journey whose cached copy is older than
    its facts -- one it never had, or one left behind by a change that did not
    run a stage recompute (2026-10-24). Writes only that column.

    A Journey is attempted once per process for a given fact version, so a
    Deal that cannot be built is not retried every sweep, while one whose
    facts move again is picked up on the next."""
    done = 0
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        # The predicate is the partial index's (migration 0159). A refreshed
        # row stamps its fact version and so leaves the predicate on its own;
        # only a Journey whose Deal could not be built keeps matching, and
        # those are the ones _backfill_tried holds back until its facts move.
        journeys = connection.execute(
            text(
                """
                SELECT journey_id, fact_version FROM auditcore.p2_journey_runtime
                WHERE tenant_id=:tenant_id
                  AND (price_variance IS NULL
                       OR price_variance_fact_version IS NULL
                       OR price_variance_fact_version <> fact_version)
                ORDER BY updated_at_utc DESC LIMIT :limit
                """
            ),
            {"tenant_id": tenant_id, "limit": limit + min(len(_backfill_tried), _TRIED_CAP)},
        ).mappings().all()
        for row in journeys:
            journey_id, fact_version = row["journey_id"], row["fact_version"]
            key = (tenant_id, journey_id, fact_version)
            if key in _backfill_tried:
                continue
            if refresh_price_variance(connection, tenant_id=tenant_id, journey_id=journey_id) is not None:
                done += 1
            elif len(_backfill_tried) < _TRIED_CAP:
                # Remembered so a Deal that cannot be built is not retried every
                # 30 seconds; forgotten past the cap rather than growing without
                # bound in a long-lived worker.
                _backfill_tried.add(key)
            if done >= limit:
                break
    return done
