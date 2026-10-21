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
from datetime import UTC, date, datetime
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
# Invoice problems belong to the journey, not to the pass that noticed them:
# the Booking and Delivery passes both re-check the invoices on file, so the
# findings are always raised under one stage key -- one finding and one task
# per problem (final check FB1).
_INVOICE_FINDING_STAGE: Literal["DELIVERY"] = "DELIVERY"
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
_BLOCKING_ISSUES = frozenset({"TAXES_NOT_READ", "PRICE_NOT_READ", "RATE_UNCLEAR", "VEHICLE_LINE_NOT_READ"})


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
        if amount:  # a printed zero is no figure; read the next one
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
    """What an invoice bills. The document type decides first (2026-10-20,
    review S9: an accessory invoice whose reading says "vehicle sale" is
    still an accessory invoice); the printed nature and purpose only decide
    for a generic invoice or an unknown type. The OEM's wholesale invoice
    is never a customer bill."""
    kind = normalise_document_type(document_type)
    if kind == "credit_note":
        return FAMILY_CREDIT_NOTE
    if kind == "debit_note":
        return FAMILY_DEBIT_NOTE
    if kind == "wholesale_invoice":
        return FAMILY_WHOLESALE
    if kind in _FAMILY_BY_TYPE:
        return _FAMILY_BY_TYPE[kind]
    if kind in VEHICLE_SALE_TYPES:
        return FAMILY_VEHICLE
    nature = _upper(raw.get("invoice_nature")).replace(" ", "_")
    if nature == "CREDIT_NOTE":
        return FAMILY_CREDIT_NOTE
    if nature == "DEBIT_NOTE":
        return FAMILY_DEBIT_NOTE
    purpose = _upper(raw.get("invoice_purpose")).replace(" ", "_")
    return _FAMILY_BY_PURPOSE.get(purpose, FAMILY_OTHER)


def _type_purpose_conflict(document_type: str, raw: dict[str, Any], family: str) -> dict[str, Any] | None:
    """A printed purpose naming a different kind of invoice than the
    document type: the type stands, the PC checks the reading."""
    purpose = _upper(raw.get("invoice_purpose")).replace(" ", "_")
    named = _FAMILY_BY_PURPOSE.get(purpose)
    if named is None or named == family or normalise_document_type(document_type) == "invoice_generic":
        return None
    if family not in (FAMILY_VEHICLE, FAMILY_ACCESSORY, FAMILY_EXTENDED_WARRANTY, FAMILY_RSA):
        return None
    return _issue("PURPOSE_CONFLICTS_WITH_TYPE",
                  f"The invoice was uploaded as {document_type} but its reading says {purpose}; "
                  "it is read as its document type.", purpose=purpose)


def _issue(code: str, message: str, **values: Any) -> dict[str, Any]:
    return {"code": code, "message": message,
            **{k: (str(v) if isinstance(v, Decimal) else v) for k, v in values.items()}}


_RATE_TOLERANCE = Decimal("0.005")


def _near(a: Decimal | None, b: Decimal | None, tolerance: Decimal = _RATE_TOLERANCE) -> bool:
    return a is not None and b is not None and abs(a - b) <= tolerance


