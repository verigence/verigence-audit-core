"""Journey export to Excel for Team Leads and above (product owner 2026-11-06).

"I would like to give an option to TL and above to download all the journey as
excel for given date range or dealerwise or outlet wise or all ... fields which i
want in the excel are as following; some of the field like DMS related may not be
available but we should have it in future."

The columns are the ones of his Deal Audit export (``Deal-Audit-Export_*.xlsx``,
192 columns, in that order): who and what the journey is, then the deal at the
Standard (price masters), Actual (the documents) and the Variance between them,
charge by charge and discount by discount, then what was to be received and
what was received.

One source per figure. Every amount is the Deal's own (``uc03_p2_journey360.deal``,
what Journey 360's Deal tab shows): the export never computes a figure the Deal
does not, so the two cannot disagree. The variance is blank until delivery has
started, as on the screen (product owner 2026-10-28). Customer contact numbers
are masked to their last four digits as everywhere else, and the Aadhaar number
to its last four (the full number is never exported).

A column Verigence does not capture yet is blank (not 0) and named as such on
the workbook's "Columns" sheet; a column it captures that is absent on a journey
is 0 (amounts) or blank (facts).

An export is a job (``p2_journey_exports``): requested here, built by the P2
worker one journey at a time (about a second each, the Deal being the cost),
stored in the document bucket and downloaded through a short-lived link.
"""
from __future__ import annotations

import io
import json
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, model_validator
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import authorize_p2

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-journey-export"])

EXPORT_ROLES = frozenset({"TL", "PM", "EXECUTIVE"})
MAX_RANGE_DAYS = 92
_READ = "audit.journey.read"
_LINK_SECONDS = 1800
# What a difference of up to Rs 10 either way is: none (the Audit Summary's tolerance).
_TOLERANCE = Decimal(10)

# ── the columns ───────────────────────────────────────────────────────────────

IDENTITY_COLUMNS = (
    "Dealership", "Location", "Booking No", "Booking Date", "Booking Intimation", "Delivery Date",
    "DMS Invoice Date", "DMS Invoice No", "Customer Name", "Customer Type", "Customer Contact No",
    "VIN / Chassis", "Model", "Variant", "Fuel Type", "Transmission", "Deal Type", "Deal Source",
    "Finance Type", "Bank", "Insurance Type", "Insurance Company", "Insurance Agent Code", "Reg Type",
    "Reg State", "SC Name", "SC Contact No", "File Status", "Review Status", "Reviewed By", "Reviewed At",
    "Aadhaar No", "PAN No", "GST No", "Pincode",
)

# Charge heads, in the export's order, with the Deal line keys each one holds.
# A head with no keys is not captured by Verigence yet (blank in the export).
CHARGE_HEADS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Ex Showroom", ("ex_showroom_price",)),
    ("Registration Type", ("road_tax_registration", "road_tax_amount", "registration_charges")),
    ("Accessories Taken", ()),
    ("Essential Kit", ("accessories_cost", "essential_kit_amount")),
    ("Maintenance Package CLASSIC 3 YRS", ()),
    ("Maintenance Package PREMIUM 3 YRS", ()),
    ("Maintenance Package ELITE 3 YRS", ()),
    ("Maintenance Package CLASSIC 5 YRS", ()),
    ("Maintenance Package PREMIUM 5 YRS", ()),
    ("Maintenance Package ELITE 5 YRS", ()),
    ("Genuine Accessory", ("genuine_accessories_amount",)),
    ("Non Genuine with OEM", ()),
    ("Non Genuine", ("non_genuine_accessories_amount",)),
    ("OBD / Body Décor Charges", ()),
    ("Insurance", ("insurance_amount",)),
    ("Fasttag Taken", ("fastag_amount",)),
    ("RSA", ("rsa_amount",)),
    ("TCS", ("tcs_amount",)),
    ("EW", ("additional_warranty_amount", "extended_warranty_amount")),
    ("Green Tax", ("green_tax_amount",)),
    ("Service Package", ("service_package_amount",)),
    ("Scheme Accessories (Free)", ()),
    ("Scheme Cashback", ()),
    ("Smart Finance / Subvention", ()),
    # Every other charge the Deal counts (handling charges, other charges ...),
    # so the heads add up to the Deal's total.
    ("Other Charges", ("*",)),
    ("HP Charges", ("hp_charges", "hypothecation_charges")),
    # The on-road price: the Deal's own total of the charges.
    ("Net Amount (On-Road)", ("=gross",)),
)

DISCOUNT_HEADS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Sales Discount", ("CASH_DISCOUNT",)),
    ("Buffer Discount", ("ADDITIONAL_DISCOUNT",)),
    ("Exchange Discount Taken", ("EXCHANGE_BONUS", "SCRAPPAGE_BONUS_DEALER", "SCRAPPAGE_BONUS_COD")),
    ("Corporate Discount Taken", ("CORPORATE_PRIVILEGE",)),
    ("Loyalty Discount", ("WELCOME_BONUS",)),
    ("Inhouse insurance discount", ("INSURANCE",)),
    ("MR Discount", ("MANAGEMENT_REFERRAL",)),
    ("OEM Referral", ()),
    # Every other discount the Deal counts (other scheme, an invoice or cost
    # sheet discount whose type is not printed ...).
    ("Other Discount", ("*",)),
    ("Accessories Discount", ()),
    ("Spin the wheel", ()),
    ("Ageing discount", ()),
    ("Vintage Scheme", ()),
    ("Buffer with self insurance or T-60% OD OEM portal", ()),
    ("Tenders or Bulk Deals", ()),
    ("DSA Commission and Discount with Portal Insurance", ()),
    ("DSA Commission and Discount with Self Insurance", ()),
    ("Referral Bonus", ()),
    ("FOC Accessories Discount (%)", ("ACCESSORIES_KIT",)),
    ("Buyback Offer", ()),
    ("Scheme: EW + RSA", ("EXT_WARRANTY_4TH_YR", "EXT_WARRANTY_4TH_5TH_YR")),
)

