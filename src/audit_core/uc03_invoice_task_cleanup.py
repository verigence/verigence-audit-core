"""Close the open invoice-check tasks of an ACCOUNT_STATEMENT Project (2026-10-26).

Mahindra Orissa takes its billed figures from the dealer's account statement
and the invoices are withheld from the Deal, so nobody is asked about them any
more: ``sync_invoice_checks`` raises nothing for such a Project. This closes
what was raised before that change -- "Check the reading of an invoice"
(``INVOICE_FIELD_REVIEW``, PC) and "Copies of one invoice disagree"
(``INVOICE_DISCREPANCY_REVIEW``, TL) -- through the same sync, so each finding
is resolved with its task cancelled and the invoices stay on file.

Started from outside with AUDIT_CORE_INVOICE_TASK_CLEANUP_TAG on the worker. A
DRY RUN (one ``invoice_task_cleanup_item`` log line per Journey that has such a
task, nothing written) unless the tag ends with "-commit". Idempotent.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from audit_core.db import set_tenant_context
from audit_core.uc03_deal_source_policy import account_statement_is_source_of_truth
from audit_core.uc03_invoice_materialization import (
    _DISCREPANCY_TASK_TYPE,
    _FIELD_REVIEW_TASK_TYPE,
    check_invoice_field_discrepancies,
)

logger = structlog.get_logger(__name__)

_CLOSED = ("COMPLETED", "CANCELLED", "FAILED", "DEAD_LETTER")
_TASK_TYPES = (_FIELD_REVIEW_TASK_TYPE, _DISCREPANCY_TASK_TYPE)


def _open_invoice_tasks(connection: Connection, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT task_type, task_status, assigned_role_code, related_finding_id
            FROM auditcore.workflow_tasks
            WHERE tenant_id = :t AND journey_id = :j AND task_type = ANY(:types)
              AND task_status <> ALL(:closed)
            ORDER BY task_type, created_at_utc
            """
        ),
        {"t": tenant_id, "j": journey_id, "types": list(_TASK_TYPES), "closed": list(_CLOSED)},
    ).mappings().all()
    return [dict(r) for r in rows]


def _journeys_with_open_tasks(connection: Connection, tenant_id: str) -> list[UUID]:
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT journey_id FROM auditcore.workflow_tasks
            WHERE tenant_id = :t AND task_type = ANY(:types) AND task_status <> ALL(:closed)
              AND journey_id IS NOT NULL
            ORDER BY journey_id
            """
        ),
        {"t": tenant_id, "types": list(_TASK_TYPES), "closed": list(_CLOSED)},
    ).scalars().all()
    return [UUID(str(r)) for r in rows]


def close_journey(
    connection: Connection, *, tenant_id: str, journey_id: UUID, tag: str, commit: bool,
) -> dict[str, Any]:
    """One Journey: the invoice tasks open now and, with ``commit``, closed."""
    before = _open_invoice_tasks(connection, tenant_id, journey_id)
    item: dict[str, Any] = {
        "open": len(before),
        "fieldReview": sum(t["task_type"] == _FIELD_REVIEW_TASK_TYPE for t in before),
        "discrepancyReview": sum(t["task_type"] == _DISCREPANCY_TASK_TYPE for t in before),
    }
    if not commit:
        return item
    result = check_invoice_field_discrepancies(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
        correlation_id=f"invoice-task-cleanup:{tag}")
    item["resolvedFindings"] = result.get("resolved", 0)
    item["openAfter"] = len(_open_invoice_tasks(connection, tenant_id, journey_id))
    return item


def run_invoice_task_cleanup(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    commit = tag.endswith("-commit")
    for tenant_id in tenant_ids:
        totals = {"journeys": 0, "open_tasks": 0, "field_review": 0, "discrepancy_review": 0,
                  "open_after": 0, "failed": 0}
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                if not account_statement_is_source_of_truth(connection, tenant_id=tenant_id):
                    continue
                journeys = _journeys_with_open_tasks(connection, tenant_id)
        except Exception:
            logger.warning("invoice_task_cleanup_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        for journey_id in journeys:
            totals["journeys"] += 1
            try:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    item = close_journey(connection, tenant_id=tenant_id, journey_id=journey_id,
                                         tag=tag, commit=commit)
            except Exception:
                totals["failed"] += 1
                logger.warning("invoice_task_cleanup_item_failed", tag=tag, tenant_id=tenant_id,
                               journey_id=str(journey_id), exc_info=True)
                continue
            totals["open_tasks"] += item["open"]
            totals["field_review"] += item["fieldReview"]
            totals["discrepancy_review"] += item["discrepancyReview"]
            totals["open_after"] += item.get("openAfter", 0)
            logger.info("invoice_task_cleanup_item", tag=tag, tenant_id=tenant_id, commit=commit,
                        journey_id=str(journey_id), **{k: str(v) for k, v in item.items()})
        logger.info("invoice_task_cleanup_finished", tag=tag, tenant_id=tenant_id, commit=commit, **totals)
