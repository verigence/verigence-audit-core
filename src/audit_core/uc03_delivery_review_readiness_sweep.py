from __future__ import annotations

"""uc03_delivery_review_readiness_sweep.py — periodic sweep that raises
TL_DELIVERY_REVIEW once a submitted Delivery's document/data review is
genuinely done.

Same shape as workflow_stale_task_recovery.py (its own module docstring
explains the cross-tenant enumeration pattern this reuses unchanged): a
timer-driven sweep across every active tenant, one broad try/except per
tenant so a single failure never aborts the rest, re-armed on the next tick.

Why a sweep rather than gating Submit itself: PC's Submit action and "the
system has actually finished confirming this is clean" are two different
events on two different clocks -- DI classification is asynchronous and its
duration isn't something a synchronous button click can wait on without
sometimes leaving PC stuck for reasons outside their control. Submit stays
unconditional, exactly as it always has (see submit_delivery_capture_v2,
which also raises PC_DELIVERY_CAPTURE for PC's own side of this same
handoff, created and completed together at that same instant). This sweep
is what raises the TL-facing half once uc03_delivery_capture_v2.
delivery_review_readiness_blockers actually comes back clean -- that
function's own logic (open document/data/manual-verification tasks, any
document still mid-processing) is unchanged from when it was Submit's own
gate; only where it's called from has moved.

TL_DELIVERY_REVIEW's own created_at_utc is what TL's SLA/KPI clock should
read from -- not journey_stage_states.capture_completed_at_utc (PC's
click), which stays exactly what it always was and keeps driving TL's
journey *visibility* (the supervisory queue) unaffected by any of this.
"""

import asyncio
from uuid import UUID

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_platform_super_admin_context, set_tenant_context
from audit_core.uc03_delivery_capture_v2 import (
    TL_DELIVERY_REVIEW_TASK_TYPE,
    TL_DELIVERY_REVIEW_WORKFLOW_TYPE,
    _linked_delivery_documents,
    delivery_review_readiness_blockers,
    tl_delivery_review_effect_key,
    tl_delivery_review_effect_key_prefix,
)
from audit_core.workflow import create_workflow_task

logger = structlog.get_logger(__name__)

# Matches workflow_stale_task_recovery.py's own default -- no real reason
# for this sweep to run any more often than the existing, already-proven
# one in this same service. TL's SLA clock starting a few minutes later
# than the instant readiness is reached costs nothing; ticking 5x more
# often for that was unjustified extra load with no correctness benefit.
DEFAULT_SWEEP_INTERVAL_SECONDS = 300.0


def _active_tenant_ids(engine: Engine) -> list[str]:
    with engine.begin() as connection:
        set_platform_super_admin_context(connection)
        rows = connection.execute(
            text(
                """
                SELECT DISTINCT tenant_id
                FROM auditcore.projects
                WHERE project_status = 'ACTIVE'
                ORDER BY tenant_id
                """
            )
        ).scalars().all()
    return list(rows)


def _submitted_deliveries_awaiting_tl_review(
    connection, *, tenant_id: str
) -> list[UUID]:
    rows = connection.execute(
        text(
            """
            SELECT ds.journey_id
            FROM auditcore.journey_stage_states ds
            WHERE ds.tenant_id=:tenant_id AND ds.stage_code='DELIVERY'
              AND ds.capture_completed_at_utc IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM auditcore.workflow_tasks t
                  WHERE t.tenant_id=ds.tenant_id
                    AND t.task_type=:task_type
                    AND t.effect_key=:effect_key_prefix || ds.journey_id::text
              )
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_type": TL_DELIVERY_REVIEW_TASK_TYPE,
            "effect_key_prefix": tl_delivery_review_effect_key_prefix(tenant_id),
        },
    ).scalars().all()
    return list(rows)


def raise_ready_delivery_reviews_for_all_tenants(engine: Engine) -> int:
    """Run one sweep across every active tenant. Never raises. Returns how
    many TL_DELIVERY_REVIEW tasks were newly raised, for tests and logging."""
    try:
        tenant_ids = _active_tenant_ids(engine)
    except Exception:
        logger.warning("delivery_review_readiness_sweep_tenant_lookup_failed", exc_info=True)
        return 0

    raised_total = 0
    for tenant_id in tenant_ids:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                journey_ids = _submitted_deliveries_awaiting_tl_review(
                    connection, tenant_id=tenant_id
                )
                for journey_id in journey_ids:
                    documents = _linked_delivery_documents(connection, tenant_id, journey_id)
                    blockers = delivery_review_readiness_blockers(
                        connection, tenant_id=tenant_id, journey_id=journey_id, documents=documents,
                    )
                    if blockers:
                        continue
                    create_workflow_task(
                        connection,
                        tenant_id=tenant_id,
                        journey_id=journey_id,
                        workflow_type=TL_DELIVERY_REVIEW_WORKFLOW_TYPE,
                        process_area="DELIVERY",
                        task_type=TL_DELIVERY_REVIEW_TASK_TYPE,
                        assigned_role_code="TL",
                        task_payload={"documentCount": len(documents)},
                        effect_key=tl_delivery_review_effect_key(tenant_id, journey_id),
                        correlation_id=None,
                    )
                    raised_total += 1
                    logger.info(
                        "delivery_review_readiness_sweep_raised",
                        tenant_id=tenant_id,
                        journey_id=str(journey_id),
                    )
        except Exception:
            logger.warning(
                "delivery_review_readiness_sweep_tenant_failed",
                tenant_id=tenant_id,
                exc_info=True,
            )
            continue
    return raised_total


async def run_delivery_review_readiness_sweep_loop(
    engine: Engine,
    *,
    interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
) -> None:
    """Sweep every `interval_seconds` until cancelled. Runs the blocking DB
    work in a thread so it never stalls the event loop the rest of the app
    shares. asyncio.CancelledError is a BaseException, not an Exception --
    the broad except below never catches it, so cancellation (app shutdown)
    always propagates cleanly without an extra try/except around the loop."""

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(raise_ready_delivery_reviews_for_all_tenants, engine)
        except Exception:
            logger.warning("delivery_review_readiness_sweep_failed", exc_info=True)