PAYMENT_COLUMNS = ("Total Amt To Be Received", "Total Amt Received", "Payment Variance", "Payment Status")


def columns() -> list[str]:
    """Every column, in the order of the Deal Audit export."""
    charges = [label for label, _ in CHARGE_HEADS]
    discounts = [label for label, _ in DISCOUNT_HEADS]
    return [
        *IDENTITY_COLUMNS,
        *(f"Std: {c}" for c in charges), "Total Standard Deal",
        *(f"Std Disc: {d}" for d in discounts), "Total Standard Discount", "Net Standard Deal",
        *(f"Act: {c}" for c in charges), "Total Actual Deal",
        *(f"Act Disc: {d}" for d in discounts), "Total Actual Discount", "Net Actual Deal",
        *(f"Var: {c}" for c in charges),
        *(f"Var Disc: {d}" for d in discounts),
        "Deal Variance", "Discount Variance", "Net Variance",
        *PAYMENT_COLUMNS,
    ]


# Where each identity column comes from, for the "Columns" sheet. None: not
# captured by Verigence yet.
IDENTITY_SOURCES: dict[str, str | None] = {
    "Dealership": "The journey's dealership",
    "Location": "The outlet's name and address",
    "Booking No": "Booking form / docket (booking reference number)",
    "Booking Date": "Booking form / docket (booking date)",
    "Booking Intimation": "Booking stage intimation date, when recorded",
    "Delivery Date": "Delivery recorded on the journey, else the vehicle gate pass date",
    "DMS Invoice Date": "Customer invoice (DMS) date",
    "DMS Invoice No": "Customer invoice (DMS) number",
    "Customer Name": "KYC name (PAN / Aadhaar), else the name entered at booking",
    "Customer Type": "Customer record",
    "Customer Contact No": "Customer mobile, masked to its last four digits",
    "VIN / Chassis": "Vehicle record (VIN, else chassis number)",
    "Model": "Matched price-master SKU",
    "Variant": "Matched price-master SKU",
    "Fuel Type": "Price-master variant / configuration",
    "Transmission": "Price-master variant / configuration",
    "Deal Type": "Booking record (deal type), when recorded",
    "Deal Source": "Booking record (deal source), when recorded",
    "Finance Type": "Finance record (loan / cash)",
    "Bank": "Finance record (financier)",
    "Insurance Type": "The Deal's insurance decision (in-house / self / not confirmed)",
    "Insurance Company": "Insurance policy (insurer)",
    "Insurance Agent Code": "Insurance policy (agent / intermediary code)",
    "Reg Type": "Registration record (type)",
    "Reg State": "Registration record (state)",
    "SC Name": "Booking form (sales person), else the dealership staff record",
    "SC Contact No": None,
    "File Status": "Journey stage (booking / delivery / closed by the Reviewer)",
    "Review Status": "Reviewer's journey closure and the Team Lead's acceptance",
    "Reviewed By": "Reviewer who closed the journey",
    "Reviewed At": "When the Reviewer closed the journey (IST)",
    "Aadhaar No": "KYC (Aadhaar), masked to its last four digits",
    "PAN No": "KYC (PAN)",
    "GST No": "GST certificate, else the buyer GSTIN on the invoice",
    "Pincode": "KYC address pincode",
}


def column_notes() -> list[tuple[str, str]]:
    """Each column with where it comes from, or that it is not captured yet."""
    not_yet = "Not captured by Verigence yet: blank. Kept so the layout stays the same once it is."
    notes: list[tuple[str, str]] = [(c, IDENTITY_SOURCES.get(c) or not_yet) for c in IDENTITY_COLUMNS]
    for prefix, heads, what in (("Std", CHARGE_HEADS, "standard"), ("Act", CHARGE_HEADS, "actual"),
                                ("Var", CHARGE_HEADS, "variance")):
        for label, keys in heads:
            notes.append((f"{prefix}: {label}", not_yet if not keys else _head_note(keys, what, charge=True)))
    for prefix, what in (("Std Disc", "standard"), ("Act Disc", "actual"), ("Var Disc", "variance")):
        for label, keys in DISCOUNT_HEADS:
            notes.append((f"{prefix}: {label}", not_yet if not keys else _head_note(keys, what, charge=False)))
    notes += [
        ("Total Standard Deal", "The Deal's charges at the standard"),
        ("Total Standard Discount", "The Deal's discounts at the entitlement"),
        ("Net Standard Deal", "Standard charges less standard discounts"),
        ("Total Actual Deal", "The Deal's charges as billed"),
        ("Total Actual Discount", "The Deal's discounts as given"),
        ("Net Actual Deal", "Actual charges less actual discounts"),
        ("Deal Variance", "Actual charges less standard charges (blank until delivery has started)"),
        ("Discount Variance", "Standard discounts less actual discounts (blank until delivery has started)"),
        ("Net Variance", "Net actual less net standard (blank until delivery has started)"),
        ("Total Amt To Be Received", "The Deal's net payable (blank until a bill charges the vehicle's price)"),
        ("Total Amt Received", "Receipts, the loan disbursed and the old vehicle taken in, less refunds"),
        ("Payment Variance", "Received less to be received"),
        ("Payment Status", "Matched (within Rs 10), Excess, Short, Loan not on file, or No amount data"),
    ]
    return notes


