"""GST_TRADE_NAME (2026-11-25): bookings of sole proprietors named by their business.

Product owner 2026-11-25: a sole proprietor's GST certificate prints the owner's
own name as its legal name and the business as its trade name, and the purchases
are in the trade name -- so the customer is named by the trade name
(uc03_p2_customer.proprietor_trade_name). Bookings named before it carry the
owner's name.

For every booking with a GST certificate on file whose GSTIN is an individual's,
this job names the customer again (uc03_p2_customer.sync_customer_name) and asks
for the booking to be settled again (STAGE_RECOMPUTE), so the customer-name check
and its tasks follow the new name. The worker also names every booking again on
start; this job reports what moved and settles the tasks.

Worker job ``GST_TRADE_NAME``: tag ``<tenant_id>:<label>``. A DRY RUN (counts
only, every change rolled back) unless the label ends with "-commit".
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_customer import is_individual_gstin, sync_customer_name

logger = structlog.get_logger(__name__)

JOB = "GST_TRADE_NAME"


class _DryRun(Exception):
    """Rolls back a dry run's naming."""


def _parse(tag: str) -> tuple[str, str, bool]:
    tenant_id, sep, label = (tag or "").partition(":")
    if not sep or not tenant_id.strip() or not label.strip():
        raise ValueError(f"{JOB}: tag must be '<tenant_id>:<label>'")
    return tenant_id.strip(), label.strip(), label.strip().endswith("-commit")


_CANDIDATES_SQL = """
    SELECT DISTINCT e.journey_id, f.effective_value #>> '{}' AS gstin
    FROM auditcore.evidence e
    JOIN auditcore.journey_document_extracted_fields f
      ON f.tenant_id = e.tenant_id AND f.journey_id = e.journey_id AND f.di_document_id = e.di_document_id
    WHERE e.tenant_id = :t AND e.document_type_key = 'gst_certificate' AND e.association_status = 'ACTIVE'
      AND f.field_key = 'gstin'
"""

_NAME_SQL = """
    SELECT c.legal_name FROM auditcore.journeys j
    JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
    WHERE j.tenant_id = :t AND j.journey_id = :j
"""

_TRADE_SQL = """
    SELECT 1 FROM auditcore.evidence e
    JOIN auditcore.journey_document_extracted_fields f
      ON f.tenant_id = e.tenant_id AND f.journey_id = e.journey_id AND f.di_document_id = e.di_document_id
    WHERE e.tenant_id = :t AND e.journey_id = :j AND e.document_type_key = 'gst_certificate'
      AND e.association_status = 'ACTIVE' AND f.field_key IN ('organisation_trade_name', 'trade_name')
      AND upper(regexp_replace(f.effective_value #>> '{}', '\\s+', ' ', 'g')) = upper(:name)
    LIMIT 1
"""


def run_gst_trade_name(engine: Engine, tag: str, reports: list[str]) -> dict[str, Any]:
    from audit_core.uc03_p2_runtime import enqueue_work

    tenant_id, label, commit = _parse(tag)
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        rows = connection.execute(text(_CANDIDATES_SQL), {"t": tenant_id}).mappings().all()
    journeys = sorted({str(r["journey_id"]) for r in rows if is_individual_gstin(r["gstin"])})
    renamed = by_trade_name = settled = failed = 0
    for journey in journeys:
        journey_id = UUID(journey)
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                before = connection.execute(text(_NAME_SQL), {"t": tenant_id, "j": journey_id}).scalar_one_or_none()
                try:
                    with connection.begin_nested():
                        sync_customer_name(connection, tenant_id=tenant_id, journey_id=journey_id)
                        after = connection.execute(text(_NAME_SQL), {"t": tenant_id, "j": journey_id}).scalar_one_or_none()
                        trade = after is not None and connection.execute(
                            text(_TRADE_SQL), {"t": tenant_id, "j": journey_id,
                                               "name": " ".join(str(after).split())}).first() is not None
                        if not commit:
                            raise _DryRun
                except _DryRun:
                    pass
                renamed += int(after != before)
                by_trade_name += int(trade)
                if commit and trade:
                    # Settled again: the customer-name check and its tasks follow the name.
                    enqueue_work(connection, tenant_id=tenant_id, journey_id=journey_id, work_type="STAGE_RECOMPUTE",
                                 work_key=f"booking:{journey_id}", payload={}, correlation_id=f"gst_trade_name:{label}")
                    settled += 1
        except Exception:
            failed += 1
            logger.warning("gst_trade_name_failed", tag=tag, journey_id=journey, exc_info=True)
    result = {"commit": commit, "proprietorBookings": len(journeys), "renamed": renamed,
              "namedByTradeName": by_trade_name, "settleRequested": settled, "failed": failed}
    logger.info("gst_trade_name_finished", tag=tag, **result)
    return result


__all__ = ["JOB", "run_gst_trade_name"]
