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


def _documents_say(connection, *, tenant_id: str, journey_id: UUID) -> str | None:
    """What the documents alone decide (the PC's answer set aside): INHOUSE,
    SELF, or None when they do not decide."""
    from audit_core.uc03_insurance_source import (
        document_decision,
        ndc_says_own,
        ndc_without_premium,
    )

    if document_decision(connection, tenant_id=tenant_id, journey_id=journey_id) is not None:
        return "INHOUSE"
    if ndc_says_own(connection, tenant_id=tenant_id, journey_id=journey_id) is not None \
            or ndc_without_premium(connection, tenant_id=tenant_id, journey_id=journey_id):
        return "SELF"
    return None


def run_insurance_source_review(engine: Engine, tag: str, tenant_ids: list[str]) -> dict:
    """Returns the counts and, per Project, the PC answers the documents now
    contradict (2026-11-17), kept on the job row."""
    from audit_core.uc03_insurance_source import resolve, sync_insurance_source_task

    commit = tag.endswith("-commit")
    report: dict = {"commit": commit, "tenants": {}}
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
                    documents = _documents_say(connection, tenant_id=tenant_id, journey_id=journey_id) \
                        if decision["decidedBy"] == "PC" else None
                    if commit:
                        sync_insurance_source_task(connection, tenant_id=tenant_id, journey_id=journey_id)
            except Exception:
                counts["failed"] = counts.get("failed", 0) + 1
                logger.warning("insurance_source_review_failed", tag=tag, tenant_id=tenant_id,
                               journey=journey["journey_reference"], exc_info=True)
                continue
            key = f"{decision['decidedBy']}_{decision['source']}".lower()
            counts[key] = counts.get(key, 0) + 1
            pc = "INHOUSE" if decision["source"] == "INHOUSE" else "SELF"
            if documents is not None and documents != pc:
                counts["pc_against_documents"] = counts.get("pc_against_documents", 0) + 1
                tenant_report = report["tenants"].setdefault(tenant_id, {})
                tenant_report.setdefault("pcAgainstDocuments", []).append(
                    {"journey": journey["journey_reference"], "pc": pc, "documents": documents})
            if decision["decidedBy"] in ("DOCUMENT", "UNCONFIRMED"):
                logger.info("insurance_source_review_item", tag=tag, tenant_id=tenant_id, commit=commit,
                            journey=journey["journey_reference"], decided_by=decision["decidedBy"],
                            source=decision["source"], basis=decision["basis"])
        logger.info("insurance_source_review_finished", tag=tag, tenant_id=tenant_id, commit=commit,
                    journeys=len(journeys), **dict(sorted(counts.items())))
        report["tenants"].setdefault(tenant_id, {}).update({"journeys": len(journeys), **dict(sorted(counts.items()))})
    return report