def _head_note(keys: tuple[str, ...], what: str, *, charge: bool) -> str:
    if keys == ("=gross",):
        return f"The Deal's total of the charges ({what})"
    if keys == ("*",):
        return f"Every other {'charge' if charge else 'discount'} the Deal counts ({what})"
    sign = ("actual less standard" if charge else "standard less actual") if what == "variance" else what
    return f"Deal lines {', '.join(keys)} ({sign}; variance blank until delivery has started)" \
        if what == "variance" else f"Deal lines {', '.join(keys)} ({sign})"


# ── one journey's row ─────────────────────────────────────────────────────────

def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _num(value: Decimal | None) -> float | int | None:
    if value is None:
        return None
    rounded = value.quantize(Decimal("0.01"))
    return int(rounded) if rounded == rounded.to_integral_value() else float(rounded)


def _head_of(key: str, heads: tuple[tuple[str, tuple[str, ...]], ...]) -> str:
    rest = None
    for label, keys in heads:
        if key in keys:
            return label
        if keys == ("*",):
            rest = label
    assert rest is not None
    return rest


def deal_amounts(deal: dict[str, Any]) -> dict[str, Any]:
    """The Std / Act / Var columns and the totals of one journey, from the Deal
    exactly as Journey 360 shows it (``uc03_p2_journey360.deal``)."""
    summary = deal.get("summary") or {}
    delivery_started = summary.get("deliveryStarted") is not False
    taken_as_charged = set(summary.get("standardTakenAsCharged") or [])
    std: dict[str, Decimal] = {}
    act: dict[str, Decimal] = {}

    def add(bucket: dict[str, Decimal], head: str, amount: Decimal | None) -> None:
        if amount is not None:
            bucket[head] = bucket.get(head, Decimal(0)) + amount

    for group in deal.get("categories") or []:
        for line in group.get("components") or []:
            # A part of a folded line (road tax inside "road tax + registration")
            # is in its parent already; an excluded line is not counted.
            if line.get("partOf") or line.get("excluded"):
                continue
            head = _head_of(str(line.get("key") or ""), CHARGE_HEADS)
            current = _dec(line.get("current"))
            standard = _dec(line.get("standard"))
            if standard is None and line.get("key") in taken_as_charged:
                standard = current
            add(std, head, standard)
            add(act, head, current)
    std_d: dict[str, Decimal] = {}
    act_d: dict[str, Decimal] = {}
    for row in deal.get("discounts") or []:
        head = _head_of(str(row.get("key") or ""), DISCOUNT_HEADS)
        current = _dec(row.get("current"))
        entitled = _dec(row.get("entitled")) if row.get("inStandard") else None
        if entitled is None and row.get("takenAsGiven"):
            entitled = current
        add(std_d, head, entitled)
        add(act_d, head, current)

    gross, discounts, net = (summary.get(k) or {} for k in ("gross", "discounts", "net"))
    std_gross, act_gross = _dec(gross.get("standard")), _dec(gross.get("current"))
    out: dict[str, Any] = {}
    for label, keys in CHARGE_HEADS:
        if keys == ("=gross",):
            s, a = std_gross, act_gross
        elif not keys:
            s = a = None
        else:
            s, a = std.get(label, Decimal(0)), act.get(label, Decimal(0))
        out[f"Std: {label}"], out[f"Act: {label}"] = _num(s), _num(a)
        out[f"Var: {label}"] = _num(a - s) if delivery_started and s is not None and a is not None else None
    for label, keys in DISCOUNT_HEADS:
        s, a = (None, None) if not keys else (std_d.get(label, Decimal(0)), act_d.get(label, Decimal(0)))
        out[f"Std Disc: {label}"], out[f"Act Disc: {label}"] = _num(s), _num(a)
        out[f"Var Disc: {label}"] = _num(s - a) if delivery_started and s is not None and a is not None else None
    std_disc, act_disc = _dec(discounts.get("standard")), _dec(discounts.get("current"))
    out["Total Standard Deal"], out["Total Actual Deal"] = _num(std_gross), _num(act_gross)
    out["Total Standard Discount"], out["Total Actual Discount"] = _num(std_disc), _num(act_disc)
    out["Net Standard Deal"], out["Net Actual Deal"] = _num(_dec(net.get("standard"))), _num(_dec(net.get("current")))
    variance = (summary.get("variance") or {}).get("currentVsStandard")
    out["Deal Variance"] = (_num(act_gross - std_gross)
                            if delivery_started and act_gross is not None and std_gross is not None else None)
    out["Discount Variance"] = (_num(std_disc - act_disc)
                                if delivery_started and std_disc is not None and act_disc is not None else None)
    out["Net Variance"] = _num(_dec(variance)) if delivery_started else None

    payable = _dec(summary.get("payable"))
    received = _dec((summary.get("paid") or {}).get("total")) or Decimal(0)
    out["Total Amt To Be Received"] = _num(payable)
    out["Total Amt Received"] = _num(received)
    if payable is None:
        out["Payment Variance"], out["Payment Status"] = None, "No amount data"
    else:
        gap = received - payable
        out["Payment Variance"] = _num(gap)
        if (summary.get("paid") or {}).get("loanUnknown"):
            out["Payment Status"] = "Loan not on file"
        elif abs(gap) <= _TOLERANCE:
            out["Payment Status"] = "Matched"
        else:
            out["Payment Status"] = "Excess" if gap > 0 else "Short"
    return out


