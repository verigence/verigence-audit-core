"""Bring every Journey's finance record to the finance rule (2026-10-27).

uc03_finance_rule now keeps new and changed Journeys right; this job corrects
what is already stored: loans from "CASH", loans no current document supports
any more (their invoice was voided), financed deals with no finance record, and
financier names that never followed a corrected document.

Started as a worker job (kind FINANCE_RULE_REPAIR). A DRY RUN -- one
``finance_rule_repair_item`` log line per Journey that would change, nothing
written -- unless the tag ends with "-commit". Records a person entered, and
loans whose disbursement a PC confirmed, are listed and left alone. Each
changed Journey is queued for a reconcile so its stage conditions (finance
case) and tasks follow. Safe to run again.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

_CHANGES = ("CREATED", "UPDATED", "REMOVED")
_LEFT_ALONE = ("KEPT_PERSON_ENTERED", "KEPT_PC_CONFIRMED_DISBURSEMENT")


def run_finance_rule_repair(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    from audit_core.uc03_finance_rule import apply_finance_rule
    from audit_core.uc03_p2_runtime import enqueue_work

    commit = tag.endswith("-commit")
    for tenant_id in tenant_ids:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            journeys = connection.execute(
                text("SELECT journey_id, journey_reference FROM auditcore.journeys WHERE tenant_id = :t "
                     "ORDER BY journey_reference"),
                {"t": tenant_id},
            ).mappings().all()
        counts: dict[str, int] = {}
        for journey in journeys:
            journey_id = UUID(str(journey["journey_id"]))
            try:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    result: dict[str, Any] = apply_finance_rule(
                        connection, tenant_id=tenant_id, journey_id=journey_id, write=commit)
                    if commit and result["action"] in _CHANGES:
                        enqueue_work(connection, tenant_id=tenant_id, journey_id=journey_id,
                                     work_type="JOURNEY_RECONCILE", work_key=str(journey_id), payload={"reason": "FINANCE_RULE"},
                                     correlation_id=f"finance-rule:{tag}")
            except Exception:
                counts["failed"] = counts.get("failed", 0) + 1
                logger.warning("finance_rule_repair_failed", tag=tag, tenant_id=tenant_id,
                               journey=journey["journey_reference"], exc_info=True)
                continue
            action = result["action"]
            counts[action] = counts.get(action, 0) + 1
            if action in _CHANGES or action in _LEFT_ALONE:
                logger.info("finance_rule_repair_item", tag=tag, tenant_id=tenant_id, commit=commit,
                            journey=journey["journey_reference"], action=action, before=result["before"],
                            provider=result["provider"], financed_amount=result["financedAmount"],
                            basis=result["basis"])
        logger.info("finance_rule_repair_finished", tag=tag, tenant_id=tenant_id, commit=commit,
                    journeys=len(journeys), **{k.lower(): v for k, v in sorted(counts.items())})