def _rate_reading(
    raw: dict[str, Any],
    header_taxable: Decimal | None,
    vehicle_line: dict[str, Any] | None,
) -> tuple[Decimal | None, list[dict[str, Any]]]:
    """This invoice's own GST rate as a fraction (0.18), cess included, and
    any problem with it (2026-10-20, review S1/S3):

    - the printed header rates: CGST + SGST, or IGST;
    - one of CGST / SGST printed alone counts twice only when the printed
      tax amounts confirm it (never doubled blindly);
    - no printed rate: the printed tax amounts over the taxable value;
    - the vehicle line's own rate is used only when it agrees with the
      header (as the total or as each of CGST/SGST); with no header tax at
      all, only when the line's own tax amount confirms it.

    None with RATE_UNCLEAR when the readings do not agree, None alone when
    nothing was printed. Never borrowed from another invoice."""
    issues: list[dict[str, Any]] = []
    cgst_rate, sgst_rate, igst_rate = (_to_decimal(raw.get(k)) for k in ("cgst_rate", "sgst_rate", "igst_rate"))
    amounts = [_to_decimal(raw.get(k)) for k in ("cgst_amount", "sgst_amount", "igst_amount")]
    amount_rate = None
    if any(a is not None for a in amounts) and header_taxable:
        amount_rate = (sum((a or Decimal(0) for a in amounts), Decimal(0)) / header_taxable).quantize(
            _RATE_PLACES, rounding=ROUND_HALF_UP)

    def unclear(message: str) -> tuple[None, list[dict[str, Any]]]:
        issues.append(_issue("RATE_UNCLEAR", message))
        return None, issues

    base: Decimal | None = None
    if igst_rate:
        base = igst_rate / 100
    elif cgst_rate and sgst_rate:
        base = (cgst_rate + sgst_rate) / 100
    elif cgst_rate or sgst_rate:
        single = (cgst_rate or sgst_rate) / 100
        both_amounts = amounts[0] is not None and amounts[1] is not None
        if _near(amount_rate, 2 * single) or (amount_rate is None and both_amounts and amounts[0] == amounts[1]):
            base = 2 * single
        elif _near(amount_rate, single):
            base = single
        else:
            return unclear(f"Only one of CGST / SGST prints a rate ({single * 100}%) and the tax amounts do not "
                           "say whether it is per component or the total.")
    elif amount_rate is not None:
        base = amount_rate

    line_rate = _to_decimal((vehicle_line or {}).get("tax_rate"))
    if line_rate:
        line_fraction = line_rate / 100
        if base is not None:
            if not (_near(line_fraction, base) or _near(2 * line_fraction, base)):
                return unclear(f"The vehicle line prints {line_rate}% but the invoice's GST is {base * 100}%.")
        else:
            line_taxable = _to_decimal(vehicle_line.get("taxable_amount"))
            line_tax = _to_decimal(vehicle_line.get("tax_amount"))
            implied = (line_tax / line_taxable) if line_tax is not None and line_taxable else None
            if _near(implied, line_fraction):
                base = line_fraction
            elif _near(implied, 2 * line_fraction):
                base = 2 * line_fraction
            else:
                return unclear(f"The vehicle line prints {line_rate}% and nothing else on the invoice confirms "
                               "the GST rate.")
    if base is None:
        return None, issues
    cess = _to_decimal(raw.get("cess_amount"))
    if cess and header_taxable:
        base += (cess / header_taxable).quantize(_RATE_PLACES, rounding=ROUND_HALF_UP)
    return base, issues


# OCR reads one invoice number differently on its copies ("INV27O000668" /
# "INV270000668", "C1R..." / "CIR..." / "CLR..."): compared without
# separators, upper-cased, O as 0 and I / L as 1 (live data, 2026-10-21).
_OCR_DIGITS = str.maketrans({"O": "0", "I": "1", "L": "1"})
_VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")


def ocr_number_key(value: Any) -> str | None:
    """An invoice number as compared across copies (never shown)."""
    key = re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper().translate(_OCR_DIGITS)
    return key or None


def _vin_parts(value: Any) -> tuple[str | None, str | None, str | None]:
    """(as printed, full 17-character VIN, last 8 characters). DMS prints the
    VIN partly or decorated ("T5H14641", "T2H36974 (MA1UN2YY2T2H36974)");
    Tally often not at all (live data, 2026-10-21)."""
    text_value = _clean_text(value)
    if not text_value:
        return None, None, None
    tokens = [t for t in re.split(r"[^0-9A-Za-z]+", text_value.upper()) if t]
    if not tokens:
        return None, None, None
    full = next((t for t in tokens if len(t) == 17 and _VIN_RE.fullmatch(t)), None)
    longest = full or max(tokens, key=len)
    return longest, full, longest[-8:] if len(longest) >= 6 else None


def _identity(document_type: str, raw: dict[str, Any]) -> dict[str, Any]:
    number = _clean_text(raw.get("invoice_number") or raw.get("debit_note_number"))
    day = _to_date(raw.get("invoice_date") or raw.get("debit_note_date"))
    vin, vin_full, vin_tail = _vin_parts(raw.get("vin_number") or raw.get("chassis_number"))
    return {
        "number": number,
        "numberKey": ocr_number_key(number) if number else None,
        "numberExact": re.sub(r"[^0-9A-Za-z]", "", number).upper() or None if number else None,
        "date": day.isoformat() if day else None,
        "vin": vin,
        "vinFull": vin_full,
        "vinTail": vin_tail,
        "grandTotal": _to_decimal(raw.get("grand_total_amount") or raw.get("total_amount")),
        # Which books the copy comes from: a DMS copy and a Tally copy of one
        # bill can print different numbers (review B1).
        "side": ("DMS" if "dms" in document_type else "TALLY" if "tally" in document_type
                 else (_upper(raw.get("source_system")) or None)),
        "purposePrinted": _upper(raw.get("invoice_purpose")) or None,
    }