def _masked_aadhaar(value: Any) -> str | None:
    digits = "".join(c for c in str(value or "") if c.isdigit())
    return f"XXXX XXXX {digits[-4:]}" if len(digits) >= 4 else None


def _ist(value: Any) -> str | None:
    if isinstance(value, datetime):
        from zoneinfo import ZoneInfo

        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return str(value) if value not in (None, "") else None


def _date_text(value: Any) -> str | None:
    if isinstance(value, datetime):
        shown = _ist(value)
        return shown[:10] if shown else None
    if isinstance(value, date):
        return value.isoformat()
    text_ = str(value or "").strip()
    return text_ or None


_DMS_INVOICE_TYPES = ("customer_invoice_dms", "customer_invoice_dms_v2", "tax_invoice_dms")
_CLOSURE_STATUS = {"PENDING": "Closed by Reviewer (awaiting TL)", "ACCEPTED": "Closed and accepted by TL",
                   "AUTO_ACCEPTED": "Closed, auto-accepted", "REJECTED": "Closure rejected by TL"}


def identity_row(base: dict[str, Any], *, deal: dict[str, Any], vehicle: dict[str, Any],
                 customer: dict[str, Any]) -> dict[str, Any]:
    """The identity and status columns of one journey: ``base`` is its row of
    ``scope_rows``; ``deal``, ``vehicle`` and ``customer`` are its Journey 360
    sections."""
    fields = {f["key"]: f.get("value") for f in customer.get("fields") or []}
    booking = vehicle.get("booking") or {}
    product = vehicle.get("product") or {}
    sku = deal.get("sku") or {}
    units = vehicle.get("units") or []
    unit = units[0] if units else {}
    delivered = (vehicle.get("delivery") or {}).get("actual_delivered_at")
    dms: dict[str, Any] = next(
        (i for i in deal.get("invoices") or [] if i.get("documentType") in _DMS_INVOICE_TYPES), {})
    insurance = deal.get("insurance") or {}
    insurance_type = {"SELF": "Self", "INHOUSE": "In-house" if insurance.get("inDeal") else "In-house (not confirmed)"}
    closure_status = base.get("closure_status")
    location = ", ".join(p for p in (base.get("outlet_name"), base.get("outlet_address")) if p)
    return {
        "Dealership": base.get("dealer_name"),
        "Location": location or None,
        "Booking No": booking.get("bookingReference") or base.get("booking_reference") or base.get("journey_reference"),
        "Booking Date": _date_text(booking.get("bookingDate") or base.get("booking_date")),
        "Booking Intimation": _date_text(base.get("intimation_date")),
        "Delivery Date": _date_text(delivered or base.get("gate_pass_delivery_date")),
        "DMS Invoice Date": _date_text(dms.get("date")),
        "DMS Invoice No": dms.get("number"),
        "Customer Name": fields.get("legalName") or fields.get("enteredName"),
        "Customer Type": fields.get("customerType"),
        "Customer Contact No": fields.get("mobile"),
        "VIN / Chassis": unit.get("vin") or unit.get("chassis_number") or base.get("vin"),
        "Model": sku.get("model") or product.get("model"),
        "Variant": sku.get("variant") or product.get("variant"),
        "Fuel Type": base.get("fuel_type"),
        "Transmission": base.get("transmission"),
        "Deal Type": booking.get("dealType"),
        "Deal Source": booking.get("dealSource"),
        "Finance Type": base.get("finance_type"),
        "Bank": base.get("financier"),
        "Insurance Type": insurance_type.get(str(insurance.get("source") or "")),
        "Insurance Company": base.get("insurer"),
        "Insurance Agent Code": base.get("agent_code"),
        "Reg Type": base.get("registration_type"),
        "Reg State": base.get("registration_state"),
        "SC Name": booking.get("salesConsultant"),
        "SC Contact No": None,
        "File Status": base.get("file_status"),
        "Review Status": _CLOSURE_STATUS.get(str(closure_status), "Not reviewed") if closure_status else "Not reviewed",
        "Reviewed By": base.get("closed_by_name"),
        "Reviewed At": _ist(base.get("closed_at_utc")),
        "Aadhaar No": _masked_aadhaar(fields.get("aadhaar")),
        "PAN No": fields.get("pan"),
        "GST No": base.get("gst_no"),
        "Pincode": fields.get("pincode"),
    }


# ── which journeys ───────────────────────────────────────────────────────────

DateBasis = Literal["BOOKING", "DELIVERY", "UPLOADED"]


class ExportRequest(BaseModel):
    dateBasis: DateBasis = "BOOKING"
    dateFrom: date | None = None
    dateTo: date | None = None
    dealerId: UUID | None = None
    outletId: UUID | None = None

    @model_validator(mode="after")
    def _range(self) -> ExportRequest:
        if (self.dateFrom is None) != (self.dateTo is None):
            raise ValueError("Give both dates, or neither for every date.")
        if self.dateFrom and self.dateTo:
            if self.dateTo < self.dateFrom:
                raise ValueError("The end date is before the start date.")
            if (self.dateTo - self.dateFrom).days > MAX_RANGE_DAYS:
                raise ValueError(f"Choose at most {MAX_RANGE_DAYS} days at a time.")
        return self


