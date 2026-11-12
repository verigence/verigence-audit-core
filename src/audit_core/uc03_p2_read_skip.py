"""Which document types a new upload is classified for but not read (product owner, 12 Nov 2026).

"Can't we keep it simple ... the PC can keep uploading the files in it; the system can check
what all has been uploaded already and continue from there. Classification still happens but
extraction won't for already uploaded. Other for the documents like payment receipts or any
other document which can be more than one." Then: "dealership receipt and payment receipt are
one only", and "UPI receipts can be ignored to be extracted and this should be standard rule
across".

DI classifies every page and reads it unless Audit Core names its type in
``skipReadingDocumentTypeKeys`` (DI migration 0066). A page not read is kept as supporting
evidence; the earlier reading stays the journey's.

Three reasons a type is named:

- **Never read** (every upload, every Project): the UPI screenshot. Nothing uses its values.
- **Read once merged**: a page of a multi-page upload whose type is always merged into one
  document is only classified; the merged document is read once (``read_once_di_types``).
  Until now only its requirement ref was dropped, which since DI's 2026-10-01 change no
  longer stopped the read -- every such page was paid for twice (seen 12 Nov 2026: about
  1,750 pages in 7 days).
- **Already on the journey** (bulk and Drive uploads only, where the PC re-shoots the whole
  set each time): a type that is one per journey and that the journey already holds read,
  with no open verification task on it. A type that can come more than once is always read:
  receipts, accessory invoices, the customer ledger, the cost sheet, the no-dues certificate,
  credit and debit notes, bank statements. A PC's own upload in the app is read as before:
  there a second copy usually means the first was wrong (the 2026-10-01 rule).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_p2_registry import Registry

NEVER_READ_DI_TYPES: frozenset[str] = frozenset({"upi_screenshot"})

# Documents a journey can hold more than one of: always read.
ALWAYS_READ_DI_TYPES: frozenset[str] = frozenset({
    "dealer_receipt",
    "accessory_invoice_dms",
    "accessory_invoice_tally",
    "customer_ledger",
    "cost_sheet",
    "no_dues_certificate",
    "credit_note",
    "debit_note",
    "bank_statement_extract",
})

_CLOSED_TASKS = ("VERIFIED_COMPLETE", "CANCELLED", "FAILED", "DEAD_LETTER")


def one_per_journey_di_types(registry: Registry) -> frozenset[str]:
    """DI types of checklist documents a journey holds one of."""
    return frozenset(
        di_type
        for template in registry.documents.values()
        if not template.is_supporting
        for di_type in template.di_types
        if di_type not in ALWAYS_READ_DI_TYPES and di_type not in NEVER_READ_DI_TYPES
    )


def from_bulk_upload(connection: Connection, *, tenant_id: str, batch_id: UUID | str) -> bool:
    """Whether the page's upload came in through a bulk or Drive upload."""
    return connection.execute(
        text("SELECT 1 FROM auditcore.p2_bulk_files WHERE tenant_id=:t AND batch_id=:b LIMIT 1"),
        {"t": tenant_id, "b": str(batch_id)},
    ).first() is not None


def already_read_di_types(
    connection: Connection, *, tenant_id: str, journey_id: UUID | str, registry: Registry,
) -> frozenset[str]:
    """One-per-journey types the journey already holds read (values from an active document)
    and that no open verification task is about -- an earlier read with a task open on it
    (manual verification, key figures not read, booking date missing) is read again."""
    wanted = one_per_journey_di_types(registry)
    held: dict[str, set[str]] = {}
    for di_type, document_id in connection.execute(
        text(
            """
            SELECT DISTINCT f.source_document_type_key, f.di_document_id::text
            FROM auditcore.journey_document_extracted_fields f
            JOIN auditcore.evidence e
              ON e.tenant_id=f.tenant_id AND e.journey_id=f.journey_id AND e.di_document_id=f.di_document_id
             AND e.association_status='ACTIVE'
            WHERE f.tenant_id=:t AND f.journey_id=:j AND f.source_document_type_key IS NOT NULL
            """
        ),
        {"t": tenant_id, "j": str(journey_id)},
    ):
        if di_type in wanted:
            held.setdefault(str(di_type), set()).add(str(document_id))
    if not held:
        return frozenset()
    open_tasks = [
        (str(r[0] or ""), str(r[1] or ""))
        for r in connection.execute(
            text(
                f"""
                SELECT reference->>'templateKey', reference::text
                FROM auditcore.p2_tasks
                WHERE tenant_id=:t AND journey_id=:j AND category='DOCUMENT_VERIFICATION'
                  AND task_status NOT IN ({", ".join(f"'{s}'" for s in _CLOSED_TASKS)})
                """
            ),
            {"t": tenant_id, "j": str(journey_id)},
        )
    ]
    out = set()
    for di_type, documents in held.items():
        templates = {t.key for t in registry.templates_for_di_type(di_type)}
        if any(template in templates or any(d in reference for d in documents)
               for template, reference in open_tasks):
            continue
        out.add(di_type)
    return frozenset(out)


def read_skip_types(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID | str,
    batch_id: UUID | str,
    registry: Registry,
    classify_only: frozenset[str] = frozenset(),
) -> list[str]:
    """The types to send as ``skipReadingDocumentTypeKeys`` for one page or document."""
    skip = set(NEVER_READ_DI_TYPES) | set(classify_only)
    if from_bulk_upload(connection, tenant_id=tenant_id, batch_id=batch_id):
        skip |= already_read_di_types(connection, tenant_id=tenant_id, journey_id=journey_id, registry=registry)
    return sorted(skip)


def describe(types: list[str]) -> dict[str, Any]:
    """For the log line."""
    return {"read_skip_types": types, "read_skip_count": len(types)}
