from __future__ import annotations

"""workflow_stale_task_recovery.py — periodic recovery sweep for
auditcore.workflow_tasks whose worker lease expired mid-execution.

Phase 0 of the UC03 document-pipeline redesign. workflow.recover_stale_worker_tasks()
existed with full test coverage but zero production callers -- exactly the missing
piece for recovering a task whose lease expired mid-execution (a worker crashed,
a deploy restarted the process, a request timed out) rather than a bug to remove.
This module is what finally calls it, on a timer, across every active tenant.

Cross-tenant enumeration uses the same narrow, already-sanctioned bypass as the
existing cross-Tenant Project SELECT path (set_platform_super_admin_context) --
not a new RLS bypass invented for this sweep. The actual recovery UPDATE for each
tenant still runs under that tenant's own set_tenant_context, exactly like any
other request-scoped connection.
"""

import asyncio

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_platform_super_admin_context, set_tenant_context
from audit_core.workflow import recover_stale_worker_tasks

logger = structlog.get_logger(__name__)

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


def recover_stale_worker_tasks_for_all_tenants(engine: Engine) -> int:
    """Run one sweep across every active tenant. Never raises.

    A single tenant's failure (a lock timeout, a transient connection error) is
    logged and skipped rather than aborting the rest of the sweep -- the next
    scheduled tick will retry it. Returns the total number of tasks recovered,
    for tests and logging.
    """

    try:
        tenant_ids = _active_tenant_ids(engine)
    except Exception:
        logger.warning("stale_worker_task_sweep_tenant_lookup_failed", exc_info=True)
        return 0

    recovered_total = 0
    for tenant_id in tenant_ids:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                recovered = recover_stale_worker_tasks(connection, tenant_id=tenant_id)
        except Exception:
            logger.warning(
                "stale_worker_task_sweep_tenant_failed",
                tenant_id=tenant_id,
                exc_info=True,
            )
            continue
        if recovered:
            recovered_total += len(recovered)
            logger.info(
                "stale_worker_task_sweep_recovered",
                tenant_id=tenant_id,
                recovered_count=len(recovered),
            )
    return recovered_total


async def run_stale_worker_task_recovery_loop(
    engine: Engine,
    *,
    interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
) -> None:
    """Sweep every `interval_seconds` until cancelled. Runs the blocking DB work
    in a thread so it never stalls the event loop the rest of the app shares.

    asyncio.CancelledError is a BaseException, not an Exception -- the broad
    except below never catches it, so cancellation (app shutdown) always
    propagates cleanly without an extra try/except around the loop."""

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(recover_stale_worker_tasks_for_all_tenants, engine)
        except Exception:
            logger.warning("stale_worker_task_sweep_failed", exc_info=True)