def supervised_outlets(connection: Connection, *, tenant_id: str, actor_id: str) -> tuple[list[str], str | None]:
    """The outlets the person holds TL, PM or Executive on (their business
    assignments; a PM or Executive assigned without a dealership covers every
    outlet), and the highest of those roles -- the same rule as the supervisors'
    gate pass views."""
    from audit_core.uc03_p2_gate_pass import scope_outlets

    outlets, roles = [], set()
    for outlet in scope_outlets(connection, tenant_id, actor_id):
        held = {str(r).upper() for r in outlet["roles"] or []} & EXPORT_ROLES
        if held:
            outlets.append(str(outlet["outlet_id"]))
            roles |= held
    top = next((r for r in ("EXECUTIVE", "PM", "TL") if r in roles), None)
    return outlets, top


_SCOPE_SQL = """
WITH base AS (
    SELECT j.journey_id, j.journey_reference, j.created_at_utc, d.dealer_name, o.outlet_name,
           o.address_text AS outlet_address, b.booking_reference, b.booking_date,
           COALESCE(dv.actual_delivered_at::date, gp.delivery_date) AS delivery_day
    FROM auditcore.journeys j
    JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
    JOIN auditcore.dealer_outlets o ON o.tenant_id=j.tenant_id AND o.outlet_id=j.outlet_id
    LEFT JOIN auditcore.bookings b ON b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id
    LEFT JOIN auditcore.deliveries dv ON dv.tenant_id=j.tenant_id AND dv.journey_id=j.journey_id
    LEFT JOIN LATERAL (
        SELECT CAST(f.effective_value #>> '{}' AS date) AS delivery_date
        FROM auditcore.journey_document_extracted_fields f
        JOIN auditcore.evidence e ON e.tenant_id=f.tenant_id AND e.journey_id=f.journey_id
         AND e.di_document_id=f.di_document_id AND e.association_status='ACTIVE'
        WHERE f.tenant_id=j.tenant_id AND f.journey_id=j.journey_id AND f.field_key='delivery_date'
          AND COALESCE(e.document_type_key, f.source_document_type_key)='gate_pass'
          AND (f.effective_value #>> '{}') ~ '^\\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\\d|3[01])$'
        ORDER BY f.updated_at_utc DESC LIMIT 1
    ) gp ON true
    WHERE j.tenant_id=:t
      AND (CAST(:dealer AS uuid) IS NULL OR j.dealer_id=CAST(:dealer AS uuid))
      AND (CAST(:outlet AS uuid) IS NULL OR j.outlet_id=CAST(:outlet AS uuid))
      AND (CAST(:outlets AS uuid[]) IS NULL OR j.outlet_id = ANY(CAST(:outlets AS uuid[])))
)
SELECT base.* FROM base
WHERE CAST(:date_from AS date) IS NULL OR (
    CASE :basis
        WHEN 'BOOKING' THEN base.booking_date
        WHEN 'DELIVERY' THEN base.delivery_day
        ELSE (base.created_at_utc AT TIME ZONE 'Asia/Kolkata')::date
    END BETWEEN CAST(:date_from AS date) AND CAST(:date_to AS date))
ORDER BY base.booking_date NULLS LAST, base.dealer_name, base.outlet_name, base.journey_reference
"""


def journeys_in_scope(connection: Connection, *, tenant_id: str, filters: dict[str, Any],
                      outlet_ids: list[str] | None) -> list[dict[str, Any]]:
    if outlet_ids is not None and not outlet_ids:
        return []
    rows = connection.execute(
        text(_SCOPE_SQL),
        {"t": tenant_id, "dealer": filters.get("dealerId"), "outlet": filters.get("outletId"),
         "outlets": outlet_ids, "basis": filters.get("dateBasis") or "BOOKING",
         "date_from": filters.get("dateFrom"), "date_to": filters.get("dateTo")},
    ).mappings().all()
    return [dict(r) for r in rows]


