"""Read gate passes again for the chassis number (closing list item 5, 2026-11-16).

The gate pass reader had no chassis field until gate_pass schema 1.1
(verigence-di migration 0068): 612 of 621 gate passes on file gave no VIN, so
the vehicle that left the gate could not be checked against the sale's VIN.
This job asks Document Intelligence to read those gate passes again (the same
forced re-read the statement re-read and the insurance re-read use); the new
reading arrives as a fact change and the Journey's checks run on it.

A page is left alone, and counted, when a person corrected one of its values,
its stage is marked reviewed, or it is still being read.

Worker job GATE_PASS_CHASSIS_REREAD. A DRY RUN (counts only) unless the tag
ends with "-commit"; "-sample" takes the first page only. Safe to run again:
a gate pass read under 1.1 holds a chassis_number reading (even an empty one)
and is not picked.
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_insurance_code_repair import insurance_pages

logger = structlog.get_logger(__name__)

GATE_PASS_TYPES = ("gate_pass",)


def run_gate_pass_chassis_reread(engine: Engine, tag: str, tenant_ids: list[str]) -> dict[str, Any]:
    from audit_core.uc03_statement_reread import _reread

    commit = tag.endswith("-commit")
    sample = "-sample" in tag
    report: dict[str, Any] = {"commit": commit, "tenants": {}}
    for tenant_id in tenant_ids:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                journeys = [dict(r) for r in connection.execute(
                    text(
                        """
                        SELECT DISTINCT j.journey_id, j.journey_reference
                        FROM auditcore.journeys j
                        JOIN auditcore.evidence e
                          ON e.tenant_id = j.tenant_id AND e.journey_id = j.journey_id
                         AND e.association_status = 'ACTIVE' AND e.document_type_key = ANY(CAST(:types AS text[]))
                        WHERE j.tenant_id = :t
                          AND NOT EXISTS (
                              SELECT 1 FROM auditcore.journey_document_extracted_fields f
                              WHERE f.tenant_id = e.tenant_id AND f.di_document_id = e.di_document_id
                                AND f.field_key = 'chassis_number')
                        ORDER BY j.journey_reference
                        """
                    ),
                    {"t": tenant_id, "types": list(GATE_PASS_TYPES)},
                ).mappings().all()]
                pages = [p for j in journeys for p in insurance_pages(connection, tenant_id, j, GATE_PASS_TYPES)]
        except Exception:
            logger.warning("gate_pass_chassis_reread_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        to_reread = [p for p in pages if p["decision"] == "REREAD"]
        skipped: dict[str, int] = {}
        for page in pages:
            if page["decision"] != "REREAD":
                skipped[page["decision"]] = skipped.get(page["decision"], 0) + 1
        if sample:
            to_reread = to_reread[:1]
        results: dict[str, int] = {}
        if commit and to_reread:
            try:
                results = _reread(engine, tenant_id, to_reread, tag)
            except Exception:
                logger.warning("gate_pass_chassis_reread_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
        summary = {"journeys": len(journeys), "pagesToReread": len(to_reread),
                   **{f"skipped_{k.lower()}": v for k, v in skipped.items()},
                   **{f"reread_{k}": v for k, v in results.items()}}
        report["tenants"][tenant_id] = summary
        logger.info("gate_pass_chassis_reread_finished", tag=tag, tenant_id=tenant_id, commit=commit, **summary)
    return report


__all__ = ["run_gate_pass_chassis_reread"]
