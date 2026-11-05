"""COST_SHEET_STATEMENTS: a dealer's account statement filed as a cost sheet is
read again as the NDC (2026-11-05).

Before the classifier's type definitions (Document Intelligence 0058, 2 Nov),
Utkal's Vehicle Account Statement -- its NDC, with the payments, discounts and
the balance -- was often classified as a cost sheet: 4 of 5 Utkal "cost
sheets" sampled on 5 Nov were statements. As a cost sheet only four figures
and the total were read; the receipts and discounts on it never reached the
Deal: of 198 Utkal cost sheets printing a total, 152 were on bookings with no NDC. Since 3 Nov the
classifier files those pages as the NDC (customer_ledger).

cost_sheet schema 1.1 reads the title printed at the top (``document_title``).
This job lists the cost sheets whose title is a statement's ("Vehicle Account
Statement", "Final Vehicle Accounts Statement", "Account(s) Statement",
"Offer Letter / Payment Details") and reads each page again as the NDC, the
same path as a PC's "Set the document type" (uc03_page_retype). A quotation
("Quotation", "Quotation cum Proforma Invoice", "Cost Sheet") stays a cost sheet.

Tag ``<tenant_id>:<label>``: a DRY RUN listing the pages unless the label ends
with ``-commit``. A page still being read or whose stage was marked reviewed is
left alone by the retype, and listed with why.
"""
from __future__ import annotations

import re
from typing import Any

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

_STATEMENT_TITLE = re.compile(r".*\b(ACCOUNTS? STATEMENT|OFFER LETTER|PAYMENT DETAILS)\b.*")
_NDC_TEMPLATE = "customer_ledger"
_CHUNK = 60


def is_statement_title(title: Any) -> bool:
    return bool(_STATEMENT_TITLE.fullmatch(re.sub(r"\s+", " ", str(title or "").strip('"').upper())))


def statement_pages(engine: Engine, tenant_id: str) -> list[dict[str, Any]]:
    """Every live cost-sheet document of the Project whose printed title is an
    account statement's, with its booking and first page."""
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        rows = connection.execute(
            text(
                """
                SELECT j.journey_reference, e.di_document_id, f.effective_value #>> '{}' AS title,
                       (SELECT min(q.page_number) FROM auditcore.p2_document_queue q
                         WHERE q.tenant_id = e.tenant_id AND q.journey_id = e.journey_id
                           AND q.di_document_id = e.di_document_id) AS page_number
                FROM auditcore.evidence e
                JOIN auditcore.journeys j ON j.tenant_id = e.tenant_id AND j.journey_id = e.journey_id
                JOIN auditcore.journey_document_extracted_fields f
                  ON f.tenant_id = e.tenant_id AND f.journey_id = e.journey_id
                 AND f.di_document_id = e.di_document_id AND f.field_key = 'document_title'
                WHERE e.tenant_id = :t AND e.association_status = 'ACTIVE' AND e.document_type_key = 'cost_sheet'
                ORDER BY j.journey_reference
                """
            ),
            {"t": tenant_id},
        ).mappings().all()
    return [dict(r) for r in rows if r["page_number"] is not None and is_statement_title(r["title"])]


def run_cost_sheet_statements(engine: Engine, tag: str, reports: list[str]) -> dict[str, Any]:
    from audit_core.uc03_page_retype import run_page_retype

    tenant_id, _, label = (tag or "").partition(":")
    if not tenant_id or not label:
        raise ValueError("the tag must be '<tenant_id>:<label>'")
    pages = statement_pages(engine, tenant_id)
    items = sorted({f"{p['journey_reference']}:{p['page_number']}:{_NDC_TEMPLATE}:cost_sheet" for p in pages})
    out: dict[str, Any] = {"commit": label.endswith("-commit"), "found": len(items),
                           "titles": sorted({str(p["title"]) for p in pages}), "sent": 0, "wouldSend": 0,
                           "skipped": []}
    for start in range(0, len(items), _CHUNK):
        result = run_page_retype(engine, tag, items[start:start + _CHUNK])
        out["sent"] += len(result["sent"])
        out["wouldSend"] += len(result["wouldSend"])
        out["skipped"] += result["skipped"]
    logger.info("cost_sheet_statements_finished", tag=tag, found=out["found"], sent=out["sent"],
                would_send=out["wouldSend"], skipped=len(out["skipped"]))
    return out