def journey_facts(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """The facts of a journey the Journey 360 sections the export reads do not
    carry: master fuel and transmission, finance, insurance, registration, the
    Reviewer's closure, GST number and the booking stage."""
    row = connection.execute(
        text(
            """
            SELECT
              (SELECT COALESCE(pc.fuel_powertrain, pv.fuel_powertrain)
                 FROM auditcore.journey_products jp
                 JOIN auditcore.product_skus s ON s.product_sku_id=jp.product_sku_id
                 LEFT JOIN auditcore.product_configurations pc ON pc.configuration_id=s.configuration_id
                 LEFT JOIN auditcore.product_variants pv ON pv.variant_id=s.variant_id
                WHERE jp.tenant_id=:t AND jp.journey_id=:j LIMIT 1) AS fuel_type,
              (SELECT COALESCE(pc.transmission, pv.transmission)
                 FROM auditcore.journey_products jp
                 JOIN auditcore.product_skus s ON s.product_sku_id=jp.product_sku_id
                 LEFT JOIN auditcore.product_configurations pc ON pc.configuration_id=s.configuration_id
                 LEFT JOIN auditcore.product_variants pv ON pv.variant_id=s.variant_id
                WHERE jp.tenant_id=:t AND jp.journey_id=:j LIMIT 1) AS transmission,
              (SELECT f.finance_type_code FROM auditcore.finance_records f
                WHERE f.tenant_id=:t AND f.journey_id=:j ORDER BY f.updated_at_utc DESC LIMIT 1) AS finance_type,
              (SELECT f.provider_name FROM auditcore.finance_records f
                WHERE f.tenant_id=:t AND f.journey_id=:j ORDER BY f.updated_at_utc DESC LIMIT 1) AS financier,
              (SELECT i.insurer_name FROM auditcore.insurance_records i
                WHERE i.tenant_id=:t AND i.journey_id=:j ORDER BY i.updated_at_utc DESC LIMIT 1) AS insurer,
              (SELECT i.agent_intermediary_code FROM auditcore.insurance_records i
                WHERE i.tenant_id=:t AND i.journey_id=:j ORDER BY i.updated_at_utc DESC LIMIT 1) AS agent_code,
              (SELECT r.registration_type_code FROM auditcore.registration_records r
                WHERE r.tenant_id=:t AND r.journey_id=:j ORDER BY r.updated_at_utc DESC LIMIT 1) AS registration_type,
              (SELECT r.registration_state FROM auditcore.registration_records r
                WHERE r.tenant_id=:t AND r.journey_id=:j ORDER BY r.updated_at_utc DESC LIMIT 1) AS registration_state,
              (SELECT v.vin FROM auditcore.p2_vehicle_identifications v
                WHERE v.tenant_id=:t AND v.journey_id=:j ORDER BY v.created_at_utc DESC LIMIT 1) AS vin,
              (SELECT st.intimation_date FROM auditcore.journey_stage_states st
                WHERE st.tenant_id=:t AND st.journey_id=:j AND st.stage_code='BOOKING') AS intimation_date,
              (SELECT CASE
                        WHEN EXISTS (SELECT 1 FROM auditcore.p2_journey_closures c
                                     WHERE c.tenant_id=:t AND c.journey_id=:j) THEN 'Closed by Reviewer'
                        WHEN bool_or(st.stage_code='DELIVERY' AND st.business_completed_at_utc IS NOT NULL)
                          THEN 'Delivery complete'
                        WHEN bool_or(st.stage_code='DELIVERY' AND st.first_started_at_utc IS NOT NULL)
                          THEN 'Delivery in progress'
                        WHEN bool_or(st.stage_code='BOOKING' AND st.business_completed_at_utc IS NOT NULL)
                          THEN 'Booking complete'
                        ELSE 'Booking in progress' END
                 FROM auditcore.journey_stage_states st WHERE st.tenant_id=:t AND st.journey_id=:j) AS file_status,
              c.acceptance_status AS closure_status, c.closed_at_utc,
              pn.display_name AS closed_by_name,
              (SELECT f.effective_value #>> '{}'
                 FROM auditcore.journey_document_extracted_fields f
                 JOIN auditcore.evidence e ON e.tenant_id=f.tenant_id AND e.journey_id=f.journey_id
                  AND e.di_document_id=f.di_document_id AND e.association_status='ACTIVE'
                WHERE f.tenant_id=:t AND f.journey_id=:j AND f.field_key IN ('gstin', 'buyer_gstin')
                  AND NULLIF(btrim(f.effective_value #>> '{}'), '') IS NOT NULL
                ORDER BY (f.field_key='gstin') DESC, f.updated_at_utc DESC LIMIT 1) AS gst_no
            FROM (SELECT 1) one
            LEFT JOIN LATERAL (
                SELECT acceptance_status, closed_at_utc, closed_by_actor_id FROM auditcore.p2_journey_closures
                WHERE tenant_id=:t AND journey_id=:j ORDER BY closed_at_utc DESC LIMIT 1
            ) c ON true
            LEFT JOIN auditcore.people_names pn ON pn.actor_id=c.closed_by_actor_id
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    return dict(row)


def export_row(connection: Connection, *, tenant_id: str, base: dict[str, Any]) -> dict[str, Any]:
    """One journey, every column."""
    from audit_core import uc03_p2_journey360 as j360

    journey_id = UUID(str(base["journey_id"]))
    deal_view = j360.deal(connection, tenant_id=tenant_id, journey_id=journey_id)
    vehicle = j360.vehicle(connection, tenant_id=tenant_id, journey_id=journey_id, deal_view=deal_view)
    customer = j360.customer(connection, tenant_id=tenant_id, journey_id=journey_id)
    facts = {**base, **journey_facts(connection, tenant_id=tenant_id, journey_id=journey_id)}
    return {**identity_row(facts, deal=deal_view, vehicle=vehicle, customer=customer), **deal_amounts(deal_view)}


# ── the workbook ─────────────────────────────────────────────────────────────

_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _cell(value: Any) -> Any:
    if isinstance(value, str):
        return _ILLEGAL.sub("", value)
    return value


def workbook_bytes(rows: Iterable[dict[str, Any]], *, title: str, filters_text: str) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    book = Workbook(write_only=True)
    sheet = book.create_sheet("Deal Audit")
    header = columns()
    bold = Font(bold=True)
    from openpyxl.cell import WriteOnlyCell

    head_cells = []
    for name in header:
        cell = WriteOnlyCell(sheet, value=name)
        cell.font = bold
        head_cells.append(cell)
    sheet.freeze_panes = "D2"
    sheet.append(head_cells)
    for row in rows:
        sheet.append([_cell(row.get(name)) for name in header])
    notes = book.create_sheet("Columns")
    notes.append([WriteOnlyCell(notes, value=v) for v in ("Column", "Where it comes from")])
    for name, note in column_notes():
        notes.append([name, note])
    about = book.create_sheet("About")
    about.append([title])
    about.append([filters_text])
    about.append([f"Generated {datetime.now(UTC).astimezone().strftime('%Y-%m-%d %H:%M UTC')}"])
    about.append([("Amounts are the Deal's own, as Journey 360 shows them. Variance is blank until delivery "
                   "has started. Blank: not on file, or not captured by Verigence yet (see Columns).")])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


# ── the job ──────────────────────────────────────────────────────────────────

_STALE_MINUTES = 10


def _filters_text(filters: dict[str, Any], names: dict[str, str]) -> str:
    basis = {"BOOKING": "booking date", "DELIVERY": "delivery date", "UPLOADED": "upload date"}[
        filters.get("dateBasis") or "BOOKING"]
    when = (f"{basis} {filters['dateFrom']} to {filters['dateTo']}" if filters.get("dateFrom")
            else f"every {basis}")
    where = names.get("outlet") or names.get("dealer") or "every dealership and outlet in scope"
    return f"Journeys by {when}; {where}."


def _file_name(filters: dict[str, Any], names: dict[str, str]) -> str:
    span = f"{filters['dateFrom']}_to_{filters['dateTo']}" if filters.get("dateFrom") else "all-dates"
    place = names.get("outlet") or names.get("dealer")
    place_part = "_" + re.sub(r"[^A-Za-z0-9]+", "-", place).strip("-")[:40] if place else ""
    return f"Journey-Export{place_part}_{span}.xlsx"


def claim_export(engine: Engine, tenant_id: str, worker_id: str) -> dict[str, Any] | None:
    """The oldest export waiting, or one whose worker stopped beating (a restart)."""
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        row = connection.execute(
            text(
                f"""
                UPDATE auditcore.p2_journey_exports SET status='RUNNING', worker_id=:w,
                       started_at_utc=COALESCE(started_at_utc, now()), heartbeat_at_utc=now(), done=0
                WHERE export_id = (
                    SELECT export_id FROM auditcore.p2_journey_exports
                    WHERE tenant_id=:t AND (status='PENDING' OR (status='RUNNING'
                          AND heartbeat_at_utc < now() - interval '{_STALE_MINUTES} minutes'))
                    ORDER BY requested_at_utc FOR UPDATE SKIP LOCKED LIMIT 1)
                RETURNING *
                """
            ),
            {"t": tenant_id, "w": worker_id},
        ).mappings().first()
    return dict(row) if row else None


def run_export(engine: Engine, export: dict[str, Any], *, storage: Any = None) -> None:
    """Build one export: every journey in its scope, one row each, then the file."""
    tenant_id = str(export["tenant_id"])
    export_id = str(export["export_id"])
    filters = dict(export["filters"] or {})
    try:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            journeys = journeys_in_scope(connection, tenant_id=tenant_id, filters=filters,
                                         outlet_ids=export.get("outlet_ids"))
            connection.execute(
                text("UPDATE auditcore.p2_journey_exports SET total=:n WHERE tenant_id=:t AND export_id=:id"),
                {"n": len(journeys), "t": tenant_id, "id": export_id},
            )
        rows: list[dict[str, Any]] = []
        failed: list[str] = []
        for index, base in enumerate(journeys, start=1):
            try:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    rows.append(export_row(connection, tenant_id=tenant_id, base=base))
            except Exception:  # one journey never stops the export; it is listed on the job
                logger.warning("journey_export_row_failed", export_id=export_id,
                               journey_id=str(base["journey_id"]), exc_info=True)
                failed.append(str(base.get("journey_reference") or base["journey_id"]))
                rows.append({"Dealership": base.get("dealer_name"), "Booking No": base.get("journey_reference"),
                             "File Status": "Could not be read for the export"})
            if index % 10 == 0 or index == len(journeys):
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    connection.execute(
                        text("UPDATE auditcore.p2_journey_exports SET done=:d, heartbeat_at_utc=now() "
                             "WHERE tenant_id=:t AND export_id=:id"),
                        {"d": index, "t": tenant_id, "id": export_id},
                    )
        names = dict(export.get("names") or {})
        data = workbook_bytes(rows, title="Verigence journey export", filters_text=_filters_text(filters, names))
        if storage is None:
            from audit_core.uc03_p2_storage import get_p2_document_storage

            storage = get_p2_document_storage()
        # The file's own name last, so the browser saves it under that name.
        key = f"exports/{tenant_id}/journeys/{export_id}/{export.get('file_name') or 'Journey-Export.xlsx'}"
        storage.put_object(key, data, content_type=XLSX_TYPE)
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_journey_exports
                    SET status='READY', object_key=:k, row_count=:n, size_bytes=:s, finished_at_utc=now(),
                        heartbeat_at_utc=now(), error=:e
                    WHERE tenant_id=:t AND export_id=:id
                    """
                ),
                {"k": key, "n": len(rows), "s": len(data), "t": tenant_id, "id": export_id,
                 "e": (f"{len(failed)} journey(s) could not be read: " + ", ".join(failed[:20])) if failed else None},
            )
        logger.info("journey_export_ready", export_id=export_id, rows=len(rows), failed=len(failed))
    except Exception as exc:
        logger.warning("journey_export_failed", export_id=export_id, exc_info=True)
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            connection.execute(
                text("UPDATE auditcore.p2_journey_exports SET status='FAILED', error=:e, finished_at_utc=now() "
                     "WHERE tenant_id=:t AND export_id=:id"),
                {"e": f"{type(exc).__name__}: {exc}"[:500], "t": tenant_id, "id": export_id},
            )


XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def run_pending_exports(engine: Engine, tenants: list[str], worker_id: str) -> int:
    """Every waiting export of these tenants, one after another."""
    built = 0
    for tenant_id in tenants:
        while (export := claim_export(engine, tenant_id, worker_id)) is not None:
            run_export(engine, export)
            built += 1
    return built


# ── the API ──────────────────────────────────────────────────────────────────

def _export_access(connection: Connection, tenant_id: str, principal: HumanPrincipal,
                   client: SecurityAuthorizationClient) -> tuple[str, str, list[str]]:
    access = authorize_p2(connection, tenant_id=tenant_id, journey_id=None, human_principal=principal,
                          authorization_client=client, permission_key=_READ)
    actor_id = str(access.actor_id or principal.subject)
    outlets, role = supervised_outlets(connection, tenant_id=tenant_id, actor_id=actor_id)
    if role is None:
        raise HTTPException(status_code=403, detail="The journey export is for Team Leads and above.")
    return actor_id, role, outlets


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "exportId": str(row["export_id"]), "status": row["status"], "filters": row["filters"],
        "fileName": row["file_name"], "total": row["total"], "done": row["done"], "rows": row["row_count"],
        "sizeBytes": row["size_bytes"], "error": row["error"],
        "requestedAtUtc": row["requested_at_utc"].isoformat() if row["requested_at_utc"] else None,
        "finishedAtUtc": row["finished_at_utc"].isoformat() if row["finished_at_utc"] else None,
    }


