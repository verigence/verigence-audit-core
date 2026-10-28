"""Read insurance policies again for the financier they are hypothecated to
(product owner 2026-10-27).

A policy prints "Hypothecation" / "Hypothecated to" / "HPA" with the financier,
and that alone proves a loan (uc03_finance_rule). Document Intelligence reads it
since insurance_cover 1.3 (field hypothecated_to); every policy read before has
no such reading. This job asks Document Intelligence to read those policies
again (reprocess force, the same request and copy the insurance code repair and
statement re-read use) and then brings the Journey's finance record to the
finance rule, so a loan the policy proves is recorded.

A page is left alone, and listed, when a person corrected one of its values,
its stage is marked reviewed, or it is still being read (insurance_pages).

Started as a worker job (kind INSURANCE_HYPOTHECATION_REREAD). A DRY RUN -- one
``insurance_hypothecation_reread_item`` log line per Journey, nothing asked or
changed -- unless the tag ends with "-commit". A tag containing "-sample" takes
only the first page, to prove the round trip before reading them all. Safe to
run again: a policy once read under 1.3 holds a hypothecated_to reading (even an
empty one) and is not picked.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from audit_core.db import set_tenant_context
from audit_core.uc03_insurance_code_repair import _INSURANCE_TYPES, insurance_pages

logger = structlog.get_logger(__name__)

_CHANGES = ("CREATED", "UPDATED", "REMOVED")


def journeys_to_reread(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    """Journeys with a live insurance policy that has no hypothecated_to reading."""
    return [dict(r) for r in connection.execute(
        text(
            """
            SELECT DISTINCT j.journey_id, j.journey_reference
            FROM auditcore.journeys j
            JOIN auditcore.evidence e
              ON e.tenant_id = j.tenant_id AND e.journey_id = j.journey_id AND e.association_status = 'ACTIVE'
             AND e.document_type_key = ANY(CAST(:types AS text[]))
            WHERE j.tenant_id = :t
              AND NOT EXISTS (
                  SELECT 1 FROM auditcore.journey_document_extracted_fields f
                  WHERE f.tenant_id = e.tenant_id AND f.di_document_id = e.di_document_id
                    AND f.field_key = 'hypothecated_to')
            ORDER BY j.journey_reference
            """
        ),
        {"t": tenant_id, "types": list(_INSURANCE_TYPES)},
    ).mappings().all()]


def _documents_to_reread(connection: Connection, tenant_id: str) -> set[str]:
    """Live insurance policies (ACTIVE evidence) with no hypothecated_to reading."""
    return {str(d) for d in connection.execute(
        text(
            """
            SELECT e.di_document_id FROM auditcore.evidence e
            WHERE e.tenant_id = :t AND e.association_status = 'ACTIVE'
              AND e.document_type_key = ANY(CAST(:types AS text[]))
              AND NOT EXISTS (
                  SELECT 1 FROM auditcore.journey_document_extracted_fields f
                  WHERE f.tenant_id = e.tenant_id AND f.di_document_id = e.di_document_id
                    AND f.field_key = 'hypothecated_to')
            """
        ),
        {"t": tenant_id, "types": list(_INSURANCE_TYPES)},
    ).scalars().all()}


def run_insurance_hypothecation_reread(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    from audit_core.uc03_finance_rule import apply_finance_rule
    from audit_core.uc03_p2_runtime import enqueue_work
    from audit_core.uc03_statement_reread import _reread

    commit = tag.endswith("-commit")
    sample = "-sample" in tag
    for tenant_id in tenant_ids:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                journeys = journeys_to_reread(connection, tenant_id)
                wanted = _documents_to_reread(connection, tenant_id)
                # Only the policy still on file: a page of a policy since replaced
                # (its evidence SUPERSEDED) cannot be copied in (2026-10-28).
                pages_by_journey = {
                    str(j["journey_id"]): [p for p in insurance_pages(connection, tenant_id, j)
                                           if str(p["diDocumentId"]) in wanted]
                    for j in journeys}
        except Exception:
            logger.warning("insurance_hypothecation_reread_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        to_reread: list[dict[str, Any]] = []
        skipped: dict[str, int] = {}
        for journey in journeys:
            pages = pages_by_journey[str(journey["journey_id"])]
            for page in pages:
                if page["decision"] == "REREAD":
                    to_reread.append(page)
                else:
                    skipped[page["decision"]] = skipped.get(page["decision"], 0) + 1
            logger.info("insurance_hypothecation_reread_item", tag=tag, tenant_id=tenant_id, commit=commit,
                        journey=journey["journey_reference"],
                        pages=[{"decision": p["decision"], "stage": p["stage"], "queueStatus": p["queueStatus"]}
                               for p in pages])
        if sample:
            to_reread = to_reread[:1]
        results: dict[str, int] = {}
        finance: dict[str, int] = {}
        hypothecated = 0
        if commit and to_reread:
            try:
                results = _reread(engine, tenant_id, to_reread, tag)
            except Exception:
                logger.warning("insurance_hypothecation_reread_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            for journey_id in dict.fromkeys(UUID(str(p["journeyId"])) for p in to_reread):
                reference = next(p["journeyReference"] for p in to_reread if UUID(str(p["journeyId"])) == journey_id)
                try:
                    with engine.begin() as connection:
                        set_tenant_context(connection, tenant_id)
                        financier = connection.execute(
                            text(
                                """
                                SELECT f.effective_value #>> '{}' FROM auditcore.journey_document_extracted_fields f
                                JOIN auditcore.evidence e
                                  ON e.tenant_id = f.tenant_id AND e.di_document_id = f.di_document_id
                                 AND e.association_status = 'ACTIVE'
                                WHERE f.tenant_id = :t AND f.journey_id = :j AND f.field_key = 'hypothecated_to'
                                  AND f.effective_value IS NOT NULL AND f.effective_value <> 'null'::jsonb
                                ORDER BY f.source_fact_version DESC NULLS LAST, f.updated_at_utc DESC LIMIT 1
                                """
                            ),
                            {"t": tenant_id, "j": journey_id},
                        ).scalar_one_or_none()
                        result = apply_finance_rule(connection, tenant_id=tenant_id, journey_id=journey_id)
                        if result["action"] in _CHANGES:
                            enqueue_work(connection, tenant_id=tenant_id, journey_id=journey_id,
                                         work_type="JOURNEY_RECONCILE", work_key=str(journey_id),
                                         payload={"reason": "FINANCE_RULE"},
                                         correlation_id=f"insurance-hypothecation:{tag}")
                except Exception:
                    finance["failed"] = finance.get("failed", 0) + 1
                    logger.warning("insurance_hypothecation_reread_finance_failed", tag=tag, tenant_id=tenant_id,
                                   journey=reference, exc_info=True)
                    continue
                hypothecated += 1 if financier else 0
                finance[result["action"]] = finance.get(result["action"], 0) + 1
                logger.info("insurance_hypothecation_reread_finance", tag=tag, tenant_id=tenant_id, journey=reference,
                            hypothecated_to=financier, action=result["action"], before=result["before"],
                            provider=result["provider"], basis=result["basis"])
        logger.info("insurance_hypothecation_reread_finished", tag=tag, tenant_id=tenant_id, commit=commit,
                    journeys=len(journeys), pages_to_reread=len(to_reread), policies_hypothecated=hypothecated,
                    **{f"skipped_{k.lower()}": v for k, v in skipped.items()},
                    **{f"reread_{k}": v for k, v in results.items()},
                    **{f"finance_{k.lower()}": v for k, v in finance.items()})
