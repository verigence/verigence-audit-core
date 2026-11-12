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
component, source document type, source document).

2026-10-20 product owner rules (R2: each invoice serves only its own lines,
never clubbed, never silently replaced): the table used to keep one row per
document *type*, so a second invoice of the same type overwrote the first
and the Deal sheet mixed figures from two invoices. It is now keyed per
source document (migration 0153); a source with no document id (the booking
form's discount rows) keeps one row per type as before. Readers that price a
deal go through :func:`active_source_rows`, which drops the rows of a
document whose evidence was voided or superseded and orders the rest
deterministically (never by when a row was last re-written).

:func:`upsert_commercial_actual` is the one ranked writer of the canonical
``commercial_lines`` actual: every producer (invoice, RTO challan, booking
form, post-extraction) goes through it, so a booking-stage re-run can never
overwrite an invoice's actual (2026-10-20, R6: the invoice is the source of
truth).
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_deal_source_policy import invoice_withheld

LineKind = str  # "COMMERCIAL" | "DISCOUNT"

# The nil UUID stands in for "no document" in the unique key, so a source
# without a document id still has exactly one row per (component, type).
_NO_DOCUMENT = "00000000-0000-0000-0000-000000000000"


def normalise_document_type(value: Any) -> str:
    """"Tax Invoice Tally", "tax-invoice-tally" and "tax_invoice_tally" are one
    type (2026-10-20: two spellings of one type were added together)."""
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


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

    One row per source document (2026-10-20): two invoices of the same type
    are two rows, never one overwriting the other.
    """
    if amount is None:
        return
    # Mahindra Orissa: the account statement is the source of truth, so an
    # invoice records nothing in the Deal (the document stays on file).
    if invoice_withheld(connection, tenant_id=tenant_id, document_type=source_document_type):
        return
    connection.execute(
        text(
            f"""
            INSERT INTO auditcore.commercial_line_source_values (
                tenant_id, journey_id, line_kind, component_key,
                source_document_type, amount, source_evidence_id, source_document_id
            ) VALUES (
                :tenant_id, :journey_id, :line_kind, :component_key,
                :source_document_type, :amount, :evidence_id, :document_id
            )
            ON CONFLICT (tenant_id, journey_id, line_kind, component_key, source_document_type,
                         (COALESCE(source_document_id, '{_NO_DOCUMENT}'::uuid)))
            DO UPDATE SET
                amount=EXCLUDED.amount,
                source_evidence_id=EXCLUDED.source_evidence_id,
                withdrawn_at_utc=NULL,
                updated_at_utc=CASE
                    WHEN auditcore.commercial_line_source_values.amount IS DISTINCT FROM EXCLUDED.amount
                    THEN now() ELSE auditcore.commercial_line_source_values.updated_at_utc END
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "line_kind": line_kind,
            "component_key": component_key.strip().lower(),
            "source_document_type": normalise_document_type(source_document_type),
            "amount": amount,
            "evidence_id": evidence_id,
            "document_id": document_id,
        },
    )


# A source row counts while its document is on file: no evidence row at all
# (a value recorded without a document link) or an ACTIVE one. A document
# that was voided, superseded or merged into another stops counting
# (2026-10-20, R2: "voided/removed documents stop counting").
_DOCUMENT_ON_FILE = """
    NOT EXISTS (
        SELECT 1 FROM auditcore.evidence e
        WHERE e.tenant_id = s.tenant_id AND e.journey_id = s.journey_id
          AND (e.di_document_id = s.source_document_id OR e.evidence_id = s.source_evidence_id)
          AND e.association_status <> 'ACTIVE'
    )
"""

# A row its document no longer gives (withdraw_unreported) stops counting as well
# (2026-11-12): a figure read once and gone on a later reading stood for good.
ACTIVE_SOURCE_CONDITION = """
    s.withdrawn_at_utc IS NULL AND NOT EXISTS (
        SELECT 1 FROM auditcore.evidence e
        WHERE e.tenant_id = s.tenant_id AND e.journey_id = s.journey_id
          AND (e.di_document_id = s.source_document_id OR e.evidence_id = s.source_evidence_id)
          AND e.association_status <> 'ACTIVE'
    )
"""