@router.post("/reports/journey-exports", status_code=202)
def request_export(
    tenant_id: str, command: ExportRequest,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Ask for an export; the worker builds it (a second or so a journey)."""
    actor_id, role, outlet_ids = _export_access(connection, tenant_id, human_principal, authorization_client)
    names: dict[str, str] = {}
    if command.outletId is not None:
        if str(command.outletId) not in outlet_ids:
            raise HTTPException(status_code=404, detail="Outlet not found in your scope.")
        names["outlet"] = connection.execute(
            text("SELECT outlet_name FROM auditcore.dealer_outlets WHERE tenant_id=:t AND outlet_id=:o"),
            {"t": tenant_id, "o": command.outletId}).scalar_one_or_none() or "outlet"
    if command.dealerId is not None:
        names["dealer"] = connection.execute(
            text("SELECT dealer_name FROM auditcore.dealers WHERE tenant_id=:t AND dealer_id=:d"),
            {"t": tenant_id, "d": command.dealerId}).scalar_one_or_none() or "dealership"
    filters = json.loads(command.model_dump_json())
    export_id = uuid4()
    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_journey_exports (
                tenant_id, export_id, requested_by_actor_id, requested_by_role, filters, outlet_ids, names,
                file_name, status
            ) VALUES (:t, :id, :a, :r, CAST(:f AS jsonb), CAST(:o AS uuid[]), CAST(:n AS jsonb), :fn, 'PENDING')
            RETURNING *
            """
        ),
        {"t": tenant_id, "id": export_id, "a": actor_id, "r": role, "f": json.dumps(filters),
         "o": outlet_ids, "n": json.dumps(names), "fn": _file_name(filters, names)},
    ).mappings().one()
    return _public(dict(row))


