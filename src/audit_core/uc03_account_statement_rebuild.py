"""Rebuild every Journey's Deal from its account statement (2026-10-25).

For a Project whose Deal source policy is ACCOUNT_STATEMENT (Mahindra Orissa),
the dealer's account statement is the source of truth (uc03_account_statement).
Migration 0162 cleared what the invoices had put in the Deal; this sets it from
the statements, Journey by Journey, and raises the CRITICAL tasks for a
statement whose vehicle line was not read and for a Tally ledger that disagrees.

Started from outside with AUDIT_CORE_ACCOUNT_STATEMENT_TAG on the worker. A DRY
RUN (one ``account_statement_item`` log line per Journey, nothing written) unless
the tag ends with "-commit". Idempotent: running it again changes nothing that
is already right.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from audit_core.db import set_tenant_context
from audit_core.uc03_account_statement import (
    KIND_FORM,
    KIND_TALLY,
    line_rows,
    load_ledger_documents,
    read_statement,
    statement_kind,
    sync_account_statement,
    tally_deviations,
    tally_postings,
)
from audit_core.uc03_deal_source_policy import account_statement_is_source_of_truth

logger = structlog.get_logger(__name__)


def _journeys(connection: Connection, tenant_id: str) -> list[UUID]:
    rows = connection.execute(
        text("SELECT journey_id FROM auditcore.journeys WHERE tenant_id = :t ORDER BY journey_id"),
        {"t": tenant_id},
    ).scalars().all()
    return [UUID(str(r)) for r in rows]


def _invoice_held(connection: Connection, tenant_id: str, journey_id: UUID) -> int:
    """Deal figures still held by an invoice (zero once the rebuild has run)."""
    from audit_core.uc03_deal_source_policy import INVOICE_SOURCE_TYPES

    return int(connection.execute(
        text(
            """
            SELECT
              (SELECT count(*) FROM auditcore.commercial_lines
               WHERE tenant_id = :t AND journey_id = :j AND actual_amount IS NOT NULL
                 AND split_part(coalesce(source_reference, ''), ':', 1) = ANY(:types))
            + (SELECT count(*) FROM auditcore.commercial_line_source_values
               WHERE tenant_id = :t AND journey_id = :j AND source_document_type = ANY(:types))
            """
        ),
        {"t": tenant_id, "j": journey_id, "types": sorted(INVOICE_SOURCE_TYPES)},
    ).scalar() or 0)


def _on_phase_two(connection: Connection, tenant_id: str, journey_id: UUID) -> bool:
    return connection.execute(
        text("SELECT 1 FROM auditcore.p2_journey_runtime WHERE tenant_id = :t AND journey_id = :j"),
        {"t": tenant_id, "j": journey_id},
    ).first() is not None


def rebuild_journey(
    connection: Connection, *, tenant_id: str, journey_id: UUID, tag: str, commit: bool,
) -> dict[str, Any]:
    """One Journey: what its statement gives and, with ``commit``, the Deal set
    from it with downstream told."""
    documents = load_ledger_documents(connection, tenant_id=tenant_id, journey_id=journey_id)
    parsed = [(d, rows, statement_kind(rows)) for d in documents for rows in [line_rows(d["fields"].get("line_items"))]]
    forms = [rows for _, rows, kind in parsed if kind == KIND_FORM]
    tallies = [rows for _, rows, kind in parsed if kind == KIND_TALLY]
    statement = read_statement(forms[0]) if forms else None
    deviations = (tally_deviations(statement, tally_postings(tallies[0])) if statement and tallies else [])
    item: dict[str, Any] = {
        "statements": len(forms), "tallies": len(tallies),
        # None when the statement has no vehicle line (str(None) is "None", which
        # made the job's no_ex_showroom total read 0 for every such statement).
        "exShowroom": (str(ex_showroom) if (ex_showroom := statement.components.get("ex_showroom_price")) is not None
                       else None) if statement else None,
        "components": sorted(statement.components) if statement else [],
        "discounts": sorted(statement.discounts) if statement else [],
        "unmapped": statement.unmapped if statement else [],
        "conflicts": statement.conflicts if statement else [],
        "tallyDeviations": [d["line"] for d in deviations],
        "invoiceHeldBefore": _invoice_held(connection, tenant_id, journey_id),
    }
    if not commit:
        return item
    sync_account_statement(connection, tenant_id=tenant_id, journey_id=journey_id,
                           correlation_id=f"account-statement:{tag}")
    if _on_phase_two(connection, tenant_id, journey_id):
        from audit_core.uc03_p2_runtime import note_facts_changed

        note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id,
                           reason="account_statement_rebuild", correlation_id=f"account-statement:{tag}")
    item["invoiceHeldAfter"] = _invoice_held(connection, tenant_id, journey_id)
    return item


def run_account_statement_rebuild(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    commit = tag.endswith("-commit")
    for tenant_id in tenant_ids:
        totals = {"journeys": 0, "with_statement": 0, "no_statement": 0, "no_ex_showroom": 0,
                  "tally_deviations": 0, "invoice_held_after": 0, "failed": 0}
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                if not account_statement_is_source_of_truth(connection, tenant_id=tenant_id):
                    continue
                journeys = _journeys(connection, tenant_id)
        except Exception:
            logger.warning("account_statement_rebuild_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        for journey_id in journeys:
            totals["journeys"] += 1
            try:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    item = rebuild_journey(connection, tenant_id=tenant_id, journey_id=journey_id,
                                           tag=tag, commit=commit)
            except Exception:
                totals["failed"] += 1
                logger.warning("account_statement_item_failed", tag=tag, tenant_id=tenant_id,
                               journey_id=str(journey_id), exc_info=True)
                continue
            totals["with_statement"] += bool(item["statements"])
            totals["no_statement"] += not item["statements"]
            totals["no_ex_showroom"] += bool(item["statements"]) and item["exShowroom"] is None
            totals["tally_deviations"] += bool(item["tallyDeviations"])
            totals["invoice_held_after"] += bool(item.get("invoiceHeldAfter"))
            logger.info("account_statement_item", tag=tag, tenant_id=tenant_id, commit=commit,
                        journey_id=str(journey_id), **{k: str(v) for k, v in item.items()})
        logger.info("account_statement_rebuild_finished", tag=tag, tenant_id=tenant_id, commit=commit, **totals)
