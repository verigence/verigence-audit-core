"""Every document on file gives its key figures, or the booking says it did not
(product owner 2026-11-04).

"all these should have been identified": an NDC whose rows were not read made
the Deal fall back to the invoice without a word, and the same silence held
for every other document -- a receipt whose amount was not read simply did
not count as paid, a policy without its premium dropped out of the deal. Live
on 4 Nov (Mahindra Orissa): 58 dealer receipts without an amount, 99 without a
receipt number, 178 without a payment mode; 33 policies without a premium; 93
DMS invoices without a VIN; 45 booking dockets without a booking date.

One rule table, ``KEY_FIGURES``: per document type, the figures the audit uses
from it. A document on file (ACTIVE, its page settled) with any of them not
read is listed in one task per booking, DOCUMENT_KEY_FIGURES_NOT_READ, HIGH for
every figure alike (product owner: "All equal, HIGH"). It closes by itself
once every figure is read (a person's correction counts), or the PC closes it
with a comment when a figure is not printed on that document -- the same
issue is then not raised again.

The NDC / cost sheet rows have their own CRITICAL task
(uc03_account_statement.unread_statements).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, text

logger = structlog.get_logger(__name__)

TASK_TYPE = "DOCUMENT_KEY_FIGURES_NOT_READ"


@dataclass(frozen=True)
class Figure:
    label: str
    keys: tuple[str, ...]  # any one read is enough (the same figure under two names)
    amount: bool = False  # zero is not a reading of an amount


def _f(label: str, *keys: str, amount: bool = False) -> Figure:
    return Figure(label, keys, amount)


_RECEIPT = (_f("amount", "amount_paid", amount=True), _f("date", "receipt_date"),
            _f("receipt number", "receipt_number"), _f("payment mode", "payment_mode"))
_INVOICE = (_f("grand total", "grand_total_amount", amount=True), _f("invoice number", "invoice_number"),
            _f("invoice date", "invoice_date"))
_BOOKING = (_f("booking date", "booking_date"), _f("customer name", "customer_name"),
            _f("mobile", "customer_phone"), _f("model", "vehicle_model"))

KEY_FIGURES: dict[str, tuple[Figure, ...]] = {
    "dealer_receipt": _RECEIPT,
    "payment_receipt": (_f("amount", "amount_paid", amount=True), _f("date", "receipt_date"),
                        _f("payment mode", "payment_mode")),
    "insurance_cover": (_f("premium", "premium_amount", amount=True), _f("policy number", "policy_number"),
                        _f("insurer", "insurer_name"), _f("policy start date", "policy_start_date"),
                        _f("chassis number", "chassis_number")),
    "rto_challan": (_f("total", "grand_total_amount", amount=True), _f("date", "receipt_date"),
                    _f("chassis number", "chassis_number")),
    "customer_invoice_dms": (*_INVOICE, _f("taxable amount", "taxable_amount", amount=True),
                             _f("VIN", "vin_number", "chassis_number")),
    "tax_invoice_tally": _INVOICE,
    "accessory_invoice_dms": _INVOICE,
    "accessory_invoice_tally": _INVOICE,
    "ew_invoice": _INVOICE,
    "rsa_invoice": _INVOICE,
    "booking_docket": _BOOKING,
    "booking_form": _BOOKING,
    "pan_card": (_f("PAN", "pan_number"),),
    "aadhaar": (_f("Aadhaar number", "aadhaar_number"),),
}

_LABELS = {
    "dealer_receipt": "Dealer receipt", "payment_receipt": "Payment receipt", "insurance_cover": "Insurance policy",
    "rto_challan": "RTO challan", "customer_invoice_dms": "Customer invoice (DMS)",
    "tax_invoice_tally": "Tax invoice (Tally)", "accessory_invoice_dms": "Accessory invoice (DMS)",
    "accessory_invoice_tally": "Accessory invoice (Tally)", "ew_invoice": "Extended warranty invoice",
    "rsa_invoice": "RSA invoice", "booking_docket": "Booking docket", "booking_form": "Booking form",
    "pan_card": "PAN card", "aadhaar": "Aadhaar",
}
# A page still being read is not judged yet.
_SETTLED = ("READY", "NEEDS_REVIEW")


def _read(value: Any, *, amount: bool) -> bool:
    if value is None:
        return False
    text_ = str(value).strip().strip('"').strip()
    if text_ in ("", "null", "[]", "{}", "None", "-", "NA", "N/A"):
        return False
    if amount:
        try:
            return Decimal(text_.replace(",", "").replace("₹", "").replace("Rs", "").strip()) != 0
        except InvalidOperation:
            return True  # printed, in a shape the reader keeps; not a blank
    return True


def missing(document_type: str, fields: dict[str, Any]) -> list[Figure]:
    """The key figures of one document that were not read."""
    return [f for f in KEY_FIGURES.get(document_type, ())
            if not any(_read(fields.get(key), amount=f.amount) for key in f.keys)]


def documents_not_fully_read(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """Every document on file of the Journey with a key figure not read, page order."""
    return _examine(connection, tenant_id=tenant_id, journey_id=journey_id)[1]


def _examine(connection: Connection, *, tenant_id: str, journey_id: UUID) -> tuple[int, list[dict[str, Any]]]:
    """(documents examined, those with a key figure not read)."""
    rows = connection.execute(
        text(
            """
            SELECT e.di_document_id, e.document_type_key, f.field_key, f.effective_value,
                   (SELECT q.queue_status FROM auditcore.p2_document_queue q
                     WHERE q.tenant_id = e.tenant_id AND q.journey_id = e.journey_id
                       AND q.di_document_id = e.di_document_id
                     ORDER BY q.created_at_utc DESC LIMIT 1) AS queue_status,
                   (SELECT min(q.page_number) FROM auditcore.p2_document_queue q
                     WHERE q.tenant_id = e.tenant_id AND q.journey_id = e.journey_id
                       AND q.di_document_id = e.di_document_id) AS page_number
            FROM auditcore.evidence e
            LEFT JOIN auditcore.journey_document_extracted_fields f
              ON f.tenant_id = e.tenant_id AND f.journey_id = e.journey_id AND f.di_document_id = e.di_document_id
            WHERE e.tenant_id = :t AND e.journey_id = :j AND e.association_status = 'ACTIVE'
              AND e.document_type_key = ANY(:types) AND e.di_document_id IS NOT NULL
            """
        ),
        {"t": tenant_id, "j": journey_id, "types": list(KEY_FIGURES)},
    ).mappings().all()
    documents: dict[str, dict[str, Any]] = {}
    for row in rows:
        doc = documents.setdefault(str(row["di_document_id"]), {
            "documentId": str(row["di_document_id"]), "documentType": str(row["document_type_key"]),
            "queueStatus": row["queue_status"], "pageNumber": row["page_number"], "fields": {},
        })
        if row["field_key"] is not None:
            doc["fields"][str(row["field_key"])] = row["effective_value"]
    out = []
    examined = 0
    for doc in documents.values():
        if doc["queueStatus"] is not None and doc["queueStatus"] not in _SETTLED:
            continue
        examined += 1
        gaps = missing(doc["documentType"], doc["fields"])
        if gaps:
            out.append({
                "documentId": doc["documentId"], "documentType": doc["documentType"],
                "document": _LABELS.get(doc["documentType"], doc["documentType"]),
                "pageNumber": doc["pageNumber"], "missing": [g.label for g in gaps],
            })
    return examined, sorted(out, key=lambda d: (d["pageNumber"] is None, d["pageNumber"] or 0,
                                                d["documentType"], d["documentId"]))


def _task_key(journey_id: UUID) -> str:
    return f"document-key-figures:{journey_id}"


def describe(documents: list[dict[str, Any]]) -> str:
    parts = []
    for d in documents:
        page = f" (page {d['pageNumber']})" if d["pageNumber"] else ""
        parts.append(f"{d['document']}{page}: {', '.join(d['missing'])}")
    return "; ".join(parts)


def sync_document_key_figures(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """Raise, refresh or close the booking's one task. Never raises."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh, resolve_if_open

    try:
        with connection.begin_nested():
            examined, documents = _examine(connection, tenant_id=tenant_id, journey_id=journey_id)
            if documents:
                raise_or_refresh(
                    connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=_task_key(journey_id),
                    task_type=TASK_TYPE, source_type="RESOLVER", source_code=TASK_TYPE,
                    title="Figures not read from documents on file",
                    description=(
                        "These documents are on file but figures the audit uses were not read from them: "
                        + describe(documents) + ". Correct the reading of each (or upload a clearer page). "
                        "If a figure is not printed on that document, close this task with a comment saying so."
                    ),
                    reference={"sourceCode": TASK_TYPE, "documents": [
                        {k: d[k] for k in ("documentId", "documentType", "missing")} for d in documents]},
                    severity="HIGH", registry=get_registry(),
                )
            else:
                resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id,
                                dedupe_key=_task_key(journey_id), evidence={"documentsNotFullyRead": 0})
            return {"examined": examined, "documents": len(documents)}
    except Exception:
        logger.warning("document_key_figures_failed", tenant_id=tenant_id, journey_id=str(journey_id), exc_info=True)
        return {"error": True}
