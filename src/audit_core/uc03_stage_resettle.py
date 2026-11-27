"""STAGE_RESETTLE (2026-11-27): open deliveries settled again after a closure rule changes.

Whether a delivery file is closed is decided by the stage engine when the Journey is settled
(uc03_p2_stage.recompute_journey_stage, through STAGE_RECOMPUTE), and a Journey is settled when
its documents or tasks change. When the rule itself changes -- 27 Nov 2026: the debit note is
optional, never mandatory -- a delivery nothing has touched since keeps the old answer: its
mandatory-documents gate still lists the debit note and the file stays open.

This job asks for each open delivery to be settled again, the usual way
(auditcore.p2_request_stage_recompute). Nothing is decided here: the stage engine decides, in
the worker, and closes the files that now meet the one closure rule (docs/rules/RULES.md,
"Delivery file closed").

Settling a Journey also rebuilds its one "Upload the missing documents" task from the
documents now mandatory, so a document that stopped being mandatory comes off the task, and the
task closes when nothing else is on it (product owner 2026-11-27: "existing debit notes tasks to
be closed and no new task for the same").

Worker job ``STAGE_RESETTLE``: tag ``<tenant_id>:<label>``, reports the journey references
(settled again whatever their state), or none for every open delivery of the Project (a Journey
that is a delivery by the one delivery rule, uc03_delivery_start, and whose delivery is not
complete) and every Journey with an open missing-document task. A DRY RUN (counts only) unless
the label ends with "-commit".
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_delivery_start import DELIVERY_STARTED_SQL

logger = structlog.get_logger(__name__)

JOB = "STAGE_RESETTLE"

_OPEN_TASK = ("VERIFIED_COMPLETE", "CANCELLED", "FAILED", "DEAD_LETTER")
_OPEN_DELIVERY = ("COALESCE(r.delivery_completion_state, '') <> 'COMPLETE' AND "
                  + DELIVERY_STARTED_SQL.format(t=":t", j="j.journey_id"))
_MISSING_TASK = """EXISTS (SELECT 1 FROM auditcore.p2_tasks k WHERE k.tenant_id=j.tenant_id
                  AND k.journey_id=j.journey_id AND k.task_type='DOCUMENT_MISSING'
                  AND k.task_status <> ALL(:closed))"""
_JOURNEYS = f"""
    SELECT j.journey_id, j.journey_reference, ({_OPEN_DELIVERY}) AS open_delivery,
           {_MISSING_TASK} AS missing_task
    FROM auditcore.journeys j
    LEFT JOIN auditcore.p2_journey_runtime r ON r.tenant_id=j.tenant_id AND r.journey_id=j.journey_id
    WHERE j.tenant_id=:t AND """


def _parse(tag: str) -> tuple[str, str, bool]:
    tenant_id, sep, label = (tag or "").partition(":")
    if not sep or not tenant_id.strip() or not label.strip():
        raise ValueError(f"{JOB}: tag must be '<tenant_id>:<label>'")
    return tenant_id.strip(), label.strip(), label.strip().endswith("-commit")


def run_stage_resettle(engine: Engine, tag: str, reports: list[str]) -> dict[str, Any]:
    tenant_id, _label, commit = _parse(tag)
    references = [str(r).strip() for r in reports or [] if str(r).strip()]
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        rows = connection.execute(
            text(_JOURNEYS + ("j.journey_reference = ANY(:r)" if references
                              else f"(({_OPEN_DELIVERY}) OR {_MISSING_TASK})")
                 + " ORDER BY j.journey_reference"),
            {"t": tenant_id, "r": references, "closed": list(_OPEN_TASK)},
        ).mappings().all()
        queued = 0
        if commit and rows:
            queued = int(connection.execute(
                text("SELECT count(auditcore.p2_request_stage_recompute(:t, x.journey_id)) "
                     "FROM unnest(CAST(:ids AS uuid[])) AS x(journey_id)"),
                {"t": tenant_id, "ids": [str(r["journey_id"]) for r in rows]},
            ).scalar_one() or 0)
    found = {str(r["journey_reference"]) for r in rows}
    result = {"commit": commit, "journeys": len(rows),
              "openDeliveries": sum(1 for r in rows if r["open_delivery"]),
              "withMissingDocumentTask": sum(1 for r in rows if r["missing_task"]),
              "queued": queued, "notFound": [r for r in references if r not in found]}
    logger.info("stage_resettle_finished", tag=tag, **{k: v for k, v in result.items() if k != "notFound"})
    return result


__all__ = ["JOB", "run_stage_resettle"]
