"""uc03_deal_source_history.py — per-source breakdown of commercial/discount
actual values, alongside (not instead of) the single canonical value.

``commercial_lines.actual_amount`` and ``discount_applications.
actual_discount_amount`` each hold exactly one current-best-value row per
(journey, component): a higher-priority document (e.g. a retail invoice)
overwrites a lower-priority one (e.g. the booking form) in place, and every
rule / SLA / compliance check that reads those tables keeps reading that
one value. That is unchanged by this module.

This module records the *same* upserts a second time, unconditionally --
regardless of whether the incoming source wins or loses the canonical
value -- into ``commercial_line_source_values``, keyed by (journey,
component, source document type). Nothing else reads that table except the
Journey Line "masters vs offered" panel, purely to show a booking-form row
and an invoice row side by side when they disagree, instead of silently
collapsing to whichever currently wins.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

LineKind = str  # "COMMERCIAL" | "DISCOUNT"


def record_source_value(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    line_kind: LineKind,
    component_key: str,
    source_document_type: str,
    amount: Decimal | None,
    evidence_id: UUID | None = None,
    document_id: UUID | None = None,
) -> None:
    """Record what one source document reported for one component, idempotently.

    A no-op when ``amount`` is None -- an absent value from one source is not
    itself a fact worth a row (it would otherwise read as "this source said
    zero"). Safe to call every time a producer runs, whether or not this
    source currently wins the canonical commercial_lines / discount_applications
    row: that is the whole point -- a losing source's own reported value is
    exactly what the comparison panel needs to show.
    """
    if amount is None:
        return
    connection.execute(
        text(
            """
            INSERT INTO auditcore.commercial_line_source_values (
                tenant_id, journey_id, line_kind, component_key,
                source_document_type, amount, source_evidence_id, source_document_id
            ) VALUES (
                :tenant_id, :journey_id, :line_kind, :component_key,
                :source_document_type, :amount, :evidence_id, :document_id
            )
            ON CONFLICT (tenant_id, journey_id, line_kind, component_key, source_document_type)
            DO UPDATE SET
                amount=EXCLUDED.amount,
                source_evidence_id=EXCLUDED.source_evidence_id,
                source_document_id=EXCLUDED.source_document_id,
                updated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "line_kind": line_kind,
            "component_key": component_key.strip().lower(),
            "source_document_type": source_document_type.strip().lower(),
            "amount": amount,
            "evidence_id": evidence_id,
            "document_id": document_id,
        },
    )


def load_source_breakdown(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[dict[str, Any]]:
    """Every recorded per-source value for this journey, one row per
    (component, source document type). Grouping by ``componentKey`` (and, for
    discounts, matching against ``discount_key``) is the caller's job."""
    rows = connection.execute(
        text(
            """
            SELECT
                line_kind             AS "lineKind",
                component_key         AS "componentKey",
                source_document_type  AS "sourceDocumentType",
                amount                AS "amount",
                source_evidence_id    AS "sourceEvidenceId",
                source_document_id    AS "sourceDocumentId",
                updated_at_utc        AS "updatedAtUtc"
            FROM auditcore.commercial_line_source_values
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY line_kind, component_key, updated_at_utc
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [dict(row) for row in rows]


__all__ = ["load_source_breakdown", "record_source_value"]
