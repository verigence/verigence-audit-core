"""Read gate passes again for the chassis number (closing list item 5, 2026-11-16).

The gate pass reader had no chassis field until gate_pass schema 1.1
(verigence-di migration 0068): 612 of 621 gate passes on file gave no VIN, so
the vehicle that left the gate could not be checked against the sale's VIN.
This job asks Document Intelligence to read those gate passes again (the same
forced re-read the statement re-read and the insurance re-read use); the new
reading arrives as a fact change and the Journey's checks run on it.

A page is left alone, and counted, when a person corrected one of its values,
its stage is marked reviewed, or it is still being read.

The same job, for another document type and field (run_field_reread):
DELIVERY_ORDER_LOAN_REREAD reads the Delivery Orders on file again for the
loan amount (delivery_order_cover schema 1.2, verigence-di 0069; product owner
2026-11-16), a DO read under 1.2 holding a loan_amount reading.

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
DELIVERY_ORDER_TYPES = ("delivery_order_cover",)


def run_gate_pass_chassis_reread(engine: Engine, tag: str, tenant_ids: list[str]) -> dict[str, Any]:
    return run_field_reread(engine, tag, tenant_ids, types=GATE_PASS_TYPES, field="chassis_number",
                            event="gate_pass_chassis_reread")


def run_delivery_order_loan_reread(engine: Engine, tag: str, tenant_ids: list[str]) -> dict[str, Any]:
    return run_field_reread(engine, tag, tenant_ids, types=DELIVERY_ORDER_TYPES, field="loan_amount",
                            event="delivery_order_loan_reread")


def run_field_reread(engine: Engine, tag: str, tenant_ids: list[str], *, types: tuple[str, ...], field: str,
                     event: str) -> dict[str, Any]:
    """Read again the pages of ``types`` on Journeys where no document of
    them holds a ``field`` reading yet (a new field of their schema)."""
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
                                AND f.field_key = :field)
                        ORDER BY j.journey_reference
                        """
                    ),
                    {"t": tenant_id, "types": list(types), "field": field},
                ).mappings().all()]
                pages = [p for j in journeys for p in insurance_pages(connection, tenant_id, j, types)]
        except Exception:
            logger.warning(f"{event}_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
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
                logger.warning(f"{event}_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
        summary = {"journeys": len(journeys), "pagesToReread": len(to_reread),
                   **{f"skipped_{k.lower()}": v for k, v in skipped.items()},
                   **{f"reread_{k}": v for k, v in results.items()}}
        report["tenants"][tenant_id] = summary
        logger.info(f"{event}_finished", tag=tag, tenant_id=tenant_id, commit=commit, **summary)
    return report


__all__ = ["run_delivery_order_loan_reread", "run_field_reread", "run_gate_pass_chassis_reread"]