def _vin_relation(a: dict[str, Any], b: dict[str, Any]) -> str | None:
    """"same" / "different" vehicle by VIN, None when either has none: the
    full VIN when both print it, else the last 8 characters (a partial VIN
    is the tail of the full one)."""
    if not a.get("vinTail") or not b.get("vinTail"):
        return None
    if a.get("vinFull") and b.get("vinFull"):
        return "same" if a["vinFull"] == b["vinFull"] else "different"
    return "same" if a["vinTail"] == b["vinTail"] else "different"


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
    line_figures = own_line is not None and (
        _to_decimal(own_line.get("gross_amount")) is not None or _to_decimal(own_line.get("taxable_amount")) is not None
    )
    use_line = bool(addon_lines) and line_figures
    vehicle_not_separable = bool(addon_lines) and not line_figures
    rate_line: dict[str, Any] | None = own_line if line_figures else None
    if use_line:
        printed_gross = _to_decimal(own_line.get("gross_amount"))
        taxable = _to_decimal(own_line.get("taxable_amount"))
    elif vehicle_not_separable:
        # The header covers the add-on lines too and the vehicle line prints
        # no figure of its own: the vehicle's price cannot be read (review
        # S2); never the header gross, which would count the add-ons twice.
        printed_gross, taxable = None, None
        issues.append(_issue("VEHICLE_LINE_NOT_READ",
                             "The invoice also bills add-ons, and its vehicle line prints no price of its own."))
    else:
        printed_gross = _to_decimal(raw.get("gross_amount_before_discount"))
        taxable = header_taxable
        if printed_gross is None and taxable is None and own_line is not None:
            printed_gross = _to_decimal(own_line.get("gross_amount"))
            taxable = _to_decimal(own_line.get("taxable_amount"))

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
        if not vehicle_not_separable:
            issues.append(_issue("PRICE_NOT_READ", "Neither the selling price nor the taxable value was read."))

    rate, rate_issues = _rate_reading(raw, header_taxable if header_taxable else taxable, rate_line)
    issues.extend(rate_issues)
    taxes_read = rate is not None
    if not taxes_read and not rate_issues:
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
    if printed_rates and gst_amounts and header_taxable and rate is not None and not use_line:
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
        "rateUnclear": bool(rate_issues),
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
    conflict = _type_purpose_conflict(kind, raw, family)
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
        "issues": [conflict] if conflict else [],
        "cancelled": None,
        "_raw": raw,
        # What a credit note says it reverses (its narration, heading and lines).
        # Every printed word, as compared with an invoice number (final
        # check FN3: a whole number, never a fragment of the text).
        "referenceTokens": sorted({(key, re.sub(r"[^0-9A-Za-z]", "", word).upper()) for word in " ".join(filter(None, [
            _clean_text(raw.get("narration")), _clean_text(raw.get("invoice_heading_as_printed")),
            *(_clean_text(i.get("description_raw")) for i in lines)])).split()
            for key in [ocr_number_key(word)] if key}),
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


def vehicle_invoice_ex_showroom(document_fields: dict[str, Any], document_type: str) -> dict[str, Any]:
    """The R1 reading of one vehicle invoice, for every caller that needs it
    (the Deal sheet, SKU identification): one implementation.

    Returns {exShowroom, rate, sellingPrice, sellingPriceSource, taxable,
    saleValue, discounts: {key: {amount (GST-inclusive), printed}}, issues}.
    ``exShowroom`` is None whenever the invoice's own figures do not give it
    (taxes not read, rate unclear, vehicle price not separable); ``issues``
    then says why. Never borrows from another document.

    ``document_type`` is required: the type decides what kind of invoice it
    is. A generic invoice that prints no purpose reads as no vehicle invoice
    here; on the Deal it is paired with the vehicle invoice it copies."""
    reading = read_invoice(document_type, dict(document_fields))
    vehicle = reading.get("vehicle") or reading.get("dealerPurchase") or {}
    return {
        "exShowroom": vehicle.get("exShowroom"),
        "rate": vehicle.get("gstRate"),
        "sellingPrice": vehicle.get("sellingPrice"),
        "sellingPriceSource": vehicle.get("sellingPriceSource"),
        "taxable": vehicle.get("taxable"),
        "saleValue": vehicle.get("saleValue"),
        "discounts": {k: {"amount": v["amount"], "printed": v["printed"]}
                      for k, v in (reading["discounts"] or (vehicle.get("discounts") or {})).items()},
        "family": reading["family"],
        "issues": reading["issues"],
    }


# ── the invoices on file, each counted once (R2) ─────────────────────────────
def _type_rank(document_type: str) -> int:
    try:
        return _TYPE_PRIORITY.index(document_type)
    except ValueError:
        return len(_TYPE_PRIORITY)


