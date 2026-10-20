"""uc03_invoice_materialization.py — project reviewed dealer invoices into the
canonical Audit Core reconciliation tables.

The DI generalized-invoice schema (verigence-di ``schemas/invoice.py``) extracts
every dealer invoice type against one lossless superset with deliberately neutral
commercial names — it never emits ``ex_showroom_price`` / ``insurance_amount`` /
``discount_amount``.  This module:

  1. Persists one typed row per reviewed invoice document in
     ``invoice_review_values`` (multiple invoice types coexist on one journey and
     are stored separately — the user's explicit requirement).
  2. Reads each invoice on its own (:func:`read_invoice`): which lines it bills,
     each discount it gives by type, its own net payable and its own arithmetic.
  3. Records each invoice's own figures per document, then sets the canonical
     ``commercial_lines`` / ``discount_applications`` / ``journey_addons``
     actuals from the invoices on file (:func:`sync_invoice_actuals`).

2026-10-20 product owner rules, implemented here:

R1  Ex-showroom comes from the customer's VEHICLE invoice only: the printed
    selling price (gross before discount) plus GST at that invoice's own
    printed rate(s) (CGST+SGST, or IGST, plus cess). Example: selling price
    7,22,033.90 x 1.18 = 8,52,000.00. The discounts printed on the invoice are
    before GST; each is shown with GST at the same rate (21,016.58 x 1.18 =
    24,799.56) and the printed figure is kept, so ex-showroom less discounts
    equals the invoice's vehicle total (8,52,000 - 24,799.56 = 8,27,200.44).
    When this invoice's own taxes are not read the line says so and the PC
    gets a field-review task; a figure is never borrowed from another invoice.
R2  Each invoice serves only its own lines (vehicle invoice: ex-showroom, each
    discount type, TCS and its own net payable; accessory / EW / RSA invoices:
    their own line, from their own grand total). The header discount and the
    discount lines are the same discount, never added together. The OEM's
    wholesale invoice is the dealer's purchase, never a customer bill. A
    voided or replaced document stops counting. The same invoice in DMS and
    Tally (same invoice number) is one invoice, shown once, and a
    disagreement between the copies is flagged.

Deterministic, idempotent, and safe to run inside the Review-Confirm transaction.
"""
from __future__ import annotations

import ast
import json
import logging
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_attribute_mapping import spec_for_field
from audit_core.uc03_deal_source_history import (
    ACTIVE_SOURCE_CONDITION,
    normalise_document_type,
    record_source_value,
    source_rank,
    upsert_commercial_actual,
)
from audit_core.uc03_delivery_commands import _machine_flag
from audit_core.uc03_manual_verification import _resolve_finding
from audit_core.uc03_v2_review_materialization import (
    _INVOICE_DOCUMENT_TYPES as INVOICE_DOCUMENT_TYPES,
)
from audit_core.uc03_v2_review_materialization import _upsert_review_value_row
from audit_core.workflow import create_workflow_task

logger = logging.getLogger(__name__)

_ORIGIN = "INVOICE_MATERIALIZATION"

# Wholesale (dealer<->OEM stock-transfer) invoices are a different
# transaction entirely -- comparing their numbers against a customer-facing
# invoice for the same field would just manufacture false "discrepancies".
_CUSTOMER_FACING_INVOICE_TYPES = tuple(t for t in INVOICE_DOCUMENT_TYPES if t != "wholesale_invoice")

_DISCREPANCY_FINDING_TYPE = "INVOICE_DISCREPANCY"
_DISCREPANCY_WORKFLOW_TYPE = "UC03_INVOICE_DISCREPANCY"
_DISCREPANCY_TASK_TYPE = "INVOICE_DISCREPANCY_REVIEW"
# 2026-10-20: an invoice whose own figures do not add up (or whose taxes were
# not read) goes to the PC to correct the reading on that document.
_FIELD_REVIEW_TASK_TYPE = "INVOICE_FIELD_REVIEW"
_CHECK_RULE_PREFIX = "INVOICE_CHECK:"
_COPIES_RULE_PREFIX = "INVOICE_COPIES_DISAGREE:"
_MULTIPLE_VEHICLE_RULE = "MULTIPLE_VEHICLE_INVOICES"
# Findings raised by the earlier per-type comparison; superseded by the
# per-invoice comparison and closed on the next run.
_LEGACY_DISAGREEMENT_PREFIX = "INVOICE_FIELD_DISAGREEMENT:"

# invoice header scalar fields persisted verbatim in invoice_review_values.
_HEADER_TEXT_FIELDS = (
    "invoice_purpose",
    "invoice_nature",
    "invoice_heading_as_printed",
    "source_system",
    "issuer_role",
    "invoice_number",
    "seller_name",
    "seller_gstin",
    "seller_address",
    "buyer_name",
    "buyer_customer_id",
    "buyer_gstin",
    "buyer_gstin_status",
    "buyer_address",
    "financed_by",
    "amount_in_words",
    "narration",
    "vehicle_description_raw",
    "sku_code",
    "model_name_raw",
    "variant_raw",
    "vin_number",
    "chassis_number",
    "engine_number",
    "vehicle_color",
    "vehicle_registration_number",
    "plan_name",
)
_HEADER_DATE_FIELDS = ("invoice_date", "coverage_start_date", "coverage_end_date")
_HEADER_DECIMAL_FIELDS = (
    "gross_amount_before_discount",
    "invoice_discount_amount",
    "taxable_amount",
    "cgst_rate",
    "cgst_amount",
    "sgst_rate",
    "sgst_amount",
    "igst_rate",
    "igst_amount",
    "cess_amount",
    "tcs_amount",
    "round_off_amount",
    "grand_total_amount",
)
_HEADER_INT_FIELDS = ("tenure_months",)
# line_items is jsonb — written separately (the shared upsert helper has no CAST).
_HEADER_COLUMNS = (
    *_HEADER_TEXT_FIELDS,
    *_HEADER_DATE_FIELDS,
    *_HEADER_DECIMAL_FIELDS,
    *_HEADER_INT_FIELDS,
)

# ── invoice families ─────────────────────────────────────────────────────────
FAMILY_VEHICLE = "VEHICLE"
FAMILY_ACCESSORY = "ACCESSORY"
FAMILY_EXTENDED_WARRANTY = "EXTENDED_WARRANTY"
FAMILY_RSA = "RSA"
FAMILY_CREDIT_NOTE = "CREDIT_NOTE"
FAMILY_DEBIT_NOTE = "DEBIT_NOTE"
FAMILY_WHOLESALE = "WHOLESALE"
FAMILY_OTHER = "OTHER"

# Vehicle-sale invoices by document type, when the invoice does not print a
# purpose (2026-10-20: every vehicle-sale invoice type, not two hard-coded ones).
VEHICLE_SALE_TYPES = frozenset({
    "customer_invoice_dms", "customer_invoice_dms_v2", "tax_invoice_tally", "tax_invoice_dms", "tax_invoice",
})
_FAMILY_BY_TYPE = {
    "accessory_invoice_dms": FAMILY_ACCESSORY,
    "accessory_invoice_tally": FAMILY_ACCESSORY,
    "ew_invoice": FAMILY_EXTENDED_WARRANTY,
    "rsa_invoice": FAMILY_RSA,
}
_FAMILY_BY_PURPOSE = {
    "VEHICLE_SALE": FAMILY_VEHICLE,
    "VEHICLE_WHOLESALE": FAMILY_WHOLESALE,
    "ACCESSORY": FAMILY_ACCESSORY,
    "EXTENDED_WARRANTY": FAMILY_EXTENDED_WARRANTY,
    "RSA": FAMILY_RSA,
}
# A single-purpose invoice bills one line: its own grand total.
_FAMILY_COMPONENT = {
    FAMILY_ACCESSORY: "accessories_cost",
    FAMILY_EXTENDED_WARRANTY: "additional_warranty_amount",
    FAMILY_RSA: "rsa_amount",
}
# Families that bill the customer (the wholesale invoice does not).
CUSTOMER_FAMILIES = frozenset({
    FAMILY_VEHICLE, FAMILY_ACCESSORY, FAMILY_EXTENDED_WARRANTY, FAMILY_RSA, FAMILY_CREDIT_NOTE,
    FAMILY_DEBIT_NOTE, FAMILY_OTHER,
})

