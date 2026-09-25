"""uc03_document_sync_recovery.py — periodic + on-demand recovery for a
document DI has fully acknowledged but audit-core never durably synced.

Confirmed live (2026-09-24): a large enough upload batch can exhaust the
per-document sync pipeline's own lock-contention retry budget
(~5 minutes, see uc03_confidence_review_policy._run_sync_booking_document_task's
own docstring) for its unluckiest member, which then stays stuck
*permanently* -- DI already got a 200 OK for its webhook callback, so it
never retries, and nothing else ever re-triggers the sync. This module is
the missing "did the sync actually finish" check, on a timer, exactly the
same shape as workflow_stale_task_recovery.py's own sweep for a different
"lease expired mid-execution" problem: no new infrastructure, an
in-process asyncio loop started from the app's own lifespan.

resolve_requirement_satisfaction (uc03_requirement_satisfaction.py) is
computed live and needs no special handling for this -- there is nothing
cached for a stuck document to poison. This module exists purely so a
stuck document's own facts don't stay missing forever.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.telemetry import record_metric
from audit_core.workflow_stale_task_recovery import _active_tenant_ids

logger = structlog.get_logger(__name__)

DEFAULT_SWEEP_INTERVAL_SECONDS = 300.0
# Comfortably past _run_sync_booking_document_task's own ~5-minute retry
# budget, so this never fights an attempt that's still legitimately in
# progress -- only ever catches one that has genuinely given up.
_STALE_AFTER_INTERVAL = "10 minutes"
_SYSTEM_SERVICE_ID = "SYSTEM:DOCUMENT_SYNC_RECOVERY"
_SWEEP_LIMIT_PER_TENANT = 200

# On-demand cooldown, same shape as uc03_document_capture_v2._di_context_ensured_until:
# a per-journey "don't re-check for a while" cache so a 1-second polling
# checklist read can't hammer this on every single call.
_ON_DEMAND_COOLDOWN_SECONDS = 120.0
_on_demand_checked_until: dict[tuple[str, str], float] = {}
_on_demand_lock = threading.Lock()


def _find_stale_document_syncs(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID | None = None,
    limit: int = _SWEEP_LIMIT_PER_TENANT,
) -> list[dict[str, Any]]:
    """Evidence DI has acknowledged (association_status='ACTIVE') but whose
    processing_status_cache is still NULL -- the exact signal
    _sync_booking_document writes on its first successful step, so NULL
    this long after linking means that step never ran, not that it's still
    legitimately in progress (a genuinely slow DI extraction still writes
    *some* status here, just not PROCESSED yet).
    """
    clauses = [
        "association_status='ACTIVE'",
        "processing_status_cache IS NULL",
        f"linked_at_utc < now() - interval '{_STALE_AFTER_INTERVAL}'",
    ]
    params: dict[str, Any] = {"tenant_id": tenant_id, "limit": limit}
    if journey_id is not None:
        clauses.append("journey_id=:journey_id")
        params["journey_id"] = journey_id
    rows = connection.execute(
        text(
            f"""
            SELECT journey_id, di_document_id, process_area
            FROM auditcore.evidence
            WHERE tenant_id=:tenant_id AND {" AND ".join(clauses)}
            ORDER BY linked_at_utc
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    return [dict(row) for row in rows]


async def _recover_document_sync(
    engine: Engine,
    *,
    tenant_id: str,
    row: dict[str, Any],
    trigger_source: str,
) -> bool:
    from audit_core.uc03_confidence_review_policy import _run_sync_booking_document_task

    journey_id = row["journey_id"]
    document_id = row["di_document_id"]
    stage_code = str(row["process_area"] or "").upper()
    try:
        await _run_sync_booking_document_task(
            engine,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document_id,
            service_id=_SYSTEM_SERVICE_ID,
            stage_code=stage_code,
        )
    except Exception:
        logger.warning(
            "uc03_document_sync_recovery_attempt_failed",
            tenant_id=tenant_id,
            journey_id=str(journey_id),
            document_id=str(document_id),
            stage_code=stage_code,
            trigger_source=trigger_source,
            exc_info=True,
        )
        return False
    logger.info(
        "uc03_document_sync_recovery_dispatched",
        tenant_id=tenant_id,
        journey_id=str(journey_id),
        document_id=str(document_id),
        stage_code=stage_code,
        trigger_source=trigger_source,
    )
    return True


