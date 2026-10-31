"""Cancel the open tasks of cash transactions (product owner, 2026-10-31).

Product owner: "For any cash transactions supress the Tasks and remove all the
existing tasks for cash transactions." New ones are no longer raised
(``uc03_p2_task_producer.SUPPRESSED_TASK_CODES``); this closes what was raised
before, on every Journey of every Project:

- the open tasks of the cash checks -- "Confirm the cash collection was
  intimated" (CASH_INTIMATION_UNCONFIRMED, PC), "Cash collected without
  intimation" (CASH_NOT_INTIMATED, TL), "Cash receipt above the statutory
  limit" (CASH_ABOVE_LIMIT, TL) and the Rule Engine's
  PAYMENT_MODE_CASH_UNRECEIPTED (PC) -- are CANCELLED with the reason on the
  task (completion_result outcome CHECK_RETIRED) and a CANCELLED task event;
- the two retired checks (the cash intimation question and its finding) leave
  no check row behind (p2_control_state), and an open CASH_NOT_INTIMATED
  finding is resolved, as it would be by a check that no longer fails. The
  checks that still run keep their rows, and CASH_ABOVE_LIMIT keeps its
  finding (the Sec 269ST limit stays on the Audit Summary).

Started as worker job ``CASH_TASK_CLEANUP``. A DRY RUN (counts logged, nothing
written) unless the tag ends with "-commit". Idempotent: a cancelled task is
never reopened (raise_or_refresh) and a second run finds nothing open.
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
    SUPPRESSED_TASK_CODES,
    SUPPRESSED_TASK_RESULT,
    _json,
)
from audit_core.uc03_p2_tasks import record_task_event

logger = structlog.get_logger(__name__)

_CODES = sorted(SUPPRESSED_TASK_CODES)
# The checks removed from the catalogue: their check rows go, their findings close.
RETIRED_CODES = ("CASH_INTIMATION_UNCONFIRMED", "CASH_NOT_INTIMATED")
_FINDING_NOTE = ("Cash transactions raise no tasks (product owner, 2026-10-31): the cash intimation "
                 "question is no longer asked.")


def _open_tasks(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT task_id, journey_id, source_code, task_type, task_status
            FROM auditcore.p2_tasks
            WHERE tenant_id = :t AND source_code = ANY(:codes) AND task_status = ANY(:open)
            ORDER BY created_at_utc
            FOR UPDATE
            """
        ),
        {"t": tenant_id, "codes": _CODES, "open": list(_OPEN)},
    ).mappings().all()
    return [dict(r) for r in rows]


def _open_retired_findings(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT audit_finding_id, journey_id, stage_code, rule_key
            FROM auditcore.audit_findings
            WHERE tenant_id = :t AND rule_key = ANY(:codes) AND finding_status IN ('OPEN','ACKNOWLEDGED')
              AND journey_id IS NOT NULL
            ORDER BY created_at_utc
            """
        ),
        {"t": tenant_id, "codes": list(RETIRED_CODES)},
    ).mappings().all()
    return [dict(r) for r in rows]


def _retired_state_rows(connection: Connection, tenant_id: str) -> Counter:
    rows = connection.execute(
        text(
            """
            SELECT control_code, count(*) AS n FROM auditcore.p2_control_state
            WHERE tenant_id = :t AND control_code = ANY(:codes) GROUP BY control_code
            """
        ),
        {"t": tenant_id, "codes": list(RETIRED_CODES)},
    ).all()
    return Counter({str(code): int(n) for code, n in rows})


def clean_tenant(connection: Connection, *, tenant_id: str, tag: str, commit: bool) -> dict[str, Any]:
    """One Project: what is open now and, with ``commit``, closed. Returns the
    counts (tasks per code and status, findings, check rows)."""
    tasks = _open_tasks(connection, tenant_id)
    findings = _open_retired_findings(connection, tenant_id)
    states = _retired_state_rows(connection, tenant_id)
    summary: dict[str, Any] = {
        "tasks": len(tasks),
        "tasksByCode": dict(Counter(str(t["source_code"]) for t in tasks)),
        "tasksByStatus": dict(Counter(str(t["task_status"]) for t in tasks)),
        "journeys": len({str(t["journey_id"]) for t in tasks}),
        "findings": len(findings),
        "findingsByCode": dict(Counter(str(f["rule_key"]) for f in findings)),
        "controlStateRows": sum(states.values()),
        "controlStateRowsByCode": dict(states),
        "cancelled": 0, "findingsResolved": 0, "controlStateRowsDeleted": 0,
    }
    if not commit:
        return summary
    cancelled = connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks
            SET task_status = 'CANCELLED',
                completion_result = COALESCE(completion_result, '{}'::jsonb) || CAST(:result AS jsonb),
                updated_at_utc = now()
            WHERE tenant_id = :t AND task_id = ANY(:ids) AND task_status = ANY(:open)
            RETURNING task_id, journey_id, source_code, task_status
            """
        ),
        {"t": tenant_id, "ids": [t["task_id"] for t in tasks], "open": list(_OPEN),
         "result": _json(SUPPRESSED_TASK_RESULT)},
    ).mappings().all()
    before = {str(t["task_id"]): str(t["task_status"]) for t in tasks}
    for row in cancelled:
        record_task_event(
            connection, tenant_id=tenant_id, journey_id=UUID(str(row["journey_id"])),
            task_id=UUID(str(row["task_id"])), event_type="CANCELLED", actor_id="SYSTEM",
            actor_role_code="SYSTEM",
            details={**SUPPRESSED_TASK_RESULT, "sourceCode": row["source_code"],
                     "previousStatus": before.get(str(row["task_id"])), "job": "CASH_TASK_CLEANUP", "tag": tag},
        )
    summary["cancelled"] = len(cancelled)

    from audit_core.uc03_manual_verification import _resolve_finding

    for finding in findings:
        _resolve_finding(
            connection, tenant_id=tenant_id, journey_id=UUID(str(finding["journey_id"])),
            stage_code=str(finding["stage_code"] or "BOOKING"), finding_id=UUID(str(finding["audit_finding_id"])),
            actor_id=None, correlation_id=f"cash-task-cleanup:{tag}", note=_FINDING_NOTE,
        )
    summary["findingsResolved"] = len(findings)
    deleted = connection.execute(
        text("DELETE FROM auditcore.p2_control_state WHERE tenant_id = :t AND control_code = ANY(:codes)"),
        {"t": tenant_id, "codes": list(RETIRED_CODES)},
    )
    summary["controlStateRowsDeleted"] = int(deleted.rowcount or 0)
    return summary


def run_cash_task_cleanup(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    commit = tag.endswith("-commit")
    for tenant_id in tenant_ids:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                summary = clean_tenant(connection, tenant_id=tenant_id, tag=tag, commit=commit)
        except Exception:
            logger.warning("cash_task_cleanup_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        for code in _CODES:
            count = summary["tasksByCode"].get(code, 0)
            if count:
                logger.info("cash_task_cleanup_code", tag=tag, tenant_id=tenant_id, commit=commit,
                            control_code=code, open_tasks=count)
        logger.info("cash_task_cleanup_finished", tag=tag, tenant_id=tenant_id, commit=commit,
                    **{k: (v if isinstance(v, int) else str(v)) for k, v in summary.items()})