# Which copy of one invoice stands for it when DMS and Tally both carry it:
# the canonical source priority (uc03_attribute_mapping._INVOICE).
_TYPE_PRIORITY = (
    "tax_invoice_tally", "customer_invoice_dms", "invoice_generic", "customer_invoice_dms_v2", "tax_invoice",
    "tax_invoice_dms", "accessory_invoice_dms", "accessory_invoice_tally", "ew_invoice", "rsa_invoice",
    "credit_note", "debit_note", "wholesale_invoice",
)

# line_items[].line_category -> commercial component (amounts for the same
# component sum across line items).
_LINE_CATEGORY_TO_COMPONENT: dict[str, str] = {
    "ACCESSORY_GENUINE": "accessories_cost",
    "ACCESSORY_NON_GENUINE": "accessories_cost",
    "EXTENDED_WARRANTY": "additional_warranty_amount",
    "RSA": "rsa_amount",
    "INSURANCE": "insurance_amount",
    "FASTAG": "fastag_amount",
    "SERVICE_PACKAGE": "service_package_amount",
}
_TCS_LINE_CATEGORY = "TCS"
_DISCOUNT_LINE_CATEGORY = "DISCOUNT_LINE"
_VEHICLE_LINE_CATEGORY = "VEHICLE"
_TAX_LINE_CATEGORY = "TAX_LINE"

# journey_addons mirrors (kept in step with uc03_v2_review_materialization).
_COMPONENT_TO_ADDON: dict[str, str] = {
    "additional_warranty_amount": "ADDITIONAL_WARRANTY",
    "accessories_cost": "ACCESSORIES_TOTAL",
    "rsa_amount": "RSA",
    "fastag_amount": "FASTAG",
}

# A printed discount description -> its benefit key, first match wins
# (2026-10-20, R2: "each discount type"). Anything not recognised stays its
# own key rather than being folded into the consumer discount.
UNCLASSIFIED_DISCOUNT = "UNCLASSIFIED_DISCOUNT"
_DISCOUNT_WORDS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), key)
    for pattern, key in (
        (r"scrap\w*.*\b(cod|deposit)\b|\b(cod|deposit)\b.*scrap|\bcertificate of deposit\b", "SCRAPPAGE_BONUS_COD"),
        (r"scrap", "SCRAPPAGE_BONUS_DEALER"),
        (r"exch|upgrade", "EXCHANGE_BONUS"),
        (r"corp", "CORPORATE_PRIVILEGE"),
        (r"loyal|welcome", "WELCOME_BONUS"),
        (r"\bmr\b|management\s*referral", "MANAGEMENT_REFERRAL"),
        (r"referral", "OTHER_SCHEME"),
        (r"insur", "INSURANCE"),
        (r"accessor", "ACCESSORIES_KIT"),
        (r"dealer|additional|\baddl\b|buffer|special|extra", "ADDITIONAL_DISCOUNT"),
        (r"\boem\b|m\s*&\s*m|mahindra|consumer|cash|scheme|offer|retail|festive", "CASH_DISCOUNT"),
    )
)

_PAISE = Decimal("0.01")
_ONE_RUPEE = Decimal(1)
_RATE_PLACES = Decimal("0.0001")
# Readings that leave a line without a figure (as opposed to a check that
# does not add up): a copy with one of these never stands for its invoice
# while a complete copy exists.
_BLOCKING_ISSUES = frozenset({"TAXES_NOT_READ", "PRICE_NOT_READ"})


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    text_value = str(value).strip().replace(",", "").replace("₹", "").replace("Rs.", "").strip()
    if not text_value or text_value.lower() in {"n/a", "na", "none", "null", "-"}:
        return None
    try:
        return Decimal(text_value)
    except (InvalidOperation, ValueError):
        return None


def _to_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        from audit_core.uc03_p2_dates import parse_extracted_date

        return parse_extracted_date(value)


def _to_int(value: Any) -> int | None:
    dec = _to_decimal(value)
    return int(dec) if dec is not None else None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    return normalized or None


def _upper(value: Any) -> str:
    return _clean_text(value).upper() if _clean_text(value) is not None else ""


def _q(value: Decimal) -> Decimal:
    return value.quantize(_PAISE, rounding=ROUND_HALF_UP)


def _line_item_rows(value: Any) -> list[dict[str, Any]]:
    """Normalise a DI array field into a list of dict rows.

    Confirmed live (2026-09-24): a Gemini adapter bug (fixed in verigence-di,
    see gemini_adapter.py's _parse_response) serialized array-valued fields
    via plain str() instead of json.dumps -- for a list of dicts that
    produces Python's single-quoted repr, e.g.
    "[{'description_raw': 'MV Tax', 'amount': 100252}]", which is not valid
    JSON. Every RTO Challan/invoice line_items value extracted before that
    fix is stuck in exactly this form in journey_document_extracted_fields
    and will stay that way until reprocessed. ast.literal_eval parses only
    Python literals (no code execution) and salvages this legacy data back
    into real rows the next time materialization runs, without needing a
    DI reprocess.
    """
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            try:
                raw = ast.literal_eval(raw)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                return []
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _line_amount(item: dict[str, Any]) -> Decimal | None:
    """What a printed line charges: its net amount, else its taxable or gross
    amount, else rate x quantity (2026-10-20: the unit rate alone undercounted
    a line of four items)."""
    for key in ("net_amount", "taxable_amount", "gross_amount"):
        amount = _to_decimal(item.get(key))
        if amount is not None:
            return amount
    rate = _to_decimal(item.get("unit_rate"))
    if rate is None:
        return None
    return rate * (_to_decimal(item.get("quantity")) or Decimal(1))


def _discount_line_amount(item: dict[str, Any]) -> Decimal | None:
    """A printed discount line's amount before GST (R1: invoice discounts are
    printed before tax), as a positive figure."""
    for key in ("taxable_amount", "gross_amount", "discount_amount", "net_amount"):
        amount = _to_decimal(item.get(key))
        if amount is not None:
            return abs(amount)
    rate = _to_decimal(item.get("unit_rate"))
    if rate is None:
        return None
    return abs(rate * (_to_decimal(item.get("quantity")) or Decimal(1)))


def classify_discount(description: Any) -> str:
    """The benefit key a printed discount description names, or
    UNCLASSIFIED_DISCOUNT when it names none."""
    words = " ".join(str(description or "").lower().split())
    if not words:
        return UNCLASSIFIED_DISCOUNT
    for pattern, key in _DISCOUNT_WORDS:
        if pattern.search(words):
            return key
    return UNCLASSIFIED_DISCOUNT


