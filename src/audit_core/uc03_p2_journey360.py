"""Phase 2 Journey 360 read model.

Every section is a small, local Audit Core read: no DI, Rule Engine or
Security call, no rule execution and no source re-resolution at request
time. The summary is cheap enough to serve on every open and carries an ETag
built from the Journey's fact version, newest activity, task and control
changes, so an unchanged Journey is answered with 304. Heavier sections load
only when their tab opens.

The Deal section lays every commercial component and discount out side by
side -- master standard (SKU price list / scheme entitlement), what the
booking offered, what was billed and what the ledger shows -- with the
variance between them, so a reviewer sees where money moved without opening
a single document.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_duplicate_booking_detection import _BASIS_LABEL
from audit_core.uc03_duplicate_receipt_detection import (
    ReceiptRecord,
    compute_duplicate_groups,
    normalize_receipt_date,
    normalize_receipt_number,
)
from audit_core.uc03_masters_alignment import (
    CONDITIONAL_DISCOUNT_EVIDENCE_DOCUMENT,
    DISCOUNT_ACTUAL_FIELD_TO_BENEFIT_KEY,
    canonical_discount_key,
)
from audit_core.uc03_p2_controls import control_statistics
from audit_core.uc03_p2_dates import parse_extracted_date
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_stage import read_booking_stage

# The tabs Journey 360 shows. "delivery", "activity" and "timeline" stay
# servable for older clients but are no longer advertised: the Vehicle tab
# carries the delivery, the Audit trail carries every event and duration.
SECTIONS = (
    "deal", "invoices", "addons", "documents", "payments", "vehicle", "tradein", "customer", "registration",
    "compliance", "audit",
)

_OPEN_TASK_EXCLUDED = "('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')"

# Which column a recorded source value belongs in.
_BOOKING_SOURCES = frozenset({"booking_form", "booking_docket", "order_taking_form"})
_LEDGER_SOURCES = frozenset({"customer_ledger"})
_QUOTE_SOURCES = frozenset({"cost_sheet"})
# Everything else reporting a value is a billed/actual source: vehicle and
# accessory invoices, EW/RSA invoices, credit notes, the insurance cover
# (premium) and the RTO challan (road tax / registration).

_CATEGORIES = (
    ("VEHICLE", "Vehicle price"),
    ("REGISTRATION", "Registration & taxes"),
    ("INSURANCE", "Insurance"),
    ("ACCESSORIES", "Accessories"),
    ("PROTECTION", "Warranty & protection plans"),
    ("OTHER", "Other charges"),
)

_COMPONENT_LABELS = {
    "ex_showroom_price": "Ex-showroom price",
    "tcs_amount": "TCS",
    "insurance_amount": "Insurance premium",
    "registration_charges": "Registration",
    "road_tax_amount": "Road tax",
    "green_tax_amount": "Green tax",
    "accessories_cost": "Accessories kit",
    "essential_kit_amount": "Essential kit",
    "genuine_accessories_amount": "Genuine accessories",
    "non_genuine_accessories_amount": "Non-genuine accessories",
    "additional_warranty_amount": "Extended warranty",
    "extended_warranty_amount": "Extended warranty",
    "rsa_amount": "Road-side assistance",
    "service_package_amount": "Service package",
    "fastag_amount": "FASTag",
    "handling_charges": "Handling charges",
}

_DISCOUNT_LABELS = {
    "CASH_DISCOUNT": "Consumer / cash discount",
    "EXCHANGE_BONUS": "Exchange bonus",
    "SCRAPPAGE_BONUS_DEALER": "Scrappage bonus (dealer)",
    "SCRAPPAGE_BONUS_COD": "Scrappage bonus (certificate of deposit)",
    "WELCOME_BONUS": "Loyalty / welcome bonus",
    "CORPORATE_PRIVILEGE": "Corporate discount",
    "ACCESSORIES_KIT": "Free accessories",
    "EXT_WARRANTY_4TH_YR": "Free extended warranty (4th year)",
    "EXT_WARRANTY_4TH_5TH_YR": "Free extended warranty (4th–5th year)",
    "INSURANCE": "Insurance discount",
    "OTHER_SCHEME": "Other scheme",
    "ADDITIONAL_DISCOUNT": "Additional dealer discount",
}


# Booking-form totals: declared by the form, never a component of the deal
# (summing them with the components would double count). Shown on their own.
_DECLARED_TOTAL_KEYS = {
    "total_price": "On-road total (declared)",
    "net_amount": "Net payable (declared)",
    "discount_amount": "Total discount (declared)",
    "bonus_amount": "Total bonus (declared)",
    "booking_amount_paid": "Booking amount paid (declared)",
    "balance_amount": "Balance (declared)",
}


def _benefit_key(key: str) -> str | None:
    """Canonical discount benefit for a discount-like key, else None."""
    lowered = key.lower()
    if lowered in _DECLARED_TOTAL_KEYS:
        return None
    if lowered in DISCOUNT_ACTUAL_FIELD_TO_BENEFIT_KEY:
        return DISCOUNT_ACTUAL_FIELD_TO_BENEFIT_KEY[lowered]
    if lowered.endswith(("_discount_amount", "_bonus_amount")):
        return "OTHER_SCHEME"
    return None


def _dec(value: Any) -> Decimal | None:
    """A number as a document prints it (\"₹ 61,308.02\") or as the database
    holds it; None when there is no number in it."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text_value = str(value).replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "").strip()
    if not text_value:
        return None
    try:
        return Decimal(text_value)
    except (ArithmeticError, ValueError):
        return None


def _money(value: Any) -> str | None:
    parsed = _dec(value)
    return None if parsed is None else str(parsed)


def _minus(a: Any, b: Any) -> str | None:
    left, right = _dec(a), _dec(b)
    if left is None or right is None:
        return None
    return str(left - right)


def _plain(row: Any) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, (UUID, Decimal)) else v) for k, v in dict(row).items()}


def component_category(key: str) -> str:
    k = key.lower()
    if "insurance" in k:
        return "INSURANCE"
    if any(t in k for t in ("registration", "road_tax", "rto", "green_tax", "hsrp")):
        return "REGISTRATION"
    if any(t in k for t in ("accessor", "essential_kit")):
        return "ACCESSORIES"
    if any(t in k for t in ("warranty", "rsa", "service_package", "amc", "shield")):
        return "PROTECTION"
    if any(t in k for t in ("ex_showroom", "tcs", "vehicle_price", "basic_price")):
        return "VEHICLE"
    return "OTHER"


def component_label(key: str) -> str:
    if key in _COMPONENT_LABELS:
        return _COMPONENT_LABELS[key]
    words = key.removesuffix("_amount").replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def discount_label(key: str) -> str:
    return _DISCOUNT_LABELS.get(key) or key.replace("_", " ").capitalize()


_ACRONYMS = {
    "pan": "PAN", "gst": "GST", "gstin": "GSTIN", "vin": "VIN", "rto": "RTO", "dms": "DMS", "ew": "EW",
    "rsa": "RSA", "tcs": "TCS", "hsn": "HSN", "ifsc": "IFSC", "upi": "UPI", "do": "DO", "po": "PO",
    "dob": "Date of birth", "id": "ID", "no": "No.", "kyc": "KYC", "misp": "MISP", "cgst": "CGST",
    "sgst": "SGST", "igst": "IGST", "utr": "UTR", "neft": "NEFT", "rtgs": "RTGS",
}


def field_label(key: str) -> str:
    words = [_ACRONYMS.get(w, w) for w in key.split("_") if w]
    text_ = " ".join(words)
    return text_[:1].upper() + text_[1:]


def _document_label(document_type: str | None) -> str:
    if not document_type:
        return "Document"
    template = get_registry().template_for_di_type(document_type)
    if template.key == "supporting_document":
        return document_type.replace("_", " ").capitalize()
    return template.display_name


