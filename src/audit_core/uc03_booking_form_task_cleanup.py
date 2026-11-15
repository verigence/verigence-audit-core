"""Cancel the open tasks due to the booking form (product owner, 2026-11-15).

Product owner: "we dont need to have any thing for booking form.. lets not
have any tasks for it" / "lets remove all the tasks due to booking forms". New
ones are no longer raised (``uc03_p2_task_producer.about_the_booking_form``,
checked in ``raise_or_refresh`` and for every check's own task); this closes
what was raised before, on every Journey of every Project: each open task
``about_the_booking_form`` matches is CANCELLED with the reason on the task
(completion_result outcome CHECK_RETIRED) and a CANCELLED task event.

Tasks that name the booking docket among other documents ("Customer name
differs on 3 documents", "Figures not read from documents on file") are not
cancelled here: the next recompute of the Journey lists them without it.

Started as worker job ``BOOKING_FORM_TASK_CLEANUP``. A DRY RUN (counts logged,
nothing written) unless the tag ends with "-commit". Idempotent: a cancelled
task is never reopened (raise_or_refresh) and a second run finds nothing open.
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
    BOOKING_FORM_TASK_RESULT,
    _json,
    about_the_booking_form,
)
from audit_core.uc03_p2_tasks import record_task_event

logger = structlog.get_logger(__name__)


def _open_tasks(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT task_id, journey_id, source_code, task_status, reference
            FROM auditcore.p2_tasks
            WHERE tenant_id = :t AND task_status = ANY(:open)
            ORDER BY created_at_utc
            FOR UPDATE
            """
        ),
        {"t": tenant_id, "open": list(_OPEN)},
    ).mappings().all()
    return [dict(r) for r in rows if about_the_booking_form(r["source_code"], r["reference"])]


def clean_tenant(connection: Connection, *, tenant_id: str, tag: str, commit: bool) -> dict[str, Any]:
    tasks = _open_tasks(connection, tenant_id)
    summary: dict[str, Any] = {
        "tasks": len(tasks),
        "tasksByCode": dict(Counter(str(t["source_code"]) for t in tasks)),
        "journeys": len({str(t["journey_id"]) for t in tasks}),
        "cancelled": 0,
    }
    if not commit or not tasks:
        return summary
    cancelled = connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks
            SET task_status = 'CANCELLED',
                completion_result = COALESCE(completion_result, '{}'::jsonb) || CAST(:result AS jsonb),
                updated_at_utc = now()
            WHERE tenant_id = :t AND task_id = ANY(:ids) AND task_status = ANY(:open)
            RETURNING task_id, journey_id, source_code
            """
        ),
        {"t": tenant_id, "ids": [t["task_id"] for t in tasks], "open": list(_OPEN),
         "result": _json(BOOKING_FORM_TASK_RESULT)},
    ).mappings().all()
    before = {str(t["task_id"]): str(t["task_status"]) for t in tasks}
    for row in cancelled:
        record_task_event(
            connection, tenant_id=tenant_id, journey_id=UUID(str(row["journey_id"])),
            task_id=UUID(str(row["task_id"])), event_type="CANCELLED", actor_id="SYSTEM",
            actor_role_code="SYSTEM",
            details={**BOOKING_FORM_TASK_RESULT, "sourceCode": row["source_code"],
                     "previousStatus": before.get(str(row["task_id"])), "job": "BOOKING_FORM_TASK_CLEANUP",
                     "tag": tag},
        )
    summary["cancelled"] = len(cancelled)
    return summary


def run_booking_form_task_cleanup(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    commit = tag.endswith("-commit")
    for tenant_id in tenant_ids:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                summary = clean_tenant(connection, tenant_id=tenant_id, tag=tag, commit=commit)
        except Exception:
            logger.warning("booking_form_task_cleanup_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        logger.info("booking_form_task_cleanup_finished", tag=tag, tenant_id=tenant_id, commit=commit,
                    **{k: (v if isinstance(v, int) else str(v)) for k, v in summary.items()})
