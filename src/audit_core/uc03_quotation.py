"""A quotation is not the cost sheet or the NDC (product owner 2026-11-25).

"Quotation is not cost sheet or NDC though it look same.. we have to be only
following NDC or Cost sheet.. yes fix if the same document is feeding other
deal lines."

The dealers' price quotation ("Quotation", "Quotation / Proforma Invoice",
"Quotation cum Proforma Invoice", "Proforma Invoice") looks like a cost sheet
and Document Intelligence files it as one. It is the price offered before the
sale, not what the dealer charged. As a cost sheet it counted as the NDC
wherever the NDC is read: it filled Deal lines, made insurance in-house ("the
NDC charges the insurance premium"), made RSA / EW sold, and was compared with
the real NDC. Live 25 Nov, UTK-JP-261031-001 (Banita Sasmal): the policy is
through a broker, the booking docket says Self and the NDC's insurance row is 0,
yet the quotation's Rs 40,210 made the insurance Dealership; 30 bookings were
in-house on a quotation's premium alone.

So, before a booking is rebuilt from its documents, a cost sheet whose printed
title is a quotation's or a proforma invoice's is set aside: the page stays on
file as a supporting document under its title, its document leaves the booking
(SUPERSEDED, never deleted), and with it every figure read from it -- every
reader of the cost sheet counts one only while its document is on the booking.
A page titled "Cost Sheet" (even "Cost Sheet cum Quotation") or an account
statement's title stays (uc03_cost_sheet_statements reads the latter as the NDC).

The page is typed by the rule, so the PC is not asked to "Set the document
type" of it. A page a person typed as a cost sheet by hand is left as it is. A
cost sheet read before its title was read is read again for its title by the
hourly cost-sheet sweep first, and judged then.

The same sweep (every hour) asks a recompute of every booking still holding a
quotation as its cost sheet, so the bookings on file before this rule, and a
quotation read later, are set aside without waiting for another upload.
"""
from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

COST_SHEET = "cost_sheet"
SUPPORTING = "supporting_document"
ACTOR = "SYSTEM:QUOTATION"
_NAME_MAX = 120  # p2_document_queue.display_name is varchar(120)

# "QUOTATION", "QUOTATION / PROFORMA INVOICE", "QUOTATION CUM PROFORMA INVOICE",
# "PROFORMA INVOICE", "PRO-FORMA INVOICE", "PERFORMA INVOICE" (as printed).
_QUOTATION = re.compile(r"\bQUOTATION\b|\bPRO\s*-?\s*FORMA\b|\bPERFORMA\b")
# The dealer's own deal sheets: a title naming one of them is not a quotation.
_DEAL_SHEET = re.compile(r"\bCOST\s*SHEET\b|\bACCOUNTS?\s+STATEMENT\b|\bOFFER\s+LETTER\b|\bPAYMENT\s+DETAILS\b")


def is_quotation_title(title: Any) -> bool:
    """True when a cost sheet's printed title is a quotation's or a proforma invoice's."""
    label = " ".join(str(title or "").strip().strip('"').upper().split())
    return bool(_QUOTATION.search(label)) and not _DEAL_SHEET.search(label)


def quotation_name(title: Any) -> str:
    name = " ".join(str(title or "").strip().strip('"').split()).title() or "Quotation"
    return name[:_NAME_MAX].rstrip(" ,/(")


def _set_by_a_person(actor: Any) -> bool:
    return bool(actor) and not str(actor).upper().startswith("SYSTEM")


_COST_SHEETS_SQL = """
    SELECT DISTINCT ON (e.di_document_id)
           e.di_document_id, f.effective_value #>> '{}' AS title,
           q.queue_id, q.type_overridden_by_actor_id
    FROM auditcore.evidence e
    JOIN auditcore.journey_document_extracted_fields f
      ON f.tenant_id = e.tenant_id AND f.journey_id = e.journey_id
     AND f.di_document_id = e.di_document_id AND f.field_key = 'document_title'
    LEFT JOIN auditcore.p2_document_queue q
      ON q.tenant_id = e.tenant_id AND q.journey_id = e.journey_id
     AND q.di_document_id = e.di_document_id AND q.retired_at_utc IS NULL
     AND q.queue_status NOT IN ('MERGED', 'CANCELLED')
    WHERE e.tenant_id = :t AND e.journey_id = :j AND e.association_status = 'ACTIVE'
      AND e.document_type_key = :cost_sheet
    ORDER BY e.di_document_id, f.updated_at_utc DESC
"""