def _single_key(reading: dict[str, Any]) -> str:
    """One invoice's own key without its copies: its number, else its date
    and total, else the document itself (review N2: a missing total never
    merges two documents)."""
    if reading["numberKey"]:
        return f"N|{reading['numberKey']}"
    if reading["grandTotal"] is not None:
        return f"D|{reading['date']}|{reading['grandTotal']}"
    return f"ID|{reading['documentId']}"


def _same_system_two_bills(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Two documents from one system are two bills when their numbers
    differ -- including numbers equal only after reading O as 0 and I/L as
    1, when their totals differ too (final check FN2). The very same number
    printed twice is one bill, read twice (copies that may disagree)."""
    if a["numberKey"] != b["numberKey"]:
        return True
    if a["numberExact"] == b["numberExact"]:
        return False
    return (a["grandTotal"] is not None and b["grandTotal"] is not None
            and abs(a["grandTotal"] - b["grandTotal"]) > _ONE_RUPEE)


def _days_apart(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def _same_bill(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """A DMS and a Tally copy of one single-purpose bill printing different
    numbers: the same grand total to the rupee, dated within a day."""
    if a["grandTotal"] is None or b["grandTotal"] is None:
        return False
    if abs(a["grandTotal"] - b["grandTotal"]) > _ONE_RUPEE:
        return False
    apart = _days_apart(a["date"], b["date"])
    return apart is None or apart <= 1


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


def _pair_generic_copies(readings: list[dict[str, Any]]) -> None:
    """A generic invoice that prints no purpose but carries the vehicle
    invoice's VIN, number or grand total is a copy of that vehicle invoice
    (review S5): re-read it as one."""
    vehicles = [r for r in readings if r["family"] == FAMILY_VEHICLE]
    for index, reading in enumerate(readings):
        if (reading["documentType"] != "invoice_generic" or reading["family"] != FAMILY_OTHER
                or reading["purposePrinted"] not in (None, "UNKNOWN", "OTHER")):
            continue
        match = any(
            _vin_relation(reading, v) == "same"
            or (reading["numberKey"] and reading["numberKey"] == v["numberKey"])
            or (reading["grandTotal"] is not None and v["grandTotal"] is not None
                and abs(reading["grandTotal"] - v["grandTotal"]) <= _ONE_RUPEE)
            for v in vehicles
        )
        if match:
            raw = {**reading["_raw"], "invoice_purpose": "VEHICLE_SALE"}
            copy = read_invoice("invoice_generic", raw, document_id=reading["documentId"],
                                evidence_id=reading["evidenceId"])
            copy["purposePrinted"] = reading["purposePrinted"]
            copy["pairedAs"] = FAMILY_VEHICLE
            readings[index] = copy


def _cancellation_amounts(reading: dict[str, Any]) -> list[Decimal]:
    """What a credit note reversing this invoice would give back: its grand
    total, or that less the TCS (and the round-off) (review RS3)."""
    total = reading["grandTotal"]
    if total is None:
        return []
    tcs = reading["lines"].get("tcs_amount") or Decimal(0)
    round_off = _to_decimal(reading["_raw"].get("round_off_amount")) or Decimal(0)
    return [total, total - tcs, total - tcs - round_off]


def _names_invoice(note: dict[str, Any], invoice: dict[str, Any]) -> bool:
    """The credit note prints this invoice's whole number as one word (or
    as the end of one, after a label such as "No."), and the number is long
    enough not to occur by chance (5+ characters)."""
    key = invoice["numberKey"]
    if not key or len(key) < 5:
        return False
    # The label before a number ("No.", "Inv") is read as printed, so "No"
    # is never taken for the digits "N0".
    return any(token == key or (token.endswith(key) and not exact[:-len(key)][-1:].isdigit())
               for token, exact in note["referenceTokens"])


def _mark_cancellations(groups: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """A credit note that reverses a whole vehicle invoice cancels that
    invoice (review S4, RB1): it names the invoice's number or carries its
    VIN, and gives back its total (grand total, or less TCS / round-off, to
    the rupee). The invoice's group -- that bill and its copy from the other
    system -- and the credit note stop counting; a separate reissue stays.
    With more than one candidate, the one it names, else the earliest.

    A credit note giving back a vehicle invoice's whole total but naming
    neither its number nor its VIN is held back (not a dealer discount) and
    the PC is asked whether it cancels that invoice (review RS2). Returns
    those questions; a part credit note stays a discount."""
    questions: list[tuple[dict[str, Any], dict[str, Any]]] = []
    vehicles = [g for g in groups if g["family"] == FAMILY_VEHICLE]
    for note_group in [g for g in groups if g["family"] == FAMILY_CREDIT_NOTE and not g["cancelled"]]:
        note = note_group["representative"]
        amount = note["grandTotal"] if note["grandTotal"] is not None else _to_decimal(note["_raw"].get("taxable_amount"))
        if amount is None:
            continue
        named_candidates, unnamed = [], []
        for group in vehicles:
            if group["cancelled"]:
                continue
            copies = group["copies"]
            if not any(abs(abs(amount) - x) <= _ONE_RUPEE for c in copies for x in _cancellation_amounts(c)):
                continue
            named = any(_names_invoice(note, c) for c in copies)
            same_vehicle = any(_vin_relation(note, c) == "same" for c in copies)
            date_key = min((c["date"] for c in copies if c["date"]), default="9999")
            if named or same_vehicle:
                named_candidates.append((not named, date_key, group["key"], group))
            else:
                unnamed.append((date_key, group["key"], group))
        if named_candidates:
            cancelled = min(named_candidates, key=lambda c: c[:3])[3]
            cancelled["cancelled"] = True
            cancelled["cancelledBy"] = note["documentId"]
            for copy in cancelled["copies"]:
                copy["cancelled"] = note["documentId"]
            note_group["cancelled"] = True
            note["cancels"] = cancelled["representative"]["documentId"]
        elif unnamed:
            target = min(unnamed, key=lambda c: c[:2])[2]
            note_group["pendingCancellation"] = target["key"]
            questions.append((note_group, target))
    return questions


def _vehicle_sets(readings: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Vehicle invoices grouped into bills (review B1, RB2, RS1; live data
    2026-10-21):

    - the same invoice number (compared OCR-tolerantly) is one bill, on
      either system, even when one copy's date or total was misread -- the
      copies are then compared, never added;
    - a DMS copy and a Tally copy of one vehicle (VIN, full or its last 8
      characters) are one bill, whatever numbers they print;
    - a DMS copy and a Tally copy where one prints no VIN are one bill when
      their grand totals agree to the rupee (the closest date first);
    - two invoices from the same system with different numbers are two
      invoices (a cancel-and-reissue), and different VINs are different
      vehicles."""
    parent = list(range(len(readings)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def members(root: int) -> list[dict[str, Any]]:
        return [readings[k] for k in range(len(readings)) if find(k) == root]

    def join(i: int, j: int) -> bool:
        a, b = find(i), find(j)
        if a == b:
            return True
        combined = members(a) + members(b)
        for x in combined:
            for y in combined:
                if _vin_relation(x, y) == "different":
                    return False  # two vehicles are never one bill
                if x["side"] and x["side"] == y["side"] and _same_system_two_bills(x, y):
                    return False  # one system, two numbers: two invoices
        parent[b] = a
        return True

    order = sorted(range(len(readings)), key=lambda k: str(readings[k]["documentId"]))
    for i in order:
        for j in order:
            if i < j and readings[i]["numberKey"] and readings[i]["numberKey"] == readings[j]["numberKey"]:
                join(i, j)
    for i in order:
        for j in order:
            if i < j and readings[i]["side"] != readings[j]["side"] and _vin_relation(readings[i], readings[j]) == "same":
                join(i, j)
    for i in order:
        a = readings[i]
        candidates = []
        for j in order:
            b = readings[j]
            if j == i or a["side"] == b["side"] or find(i) == find(j):
                continue
            if a["vinTail"] and b["vinTail"]:
                continue  # both print a VIN: the VIN decides
            if a["grandTotal"] is None or b["grandTotal"] is None or abs(a["grandTotal"] - b["grandTotal"]) > _ONE_RUPEE:
                continue
            apart = _days_apart(a["date"], b["date"])
            candidates.append((apart if apart is not None else 99999, str(b["documentId"]), j))
        for _, _, j in sorted(candidates):
            if join(i, j):
                break
    for i in order:
        for j in order:
            a, b = readings[i], readings[j]
            if i < j and not a["numberKey"] and not b["numberKey"] and _single_key(a) == _single_key(b):
                join(i, j)
    sets: dict[int, list[dict[str, Any]]] = {}
    for i, reading in enumerate(readings):
        sets.setdefault(find(i), []).append(reading)
    return list(sets.values())


def _other_sets(readings: list[dict[str, Any]]) -> tuple[list[list[dict[str, Any]]], list[tuple[Any, Any]]]:
    """Non-vehicle invoices of one family grouped by number (else date and
    total); then a DMS and a Tally accessory / EW / RSA bill printing
    different numbers but the same total, dated within a day, are one bill
    (review B1). A DMS and a Tally bill that differ stay two, and the PC is
    asked whether they are one bill."""
    by_key: dict[str, list[dict[str, Any]]] = {}
    for reading in readings:
        by_key.setdefault(_single_key(reading), []).append(reading)
    sets = []
    for _, members in sorted(by_key.items()):
        # One OCR key can hold two bills of one system (final check FN2).
        parts: list[list[dict[str, Any]]] = []
        for reading in sorted(members, key=lambda r: str(r["documentId"])):
            home = next((part for part in parts if not any(
                m["side"] and m["side"] == reading["side"] and _same_system_two_bills(m, reading) for m in part)), None)
            if home is None:
                parts.append([reading])
            else:
                home.append(reading)
        sets += [sorted(part, key=_copy_order) for part in parts]
    questions: list[tuple[Any, Any]] = []
    if not readings or readings[0]["family"] not in _FAMILY_COMPONENT:
        return sets, questions

    def side(group: list[dict[str, Any]]) -> set[str]:
        return {r["side"] for r in group if r["side"] in ("DMS", "TALLY")}

    merged: list[list[dict[str, Any]]] = []
    used: set[int] = set()
    for i, a in enumerate(sets):
        if i in used:
            continue
        for j in range(i + 1, len(sets)):
            b = sets[j]
            if j in used or side(a) == side(b) or not side(a) or not side(b) or len(side(a) | side(b)) != 2:
                continue
            if _same_bill(a[0], b[0]):
                a = a + b
                used.add(j)
                break
        merged.append(a)
    for i, a in enumerate(merged):
        for b in merged[i + 1:]:
            if side(a) == {"DMS"} and side(b) == {"TALLY"} or side(a) == {"TALLY"} and side(b) == {"DMS"}:
                questions.append((a, b))
    return merged, questions


def select_invoices(
    readings: list[dict[str, Any]], *, plausible_from: date | None = None, today: date | None = None,
) -> dict[str, Any]:
    """Group the invoices on file into distinct invoices and count each once.

    - Vehicle invoices: see _vehicle_sets (same number, or a DMS and a Tally
      copy of one vehicle, or of one total when one prints no VIN, are one
      bill; one system with two numbers is two invoices). A generic copy
      that prints no purpose joins the vehicle invoice it copies.
    - Accessory / EW / RSA invoices: one number is one bill; a DMS and a
      Tally bill with different numbers are one bill when their totals agree
      to the rupee within a day, else two bills and the PC is asked.
    - The copy that stands for an invoice is the complete one, then the one
      whose own arithmetic holds, then the canonical document-type priority,
      then the document id. Never by upload time.
    - A credit note reversing a whole vehicle invoice cancels it (see
      _mark_cancellations). One car has one vehicle invoice: when more than
      one is still on file, the latest dated one (within the deal's window,
      booking date to today) prices the deal and a Team Lead is asked.
    - The wholesale invoice is kept apart as the dealer's purchase.
    """
    # Fresh copies: grouping marks readings, and a caller's own readings
    # must not carry those marks into another call (review RN3).
    readings = [dict(r) for r in readings]
    _pair_generic_copies(readings)
    families: dict[str, list[dict[str, Any]]] = {}
    for reading in readings:
        families.setdefault(reading["family"], []).append(reading)
    raw_sets: list[list[dict[str, Any]]] = []
    question_sets: list[tuple[Any, Any]] = []
    for family, members in sorted(families.items()):
        if family == FAMILY_VEHICLE:
            raw_sets += _vehicle_sets(members)
        else:
            sets, questions = _other_sets(members)
            raw_sets += sets
            question_sets += questions

    def group_key(copies: list[dict[str, Any]]) -> str:
        family = copies[0]["family"]
        numbers = sorted({r["numberKey"] for r in copies if r["numberKey"]})
        vin = next((r["vinFull"] or r["vinTail"] for r in copies if r["vinTail"]), None)
        if family == FAMILY_VEHICLE and vin:
            return f"{family}|V|{vin}|{'+'.join(numbers)}" if numbers else f"{family}|V|{vin}"
        numbers = sorted({r["numberKey"] for r in copies if r["numberKey"]})
        if numbers:
            exact = sorted({r["numberExact"] for r in copies if r["numberExact"]})
            mine = {str(r["documentId"]) for r in copies}
            twin = any(not ({str(r["documentId"]) for r in g} & mine) and g[0]["family"] == family
                       and {r["numberKey"] for r in g if r["numberKey"]} == set(numbers) for g in raw_sets)
            return f"{family}|N|{'+'.join(numbers)}" + (f"|{'+'.join(exact)}" if twin else "")
        return f"{family}|{_single_key(copies[0])}"

    built: list[dict[str, Any]] = []
    for copies in raw_sets:
        copies = sorted(copies, key=_copy_order)
        rep = copies[0]
        built.append({
            "key": group_key(copies),
            "family": rep["family"],
            "representative": rep,
            "copies": copies,
            "disagreements": _copies_disagree(copies) if len(copies) > 1 else [],
            "cancelled": False,
            "counted": rep["family"] in CUSTOMER_FAMILIES,
        })
    built.sort(key=lambda g: g["key"])
    cancellation_questions = _mark_cancellations(built)
    for group in built:
        if group["cancelled"] or group.get("pendingCancellation"):
            group["counted"] = False
    questions = []
    for a, b in question_sets:
        ga = next(g for g in built if a[0] in g["copies"])
        gb = next(g for g in built if b[0] in g["copies"])
        questions.append((ga, gb))
    vehicle_groups = [g for g in built if g["family"] == FAMILY_VEHICLE and not g["cancelled"]]
    vehicle = None
    if vehicle_groups:
        window_to = (today or datetime.now(UTC).date()).isoformat()
        window_from = plausible_from.isoformat() if plausible_from else "0000"

        def vehicle_rank(group: dict[str, Any]) -> tuple[Any, ...]:
            rep = group["representative"]
            complete = not any(i["code"] in _BLOCKING_ISSUES for i in rep["issues"])
            # A date misread by years (2024 for 2026) never decides which
            # invoice is the latest: only dates inside the deal's window
            # (booking date to today) count (live data, 2026-10-21).
            dated = [c["date"] for c in group["copies"] if c["date"] and window_from <= c["date"] <= window_to]
            return (complete, max(dated, default=""))

        # A complete reading first, then the latest invoice date (a reissue
        # replaces the earlier invoice); on a tie, the better copy (review N1).
        ordered = sorted(vehicle_groups, key=lambda g: _copy_order(g["representative"]))
        vehicle = max(ordered, key=vehicle_rank)
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
        "cancelled": [g for g in built if g["cancelled"]],
        "sameBillQuestions": questions,
        "cancellationQuestions": cancellation_questions,
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
def plausible_from(connection: Connection, *, tenant_id: str, journey_id: UUID) -> date | None:
    """The earliest date a vehicle invoice of this deal can carry: the
    booking date on the booking form (the PC's correction first), when read
    -- a year misread on an invoice falls outside. The one source the Deal,
    the canonical tables and the receipts check all use (final check FN1)."""
    try:
        from audit_core.uc03_p2_dates import booking_form_date

        form = booking_form_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    except Exception:  # never let a date lookup stop materialisation
        logger.warning("uc03_invoice_booking_date_unavailable", exc_info=True)
        return None
    return (form or {}).get("date")


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


def _document_on_file(connection: Connection, *, tenant_id: str, journey_id: UUID, document_id: str) -> bool:
    """True unless the document's evidence on this journey is no longer
    ACTIVE (voided, superseded, merged)."""
    try:
        document = UUID(document_id)
    except ValueError:
        return False
    return not connection.execute(
        text(
            """
            SELECT EXISTS (
              SELECT 1 FROM auditcore.evidence e
              WHERE e.tenant_id=:tenant_id AND e.journey_id=:journey_id AND e.di_document_id=:document_id
                AND e.association_status <> 'ACTIVE')
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "document_id": document},
    ).scalar_one()


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
    selection = select_invoices(readings, plausible_from=plausible_from(
        connection, tenant_id=tenant_id, journey_id=journey_id))
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
    reviewed = {str(r["documentId"]) for r in readings}
    cleared = 0
    for row in held:
        holder = _reference_document_type(row["source_reference"])
        if holder not in _INVOICE_SOURCE_TYPES or row["component_key"] in selection["lines"]:
            continue
        holder_id = str(row["source_reference"]).split(":", 1)[1]
        # Cleared only when that invoice is gone (voided / superseded), is
        # the wholesale invoice, or was read here and no longer bills the
        # line -- never merely because a value written for a document not
        # yet confirmed has no review row (that flip-flopped with the booking
        # form on every pass; final check FS1).
        if holder != "wholesale_invoice" and holder_id not in reviewed and _document_on_file(
                connection, tenant_id=tenant_id, journey_id=journey_id, document_id=holder_id):
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
        if group["cancelled"] or (not group["counted"] and group["family"] != FAMILY_VEHICLE):
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
    for first, second in selection.get("sameBillQuestions", []):
        dms, tally = (first, second) if first["representative"]["side"] == "DMS" else (second, first)
        label = {FAMILY_ACCESSORY: "accessory", FAMILY_EXTENDED_WARRANTY: "extended warranty",
                 FAMILY_RSA: "RSA"}.get(first["family"], "")
        rule_key = f"INVOICE_SAME_BILL_QUESTION:{dms['key']}~{tally['key']}"
        d, t = dms["representative"], tally["representative"]
        wanted[rule_key] = {
            "title": f"Two {label} invoices (DMS {d['number'] or '-'}, Tally {t['number'] or '-'}): "
                     "same bill or two bills?",
            "description": (
                f"DMS {d['number'] or '(no number)'} for {d['grandTotal']} dated {d['date']} and Tally "
                f"{t['number'] or '(no number)'} for {t['grandTotal']} dated {t['date']} print different numbers "
                "and totals, so both are counted. If they are one bill, void the wrong copy; if two, confirm."
            ),
            "payload": {"documentIds": [d["documentId"], t["documentId"]], "family": first["family"]},
            "role": "PC", "task_type": _FIELD_REVIEW_TASK_TYPE, "severity": "MEDIUM",
        }
    for note_group, vehicle_group in selection.get("cancellationQuestions", []):
        note, vehicle = note_group["representative"], vehicle_group["representative"]
        rule_key = f"INVOICE_CANCELLATION_QUESTION:{note_group['key']}~{vehicle_group['key']}"
        wanted[rule_key] = {
            "title": f"Does credit note {note['number'] or '-'} cancel invoice {vehicle['number'] or '-'}?",
            "description": (
                f"Credit note {note['number'] or '(no number)'} gives back {note['grandTotal']}, the whole of vehicle "
                f"invoice {vehicle['number'] or '(no number)'}, but names neither that invoice nor its VIN. It is "
                "held back (not counted as a discount) until confirmed: if it cancels the invoice, void the invoice; "
                "if it is a discount, correct its narration."
            ),
            "payload": {"documentIds": [note["documentId"], vehicle["documentId"]]},
            "role": "PC", "task_type": _FIELD_REVIEW_TASK_TYPE, "severity": "HIGH",
        }
    if selection["multipleVehicle"]:
        vehicles = [g["representative"] for g in selection["groups"] if g["family"] == FAMILY_VEHICLE]
        chosen = selection["vehicle"]["representative"]
        wanted[_MULTIPLE_VEHICLE_RULE] = {
            "title": "More than one vehicle invoice on file",
            "description": (
                "These vehicle invoices have different invoice numbers: "
                + ", ".join(_invoice_name(r) for r in vehicles)
                + f". The deal is priced on {_invoice_name(chosen)} (the latest); the receipts-vs-invoices check "
                "counts every one of them until the cancelled one is voided or reversed by a credit note. "
                "Void the cancelled invoice, or confirm both were issued."
            ),
            "payload": {"documentIds": [r["documentId"] for r in vehicles], "pricedOn": chosen["documentId"]},
            "role": "TL", "task_type": _DISCREPANCY_TASK_TYPE, "severity": "HIGH",
        }

    for rule_key, spec in sorted(wanted.items()):
        _flag(connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=_INVOICE_FINDING_STAGE,
              correlation_id=correlation_id, rule_key=rule_key, title=spec["title"],
              description=spec["description"], payload=spec["payload"], role=spec["role"],
              task_type=spec["task_type"], severity=spec["severity"])

    open_rows = connection.execute(
        text(
            """
            SELECT audit_finding_id, rule_key, stage_code FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
              AND (rule_key LIKE :check OR rule_key LIKE :copies OR rule_key = :multiple OR rule_key LIKE :legacy
                   OR rule_key LIKE :question OR rule_key LIKE :cancellation)
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "check": f"{_CHECK_RULE_PREFIX}%",
         "copies": f"{_COPIES_RULE_PREFIX}%", "multiple": _MULTIPLE_VEHICLE_RULE,
         "legacy": f"{_LEGACY_DISAGREEMENT_PREFIX}%", "question": "INVOICE_SAME_BILL_QUESTION:%",
         "cancellation": "INVOICE_CANCELLATION_QUESTION:%"},
    ).mappings().all()
    resolved = 0
    for row in open_rows:
        if row["rule_key"] in wanted and row["stage_code"] == _INVOICE_FINDING_STAGE:
            continue
        # A finding no longer shown, or the same problem raised earlier under
        # the other stage key: closed (its task is cancelled with it).
        _resolve_finding(
            connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=row["stage_code"] or stage_code,
            finding_id=row["audit_finding_id"], actor_id=None, correlation_id=correlation_id,
            note=("The invoices on file no longer show this problem." if row["rule_key"] not in wanted
                  else "Merged into the journey's one finding for this invoice problem."),
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
        selection = select_invoices(
            load_invoice_readings(connection, tenant_id=tenant_id, journey_id=journey_id),
            plausible_from=plausible_from(connection, tenant_id=tenant_id, journey_id=journey_id))
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
    "ocr_number_key",
    "plausible_from",
    "read_invoice",
    "select_invoices",
    "sync_invoice_actuals",
    "sync_invoice_checks",
    "vehicle_invoice_ex_showroom",
]
