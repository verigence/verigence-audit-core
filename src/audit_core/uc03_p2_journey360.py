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
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_duplicate_booking_detection import _BASIS_LABEL
from audit_core.uc03_masters_alignment import (
    CONDITIONAL_DISCOUNT_EVIDENCE_DOCUMENT,
    DISCOUNT_ACTUAL_FIELD_TO_BENEFIT_KEY,
    canonical_discount_key,
)
from audit_core.uc03_p2_controls import control_statistics
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_stage import read_booking_stage

SECTIONS = (
    "deal", "addons", "documents", "payments", "vehicle", "registration",
    "delivery", "compliance", "activity",
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


def _money(value: Any) -> str | None:
    return None if value is None else str(Decimal(str(value)))


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _minus(a: Any, b: Any) -> str | None:
    if a is None or b is None:
        return None
    return str(Decimal(str(a)) - Decimal(str(b)))


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
def _paid(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Decimal]:
    """Money actually received: evidenced receipts on ACTIVE documents plus
    loan disbursements not already booked as a receipt (a disbursement
    matched to a payment row is counted once, as that payment)."""
    row = connection.execute(
        text(
            """
            SELECT
              COALESCE((SELECT SUM(p.amount) FROM auditcore.payments p
                 WHERE p.tenant_id=:t AND p.journey_id=:j AND p.amount > 0
                   AND p.status_source='EVIDENCE'
                   AND EXISTS (SELECT 1 FROM auditcore.evidence e
                     WHERE e.tenant_id=p.tenant_id AND e.journey_id=p.journey_id
                       AND e.di_document_id=p.source_di_document_id
                       AND e.association_status='ACTIVE')), 0) AS receipts,
              COALESCE((SELECT SUM(f.loan_disbursement_amount) FROM auditcore.finance_records f
                 WHERE f.tenant_id=:t AND f.journey_id=:j
                   AND f.loan_disbursement_amount IS NOT NULL
                   AND f.loan_disbursement_payment_id IS NULL), 0) AS loan
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    receipts = Decimal(row["receipts"] or 0)
    loan = Decimal(row["loan"] or 0)
    return {"receipts": receipts, "loan": loan, "total": receipts + loan}


# ── summary ──────────────────────────────────────────────────────────────────
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
            "deliveredAtUtc": head["delivered_at"],
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
def _columns(per_source: list[Any]) -> dict[str, Any]:
    """Booking / billed / ledger / quote value for one component. Sources are
    ordered oldest first, so the newest document of each kind wins."""
    out: dict[str, Any] = {"booking": None, "billed": None, "ledger": None, "quote": None}
    for source in per_source:
        document_type = source["source_document_type"]
        column = (
            "booking" if document_type in _BOOKING_SOURCES
            else "ledger" if document_type in _LEDGER_SOURCES
            else "quote" if document_type in _QUOTE_SOURCES
            else "billed"
        )
        out[column] = source["amount"]
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
        reference = cols["billed"] if cols["billed"] is not None else cols["booking"]
        effective_source = None
        if line and line["source_reference"]:
            effective_source = _document_label(str(line["source_reference"]).split(":")[0])
        categories[component_category(key)].append({
            "key": key,
            "label": component_label(key),
            "standard": _money(standard),
            "booking": _money(cols["booking"]),
            "billed": _money(cols["billed"]),
            "ledger": _money(cols["ledger"]),
            "quote": _money(cols["quote"]),
            "effective": _money(line["actual_amount"]) if line else None,
            "effectiveSource": effective_source,
            "variance": _minus(reference, standard),
            "bookingVsBilled": _minus(cols["billed"], cols["booking"]),
            "flags": _flags("COMMERCIAL", standard, cols["booking"], cols["billed"], cols["ledger"]),
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
    std_total, bk_total, cur_total, bl_total, lg_total = (
        _column_total(all_rows, c) for c in ("standard", "booking", "current", "billed", "ledger")
    )
    d_std, d_bk, d_cur, d_bl = (_column_total(discount_rows, c) for c in ("entitled", "booking", "current", "billed"))
    net_std, net_bk, net_cur = _net(std_total, d_std), _net(bk_total, d_bk), _net(cur_total, d_cur)

    def matched(left: str, right: str) -> str | None:
        """Charges less discounts, compared only where both sides have a
        value, so a component missing on one side is never a variance."""
        pairs = [(r[left], r[right]) for r in all_rows if r[left] is not None and r[right] is not None]
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
    invoiced = sum(1 for r in all_rows if r["billed"] is not None)
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
            "components": len(all_rows),
            "paid": {"receipts": str(paid["receipts"]), "loan": str(paid["loan"]), "total": str(paid["total"])},
            "payable": payable,
            "balanceDue": _minus(payable, paid["total"]),
        },
        "flagged": sum(1 for r in all_rows + discount_rows if r["flags"]),
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
                   q.queue_id, q.template_key, q.page_numbers, q.page_number
            FROM auditcore.evidence e
            LEFT JOIN LATERAL (
              SELECT queue_id, template_key, page_numbers, page_number FROM auditcore.p2_document_queue q
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
            rows.append({
                "key": f["field_key"],
                "label": field_label(str(f["field_key"])),
                "value": value,
                "machineValue": f["extracted_value"],
                "corrected": bool(f["is_modified"]),
                "confidence": confidence,
                "reviewed": f["reviewed_at_utc"] is not None or bool(f["is_modified"]),
                "needsReview": template.needs_review(f["field_key"], confidence)
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
            "label": template.display_name if template.key != "supporting_document"
            else _document_label(e["document_type_key"]),
            "stage": template.stage,
            "pages": list(e["page_numbers"] or ([e["page_number"]] if e["page_number"] else [])),
            "linkedAtUtc": e["linked_at_utc"],
            "fields": rows,
            "fieldCount": len(rows),
            "needsReview": sum(1 for r in rows if r["needsReview"]),
            "corrected": sum(1 for r in rows if r["corrected"]),
        })
    return {"documents": out}


# ── payments ─────────────────────────────────────────────────────────────────
def payments(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    rows = connection.execute(
        text(
            """
            SELECT p.payment_id, p.amount, p.receipt_number, p.receipt_date, p.payment_at_utc,
                   COALESCE(p.payment_mode_code, p.payment_method_code) AS mode,
                   p.payment_stage, p.source_di_document_id, p.receipt_bank_name,
                   p.payment_reference, p.status_source,
                   e.association_status, e.document_type_key,
                   (SELECT m.match_status FROM auditcore.payment_bank_matches m
                     WHERE m.tenant_id=p.tenant_id AND m.payment_id=p.payment_id
                     ORDER BY m.updated_at_utc DESC LIMIT 1) AS bank_match
            FROM auditcore.payments p
            LEFT JOIN auditcore.evidence e
              ON e.tenant_id=p.tenant_id AND e.journey_id=p.journey_id
             AND e.di_document_id=p.source_di_document_id
            WHERE p.tenant_id=:t AND p.journey_id=:j AND p.amount > 0
            ORDER BY COALESCE(p.receipt_date, p.payment_at_utc::date) NULLS LAST, p.created_at_utc
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    items = []
    by_stage: dict[str, Decimal] = {}
    for r in rows:
        counted = r["status_source"] == "EVIDENCE" and r["association_status"] == "ACTIVE"
        if counted:
            stage = str(r["payment_stage"] or "BOOKING")
            by_stage[stage] = by_stage.get(stage, Decimal(0)) + Decimal(r["amount"])
        items.append({
            "paymentId": str(r["payment_id"]),
            "amount": _money(r["amount"]),
            "receiptNumber": r["receipt_number"],
            "receiptDate": r["receipt_date"] or r["payment_at_utc"],
            "mode": r["mode"],
            "stage": r["payment_stage"],
            "bank": r["receipt_bank_name"],
            "reference": r["payment_reference"],
            "documentId": str(r["source_di_document_id"]) if r["source_di_document_id"] else None,
            "document": _document_label(r["document_type_key"]) if r["document_type_key"] else None,
            "counted": counted,
            "notCountedReason": None if counted else (
                "Document removed or replaced" if r["association_status"] else "No supporting document"
            ),
            "bankMatch": r["bank_match"],
        })
    paid = _paid(connection, tenant_id=tenant_id, journey_id=journey_id)
    return {
        "items": items,
        "receiptsTotal": str(paid["receipts"]),
        "loanDisbursed": str(paid["loan"]),
        "paidTotal": str(paid["total"]),
        "byStage": {k: str(v) for k, v in by_stage.items()},
    }


# ── vehicle, registration, delivery ──────────────────────────────────────────
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
    return {"product": _plain(product) if product else None, "units": units, "photoCount": int(photo_count)}


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
    return {
        **legacy,
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


BUILDERS = {
    "deal": deal,
    "addons": addons,
    "documents": documents,
    "payments": payments,
    "vehicle": vehicle,
    "registration": registration,
    "delivery": delivery,
    "compliance": compliance,
    "activity": activity,
    "duplicates": duplicates,
    "compliance-report": compliance_report,
}