def set_aside_quotations(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str | None = None,
) -> list[dict[str, Any]]:
    """Take every quotation filed as the booking's cost sheet off the booking,
    keeping its page on file as a supporting document. Returns what was set
    aside. Idempotent: a quotation set aside is no longer on the booking."""
    from audit_core.uc03_p2_runtime import record_activity

    rows = connection.execute(
        text(_COST_SHEETS_SQL), {"t": tenant_id, "j": journey_id, "cost_sheet": COST_SHEET},
    ).mappings().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        if not is_quotation_title(row["title"]) or _set_by_a_person(row["type_overridden_by_actor_id"]):
            continue
        name = quotation_name(row["title"])
        why = (f"Kept as a supporting document ({name}): a quotation is the price offered before the sale, "
               "not the cost sheet or the NDC; no figure is taken from it.")
        if row["queue_id"] is not None:
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_document_queue
                    SET queue_status = 'SUPPORTING', template_key = :k, display_name = :n, status_reason = :why,
                        type_overridden_by_actor_id = :actor, updated_at_utc = now()
                    WHERE tenant_id = :t AND queue_id = :q
                    """
                ),
                {"t": tenant_id, "q": row["queue_id"], "k": SUPPORTING, "n": name, "why": why, "actor": ACTOR},
            )
        connection.execute(
            text(
                """
                UPDATE auditcore.evidence
                SET association_status = 'SUPERSEDED', void_reason = :why, voided_by_actor_id = :actor,
                    voided_at_utc = now()
                WHERE tenant_id = :t AND journey_id = :j AND di_document_id = :d AND association_status = 'ACTIVE'
                """
            ),
            {"t": tenant_id, "j": journey_id, "d": row["di_document_id"], "actor": ACTOR, "why": why},
        )
        record_activity(
            connection, tenant_id=tenant_id, journey_id=journey_id, event_type="PAGE_KEPT_AS_OTHER",
            subject_type="DOCUMENT_PAGE", subject_id=str(row["queue_id"] or row["di_document_id"]),
            details={"templateKey": SUPPORTING, "name": name, "by": "system:quotation",
                     "title": row["title"], "was": COST_SHEET},
            correlation_id=correlation_id,
        )
        out.append({"diDocumentId": str(row["di_document_id"]), "title": row["title"], "name": name})
    return out


def bookings_holding_a_quotation(connection: Connection, *, tenant_id: str) -> list[UUID]:
    """Every booking of the Project with a quotation still on it as its cost
    sheet (and not typed so by a person)."""
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT e.journey_id, f.effective_value #>> '{}' AS title
            FROM auditcore.evidence e
            JOIN auditcore.journey_document_extracted_fields f
              ON f.tenant_id = e.tenant_id AND f.journey_id = e.journey_id
             AND f.di_document_id = e.di_document_id AND f.field_key = 'document_title'
            WHERE e.tenant_id = :t AND e.association_status = 'ACTIVE' AND e.document_type_key = :cost_sheet
              AND NOT EXISTS (
                  SELECT 1 FROM auditcore.p2_document_queue q
                  WHERE q.tenant_id = e.tenant_id AND q.journey_id = e.journey_id
                    AND q.di_document_id = e.di_document_id AND q.retired_at_utc IS NULL
                    AND q.queue_status NOT IN ('MERGED', 'CANCELLED')
                    AND q.type_overridden_by_actor_id IS NOT NULL
                    AND q.type_overridden_by_actor_id NOT LIKE 'SYSTEM%')
            """
        ),
        {"t": tenant_id, "cost_sheet": COST_SHEET},
    ).mappings().all()
    return sorted({row["journey_id"] for row in rows if is_quotation_title(row["title"])}, key=str)


def request_quotation_recomputes(engine: Engine, tenant_id: str, *, commit: bool = True) -> dict[str, Any]:
    """Ask a recompute of every booking still holding a quotation as its cost
    sheet; the recompute sets it aside (worker.settle_journey). A dry run
    (``commit`` False) only counts them."""
    from audit_core.uc03_p2_runtime import note_facts_changed

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        journeys = bookings_holding_a_quotation(connection, tenant_id=tenant_id)
        if commit:
            for journey_id in journeys:
                note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id,
                                   reason="QUOTATION_NOT_COST_SHEET")
    if journeys:
        logger.info("quotation_recomputes_requested", tenant_id=tenant_id, bookings=len(journeys), commit=commit)
    return {"commit": commit, "bookings": len(journeys), "journeyIds": [str(j) for j in journeys]}