def journey_etag(connection: Connection, *, tenant_id: str, journey_id: UUID) -> str:
    row = connection.execute(
        text(
            """
            SELECT
              COALESCE((SELECT fact_version FROM auditcore.p2_journey_runtime
                        WHERE tenant_id=:t AND journey_id=:j), 0) AS fact_version,
              COALESCE((SELECT MAX(event_id) FROM auditcore.p2_activity_events
                        WHERE tenant_id=:t AND journey_id=:j), 0) AS last_event,
              COALESCE((SELECT MAX(updated_at_utc)::text FROM auditcore.p2_tasks
                        WHERE tenant_id=:t AND journey_id=:j), '') AS tasks_at,
              COALESCE((SELECT MAX(updated_at_utc)::text FROM auditcore.p2_control_state
                        WHERE tenant_id=:t AND journey_id=:j), '') AS controls_at,
              COALESCE((SELECT MAX(COALESCE(deleted_at_utc, uploaded_at_utc))::text
                        FROM auditcore.delivery_vehicle_photos
                        WHERE tenant_id=:t AND journey_id=:j), '') AS photos_at,
              -- Deal facts can change outside the P2 pipeline (model pick,
              -- loan disbursement, pricing date via existing endpoints).
              COALESCE((SELECT MAX(updated_at_utc)::text FROM auditcore.journey_products
                        WHERE tenant_id=:t AND journey_id=:j), '') AS product_at,
              COALESCE((SELECT MAX(updated_at_utc)::text FROM auditcore.commercial_lines
                        WHERE tenant_id=:t AND journey_id=:j), '') AS lines_at,
              COALESCE((SELECT MAX(updated_at_utc)::text FROM auditcore.discount_applications
                        WHERE tenant_id=:t AND journey_id=:j), '') AS discounts_at,
              COALESCE((SELECT MAX(updated_at_utc)::text FROM auditcore.finance_records
                        WHERE tenant_id=:t AND journey_id=:j), '') AS finance_at,
              COALESCE((SELECT MAX(updated_at_utc)::text FROM auditcore.bookings
                        WHERE tenant_id=:t AND journey_id=:j), '') AS booking_at
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    material = ":".join(str(v) for v in row.values())
    return '"' + hashlib.sha256(material.encode()).hexdigest()[:24] + '"'


# ── money helpers ────────────────────────────────────────────────────────────
def _duplicate_receipts(rows: list[Any]) -> dict[Any, Any]:
    """payment_id -> the earlier payment it repeats (same receipt number,
    amount and date), by the stage engine's own rule."""
    records = [
        ReceiptRecord(
            document_id=r["payment_id"], stage_code="BOOKING", document_type_key="receipt",
            receipt_number=normalize_receipt_number(r["receipt_number"]), amount=Decimal(str(r["amount"])),
            receipt_date=normalize_receipt_date(r["receipt_date"]),
        )
        for r in rows
    ]
    out: dict[Any, Any] = {}
    for group in compute_duplicate_groups(records):
        if group.dates_match:
            for later in group.documents[1:]:
                out[later.document_id] = group.documents[0].document_id
    return out


def _loan_received(connection: Connection, *, tenant_id: str, journey_id: UUID) -> Decimal:
    """Loan disbursements not already booked as a receipt (a disbursement
    matched to a payment row is counted once, as that payment)."""
    value = connection.execute(
        text(
            """
            SELECT COALESCE(SUM(f.loan_disbursement_amount), 0) FROM auditcore.finance_records f
            WHERE f.tenant_id=:t AND f.journey_id=:j
              AND f.loan_disbursement_amount IS NOT NULL AND f.loan_disbursement_payment_id IS NULL
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()
    return Decimal(value or 0)


def _paid(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Decimal]:
    """Money actually received: evidenced receipts on ACTIVE documents, the
    same receipt uploaded twice counted once, plus the loan received."""
    rows = connection.execute(
        text(
            """
            SELECT p.payment_id, p.amount, p.receipt_number, p.receipt_date
            FROM auditcore.payments p
            JOIN auditcore.evidence e
              ON e.tenant_id=p.tenant_id AND e.journey_id=p.journey_id AND e.di_document_id=p.source_di_document_id
            WHERE p.tenant_id=:t AND p.journey_id=:j AND p.amount > 0
              AND p.status_source='EVIDENCE' AND e.association_status='ACTIVE'
            ORDER BY p.receipt_date ASC NULLS LAST, p.created_at_utc ASC, p.payment_id ASC
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    duplicates = _duplicate_receipts(rows)
    receipts = sum((Decimal(r["amount"]) for r in rows if r["payment_id"] not in duplicates), Decimal(0))
    loan = _loan_received(connection, tenant_id=tenant_id, journey_id=journey_id)
    return {"receipts": receipts, "loan": loan, "total": receipts + loan}


# ── summary ──────────────────────────────────────────────────────────────────
def gate_pass_date(connection: Connection, *, tenant_id: str, journey_id: UUID) -> str | None:
    """The delivery date as the gate pass prints it (ISO), or None when no
    gate pass has been read."""
    from audit_core.uc03_p2_dates import parse_extracted_date

    for doc in _document_facts(connection, tenant_id=tenant_id, journey_id=journey_id, di_types=("gate_pass",)):
        parsed = parse_extracted_date(doc["fields"].get("delivery_date"))
        if parsed is not None:
            return parsed.isoformat()
    return None


def summary(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    head = connection.execute(
        text(
            """
            SELECT j.journey_id, j.journey_reference, j.created_at_utc,
                   c.display_name AS customer_name, c.legal_name, c.customer_type_code, c.mobile_last4,
                   d.dealer_name, o.outlet_name, o.city,
                   jp.model_name_snapshot AS model, jp.variant_name_snapshot AS variant,
                   jp.colour_name_snapshot AS colour, jp.selection_status,
                   sku.sku_code,
                   (SELECT COALESCE(v.vin, v.chassis_number) FROM auditcore.vehicle_records v
                     WHERE v.tenant_id=j.tenant_id AND v.journey_id=j.journey_id
                     ORDER BY v.updated_at_utc DESC LIMIT 1) AS vin,
                   (SELECT r.registration_number FROM auditcore.registration_records r
                     WHERE r.tenant_id=j.tenant_id AND r.journey_id=j.journey_id
                       AND r.registration_number IS NOT NULL
                     ORDER BY r.updated_at_utc DESC LIMIT 1) AS registration_number,
                   (SELECT f.provider_name FROM auditcore.finance_records f
                     WHERE f.tenant_id=j.tenant_id AND f.journey_id=j.journey_id
                     ORDER BY f.updated_at_utc DESC LIMIT 1) AS financier,
                   (SELECT i.insurer_name FROM auditcore.insurance_records i
                     WHERE i.tenant_id=j.tenant_id AND i.journey_id=j.journey_id
                     ORDER BY i.updated_at_utc DESC LIMIT 1) AS insurer,
                   (SELECT dl.actual_delivered_at FROM auditcore.deliveries dl
                     WHERE dl.tenant_id=j.tenant_id AND dl.journey_id=j.journey_id
                     ORDER BY dl.updated_at_utc DESC LIMIT 1) AS delivered_at
            FROM auditcore.journeys j
            JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
            JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
            JOIN auditcore.dealer_outlets o
              ON o.tenant_id=j.tenant_id AND o.dealer_id=j.dealer_id AND o.outlet_id=j.outlet_id
            LEFT JOIN auditcore.journey_products jp
              ON jp.tenant_id=j.tenant_id AND jp.journey_id=j.journey_id
            LEFT JOIN auditcore.product_skus sku ON sku.product_sku_id=jp.product_sku_id
            WHERE j.tenant_id=:t AND j.journey_id=:j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    counts = connection.execute(
        text(
            f"""
            SELECT
              (SELECT COUNT(*) FROM auditcore.evidence WHERE tenant_id=:t AND journey_id=:j
                 AND association_status='ACTIVE') AS documents,
              (SELECT COUNT(*) FROM auditcore.delivery_vehicle_photos WHERE tenant_id=:t
                 AND journey_id=:j AND deleted_at_utc IS NULL) AS photos,
              (SELECT COUNT(*) FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j
                 AND task_status NOT IN {_OPEN_TASK_EXCLUDED}) AS open_tasks,
              (SELECT COUNT(*) FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j
                 AND task_status NOT IN {_OPEN_TASK_EXCLUDED} AND due_at_utc < now()) AS overdue_tasks,
              (SELECT COUNT(*) FROM auditcore.audit_findings WHERE tenant_id=:t AND journey_id=:j
                 AND finding_status IN ('OPEN','ACKNOWLEDGED')) AS open_findings
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    stage = read_booking_stage(connection, tenant_id=tenant_id, journey_id=journey_id)
    deal_view = deal(connection, tenant_id=tenant_id, journey_id=journey_id)
    vehicle_text = " · ".join(v for v in (head["model"], head["variant"], head["colour"]) if v) or None
    return {
        "journey": {
            "journeyId": str(head["journey_id"]),
            "reference": head["journey_reference"],
            "customerName": head["legal_name"] or head["customer_name"],
            "customerType": head["customer_type_code"],
            "mobileLast4": head["mobile_last4"],
            "dealerName": head["dealer_name"],
            "outletName": head["outlet_name"],
            "city": head["city"],
            "vehicle": vehicle_text,
            "skuCode": head["sku_code"],
            "vehicleResolution": head["selection_status"],
            "vin": head["vin"],
            "registrationNumber": head["registration_number"],
            "financier": head["financier"],
            "insurer": head["insurer"],
            "createdAtUtc": head["created_at_utc"],
            "deliveredAtUtc": gate_pass_date(connection, tenant_id=tenant_id, journey_id=journey_id) or head["delivered_at"],
        },
        "stage": stage,
        "money": {
            **deal_view["summary"],
            "flagged": deal_view["flagged"],
            "minimumBookingAmount": stage.get("minimumBookingAmount"),
            "bookingReceiptTotal": stage.get("bookingReceiptTotal"),
        },
        "numbers": {
            "documents": int(counts["documents"] or 0),
            "vehiclePhotos": int(counts["photos"] or 0),
            "openTasks": int(counts["open_tasks"] or 0),
            "overdueTasks": int(counts["overdue_tasks"] or 0),
            "openFindings": int(counts["open_findings"] or 0),
        },
        "controls": control_statistics(connection, tenant_id=tenant_id, journey_id=journey_id),
        "sections": list(SECTIONS),
    }


# ── deal ─────────────────────────────────────────────────────────────────────
def _column_for(document_type: Any) -> str:
    """Which column a document's value belongs in: the booking form is the
    offer, the ledger and cost sheet are what they are, and every invoice,
    cover note or challan is a billed (actual) value."""
    return (
        "booking" if document_type in _BOOKING_SOURCES
        else "ledger" if document_type in _LEDGER_SOURCES
        else "quote" if document_type in _QUOTE_SOURCES
        else "billed"
    )


def _columns(per_source: list[Any]) -> dict[str, Any]:
    """Booking / billed / ledger / quote value for one component. Sources are
    ordered oldest first, so the newest document of each kind wins."""
    out: dict[str, Any] = {"booking": None, "billed": None, "ledger": None, "quote": None}
    for source in per_source:
        out[_column_for(source["source_document_type"])] = source["amount"]
    return out


def _billed_disagree(per_source: list[Any]) -> bool:
    """Two invoices (say the DMS retail invoice and the Tally tax invoice)
    printing different amounts for the same component."""
    amounts = {
        _dec(s["amount"]) for s in per_source
        if _column_for(s["source_document_type"]) == "billed" and _dec(s["amount"]) is not None
    }
    return len(amounts) > 1


# A whole invoice of one purpose bills one component; a categorised line
# item inside any invoice bills its own (kept in step with
# uc03_invoice_materialization).
_WHOLE_DOCUMENT_COMPONENT = {
    "accessory_invoice_dms": "accessories_cost", "accessory_invoice_tally": "accessories_cost",
    "ew_invoice": "additional_warranty_amount", "rsa_invoice": "rsa_amount",
}
_LINE_COMPONENT = {
    "ACCESSORY_GENUINE": "accessories_cost", "ACCESSORY_NON_GENUINE": "accessories_cost",
    "EXTENDED_WARRANTY": "additional_warranty_amount", "RSA": "rsa_amount", "INSURANCE": "insurance_amount",
    "FASTAG": "fastag_amount", "TCS": "tcs_amount",
}


def _line_entries(fields: dict[str, Any]) -> list[dict[str, Any]]:
    """The line items of an invoice as extracted, whatever shape they were stored in."""
    raw = fields.get("line_items")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    return [entry for entry in (raw if isinstance(raw, list) else []) if isinstance(entry, dict)]


def _document_billed(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the documents themselves say, component by component: the booking
    form's own prices, the whole of an accessory, extended warranty or RSA
    invoice, the premium on the insurance cover note, and the categorised
    line items of any invoice. The invoice is the source of truth for the
    deal, so these values stand in their column whenever the materialised
    source table has no row yet for that (component, document)."""
    out: list[dict[str, Any]] = []
    for doc in documents:
        kind, fields = doc["documentType"], doc["fields"]
        if kind not in _ITEM_SOURCES and kind != "insurance_cover" and kind not in _BOOKING_SOURCES:
            continue
        totals: dict[str, Decimal] = {}
        whole = _WHOLE_DOCUMENT_COMPONENT.get(kind)
        if kind in _BOOKING_SOURCES:
            # The booking form prices each component and discount by name.
            for key, value in fields.items():
                amount = _dec(value)
                if amount is not None and (key in _COMPONENT_LABELS or _benefit_key(key)):
                    totals[key] = amount
        elif whole:
            amount = _dec(fields.get("grand_total_amount"))
            if amount is None:
                amount = _dec(fields.get("taxable_amount"))
            if amount is not None:
                totals[whole] = amount
        elif kind == "insurance_cover":
            amount = _dec(fields.get("premium_amount"))
            if amount is not None:
                totals["insurance_amount"] = amount
        else:
            for entry in _line_entries(fields):
                component = _LINE_COMPONENT.get(str(entry.get("line_category") or "").upper())
                amount = _dec(entry.get("net_amount"))
                if amount is None:
                    amount = _dec(entry.get("gross_amount"))
                if component and amount is not None:
                    totals[component] = totals.get(component, Decimal(0)) + amount
        for component, amount in totals.items():
            out.append({
                "line_kind": "COMMERCIAL", "component_key": component, "source_document_type": kind,
                "amount": amount, "source_document_id": doc["documentId"],
            })
    # oldest document first, as the materialised rows are ordered
    out.reverse()
    return out


def _flags(kind: str, standard: Any, booking: Any, billed: Any, ledger: Any) -> list[str]:
    flags: list[str] = []
    std, bk, bl, lg = _dec(standard), _dec(booking), _dec(billed), _dec(ledger)
    if bk is not None and bl is not None and bk != bl:
        flags.append("BOOKING_VS_BILLED")
    if bl is not None and lg is not None and bl != lg:
        flags.append("BILLED_VS_LEDGER")
    reference = bl if bl is not None else bk
    if std is not None and reference is not None and reference != std:
        if kind == "COMMERCIAL":
            flags.append("ABOVE_STANDARD" if reference > std else "BELOW_STANDARD")
        else:
            flags.append("OVER_ENTITLEMENT" if reference > std else "UNDER_ENTITLEMENT")
    return flags


def _source_list(per_source: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "document": _document_label(s["source_document_type"]),
            "documentType": s["source_document_type"],
            "amount": _money(s["amount"]),
            "documentId": str(s["source_document_id"]) if s["source_document_id"] else None,
        }
        for s in per_source
    ]


def _column_total(rows: list[dict[str, Any]], column: str) -> str | None:
    values = [Decimal(r[column]) for r in rows if r.get(column) is not None]
    return str(sum(values, Decimal(0))) if values else None


def _net(gross: str | None, disc: str | None) -> str | None:
    return None if gross is None else str(Decimal(gross) - Decimal(disc or 0))


# The lines a customer opts in or out of: every discount, the accessories
# and the extended warranty (decision 2026-09-30). Insurance has its own
# Inhouse / Self choice.
_OPTED_COMPONENTS = frozenset({
    "accessories_cost", "essential_kit_amount", "genuine_accessories_amount", "non_genuine_accessories_amount",
    "additional_warranty_amount", "extended_warranty_amount",
})
_VEHICLE_INVOICE_TYPES = ("customer_invoice_dms", "tax_invoice_tally")


def _opted(billed: Any, booking: Any, *, invoiced: bool) -> dict[str, Any]:
    """Whether the customer took this line, read from the invoice once the
    deal has one (a line the invoice does not carry is opted out), else
    from the booking form."""
    if invoiced:
        value = _dec(billed)
        return {"taken": value is not None and value > 0, "source": "invoice"}
    value = _dec(booking)
    if value is None:
        return {"taken": False, "source": None}
    return {"taken": value > 0, "source": "booking"}


def resolve_insurance_source(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """Inhouse (through the dealership; the premium is part of the deal) or
    Self (the customer arranged it; the premium is not). The PC's
    confirmation decides; until then Inhouse is assumed. The cover-note
    rule that reads it from the document slots in here."""
    row = connection.execute(
        text(
            """
            SELECT insurance_source, insurance_source_set_at_utc, insurance_source_set_by_actor_id
            FROM auditcore.insurance_records WHERE tenant_id=:t AND journey_id=:j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().first()
    if row and row["insurance_source"]:
        return {"source": str(row["insurance_source"]), "decidedBy": "PC",
                "decidedAt": row["insurance_source_set_at_utc"], "actorId": row["insurance_source_set_by_actor_id"]}
    return {"source": "INHOUSE", "decidedBy": "DEFAULT", "decidedAt": None, "actorId": None}


def deal(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    params = {"t": tenant_id, "j": journey_id}
    lines = connection.execute(
        text(
            """
            SELECT component_key, standard_amount, actual_amount, actual_source_kind, source_reference
            FROM auditcore.commercial_lines WHERE tenant_id=:t AND journey_id=:j
            """
        ),
        params,
    ).mappings().all()
    sources = connection.execute(
        text(
            """
            SELECT line_kind, component_key, source_document_type, amount, source_document_id
            FROM auditcore.commercial_line_source_values WHERE tenant_id=:t AND journey_id=:j
            ORDER BY updated_at_utc, source_document_type
            """
        ),
        params,
    ).mappings().all()
    discounts = connection.execute(
        text(
            """
            SELECT da.discount_key, da.standard_eligible_amount, da.actual_discount_amount,
                   da.eligibility_result, da.evidence_status, da.actual_source_kind,
                   ds.scheme_code, ds.scheme_name, ds.scheme_category,
                   dsv.version_no AS scheme_version, dsv.effective_from, dsv.effective_to,
                   dsv.combinability_code
            FROM auditcore.discount_applications da
            LEFT JOIN auditcore.discount_scheme_versions dsv
              ON dsv.tenant_id=da.tenant_id AND dsv.discount_scheme_version_id=da.discount_scheme_version_id
            LEFT JOIN auditcore.discount_schemes ds
              ON ds.tenant_id=dsv.tenant_id AND ds.discount_scheme_id=dsv.discount_scheme_id
            WHERE da.tenant_id=:t AND da.journey_id=:j
            ORDER BY da.discount_key, da.discount_application_id
            """
        ),
        params,
    ).mappings().all()
    sku = connection.execute(
        text(
            """
            SELECT pl.price_list_name, plv.version_no, plv.effective_from,
                   jp.model_name_snapshot AS model, jp.variant_name_snapshot AS variant,
                   jp.colour_name_snapshot AS colour, jp.selection_status, jp.selection_method,
                   sku.sku_code
            FROM auditcore.journeys j
            LEFT JOIN auditcore.price_list_versions plv
              ON plv.tenant_id=j.tenant_id AND plv.price_list_version_id=j.price_list_version_id
            LEFT JOIN auditcore.price_lists pl
              ON pl.tenant_id=plv.tenant_id AND pl.price_list_id=plv.price_list_id
            LEFT JOIN auditcore.journey_products jp
              ON jp.tenant_id=j.tenant_id AND jp.journey_id=j.journey_id
            LEFT JOIN auditcore.product_skus sku ON sku.product_sku_id=jp.product_sku_id
            WHERE j.tenant_id=:t AND j.journey_id=:j
            """
        ),
        params,
    ).mappings().first()
    proof_on_file = {
        str(r[0]) for r in connection.execute(
            text(
                """
                SELECT DISTINCT document_type_key FROM auditcore.evidence
                WHERE tenant_id=:t AND journey_id=:j AND association_status='ACTIVE'
                  AND document_type_key IS NOT NULL
                """
            ),
            params,
        ).all()
    }

    # The documents' own billed values fill in where the materialised source
    # table has no row yet for that (component, document type).
    documents = _document_facts(
        connection, tenant_id=tenant_id, journey_id=journey_id,
        di_types=tuple(dict.fromkeys(_INVOICE_TYPES + _ITEM_SOURCES + ("insurance_cover",) + tuple(_BOOKING_SOURCES))),
    )
    invoice_documents = [d for d in documents if d["documentType"] in _INVOICE_TYPES]
    invoiced_deal = bool(invoice_documents) or any(str(s["source_document_type"]) in _INVOICE_TYPES for s in sources)
    present = {(str(s["component_key"]), str(s["source_document_type"])) for s in sources}
    sources = list(sources) + [
        s for s in _document_billed(documents) if (s["component_key"], s["source_document_type"]) not in present
    ]

    # Normalise every source row onto (kind, key): discount-like commercial
    # fields fold onto their scheme benefit, declared totals are set apart.
    declared: dict[str, list[Any]] = {}
    normalised: list[dict[str, Any]] = []
    for s in sources:
        raw = str(s["component_key"])
        if raw.lower() in _DECLARED_TOTAL_KEYS:
            declared.setdefault(raw.lower(), []).append(s)
            continue
        benefit = _benefit_key(raw)
        if s["line_kind"] == "DISCOUNT":
            normalised.append({**s, "line_kind": "DISCOUNT", "component_key": benefit or canonical_discount_key(raw)})
        elif benefit:
            normalised.append({**s, "line_kind": "DISCOUNT", "component_key": benefit})
        else:
            normalised.append(dict(s))
    sources = normalised

    by_line = {
        str(r["component_key"]): r for r in lines
        if str(r["component_key"]).lower() not in _DECLARED_TOTAL_KEYS and not _benefit_key(str(r["component_key"]))
    }
    commercial_keys = set(by_line) | {
        str(s["component_key"]) for s in sources if s["line_kind"] == "COMMERCIAL"
    }
    categories: dict[str, list[dict[str, Any]]] = {code: [] for code, _ in _CATEGORIES}
    for key in commercial_keys:
        line = by_line.get(key)
        per_source = [s for s in sources if s["line_kind"] == "COMMERCIAL" and s["component_key"] == key]
        cols = _columns(per_source)
        standard = line["standard_amount"] if line else None
        effective_source = None
        if line and line["source_reference"]:
            effective_type = str(line["source_reference"]).split(":")[0]
            effective_source = _document_label(effective_type)
            # The canonical line already knows which document set its value;
            # a source table written before that document was recorded
            # still shows it in the right column.
            if line["actual_amount"] is not None and cols[_column_for(effective_type)] is None:
                cols[_column_for(effective_type)] = line["actual_amount"]
        reference = cols["billed"] if cols["billed"] is not None else cols["booking"]
        flags = _flags("COMMERCIAL", standard, cols["booking"], cols["billed"], cols["ledger"])
        if _billed_disagree(per_source):
            flags.append("INVOICES_DISAGREE")
        categories[component_category(key)].append({
            "key": key,
            "label": component_label(key),
            "opted": _opted(cols["billed"], cols["booking"], invoiced=invoiced_deal) if key in _OPTED_COMPONENTS else None,
            "standard": _money(standard),
            "booking": _money(cols["booking"]),
            "billed": _money(cols["billed"]),
            "ledger": _money(cols["ledger"]),
            "quote": _money(cols["quote"]),
            "effective": _money(line["actual_amount"]) if line else None,
            "effectiveSource": effective_source,
            "variance": _minus(reference, standard),
            "bookingVsBilled": _minus(cols["billed"], cols["booking"]),
            "flags": flags,
            "sources": _source_list(per_source),
        })

    order = list(_COMPONENT_LABELS)
    groups = []
    for code, label in _CATEGORIES:
        rows = sorted(categories[code], key=lambda r: (order.index(r["key"]) if r["key"] in order else 99, r["label"]))
        if rows:
            groups.append({
                "code": code,
                "label": label,
                "components": rows,
                "totals": {c: _column_total(rows, c) for c in ("standard", "booking", "billed", "ledger", "effective")},
            })
    all_rows = [r for g in groups for r in g["components"]]

    by_discount: dict[str, dict[str, Any]] = {}
    for row in discounts:
        by_discount.setdefault(canonical_discount_key(str(row["discount_key"])), dict(row))
    discount_keys = set(by_discount) | {str(s["component_key"]) for s in sources if s["line_kind"] == "DISCOUNT"}
    d_order = list(_DISCOUNT_LABELS)
    discount_rows = []
    for key in sorted(discount_keys, key=lambda k: (d_order.index(k) if k in d_order else 99, k)):
        row = by_discount.get(key) or {}
        per_source = [s for s in sources if s["line_kind"] == "DISCOUNT" and s["component_key"] == key]
        cols = _columns(per_source)
        entitled = row.get("standard_eligible_amount")
        # Given with no scheme entitlement (reconciliation marks it
        # NOT_ELIGIBLE; a discretionary dealer discount never has one): the
        # entitlement is zero, so the whole amount is the over-grant.
        if entitled is None and (row.get("eligibility_result") == "NOT_ELIGIBLE" or key == "ADDITIONAL_DISCOUNT"):
            entitled = Decimal(0)
        reference = cols["billed"] if cols["billed"] is not None else cols["booking"]
        if reference is None:
            reference = row.get("actual_discount_amount")
        proof_type = CONDITIONAL_DISCOUNT_EVIDENCE_DOCUMENT.get(key)
        discount_rows.append({
            "key": key,
            "label": discount_label(key),
            "opted": _opted(cols["billed"], cols["booking"], invoiced=invoiced_deal),
            "scheme": {
                "code": row.get("scheme_code"),
                "name": row.get("scheme_name"),
                "category": row.get("scheme_category"),
                "version": row.get("scheme_version"),
                "validFrom": row.get("effective_from"),
                "validTo": row.get("effective_to"),
                "combinability": row.get("combinability_code"),
            } if row.get("scheme_code") else None,
            "eligibility": row.get("eligibility_result"),
            "evidenceStatus": row.get("evidence_status"),
            "proof": {
                "documentType": proof_type,
                "document": _document_label(proof_type),
                "onFile": proof_type in proof_on_file,
            } if proof_type else None,
            "entitled": _money(entitled),
            "booking": _money(cols["booking"]),
            "billed": _money(cols["billed"]),
            "ledger": _money(cols["ledger"]),
            "effective": _money(row.get("actual_discount_amount")),
            "variance": _minus(reference, entitled),
            "flags": _flags("DISCOUNT", entitled, cols["booking"], cols["billed"], cols["ledger"]),
            "sources": _source_list(per_source),
        })

    # "Current" is what the deal stands at now: the billed value where the
    # component has been invoiced, else what the booking offered. Totals of a
    # partly-invoiced deal therefore never look short.
    for row in all_rows:
        row["current"] = row["billed"] if row["billed"] is not None else row["booking"]
    for row in discount_rows:
        row["current"] = next(
            (row[c] for c in ("billed", "booking", "effective") if row[c] is not None), None,
        )
    # Insurance: Inhouse premium is part of the deal; Self (the customer
    # arranged it) is shown but kept out of every total.
    insurance = resolve_insurance_source(connection, tenant_id=tenant_id, journey_id=journey_id)
    self_insured = insurance["source"] == "SELF"
    for group in groups:
        if group["code"] == "INSURANCE":
            group["excluded"] = self_insured
            for row in group["components"]:
                row["excluded"] = self_insured
    counted_rows = [r for r in all_rows if not r.get("excluded")]
    insurance["invoiceOnFile"] = any(
        s["line_kind"] == "COMMERCIAL" and s["component_key"] in _ADDON_COMPONENTS["insurance"]
        and str(s["source_document_type"]) in _INVOICE_TYPES for s in sources
    ) or bool(_line_items(invoice_documents, _LINE_CATEGORIES["insurance"]))
    insurance["vehicleInvoiced"] = any(d["documentType"] in _VEHICLE_INVOICE_TYPES for d in documents)
    std_total, bk_total, cur_total, bl_total, lg_total = (
        _column_total(counted_rows, c) for c in ("standard", "booking", "current", "billed", "ledger")
    )
    d_std, d_bk, d_cur, d_bl = (_column_total(discount_rows, c) for c in ("entitled", "booking", "current", "billed"))
    net_std, net_bk, net_cur = _net(std_total, d_std), _net(bk_total, d_bk), _net(cur_total, d_cur)

    def matched(left: str, right: str) -> str | None:
        """Charges less discounts, compared only where both sides have a
        value, so a component missing on one side is never a variance."""
        pairs = [(r[left], r[right]) for r in counted_rows if r[left] is not None and r[right] is not None]
        d_left = "entitled" if left == "standard" else left
        d_right = "entitled" if right == "standard" else right
        d_pairs = [(r[d_left], r[d_right]) for r in discount_rows
                   if r[d_left] is not None and r[d_right] is not None]
        if not pairs and not d_pairs:
            return None
        charges = sum((Decimal(a) - Decimal(b) for a, b in pairs), Decimal(0))
        discounts_delta = sum((Decimal(a) - Decimal(b) for a, b in d_pairs), Decimal(0))
        return str(charges - discounts_delta)

    paid = _paid(connection, tenant_id=tenant_id, journey_id=journey_id)
    payable = net_cur or net_std
    invoiced = sum(1 for r in counted_rows if r["billed"] is not None)
    registry = get_registry()
    invoices = [
        {
            "documentId": doc["documentId"], "documentType": doc["documentType"],
            "label": registry.template_for_di_type(doc["documentType"], stage=None).display_name,
            "number": doc["fields"].get("invoice_number") or doc["fields"].get("debit_note_number"),
            "date": doc["fields"].get("invoice_date") or doc["fields"].get("debit_note_date"),
            "total": _money(doc["fields"].get("grand_total_amount") or doc["fields"].get("total_amount")),
        }
        for doc in documents if doc["documentType"] in _INVOICE_TYPES
    ]
    return {
        "sku": {
            "skuCode": sku["sku_code"] if sku else None,
            "model": sku["model"] if sku else None,
            "variant": sku["variant"] if sku else None,
            "colour": sku["colour"] if sku else None,
            "resolution": sku["selection_status"] if sku else None,
            "method": sku["selection_method"] if sku else None,
            "priceList": sku["price_list_name"] if sku else None,
            "priceListVersion": sku["version_no"] if sku else None,
            "priceListEffectiveFrom": sku["effective_from"] if sku else None,
        },
        "categories": groups,
        "discounts": discount_rows,
        "insurance": insurance,
        # The invoices consolidated into this sheet, retail and tax alike.
        "invoices": invoices,
        "declared": [
            {"key": key, "label": label,
             **{c: _money(v) for c, v in _columns(declared[key]).items()},
             "sources": _source_list(declared[key])}
            for key, label in _DECLARED_TOTAL_KEYS.items() if key in declared
        ],
        "summary": {
            "gross": {"standard": std_total, "booking": bk_total, "current": cur_total,
                      "billed": bl_total, "ledger": lg_total},
            "discounts": {"standard": d_std, "booking": d_bk, "current": d_cur, "billed": d_bl},
            "net": {"standard": net_std, "booking": net_bk, "current": net_cur},
            "variance": {
                "bookingVsStandard": matched("booking", "standard"),
                "currentVsStandard": matched("current", "standard"),
                "billedVsBooking": matched("billed", "booking"),
            },
            "invoicedComponents": invoiced,
            "components": len(counted_rows),
            "paid": {"receipts": str(paid["receipts"]), "loan": str(paid["loan"]), "total": str(paid["total"])},
            "payable": payable,
            "balanceDue": _minus(payable, paid["total"]),
        },
        "flagged": sum(1 for r in counted_rows + discount_rows if r["flags"]),
    }


# ── add-ons: insurance, accessories, protection plans, finance, exchange ─────
def _rows(connection: Connection, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [_plain(r) for r in connection.execute(text(sql), params).mappings().all()]


def addons(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    params = {"t": tenant_id, "j": journey_id}
    insurance = _rows(connection, """
        SELECT insurer_name, policy_reference, cover_note_reference, insurance_by,
               self_insurance_flag, agent_intermediary_name, agent_intermediary_code, misp_code,
               standard_premium_amount, actual_premium_amount, add_ons, actual_status_code
        FROM auditcore.insurance_records WHERE tenant_id=:t AND journey_id=:j
        ORDER BY updated_at_utc DESC""", params)
    for item in insurance:
        item["premium_variance"] = _minus(item["actual_premium_amount"], item["standard_premium_amount"])
    finance = _rows(connection, """
        SELECT f.finance_type_code, f.provider_name, f.financed_amount, f.loan_disbursement_amount,
               f.loan_disbursement_confidence, f.loan_disbursement_match_basis, f.do_reference,
               f.po_reference, f.actual_status_code,
               p.receipt_date AS disbursement_date, p.payment_reference AS disbursement_reference
        FROM auditcore.finance_records f
        LEFT JOIN auditcore.payments p
          ON p.tenant_id=f.tenant_id AND p.payment_id=f.loan_disbursement_payment_id
        WHERE f.tenant_id=:t AND f.journey_id=:j
        ORDER BY f.updated_at_utc DESC""", params)
    for item in finance:
        item["disbursement_gap"] = _minus(item["financed_amount"], item["loan_disbursement_amount"])
    plans = _rows(connection, """
        SELECT addon_type_code, provider_name, standard_amount, actual_amount, reference_number, source_kind
        FROM auditcore.journey_addons WHERE tenant_id=:t AND journey_id=:j
        ORDER BY addon_type_code""", params)
    for item in plans:
        item["variance"] = _minus(item["actual_amount"], item["standard_amount"])
        item["label"] = str(item["addon_type_code"]).replace("_", " ").capitalize()
    exchange = _rows(connection, """
        SELECT old_vehicle_registration, old_vehicle_make_model, quoted_value, actual_value,
               actual_status_code, handover_at_utc, payment_at_utc
        FROM auditcore.trade_in_cases WHERE tenant_id=:t AND journey_id=:j
        ORDER BY updated_at_utc DESC""", params)
    for item in exchange:
        item["variance"] = _minus(item["actual_value"], item["quoted_value"])

    # Accessory, insurance and protection charges live on the deal lines too;
    # surface them here so the add-on view is complete on its own.
    deal_view = deal(connection, tenant_id=tenant_id, journey_id=journey_id)
    lines = {g["code"]: g for g in deal_view["categories"]}
    discounts = {d["key"]: d for d in deal_view["discounts"]}
    is_accessory = lambda p: "ACCESS" in str(p["addon_type_code"]).upper()
    return {
        "taken": _taken_addons(connection, tenant_id=tenant_id, journey_id=journey_id, deal_view=deal_view),
        "insurance": {"records": insurance, "charges": lines.get("INSURANCE"),
                      "discount": discounts.get("INSURANCE")},
        "accessories": {"records": [p for p in plans if is_accessory(p)], "charges": lines.get("ACCESSORIES"),
                        "discount": discounts.get("ACCESSORIES_KIT")},
        "protection": {"records": [p for p in plans if not is_accessory(p)], "charges": lines.get("PROTECTION"),
                       "discounts": [d for k, d in discounts.items() if k.startswith("EXT_WARRANTY")]},
        "finance": {"records": finance},
        "exchange": {"records": exchange, "discount": discounts.get("EXCHANGE_BONUS")},
        "scrappage": {"discounts": [d for k, d in discounts.items() if k.startswith("SCRAPPAGE")]},
    }


# ── documents with every extracted field ─────────────────────────────────────
def documents(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    registry = get_registry()
    evidence = connection.execute(
        text(
            """
            SELECT e.evidence_id, e.di_document_id, e.document_type_key, e.process_area, e.linked_at_utc,
                   q.queue_id, q.template_key, q.page_numbers, q.page_number, q.display_name
            FROM auditcore.evidence e
            LEFT JOIN LATERAL (
              SELECT queue_id, template_key, page_numbers, page_number, display_name
              FROM auditcore.p2_document_queue q
              WHERE q.tenant_id=e.tenant_id AND q.journey_id=e.journey_id AND q.di_document_id=e.di_document_id
                AND q.retired_at_utc IS NULL
              ORDER BY q.updated_at_utc DESC LIMIT 1
            ) q ON true
            WHERE e.tenant_id=:t AND e.journey_id=:j AND e.association_status='ACTIVE'
            ORDER BY e.linked_at_utc
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    fields = connection.execute(
        text(
            """
            SELECT di_document_id, field_key, extracted_value, effective_value, confidence_score,
                   is_modified, reviewed_at_utc, modified_at_utc
            FROM auditcore.journey_document_extracted_fields
            WHERE tenant_id=:t AND journey_id=:j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    by_document: dict[str, list[Any]] = {}
    for f in fields:
        by_document.setdefault(str(f["di_document_id"]), []).append(f)

    out = []
    for e in evidence:
        template = (
            registry.documents.get(e["template_key"]) if e["template_key"] else None
        ) or registry.template_for_di_type(e["document_type_key"], stage=e["process_area"])
        schema_order = [f["key"] for f in registry.di_fields(template.di_types[0])] if template.di_types else []
        key_order = list(template.key_fields) + [k for k in schema_order if k not in template.key_fields]
        rows = []
        for f in by_document.get(str(e["di_document_id"]), []):
            confidence = float(f["confidence_score"]) if f["confidence_score"] is not None else None
            value = f["effective_value"] if f["effective_value"] is not None else f["extracted_value"]
            # Same rule as the review tasks (uc03_p2_stage.unreviewed_fields):
            # only a value that was read, below 90%, needs a look. A field the
            # extractor left empty has nothing to verify.
            populated = value is not None and value != ""
            rows.append({
                "key": f["field_key"],
                "label": field_label(str(f["field_key"])),
                "value": value,
                "machineValue": f["extracted_value"],
                "corrected": bool(f["is_modified"]),
                "confidence": confidence,
                "reviewed": f["reviewed_at_utc"] is not None or bool(f["is_modified"]),
                "needsReview": populated and template.needs_review(f["field_key"], confidence)
                and f["reviewed_at_utc"] is None and not f["is_modified"],
                "keyField": f["field_key"] in template.key_fields,
            })
        rows.sort(key=lambda r: (0 if r["keyField"] else 1,
                                 key_order.index(r["key"]) if r["key"] in key_order else 99, r["label"]))
        out.append({
            "documentId": str(e["di_document_id"]),
            "evidenceId": str(e["evidence_id"]),
            "queueId": str(e["queue_id"]) if e["queue_id"] else None,
            "documentType": e["document_type_key"],
            "templateKey": template.key,
            "label": e["display_name"] or (template.display_name if template.key != "supporting_document"
                                           else _document_label(e["document_type_key"])),
            "stage": template.stage,
            "pages": list(e["page_numbers"] or ([e["page_number"]] if e["page_number"] else [])),
            "linkedAtUtc": e["linked_at_utc"],
            "fields": rows,
            "fieldCount": len(rows),
            "needsReview": sum(1 for r in rows if r["needsReview"]),
            "corrected": sum(1 for r in rows if r["corrected"]),
        })
    # Pages the PC kept as Others (decision 2026-09-30): on file under the
    # name the PC gave them, never read, so no evidence row and no values.
    listed = {d["documentId"] for d in out}
    others = connection.execute(
        text(
            """
            SELECT queue_id, di_document_id, display_name, business_stage, page_numbers, page_number, updated_at_utc
            FROM auditcore.p2_document_queue
            WHERE tenant_id=:t AND journey_id=:j AND queue_status='SUPPORTING'
              AND type_overridden_by_actor_id IS NOT NULL AND display_name IS NOT NULL
              AND retired_at_utc IS NULL
            ORDER BY updated_at_utc
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    for o in others:
        document_id = str(o["di_document_id"]) if o["di_document_id"] else f"page:{o['queue_id']}"
        if document_id in listed:
            continue
        out.append({
            "documentId": document_id,
            "evidenceId": None,
            "queueId": str(o["queue_id"]),
            "documentType": None,
            "templateKey": "supporting_document",
            "label": o["display_name"],
            "stage": o["business_stage"] or "BOOKING",
            "pages": list(o["page_numbers"] or ([o["page_number"]] if o["page_number"] else [])),
            "linkedAtUtc": o["updated_at_utc"],
            "fields": [],
            "fieldCount": 0,
            "needsReview": 0,
            "corrected": 0,
        })
    return {"documents": out}


# ── payments ─────────────────────────────────────────────────────────────────
_BANK_DATE_WINDOW_DAYS = 3
_MIN_UTR_LEN = 6
_BANK_METHOD_LABEL = {"REFERENCE_EXACT": "REFERENCE", "UTR_SUFFIX": "UTR", "AMOUNT_DATE": "AMOUNT_DATE"}


def _normalize_ref(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _utr_suffix_match(left: str, right: str) -> bool:
    stripped = left.lstrip("0")
    return len(stripped) >= _MIN_UTR_LEN and right.endswith(stripped)


def _is_cash(mode: Any) -> bool:
    return _normalize_ref(mode).startswith("CASH")


def _iso_day(value: Any) -> str | None:
    parsed = parse_extracted_date(value)
    return parsed.isoformat() if parsed else None


def _bank_lines(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """Every bank statement entry read on the Journey, as the statement prints
    it (one extracted entry per statement document)."""
    lines = []
    for doc in _document_facts(connection, tenant_id=tenant_id, journey_id=journey_id,
                               di_types=("bank_statement_extract",)):
        f = doc["fields"]
        credit, debit = _dec(f.get("credit_amount")), _dec(f.get("debit_amount"))
        if credit is None and debit is None:
            continue
        lines.append({
            "documentId": doc["documentId"],
            "bank": f.get("bank_name"), "accountHolder": f.get("account_holder_name"),
            "accountNumber": f.get("account_number"),
            "date": _iso_day(f.get("transaction_date") or f.get("value_date")),
            "description": f.get("transaction_description"), "reference": f.get("reference_no"),
            "counterparty": f.get("counterparty_name"),
            "credit": _money(credit), "debit": _money(debit), "balance": _money(f.get("running_balance")),
            "matchedPaymentId": None, "matchedReceipt": None, "matchMethod": None,
        })
    lines.sort(key=lambda line: (line["date"] or "9999", line["documentId"]))
    return lines


def _match_bank_line(payment: dict[str, Any], lines: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """The statement credit a receipt stands for: the same amount within a few
    days of the receipt date, tied by the payment reference (UTR) when both
    sides print one, else the one credit of that amount in the window."""
    amount = _dec(payment["amount"])
    receipt_date = parse_extracted_date(payment["receiptDate"])
    reference = _normalize_ref(payment["reference"])
    candidates = []
    for line in lines:
        if line["matchedPaymentId"] or amount is None or _dec(line["credit"]) != amount:
            continue
        line_date = parse_extracted_date(line["date"])
        if receipt_date and line_date and abs((line_date - receipt_date).days) > _BANK_DATE_WINDOW_DAYS:
            continue
        candidates.append(line)
    if not candidates:
        return None, "NONE"
    if reference:
        exact = [line for line in candidates if _normalize_ref(line["reference"]) == reference]
        if len(exact) == 1:
            return exact[0], "REFERENCE"
        suffix = [line for line in candidates if _normalize_ref(line["reference"]) and (
            _utr_suffix_match(reference, _normalize_ref(line["reference"]))
            or _utr_suffix_match(_normalize_ref(line["reference"]), reference))]
        if not exact and len(suffix) == 1:
            return suffix[0], "UTR"
    if len(candidates) == 1:
        return candidates[0], "AMOUNT_DATE"
    return None, "AMBIGUOUS"


def payments(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """Every receipt on the Journey and what it is worth: the same receipt
    uploaded twice (same number, amount and date) counts once, a receipt
    whose document was removed counts nothing, and each counted receipt is
    tied to the bank statement credit it stands for when a statement is on
    file."""
    rows = connection.execute(
        text(
            """
            SELECT p.payment_id, p.amount, p.receipt_number, p.receipt_date, p.payment_at_utc,
                   COALESCE(p.payment_mode_code, p.payment_method_code) AS mode,
                   p.payment_stage, p.source_di_document_id, p.receipt_bank_name,
                   p.payment_reference, p.status_source, p.created_at_utc,
                   e.association_status, e.document_type_key
            FROM auditcore.payments p
            LEFT JOIN auditcore.evidence e
              ON e.tenant_id=p.tenant_id AND e.journey_id=p.journey_id
             AND e.di_document_id=p.source_di_document_id
            WHERE p.tenant_id=:t AND p.journey_id=:j AND p.amount > 0
            ORDER BY COALESCE(p.receipt_date, p.payment_at_utc::date) NULLS LAST, p.created_at_utc, p.payment_id
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    stored = {
        str(r["payment_id"]): dict(r) for r in connection.execute(
            text(
                """
                SELECT m.payment_id, m.match_status, m.match_method, bl.source_di_document_id
                FROM auditcore.payment_bank_matches m
                LEFT JOIN auditcore.bank_statement_lines bl
                  ON bl.tenant_id=m.tenant_id AND bl.bank_statement_line_id=m.bank_statement_line_id
                WHERE m.tenant_id=:t AND m.journey_id=:j
                """
            ),
            {"t": tenant_id, "j": journey_id},
        ).mappings().all()
    }

    # The same receipt again: same number, amount and date as an earlier one
    # (same amount and date when neither prints a number).
    eligible = [r for r in rows if r["status_source"] == "EVIDENCE" and r["association_status"] == "ACTIVE"]
    duplicate_of = _duplicate_receipts(eligible)
    number_of = {r["payment_id"]: r["receipt_number"] for r in rows}

    lines = _bank_lines(connection, tenant_id=tenant_id, journey_id=journey_id)
    by_document = {line["documentId"]: line for line in lines}
    items = []
    by_stage: dict[str, Decimal] = {}
    for r in rows:
        payment_id = str(r["payment_id"])
        evidenced = r["status_source"] == "EVIDENCE" and r["association_status"] == "ACTIVE"
        original = duplicate_of.get(r["payment_id"])
        counted = evidenced and original is None
        stage = str(r["payment_stage"] or "BOOKING")
        if counted:
            by_stage[stage] = by_stage.get(stage, Decimal(0)) + Decimal(r["amount"])
        if original is not None:
            reason = "Duplicate: the same receipt number, amount and date as " + (
                f"receipt {number_of.get(original)}" if number_of.get(original) else "an earlier receipt")
        elif not evidenced:
            reason = "Document removed or replaced" if r["association_status"] else "No supporting document"
        else:
            reason = None
        item = {
            "paymentId": payment_id,
            "amount": _money(r["amount"]),
            "receiptNumber": r["receipt_number"],
            "receiptDate": r["receipt_date"] or r["payment_at_utc"],
            "mode": r["mode"],
            "stage": stage,
            "bank": r["receipt_bank_name"],
            "reference": r["payment_reference"],
            "documentId": str(r["source_di_document_id"]) if r["source_di_document_id"] else None,
            "document": _document_label(r["document_type_key"]) if r["document_type_key"] else None,
            "counted": counted,
            "notCountedReason": reason,
            "duplicateOf": str(original) if original is not None else None,
            "bankMatch": None,
            "bank_": None,
        }
        items.append(item)

    # Tie each counted receipt to its statement credit: a match the
    # reconciliation already recorded first, else read straight off the
    # statement entries on file.
    for item in items:
        if not item["counted"]:
            item["bankStatement"] = None
            continue
        if _is_cash(item["mode"]):
            item["bankStatement"] = {"status": "NOT_APPLICABLE", "method": None, "documentId": None,
                                     "date": None, "reference": None, "recorded": False}
            item["bankMatch"] = "NOT_APPLICABLE"
            continue
        if not lines:
            item["bankStatement"] = {"status": "NO_STATEMENT", "method": None, "documentId": None,
                                     "date": None, "reference": None, "recorded": False}
            item["bankMatch"] = "NO_STATEMENT"
            continue
        line, method, recorded = None, "NONE", False
        known = stored.get(item["paymentId"])
        if known and known["match_status"] == "MATCHED" and known["source_di_document_id"] is not None:
            candidate = by_document.get(str(known["source_di_document_id"]))
            if candidate is not None and not candidate["matchedPaymentId"]:
                line, method, recorded = candidate, _BANK_METHOD_LABEL.get(str(known["match_method"]), "REFERENCE"), True
        if line is None:
            line, method = _match_bank_line(item, lines)
        if line is not None:
            line["matchedPaymentId"] = item["paymentId"]
            line["matchedReceipt"] = item["receiptNumber"]
            line["matchMethod"] = method
            status = "MATCHED"
        else:
            status = "AMBIGUOUS" if method == "AMBIGUOUS" else "UNMATCHED"
        item["bankStatement"] = {
            "status": status, "method": method if line is not None else None,
            "documentId": line["documentId"] if line else None, "date": line["date"] if line else None,
            "reference": line["reference"] if line else None, "recorded": recorded,
        }
        item["bankMatch"] = status
    for item in items:
        item.pop("bank_", None)

    receipts = sum((Decimal(i["amount"]) for i in items if i["counted"]), Decimal(0))
    loan = _loan_received(connection, tenant_id=tenant_id, journey_id=journey_id)
    credits = [_dec(line["credit"]) for line in lines if _dec(line["credit"]) is not None]
    matched_credit = [_dec(line["credit"]) for line in lines if line["matchedPaymentId"] and _dec(line["credit"]) is not None]
    return {
        "items": items,
        "receiptsTotal": str(receipts),
        "loanDisbursed": str(loan),
        "paidTotal": str(receipts + loan),
        "byStage": {k: str(v) for k, v in by_stage.items()},
        "duplicates": len(duplicate_of),
        "bankStatement": {
            "lines": lines,
            "creditsTotal": str(sum(credits, Decimal(0))),
            "matchedTotal": str(sum(matched_credit, Decimal(0))),
            "matched": sum(1 for line in lines if line["matchedPaymentId"]),
            "unmatchedCredits": sum(1 for line in lines if _dec(line["credit"]) and not line["matchedPaymentId"]),
            "receiptsWithoutCredit": sum(1 for i in items if (i.get("bankStatement") or {}).get("status") in ("UNMATCHED", "AMBIGUOUS")),
        },
    }


# ── vehicle, registration, delivery ──────────────────────────────────────────
def _document_facts(
    connection: Connection, *, tenant_id: str, journey_id: UUID, di_types: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Every ACTIVE document of the given types, newest first, with its
    fields as the PC left them (corrected values win over extracted ones)."""
    rows = connection.execute(
        text(
            """
            SELECT e.di_document_id, e.document_type_key, e.linked_at_utc, f.field_key, f.effective_value
            FROM auditcore.evidence e
            JOIN auditcore.journey_document_extracted_fields f
              ON f.tenant_id=e.tenant_id AND f.journey_id=e.journey_id AND f.di_document_id=e.di_document_id
            WHERE e.tenant_id=:t AND e.journey_id=:j AND e.association_status='ACTIVE'
              AND e.document_type_key = ANY(:types)
            ORDER BY e.linked_at_utc DESC, e.di_document_id, f.field_key
            """
        ),
        {"t": tenant_id, "j": journey_id, "types": list(di_types)},
    ).mappings().all()
    documents: dict[str, dict[str, Any]] = {}
    for row in rows:
        doc = documents.setdefault(str(row["di_document_id"]), {
            "documentId": str(row["di_document_id"]), "documentType": str(row["document_type_key"]),
            "linkedAtUtc": row["linked_at_utc"], "fields": {},
        })
        value = row["effective_value"]
        if value not in (None, "", "null"):
            doc["fields"][str(row["field_key"])] = value
    return list(documents.values())


def _camel(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(part.capitalize() for part in rest)


def _pick(fields: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {_camel(key): fields.get(key) for key in keys}


_CERTIFICATE_KEYS = (
    "certificate_variant", "certificate_number", "certificate_issue_date", "certificate_valid_until_date",
    "old_vehicle_registration_number", "old_vehicle_make", "old_vehicle_model", "old_vehicle_type",
    "old_vehicle_fuel_type", "old_vehicle_year_of_manufacturing", "original_owner_name", "current_holder_name",
    "trade_number", "trade_date", "scrapping_facility_name", "rvsf_registration_number", "state_of_scrapping",
)
_VALUATION_KEYS = (
    "report_number", "valuation_date", "valuation_valid_until", "evaluator_name", "registration_number",
    "make", "model", "variant", "fuel_type", "manufacture_month_year", "odometer_km", "number_of_owners",
    "overall_grade", "base_market_value", "final_offer_value", "loan_outstanding_on_vehicle",
)


def _identity(fields: dict[str, Any], keys: tuple[str, ...]) -> set[str]:
    """Every number that identifies the document (certificate number, trade
    number, registration...), so two readings that each caught a different
    one still meet on any they share."""
    tokens = set()
    for key in keys:
        token = "".join(c for c in str(fields.get(key) or "").upper() if c.isalnum())
        if token:
            tokens.add(f"{key}:{token}")
    return tokens


def _one_per_document(
    documents: list[dict[str, Any]], *, keys: tuple[str, ...], identity: tuple[str, ...],
) -> list[dict[str, Any]]:
    """One record per real-world document. The same certificate read from two
    uploads (or from two pages of one upload) carries the same number, so
    the readings merge: every field takes the first value any reading
    gave it, and the record names every document it came from."""
    merged: list[dict[str, Any]] = []
    for doc in documents:
        ident = _identity(doc["fields"], identity)
        picked = _pick(doc["fields"], keys)
        target = next((m for m in merged if ident & m["identity"]), None)
        if target is None:
            merged.append({"identity": ident, "documentId": doc["documentId"], "documentIds": [doc["documentId"]], **picked})
            continue
        target["identity"] |= ident
        target["documentIds"].append(doc["documentId"])
        for key, value in picked.items():
            if target.get(key) in (None, "") and value not in (None, ""):
                target[key] = value
    for record in merged:
        record.pop("identity", None)
    return merged


def tradein(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """Trade-in / Scrappage as Phase 1 lays it out: the booking form's
    exchange fields, the trade-in case, every Scrappage Certificate of
    Deposit read on the Journey (once each, however many times it was
    uploaded) and any old-vehicle valuation report."""
    params = {"t": tenant_id, "j": journey_id}
    booking = next(iter(_document_facts(
        connection, tenant_id=tenant_id, journey_id=journey_id, di_types=("booking_form", "booking_docket"),
    )), None)
    booking_fields = booking["fields"] if booking else {}
    case = _rows(connection, """
        SELECT old_vehicle_registration, old_vehicle_make_model, quoted_value, actual_value,
               actual_status_code, handover_at_utc, payment_at_utc, resale_at_utc, source_kind
        FROM auditcore.trade_in_cases WHERE tenant_id=:t AND journey_id=:j
        ORDER BY updated_at_utc DESC LIMIT 1""", params)
    trade_in = case[0] if case else None
    if trade_in:
        trade_in["variance"] = _minus(trade_in["actual_value"], trade_in["quoted_value"])
    certificates = _one_per_document(
        _document_facts(connection, tenant_id=tenant_id, journey_id=journey_id,
                        di_types=("scrappage_certificate_of_deposit",)),
        keys=_CERTIFICATE_KEYS,
        identity=("certificate_number", "trade_number", "old_vehicle_registration_number"),
    )
    valuations = _one_per_document(
        _document_facts(connection, tenant_id=tenant_id, journey_id=journey_id, di_types=("valuation_report",)),
        keys=_VALUATION_KEYS,
        identity=("report_number", "registration_number"),
    )
    applicable = booking_fields.get("exchange_applicable")
    if isinstance(applicable, str):
        applicable = applicable.strip().lower() in ("yes", "true", "y", "1")
    return {
        "exchange": {
            "applicable": applicable if isinstance(applicable, bool) else None,
            "value": _money(booking_fields.get("exchange_value")),
        },
        "tradeIn": trade_in,
        "certificates": certificates,
        "valuations": valuations,
    }


_ADDON_COMPONENTS = {
    "accessories": ("accessories_cost", "essential_kit_amount", "genuine_accessories_amount",
                    "non_genuine_accessories_amount"),
    "warranty": ("additional_warranty_amount", "extended_warranty_amount"),
    "insurance": ("insurance_amount",),
}
# The document that proves each add-on was taken.
_ADDON_DOCUMENTS = {
    "accessories": ("accessory_invoice_dms", "accessory_invoice_tally"),
    "warranty": ("ew_invoice",),
    "insurance": ("insurance_cover",),
}
_ADDON_HINTS = {"accessories": ("ACCESS",), "warranty": ("WARRANTY",), "insurance": ("INSUR",)}
_LINE_CATEGORIES = {
    "accessories": ("ACCESSORY_GENUINE", "ACCESSORY_NON_GENUINE"),
    "warranty": ("EXTENDED_WARRANTY",),
    "insurance": ("INSURANCE",),
}
_ITEM_SOURCES = ("accessory_invoice_dms", "accessory_invoice_tally", "customer_invoice_dms", "tax_invoice_tally",
                 "ew_invoice", "rsa_invoice", "invoice_generic")


def _line_items(documents: list[dict[str, Any]], categories: tuple[str, ...]) -> list[dict[str, Any]]:
    """Invoice line items of the given categories, as printed, one per line."""
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for doc in documents:
        for entry in _line_entries(doc["fields"]):
            if str(entry.get("line_category") or "").upper() not in categories:
                continue
            name = str(entry.get("description_raw") or entry.get("description") or entry.get("item_code") or "").strip()
            amount = _money(entry.get("net_amount") if entry.get("net_amount") is not None else entry.get("gross_amount"))
            key = (name, amount)
            if not name or key in seen:
                continue
            seen.add(key)
            items.append({"name": name, "amount": amount, "quantity": entry.get("quantity"),
                          "itemCode": entry.get("item_code"), "documentId": doc["documentId"]})
    return items


def _taken_addons(
    connection: Connection, *, tenant_id: str, journey_id: UUID, deal_view: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Whether accessories, an extended warranty and insurance were taken with
    the car, for how much, from whom, and the items bought.

    One answer for every tab: an add-on is taken when a document proves it
    (the accessory invoice, the warranty invoice, the cover note), when an
    invoice lists items of its kind, or when any source prices it above
    zero. The amount is what was billed for it; the booking form's figure
    stands only until an invoice or cover note is read, and its zero never
    hides a policy that exists.
    """
    params = {"t": tenant_id, "j": journey_id}
    by_key = {r["key"]: r for g in deal_view["categories"] for r in g["components"]}
    documents = _document_facts(connection, tenant_id=tenant_id, journey_id=journey_id,
                                di_types=_ITEM_SOURCES + ("insurance_cover",))
    invoices = [d for d in documents if d["documentType"] != "insurance_cover"]
    plans = {str(r["addon_type_code"]).upper(): r for r in _rows(connection, """
        SELECT addon_type_code, provider_name, actual_amount, reference_number FROM auditcore.journey_addons
        WHERE tenant_id=:t AND journey_id=:j""", params)}
    insurer = _rows(connection, """
        SELECT insurer_name, policy_reference, cover_note_reference, insurance_by, agent_intermediary_name,
               agent_intermediary_code, misp_code, standard_premium_amount, actual_premium_amount, add_ons
        FROM auditcore.insurance_records
        WHERE tenant_id=:t AND journey_id=:j ORDER BY updated_at_utc DESC LIMIT 1""", params)
    cover = next((d for d in documents if d["documentType"] == "insurance_cover"), None)
    cover_fields = cover["fields"] if cover else {}
    insurance_row = insurer[0] if insurer else {}
    ew = next(iter(d for d in invoices if d["documentType"] == "ew_invoice"), None)
    ew_fields = ew["fields"] if ew else {}
    details: dict[str, dict[str, Any]] = {
        "accessories": {
            "invoiceNumbers": sorted({str(d["fields"].get("invoice_number")) for d in invoices
                                      if d["documentType"].startswith("accessory_invoice") and d["fields"].get("invoice_number")}),
        },
        "insurance": {
            "insurerName": insurance_row.get("insurer_name") or cover_fields.get("insurer_name"),
            "policyNumber": insurance_row.get("policy_reference") or cover_fields.get("policy_number"),
            "policyType": cover_fields.get("policy_type"),
            "coverNoteReference": insurance_row.get("cover_note_reference"),
            "insuranceBy": insurance_row.get("insurance_by"),
            "policyStartDate": cover_fields.get("policy_start_date"),
            "policyEndDate": cover_fields.get("policy_end_date"),
            "issueDate": cover_fields.get("issue_date"),
            "idvAmount": _money(cover_fields.get("idv_amount")),
            "standardPremium": _money(insurance_row.get("standard_premium_amount")),
            "actualPremium": _money(cover_fields.get("premium_amount") or insurance_row.get("actual_premium_amount")),
            "addOns": cover_fields.get("add_ons") or insurance_row.get("add_ons"),
            "agentName": insurance_row.get("agent_intermediary_name") or cover_fields.get("agent_intermediary_name"),
            "agentCode": insurance_row.get("agent_intermediary_code") or cover_fields.get("agent_intermediary_code"),
            "mispCode": insurance_row.get("misp_code") or cover_fields.get("misp_code"),
        },
        "warranty": {
            "planName": ew_fields.get("plan_name"),
            "providerName": ew_fields.get("seller_name"),
            "invoiceNumber": ew_fields.get("invoice_number"),
            "invoiceDate": ew_fields.get("invoice_date"),
            "coverageStartDate": ew_fields.get("coverage_start_date"),
            "coverageEndDate": ew_fields.get("coverage_end_date"),
            "tenureMonths": ew_fields.get("tenure_months"),
        },
    }
    out: dict[str, dict[str, Any]] = {}
    for kind, components in _ADDON_COMPONENTS.items():
        rows = [by_key[c] for c in components if c in by_key]
        billed = [_dec(r["billed"]) for r in rows if r["billed"] is not None]
        booking = [_dec(r["booking"]) for r in rows if r["booking"] is not None]
        proof = [d for d in invoices if d["documentType"] in _ADDON_DOCUMENTS[kind]]
        if kind == "insurance" and cover:
            proof = [cover]
        items = _line_items(invoices, _LINE_CATEGORIES[kind])
        plan = next((plans[code] for code in plans if any(hint in code for hint in _ADDON_HINTS[kind])), None)
        amount: Decimal | None
        source: str | None
        if billed:
            amount, source = sum(billed, Decimal(0)), "billed"
        elif kind == "insurance" and _dec(insurance_row.get("actual_premium_amount")) is not None:
            amount, source = _dec(insurance_row.get("actual_premium_amount")), "billed"
        elif plan is not None and _dec(plan["actual_amount"]) is not None:
            amount, source = _dec(plan["actual_amount"]), "record"
        elif booking and sum(booking, Decimal(0)) > 0:
            amount, source = sum(booking, Decimal(0)), "booking"
        else:
            amount, source = None, None
        provider = (plan or {}).get("provider_name")
        if kind == "insurance":
            provider = provider or details["insurance"]["insurerName"]
        if kind == "warranty":
            provider = provider or details["warranty"]["providerName"]
        out[kind] = {
            "taken": bool(proof or items or (amount is not None and amount > 0) or (kind == "insurance" and insurer)),
            "amount": str(amount) if amount is not None else None,
            "amountSource": source,
            "provider": provider,
            "items": items,
            "details": {k: v for k, v in details[kind].items() if v not in (None, "", [])},
            "documentIds": [d["documentId"] for d in proof],
        }
    return out


def vehicle(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    params = {"t": tenant_id, "j": journey_id}
    product = connection.execute(
        text(
            """
            SELECT jp.model_name_snapshot AS model, jp.variant_name_snapshot AS variant,
                   jp.colour_name_snapshot AS colour, jp.model_code_snapshot AS model_code,
                   jp.variant_code_snapshot AS variant_code, jp.colour_code_snapshot AS colour_code,
                   jp.selection_status, jp.selection_method, jp.selection_score,
                   jp.sku_resolution_remarks, sku.sku_code
            FROM auditcore.journey_products jp
            LEFT JOIN auditcore.product_skus sku ON sku.product_sku_id=jp.product_sku_id
            WHERE jp.tenant_id=:t AND jp.journey_id=:j
            """
        ),
        params,
    ).mappings().first()
    units = _rows(connection, """
        SELECT vin, chassis_number, dms_reference, invoice_reference, allocated_at_utc, source_kind
        FROM auditcore.vehicle_records WHERE tenant_id=:t AND journey_id=:j
        ORDER BY updated_at_utc DESC""", params)
    photo_count = connection.execute(
        text(
            """
            SELECT COUNT(*) FROM auditcore.delivery_vehicle_photos
            WHERE tenant_id=:t AND journey_id=:j AND deleted_at_utc IS NULL
            """
        ),
        params,
    ).scalar_one()

    # Phase 1's Vehicle panel: what was taken with the car (accessories,
    # extended warranty, insurance) with the items bought -- the same answer
    # the Add-ons tab gives -- the booking facts and how the delivery went.
    deal_view = deal(connection, tenant_id=tenant_id, journey_id=journey_id)
    addons = _taken_addons(connection, tenant_id=tenant_id, journey_id=journey_id, deal_view=deal_view)

    booking_doc = next(iter(_document_facts(
        connection, tenant_id=tenant_id, journey_id=journey_id, di_types=("booking_form", "booking_docket"),
    )), None)
    form = booking_doc["fields"] if booking_doc else {}
    booking_row = connection.execute(
        text(
            """
            SELECT b.booking_reference, b.booking_confirmation_date, b.deal_type_code, b.deal_source_code,
                   b.lead_source_code, b.expected_delivery_text, b.expected_delivery_date,
                   st.display_name AS sales_staff_name, o.outlet_name, o.address_text AS outlet_address
            FROM auditcore.bookings b
            LEFT JOIN auditcore.dealership_staff st
              ON st.tenant_id=b.tenant_id AND st.dealership_staff_id=b.sales_staff_id
            LEFT JOIN auditcore.journeys jn ON jn.tenant_id=b.tenant_id AND jn.journey_id=b.journey_id
            LEFT JOIN auditcore.dealer_outlets o
              ON o.tenant_id=jn.tenant_id AND o.outlet_id=jn.outlet_id
            WHERE b.tenant_id=:t AND b.journey_id=:j
            ORDER BY b.updated_at_utc DESC LIMIT 1
            """
        ),
        params,
    ).mappings().first()
    b = dict(booking_row) if booking_row else {}
    booking = {
        "bookingReference": form.get("booking_reference_number") or b.get("booking_reference"),
        "bookingDate": form.get("booking_date") or (b["booking_confirmation_date"].isoformat() if b.get("booking_confirmation_date") else None),
        "salesConsultant": form.get("sales_person") or b.get("sales_staff_name"),
        "dealerBranch": form.get("dealer_branch") or b.get("outlet_address") or b.get("outlet_name"),
        "dealType": b.get("deal_type_code"),
        "dealSource": b.get("deal_source_code"),
        "leadSource": b.get("lead_source_code"),
        "expectedDelivery": form.get("expected_delivery_date") or form.get("expected_delivery")
        or (b["expected_delivery_date"].isoformat() if b.get("expected_delivery_date") else None)
        or b.get("expected_delivery_text"),
    }
    delivered = _rows(connection, """
        SELECT actual_delivery_status_code, status_label_snapshot, planned_delivery_at, delivery_intimated_at,
               actual_delivered_at
        FROM auditcore.deliveries WHERE tenant_id=:t AND journey_id=:j ORDER BY updated_at_utc DESC LIMIT 1""",
        params)
    # What the Journey 360 header strip used to carry (removed 2026-09-30 as
    # a duplicate): the registration, financier and insurer on file, and
    # when the journey started. Same sources as the summary.
    head = connection.execute(
        text(
            """
            SELECT j.created_at_utc,
                   (SELECT r.registration_number FROM auditcore.registration_records r
                     WHERE r.tenant_id=j.tenant_id AND r.journey_id=j.journey_id
                       AND r.registration_number IS NOT NULL
                     ORDER BY r.updated_at_utc DESC LIMIT 1) AS registration_number,
                   (SELECT f.provider_name FROM auditcore.finance_records f
                     WHERE f.tenant_id=j.tenant_id AND f.journey_id=j.journey_id
                     ORDER BY f.updated_at_utc DESC LIMIT 1) AS financier,
                   (SELECT i.insurer_name FROM auditcore.insurance_records i
                     WHERE i.tenant_id=j.tenant_id AND i.journey_id=j.journey_id
                     ORDER BY i.updated_at_utc DESC LIMIT 1) AS insurer
            FROM auditcore.journeys j
            WHERE j.tenant_id=:t AND j.journey_id=:j
            """
        ),
        params,
    ).mappings().one()
    return {
        "product": _plain(product) if product else None, "units": units, "photoCount": int(photo_count),
        "addons": addons, "booking": booking, "delivery": delivered[0] if delivered else None,
        "journey": {
            "startedAtUtc": head["created_at_utc"],
            "registrationNumber": head["registration_number"],
            "financier": head["financier"],
            "insurer": head["insurer"],
        },
    }


_CUSTOMER_FIELDS = (
    # (key, label, semantic key of the KYC-reviewed value, or None for a
    # value that only the customer record holds)
    ("enteredName", "Entered name", None),
    ("legalName", "Legal / KYC name", "customer_name"),
    ("pan", "PAN", "pan"),
    ("aadhaar", "Aadhaar", "aadhaar_number"),
    ("dateOfBirth", "Date of birth", "customer_date_of_birth"),
    ("gender", "Gender", "customer_gender"),
    ("mobile", "Mobile", "customer_phone"),
    ("email", "Email", "customer_email"),
    ("address", "Address", "customer_address"),
    ("pincode", "Pincode", "pincode"),
    ("state", "State", "kyc_state"),
    ("district", "District", "kyc_district"),
    ("relationship", "Relationship", "customer_relationship_type"),
    ("relationshipName", "Relationship name", "customer_relationship_name"),
    ("customerType", "Customer type", None),
    ("identityStatus", "Identity status", None),
)


def _masked_phone(value: Any) -> str | None:
    digits = "".join(c for c in str(value or "") if c.isdigit())
    return f"******{digits[-4:]}" if len(digits) >= 4 else None


def customer(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """The customer as the KYC documents establish them, in Phase 1's
    Customer panel layout: what was entered at booking, what the PAN and
    Aadhaar say (the source of truth once reviewed), and how to reach them.
    Phone numbers are masked to their last four digits, as everywhere else."""
    from audit_core.uc03_journey_reviewed_details import (
        KYC_DOCUMENT_TYPES,
        annotate_and_resolve_reviewed_fields,
        load_reviewed_field_details,
        mask_contact_fields,
    )

    record = connection.execute(
        text(
            """
            SELECT c.display_name, c.legal_name, c.legal_name_status, c.mobile_number, c.mobile_last4,
                   c.email_reference, c.customer_type_code, c.relationship_type, c.relationship_name
            FROM auditcore.journeys j
            JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
            WHERE j.tenant_id=:t AND j.journey_id=:j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().first()
    rows = load_reviewed_field_details(connection, tenant_id=tenant_id, journey_id=journey_id)
    fields, resolved = annotate_and_resolve_reviewed_fields(rows)
    mask_contact_fields(fields, resolved, full_contact=False)

    def resolved_value(semantic: str) -> tuple[Any, str | None]:
        item = resolved.get(semantic) or {}
        value = item.get("value")
        return (None, None) if value in (None, "") else (value, item.get("documentTypeKey"))

    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for key, _, semantic in _CUSTOMER_FIELDS:
        if semantic is None:
            continue
        value, source = resolved_value(semantic)
        if value is not None:
            values[key] = value
            if source:
                sources[key] = str(source)
    kyc_named = str(sources.get("legalName") or "").casefold() in KYC_DOCUMENT_TYPES
    if record is not None:
        values["enteredName"] = record["display_name"]
        if not kyc_named and record["legal_name"]:
            values["legalName"] = record["legal_name"]
        values.setdefault("mobile", _masked_phone(record["mobile_number"] or record["mobile_last4"]))
        values.setdefault("email", record["email_reference"])
        values.setdefault("relationship", record["relationship_type"])
        values.setdefault("relationshipName", record["relationship_name"])
        values["customerType"] = record["customer_type_code"]
    identity = (
        "DOCUMENT_VERIFIED" if kyc_named
        else "VERIFIED" if record is not None and record["legal_name_status"] == "VERIFIED"
        else "CONFLICT" if record is not None and record["legal_name_status"] == "CONFLICT"
        else "PENDING"
    )
    values["identityStatus"] = identity
    return {
        "fields": [
            {"key": key, "label": label, "value": values.get(key), "source": sources.get(key)}
            for key, label, _ in _CUSTOMER_FIELDS
        ],
        "identityStatus": identity,
        "kycDocuments": sorted({str(v) for v in sources.values() if str(v).casefold() in KYC_DOCUMENT_TYPES}),
    }


_INVOICE_TYPES = (
    "customer_invoice_dms", "tax_invoice_tally", "accessory_invoice_dms", "accessory_invoice_tally",
    "ew_invoice", "rsa_invoice", "wholesale_invoice", "invoice_generic", "credit_note", "debit_note",
)
_INVOICE_HEADER_KEYS = (
    "invoice_number", "invoice_date", "invoice_nature", "invoice_purpose", "source_system", "seller_name",
    "seller_gstin", "buyer_name", "buyer_gstin", "financed_by", "model_name_raw", "variant_raw", "vin_number",
    "chassis_number", "engine_number", "vehicle_registration_number", "plan_name", "coverage_start_date",
    "coverage_end_date", "tenure_months", "narration",
)
_INVOICE_TOTAL_KEYS = (
    "gross_amount_before_discount", "invoice_discount_amount", "taxable_amount", "cgst_amount", "sgst_amount",
    "igst_amount", "cess_amount", "tcs_amount", "round_off_amount", "grand_total_amount",
)
# A debit note prints its own field names; read them as an invoice's.
_DEBIT_NOTE_ALIASES = {
    "debit_note_number": "invoice_number", "debit_note_date": "invoice_date", "dealer_name": "seller_name",
    "customer_name": "buyer_name", "total_amount": "grand_total_amount",
}


def invoices(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """Every invoice read on the Journey (vehicle, accessories, extended
    warranty, RSA, wholesale, credit and debit notes) with its header, its
    totals and every line item as printed."""
    registry = get_registry()
    documents = []
    for doc in _document_facts(connection, tenant_id=tenant_id, journey_id=journey_id, di_types=_INVOICE_TYPES):
        fields = {**doc["fields"]}
        for source, target in _DEBIT_NOTE_ALIASES.items():
            if source in fields and target not in fields:
                fields[target] = fields[source]
        template = registry.template_for_di_type(doc["documentType"], stage=None)
        raw = fields.get("line_items")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = None
        lines = []
        for entry in raw if isinstance(raw, list) else []:
            if not isinstance(entry, dict):
                continue
            lines.append({
                "description": entry.get("description_raw") or entry.get("description"),
                "category": entry.get("line_category"),
                "itemCode": entry.get("item_code"),
                "hsnSac": entry.get("hsn_sac"),
                "quantity": entry.get("quantity"),
                "unitRate": _money(entry.get("unit_rate")),
                "grossAmount": _money(entry.get("gross_amount")),
                "discountAmount": _money(entry.get("discount_amount")),
                "taxableAmount": _money(entry.get("taxable_amount")),
                "taxRate": entry.get("tax_rate"),
                "taxAmount": _money(entry.get("tax_amount")),
                "netAmount": _money(entry.get("net_amount")),
            })
        documents.append({
            "documentId": doc["documentId"],
            "documentType": doc["documentType"],
            "label": template.display_name,
            "linkedAtUtc": doc["linkedAtUtc"],
            "header": {_camel(k): fields.get(k) for k in _INVOICE_HEADER_KEYS if fields.get(k) not in (None, "")},
            "totals": {_camel(k): _money(fields.get(k)) for k in _INVOICE_TOTAL_KEYS if fields.get(k) not in (None, "")},
            "particulars": fields.get("particulars"),
            "lineItems": lines,
        })
    order = {t: i for i, t in enumerate(_INVOICE_TYPES)}
    documents.sort(key=lambda d: (order.get(d["documentType"], 99), str(d["linkedAtUtc"] or "")))
    return {"documents": documents, "count": len(documents)}


def registration(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    records = _rows(connection, """
        SELECT registration_number, registration_state, registration_territory,
               registration_district, registration_type_code, registration_category_code,
               registration_by, actual_status_code
        FROM auditcore.registration_records WHERE tenant_id=:t AND journey_id=:j
        ORDER BY updated_at_utc DESC""", {"t": tenant_id, "j": journey_id})
    deal_view = deal(connection, tenant_id=tenant_id, journey_id=journey_id)
    charges = next((g for g in deal_view["categories"] if g["code"] == "REGISTRATION"), None)
    return {"records": records, "charges": charges}


def delivery(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    stage = read_booking_stage(connection, tenant_id=tenant_id, journey_id=journey_id)
    records = _rows(connection, """
        SELECT planned_delivery_at, delivery_intimated_at, actual_delivered_at,
               actual_delivery_status_code, status_label_snapshot
        FROM auditcore.deliveries WHERE tenant_id=:t AND journey_id=:j
        ORDER BY updated_at_utc DESC""", {"t": tenant_id, "j": journey_id})
    return {"readiness": stage.get("delivery"), "records": records}


# ── compliance and activity ──────────────────────────────────────────────────
_STATUS_ORDER = {
    "FAIL": 0, "ERROR_TERMINAL": 1, "RETRY_PENDING": 2, "WAITING_FOR_FACTS": 3, "PASS": 4, "NOT_APPLICABLE": 5,
}


_DERIVED_SOURCES = {"derived": "computed", "deal_reconciliation": "price masters", "masters": "price masters",
                    "journey": "journey", "payments": "payments"}


def _operand_label(operand: str) -> str | None:
    document, _, field = operand.partition(".")
    if not field:
        return None
    field = field.replace(":", "_").lower()
    document = document.strip("_")
    source = _DERIVED_SOURCES.get(document) or _document_label(document)
    return f"{field_label(field)} ({source})"


def control_labels(connection: Connection) -> dict[str, str]:
    """Human names: the rule catalog title for Audit Core rules, else what a
    Rule Engine control compares ("X (doc) vs Y (doc)"), else the code."""
    titles = {
        str(code): str(title)
        for code, title in connection.execute(
            text("SELECT rule_code, title FROM auditcore.rule_definitions WHERE title IS NOT NULL")
        ).all()
    }
    labels: dict[str, str] = {}
    for control in get_registry().controls.values():
        label = titles.get(control.code)
        operands = control.operands or {}
        if not label and isinstance(operands.get("left"), str) and isinstance(operands.get("right"), str):
            left, right = _operand_label(operands["left"]), _operand_label(operands["right"])
            if left and right:
                label = f"{left} vs {right}"
        labels[control.code] = label or control.code.replace("_", " ").capitalize()
    return labels


def compliance(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """Every control with its state and reason, grouped by stage."""
    registry = get_registry()
    labels = control_labels(connection)
    rows = connection.execute(
        text(
            """
            SELECT control_code, control_status, status_reason, details, last_evaluated_at_utc
            FROM auditcore.p2_control_state WHERE tenant_id=:t AND journey_id=:j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    state = {str(r["control_code"]): r for r in rows}
    groups: dict[str, list[dict[str, Any]]] = {"BOOKING": [], "DELIVERY": []}
    for control in registry.controls.values():
        row = state.get(control.code)
        details = (row["details"] if row else None) or {}
        item = {
            "code": control.code,
            "label": labels[control.code],
            "category": control.category,
            "severity": control.severity,
            "executor": control.executor,
            "status": str(row["control_status"]) if row else "WAITING_FOR_FACTS",
            "reason": (row["status_reason"] if row else None) or "Waiting for the documents this check needs.",
            "evaluatedAtUtc": row["last_evaluated_at_utc"] if row else None,
            "leftValue": details.get("leftValue"),
            "rightValue": details.get("rightValue"),
        }
        for stage, items in groups.items():
            if control.applies_to_stage(stage):
                items.append(item)
    for items in groups.values():
        items.sort(key=lambda i: (_STATUS_ORDER.get(i["status"], 9), i["label"]))
    return {"stages": groups, "statistics": control_statistics(connection, tenant_id=tenant_id, journey_id=journey_id)}


def activity(connection: Connection, *, tenant_id: str, journey_id: UUID, limit: int = 80) -> dict[str, Any]:
    rows = connection.execute(
        text(
            """
            SELECT event_id, event_type, subject_type, subject_id, details, created_at_utc
            FROM auditcore.p2_activity_events
            WHERE tenant_id=:t AND journey_id=:j AND event_type <> 'FACTS_CHANGED'
            ORDER BY event_id DESC LIMIT :limit
            """
        ),
        {"t": tenant_id, "j": journey_id, "limit": limit},
    ).mappings().all()
    return {"events": [_plain(r) for r in rows]}


# ── duplicates and the compliance report ─────────────────────────────────────
def duplicates(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """Duplicate-booking pairs touching this Journey, in both directions:
    this booking looks like a duplicate of an earlier one, or an earlier
    booking held here has a later look-alike."""
    rows = connection.execute(
        text(
            """
            SELECT f.audit_finding_id, f.journey_id, f.severity, f.finding_status, f.created_at_utc,
                   f.title, f.description, ev.safe_payload
            FROM auditcore.audit_findings f
            LEFT JOIN LATERAL (
              SELECT safe_payload FROM auditcore.audit_finding_events e
              WHERE e.tenant_id=f.tenant_id AND e.audit_finding_id=f.audit_finding_id
              ORDER BY e.occurred_at_utc LIMIT 1
            ) ev ON true
            WHERE f.tenant_id=:t AND f.finding_type_code='DUPLICATE_BOOKING'
              AND f.finding_status IN ('OPEN','ACKNOWLEDGED')
              AND (f.journey_id=:j OR ev.safe_payload->>'believedOriginalJourneyId' = CAST(:j AS text))
            ORDER BY f.created_at_utc DESC
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    other_ids = set()
    for r in rows:
        payload = r["safe_payload"] or {}
        other_ids.add(str(payload.get("believedOriginalJourneyId")) if str(r["journey_id"]) == str(journey_id)
                      else str(r["journey_id"]))
    others: dict[str, dict[str, Any]] = {}
    if other_ids:
        for o in connection.execute(
            text(
                """
                SELECT j.journey_id, j.journey_reference, j.created_at_utc,
                       COALESCE(c.legal_name, c.display_name) AS customer_name, o.outlet_name,
                       NULLIF(concat_ws(' · ', jp.model_name_snapshot, jp.variant_name_snapshot), '') AS vehicle
                FROM auditcore.journeys j
                JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
                JOIN auditcore.dealer_outlets o ON o.tenant_id=j.tenant_id AND o.outlet_id=j.outlet_id
                LEFT JOIN auditcore.journey_products jp ON jp.tenant_id=j.tenant_id AND jp.journey_id=j.journey_id
                WHERE j.tenant_id=:t AND j.journey_id = ANY(CAST(:ids AS uuid[]))
                """
            ),
            {"t": tenant_id, "ids": sorted(i for i in other_ids if i and i != "None")},
        ).mappings().all():
            others[str(o["journey_id"])] = _plain(o)
    pairs = []
    for r in rows:
        payload = r["safe_payload"] or {}
        this_is_duplicate = str(r["journey_id"]) == str(journey_id)
        other = str(payload.get("believedOriginalJourneyId")) if this_is_duplicate else str(r["journey_id"])
        pairs.append({
            "findingId": str(r["audit_finding_id"]),
            "role": "THIS_IS_DUPLICATE" if this_is_duplicate else "THIS_HOLDS_BOOKING",
            "severity": r["severity"],
            "status": r["finding_status"],
            "raisedAtUtc": r["created_at_utc"],
            "matchBasis": payload.get("matchBasis"),
            "matchBasisLabel": _BASIS_LABEL.get(str(payload.get("matchBasis") or ""), payload.get("matchBasis")),
            "matchConfidencePercent": payload.get("matchConfidencePercent"),
            "matchConfidenceLabel": payload.get("matchConfidenceLabel"),
            "originalityBasis": payload.get("originalityBasis"),
            "otherJourney": others.get(other) or {"journey_id": other},
        })
    return {"pairs": pairs}


_BLOCKING_STATES = {"FAIL"}
_INCOMPLETE_STATES = {"WAITING_FOR_FACTS", "RETRY_PENDING", "ERROR_TERMINAL"}


def compliance_report(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """The legacy compliance report, extended with the Phase 2 control ledger,
    the deal variance, document readiness, duplicate pairs and open tasks,
    plus one verdict a reviewer can sign off against."""
    from audit_core.uc03_compliance_report import build_compliance_report

    legacy = build_compliance_report(connection, tenant_id=tenant_id, journey_id=journey_id).model_dump(mode="json")
    ledger = compliance(connection, tenant_id=tenant_id, journey_id=journey_id)
    deal_view = deal(connection, tenant_id=tenant_id, journey_id=journey_id)
    stage = read_booking_stage(connection, tenant_id=tenant_id, journey_id=journey_id)
    tasks = _rows(connection, f"""
        SELECT task_id, title, task_type, priority, assigned_role_code, task_status, due_at_utc, created_at_utc
        FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j
          AND task_status NOT IN {_OPEN_TASK_EXCLUDED}
        ORDER BY CASE priority WHEN 'URGENT' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'NORMAL' THEN 2 ELSE 3 END,
                 due_at_utc NULLS LAST""", {"t": tenant_id, "j": journey_id})
    dupes = duplicates(connection, tenant_id=tenant_id, journey_id=journey_id)

    active_stages = ["BOOKING"] + (["DELIVERY"] if str(stage.get("stage", "")).startswith("DELIVERY") else [])
    considered = [c for s in active_stages for c in ledger["stages"][s]]
    seen: set[str] = set()
    unique = [c for c in considered if not (c["code"] in seen or seen.add(c["code"]))]
    failed = [c for c in unique if c["status"] in _BLOCKING_STATES]
    incomplete = [c for c in unique if c["status"] in _INCOMPLETE_STATES]
    gates = stage.get("gates") or {}
    missing_documents = [g for g in gates.values() if not g.get("passed") and g.get("kind") == "DOCUMENT_READY"]
    high_open = int(legacy["summary"]["highOrCriticalOpen"])
    if failed or high_open or dupes["pairs"]:
        verdict, verdict_label = "NON_COMPLIANT", "Issues found"
    elif incomplete or missing_documents or tasks:
        verdict, verdict_label = "INCOMPLETE", "Audit not complete"
    else:
        verdict, verdict_label = "COMPLIANT", "Compliant"
    flagged_lines = [
        {"label": r["label"], "category": g["label"], "standard": r["standard"], "booking": r["booking"],
         "billed": r["billed"], "ledger": r["ledger"], "variance": r["variance"], "flags": r["flags"]}
        for g in deal_view["categories"] for r in g["components"] if r["flags"]
    ] + [
        {"label": d["label"], "category": "Discounts", "standard": d["entitled"], "booking": d["booking"],
         "billed": d["billed"], "ledger": d["ledger"], "variance": d["variance"], "flags": d["flags"]}
        for d in deal_view["discounts"] if d["flags"]
    ]
    # Anyone who can open the Journey may print the report; until the Team
    # Lead has reviewed the completed delivery it is a draft and says so.
    reviewed = connection.execute(
        text(
            """
            SELECT j.review_completed_at_utc,
                   (SELECT e.actor_role_code FROM auditcore.p2_task_events e
                      JOIN auditcore.p2_tasks t ON t.tenant_id=e.tenant_id AND t.task_id=e.task_id
                     WHERE e.tenant_id=j.tenant_id AND e.journey_id=j.journey_id
                       AND t.task_type='DELIVERY_REVIEW' AND e.event_type='COMPLETE_ACTION'
                     ORDER BY e.created_at_utc DESC LIMIT 1) AS reviewer_role
            FROM auditcore.journeys j WHERE j.tenant_id=:t AND j.journey_id=:j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one_or_none()
    reviewed_at = reviewed["review_completed_at_utc"] if reviewed else None
    review = {
        "status": "REVIEWED" if reviewed_at else "DRAFT",
        "reviewedAtUtc": reviewed_at,
        "reviewerRole": (reviewed["reviewer_role"] if reviewed else None) or ("TL" if reviewed_at else None),
        "label": (f"Reviewed by the {'Team Lead' if (reviewed or {}).get('reviewer_role') in (None, 'TL') else 'Project Manager'}"
                  if reviewed_at else "Draft: the audit is in progress and the delivery has not been reviewed"),
    }
    return {
        **legacy,
        "review": review,
        "verdict": {"code": verdict, "label": verdict_label,
                    "failedControls": len(failed), "incompleteControls": len(incomplete),
                    "openTasks": len(tasks), "duplicatePairs": len(dupes["pairs"]),
                    "highOrCriticalFindings": high_open},
        "stage": {"code": stage.get("stage"), "gates": gates, "delivery": stage.get("delivery")},
        "controls": {s: ledger["stages"][s] for s in active_stages},
        "controlStatistics": ledger["statistics"],
        "deal": {"summary": deal_view["summary"], "sku": deal_view["sku"], "flaggedLines": flagged_lines},
        "duplicates": dupes["pairs"],
        "openTasks": tasks,
    }


def _timeline(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    from audit_core.uc03_p2_workflow import timeline

    return timeline(connection, tenant_id=tenant_id, journey_id=journey_id)


# ── audit trail: what happened, who did it, how long each step took ─────────
_ACTIVITY_KIND = {
    "JOURNEY_STARTED": "journey",
    "UPLOAD_INITIALIZED": "document", "UPLOAD_ACCEPTED": "document", "UPLOAD_SPLIT_COMPLETE": "document",
    "UPLOAD_DUPLICATE": "document", "P2_WORK_DEAD_LETTER": "document", "JOURNEY_CANCELLED": "journey",
    "DOCUMENTS_GROUPED": "document", "DOCUMENT_READY": "document", "DOCUMENT_SETTLED": "document",
    "DOCUMENT_REPLACEMENT_INITIALIZED": "document", "DOCUMENT_REPLACED": "document",
    "DOCUMENT_REMOVED": "document", "DOCUMENT_VOIDED": "document",
    "PAGE_RETRY_REQUESTED": "document", "PAGE_RETYPED": "document",
    "VEHICLE_PHOTO_ADDED": "document", "VEHICLE_PHOTO_REMOVED": "document",
    "FIELD_CORRECTED": "review", "FIELD_CONFIRMED": "review", "RECHECK_REQUESTED": "review",
    "PRICING_DATE_CHANGED": "review",
    "STAGE_CHANGED": "stage",
    "CONTROL_CHANGED": "check",
    "TASK_RAISED": "task", "TASK_VERIFIED": "task",
}

_TASK_CLOSED = ("VERIFIED_COMPLETE", "CANCELLED", "FAILED", "DEAD_LETTER")


def audit(connection: Connection, *, tenant_id: str, journey_id: UUID, limit: int = 300) -> dict[str, Any]:
    """The journey's complete history for a reviewer: the milestones with the
    time between them, every task with when it opened and closed, and one
    ordered stream of everything that happened (uploads, reads, reviews,
    stage changes, task events, checks) with who did it."""
    journey = connection.execute(
        text(
            """
            SELECT j.created_at_utc, j.created_by_display_name, j.review_completed_at_utc,
                   dl.actual_delivered_at,
                   (SELECT MIN(b.created_at_utc) FROM auditcore.p2_upload_batches b
                     WHERE b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id) AS first_upload_at,
                   (SELECT MIN(e.created_at_utc) FROM auditcore.p2_activity_events e
                     WHERE e.tenant_id=j.tenant_id AND e.journey_id=j.journey_id
                       AND e.event_type='DOCUMENT_READY') AS first_document_read_at
            FROM auditcore.journeys j
            LEFT JOIN auditcore.deliveries dl ON dl.tenant_id=j.tenant_id AND dl.journey_id=j.journey_id
            WHERE j.tenant_id=:t AND j.journey_id=:j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    stages = {
        str(r["stage_code"]): dict(r)
        for r in connection.execute(
            text(
                """
                SELECT stage_code, business_status, closure_disposition, first_started_at_utc,
                       capture_completed_at_utc, business_completed_at_utc, booking_confirmed_at_utc
                FROM auditcore.journey_stage_states WHERE tenant_id=:t AND journey_id=:j
                """
            ),
            {"t": tenant_id, "j": journey_id},
        ).mappings().all()
    }
    booking = stages.get("BOOKING") or {}
    delivery = stages.get("DELIVERY") or {}
    cancelled = booking.get("business_status") in ("BOOKING_CANCELLED", "DUPLICATE_BOOKING") \
        or booking.get("closure_disposition") == "NO_DELIVERY"

    pc = journey["created_by_display_name"]
    candidates: list[tuple[str, str, Any, str | None]] = [
        ("JOURNEY_STARTED", "Journey started", journey["created_at_utc"], f"PC {pc}" if pc else "PC"),
        ("FIRST_UPLOAD", "First document uploaded", journey["first_upload_at"], "PC"),
        ("FIRST_DOCUMENT_READ", "First document read", journey["first_document_read_at"], "Automatic"),
        ("BOOKING_SUBMITTED", "Booking documents submitted", booking.get("capture_completed_at_utc"), "PC"),
        ("BOOKING_CONFIRMED", "Booking confirmed", booking.get("booking_confirmed_at_utc"), None),
        ("BOOKING_CLOSED", "Booking cancelled" if cancelled else "Booking complete",
         booking.get("business_completed_at_utc"), None),
        ("DELIVERY_STARTED", "Delivery started", delivery.get("first_started_at_utc"), "PC"),
        ("DELIVERY_SUBMITTED", "Delivery documents submitted", delivery.get("capture_completed_at_utc"), "PC"),
        ("GATE_PASS", "Gate pass: vehicle delivered", journey["actual_delivered_at"], None),
        ("DELIVERY_CLOSED", "Delivery complete", delivery.get("business_completed_at_utc"), None),
        ("TL_REVIEWED", "Reviewed by Team Lead", journey["review_completed_at_utc"], "TL"),
    ]
    reached = sorted((c for c in candidates if c[2] is not None), key=lambda c: c[2])
    milestones = []
    previous = None
    for key, label, at, who in reached:
        hours = round((at - previous).total_seconds() / 3600, 1) if previous is not None else None
        milestones.append({"key": key, "label": label, "atUtc": at, "who": who, "hoursSincePrevious": hours})
        previous = at
    pending = [{"key": key, "label": label} for key, label, at, _ in candidates if at is None
               and not (cancelled and key in ("DELIVERY_STARTED", "DELIVERY_SUBMITTED", "GATE_PASS",
                                              "DELIVERY_CLOSED", "TL_REVIEWED"))]

    task_rows = connection.execute(
        text(
            """
            SELECT task_id, title, category, task_type, assigned_role_code, raised_by_role_code, origin_kind,
                   severity, task_status, created_at_utc, verified_at_utc, updated_at_utc,
                   CASE WHEN task_status IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')
                        THEN COALESCE(verified_at_utc, updated_at_utc) END AS closed_at_utc
            FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j
            ORDER BY created_at_utc
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    tasks = []
    closed_hours: list[float] = []
    for r in task_rows:
        hours = round((r["closed_at_utc"] - r["created_at_utc"]).total_seconds() / 3600, 1) \
            if r["closed_at_utc"] is not None else None
        if hours is not None:
            closed_hours.append(hours)
        tasks.append({
            "taskId": str(r["task_id"]), "title": r["title"], "category": r["category"], "taskType": r["task_type"],
            "role": r["assigned_role_code"], "raisedBy": r["raised_by_role_code"] or ("System" if r["origin_kind"] == "SYSTEM" else None),
            "severity": r["severity"], "status": r["task_status"],
            "openedAtUtc": r["created_at_utc"], "closedAtUtc": r["closed_at_utc"], "hoursOpen": hours,
        })
    task_summary = {
        "opened": len(tasks),
        "closed": len(closed_hours),
        "open": len(tasks) - len(closed_hours),
        "avgHoursToClose": round(sum(closed_hours) / len(closed_hours), 1) if closed_hours else None,
    }

    events: list[dict[str, Any]] = []
    for r in connection.execute(
        text(
            """
            SELECT event_type, subject_type, subject_id, details, created_at_utc
            FROM auditcore.p2_activity_events
            WHERE tenant_id=:t AND journey_id=:j AND event_type <> 'FACTS_CHANGED'
            ORDER BY event_id DESC LIMIT :limit
            """
        ),
        {"t": tenant_id, "j": journey_id, "limit": limit},
    ).mappings().all():
        details = r["details"] or {}
        events.append({
            "atUtc": r["created_at_utc"], "kind": _ACTIVITY_KIND.get(str(r["event_type"]), "other"),
            "type": r["event_type"], "subject": r["subject_id"] if r["subject_type"] != "JOURNEY" else None,
            "who": details.get("actorRole") or details.get("actor_role") or ("PC" if str(r["event_type"]) in ("UPLOAD_ACCEPTED", "UPLOAD_INITIALIZED", "JOURNEY_STARTED", "PAGE_RETRY_REQUESTED", "PAGE_RETYPED") else "Automatic"),
            "details": details,
        })
    for r in connection.execute(
        text(
            """
            SELECT e.event_type, e.actor_role_code, e.comment, e.details, e.created_at_utc, t.title, t.assigned_role_code
            FROM auditcore.p2_task_events e
            JOIN auditcore.p2_tasks t ON t.tenant_id=e.tenant_id AND t.task_id=e.task_id
            WHERE e.tenant_id=:t AND e.journey_id=:j
            ORDER BY e.task_event_id DESC LIMIT :limit
            """
        ),
        {"t": tenant_id, "j": journey_id, "limit": limit},
    ).mappings().all():
        events.append({
            "atUtc": r["created_at_utc"], "kind": "task", "type": f"TASK_{r['event_type']}",
            "subject": r["title"], "who": r["actor_role_code"] or "System",
            "details": {**(r["details"] or {}), **({"comment": r["comment"]} if r["comment"] else {}),
                        "assignedTo": r["assigned_role_code"]},
        })
    for r in connection.execute(
        text(
            """
            SELECT stage_code, event_type, source_kind, actor_role_snapshot, occurred_at_utc
            FROM auditcore.journey_workflow_events WHERE tenant_id=:t AND journey_id=:j
            ORDER BY occurred_at_utc DESC LIMIT :limit
            """
        ),
        {"t": tenant_id, "j": journey_id, "limit": limit},
    ).mappings().all():
        events.append({
            "atUtc": r["occurred_at_utc"], "kind": "stage", "type": str(r["event_type"]).removeprefix("P2_"),
            "subject": r["stage_code"], "who": r["actor_role_snapshot"] or ("Automatic" if r["source_kind"] == "MACHINE" else None),
            "details": {},
        })
    events.sort(key=lambda e: e["atUtc"])

    # How each stage completed: the gates the stage engine evaluated, with
    # their outcome and when, and every rule (Audit Core control or Rule
    # Engine control) that ran for the stage with what it found.
    registry = get_registry()
    gate_rows = connection.execute(
        text(
            """
            SELECT stage_code, gate_key, gate_status, details, evaluated_at_utc
            FROM auditcore.p2_stage_gate_state WHERE tenant_id=:t AND journey_id=:j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    gate_state = {(str(r["stage_code"]), str(r["gate_key"])): r for r in gate_rows}
    control_rows = connection.execute(
        text(
            """
            SELECT control_code, control_status, status_reason, details, last_evaluated_at_utc,
                   evaluation_count, executor_type, stage_code
            FROM auditcore.p2_control_state WHERE tenant_id=:t AND journey_id=:j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    control_state = {str(r["control_code"]): r for r in control_rows}
    labels = control_labels(connection)
    completion: dict[str, Any] = {}
    for stage_code in ("BOOKING", "DELIVERY"):
        stage_template = registry.stages.get(stage_code)
        stage_row = stages.get(stage_code) or {}
        gates = []
        for gate in (stage_template.gates if stage_template else ()):
            row = gate_state.get((stage_code, gate.key))
            gates.append({
                "key": gate.key, "label": gate.label, "kind": gate.kind,
                "status": str(row["gate_status"]) if row else "WAITING",
                "evaluatedAtUtc": row["evaluated_at_utc"] if row else None,
                "details": dict(row["details"] or {}) if row else {},
            })
        controls = []
        for control in registry.controls.values():
            if not control.applies_to_stage(stage_code):
                continue
            row = control_state.get(control.code)
            if row is None:
                continue
            details = dict(row["details"] or {})
            controls.append({
                "code": control.code, "label": labels.get(control.code, control.code),
                "executor": str(row["executor_type"] or control.executor),
                "status": str(row["control_status"]), "reason": row["status_reason"],
                "evaluatedAtUtc": row["last_evaluated_at_utc"], "evaluations": int(row["evaluation_count"] or 0),
                "leftValue": details.get("leftValue"), "rightValue": details.get("rightValue"),
            })
        controls.sort(key=lambda c: (_STATUS_ORDER.get(c["status"], 9), c["label"]))
        completion[stage_code.lower()] = {
            "completedAtUtc": stage_row.get("business_completed_at_utc"),
            "status": stage_row.get("business_status"),
            "gates": gates,
            "controls": controls,
            "counts": {
                "fired": len(controls),
                "passed": sum(1 for c in controls if c["status"] == "PASS"),
                "failed": sum(1 for c in controls if c["status"] == "FAIL"),
                "waiting": sum(1 for c in controls if c["status"] not in ("PASS", "FAIL")),
            },
        }
    if len(events) > limit:
        events = events[-limit:]

    # The stage durations and the time each role spent, so one tab holds
    # everything the separate Timeline used to; one query, the rest is known.
    def hours(start: Any, end: Any) -> float | None:
        return round((end - start).total_seconds() / 3600, 1) if start and end else None

    stage_times = {}
    for code in ("BOOKING", "DELIVERY"):
        row = stages.get(code) or {}
        started = row.get("first_started_at_utc") or (journey["created_at_utc"] if code == "BOOKING" else None)
        # Completion is the stage engine's; the Gate Pass date (actual_delivered_at)
        # is a separate milestone above, never completion.
        completed = row.get("business_completed_at_utc")
        stage_cancelled = code == "BOOKING" and cancelled
        stage_times[code] = {
            "status": row.get("business_status"), "startedAtUtc": started,
            "submittedAtUtc": row.get("capture_completed_at_utc"),
            "completedAtUtc": None if stage_cancelled else completed, "cancelled": stage_cancelled,
            "hoursToSubmit": hours(started, row.get("capture_completed_at_utc")),
            "hoursToComplete": None if stage_cancelled else hours(started, completed),
            "bookingConfirmDate": row.get("booking_confirmed_at_utc") if code == "BOOKING" else None,
        }
    roles = [
        {"role": r["role"], "tasks": int(r["tasks"]), "open": int(r["open"]),
         "avgHoursToClose": round(float(r["avg_hours"]), 1) if r["avg_hours"] is not None else None,
         "totalHours": round(float(r["total_hours"] or 0), 1)}
        for r in connection.execute(
            text(
                """
                SELECT assigned_role_code AS role, COUNT(*) AS tasks,
                       COUNT(*) FILTER (WHERE closed_at IS NULL) AS open,
                       AVG(EXTRACT(EPOCH FROM closed_at - created_at_utc) / 3600.0) FILTER (WHERE closed_at IS NOT NULL)
                         AS avg_hours,
                       SUM(EXTRACT(EPOCH FROM COALESCE(closed_at, now()) - created_at_utc) / 3600.0) AS total_hours
                FROM (
                  SELECT assigned_role_code, created_at_utc,
                         CASE WHEN task_status IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')
                              THEN COALESCE(verified_at_utc, updated_at_utc) END AS closed_at
                  FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j
                ) t GROUP BY assigned_role_code ORDER BY assigned_role_code
                """
            ),
            {"t": tenant_id, "j": journey_id},
        ).mappings().all()
    ]

    return {
        "pc": pc,
        "milestones": milestones,
        "pending": pending,
        "stages": stage_times,
        "roles": roles,
        "tasks": {"summary": task_summary, "items": tasks},
        "events": [_plain(e) for e in events],
        "completion": completion,
    }


BUILDERS = {
    "deal": deal,
    "invoices": invoices,
    "addons": addons,
    "documents": documents,
    "payments": payments,
    "vehicle": vehicle,
    "tradein": tradein,
    "customer": customer,
    "registration": registration,
    "delivery": delivery,
    "compliance": compliance,
    "activity": activity,
    "duplicates": duplicates,
    "compliance-report": compliance_report,
    "timeline": _timeline,
    "audit": audit,
}
