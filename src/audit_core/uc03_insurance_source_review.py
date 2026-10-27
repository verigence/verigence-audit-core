"""Apply the in-house / external insurance rule to every Journey (2026-10-27).

New and changed Journeys follow uc03_insurance_source as they are rechecked;
this job applies it to all Journeys at once: one log line per Journey with what
the rule decides (PC, DOCUMENT with its reason, or UNCONFIRMED -> a PC task).

Started as a worker job (kind INSURANCE_SOURCE_REVIEW). A DRY RUN -- nothing
raised -- unless the tag ends with "-commit". Safe to run again.
"""
from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)


def run_insurance_source_review(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    from audit_core.uc03_insurance_source import resolve, sync_insurance_source_task

    commit = tag.endswith("-commit")
    for tenant_id in tenant_ids:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            journeys = connection.execute(
                text("SELECT j.journey_id, j.journey_reference FROM auditcore.journeys j "
                     "JOIN auditcore.insurance_records r ON r.tenant_id = j.tenant_id AND r.journey_id = j.journey_id "
                     "WHERE j.tenant_id = :t ORDER BY j.journey_reference"),
                {"t": tenant_id},
            ).mappings().all()
        counts: dict[str, int] = {}
        for journey in journeys:
            journey_id = UUID(str(journey["journey_id"]))
            try:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    decision = resolve(connection, tenant_id=tenant_id, journey_id=journey_id)
                    if commit:
                        sync_insurance_source_task(connection, tenant_id=tenant_id, journey_id=journey_id)
            except Exception:
                counts["failed"] = counts.get("failed", 0) + 1
                logger.warning("insurance_source_review_failed", tag=tag, tenant_id=tenant_id,
                               journey=journey["journey_reference"], exc_info=True)
                continue
            key = f"{decision['decidedBy']}_{decision['source']}".lower()
            counts[key] = counts.get(key, 0) + 1
            if decision["decidedBy"] in ("DOCUMENT", "UNCONFIRMED"):
                logger.info("insurance_source_review_item", tag=tag, tenant_id=tenant_id, commit=commit,
                            journey=journey["journey_reference"], decided_by=decision["decidedBy"],
                            source=decision["source"], basis=decision["basis"])
        logger.info("insurance_source_review_finished", tag=tag, tenant_id=tenant_id, commit=commit,
                    journeys=len(journeys), **dict(sorted(counts.items())))