async def recover_stale_document_syncs_for_all_tenants(engine: Engine) -> int:
    """Run one sweep across every active tenant. Never raises -- a single
    tenant's failure (a lock timeout, a transient connection error) is
    logged and skipped rather than aborting the rest of the sweep, the
    same convention workflow_stale_task_recovery.py's own sweep uses.
    """
    started = time.perf_counter()
    try:
        tenant_ids = await asyncio.to_thread(_active_tenant_ids, engine)
    except Exception:
        logger.warning("uc03_document_sync_recovery_sweep_tenant_lookup_failed", exc_info=True)
        return 0

    recovered_total = 0
    for tenant_id in tenant_ids:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                stale = _find_stale_document_syncs(connection, tenant_id=tenant_id)
        except Exception:
            logger.warning(
                "uc03_document_sync_recovery_sweep_tenant_query_failed",
                tenant_id=tenant_id,
                exc_info=True,
            )
            continue
        for row in stale:
            if await _recover_document_sync(
                engine, tenant_id=tenant_id, row=row, trigger_source="SWEEP",
            ):
                recovered_total += 1

    duration_ms = (time.perf_counter() - started) * 1000.0
    record_metric(
        "audit_core.uc03_document_sync_recovery_sweep.duration_ms",
        duration_ms,
        kind="histogram",
    )
    record_metric(
        "audit_core.uc03_document_sync_recovery_sweep.recovered_count",
        recovered_total,
    )
    return recovered_total


async def run_document_sync_recovery_loop(
    engine: Engine,
    *,
    interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
) -> None:
    """Sweep every `interval_seconds` until cancelled -- same shape as
    workflow_stale_task_recovery.run_stale_worker_task_recovery_loop.
    asyncio.CancelledError is a BaseException, not an Exception, so the
    broad except below never catches it and app-shutdown cancellation
    always propagates cleanly.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await recover_stale_document_syncs_for_all_tenants(engine)
        except Exception:
            logger.warning("uc03_document_sync_recovery_sweep_failed", exc_info=True)


def dispatch_stale_document_sync_recovery_on_demand(
    connection: Connection,
    background_tasks: Any,
    engine: Engine,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> None:
    """Journey-scoped counterpart to the periodic sweep, called from the
    capture-v2 checklist reads so a stuck document self-heals within a
    couple of page loads instead of waiting for the next scheduled sweep.
    Cooldown-gated (same shape as uc03_document_capture_v2's own
    _di_context_ensured_until cache) so a 1-second polling read can't
    trigger this on every single call -- the in-memory check below is a
    single dict lookup, costing nothing when still within cooldown.
    """
    cache_key = (tenant_id, str(journey_id))
    now = time.monotonic()
    with _on_demand_lock:
        if _on_demand_checked_until.get(cache_key, 0.0) > now:
            return
        _on_demand_checked_until[cache_key] = now + _ON_DEMAND_COOLDOWN_SECONDS

    try:
        stale = _find_stale_document_syncs(connection, tenant_id=tenant_id, journey_id=journey_id)
    except Exception:
        logger.warning(
            "uc03_document_sync_recovery_on_demand_query_failed",
            tenant_id=tenant_id,
            journey_id=str(journey_id),
            exc_info=True,
        )
        return
    for row in stale:
        background_tasks.add_task(
            _dispatch_on_demand_recovery, engine, tenant_id=tenant_id, row=row,
        )


async def _dispatch_on_demand_recovery(engine: Engine, *, tenant_id: str, row: dict[str, Any]) -> None:
    await _recover_document_sync(engine, tenant_id=tenant_id, row=row, trigger_source="ON_DEMAND")
