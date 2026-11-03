"""RULES_RERUN (2026-11-03): every booking's checks run again after a rule changes.

A booking's checks (uc03_p2_audit_rules, through CONTROL_EVALUATE) run when
its facts change. When a rule itself changes, a booking whose documents did
not change keeps the findings and tasks the old rule raised. Live 2026-11-03:
after the billing-documents rule of 2 Nov (only billing documents count, never
the booking docket), 178 open "Deal charged below the standard price" tasks
still read only booking-form figures, last refreshed before the rule changed.

This job asks for each booking's checks to run again, forced, exactly as
AUDIT_TASK_CLEANUP does for its bookings. A finding whose check now passes,
or can no longer be judged, is resolved and its task closed. One that still
fails is refreshed with what the check now finds. Nothing is decided here:
the checks decide, in the worker, the usual way.

Worker job ``RULES_RERUN``: tag ``<tenant_id>:<label>``, reports the journey
references, or none for every booking of the Project. A DRY RUN (counts only)
unless the label ends with "-commit".
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

JOB = "RULES_RERUN"


def _parse(tag: str) -> tuple[str, str, bool]:
    tenant_id, sep, label = (tag or "").partition(":")
    if not sep or not tenant_id.strip() or not label.strip():
        raise ValueError(f"{JOB}: tag must be '<tenant_id>:<label>'")
    return tenant_id.strip(), label.strip(), label.strip().endswith("-commit")


def run_rules_rerun(engine: Engine, tag: str, reports: list[str]) -> dict[str, Any]:
    from audit_core.uc03_audit_task_cleanup import _rerun_native

    tenant_id, label, commit = _parse(tag)
    references = [str(r).strip() for r in reports or [] if str(r).strip()]
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        rows = connection.execute(
            text(
                "SELECT journey_id, journey_reference FROM auditcore.journeys WHERE tenant_id=:t "
                + ("AND journey_reference = ANY(:r) " if references else "")
                + "ORDER BY journey_reference"
            ),
            {"t": tenant_id, "r": references},
        ).mappings().all()
    units = queued = failed = 0
    for row in rows:
        if not commit:
            continue
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                units += len(_rerun_native(connection, tenant_id, UUID(str(row["journey_id"])), label))
                queued += 1
        except Exception:
            failed += 1
            logger.warning("rules_rerun_failed", tag=tag, reference=row["journey_reference"], exc_info=True)
    found = {str(r["journey_reference"]) for r in rows}
    result = {"commit": commit, "bookings": len(rows), "queued": queued, "units": units, "failed": failed,
              "notFound": [r for r in references if r not in found]}
    logger.info("rules_rerun_finished", tag=tag, **{k: v for k, v in result.items() if k != "notFound"})
    return result


__all__ = ["JOB", "run_rules_rerun"]
