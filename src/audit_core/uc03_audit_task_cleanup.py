"""Audit tasks that named no document (2026-11-01).

993 open tasks read only "The audit check failed. Review the related
documents." -- the check's own task, raised with nothing to show because what
the check found is written elsewhere. This job, per Project:

1. cancels the open check tasks of WRONG_DOCUMENT, MANUAL_VERIFICATION and
   INVOICE_SKU_MISMATCH (``uc03_p2_task_producer.OWN_TASK_CODES``): each
   problem has its own task naming the documents. Only the check's own task
   (control:<journey>:<code>) -- never the PC's field review or name tasks;
2. runs the native checks again (forced) on the bookings whose DUPLICATE_RECEIPT
   or DUPLICATE_BOOKING task reads the fallback sentence: the receipt task then
   names the receipt, the pair is raised again with what matched.

Started as worker job ``AUDIT_TASK_CLEANUP``. A DRY RUN (logged, nothing
written) unless the tag ends with "-commit". Idempotent: a second run finds
no task to cancel and no fallback task.
"""
from __future__ import annotations

from collections import Counter
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_task_producer import (
    _OPEN,
    OWN_TASK_CODES,
    OWN_TASK_RESULT,
    _json,
)
from audit_core.uc03_p2_tasks import record_task_event

logger = structlog.get_logger(__name__)

JOB = "AUDIT_TASK_CLEANUP"
FALLBACK = "The audit check failed. Review the related documents."
_RERUN_CODES = ("DUPLICATE_RECEIPT", "DUPLICATE_BOOKING")


def _own_task_shells(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(
        text(
            """
            SELECT task_id, journey_id, source_code, task_status FROM auditcore.p2_tasks
            WHERE tenant_id=:t AND dedupe_key LIKE 'control:%' AND source_code = ANY(:codes)
              AND task_status = ANY(:open)
            ORDER BY created_at_utc
            FOR UPDATE
            """
        ),
        {"t": tenant_id, "codes": sorted(OWN_TASK_CODES), "open": list(_OPEN)},
    ).mappings().all()]


def _cancel_shells(connection: Connection, tenant_id: str, tasks: list[dict[str, Any]], tag: str) -> int:
    if not tasks:
        return 0
    cancelled = connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks
            SET task_status='CANCELLED',
                completion_result=COALESCE(completion_result, '{}'::jsonb) || CAST(:result AS jsonb),
                updated_at_utc=now()
            WHERE tenant_id=:t AND task_id = ANY(:ids) AND task_status = ANY(:open)
              AND dedupe_key LIKE 'control:%' AND source_code = ANY(:codes)
            RETURNING task_id, journey_id, source_code
            """
        ),
        {"t": tenant_id, "ids": [t["task_id"] for t in tasks], "open": list(_OPEN),
         "codes": sorted(OWN_TASK_CODES), "result": _json(OWN_TASK_RESULT)},
    ).mappings().all()
    before = {str(t["task_id"]): str(t["task_status"]) for t in tasks}
    for row in cancelled:
        record_task_event(
            connection, tenant_id=tenant_id, journey_id=UUID(str(row["journey_id"])),
            task_id=UUID(str(row["task_id"])), event_type="CANCELLED", actor_id="SYSTEM",
            actor_role_code="SYSTEM",
            details={**OWN_TASK_RESULT, "sourceCode": row["source_code"],
                     "previousStatus": before.get(str(row["task_id"])), "job": JOB, "tag": tag},
        )
    return len(cancelled)


def _fallback_journeys(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(
        text(
            """
            SELECT journey_id, array_agg(DISTINCT source_code ORDER BY source_code) AS codes
            FROM auditcore.p2_tasks
            WHERE tenant_id=:t AND dedupe_key LIKE 'control:%' AND source_code = ANY(:codes)
              AND task_status = ANY(:open) AND description = :fallback
            GROUP BY journey_id ORDER BY journey_id
            """
        ),
        {"t": tenant_id, "codes": list(_RERUN_CODES), "open": list(_OPEN), "fallback": FALLBACK},
    ).mappings().all()]


def _rerun_native(connection: Connection, tenant_id: str, journey_id: UUID, tag: str) -> list[str]:
    from audit_core.uc03_p2_controls import delivery_active
    from audit_core.uc03_p2_runtime import enqueue_work

    units = ["NATIVE:BOOKING"]
    if delivery_active(connection, tenant_id=tenant_id, journey_id=journey_id):
        units.append("NATIVE:DELIVERY")
    for unit in units:
        enqueue_work(connection, tenant_id=tenant_id, journey_id=journey_id, work_type="CONTROL_EVALUATE",
                     work_key=f"unit:{journey_id}:{unit}", payload={"unit": unit, "force": True},
                     correlation_id=f"{JOB.lower()}:{tag}")
    return units


def clean_tenant(engine: Engine, *, tenant_id: str, tag: str, commit: bool) -> dict[str, Any]:
    totals: dict[str, Any] = {"shells": 0, "shellsByCode": {}, "cancelled": 0, "rerunJourneys": 0,
                              "rerunByCode": {}}
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        shells = _own_task_shells(connection, tenant_id)
        totals["shells"] = len(shells)
        totals["shellsByCode"] = dict(Counter(str(t["source_code"]) for t in shells))
        if commit:
            totals["cancelled"] = _cancel_shells(connection, tenant_id, shells, tag)

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        fallback = _fallback_journeys(connection, tenant_id)
        totals["rerunJourneys"] = len(fallback)
        totals["rerunByCode"] = dict(Counter(c for row in fallback for c in row["codes"]))
        for row in fallback:
            if commit:
                _rerun_native(connection, tenant_id, UUID(str(row["journey_id"])), tag)
    return totals


def run_audit_task_cleanup(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    commit = tag.endswith("-commit")
    for tenant_id in tenant_ids:
        try:
            totals = clean_tenant(engine, tenant_id=tenant_id, tag=tag, commit=commit)
        except Exception:
            logger.warning("audit_task_cleanup_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        logger.info("audit_task_cleanup_finished", tag=tag, tenant_id=tenant_id, commit=commit, **totals)