def withdraw_unreported(
    connection: Connection, *, tenant_id: str, journey_id: UUID, source_document_type: str,
    reported: set[tuple[str, str, str | None]],
    keep_documents: set[str] | None = None, keep_all: bool = False,
) -> int:
    """Take back what a source said earlier and no longer says.

    ``reported`` is everything the source type gives for the Journey on this reading, as
    (line kind, component key, document id or None). Any other row of that type still
    standing is marked withdrawn: its document was read again and no longer gives the
    figure, or another copy now gives it. Until 2026-11-12 such a row stood for as long as
    its document stayed on file -- SHI-SS-261027-024's NDC has no insurance row, but its
    earlier "insurance Rs 82,000" kept the insurance in-house and the premium in the Deal.

    A document that could not be read at all on this pass says nothing about what it
    prints: the rows of the documents in ``keep_documents`` (and, with ``keep_all``, every
    row of the type -- for a source recorded under no document id) are never withdrawn,
    and a row of theirs withdrawn earlier is brought back when nothing of that document
    (or type) stands. Found the day this shipped (2026-11-12): four NDCs whose rows did
    not parse had every figure taken back, their vehicle price included.

    Call it once per source type, after every figure of this reading is recorded
    (record_source_value brings a row back when the document gives the figure again).
    Returns the number of rows withdrawn."""
    kept = {str(d) for d in (keep_documents or set())}
    # Bring back what was taken from an unread document, when none of its rows stands.
    for document in sorted(kept) if not keep_all else [None]:
        which = "TRUE" if keep_all else "s.source_document_id = CAST(:document AS uuid)"
        stands = connection.execute(
            text(
                f"""
                SELECT count(*) FILTER (WHERE s.withdrawn_at_utc IS NULL) AS standing,
                       count(*) FILTER (WHERE s.withdrawn_at_utc IS NOT NULL) AS taken
                FROM auditcore.commercial_line_source_values s
                WHERE s.tenant_id = :t AND s.journey_id = :j AND {NORMALISED_TYPE_SQL} = :type AND {which}
                  AND {_DOCUMENT_ON_FILE}
                """
            ),
            {"t": tenant_id, "j": journey_id, "type": normalise_document_type(source_document_type),
             "document": document},
        ).mappings().one()
        if stands["taken"] and not stands["standing"]:
            connection.execute(
                text(
                    f"""
                    UPDATE auditcore.commercial_line_source_values s
                    SET withdrawn_at_utc = NULL
                    WHERE s.tenant_id = :t AND s.journey_id = :j AND s.withdrawn_at_utc IS NOT NULL
                      AND {NORMALISED_TYPE_SQL} = :type AND {which} AND {_DOCUMENT_ON_FILE}
                    """
                ),
                {"t": tenant_id, "j": journey_id, "type": normalise_document_type(source_document_type),
                 "document": document},
            )
    if keep_all:
        return 0
    wanted = {(kind, key.strip().lower(), str(document) if document else None) for kind, key, document in reported}
    standing = connection.execute(
        text(
            f"""
            SELECT s.line_kind, s.component_key, s.source_document_type AS stored_type,
                   CAST(s.source_document_id AS text) AS document_id
            FROM auditcore.commercial_line_source_values s
            WHERE s.tenant_id = :t AND s.journey_id = :j AND s.withdrawn_at_utc IS NULL
              AND {NORMALISED_TYPE_SQL} = :type
            """
        ),
        {"t": tenant_id, "j": journey_id, "type": normalise_document_type(source_document_type)},
    ).mappings().all()
    withdrawn = 0
    for row in standing:
        if (row["line_kind"], row["component_key"], row["document_id"]) in wanted or row["document_id"] in kept:
            continue
        withdrawn += connection.execute(
            text(
                """
                UPDATE auditcore.commercial_line_source_values s
                SET withdrawn_at_utc = now()
                WHERE s.tenant_id = :t AND s.journey_id = :j AND s.withdrawn_at_utc IS NULL
                  AND s.line_kind = :kind AND s.component_key = :key AND s.source_document_type = :stored_type
                  AND s.source_document_id IS NOT DISTINCT FROM CAST(:document AS uuid)
                """
            ),
            {"t": tenant_id, "j": journey_id, "kind": row["line_kind"], "key": row["component_key"],
             "stored_type": row["stored_type"], "document": row["document_id"]},
        ).rowcount
    return withdrawn


# The SQL twin of normalise_document_type.
NORMALISED_TYPE_SQL = "trim(both '_' from regexp_replace(lower(s.source_document_type), '[^a-z0-9]+', '_', 'g'))"


