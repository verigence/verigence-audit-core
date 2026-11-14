"""SAME_SALE_SETTLE (2026-11-12): the copies already on file, settled once.

Product owner 2026-11-12, AVINASH PATRA (UTK-PR-261103-001 booking file, UTK-PR-261104-002
delivery file): "if the same details are found but with additional documents then we will
update the existing booking"; and "i never put a rule of 48 hours.. if all the documents
matches, add additional documents, if it exactly same documents auto delete the booking".
The same-file check (uc03_p2_same_file) now acts on a same-outlet copy however old and
knows one sale by its dealer receipt. This job runs that check once on every journey that
shares a PAN, an Aadhaar or a dealer receipt number with another journey of its outlet:
the documents only the smaller copy holds are carried to the one kept, the copy is
deleted, and both show under Duplicate bookings -- exactly as a new upload is handled. A
company's file with documents of its own, a reviewed copy, or one holding a document that
cannot be carried is kept and alerted, as always.

Product owner 2026-11-14 (one rule, uc03_p2_same_file): every journey of a dealership sharing
a PAN, an Aadhaar, a mobile, a GSTIN or a dealer receipt number with another journey of the
same dealership is checked again ("check all in duplicate bookings having same PAN or same
aadhar"); the copy with fewer pages (the older on a tie) is deleted after what only it holds
is copied over. Nothing keeps it any more but two people, two vehicles or a closed journey.

Worker job ``SAME_SALE_SETTLE``: tag ``<tenant_id>:<label>``; reports the journey
references to look at, or none for every such journey. A DRY RUN unless the label ends
with "-commit": the check runs inside a transaction that is rolled back, and the outcome
for each pair is returned.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

JOB = "SAME_SALE_SETTLE"

_CANDIDATES_SQL = """
    WITH k AS (
        SELECT DISTINCT f.journey_id,
               CASE WHEN f.field_key IN ('buyer_gstin', 'customer_gstin') THEN 'gstin' ELSE f.field_key END
               || ':' || CASE
                   WHEN f.field_key = 'aadhaar_number'
                       THEN regexp_replace(f.effective_value #>> '{}', '[^0-9]', '', 'g')
                   WHEN f.field_key = 'customer_phone'
                       THEN right(regexp_replace(f.effective_value #>> '{}', '[^0-9]', '', 'g'), 10)
                   ELSE regexp_replace(upper(f.effective_value #>> '{}'), '[^A-Z0-9]', '', 'g') END AS code
        FROM auditcore.journey_document_extracted_fields f
        WHERE f.tenant_id = :t AND f.effective_value IS NOT NULL AND f.effective_value <> 'null'::jsonb
          AND (f.field_key IN ('pan_number', 'aadhaar_number', 'customer_phone', 'gstin', 'buyer_gstin',
                               'customer_gstin')
               OR (f.field_key = 'receipt_number' AND f.source_document_type_key = 'dealer_receipt'))
          AND EXISTS (SELECT 1 FROM auditcore.evidence e
                      WHERE e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id
                        AND e.di_document_id = f.di_document_id AND e.association_status = 'ACTIVE')
    )
    SELECT DISTINCT j.journey_id, j.journey_reference, j.created_at_utc
    FROM k a
    JOIN k b ON a.code = b.code AND a.journey_id <> b.journey_id AND length(a.code) >= 14
    JOIN auditcore.journeys j ON j.tenant_id = :t AND j.journey_id = a.journey_id
    JOIN auditcore.journeys o ON o.tenant_id = :t AND o.journey_id = b.journey_id AND o.dealer_id = j.dealer_id
    ORDER BY j.created_at_utc
"""


def _parse(tag: str) -> tuple[str, bool]:
    tenant_id, sep, label = (tag or "").partition(":")
    if not sep or not tenant_id.strip() or not label.strip():
        raise ValueError(f"{JOB}: tag must be '<tenant_id>:<label>'")
    return tenant_id.strip(), label.strip().endswith("-commit")


def run_same_sale_settle(engine: Engine, tag: str, reports: list[str]) -> dict[str, Any]:
    from audit_core.uc03_p2_same_file import ALERT_KIND, _detect

    tenant_id, commit = _parse(tag)
    wanted = {str(r).strip() for r in reports or [] if str(r).strip()}
    connection = engine.connect()
    transaction = connection.begin()
    try:
        set_tenant_context(connection, tenant_id)
        rows = connection.execute(text(_CANDIDATES_SQL), {"t": tenant_id}).mappings().all()
        rows = [r for r in rows if not wanted or r["journey_reference"] in wanted]
        failed = 0
        for row in rows:
            try:
                with connection.begin_nested():
                    _detect(connection, tenant_id=tenant_id, journey_id=UUID(str(row["journey_id"])),
                            correlation_id=f"same-sale-settle:{tag}")
            except Exception:
                failed += 1
                logger.warning("same_sale_settle_failed", tag=tag, reference=row["journey_reference"],
                               exc_info=True)
        # What this run raised: inside one transaction now() is its start, the alerts' own time.
        alerts = connection.execute(
            text(
                """
                SELECT a.subject_journey_reference AS copy, k.journey_reference AS kept, a.outcome,
                       a.payload ->> 'keptReason' AS kept_reason, a.payload -> 'carryDocuments' AS carry
                FROM auditcore.p2_tenant_alerts a
                LEFT JOIN auditcore.journeys k ON k.tenant_id = a.tenant_id AND k.journey_id = a.related_journey_id
                WHERE a.tenant_id = :t AND a.alert_kind = :k AND (a.created_at_utc = now() OR a.updated_at_utc = now())
                ORDER BY a.subject_journey_reference
                """
            ),
            {"t": tenant_id, "k": ALERT_KIND},
        ).mappings().all()
        pairs = [{"copy": a["copy"], "kept": a["kept"], "outcome": a["outcome"],
                  **({"keptReason": a["kept_reason"]} if a["kept_reason"] else {}),
                  **({"carry": a["carry"] if isinstance(a["carry"], list) else json.loads(a["carry"])}
                     if a["carry"] else {})}
                 for a in alerts]
        if commit:
            transaction.commit()
        else:
            transaction.rollback()
    except Exception:
        transaction.rollback()
        raise
    finally:
        connection.close()
    result = {"commit": commit, "journeys": len(rows), "failed": failed, "pairs": pairs,
              "toDelete": sum(1 for p in pairs if p["outcome"] == "PENDING_DELETE"),
              "kept": sum(1 for p in pairs if p["outcome"] != "PENDING_DELETE")}
    logger.info("same_sale_settle_finished", tag=tag, journeys=len(rows), failed=failed,
                to_delete=result["toDelete"], kept=result["kept"])
    return result


__all__ = ["JOB", "run_same_sale_settle"]