def _fields_by_key(document: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in getattr(document, "fields", []) or []:
        key = str(getattr(field, "fieldKey", "")).strip().lower()
        if not key:
            continue
        out[key] = getattr(field, "value", None)
    return out


def _header_values(raw: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key in _HEADER_TEXT_FIELDS:
        values[key] = _clean_text(raw.get(key))
    for key in _HEADER_DATE_FIELDS:
        values[key] = _to_date(raw.get(key))
    for key in _HEADER_DECIMAL_FIELDS:
        values[key] = _to_decimal(raw.get(key))
    for key in _HEADER_INT_FIELDS:
        values[key] = _to_int(raw.get(key))
    return values


# ── reading one invoice on its own (R1, R2) ──────────────────────────────────
def invoice_family(document_type: Any, raw: dict[str, Any]) -> str:
    """What an invoice bills, from its printed nature and purpose, else its
    document type. The OEM's wholesale invoice is never a customer bill."""
    kind = normalise_document_type(document_type)
    nature = _upper(raw.get("invoice_nature")).replace(" ", "_")
    if kind == "credit_note" or nature == "CREDIT_NOTE":
        return FAMILY_CREDIT_NOTE
    if kind == "debit_note" or nature == "DEBIT_NOTE":
        return FAMILY_DEBIT_NOTE
    if kind == "wholesale_invoice":
        return FAMILY_WHOLESALE
    purpose = _upper(raw.get("invoice_purpose")).replace(" ", "_")
    if purpose in _FAMILY_BY_PURPOSE:
        return _FAMILY_BY_PURPOSE[purpose]
    if kind in _FAMILY_BY_TYPE:
        return _FAMILY_BY_TYPE[kind]
    if kind in VEHICLE_SALE_TYPES:
        return FAMILY_VEHICLE
    return FAMILY_OTHER


def _issue(code: str, message: str, **values: Any) -> dict[str, Any]:
    return {"code": code, "message": message,
            **{k: (str(v) if isinstance(v, Decimal) else v) for k, v in values.items()}}


def _gst_rate(raw: dict[str, Any], taxable: Decimal | None, line_rate: Decimal | None = None) -> Decimal | None:
    """This invoice's own GST rate as a fraction (0.18), cess included:
    the printed rate(s), else the printed tax amounts over the printed
    taxable value. None when this invoice printed neither (never borrowed)."""
    cgst_rate, sgst_rate, igst_rate = (_to_decimal(raw.get(k)) for k in ("cgst_rate", "sgst_rate", "igst_rate"))
    amounts = [_to_decimal(raw.get(k)) for k in ("cgst_amount", "sgst_amount", "igst_amount")]
    base: Decimal | None = None
    if line_rate:
        base = line_rate / 100
    elif igst_rate:
        base = igst_rate / 100
    elif cgst_rate or sgst_rate:
        # CGST and SGST are always levied at the same rate.
        base = ((cgst_rate or sgst_rate or Decimal(0)) + (sgst_rate or cgst_rate or Decimal(0))) / 100
    elif any(a is not None for a in amounts) and taxable:
        base = (sum((a or Decimal(0) for a in amounts), Decimal(0)) / taxable).quantize(
            _RATE_PLACES, rounding=ROUND_HALF_UP)
    if base is None:
        return None
    cess = _to_decimal(raw.get("cess_amount"))
    if cess and taxable:
        base += (cess / taxable).quantize(_RATE_PLACES, rounding=ROUND_HALF_UP)
    return base


def _identity(document_type: str, raw: dict[str, Any]) -> dict[str, Any]:
    number = _clean_text(raw.get("invoice_number") or raw.get("debit_note_number"))
    day = _to_date(raw.get("invoice_date") or raw.get("debit_note_date"))
    vin = _clean_text(raw.get("vin_number") or raw.get("chassis_number"))
    return {
        "number": number,
        # One invoice number however the copy prints it: "DMS/26/0815",
        # "dms 26 0815" and "DMS-26-0815" are one number.
        "numberKey": re.sub(r"[^0-9A-Za-z]", "", number).upper() or None if number else None,
        "date": day.isoformat() if day else None,
        "vin": re.sub(r"\s+", "", vin).upper() if vin else None,
        "grandTotal": _to_decimal(raw.get("grand_total_amount") or raw.get("total_amount")),
    }


def _read_vehicle(raw: dict[str, Any], lines: list[dict[str, Any]], out: dict[str, Any]) -> None:
    """The vehicle invoice's own lines: ex-showroom (R1), each discount by
    type, TCS, any add-on lines it carries, and its own checks."""
    issues: list[dict[str, Any]] = out["issues"]
    vehicle_lines = [i for i in lines if _upper(i.get("line_category")) == _VEHICLE_LINE_CATEGORY]
    addon_lines = [i for i in lines if _upper(i.get("line_category")) in _LINE_CATEGORY_TO_COMPONENT]
    header_taxable = _to_decimal(raw.get("taxable_amount"))

    # Add-on lines on the same invoice (accessories, insurance, FASTag ...)
    # are their own lines; the header then covers them too, so the vehicle
    # is read from its own VEHICLE line when that line prints its figures.
    own_line = vehicle_lines[0] if len(vehicle_lines) == 1 else None
    use_line = bool(addon_lines) and own_line is not None and (
        _to_decimal(own_line.get("gross_amount")) is not None or _to_decimal(own_line.get("taxable_amount")) is not None
    )
    if use_line:
        printed_gross = _to_decimal(own_line.get("gross_amount"))
        taxable = _to_decimal(own_line.get("taxable_amount"))
        line_rate = _to_decimal(own_line.get("tax_rate"))
    else:
        printed_gross = _to_decimal(raw.get("gross_amount_before_discount"))
        taxable = header_taxable
        line_rate = None
        if printed_gross is None and taxable is None and own_line is not None:
            printed_gross = _to_decimal(own_line.get("gross_amount"))
            taxable = _to_decimal(own_line.get("taxable_amount"))
            line_rate = _to_decimal(own_line.get("tax_rate"))

    # Discounts: the header discount OR the discount lines -- one discount
    # printed twice, never added together (R2).
    header_discount = _to_decimal(raw.get("invoice_discount_amount"))
    header_discount = abs(header_discount) if header_discount else None
    printed: dict[str, dict[str, Any]] = {}
    discount_lines = [i for i in lines if _upper(i.get("line_category")) == _DISCOUNT_LINE_CATEGORY]
    for item in discount_lines:
        amount = _discount_line_amount(item)
        if not amount:
            continue
        description = _clean_text(item.get("description_raw") or item.get("description"))
        entry = printed.setdefault(classify_discount(description), {"printed": Decimal(0), "descriptions": []})
        entry["printed"] += _q(amount)
        if description:
            entry["descriptions"].append(description)
    lines_total = sum((e["printed"] for e in printed.values()), Decimal(0))
    if printed:
        if header_discount is not None and abs(header_discount - lines_total) > _ONE_RUPEE:
            issues.append(_issue(
                "DISCOUNT_HEADER_LINES_DISAGREE",
                f"The invoice discount {header_discount} does not equal its discount lines {lines_total}.",
                header=header_discount, lines=lines_total,
            ))
    elif header_discount:
        printed[UNCLASSIFIED_DISCOUNT] = {"printed": _q(header_discount), "descriptions": []}
    elif own_line is not None and _to_decimal(own_line.get("discount_amount")):
        printed[UNCLASSIFIED_DISCOUNT] = {"printed": abs(_to_decimal(own_line.get("discount_amount"))),
                                          "descriptions": []}
    discount_total = sum((e["printed"] for e in printed.values()), Decimal(0))

    # Selling price: as printed, else this invoice's taxable value plus this
    # invoice's own discount.
    if printed_gross is not None:
        selling, selling_source = printed_gross, "PRINTED"
    elif taxable is not None:
        selling, selling_source = taxable + discount_total, "TAXABLE_PLUS_DISCOUNT"
    else:
        selling, selling_source = None, None
        issues.append(_issue("PRICE_NOT_READ", "Neither the selling price nor the taxable value was read."))

    rate = _gst_rate(raw, header_taxable if header_taxable else taxable, line_rate)
    taxes_read = rate is not None
    if not taxes_read:
        issues.append(_issue("TAXES_NOT_READ", "The GST on this invoice (CGST/SGST or IGST) was not read."))

    ex_showroom = _q(selling * (1 + rate)) if selling is not None and rate is not None else None
    if ex_showroom is not None:
        out["lines"]["ex_showroom_price"] = ex_showroom
    for key, entry in printed.items():
        out["discounts"][key] = {
            "amount": _q(entry["printed"] * (1 + rate)) if rate is not None else None,
            "printed": entry["printed"],
            "descriptions": entry["descriptions"],
        }

    # TCS: the header figure, else its TCS lines (one or the other).
    tcs = _to_decimal(raw.get("tcs_amount"))
    if tcs is None:
        tcs_lines = [_line_amount(i) for i in lines if _upper(i.get("line_category")) == _TCS_LINE_CATEGORY]
        tcs = sum((a for a in tcs_lines if a is not None), Decimal(0)) if any(a is not None for a in tcs_lines) else None
    if tcs is not None:
        out["lines"]["tcs_amount"] = tcs
    for item in addon_lines:
        amount = _line_amount(item)
        if amount is not None:
            component = _LINE_CATEGORY_TO_COMPONENT[_upper(item.get("line_category"))]
            out["lines"][component] = out["lines"].get(component, Decimal(0)) + amount

    # The invoice's own arithmetic, to the rupee (R1 / Q5).
    taxes = [_to_decimal(raw.get(k)) for k in ("cgst_amount", "sgst_amount", "igst_amount", "cess_amount")]
    tax_total = sum((t or Decimal(0) for t in taxes), Decimal(0))
    if printed_gross is not None and taxable is not None and abs(printed_gross - discount_total - taxable) > _ONE_RUPEE:
        issues.append(_issue(
            "GROSS_LESS_DISCOUNT_NOT_TAXABLE",
            f"Selling price {printed_gross} less discount {discount_total} does not equal the taxable value {taxable}.",
            gross=printed_gross, discount=discount_total, taxable=taxable,
        ))
    printed_rates = any(_to_decimal(raw.get(k)) for k in ("cgst_rate", "sgst_rate", "igst_rate"))
    gst_amounts = [t for t in taxes[:3] if t is not None]
    if printed_rates and gst_amounts and header_taxable and not use_line:
        base = rate - ((_to_decimal(raw.get("cess_amount")) or Decimal(0)) / header_taxable).quantize(
            _RATE_PLACES, rounding=ROUND_HALF_UP)
        if abs(_q(header_taxable * base) - sum(gst_amounts, Decimal(0))) > _ONE_RUPEE:
            issues.append(_issue(
                "GST_NOT_AT_RATE",
                f"GST {sum(gst_amounts, Decimal(0))} is not the printed rate on the taxable value {header_taxable}.",
                taxable=header_taxable, gst=sum(gst_amounts, Decimal(0)),
            ))
    grand_total = _to_decimal(raw.get("grand_total_amount"))
    round_off = _to_decimal(raw.get("round_off_amount")) or Decimal(0)
    if grand_total is not None and header_taxable is not None and any(t is not None for t in taxes):
        expected = header_taxable + tax_total + (tcs or Decimal(0)) + round_off
        if abs(expected - grand_total) > _ONE_RUPEE:
            issues.append(_issue(
                "TOTAL_DOES_NOT_ADD_UP",
                f"Taxable value + GST + TCS + round-off = {expected}, but the grand total is {grand_total}.",
                expected=expected, grandTotal=grand_total,
            ))
    if grand_total is not None:
        out["netPayable"] = grand_total
    elif header_taxable is not None and taxes_read:
        out["netPayable"] = header_taxable + tax_total + (tcs or Decimal(0)) + round_off
    out["vehicle"] = {
        "sellingPrice": selling,
        "sellingPriceSource": selling_source,
        "taxable": taxable,
        "gstRate": rate,
        "taxesRead": taxes_read,
        "exShowroom": ex_showroom,
        "discountPrinted": discount_total,
        # What the vehicle sold for: ex-showroom less every discount (the
        # invoice's taxable value plus its GST).
        "saleValue": (ex_showroom - sum((d["amount"] for d in out["discounts"].values()), Decimal(0))
                      if ex_showroom is not None else None),
    }


def read_invoice(
    document_type: Any,
    raw: dict[str, Any],
    *,
    document_id: Any = None,
    evidence_id: Any = None,
) -> dict[str, Any]:
    """One invoice read on its own: what it bills (``lines``), each discount
    by type (``discounts``: GST-inclusive ``amount`` and the ``printed``
    pre-GST figure), its own net payable, and every check of its own
    arithmetic that failed (``issues``). Nothing is taken from another
    document (2026-10-20, R1/R2)."""
    kind = normalise_document_type(document_type)
    family = invoice_family(kind, raw)
    lines = _line_item_rows(raw.get("line_items"))
    out: dict[str, Any] = {
        "documentId": str(document_id) if document_id is not None else None,
        "evidenceId": str(evidence_id) if evidence_id is not None else None,
        "documentType": kind,
        "family": family,
        **_identity(kind, raw),
        "lines": {},
        "discounts": {},
        "netPayable": None,
        "vehicle": None,
        "issues": [],
    }
    if family in (FAMILY_VEHICLE, FAMILY_WHOLESALE):
        _read_vehicle(raw, lines, out)
        if family == FAMILY_WHOLESALE:
            # The dealer's purchase: shown as such, never a customer line.
            out["dealerPurchase"] = {**out["vehicle"], "lines": out["lines"], "discounts": out["discounts"]}
            out["lines"], out["discounts"], out["issues"] = {}, {}, []
        return out

    grand_total = out["grandTotal"]
    taxable = _to_decimal(raw.get("taxable_amount"))
    if family == FAMILY_CREDIT_NOTE:
        # A credit note gives back part of an earlier invoice; its narration
        # names what for, else it is a dealer discount.
        amount = grand_total if grand_total is not None else taxable
        if amount:
            words = " ".join(filter(None, [
                _clean_text(raw.get("narration")), _clean_text(raw.get("invoice_heading_as_printed")),
                *(_clean_text(i.get("description_raw")) for i in lines),
            ]))
            key = classify_discount(words)
            if key == UNCLASSIFIED_DISCOUNT:
                key = "ADDITIONAL_DISCOUNT"
            out["discounts"][key] = {"amount": abs(amount), "printed": abs(taxable if taxable else amount),
                                     "descriptions": [words] if words else []}
            out["netPayable"] = -abs(amount)
        return out
    if family == FAMILY_DEBIT_NOTE:
        out["netPayable"] = grand_total
        return out

    # Lines as printed (accessory, EW and RSA invoices check their own lines
    # against the grand total; the grand total is what they bill).
    lines_total = Decimal(0)
    every_line_priced = bool(lines)
    for item in lines:
        category = _upper(item.get("line_category"))
        amount = _line_amount(item)
        if amount is None:
            every_line_priced = False
            continue
        if category == _DISCOUNT_LINE_CATEGORY:
            lines_total -= abs(amount)
        else:
            lines_total += amount
        component = _LINE_CATEGORY_TO_COMPONENT.get(category)
        if family == FAMILY_OTHER and component is not None:
            out["lines"][component] = out["lines"].get(component, Decimal(0)) + amount
    round_off = _to_decimal(raw.get("round_off_amount")) or Decimal(0)
    single = _FAMILY_COMPONENT.get(family)
    if single is not None:
        value = grand_total
        if value is None:
            value = lines_total if every_line_priced else taxable
        if value is not None:
            out["lines"][single] = value
        if grand_total is not None and every_line_priced and abs(lines_total + round_off - grand_total) > _ONE_RUPEE:
            out["issues"].append(_issue(
                "LINES_DO_NOT_ADD_UP",
                f"The line items add up to {lines_total}, but the grand total is {grand_total}.",
                lines=lines_total, grandTotal=grand_total,
            ))
        out["netPayable"] = value
        return out
    out["netPayable"] = grand_total if grand_total is not None else (
        sum(out["lines"].values(), Decimal(0)) if out["lines"] else None)
    return out


# ── the invoices on file, each counted once (R2) ─────────────────────────────
def _type_rank(document_type: str) -> int:
    try:
        return _TYPE_PRIORITY.index(document_type)
    except ValueError:
        return len(_TYPE_PRIORITY)


def _group_key(reading: dict[str, Any]) -> tuple[str, ...]:
    """The same invoice whatever copy it is (DMS or Tally, uploaded twice):
    one invoice number in one family. Without a number, the same date and
    grand total; with neither, the document itself."""
    family = reading["family"]
    if reading["numberKey"]:
        return (family, "N", reading["numberKey"])
    if reading["date"] or reading["grandTotal"] is not None:
        return (family, "D", str(reading["date"]), str(reading["grandTotal"]))
    return (family, "ID", str(reading["documentId"]))


def _copy_order(reading: dict[str, Any]) -> tuple[Any, ...]:
    """The copy that stands for its invoice: a complete reading, then one
    whose own arithmetic holds, then the canonical type priority, then the
    document id."""
    blocked = any(i["code"] in _BLOCKING_ISSUES for i in reading["issues"])
    return (blocked, bool(reading["issues"]), _type_rank(reading["documentType"]), str(reading["documentId"]))


def _copies_disagree(copies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every figure the copies of one invoice print differently."""
    out: list[dict[str, Any]] = []
    checks: list[tuple[str, Any]] = [
        ("netPayable", lambda r: r["netPayable"]),
        ("date", lambda r: r["date"]),
        ("vin", lambda r: r["vin"]),
    ]
    components = sorted({k for r in copies for k in r["lines"]})
    keys = sorted({k for r in copies for k in r["discounts"]})
    checks += [(f"line:{c}", lambda r, c=c: r["lines"].get(c)) for c in components]
    # A discount one copy gives and the other does not is a disagreement
    # (the missing one reads as nil); so is a different total discount.
    checks += [(f"discount:{k}", lambda r, k=k: (r["discounts"].get(k) or {}).get("printed", Decimal(0)))
               for k in keys]
    checks.append(("discountTotal", lambda r: sum((e["printed"] for e in r["discounts"].values()), Decimal(0))))
    for name, value in checks:
        values = {r["documentId"]: value(r) for r in copies}
        present = {v for v in values.values() if v is not None}
        if len(present) <= 1:
            continue
        if all(isinstance(v, Decimal) for v in present) and max(present) - min(present) <= _ONE_RUPEE:
            continue
        out.append({"field": name, "byDocument": {d: (str(v) if v is not None else None) for d, v in values.items()},
                    "byType": {r["documentType"]: (str(value(r)) if value(r) is not None else None) for r in copies}})
    return out


def select_invoices(readings: list[dict[str, Any]]) -> dict[str, Any]:
    """Group the invoices on file into distinct invoices and count each once.

    - Copies of one invoice (same family and number) are one invoice; the
      copy that stands for it is the complete one, then the one whose own
      arithmetic holds, then by the canonical document-type priority, then
      by document id. Never by upload time.
    - Different invoice numbers are different invoices. Accessory, EW, RSA,
      other invoices, credit and debit notes all count. One car has one
      vehicle invoice: when more than one is on file, the latest dated one
      prices the deal and the rest are flagged for a Team Lead.
    - The wholesale invoice is kept apart as the dealer's purchase.
    """
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for reading in readings:
        groups.setdefault(_group_key(reading), []).append(reading)
    built: list[dict[str, Any]] = []
    for key, copies in sorted(groups.items(), key=lambda kv: kv[0]):
        copies = sorted(copies, key=_copy_order)
        built.append({
            "key": "|".join(key),
            "family": copies[0]["family"],
            "representative": copies[0],
            "copies": copies,
            "disagreements": _copies_disagree(copies) if len(copies) > 1 else [],
            "counted": copies[0]["family"] in CUSTOMER_FAMILIES,
        })
    vehicle_groups = [g for g in built if g["family"] == FAMILY_VEHICLE]
    vehicle = None
    if vehicle_groups:
        def vehicle_rank(group: dict[str, Any]) -> tuple[Any, ...]:
            rep = group["representative"]
            complete = not any(i["code"] in _BLOCKING_ISSUES for i in rep["issues"])
            return (complete, rep["date"] or "", rep["numberKey"] or "", str(rep["documentId"]))

        # A complete reading first, then the latest invoice date (a reissue
        # replaces the earlier invoice), then number and document id.
        vehicle = max(vehicle_groups, key=vehicle_rank)
        for group in vehicle_groups:
            group["counted"] = group is vehicle
    counted = [g for g in built if g["counted"]]
    lines: dict[str, Decimal] = {}
    line_documents: dict[str, list[dict[str, Any]]] = {}
    discounts: dict[str, dict[str, Any]] = {}
    for group in counted:
        rep = group["representative"]
        for component, amount in rep["lines"].items():
            lines[component] = lines.get(component, Decimal(0)) + amount
            line_documents.setdefault(component, []).append(rep)
        if group["family"] in (FAMILY_VEHICLE, FAMILY_CREDIT_NOTE):
            for key, entry in rep["discounts"].items():
                slot = discounts.setdefault(key, {"amount": Decimal(0), "printed": Decimal(0), "documents": [],
                                                  "taxesNotRead": False})
                if entry["amount"] is None:
                    slot["taxesNotRead"] = True
                else:
                    slot["amount"] += entry["amount"]
                slot["printed"] += entry["printed"]
                slot["documents"].append(rep)
    for slot in discounts.values():
        if slot["taxesNotRead"] and slot["amount"] == 0:
            slot["amount"] = None
    payables = [g["representative"]["netPayable"] for g in counted]
    return {
        "groups": built,
        "counted": counted,
        "vehicle": vehicle,
        "multipleVehicle": len(vehicle_groups) > 1,
        "families": {g["family"] for g in counted},
        "lines": lines,
        "lineDocuments": line_documents,
        "discounts": discounts,
        "netPayable": sum((p for p in payables if p is not None), Decimal(0)) if counted else None,
        "netPayableMissing": [g["representative"] for g in counted if g["representative"]["netPayable"] is None],
        "wholesale": [g["representative"] for g in built if g["family"] == FAMILY_WHOLESALE],
    }


def derive_commercials(raw: dict[str, Any], document_type: str = "") -> dict[str, Decimal]:
    """OEM-neutral invoice fields -> the commercial lines this one invoice
    bills (see :func:`read_invoice`)."""
    return dict(read_invoice(document_type, raw)["lines"])


def derive_discounts(raw: dict[str, Any], document_type: str = "") -> dict[str, Decimal]:
    """The discounts this one invoice gives, by benefit key, GST-inclusive
    (the printed pre-GST figure when the invoice's taxes were not read)."""
    return {
        key: (entry["amount"] if entry["amount"] is not None else entry["printed"])
        for key, entry in read_invoice(document_type, raw)["discounts"].items()
    }


# ── canonical upserts (invoice-first precedence) ─────────────────────────────
def _reference_document_type(source_reference: Any) -> str | None:
    if source_reference is None:
        return None
    candidate = str(source_reference)
    return normalise_document_type(candidate.split(":", 1)[0]) if ":" in candidate else None


def _source_rank(priority: tuple[str, ...], document_type: str | None) -> int:
    return source_rank(priority, document_type)


def _upsert_commercial_line(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    component_key: str,
    amount: Decimal,
    document_type: str,
    document_id: UUID,
    evidence_id: UUID | None,
) -> bool:
    """Record a document's value and set the canonical actual when it ranks
    ahead of the current holder (see uc03_deal_source_history)."""
    return upsert_commercial_actual(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        component_key=component_key,
        amount=amount,
        document_type=document_type,
        document_id=document_id,
        evidence_id=evidence_id,
    )


def _upsert_addon(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    addon_type_code: str,
    amount: Decimal,
    document_type: str,
    evidence_id: UUID | None,
) -> None:
    details = json.dumps({"origin": _ORIGIN, "sourceDocumentType": document_type.casefold()})
    connection.execute(
        text(
            """
            WITH upd AS (
                UPDATE auditcore.journey_addons
                SET actual_amount=:amount,
                    source_kind='EVIDENCE',
                    source_evidence_id=:evidence_id,
                    details=CAST(:details AS jsonb),
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND addon_type_code=:addon_type_code
                  AND (details->>'origin' IS DISTINCT FROM 'BOOKING_FORM_MATERIALIZATION'
                       OR source_kind='EVIDENCE')
                RETURNING journey_addon_id
            )
            INSERT INTO auditcore.journey_addons (
                tenant_id, journey_id, addon_type_code, actual_amount,
                source_kind, source_evidence_id, details
            )
            SELECT CAST(:tenant_id AS varchar), CAST(:journey_id AS uuid), CAST(:addon_type_code AS varchar), :amount,
                   'EVIDENCE', CAST(:evidence_id AS uuid), CAST(:details AS jsonb)
            WHERE NOT EXISTS (SELECT 1 FROM upd)
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "addon_type_code": addon_type_code,
            "amount": amount,
            "evidence_id": evidence_id,
            "details": details,
        },
    )


def _details(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value or {})


def _write_invoice_discount(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    discount_key: str,
    amount: Decimal | None,
    printed: Decimal | None,
    document_type: str | None,
    document_id: str | None,
    evidence_id: Any,
    origin: str = _ORIGIN,
) -> None:
    """Set the canonical invoice actual for one discount key. The invoices on
    file decide it outright (see :func:`sync_invoice_actuals`); a booking-form
    or calculated actual is always superseded, the entitlement on the row is
    kept."""
    existing = connection.execute(
        text(
            """
            SELECT discount_application_id, details, actual_discount_amount
            FROM auditcore.discount_applications
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND discount_key=:discount_key
            ORDER BY created_at_utc DESC
            LIMIT 1
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "discount_key": discount_key},
    ).mappings().one_or_none()
    payload: dict[str, Any] = {
        "origin": origin,
        "sourceDocumentType": document_type,
        "sourceDocumentId": document_id,
        "printedBeforeGst": str(printed) if printed is not None else None,
    }
    if existing is not None:
        merged = {**_details(existing["details"]), **payload}
        if existing["actual_discount_amount"] == amount and _details(existing["details"]) == merged:
            return
        connection.execute(
            text(
                """
                UPDATE auditcore.discount_applications
                SET actual_discount_amount=:amount,
                    actual_source_kind=CASE WHEN CAST(:amount AS numeric) IS NULL THEN actual_source_kind
                                            ELSE 'EVIDENCE' END,
                    source_evidence_id=CAST(:evidence_id AS uuid),
                    details=CAST(:details AS jsonb),
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND discount_application_id=:application_id
                """
            ),
            {"tenant_id": tenant_id, "application_id": existing["discount_application_id"], "amount": amount,
             "evidence_id": evidence_id, "details": json.dumps(merged)},
        )
        return
    if amount is None:
        return
    connection.execute(
        text(
            """
            INSERT INTO auditcore.discount_applications (
                tenant_id, journey_id, discount_key,
                actual_discount_amount, actual_source_kind,
                source_evidence_id, details
            ) VALUES (
                :tenant_id, :journey_id, :discount_key,
                :amount, 'EVIDENCE', CAST(:evidence_id AS uuid), CAST(:details AS jsonb)
            )
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "discount_key": discount_key, "amount": amount,
         "evidence_id": evidence_id, "details": json.dumps(payload)},
    )


# ── the invoices on file -> canonical actuals ────────────────────────────────
def load_invoice_readings(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """Every invoice persisted for this journey whose document is still on
    file, read on its own."""
    rows = connection.execute(
        text(
            """
            SELECT v.*
            FROM auditcore.invoice_review_values v
            WHERE v.tenant_id=:tenant_id AND v.journey_id=:journey_id
              AND NOT EXISTS (
                SELECT 1 FROM auditcore.evidence e
                WHERE e.tenant_id=v.tenant_id AND e.journey_id=v.journey_id
                  AND (e.di_document_id=v.source_di_document_id OR e.evidence_id=v.source_evidence_id)
                  AND e.association_status <> 'ACTIVE'
              )
            ORDER BY v.source_di_document_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    readings = []
    for row in rows:
        raw = {key: row[key] for key in _HEADER_COLUMNS if key in row}
        raw["line_items"] = row.get("line_items")
        readings.append(read_invoice(
            row["document_type_key"], raw,
            document_id=row["source_di_document_id"], evidence_id=row["source_evidence_id"],
        ))
    return readings


_INVOICE_SOURCE_TYPES = frozenset(normalise_document_type(t) for t in INVOICE_DOCUMENT_TYPES) | {"debit_note"}


def _fallback_source(connection: Connection, *, tenant_id: str, journey_id: UUID, component_key: str
                     ) -> dict[str, Any] | None:
    """The strongest remaining non-invoice source of a component on a
    document still on file (the booking form, a challan ...)."""
    rows = connection.execute(
        text(
            f"""
            SELECT s.source_document_type, s.amount, s.source_document_id, s.source_evidence_id
            FROM auditcore.commercial_line_source_values s
            WHERE s.tenant_id=:tenant_id AND s.journey_id=:journey_id
              AND s.line_kind='COMMERCIAL' AND s.component_key=:component_key
              AND NOT (s.source_document_type = ANY(:invoice_types))
              AND {ACTIVE_SOURCE_CONDITION}
            ORDER BY s.source_document_type, s.source_document_id NULLS FIRST
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "component_key": component_key,
         "invoice_types": sorted(_INVOICE_SOURCE_TYPES)},
    ).mappings().all()
    if not rows:
        return None
    spec = spec_for_field(component_key)
    priority = spec.source_priority if spec is not None else ()
    return dict(min(rows, key=lambda r: (source_rank(priority, r["source_document_type"]),
                                         str(r["source_document_id"]))))


def _set_invoice_actual(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    component_key: str,
    amount: Decimal,
    reading: dict[str, Any],
) -> bool:
    """The canonical actual of a line an invoice bills: the invoices on file
    decide it, unless a stronger non-invoice document holds it (an insurance
    cover outranks an invoice's insurance line)."""
    spec = spec_for_field(component_key)
    priority = spec.source_priority if spec is not None else ()
    reference = f"{reading['documentType']}:{reading['documentId']}"
    existing = connection.execute(
        text(
            """
            SELECT source_reference, actual_amount FROM auditcore.commercial_lines
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND component_key=:component_key
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "component_key": component_key},
    ).mappings().one_or_none()
    if existing is not None:
        holder = _reference_document_type(existing["source_reference"])
        if (holder is not None and holder not in _INVOICE_SOURCE_TYPES
                and source_rank(priority, holder) < source_rank(priority, reading["documentType"])):
            return False
        if existing["actual_amount"] == amount and existing["source_reference"] == reference:
            return False
    connection.execute(
        text(
            """
            INSERT INTO auditcore.commercial_lines (
                tenant_id, journey_id, component_key, actual_amount,
                actual_source_kind, source_evidence_id, source_reference
            ) VALUES (
                :tenant_id, :journey_id, :component_key, :amount, 'EVIDENCE', CAST(:evidence_id AS uuid), :reference
            )
            ON CONFLICT (tenant_id, journey_id, component_key) DO UPDATE SET
                actual_amount=EXCLUDED.actual_amount,
                actual_source_kind='EVIDENCE',
                source_evidence_id=EXCLUDED.source_evidence_id,
                source_reference=EXCLUDED.source_reference,
                updated_at_utc=now()
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "component_key": component_key, "amount": amount,
         "evidence_id": reading["evidenceId"], "reference": reference},
    )
    return True


def sync_invoice_actuals(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: Literal["BOOKING", "DELIVERY"] = "DELIVERY",
    correlation_id: str = "",
) -> dict[str, int]:
    """Set every canonical actual an invoice owns from the invoices on file,
    and nothing else: each invoice counted once, voided documents left out,
    and a line or discount an invoice no longer carries recomputed from the
    remaining sources (2026-10-20, R2)."""
    readings = load_invoice_readings(connection, tenant_id=tenant_id, journey_id=journey_id)
    selection = select_invoices(readings)
    lines_written = 0
    for component_key, amount in sorted(selection["lines"].items()):
        reading = selection["lineDocuments"][component_key][0]
        if _set_invoice_actual(connection, tenant_id=tenant_id, journey_id=journey_id,
                               component_key=component_key, amount=amount, reading=reading):
            lines_written += 1
        addon_code = _COMPONENT_TO_ADDON.get(component_key)
        if addon_code is not None:
            _upsert_addon(connection, tenant_id=tenant_id, journey_id=journey_id, addon_type_code=addon_code,
                          amount=amount, document_type=reading["documentType"], evidence_id=reading["evidenceId"])

    # A line an invoice used to hold but no invoice on file bills now (the
    # invoice was voided or replaced, or it was the wholesale invoice):
    # back to the strongest remaining source, else empty.
    held = connection.execute(
        text(
            """
            SELECT component_key, source_reference FROM auditcore.commercial_lines
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND source_reference IS NOT NULL
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    cleared = 0
    for row in held:
        holder = _reference_document_type(row["source_reference"])
        if holder not in _INVOICE_SOURCE_TYPES or row["component_key"] in selection["lines"]:
            continue
        fallback = _fallback_source(connection, tenant_id=tenant_id, journey_id=journey_id,
                                    component_key=row["component_key"])
        connection.execute(
            text(
                """
                UPDATE auditcore.commercial_lines
                SET actual_amount=:amount, source_reference=:reference,
                    source_evidence_id=CAST(:evidence_id AS uuid),
                    actual_source_kind=CASE WHEN CAST(:amount AS numeric) IS NULL THEN NULL ELSE 'EVIDENCE' END,
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND component_key=:component_key
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "component_key": row["component_key"],
             "amount": fallback["amount"] if fallback else None,
             "reference": (f"{fallback['source_document_type']}:{fallback['source_document_id']}"
                           if fallback else None),
             "evidence_id": fallback["source_evidence_id"] if fallback else None},
        )
        cleared += 1

    # Discounts: the vehicle invoice's own (and credit notes'), by type. A
    # key an invoice set earlier but no longer gives is not claimed on the
    # invoice (actual cleared); when no vehicle invoice is on file any more
    # the row goes back to the booking form's figure on the next
    # reconciliation.
    discounts_written = 0
    for key, slot in sorted(selection["discounts"].items()):
        rep = slot["documents"][0]
        _write_invoice_discount(
            connection, tenant_id=tenant_id, journey_id=journey_id, discount_key=key, amount=slot["amount"],
            printed=slot["printed"], document_type=rep["documentType"], document_id=rep["documentId"],
            evidence_id=rep["evidenceId"],
        )
        discounts_written += 1
    stale = connection.execute(
        text(
            """
            SELECT discount_key FROM auditcore.discount_applications
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND details->>'origin' = :origin
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "origin": _ORIGIN},
    ).scalars().all()
    vehicle_on_file = selection["vehicle"] is not None
    for key in stale:
        if key in selection["discounts"]:
            continue
        _write_invoice_discount(
            connection, tenant_id=tenant_id, journey_id=journey_id, discount_key=key, amount=None, printed=None,
            document_type=None, document_id=None, evidence_id=None,
            origin=_ORIGIN if vehicle_on_file else "INVOICE_WITHDRAWN",
        )

    checks = sync_invoice_checks(connection, tenant_id=tenant_id, journey_id=journey_id, selection=selection,
                                 stage_code=stage_code, correlation_id=correlation_id)
    return {"commercialLines": lines_written, "commercialLinesCleared": cleared,
            "discountApplications": discounts_written, **checks}


# ── per-invoice checks -> findings and review tasks ──────────────────────────
def _ensure_invoice_task(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    finding_id: UUID,
    process_area: str,
    correlation_id: str,
    payload: dict[str, Any],
    role: str = "TL",
    task_type: str = _DISCREPANCY_TASK_TYPE,
    severity: str = "HIGH",
) -> None:
    open_task_count = connection.execute(
        text(
            """
            SELECT count(*) FROM auditcore.workflow_tasks
            WHERE tenant_id=:tenant_id AND related_finding_id=:fid
              AND task_status NOT IN ('COMPLETED', 'CANCELLED', 'FAILED', 'DEAD_LETTER')
            """
        ),
        {"tenant_id": tenant_id, "fid": finding_id},
    ).scalar_one()
    if open_task_count > 0:
        return
    create_workflow_task(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        workflow_type=_DISCREPANCY_WORKFLOW_TYPE,
        process_area=process_area,
        task_type=task_type,
        assigned_role_code=role,
        related_finding_id=finding_id,
        severity=severity,
        task_payload=payload,
        effect_key=f"task:{finding_id}:{task_type.lower()}",
        correlation_id=correlation_id,
    )


# Kept for callers of the earlier name.
_ensure_invoice_discrepancy_task = _ensure_invoice_task


def _flag(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: Literal["BOOKING", "DELIVERY"],
    correlation_id: str,
    rule_key: str,
    title: str,
    description: str,
    payload: dict[str, Any],
    role: str,
    task_type: str,
    severity: Literal["MEDIUM", "HIGH"],
) -> None:
    finding_id = _machine_flag(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=stage_code,
        rule_key=rule_key,
        finding_type=_DISCREPANCY_FINDING_TYPE,
        severity=severity,
        title=title,
        description=description,
        correlation_id=correlation_id,
        safe_payload=payload,
        blocking_completion=False,
    )
    _ensure_invoice_task(
        connection, tenant_id=tenant_id, journey_id=journey_id, finding_id=finding_id, process_area=stage_code,
        correlation_id=correlation_id, payload={"ruleKey": rule_key, "findingId": str(finding_id), **payload},
        role=role, task_type=task_type, severity=severity,
    )


def _invoice_name(reading: dict[str, Any]) -> str:
    label = reading["documentType"].replace("_", " ")
    return f"{label} {reading['number']}" if reading["number"] else label


def sync_invoice_checks(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    selection: dict[str, Any],
    stage_code: Literal["BOOKING", "DELIVERY"] = "DELIVERY",
    correlation_id: str = "",
) -> dict[str, int]:
    """Raise one finding and task per problem with the invoices on file, and
    resolve the ones that no longer hold:

    - an invoice whose own figures do not add up, or whose taxes / price were
      not read -> the PC re-checks the reading on that document;
    - two copies of one invoice (DMS and Tally) printing different figures,
      or more than one vehicle invoice on file -> a Team Lead validates.
    """
    wanted: dict[str, dict[str, Any]] = {}
    for group in selection["groups"]:
        if not group["counted"] and group["family"] != FAMILY_VEHICLE:
            continue
        for reading in group["copies"]:
            for issue in reading["issues"]:
                rule_key = f"{_CHECK_RULE_PREFIX}{reading['documentId']}:{issue['code']}"
                wanted[rule_key] = {
                    "title": f"Check the reading of {_invoice_name(reading)}",
                    "description": f"{issue['message']} Open the invoice, correct the figures read from it, "
                                   "and confirm. This closes itself once the invoice adds up.",
                    "payload": {"documentId": reading["documentId"], "documentType": reading["documentType"],
                                "invoiceNumber": reading["number"], **issue},
                    "role": "PC", "task_type": _FIELD_REVIEW_TASK_TYPE, "severity": "MEDIUM",
                }
        if group["disagreements"]:
            rule_key = f"{_COPIES_RULE_PREFIX}{group['key']}"
            names = ", ".join(_invoice_name(r) for r in group["copies"])
            fields = "; ".join(
                f"{d['field']}: " + ", ".join(f"{t} {v}" for t, v in sorted(d["byType"].items()))
                for d in group["disagreements"]
            )
            wanted[rule_key] = {
                "title": "Copies of one invoice disagree",
                "description": f"{names} are the same invoice but print different figures ({fields}). "
                               "It is counted once; a Team Lead must validate which figure is correct.",
                "payload": {"group": group["key"], "documentIds": [r["documentId"] for r in group["copies"]],
                            "disagreements": group["disagreements"]},
                "role": "TL", "task_type": _DISCREPANCY_TASK_TYPE, "severity": "HIGH",
            }
    if selection["multipleVehicle"]:
        vehicles = [g["representative"] for g in selection["groups"] if g["family"] == FAMILY_VEHICLE]
        chosen = selection["vehicle"]["representative"]
        wanted[_MULTIPLE_VEHICLE_RULE] = {
            "title": "More than one vehicle invoice on file",
            "description": (
                "These vehicle invoices have different invoice numbers: "
                + ", ".join(_invoice_name(r) for r in vehicles)
                + f". The deal is priced on {_invoice_name(chosen)} (the latest). Void the cancelled invoice, "
                "or confirm both were issued."
            ),
            "payload": {"documentIds": [r["documentId"] for r in vehicles], "pricedOn": chosen["documentId"]},
            "role": "TL", "task_type": _DISCREPANCY_TASK_TYPE, "severity": "HIGH",
        }

    for rule_key, spec in sorted(wanted.items()):
        _flag(connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage_code,
              correlation_id=correlation_id, rule_key=rule_key, title=spec["title"],
              description=spec["description"], payload=spec["payload"], role=spec["role"],
              task_type=spec["task_type"], severity=spec["severity"])

    open_rows = connection.execute(
        text(
            """
            SELECT audit_finding_id, rule_key, stage_code FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
              AND (rule_key LIKE :check OR rule_key LIKE :copies OR rule_key = :multiple OR rule_key LIKE :legacy)
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "check": f"{_CHECK_RULE_PREFIX}%",
         "copies": f"{_COPIES_RULE_PREFIX}%", "multiple": _MULTIPLE_VEHICLE_RULE,
         "legacy": f"{_LEGACY_DISAGREEMENT_PREFIX}%"},
    ).mappings().all()
    resolved = 0
    for row in open_rows:
        if row["rule_key"] in wanted:
            continue
        _resolve_finding(
            connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=row["stage_code"] or stage_code,
            finding_id=row["audit_finding_id"], actor_id=None, correlation_id=correlation_id,
            note="The invoices on file no longer show this problem.",
        )
        resolved += 1
    return {"raised": len(wanted), "resolved": resolved}


def check_invoice_field_discrepancies(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: Literal["BOOKING", "DELIVERY"],
    correlation_id: str = "",
) -> dict[str, int]:
    """Raise / resolve the findings for the invoices on file (see
    :func:`sync_invoice_checks`). Copies of one invoice are compared with
    each other; different invoices are never compared as if they were one
    (2026-10-20, R2). Never raises -- best-effort, safe on every call."""
    try:
        selection = select_invoices(load_invoice_readings(connection, tenant_id=tenant_id, journey_id=journey_id))
        return sync_invoice_checks(connection, tenant_id=tenant_id, journey_id=journey_id, selection=selection,
                                   stage_code=stage_code, correlation_id=correlation_id)
    except Exception:
        logger.warning("uc03_invoice_producer_failed", exc_info=True,
                       extra={"tenant_id": tenant_id, "journey_id": str(journey_id)})
        return {"raised": 0, "resolved": 0, "error": 1}


# ── producer ────────────────────────────────────────────────────────────────
def materialize_reviewed_invoices(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[Any],
    actor_id: str,
    stage_code: Literal["BOOKING", "DELIVERY"] = "DELIVERY",
    correlation_id: str = "",
) -> dict[str, int]:
    """Persist every reviewed invoice, record each one's own figures per
    document, then set the canonical actuals from every invoice on file."""

    invoices_written = 0
    for document in documents:
        document_type = normalise_document_type(getattr(document, "documentTypeKey", "") or "")
        if document_type not in INVOICE_DOCUMENT_TYPES:
            continue
        if str(getattr(document, "extractionState", "")).upper() != "READY":
            continue

        raw = _fields_by_key(document)
        if not raw:
            continue

        header = _header_values(raw)
        evidence_id = getattr(document, "evidenceId", None)
        row_id = _upsert_review_value_row(
            connection,
            table_name="invoice_review_values",
            id_column="invoice_review_value_id",
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document.documentId,
            evidence_id=evidence_id,
            actor_id=actor_id,
            columns=_HEADER_COLUMNS,
            values=header,
            extra_insert_columns={"document_type_key": document_type},
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.invoice_review_values
                SET line_items = CAST(:line_items AS jsonb)
                WHERE tenant_id = :tenant_id
                  AND invoice_review_value_id = CAST(:row_id AS uuid)
                """
            ),
            {
                "tenant_id": tenant_id,
                "row_id": row_id,
                "line_items": json.dumps(_line_item_rows(raw.get("line_items")), default=str),
            },
        )
        invoices_written += 1

        reading = read_invoice(document_type, raw, document_id=document.documentId, evidence_id=evidence_id)
        if reading["family"] not in CUSTOMER_FAMILIES:
            continue  # the wholesale invoice: the dealer's purchase, kept as its review row only
        for component_key, amount in reading["lines"].items():
            record_source_value(
                connection, tenant_id=tenant_id, journey_id=journey_id, line_kind="COMMERCIAL",
                component_key=component_key, source_document_type=document_type, amount=amount,
                evidence_id=evidence_id, document_id=document.documentId,
            )
        for discount_key, entry in reading["discounts"].items():
            record_source_value(
                connection, tenant_id=tenant_id, journey_id=journey_id, line_kind="DISCOUNT",
                component_key=discount_key, source_document_type=document_type, amount=entry["amount"],
                evidence_id=evidence_id, document_id=document.documentId,
            )

    # Re-derived from every invoice on file on each pass that carries the
    # journey's documents, so a voided invoice stops counting on the next
    # re-materialisation (an empty pass has nothing to re-derive from).
    synced = sync_invoice_actuals(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage_code,
        correlation_id=correlation_id,
    ) if documents else {"commercialLines": 0, "discountApplications": 0}
    result = {
        "invoices": invoices_written,
        "commercialLines": synced["commercialLines"],
        "discountApplications": synced["discountApplications"],
        "journeyAddons": 0,
        "discrepanciesRaised": synced.get("raised", 0),
        "discrepanciesResolved": synced.get("resolved", 0),
    }
    if invoices_written:
        logger.info(
            "UC03 reviewed invoices materialized",
            extra={"tenant_id": tenant_id, "journey_id": str(journey_id), **result},
        )
    return result


__all__ = [
    "CUSTOMER_FAMILIES",
    "FAMILY_ACCESSORY",
    "FAMILY_CREDIT_NOTE",
    "FAMILY_DEBIT_NOTE",
    "FAMILY_EXTENDED_WARRANTY",
    "FAMILY_OTHER",
    "FAMILY_RSA",
    "FAMILY_VEHICLE",
    "FAMILY_WHOLESALE",
    "INVOICE_DOCUMENT_TYPES",
    "UNCLASSIFIED_DISCOUNT",
    "VEHICLE_SALE_TYPES",
    "check_invoice_field_discrepancies",
    "classify_discount",
    "derive_commercials",
    "derive_discounts",
    "invoice_family",
    "load_invoice_readings",
    "materialize_reviewed_invoices",
    "read_invoice",
    "select_invoices",
    "sync_invoice_actuals",
    "sync_invoice_checks",
]