def active_source_rows(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """Every per-source value on documents still on file, one per (kind,
    component, normalised type, document), in a fixed order -- never by when
    a row was last re-written, which only says which document was
    re-processed last. Two spellings of one type on one document (rows
    written before migration 0153) are one source: the newest row stands,
    so a stale spelling never wins (review S6)."""
    rows = connection.execute(
        text(
            f"""
            SELECT line_kind, component_key, source_document_type, amount, source_document_id, source_evidence_id
            FROM (
                SELECT DISTINCT ON (s.line_kind, s.component_key, {NORMALISED_TYPE_SQL}, s.source_document_id)
                       s.line_kind, s.component_key, {NORMALISED_TYPE_SQL} AS source_document_type, s.amount,
                       s.source_document_id, s.source_evidence_id
                FROM auditcore.commercial_line_source_values s
                WHERE s.tenant_id = :tenant_id AND s.journey_id = :journey_id
                  AND {ACTIVE_SOURCE_CONDITION}
                ORDER BY s.line_kind, s.component_key, {NORMALISED_TYPE_SQL}, s.source_document_id,
                         s.updated_at_utc DESC,
                         (s.source_document_type = {NORMALISED_TYPE_SQL}) DESC
            ) newest
            ORDER BY line_kind, component_key, source_document_type, source_document_id NULLS FIRST
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def load_source_breakdown(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[dict[str, Any]]:
    """Every recorded per-source value for this journey on a document still on
    file, one row per (component, source document). Grouping by
    ``componentKey`` (and, for discounts, matching against ``discount_key``)
    is the caller's job."""
    rows = connection.execute(
        text(
            f"""
            SELECT * FROM (
                SELECT DISTINCT ON (s.line_kind, s.component_key, {NORMALISED_TYPE_SQL}, s.source_document_id)
                    s.line_kind             AS "lineKind",
                    s.component_key         AS "componentKey",
                    {NORMALISED_TYPE_SQL}   AS "sourceDocumentType",
                    s.amount                AS "amount",
                    s.source_evidence_id    AS "sourceEvidenceId",
                    s.source_document_id    AS "sourceDocumentId",
                    s.updated_at_utc        AS "updatedAtUtc"
                FROM auditcore.commercial_line_source_values s
                WHERE s.tenant_id = :tenant_id AND s.journey_id = :journey_id
                  AND {ACTIVE_SOURCE_CONDITION}
                ORDER BY s.line_kind, s.component_key, {NORMALISED_TYPE_SQL}, s.source_document_id,
                         s.updated_at_utc DESC,
                         (s.source_document_type = {NORMALISED_TYPE_SQL}) DESC
            ) newest
            ORDER BY "lineKind", "componentKey", "sourceDocumentType", "sourceDocumentId" NULLS FIRST
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _as_uuid(value: Any) -> UUID | None:
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _reference_type(source_reference: Any) -> str | None:
    if source_reference is None or ":" not in str(source_reference):
        return None
    return normalise_document_type(str(source_reference).split(":", 1)[0])


def source_rank(priority: tuple[str, ...], document_type: str | None) -> int:
    """Position of a document type in a component's source priority (lower is
    stronger); a type not listed ranks after every listed one."""
    normalized = normalise_document_type(document_type)
    for index, item in enumerate(priority):
        if normalise_document_type(item) == normalized:
            return index
    return len(priority) + 1


def upsert_commercial_actual(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    component_key: str,
    amount: Decimal,
    document_type: str,
    document_id: UUID | str | None,
    evidence_id: UUID | None,
    record: bool = True,
) -> bool:
    """Record the source value, then set the canonical commercial_lines actual
    only when this document ranks ahead of the one holding it.

    Rank comes from the component's source priority (uc03_attribute_mapping:
    invoices, then the deal sheet, then the booking form). The same document
    type may refresh its own value (a corrected re-review); a weaker source
    never replaces a stronger one -- so the booking form never overwrites an
    invoice's actual (2026-10-20, R6). Returns True when the canonical row
    changed."""
    from audit_core.uc03_attribute_mapping import spec_for_field

    document_type = normalise_document_type(document_type)
    # Mahindra Orissa: the account statement is the source of truth (see
    # uc03_account_statement); an invoice sets no canonical actual either.
    if invoice_withheld(connection, tenant_id=tenant_id, document_type=document_type):
        return False
    if record:
        record_source_value(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            line_kind="COMMERCIAL",
            component_key=component_key,
            source_document_type=document_type,
            amount=amount,
            evidence_id=evidence_id,
            document_id=_as_uuid(document_id),
        )
    spec = spec_for_field(component_key)
    priority = spec.source_priority if spec is not None else (document_type,)
    reference = f"{document_type}:{document_id}"
    existing = connection.execute(
        text(
            """
            SELECT source_reference, actual_amount
            FROM auditcore.commercial_lines
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND component_key=:component_key
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "component_key": component_key},
    ).mappings().one_or_none()
    if existing is not None:
        existing_type = _reference_type(existing["source_reference"])
        if existing_type is not None:
            existing_rank = source_rank(priority, existing_type)
            new_rank = source_rank(priority, document_type)
            if existing_rank < new_rank:
                return False  # a stronger source already holds it
            if existing_rank == new_rank and existing_type != document_type:
                return False  # an equally strong, different source: keep it
        if existing["actual_amount"] == amount and existing["source_reference"] == reference:
            return False
    connection.execute(
        text(
            """
            INSERT INTO auditcore.commercial_lines (
                tenant_id, journey_id, component_key, actual_amount,
                actual_source_kind, source_evidence_id, source_reference
            ) VALUES (
                :tenant_id, :journey_id, :component_key, :actual_amount,
                'EVIDENCE', :evidence_id, :source_reference
            )
            ON CONFLICT (tenant_id, journey_id, component_key) DO UPDATE SET
                actual_amount=EXCLUDED.actual_amount,
                actual_source_kind='EVIDENCE',
                source_evidence_id=EXCLUDED.source_evidence_id,
                source_reference=EXCLUDED.source_reference,
                updated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "component_key": component_key,
            "actual_amount": amount,
            "evidence_id": evidence_id,
            "source_reference": reference,
        },
    )
    return True


__all__ = [
    "ACTIVE_SOURCE_CONDITION",
    "NORMALISED_TYPE_SQL",
    "active_source_rows",
    "load_source_breakdown",
    "normalise_document_type",
    "record_source_value",
    "source_rank",
    "upsert_commercial_actual",
]