@router.get("/reports/journey-exports")
def list_exports(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The caller's own exports of the last 14 days, newest first."""
    actor_id, _, _ = _export_access(connection, tenant_id, human_principal, authorization_client)
    rows = connection.execute(
        text(
            """
            SELECT * FROM auditcore.p2_journey_exports
            WHERE tenant_id=:t AND requested_by_actor_id=:a AND requested_at_utc > now() - interval '14 days'
            ORDER BY requested_at_utc DESC LIMIT 20
            """
        ),
        {"t": tenant_id, "a": actor_id},
    ).mappings().all()
    return {"exports": [_public(dict(r)) for r in rows]}


@router.get("/reports/journey-exports/scope")
def export_scope(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The dealerships and outlets the caller can export, for the filter lists."""
    from audit_core.uc03_p2_gate_pass import scope_outlets

    actor_id, role, outlet_ids = _export_access(connection, tenant_id, human_principal, authorization_client)
    allowed = set(outlet_ids)
    outlets = [
        {"dealerId": str(o["dealer_id"]), "dealerName": o["dealer_name"], "outletId": str(o["outlet_id"]),
         "outletName": o["outlet_name"]}
        for o in scope_outlets(connection, tenant_id, actor_id) if str(o["outlet_id"]) in allowed
    ]
    return {"role": role, "maxRangeDays": MAX_RANGE_DAYS, "outlets": outlets}


@router.get("/reports/journey-exports/{export_id}/file")
def export_file(
    tenant_id: str, export_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """A short-lived link to a finished export (its requester's only)."""
    actor_id, _, _ = _export_access(connection, tenant_id, human_principal, authorization_client)
    row = connection.execute(
        text("SELECT * FROM auditcore.p2_journey_exports WHERE tenant_id=:t AND export_id=:id "
             "AND requested_by_actor_id=:a"),
        {"t": tenant_id, "id": export_id, "a": actor_id},
    ).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Export not found.")
    if row["status"] != "READY" or not row["object_key"]:
        raise HTTPException(status_code=409, detail="The export is not ready yet.")
    from audit_core.uc03_p2_storage import (
        P2DocumentStorageError,
        get_p2_document_storage,
    )

    try:
        url = get_p2_document_storage().presign_get(str(row["object_key"]), expires_seconds=_LINK_SECONDS)
    except (RuntimeError, P2DocumentStorageError) as exc:
        raise HTTPException(status_code=503, detail="The file cannot be opened right now. Try again.") from exc
    return {"exportId": str(export_id), "fileName": row["file_name"], "url": url, "expiresInSeconds": _LINK_SECONDS}


__all__ = ["columns", "deal_amounts", "identity_row", "router", "run_export", "run_pending_exports"]
