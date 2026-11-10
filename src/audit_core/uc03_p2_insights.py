"""Business insights: what the dealers sell with the vehicle, and where discount goes
(product owner, 2026-11-10).

"Need some real good analytics meant for discount control, more deep insights on the
business dealers are doing ... insurance, scrappage, extended warranty, FASTag, RSA,
finance as well, amount of loan ... across dealership, model wise penetration, pin code
wise penetration."

``GET /p2/v1/tenants/{t}/insights/{report}`` for a Team Lead, PM or Executive (an
administrator too), over the outlets they are mapped to (a mapping with no dealer is the
whole Project), with ``dealer_id``, ``outlet_id`` and a booking-date range (``from`` /
``to``) as filters. Nothing is written and nothing is copied: every figure is read from
what Audit Core holds now, with Audit Core's own rules, so a figure here is the figure
on the Journey.

    sales         what was booked, billed and delivered: by month, dealership, outlet and
                  model, and the days from booking to delivery
    penetration   finance, in-house insurance, accessories, FASTag, RSA, extended warranty
                  and the scrappage certificate, by dealership, outlet, model and district
    insurance     in-house against outside, the agents outside policies went through,
                  insurers, add-on covers and the renewal calendar
    scrappage     the certificate register, where the old vehicles came from, the road
                  tax the certificates saved and certificates used twice
    finance       financiers, loan amount, loan against ex-showroom, sanction against
                  disbursal
    payments      how the money came in: by mode, cash against the cash limit, and what
                  no mode was read for
    catchment     where the customers live: district and PIN code, per outlet
    discounts     discount given against ex-showroom, the Deal's own verdict on every
                  line, like-for-like by outlet and model, outliers and the decisions
                  taken on excess discount tasks

How the figures are read (each one a rule already in force elsewhere):

Live Journey
    not a cancelled or duplicate booking and not closed with no delivery (the Journey
    list's own "cancelled"; ``uc03_discount_variance``).
Statement deal -- the denominator of every penetration figure
    a live Journey whose account statement (the NDC; ``customer_ledger``) or Tally
    customer ledger is on file. The statement is where RSA, FASTag, extended warranty
    and insurance are charged (product owner 2026-11-02, 2026-11-09), so a product is
    "taken" when the statement charges it. A booking form's quote is never a sale.
    Only documents still on file count (``uc03_deal_source_history``).
In-house insurance (``uc03_insurance_source``, product owner 2026-10-27, 2026-11-02)
    the PC's confirmation decides; else the dealer's statement charges the premium;
    else the policy carries one of the dealer's own Agent / MISP codes; else a dealer
    invoice bills the premium. A policy on file with none of these is "outside, not
    confirmed" -- the PC's task asks.
Financed (``uc03_finance_rule``)
    a finance record stands on the Journey. The loan amount is the sanctioned amount
    of the bank approval letter, else the disbursal receipt matched to the loan
    (product owner 2026-11-02). The record's ``financed_amount`` is not a loan figure.
Scrappage
    a Certificate of Deposit read on the Journey. The road tax effect compares the RTO
    challan's tax, as a share of ex-showroom, with and without a certificate inside the
    same price band, on the models that carry certificates.
Discount verdicts
    read from the Deal's stored result (``p2_journey_runtime.discount_variance``), the
    same rows the Excess Discount screen lists: over entitlement, kit given as cash,
    entitlement unknown (with the Deal's reason), under entitlement. Discount given is
    the Deal's discount lines on Journeys with a statement or cost sheet on file.
Receipts (``uc03_p2_audit_rules``)
    every evidenced receipt on a document still on file, a receipt read twice counted
    once, refunds apart. Cash is a receipt whose mode reads as cash; the limit for one
    cash receipt is the audit rule's own (``P2_CASH_RECEIPT_LIMIT``).
Model
    the catalogue model of the resolved SKU; a Journey with no SKU is "Model not
    resolved" and stays in the totals.
District
    the district on the customer's address as read, spellings of one district folded
    together; a Journey with a PIN and no district takes the district its PIN's other
    customers carry.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from statistics import median
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_deal_source_history import (
    ACTIVE_SOURCE_CONDITION,
    NORMALISED_TYPE_SQL,
)
from audit_core.uc03_insurance_source import normalize_code, premiums_on_dealer_invoices
from audit_core.uc03_p2_access import authorize_p2
from audit_core.uc03_p2_audit_rules import _CASH_RECEIPT_LIMIT
from audit_core.uc03_p2_daily_report import _require_report_role
from audit_core.uc03_p2_dates import parse_extracted_date, programme_date_floor
from audit_core.uc03_p2_journey360 import _duplicate_receipts, discount_label
from audit_core.uc03_p2_project_tracker import _scope
from audit_core.uc03_payment_mode import PAYMENT_MODE_TYPES

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-insights"])

_READ_PERMISSION = "audit.journey.read"
_ADMIN_ROLES = ("SUPER_ADMIN", "TENANT_ADMIN")
REPORTS = ("sales", "discounts", "penetration", "insurance", "finance", "scrappage", "payments", "catchment")

# The dealer's account statement (the NDC) and the Tally customer ledger: the documents
# that charge the value-added products. The cost sheet prints no RSA, FASTag or
# extended warranty row, so it never makes a Journey a statement deal.
STATEMENT_DOCUMENTS = ("customer_ledger", "customer_ledger_tally")
# The dealer's own deal sheets (uc03_insurance_source._STATEMENT_TYPES): a premium or an
# ex-showroom price on any of them is the dealer's billing.
DEAL_SHEET_DOCUMENTS = (*STATEMENT_DOCUMENTS, "cost_sheet", "erp_settlement")
RTO_DOCUMENT = "rto_challan"

# A group with fewer deals than this is shown, and marked as too small to read a rate from.
THIN_SAMPLE = 10
# Discount control: a deal is an outlier above either mark.
OUTLIER_PERCENT = 5.0
OUTLIER_AMOUNT = 200_000
# Rupees: differences below this are rounding, as on the Deal (uc03_discount_given).
LINE_TOLERANCE = 100
# Sanction against disbursal, and statement charge against RTO challan: below this the
# two documents agree.
DOCUMENT_GAP = 5_000

PRODUCTS: tuple[tuple[str, str, str], ...] = (
    ("finance", "Finance", "A finance record stands on the Journey."),
    ("insurance", "In-house insurance",
     "The PC confirmed in-house, the statement charges the premium, or the policy carries the dealer's code."),
    ("accessories", "Accessories", "The statement charges accessories."),
    ("fastag", "FASTag", "The statement charges FASTag."),
    ("rsa", "RSA", "The statement charges roadside assistance."),
    ("extendedWarranty", "Extended warranty", "The statement charges an extended / additional warranty."),
    ("scrappage", "Scrappage certificate", "A Certificate of Deposit is on file."),
)

INHOUSE_CONFIRMED, INHOUSE_STATEMENT = "INHOUSE_CONFIRMED", "INHOUSE_STATEMENT"
INHOUSE_DEALER_CODE, INHOUSE_INVOICE = "INHOUSE_DEALER_CODE", "INHOUSE_INVOICE"
EXTERNAL_CONFIRMED, OUTSIDE_UNCONFIRMED, NO_POLICY = "EXTERNAL_CONFIRMED", "OUTSIDE_UNCONFIRMED", "NO_POLICY"
CHANNEL_LABELS = {
    INHOUSE_STATEMENT: "In-house: premium on the statement",
    INHOUSE_DEALER_CODE: "In-house: dealer's code on the policy",
    INHOUSE_INVOICE: "In-house: premium on the dealer invoice",
    INHOUSE_CONFIRMED: "In-house: confirmed by the PC",
    EXTERNAL_CONFIRMED: "Outside: confirmed by the PC",
    OUTSIDE_UNCONFIRMED: "Outside the dealer's code, not confirmed",
    NO_POLICY: "No policy on file",
}
_INHOUSE = (INHOUSE_CONFIRMED, INHOUSE_STATEMENT, INHOUSE_DEALER_CODE, INHOUSE_INVOICE)
_OUTSIDE = (EXTERNAL_CONFIRMED, OUTSIDE_UNCONFIRMED)

OVER, KIT_AS_CASH, UNKNOWN, UNDER = "OVER_ENTITLEMENT", "KIT_AS_CASH", "ENTITLEMENT_UNKNOWN", "UNDER_ENTITLEMENT"
VERDICT_LABELS = {
    OVER: "Over entitlement", KIT_AS_CASH: "Kit given as cash",
    UNKNOWN: "Entitlement unknown", UNDER: "Under entitlement",
}
_DISCOUNT_RULES = ("EXCESS_DISCOUNT", "KIT_GIVEN_AS_CASH")

# Lenders and insurers as documents spell them, grouped under one name. Universal
# knowledge about Indian lenders and insurers, not tenant data, so it stays a plain
# table here (as uc03_finance_disbursement_resolution._LENDER_ALIASES does); a name that
# matches nothing keeps its own spelling.
_LENDER_GROUPS: tuple[tuple[str, str], ...] = (
    (r"MAHINDRA|MMFSL|M\s*&\s*M\s*FIN", "Mahindra Finance"),
    (r"STATE\s+BANK|\bSBI\b", "State Bank of India"),
    (r"HDFC", "HDFC Bank"), (r"\bHDB\b", "HDB Financial"),
    (r"CHOLA", "Cholamandalam"), (r"INDUSIND", "IndusInd Bank"),
    (r"PUNJAB\s+NATIONAL|\bPNB\b", "Punjab National Bank"),
    (r"BANK\s+OF\s+BARODA|\bBOB\b", "Bank of Baroda"),
    (r"UNION\s+BANK", "Union Bank of India"), (r"AXIS", "Axis Bank"),
    (r"BANK\s+OF\s+INDIA", "Bank of India"), (r"SUNDARAM", "Sundaram Finance"),
    (r"CANARA", "Canara Bank"), (r"\bUCO\b", "UCO Bank"), (r"ICICI", "ICICI Bank"),
    (r"KOTAK", "Kotak Mahindra Bank"), (r"IDFC", "IDFC First Bank"), (r"\bYES\b", "Yes Bank"),
    (r"TATA", "Tata Capital"), (r"BAJAJ", "Bajaj Finance"), (r"SHRIRAM", "Shriram Finance"),
    (r"INDIAN\s+BANK", "Indian Bank"), (r"INDIAN\s+OVERSEAS|\bIOB\b", "Indian Overseas Bank"),
    (r"CENTRAL\s+BANK", "Central Bank of India"), (r"BANK\s+OF\s+MAHARASHTRA", "Bank of Maharashtra"),
    (r"FEDERAL", "Federal Bank"), (r"\bIDBI\b", "IDBI Bank"), (r"AU\s+SMALL", "AU Small Finance Bank"),
    (r"EQUITAS", "Equitas Small Finance Bank"), (r"HINDUJA", "Hinduja Leyland Finance"),
    (r"TOYOTA", "Toyota Financial Services"), (r"KOGTA", "Kogta Financial"),
    (r"L\s*&\s*T|\bLNT\b|L\s+AND\s+T", "L&T Finance"), (r"\bTVS\b", "TVS Credit"),
    (r"GRAMEEN|GRAMIN|GRAMYA", "Regional rural bank"), (r"CO-?\s*OP", "Co-operative bank"),
)
_INSURER_GROUPS: tuple[tuple[str, str], ...] = (
    (r"TATA\s*AIG", "Tata AIG"), (r"ICICI", "ICICI Lombard"), (r"HDFC", "HDFC ERGO"),
    (r"BAJAJ", "Bajaj Allianz"), (r"NEW\s+INDIA", "New India Assurance"),
    (r"UNITED\s+INDIA", "United India"), (r"ORIENTAL", "Oriental Insurance"),
    (r"NATIONAL", "National Insurance"), (r"\bSBI\b", "SBI General"), (r"IFFCO", "IFFCO Tokio"),
    (r"RELIANCE", "Reliance General"), (r"CHOLA", "Cholamandalam MS"),
    (r"ROYAL\s+SUNDARAM", "Royal Sundaram"), (r"DIGIT", "Go Digit"), (r"KOTAK|ZURICH", "Zurich Kotak"),
    (r"SOMPO", "Universal Sompo"), (r"MAGMA", "Magma General"), (r"SHRIRAM", "Shriram General"),
    (r"LIBERTY", "Liberty General"), (r"GENERALI", "Future Generali"), (r"\bZUNO\b|EDELWEISS", "Zuno"),
    (r"\bACKO\b", "Acko"), (r"\bNAVI\b", "Navi General"), (r"RAHEJA", "Raheja QBE"),
)
_COVER_GROUPS: tuple[tuple[str, str], ...] = (
    (r"DEPRECIATION|\bNIL\s+DEP|\bZERO\s+DEP|BUMPER\s+TO\s+BUMPER", "Zero depreciation"),
    (r"ENGINE|GEAR\s*BOX", "Engine protect"),
    (r"INVOICE", "Return to invoice"), (r"CONSUMABLE", "Consumables"),
    (r"ROAD\s*SIDE|EMERGENCY\s+ASSIST|\bRSA\b", "Roadside assistance"),
    (r"\bKEY\b", "Key replacement"), (r"TYRE", "Tyre cover"),
    (r"NO\s+CLAIM|\bNCB\b", "NCB protection"), (r"BELONGING", "Personal belongings"),
    (r"PERSONAL\s+ACCIDENT|\bPA\b|\bCPA\b", "Personal accident"),
    (r"ACCESSOR", "Accessories cover"),
)
# The state an old vehicle's registration belongs to (the two letters every Indian
# registration starts with).
_RTO_STATES = {
    "AN": "Andaman & Nicobar", "AP": "Andhra Pradesh", "AR": "Arunachal Pradesh", "AS": "Assam",
    "BR": "Bihar", "CG": "Chhattisgarh", "CH": "Chandigarh", "DD": "Daman & Diu", "DL": "Delhi",
    "DN": "Dadra & Nagar Haveli", "GA": "Goa", "GJ": "Gujarat", "HP": "Himachal Pradesh", "HR": "Haryana",
    "JH": "Jharkhand", "JK": "Jammu & Kashmir", "KA": "Karnataka", "KL": "Kerala", "LA": "Ladakh",
    "LD": "Lakshadweep", "MH": "Maharashtra", "ML": "Meghalaya", "MN": "Manipur", "MP": "Madhya Pradesh",
    "MZ": "Mizoram", "NL": "Nagaland", "OD": "Odisha", "OR": "Odisha", "PB": "Punjab", "PY": "Puducherry",
    "RJ": "Rajasthan", "SK": "Sikkim", "TN": "Tamil Nadu", "TR": "Tripura", "TS": "Telangana",
    "TG": "Telangana", "UK": "Uttarakhand", "UA": "Uttarakhand", "UP": "Uttar Pradesh", "WB": "West Bengal",
}
_PRICE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("Under ₹10 lakh", 0, 1_000_000), ("₹10–20 lakh", 1_000_000, 2_000_000), ("₹20 lakh and above", 2_000_000, float("inf")),
)
_LTV_BANDS: tuple[tuple[str, float, float], ...] = (
    ("Under 60%", 0, 60), ("60–75%", 60, 75), ("75–85%", 75, 85), ("85–95%", 85, 95),
    ("95–100%", 95, 100.0001), ("Above ex-showroom", 100.0001, float("inf")),
)
_LOAN_BANDS: tuple[tuple[str, float, float], ...] = (
    ("Under ₹5 lakh", 0, 500_000), ("₹5–10 lakh", 500_000, 1_000_000), ("₹10–15 lakh", 1_000_000, 1_500_000),
    ("₹15–20 lakh", 1_500_000, 2_000_000), ("₹20 lakh and above", 2_000_000, float("inf")),
)
_DAYS_BANDS: tuple[tuple[str, float, float], ...] = (
    ("Within 7 days", 0, 8), ("8–15 days", 8, 16), ("16–30 days", 16, 31), ("31–60 days", 31, 61),
    ("Over 60 days", 61, float("inf")),
)
CASH_LIMIT = float(_CASH_RECEIPT_LIMIT)
MODEL_NOT_RESOLVED = "Model not resolved"
DISTRICT_NOT_READ = "District not read"

_FIELD_KEYS = ("address_pincode", "address_district", "finance_sanctioned_amount", "policy_end_date")


# --------------------------------------------------------------------------- small helpers

def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_amount(value: Any) -> float | None:
    """A rupee amount as a document prints it ("9,90,000.00", "Rs. 990000/-"); None when
    the value is not an amount."""
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d[\d,]*(?:\.\d+)?", str(value))
    return float(match.group().replace(",", "")) if match else None


def _rupees(value: float | None) -> int | None:
    return None if value is None else round(value)


def _pct(part: float, whole: float) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def _round1(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _median(values: Iterable[float | None]) -> float | None:
    data = [v for v in values if v is not None]
    return float(median(data)) if data else None


def _group_name(value: Any, groups: tuple[tuple[str, str], ...]) -> str | None:
    raw = re.sub(r"\s+", " ", str(value or "")).strip()
    if not raw:
        return None
    upper = raw.upper()
    for pattern, name in groups:
        if re.search(pattern, upper):
            return name
    return raw.title()


def lender_group(value: Any) -> str | None:
    return _group_name(value, _LENDER_GROUPS)


def insurer_group(value: Any) -> str | None:
    return _group_name(value, _INSURER_GROUPS)


def cover_group(value: Any) -> str | None:
    raw = re.sub(r"\s+", " ", str(value or "")).strip()
    if not raw:
        return None
    upper = raw.upper()
    for pattern, name in _COVER_GROUPS:
        if re.search(pattern, upper):
            return name
    return "Other cover"


def district_key(value: Any) -> str:
    """One key for every spelling of a district: KHORDA, KHORDHA and KHURDA; JAJPUR and
    JAJAPUR; BALESHWAR and BALESWAR. The first letter, then the consonants (H and Y read
    as vowels), a doubled letter once."""
    letters = re.sub(r"[^A-Z]", "", str(value or "").upper())
    if len(letters) < 3:
        return ""
    key = letters[0] + re.sub(r"[AEIOUYH]", "", letters[1:])
    return re.sub(r"(.)\1+", r"\1", key)


def old_vehicle_state(registration: Any) -> str | None:
    letters = re.sub(r"[^A-Z0-9]", "", str(registration or "").upper())[:2]
    return _RTO_STATES.get(letters)


def _band(value: float, bands: tuple[tuple[str, float, float], ...]) -> str:
    for label, low, high in bands:
        if low <= value < high:
            return label
    return bands[-1][0]


def _covers(value: Any) -> list[str]:
    """The add-on covers of a policy; the list is sometimes stored as its own JSON text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, (str, int, float)) and str(item).strip()]


# --------------------------------------------------------------------------- reading the facts

_JOURNEYS_SQL = """
    SELECT j.journey_id, j.journey_reference, j.dealer_id, d.dealer_name, j.outlet_id, o.outlet_name,
           j.created_by_display_name AS pc_name,
           COALESCE(NULLIF(c.display_name, ''), j.journey_reference) AS customer,
           CASE WHEN b.booking_date >= :date_floor THEN b.booking_date END AS booking_date,
           (j.created_at_utc AT TIME ZONE COALESCE(p.timezone_name, 'Asia/Kolkata'))::date AS opened_on,
           CASE WHEN dl.actual_delivered_at::date >= :date_floor THEN dl.actual_delivered_at::date END AS delivered_on,
           EXISTS (SELECT 1 FROM auditcore.journey_stage_states ds
                   WHERE ds.tenant_id = j.tenant_id AND ds.journey_id = j.journey_id
                     AND ds.stage_code = 'DELIVERY') AS delivery_started,
           m.model_name AS model, v.variant_name AS variant, jp.vehicle_category
    FROM auditcore.journeys j
    LEFT JOIN auditcore.projects p ON p.tenant_id = j.tenant_id
    LEFT JOIN auditcore.dealers d ON d.tenant_id = j.tenant_id AND d.dealer_id = j.dealer_id
    LEFT JOIN auditcore.dealer_outlets o
      ON o.tenant_id = j.tenant_id AND o.dealer_id = j.dealer_id AND o.outlet_id = j.outlet_id
    LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
    LEFT JOIN auditcore.bookings b ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
    LEFT JOIN LATERAL (
        SELECT x.actual_delivered_at FROM auditcore.deliveries x
        WHERE x.tenant_id = j.tenant_id AND x.journey_id = j.journey_id
        ORDER BY x.actual_delivered_at DESC NULLS LAST LIMIT 1
    ) dl ON true
    LEFT JOIN LATERAL (
        SELECT x.product_sku_id, x.vehicle_category FROM auditcore.journey_products x
        WHERE x.tenant_id = j.tenant_id AND x.journey_id = j.journey_id
        ORDER BY (x.product_sku_id IS NOT NULL) DESC, x.updated_at_utc DESC LIMIT 1
    ) jp ON true
    LEFT JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id
    LEFT JOIN auditcore.product_models m ON m.model_id = s.model_id
    LEFT JOIN auditcore.product_variants v ON v.variant_id = s.variant_id
    WHERE j.tenant_id = :t
      -- A cancelled or duplicate booking, or one closed with no delivery, is no deal
      -- (the Journey list's own "cancelled"; uc03_discount_variance).
      AND NOT EXISTS (
        SELECT 1 FROM auditcore.journey_stage_states bs
        WHERE bs.tenant_id = j.tenant_id AND bs.journey_id = j.journey_id AND bs.stage_code = 'BOOKING'
          AND (bs.business_status IN ('BOOKING_CANCELLED', 'DUPLICATE_BOOKING')
               OR bs.closure_disposition = 'NO_DELIVERY'))
"""

_SOURCES_SQL = f"""
    SELECT s.journey_id, {NORMALISED_TYPE_SQL} AS document_type, s.component_key, max(s.amount) AS amount
    FROM auditcore.commercial_line_source_values s
    WHERE s.tenant_id = :t AND s.line_kind = 'COMMERCIAL'
      AND {NORMALISED_TYPE_SQL} = ANY(CAST(:types AS text[]))
      AND {ACTIVE_SOURCE_CONDITION}
    GROUP BY s.journey_id, {NORMALISED_TYPE_SQL}, s.component_key
"""

_STATEMENT_JOURNEYS_SQL = f"""
    SELECT DISTINCT s.journey_id, {NORMALISED_TYPE_SQL} AS document_type
    FROM auditcore.commercial_line_source_values s
    WHERE s.tenant_id = :t AND {NORMALISED_TYPE_SQL} = ANY(CAST(:types AS text[]))
      AND {ACTIVE_SOURCE_CONDITION}
"""

# One reading per Journey and field: a person's correction first, then a reviewed
# reading, then the most confident one -- and only from a document still on file.
_FIELDS_SQL = """
    SELECT DISTINCT ON (f.journey_id, f.field_key) f.journey_id, f.field_key, f.effective_value #>> '{}' AS value
    FROM auditcore.journey_document_extracted_fields f
    JOIN auditcore.evidence e
      ON e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id
     AND e.di_document_id = f.di_document_id AND e.association_status = 'ACTIVE'
    WHERE f.tenant_id = :t AND f.field_key = ANY(CAST(:keys AS text[])) AND f.effective_value IS NOT NULL
      AND jsonb_typeof(f.effective_value) IN ('string', 'number')
    ORDER BY f.journey_id, f.field_key, COALESCE(f.is_modified, false) DESC, (f.reviewed_at_utc IS NOT NULL) DESC,
             f.confidence_score DESC NULLS LAST, f.updated_at_utc DESC
"""


def _rows(connection: Connection, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(text(sql), params).mappings().all()]


def load_facts(connection: Connection, *, tenant_id: str) -> list[dict[str, Any]]:
    """One fact per live Journey of the Project, everything the reports read."""
    floor = programme_date_floor() or date(1900, 1, 1)
    facts: dict[str, dict[str, Any]] = {}
    for r in _rows(connection, _JOURNEYS_SQL, t=tenant_id, date_floor=floor):
        facts[str(r["journey_id"])] = {
            "journeyId": str(r["journey_id"]), "reference": r["journey_reference"], "customer": r["customer"],
            "dealerId": None if r["dealer_id"] is None else str(r["dealer_id"]),
            "dealer": r["dealer_name"] or "Dealership not set",
            "outletId": None if r["outlet_id"] is None else str(r["outlet_id"]),
            "outlet": r["outlet_name"] or "Outlet not set", "pc": r["pc_name"],
            # The booking date (uc03_booking_date); without one, the day the Journey was
            # opened -- the day the PC uploaded the booking documents (2026-11-07).
            "bookingDate": r["booking_date"] or r["opened_on"], "bookingDateRead": r["booking_date"] is not None,
            "deliveredOn": r["delivered_on"], "deliveryStarted": bool(r["delivery_started"]),
            "model": r["model"], "variant": r["variant"], "category": r["vehicle_category"],
            "statement": False, "dealSheet": False, "charged": {}, "sheet": {}, "rto": {},
            "exShowroom": None, "policy": None, "channel": NO_POLICY, "finance": None,
            "certificates": [], "discounts": [], "verdicts": [], "pin": None, "districtRaw": None,
            "sanctioned": None, "policyEnd": None, "dealerCode": False, "invoicePremium": None,
            "receipts": [], "refunds": [],
        }

    for r in _rows(connection, _STATEMENT_JOURNEYS_SQL, t=tenant_id, types=list(DEAL_SHEET_DOCUMENTS)):
        fact = facts.get(str(r["journey_id"]))
        if fact is None:
            continue
        fact["dealSheet"] = True
        if r["document_type"] in STATEMENT_DOCUMENTS:
            fact["statement"] = True

    for r in _rows(connection, _SOURCES_SQL, t=tenant_id, types=[*DEAL_SHEET_DOCUMENTS, RTO_DOCUMENT]):
        fact = facts.get(str(r["journey_id"]))
        amount = _num(r["amount"])
        if fact is None or amount is None:
            continue
        key, doc = str(r["component_key"]), str(r["document_type"])
        if doc == RTO_DOCUMENT:
            fact["rto"][key] = max(amount, fact["rto"].get(key, 0.0))
            continue
        fact["sheet"][key] = max(amount, fact["sheet"].get(key, 0.0))
        if doc in STATEMENT_DOCUMENTS:
            fact["charged"][key] = max(amount, fact["charged"].get(key, 0.0))

    # The Deal's own ex-showroom line when a deal sheet gave it; else the deal sheets' reading.
    for r in _rows(connection, """
        SELECT journey_id, actual_amount, split_part(COALESCE(source_reference, ''), ':', 1) AS source
        FROM auditcore.commercial_lines
        WHERE tenant_id = :t AND component_key = 'ex_showroom_price' AND actual_amount IS NOT NULL
    """, t=tenant_id):
        fact = facts.get(str(r["journey_id"]))
        if fact is not None and str(r["source"]).lower() in DEAL_SHEET_DOCUMENTS:
            fact["exShowroom"] = _num(r["actual_amount"])
    for fact in facts.values():
        if fact["exShowroom"] is None and fact["sheet"].get("ex_showroom_price"):
            fact["exShowroom"] = fact["sheet"]["ex_showroom_price"]

    codes: dict[str, set[str]] = defaultdict(set)
    for r in _rows(connection, "SELECT dealer_id, normalized_code FROM auditcore.dealer_insurance_codes "
                               "WHERE tenant_id = :t", t=tenant_id):
        codes[str(r["dealer_id"])].add(str(r["normalized_code"]))
    for r in _rows(connection, """
        SELECT journey_id, insurer_name, policy_reference, actual_premium_amount, insurance_by, insurance_source,
               agent_intermediary_name, agent_intermediary_code, misp_code, add_ons
        FROM auditcore.insurance_records WHERE tenant_id = :t
    """, t=tenant_id):
        fact = facts.get(str(r["journey_id"]))
        if fact is None:
            continue
        premium = _num(r["actual_premium_amount"])
        fact["policy"] = {
            "insurer": r["insurer_name"], "policyNumber": r["policy_reference"],
            "premium": premium if premium and premium > 0 else None,
            "insuranceBy": r["insurance_by"], "confirmed": (r["insurance_source"] or "").upper() or None,
            "agentName": r["agent_intermediary_name"], "agentCode": r["agent_intermediary_code"],
            "mispCode": r["misp_code"], "covers": _covers(r["add_ons"]),
        }
    for fact in facts.values():
        fact["channel"], fact["dealerCode"] = insurance_channel(fact, codes.get(fact["dealerId"] or "", set()))
    # The rule's last word: a premium billed on a dealer invoice. Asked only for the
    # policies nothing else decided, in one statement.
    undecided = [f for f in facts.values() if f["channel"] == OUTSIDE_UNCONFIRMED]
    invoiced = premiums_on_dealer_invoices(
        connection, tenant_id=tenant_id, journey_ids=[f["journeyId"] for f in undecided])
    for fact in undecided:
        if fact["journeyId"] in invoiced:
            fact["channel"] = INHOUSE_INVOICE
            fact["invoicePremium"] = invoiced[fact["journeyId"]]

    for r in _rows(connection, """
        SELECT journey_id, provider_name, loan_disbursement_amount FROM auditcore.finance_records
        WHERE tenant_id = :t ORDER BY updated_at_utc
    """, t=tenant_id):
        fact = facts.get(str(r["journey_id"]))
        if fact is not None:
            disbursed = _num(r["loan_disbursement_amount"])
            fact["finance"] = {"lenderRaw": r["provider_name"], "lender": lender_group(r["provider_name"]),
                               "disbursed": disbursed if disbursed and disbursed > 0 else None}

    seen: set[tuple[str, str]] = set()
    for r in _rows(connection, """
        SELECT journey_id, certificate_number, certificate_variant, old_vehicle_registration_number,
               scrapping_facility_name, certificate_issue_date
        FROM auditcore.scrappage_certificate_review_values WHERE tenant_id = :t
        ORDER BY journey_id, (NULLIF(certificate_number, '') IS NOT NULL) DESC, updated_at_utc DESC
    """, t=tenant_id):
        fact = facts.get(str(r["journey_id"]))
        if fact is None:
            continue
        number = normalize_code(r["certificate_number"])
        marker = (fact["journeyId"], number)
        # A certificate read from two pages is one certificate; one with no number read
        # stands for the Journey only when nothing else does.
        if marker in seen or (not number and fact["certificates"]):
            continue
        seen.add(marker)
        fact["certificates"].append({
            "number": (r["certificate_number"] or "").strip() or None, "key": number or None,
            "kind": (r["certificate_variant"] or "").upper() or None,
            "oldRegistration": (r["old_vehicle_registration_number"] or "").strip() or None,
            "state": old_vehicle_state(r["old_vehicle_registration_number"]),
            "facility": (r["scrapping_facility_name"] or "").strip() or None,
        })

    for r in _rows(connection, """
        SELECT journey_id, discount_key, actual_discount_amount FROM auditcore.discount_applications
        WHERE tenant_id = :t AND actual_discount_amount > 0
    """, t=tenant_id):
        fact = facts.get(str(r["journey_id"]))
        if fact is not None:
            fact["discounts"].append({"key": r["discount_key"], "amount": float(r["actual_discount_amount"])})

    for r in _rows(connection, """
        SELECT r.journey_id, e.item FROM auditcore.p2_journey_runtime r
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(r.discount_variance, '[]'::jsonb)) AS e(item)
        WHERE r.tenant_id = :t
    """, t=tenant_id):
        fact = facts.get(str(r["journey_id"]))
        item = r["item"] if isinstance(r["item"], dict) else {}
        if fact is not None and item.get("status"):
            fact["verdicts"].append({
                "key": item.get("key"), "label": item.get("label") or discount_label(str(item.get("key") or "")),
                "status": item["status"], "given": parse_amount(item.get("invoice")) or 0.0,
                "entitled": parse_amount(item.get("entitled")), "variance": parse_amount(item.get("variance")),
                "kit": parse_amount(item.get("kit")), "reason": item.get("reason"),
            })

    _load_payments(connection, tenant_id, facts)

    for r in _rows(connection, _FIELDS_SQL, t=tenant_id, keys=list(_FIELD_KEYS)):
        fact = facts.get(str(r["journey_id"]))
        value = (r["value"] or "").strip()
        if fact is None or not value:
            continue
        if r["field_key"] == "address_pincode" and re.fullmatch(r"[1-9][0-9]{5}", value):
            fact["pin"] = value
        elif r["field_key"] == "address_district" and district_key(value):
            fact["districtRaw"] = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z ]", " ", value)).strip()
        elif r["field_key"] == "finance_sanctioned_amount":
            fact["sanctioned"] = parse_amount(value)
        elif r["field_key"] == "policy_end_date":
            fact["policyEnd"] = parse_extracted_date(value)

    out = list(facts.values())
    _resolve_districts(out)
    for fact in out:
        fact["loan"] = loan_amount(fact)
    return out


def _load_payments(connection: Connection, tenant_id: str, facts: dict[str, dict[str, Any]]) -> None:
    """The receipts and refunds of every Journey, as the payment rules count them
    (uc03_p2_audit_rules._Facts.payments): evidenced, on a document still on file, a
    receipt read twice counted once."""
    labels = dict(PAYMENT_MODE_TYPES)
    by_journey: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in _rows(connection, """
        SELECT p.payment_id, p.journey_id, p.amount, p.receipt_number, p.receipt_date, p.payment_reference,
               COALESCE(p.payment_mode_code, p.payment_method_code) AS mode
        FROM auditcore.payments p
        JOIN auditcore.evidence e
          ON e.tenant_id = p.tenant_id AND e.journey_id = p.journey_id AND e.di_document_id = p.source_di_document_id
        WHERE p.tenant_id = :t AND p.status_source = 'EVIDENCE' AND e.association_status = 'ACTIVE'
          AND p.amount IS NOT NULL
        ORDER BY p.journey_id, p.receipt_date ASC NULLS LAST, p.created_at_utc ASC, p.payment_id ASC
    """, t=tenant_id):
        if str(r["journey_id"]) in facts:
            by_journey[str(r["journey_id"])].append(r)
    for journey_id, rows in by_journey.items():
        duplicates = _duplicate_receipts([r for r in rows if Decimal(r["amount"]) > 0])
        fact = facts[journey_id]
        for r in rows:
            if r["payment_id"] in duplicates:
                continue
            mode = str(r["mode"] or "").upper() or "OTHERS"
            entry = {"amount": float(r["amount"]), "mode": mode, "modeLabel": labels.get(mode, mode.title()),
                     "date": r["receipt_date"], "receiptNumber": r["receipt_number"]}
            if entry["amount"] < 0 or mode == "REFUND":
                fact["refunds"].append(entry)
            elif entry["amount"] > 0:
                fact["receipts"].append(entry)


def insurance_channel(fact: dict[str, Any], dealer_codes: set[str]) -> tuple[str, bool]:
    """(channel, the policy carries the dealer's code) by the one rule of
    uc03_insurance_source: the PC's answer, else the premium on the dealer's deal sheet,
    else the dealer's Agent / MISP code on the policy; a policy with none of these is
    outside and not confirmed -- unless a dealer invoice bills the premium, which
    ``load_facts`` asks for those policies alone."""
    policy = fact.get("policy") or {}
    on_file = bool(policy.get("insurer") or policy.get("policyNumber") or policy.get("premium"))
    dealer_code = any(
        policy.get(key) and normalize_code(policy[key]) in dealer_codes for key in ("mispCode", "agentCode"))
    confirmed = policy.get("confirmed")
    if confirmed == "INHOUSE":
        return INHOUSE_CONFIRMED, dealer_code
    if confirmed:
        return EXTERNAL_CONFIRMED, dealer_code
    if (fact["sheet"].get("insurance_amount") or 0) > 0:
        return INHOUSE_STATEMENT, dealer_code
    if dealer_code:
        return INHOUSE_DEALER_CODE, dealer_code
    return (OUTSIDE_UNCONFIRMED if on_file else NO_POLICY), dealer_code


def loan_amount(fact: dict[str, Any]) -> float | None:
    """The loan of a financed Journey: the sanctioned amount, else the disbursal -- when
    it can be a loan on this vehicle (a misread carries too few or too many digits)."""
    if fact.get("finance") is None:
        return None
    ex = fact.get("exShowroom")
    for candidate in (fact.get("sanctioned"), fact["finance"].get("disbursed")):
        if candidate is not None and candidate >= 50_000 and (ex is None or candidate <= 1.6 * ex):
            return candidate
    return None


def _resolve_districts(facts: list[dict[str, Any]]) -> None:
    spellings: dict[str, Counter[str]] = defaultdict(Counter)
    by_pin: dict[str, Counter[str]] = defaultdict(Counter)
    for fact in facts:
        key = district_key(fact["districtRaw"])
        fact["districtKey"] = key or None
        if key:
            spellings[key][fact["districtRaw"].title()] += 1
            if fact["pin"]:
                by_pin[fact["pin"]][key] += 1
    for fact in facts:
        if fact["districtKey"] is None and fact["pin"] in by_pin:
            fact["districtKey"] = by_pin[fact["pin"]].most_common(1)[0][0]
        fact["district"] = (
            min(spellings[fact["districtKey"]].items(), key=lambda kv: (-kv[1], kv[0]))[0]
            if fact["districtKey"] else None)


# --------------------------------------------------------------------------- product penetration

def _taken(fact: dict[str, Any], product: str) -> bool:
    charged = fact["charged"]
    if product == "finance":
        return fact["finance"] is not None
    if product == "insurance":
        return fact["channel"] in _INHOUSE
    if product == "accessories":
        return (charged.get("accessories_cost") or 0) > 0
    if product == "fastag":
        return (charged.get("fastag_amount") or 0) > 0
    if product == "rsa":
        return (charged.get("rsa_amount") or 0) > 0
    if product == "extendedWarranty":
        return max(charged.get("additional_warranty_amount") or 0, charged.get("extended_warranty_amount") or 0) > 0
    return bool(fact["certificates"])


def _value(fact: dict[str, Any], product: str) -> float | None:
    """The rupees behind a product taken: the loan, the premium, the charge."""
    charged = fact["charged"]
    if product == "finance":
        return fact["loan"]
    if product == "insurance":
        return (fact["sheet"].get("insurance_amount") or fact["invoicePremium"]
                or (fact["policy"] or {}).get("premium"))
    if product == "accessories":
        return charged.get("accessories_cost")
    if product == "fastag":
        return charged.get("fastag_amount")
    if product == "rsa":
        return charged.get("rsa_amount")
    if product == "extendedWarranty":
        return max(charged.get("additional_warranty_amount") or 0, charged.get("extended_warranty_amount") or 0) or None
    return None


def _product_cells(deals: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    cells: dict[str, dict[str, Any]] = {}
    for key, _label, _how in PRODUCTS:
        takers = [f for f in deals if _taken(f, key)]
        values = [v for v in (_value(f, key) for f in takers) if v]
        cells[key] = {
            "count": len(takers), "percent": _pct(len(takers), len(deals)),
            "amount": _rupees(sum(values)) if values else None,
            "typical": _rupees(_median(values)),
        }
    return cells


def _penetration_rows(
    deals: list[dict[str, Any]], key_of: Callable[[dict[str, Any]], tuple[str, str, str | None]],
    model_rates: dict[str, dict[str, float]] | None,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    labels: dict[str, tuple[str, str | None]] = {}
    for fact in deals:
        key, label, parent = key_of(fact)
        groups[key].append(fact)
        labels[key] = (label, parent)
    rows = []
    for key, members in groups.items():
        row: dict[str, Any] = {
            "key": key, "label": labels[key][0], "parent": labels[key][1], "deals": len(members),
            "thin": len(members) < THIN_SAMPLE, "products": _product_cells(members),
        }
        if model_rates is not None:
            # What this group would sell at the network's rate for each of its models.
            for product, _label, _how in PRODUCTS:
                expected = sum(model_rates[product].get(f["model"] or MODEL_NOT_RESOLVED, 0.0) for f in members)
                cell = row["products"][product]
                cell["expected"] = round(expected, 1)
                cell["gap"] = round(cell["count"] - expected, 1)
        rows.append(row)
    rows.sort(key=lambda r: (-r["deals"], r["label"]))
    return rows


def penetration_report(facts: list[dict[str, Any]]) -> dict[str, Any]:
    deals = [f for f in facts if f["statement"]]
    model_rates: dict[str, dict[str, float]] = {key: {} for key, _l, _h in PRODUCTS}
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact in deals:
        by_model[fact["model"] or MODEL_NOT_RESOLVED].append(fact)
    for model, members in by_model.items():
        for product, _label, _how in PRODUCTS:
            model_rates[product][model] = sum(1 for f in members if _taken(f, product)) / len(members)
    return {
        "basis": {
            "liveJourneys": len(facts), "statementDeals": len(deals),
            "withoutStatement": len(facts) - len(deals), "thinBelow": THIN_SAMPLE,
        },
        "products": [{"key": key, "label": label, "how": how} for key, label, how in PRODUCTS],
        "network": {"deals": len(deals), "products": _product_cells(deals)},
        "dealers": _penetration_rows(deals, lambda f: (f["dealerId"] or "-", f["dealer"], None), model_rates),
        "outlets": _penetration_rows(
            deals, lambda f: (f["outletId"] or "-", f["outlet"], f["dealer"]), model_rates),
        "models": _penetration_rows(
            deals, lambda f: (f["model"] or MODEL_NOT_RESOLVED, f["model"] or MODEL_NOT_RESOLVED, f["category"]),
            None),
        "districts": _penetration_rows(
            deals, lambda f: (f["districtKey"] or "-", f["district"] or DISTRICT_NOT_READ, None), model_rates),
        "notes": [
            ("A statement deal is a live Journey whose account statement (NDC) or Tally customer ledger is on "
            "file. Every percentage is of statement deals."),
            ("Expected is what the group would sell at the network's rate for each of its models, so a "
            "commercial-vehicle outlet is not measured against an SUV outlet. Gap is actual minus expected."),
            f"A group with fewer than {THIN_SAMPLE} statement deals is marked: its rate moves a lot with one deal.",
        ],
    }


# --------------------------------------------------------------------------- insurance

def _agent(policy: dict[str, Any]) -> tuple[str, str, str | None]:
    code = policy.get("agentCode") or policy.get("mispCode")
    name = re.sub(r"\s+", " ", str(policy.get("agentName") or "")).strip()
    if code and normalize_code(code):
        return f"code:{normalize_code(code)}", name.title() or "Name not read", str(code).strip()
    if name:
        return f"name:{re.sub(r'[^A-Z0-9]', '', name.upper())}", name.title(), None
    return "none", "No agent or code on the policy", None


def insurance_report(facts: list[dict[str, Any]], *, today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(UTC).date()
    deals = [f for f in facts if f["statement"]]

    def premium(fact: dict[str, Any]) -> float:
        return float(fact["sheet"].get("insurance_amount") or fact["invoicePremium"]
                     or (fact["policy"] or {}).get("premium") or 0.0)

    def channel_block(members: list[dict[str, Any]]) -> dict[str, Any]:
        counts = Counter(f["channel"] for f in members)
        inhouse = [f for f in members if f["channel"] in _INHOUSE]
        outside = [f for f in members if f["channel"] in _OUTSIDE]
        return {
            "deals": len(members),
            "channels": {key: counts.get(key, 0) for key in CHANNEL_LABELS},
            "inhouse": len(inhouse), "inhousePercent": _pct(len(inhouse), len(members)),
            "inhousePremium": _rupees(sum(premium(f) for f in inhouse)),
            "outside": len(outside), "outsidePercent": _pct(len(outside), len(members)),
            "outsidePremium": _rupees(sum(premium(f) for f in outside)),
            "noPolicy": counts.get(NO_POLICY, 0),
        }

    def grouped(key_of: Callable[[dict[str, Any]], tuple[str, str, str | None]]) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        labels: dict[str, tuple[str, str | None]] = {}
        for fact in deals:
            key, label, parent = key_of(fact)
            groups[key].append(fact)
            labels[key] = (label, parent)
        rows = [{"key": key, "label": labels[key][0], "parent": labels[key][1],
                 "thin": len(members) < THIN_SAMPLE, **channel_block(members)}
                for key, members in groups.items()]
        return sorted(rows, key=lambda r: (-r["deals"], r["label"]))

    agents: dict[str, dict[str, Any]] = {}
    for fact in deals:
        if fact["channel"] not in _OUTSIDE:
            continue
        key, name, code = _agent(fact["policy"] or {})
        entry = agents.setdefault(key, {"key": key, "names": Counter(), "code": code, "policies": 0,
                                        "premium": 0.0, "confirmedOutside": 0, "dealers": Counter(),
                                        "outlets": Counter(), "journeys": []})
        entry["names"][name] += 1
        entry["policies"] += 1
        entry["premium"] += premium(fact)
        entry["confirmedOutside"] += fact["channel"] == EXTERNAL_CONFIRMED
        entry["dealers"][fact["dealer"]] += 1
        entry["outlets"][fact["outlet"]] += 1
        entry["journeys"].append({"journeyId": fact["journeyId"], "reference": fact["reference"]})
    agent_rows = [{
        "key": a["key"], "agent": a["names"].most_common(1)[0][0], "code": a["code"], "policies": a["policies"],
        "premium": _rupees(a["premium"]), "confirmedOutside": a["confirmedOutside"],
        "dealers": [name for name, _n in a["dealers"].most_common()],
        "outlets": [{"outlet": name, "policies": n} for name, n in a["outlets"].most_common()],
        "journeys": a["journeys"][:25],
    } for a in agents.values()]
    agent_rows.sort(key=lambda r: (-r["policies"], -(r["premium"] or 0), r["agent"]))

    # The dealer's own code on the policy while the statement charges no premium: the
    # premium was collected somewhere other than the statement.
    off_statement = [f for f in deals if f["dealerCode"] and not (f["sheet"].get("insurance_amount") or 0) > 0]

    with_policy = [f for f in deals if f["channel"] != NO_POLICY and f["policy"]]
    insurers: dict[str, dict[str, Any]] = {}
    for fact in with_policy:
        name = insurer_group(fact["policy"].get("insurer")) or "Insurer not read"
        entry = insurers.setdefault(name, {"insurer": name, "policies": 0, "premium": 0.0, "inhouse": 0, "outside": 0})
        entry["policies"] += 1
        entry["premium"] += premium(fact)
        entry["inhouse"] += fact["channel"] in _INHOUSE
        entry["outside"] += fact["channel"] in _OUTSIDE
    insurer_rows = sorted(
        ({**i, "premium": _rupees(i["premium"]), "sharePercent": _pct(i["policies"], len(with_policy))}
         for i in insurers.values()), key=lambda r: (-r["policies"], r["insurer"]))

    with_covers = [f for f in with_policy if f["policy"]["covers"]]
    covers: Counter[str] = Counter()
    for fact in with_covers:
        for name in {cover_group(c) for c in fact["policy"]["covers"]} - {None}:
            covers[name] += 1
    cover_rows = [{"cover": name, "policies": n, "percent": _pct(n, len(with_covers))}
                  for name, n in sorted(covers.items(), key=lambda kv: (-kv[1], kv[0]))]

    renewals: dict[str, dict[str, Any]] = {}
    for fact in with_policy:
        end = fact["policyEnd"]
        if end is None or end < today:
            continue
        month = end.strftime("%Y-%m")
        entry = renewals.setdefault(month, {"month": month, "policies": 0, "premium": 0.0, "inhouse": 0, "outside": 0})
        entry["policies"] += 1
        entry["premium"] += premium(fact)
        entry["inhouse"] += fact["channel"] in _INHOUSE
        entry["outside"] += fact["channel"] in _OUTSIDE
    renewal_rows = [{**r, "premium": _rupees(r["premium"])} for _m, r in sorted(renewals.items())]

    return {
        "basis": {"liveJourneys": len(facts), "statementDeals": len(deals), "policiesOnFile": len(with_policy),
                  "policiesWithCovers": len(with_covers),
                  "renewalDatesRead": sum(1 for f in with_policy if f["policyEnd"] is not None),
                  "thinBelow": THIN_SAMPLE},
        "channelLabels": CHANNEL_LABELS,
        "network": channel_block(deals),
        "dealers": grouped(lambda f: (f["dealerId"] or "-", f["dealer"], None)),
        "outlets": grouped(lambda f: (f["outletId"] or "-", f["outlet"], f["dealer"])),
        "models": grouped(lambda f: (f["model"] or MODEL_NOT_RESOLVED, f["model"] or MODEL_NOT_RESOLVED, f["category"])),
        "outsideAgents": agent_rows,
        "dealerCodeOffStatement": {
            "policies": len(off_statement), "premium": _rupees(sum(premium(f) for f in off_statement)),
            "journeys": [{"journeyId": f["journeyId"], "reference": f["reference"], "dealer": f["dealer"],
                          "outlet": f["outlet"], "premium": _rupees(premium(f))} for f in off_statement[:50]],
        },
        "insurers": insurer_rows,
        "covers": cover_rows,
        "renewals": renewal_rows,
        "notes": [
            ("In-house, in this order: the PC's confirmation; the premium charged on the dealer's statement or "
            "cost sheet; the dealer's own Agent / MISP code on the policy; the premium billed on a dealer "
            "invoice. A policy on file with none of these is outside the dealer's code and not confirmed."),
            "Premium is the statement's insurance charge; where the statement charges none, the policy's premium.",
            ("Outside agents lists every policy that is outside the dealer's code, grouped by the agent code "
            "printed on the policy (by the agent's name where no code was read)."),
            "Add-on covers are counted once per policy, on the policies where the cover list was read.",
        ],
    }


# --------------------------------------------------------------------------- scrappage

def _road_tax_percent(fact: dict[str, Any]) -> float | None:
    tax, ex = fact["rto"].get("road_tax_amount"), fact["exShowroom"]
    if not tax or not ex:
        return None
    share = 100.0 * tax / ex
    return share if 2.0 <= share <= 25.0 else None  # outside this a figure was misread


def scrappage_report(facts: list[dict[str, Any]], *, home_states: set[str] | None = None) -> dict[str, Any]:
    """``home_states``: the states the Project's outlets are in; an old vehicle
    registered anywhere else came from outside."""
    home = {s.lower() for s in (home_states or set())}
    deals = [f for f in facts if f["statement"]]
    holders = [f for f in facts if f["certificates"]]
    statement_holders = [f for f in holders if f["statement"]]

    def bonus(fact: dict[str, Any]) -> float:
        return sum(d["amount"] for d in fact["discounts"] if str(d["key"]).startswith("SCRAPPAGE_BONUS"))

    def charged_registration(fact: dict[str, Any]) -> float | None:
        return fact["charged"].get("registration_charges") or fact["charged"].get("road_tax_registration")

    def paid_rto(fact: dict[str, Any]) -> float | None:
        tax = fact["rto"].get("road_tax_amount")
        return None if not tax else tax + (fact["rto"].get("registration_charges") or 0.0)

    kinds, states = Counter(), Counter()
    used: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact in holders:
        for certificate in fact["certificates"]:
            kinds[certificate["kind"] or "NOT_READ"] += 1
            states[certificate["state"] or "State not read"] += 1
            if certificate["key"]:
                used[certificate["key"]].append(fact)
    reuse = [{
        "certificateNumber": next(c["number"] for f in members for c in f["certificates"] if c["key"] == key),
        "journeys": [{"journeyId": f["journeyId"], "reference": f["reference"], "customer": f["customer"],
                      "outlet": f["outlet"]} for f in members],
    } for key, members in used.items() if len({f["journeyId"] for f in members}) > 1]

    # The road tax effect, like for like: the models that carry certificates, inside one
    # price band, with against without.
    models = {f["model"] for f in holders if f["model"]}
    bands: dict[str, dict[str, list[dict[str, Any]]]] = {
        label: {"with": [], "without": []} for label, _lo, _hi in _PRICE_BANDS}
    for fact in facts:
        if fact["model"] in models and _road_tax_percent(fact) is not None:
            bands[_band(fact["exShowroom"], _PRICE_BANDS)]["with" if fact["certificates"] else "without"].append(fact)
    effect, saved_total, saved_deals = [], 0.0, 0
    for label, _lo, _hi in _PRICE_BANDS:
        with_c, without = bands[label]["with"], bands[label]["without"]
        with_pct = _median(_road_tax_percent(f) for f in with_c)
        without_pct = _median(_road_tax_percent(f) for f in without)
        saving = None
        if with_pct is not None and without_pct is not None and without_pct > with_pct:
            saving = sum((without_pct - with_pct) / 100.0 * f["exShowroom"] for f in with_c)
            saved_total += saving
            saved_deals += len(with_c)
        effect.append({
            "band": label, "withCertificate": len(with_c), "withoutCertificate": len(without),
            "taxPercentWith": None if with_pct is None else round(with_pct, 2),
            "taxPercentWithout": None if without_pct is None else round(without_pct, 2),
            "savingPerDeal": _rupees(saving / len(with_c)) if saving and with_c else None,
            "saving": _rupees(saving),
        })

    def share_rows(key_of: Callable[[dict[str, Any]], tuple[str, str, str | None]]) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        labels: dict[str, tuple[str, str | None]] = {}
        for fact in deals:
            key, label, parent = key_of(fact)
            groups[key].append(fact)
            labels[key] = (label, parent)
        rows = [{
            "key": key, "label": labels[key][0], "parent": labels[key][1], "deals": len(members),
            "thin": len(members) < THIN_SAMPLE,
            "certificates": sum(1 for f in members if f["certificates"]),
            "percent": _pct(sum(1 for f in members if f["certificates"]), len(members)),
            "bonusGiven": sum(1 for f in members if f["certificates"] and bonus(f) > 0),
        } for key, members in groups.items()]
        return sorted(rows, key=lambda r: (-r["certificates"], -r["deals"], r["label"]))

    register = []
    for fact in sorted(holders, key=lambda f: (f["dealer"], f["outlet"], f["reference"] or "")):
        charged, paid = charged_registration(fact), paid_rto(fact)
        tax_pct = _road_tax_percent(fact)
        first = fact["certificates"][0]
        register.append({
            "journeyId": fact["journeyId"], "reference": fact["reference"], "customer": fact["customer"],
            "dealer": fact["dealer"], "outlet": fact["outlet"], "model": fact["model"] or MODEL_NOT_RESOLVED,
            "bookingDate": None if fact["bookingDate"] is None else fact["bookingDate"].isoformat(),
            "certificateNumber": first["number"], "kind": first["kind"],
            "oldRegistration": first["oldRegistration"], "state": first["state"], "facility": first["facility"],
            "certificates": len(fact["certificates"]), "bonusGiven": _rupees(bonus(fact)) or 0,
            "exShowroom": _rupees(fact["exShowroom"]), "roadTaxPaid": _rupees(fact["rto"].get("road_tax_amount")),
            "roadTaxPercent": None if tax_pct is None else round(tax_pct, 2),
            "chargedOnStatement": _rupees(charged), "paidAtRto": _rupees(paid),
            "chargedAbovePaid": _rupees(charged - paid) if charged and paid else None,
        })
    above = [r for r in register if (r["chargedAbovePaid"] or 0) > DOCUMENT_GAP]

    certificates = sum(len(f["certificates"]) for f in holders)
    out_of_state = (
        sum(n for state, n in states.items() if state != "State not read" and state.lower() not in home)
        if home else None)
    return {
        "basis": {"liveJourneys": len(facts), "statementDeals": len(deals), "thinBelow": THIN_SAMPLE,
                  "gapRupees": DOCUMENT_GAP},
        "summary": {
            "journeysWithCertificate": len(holders), "statementDealsWithCertificate": len(statement_holders),
            "percentOfStatementDeals": _pct(len(statement_holders), len(deals)),
            "certificates": certificates, "transferred": kinds.get("TRANSFERRED", 0),
            "original": kinds.get("ORIGINAL", 0),
            "bonusGiven": sum(1 for f in holders if bonus(f) > 0),
            "certificateWithoutBonus": sum(1 for f in holders if bonus(f) <= 0),
            "oldVehiclesOutsideState": out_of_state,
            "oldVehiclesStateRead": sum(n for state, n in states.items() if state != "State not read"),
            "certificatesUsedTwice": len(reuse),
            "estimatedRoadTaxSaved": _rupees(saved_total) or 0, "roadTaxSavedOnDeals": saved_deals,
            "chargedAbovePaid": {"deals": len(above), "amount": sum(r["chargedAbovePaid"] for r in above)},
        },
        "states": [{"state": state, "certificates": n} for state, n in states.most_common()],
        "roadTaxEffect": effect,
        "dealers": share_rows(lambda f: (f["dealerId"] or "-", f["dealer"], None)),
        "outlets": share_rows(lambda f: (f["outletId"] or "-", f["outlet"], f["dealer"])),
        "models": share_rows(lambda f: (f["model"] or MODEL_NOT_RESOLVED, f["model"] or MODEL_NOT_RESOLVED, f["category"])),
        "usedTwice": reuse,
        "register": register,
        "notes": [
            ("A certificate is a scrappage Certificate of Deposit read on the Journey; 'transferred' is one "
            "bought from another holder, 'original' the buyer's own vehicle."),
            ("The road tax effect compares the RTO challan's road tax, as a share of ex-showroom, with and "
            "without a certificate inside one price band, on the models that carry certificates. The saving "
            "is that difference on the certificate deals: an estimate, not a figure from a document."),
            (f"Charged above paid: the statement's registration charge is more than the RTO challan by over "
            f"₹{DOCUMENT_GAP:,}. Statements also carry other registration fees, so each is to be checked."),
        ],
    }


# --------------------------------------------------------------------------- finance

def _ltv(fact: dict[str, Any]) -> float | None:
    if not fact["loan"] or not fact["exShowroom"]:
        return None
    return 100.0 * fact["loan"] / fact["exShowroom"]


def finance_report(facts: list[dict[str, Any]]) -> dict[str, Any]:
    deals = [f for f in facts if f["statement"]]
    financed = [f for f in deals if f["finance"] is not None]
    with_loan = [f for f in financed if f["loan"]]
    with_ltv = [f for f in with_loan if _ltv(f) is not None]

    def block(members: list[dict[str, Any]]) -> dict[str, Any]:
        fin = [f for f in members if f["finance"] is not None]
        loans = [f["loan"] for f in fin if f["loan"]]
        return {
            "deals": len(members), "financed": len(fin), "percent": _pct(len(fin), len(members)),
            "loanKnown": len(loans), "loanTotal": _rupees(sum(loans)) if loans else None,
            "typicalLoan": _rupees(_median(loans)),
            "typicalLoanToValue": _round1(_median(_ltv(f) for f in fin)),
        }

    def grouped(key_of: Callable[[dict[str, Any]], tuple[str, str, str | None]]) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        labels: dict[str, tuple[str, str | None]] = {}
        for fact in deals:
            key, label, parent = key_of(fact)
            groups[key].append(fact)
            labels[key] = (label, parent)
        rows = [{"key": key, "label": labels[key][0], "parent": labels[key][1],
                 "thin": len(members) < THIN_SAMPLE, **block(members)} for key, members in groups.items()]
        return sorted(rows, key=lambda r: (-r["deals"], r["label"]))

    lenders: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact in financed:
        lenders[fact["finance"]["lender"] or "Financier not read"].append(fact)
    lender_rows = []
    for name, members in lenders.items():
        loans = [f["loan"] for f in members if f["loan"]]
        ltv = _round1(_median(_ltv(f) for f in members))
        lender_rows.append({
            "lender": name, "deals": len(members), "sharePercent": _pct(len(members), len(financed)),
            "loanKnown": len(loans), "loanTotal": _rupees(sum(loans)) if loans else None,
            "typicalLoan": _rupees(_median(loans)),
            "typicalLoanToValue": ltv,
            "dealers": dict(Counter(f["dealer"] for f in members).most_common()),
        })
    lender_rows.sort(key=lambda r: (-r["deals"], r["lender"]))

    gaps = []
    both = 0
    for fact in financed:
        sanctioned, disbursed = fact["sanctioned"], fact["finance"]["disbursed"]
        ex = fact["exShowroom"]
        sound = all(v is not None and v >= 50_000 and (ex is None or v <= 1.6 * ex) for v in (sanctioned, disbursed))
        if not sound:
            continue
        both += 1
        if abs(sanctioned - disbursed) > DOCUMENT_GAP:
            gaps.append({
                "journeyId": fact["journeyId"], "reference": fact["reference"], "customer": fact["customer"],
                "dealer": fact["dealer"], "outlet": fact["outlet"], "lender": fact["finance"]["lender"],
                "sanctioned": _rupees(sanctioned), "disbursed": _rupees(disbursed),
                "short": _rupees(sanctioned - disbursed),
            })
    gaps.sort(key=lambda r: -abs(r["short"]))
    short = [g for g in gaps if g["short"] > 0]

    ltv_values = [_ltv(f) for f in with_ltv]
    return {
        "basis": {"liveJourneys": len(facts), "statementDeals": len(deals), "thinBelow": THIN_SAMPLE,
                  "gapRupees": DOCUMENT_GAP,
                  "financedAllLiveJourneys": sum(1 for f in facts if f["finance"] is not None)},
        "summary": {
            **block(deals),
            "loanAboveExShowroom": sum(1 for v in ltv_values if v > 100.0),
            "loanBelowSixtyPercent": sum(1 for v in ltv_values if v < 60.0),
            "loanToValueKnown": len(with_ltv),
            "sanctionAndDisbursalRead": both, "sanctionDiffersFromDisbursal": len(gaps),
            "shortDisbursed": {"deals": len(short), "amount": sum(g["short"] for g in short)},
        },
        "lenders": lender_rows,
        "loanToValueBands": [
            {"band": label, "deals": sum(1 for v in ltv_values if low <= v < high)} for label, low, high in _LTV_BANDS],
        "loanBands": [
            {"band": label, "deals": sum(1 for f in with_loan if low <= f["loan"] < high)}
            for label, low, high in _LOAN_BANDS],
        "dealers": grouped(lambda f: (f["dealerId"] or "-", f["dealer"], None)),
        "outlets": grouped(lambda f: (f["outletId"] or "-", f["outlet"], f["dealer"])),
        "models": grouped(lambda f: (f["model"] or MODEL_NOT_RESOLVED, f["model"] or MODEL_NOT_RESOLVED, f["category"])),
        "sanctionGaps": gaps[:200],
        "notes": [
            ("Financed: a finance record stands on the Journey (hypothecation on the RTO or insurance papers, "
            "or a receipt in the financier's name). Every percentage is of statement deals."),
            ("Loan amount: the sanctioned amount on the bank approval letter, else the disbursal receipt matched "
            "to the loan. A figure that cannot be a loan on that vehicle is left out, so 'loan known' is lower "
            "than 'financed'."),
            "Loan to value is the loan as a share of the ex-showroom price on the dealer's statement or cost sheet.",
            f"Sanction against disbursal lists Journeys where both were read and differ by more than ₹{DOCUMENT_GAP:,}.",
        ],
    }


# --------------------------------------------------------------------------- catchment

def catchment_report(facts: list[dict[str, Any]]) -> dict[str, Any]:
    def place_rows(key_of: Callable[[dict[str, Any]], tuple[str, str, str | None] | None]) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        labels: dict[str, tuple[str, str | None]] = {}
        for fact in facts:
            keyed = key_of(fact)
            if keyed is None:
                continue
            groups[keyed[0]].append(fact)
            labels[keyed[0]] = (keyed[1], keyed[2])
        rows = []
        for key, members in groups.items():
            deals = [f for f in members if f["statement"]]
            outlets = Counter(f["outlet"] for f in members)
            rows.append({
                "key": key, "label": labels[key][0], "parent": labels[key][1], "journeys": len(members),
                "sharePercent": _pct(len(members), len(facts)), "pins": len({f["pin"] for f in members if f["pin"]}),
                "statementDeals": len(deals), "thin": len(deals) < THIN_SAMPLE,
                "products": _product_cells(deals) if deals else None,
                "outlets": [{"outlet": name, "journeys": n} for name, n in outlets.most_common(5)],
            })
        return sorted(rows, key=lambda r: (-r["journeys"], r["label"]))

    outlets: dict[str, dict[str, Any]] = {}
    for fact in facts:
        entry = outlets.setdefault(fact["outletId"] or "-", {
            "key": fact["outletId"] or "-", "label": fact["outlet"], "parent": fact["dealer"], "journeys": 0,
            "located": 0, "districts": Counter()})
        entry["journeys"] += 1
        if fact["districtKey"]:
            entry["located"] += 1
            entry["districts"][fact["district"]] += 1
    outlet_rows = []
    for entry in outlets.values():
        top = entry["districts"].most_common()
        outlet_rows.append({
            "key": entry["key"], "label": entry["label"], "parent": entry["parent"], "journeys": entry["journeys"],
            "located": entry["located"], "districts": len(top),
            "homeDistrict": top[0][0] if top else None,
            "homeSharePercent": _pct(top[0][1], entry["located"]) if top else None,
            "topDistricts": [{"district": name, "journeys": n, "percent": _pct(n, entry["located"])}
                             for name, n in top[:6]],
        })
    outlet_rows.sort(key=lambda r: (-r["journeys"], r["label"]))

    return {
        "basis": {
            "liveJourneys": len(facts), "withPin": sum(1 for f in facts if f["pin"]),
            "withDistrict": sum(1 for f in facts if f["districtKey"]),
            "withoutPlace": sum(1 for f in facts if not f["pin"] and not f["districtKey"]),
            "pins": len({f["pin"] for f in facts if f["pin"]}),
            "districts": len({f["districtKey"] for f in facts if f["districtKey"]}), "thinBelow": THIN_SAMPLE,
        },
        "products": [{"key": key, "label": label, "how": how} for key, label, how in PRODUCTS],
        "districts": place_rows(
            lambda f: (f["districtKey"], f["district"], None) if f["districtKey"] else None),
        "pins": place_rows(lambda f: (f["pin"], f["pin"], f["district"]) if f["pin"] else None),
        "outlets": outlet_rows,
        "notes": [
            ("The place is the customer's address as read from the Journey's documents: the PIN code and the "
            "district. Spellings of one district are folded together."),
            ("Penetration by place is of the statement deals in that place; a place with fewer than "
            f"{THIN_SAMPLE} statement deals is marked, and most single PIN codes are below it today."),
            "Home district is the district most of an outlet's located customers come from.",
        ],
    }


# --------------------------------------------------------------------------- sales

def price_check(facts: list[dict[str, Any]]) -> Callable[[dict[str, Any]], bool]:
    """Whether a Journey's billed ex-showroom can be right: within half to double what
    its model bills at (three such deals at least), else within a quarter to four times
    the network's. A price outside that was misread; the deal keeps its rupees elsewhere
    and is left out of every figure that divides by, or adds up, ex-showroom."""
    by_model: dict[str, list[float]] = defaultdict(list)
    for fact in facts:
        if fact["dealSheet"] and fact["exShowroom"]:
            by_model[fact["model"] or MODEL_NOT_RESOLVED].append(fact["exShowroom"])
    network = _median(v for values in by_model.values() for v in values)

    def sound(fact: dict[str, Any]) -> bool:
        ex = fact["exShowroom"]
        if not ex:
            return False
        values = by_model.get(fact["model"] or MODEL_NOT_RESOLVED, [])
        if fact["model"] and len(values) >= 3:
            return 0.5 <= ex / median(values) <= 2.0
        return network is not None and 0.25 <= ex / network <= 4.0

    return sound


def _days_to_deliver(fact: dict[str, Any]) -> int | None:
    """Booking to delivery, where the booking date itself was read (never the opening day)."""
    if not fact["bookingDateRead"] or fact["deliveredOn"] is None:
        return None
    days = (fact["deliveredOn"] - fact["bookingDate"]).days
    return days if days >= 0 else None


def sales_report(facts: list[dict[str, Any]]) -> dict[str, Any]:
    sound = price_check(facts)

    def block(members: list[dict[str, Any]]) -> dict[str, Any]:
        billed = [f for f in members if f["dealSheet"] and sound(f)]
        days = [d for d in (_days_to_deliver(f) for f in members) if d is not None]
        return {
            "journeys": len(members), "sharePercent": _pct(len(members), len(facts)),
            "deliveryStarted": sum(1 for f in members if f["deliveryStarted"]),
            "delivered": sum(1 for f in members if f["deliveredOn"] is not None),
            "statementDeals": sum(1 for f in members if f["statement"]),
            "billedDeals": len(billed), "billedValue": _rupees(sum(f["exShowroom"] for f in billed)) or 0,
            "typicalExShowroom": _rupees(_median(f["exShowroom"] for f in billed)),
            "typicalDaysToDeliver": _median(days), "daysMeasuredOn": len(days),
        }

    def grouped(key_of: Callable[[dict[str, Any]], tuple[str, str, str | None]]) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        labels: dict[str, tuple[str, str | None]] = {}
        for fact in facts:
            key, label, parent = key_of(fact)
            groups[key].append(fact)
            labels[key] = (label, parent)
        rows = [{"key": key, "label": labels[key][0], "parent": labels[key][1],
                 "thin": len(members) < THIN_SAMPLE, **block(members)} for key, members in groups.items()]
        return sorted(rows, key=lambda r: (-r["journeys"], r["label"]))

    months: dict[str, dict[str, Any]] = {}
    for fact in facts:
        if fact["bookingDate"] is not None:
            month = fact["bookingDate"].strftime("%Y-%m")
            months.setdefault(month, {"month": month, "booked": 0, "delivered": 0})["booked"] += 1
        if fact["deliveredOn"] is not None:
            month = fact["deliveredOn"].strftime("%Y-%m")
            months.setdefault(month, {"month": month, "booked": 0, "delivered": 0})["delivered"] += 1

    all_days = [d for d in (_days_to_deliver(f) for f in facts) if d is not None]
    return {
        "basis": {
            "liveJourneys": len(facts), "thinBelow": THIN_SAMPLE,
            "bookingDateRead": sum(1 for f in facts if f["bookingDateRead"]),
            "priceInDoubt": sum(1 for f in facts if f["dealSheet"] and f["exShowroom"] and not sound(f)),
            "modelNotResolved": sum(1 for f in facts if not f["model"]),
        },
        "network": block(facts),
        "months": [months[m] for m in sorted(months)],
        "daysToDeliverBands": [
            {"band": label, "deals": sum(1 for d in all_days if low <= d < high)} for label, low, high in _DAYS_BANDS],
        "dealers": grouped(lambda f: (f["dealerId"] or "-", f["dealer"], None)),
        "outlets": grouped(lambda f: (f["outletId"] or "-", f["outlet"], f["dealer"])),
        "models": grouped(
            lambda f: (f["model"] or MODEL_NOT_RESOLVED, f["model"] or MODEL_NOT_RESOLVED, f["category"])),
        "categories": grouped(
            lambda f: (f["category"] or "-", f["category"] or "Category not set", None)),
        "notes": [
            ("A live Journey is a booking that is not cancelled, not a duplicate and not closed with no "
             "delivery. Delivery started: the delivery stage is open. Delivered: the delivery date stands."),
            ("Billed value is the ex-showroom price on the dealer's statement or cost sheet; a deal with none "
             "on file yet is not in it, and a price far from what its model bills at is left out."),
            ("By month: booked in the month of the booking date (the day the Journey was opened where no "
             "booking date stands), delivered in the month of the delivery date."),
            ("Days to deliver runs from the booking date as read to the delivery date, on delivered Journeys "
             "that have both."),
        ],
    }


# --------------------------------------------------------------------------- payments

def payments_report(facts: list[dict[str, Any]]) -> dict[str, Any]:
    def is_cash(receipt: dict[str, Any]) -> bool:
        return receipt["mode"].startswith("CASH")

    def block(members: list[dict[str, Any]]) -> dict[str, Any]:
        receipts = [r for f in members for r in f["receipts"]]
        amount = sum(r["amount"] for r in receipts)
        cash = [r for r in receipts if is_cash(r)]
        unread = [r for r in receipts if r["mode"] == "OTHERS"]
        large = [r for r in cash if r["amount"] > CASH_LIMIT]
        return {
            "journeys": len(members), "journeysWithReceipts": sum(1 for f in members if f["receipts"]),
            "receipts": len(receipts), "amount": _rupees(amount) or 0,
            "cashReceipts": len(cash), "cashAmount": _rupees(sum(r["amount"] for r in cash)) or 0,
            "cashPercent": _pct(sum(r["amount"] for r in cash), amount),
            "journeysPayingCash": sum(1 for f in members if any(is_cash(r) for r in f["receipts"])),
            "cashAboveLimit": len(large), "cashAboveLimitAmount": _rupees(sum(r["amount"] for r in large)) or 0,
            "journeysCashAboveLimitInAll": sum(
                1 for f in members if sum(r["amount"] for r in f["receipts"] if is_cash(r)) > CASH_LIMIT),
            "modeNotRead": len(unread), "modeNotReadAmount": _rupees(sum(r["amount"] for r in unread)) or 0,
            "modeNotReadPercent": _pct(sum(r["amount"] for r in unread), amount),
        }

    def grouped(key_of: Callable[[dict[str, Any]], tuple[str, str, str | None]]) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        labels: dict[str, tuple[str, str | None]] = {}
        for fact in facts:
            key, label, parent = key_of(fact)
            groups[key].append(fact)
            labels[key] = (label, parent)
        rows = [{"key": key, "label": labels[key][0], "parent": labels[key][1], **block(members)}
                for key, members in groups.items()]
        return sorted(rows, key=lambda r: (-r["amount"], r["label"]))

    modes: dict[str, dict[str, Any]] = {}
    total = 0.0
    for fact in facts:
        for receipt in fact["receipts"]:
            entry = modes.setdefault(receipt["mode"], {
                "mode": receipt["mode"], "label": receipt["modeLabel"], "receipts": 0, "amount": 0.0,
                "journeys": set(), "amounts": []})
            entry["receipts"] += 1
            entry["amount"] += receipt["amount"]
            entry["journeys"].add(fact["journeyId"])
            entry["amounts"].append(receipt["amount"])
            total += receipt["amount"]
    mode_rows = sorted(({
        "mode": m["mode"], "label": "Mode not read" if m["mode"] == "OTHERS" else m["label"],
        "receipts": m["receipts"], "journeys": len(m["journeys"]), "amount": _rupees(m["amount"]),
        "sharePercent": _pct(m["amount"], total), "typical": _rupees(_median(m["amounts"])),
    } for m in modes.values()), key=lambda r: (-r["amount"], r["label"]))

    def journey_row(fact: dict[str, Any]) -> dict[str, Any]:
        return {"journeyId": fact["journeyId"], "reference": fact["reference"], "customer": fact["customer"],
                "dealer": fact["dealer"], "outlet": fact["outlet"], "pc": fact["pc"]}

    large_cash = sorted(({
        **journey_row(fact), "amount": _rupees(receipt["amount"]),
        "date": None if receipt["date"] is None else receipt["date"].isoformat(),
        "receiptNumber": receipt["receiptNumber"],
    } for fact in facts for receipt in fact["receipts"] if is_cash(receipt) and receipt["amount"] > CASH_LIMIT),
        key=lambda r: -r["amount"])
    cash_in_all = []
    for fact in facts:
        cash = [r for r in fact["receipts"] if is_cash(r)]
        if sum(r["amount"] for r in cash) > CASH_LIMIT:
            cash_in_all.append({**journey_row(fact), "cashTotal": _rupees(sum(r["amount"] for r in cash)),
                                "cashReceipts": len(cash), "largestReceipt": _rupees(max(r["amount"] for r in cash))})
    cash_in_all.sort(key=lambda r: -r["cashTotal"])
    refunds = [r for f in facts for r in f["refunds"]]

    return {
        "basis": {"liveJourneys": len(facts), "cashLimit": _rupees(CASH_LIMIT),
                  "refunds": len(refunds), "refundAmount": _rupees(sum(abs(r["amount"]) for r in refunds)) or 0},
        "network": block(facts),
        "modes": mode_rows,
        "dealers": grouped(lambda f: (f["dealerId"] or "-", f["dealer"], None)),
        "outlets": grouped(lambda f: (f["outletId"] or "-", f["outlet"], f["dealer"])),
        "cashAboveLimit": large_cash[:200],
        "cashAboveLimitInAll": cash_in_all[:200],
        "notes": [
            ("Receipts are the payment receipts read on the Journeys, on documents still on file; a receipt "
             "uploaded twice is counted once and refunds are kept apart."),
            (f"Cash above the limit: one cash receipt of more than ₹{CASH_LIMIT:,.0f}, the limit the audit rule "
             "uses. 'In all' lists Journeys whose cash receipts add up to more than that, receipt by receipt "
             "within it or not."),
            ("Mode not read: the receipt carries no payment mode the reader could classify. It is not cash "
             "and not a bank transfer until someone reads it."),
        ],
    }


# --------------------------------------------------------------------------- discount control

def _given(fact: dict[str, Any]) -> float:
    return sum(d["amount"] for d in fact["discounts"])


def discount_report(facts: list[dict[str, Any]], findings: list[dict[str, Any]], *, not_yet_calculated: int = 0,
                    ) -> dict[str, Any]:
    billed = [f for f in facts if f["dealSheet"]]
    # A discount is measured against the ex-showroom the dealer billed; a misread price
    # keeps its rupees and is left out of the percentages (``price_check``).
    sound_price = price_check(facts)
    by_model = {f["model"] or MODEL_NOT_RESOLVED for f in billed}

    priced = [f for f in billed if sound_price(f)]
    model_rate = {}
    for model in by_model:
        members = [f for f in priced if (f["model"] or MODEL_NOT_RESOLVED) == model]
        ex = sum(f["exShowroom"] for f in members)
        model_rate[model] = (sum(_given(f) for f in members) / ex) if ex else 0.0

    def verdict_totals(members: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        out = {key: {"lines": 0, "journeys": set(), "amount": 0.0} for key in VERDICT_LABELS}
        for fact in members:
            for line in fact["verdicts"]:
                cell = out.get(line["status"])
                if cell is None:
                    continue
                cell["lines"] += 1
                cell["journeys"].add(fact["journeyId"])
                if line["status"] == OVER:
                    cell["amount"] += line["variance"] or 0.0
                elif line["status"] == KIT_AS_CASH:
                    cell["amount"] += line["kit"] or 0.0
                elif line["status"] == UNKNOWN:
                    cell["amount"] += line["given"]
                else:
                    cell["amount"] += abs(line["variance"] or 0.0)
        return {key: {"lines": c["lines"], "journeys": len(c["journeys"]), "amount": _rupees(c["amount"])}
                for key, c in out.items()}

    def block(members: list[dict[str, Any]], *, like_for_like: bool) -> dict[str, Any]:
        measured = [f for f in members if sound_price(f)]
        ex = sum(f["exShowroom"] for f in measured)
        given_measured = sum(_given(f) for f in measured)
        discounted = [f for f in members if _given(f) > 0]
        row: dict[str, Any] = {
            "deals": len(members), "discountedDeals": len(discounted),
            "given": _rupees(sum(_given(f) for f in members)),
            "exShowroom": _rupees(ex), "givenPercent": round(100.0 * given_measured / ex, 2) if ex else None,
            "perDeal": _rupees(sum(_given(f) for f in members) / len(members)) if members else None,
            "typicalDiscount": _rupees(_median(_given(f) for f in discounted)),
            "verdicts": verdict_totals(members),
        }
        if like_for_like:
            expected = sum(model_rate.get(f["model"] or MODEL_NOT_RESOLVED, 0.0) * f["exShowroom"] for f in measured)
            row["expectedGiven"] = _rupees(expected)
            row["aboveExpected"] = _rupees(given_measured - expected)
            row["index"] = round(given_measured / expected, 2) if expected else None
        return row

    def grouped(key_of: Callable[[dict[str, Any]], tuple[str, str, str | None]], *, like_for_like: bool,
                ) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        labels: dict[str, tuple[str, str | None]] = {}
        for fact in billed:
            key, label, parent = key_of(fact)
            groups[key].append(fact)
            labels[key] = (label, parent)
        rows = [{"key": key, "label": labels[key][0], "parent": labels[key][1],
                 "thin": len(members) < THIN_SAMPLE, **block(members, like_for_like=like_for_like)}
                for key, members in groups.items()]
        return sorted(rows, key=lambda r: (-(r["given"] or 0), r["label"]))

    keys: dict[str, dict[str, Any]] = {}
    for fact in billed:
        for line in fact["discounts"]:
            entry = keys.setdefault(line["key"], {
                "key": line["key"], "label": discount_label(str(line["key"])), "deals": 0, "given": 0.0,
                OVER: 0.0, KIT_AS_CASH: 0.0, UNKNOWN: 0.0})
            entry["deals"] += 1
            entry["given"] += line["amount"]
        for line in fact["verdicts"]:
            entry = keys.setdefault(line["key"], {
                "key": line["key"], "label": line["label"], "deals": 0, "given": 0.0,
                OVER: 0.0, KIT_AS_CASH: 0.0, UNKNOWN: 0.0})
            if line["status"] == OVER:
                entry[OVER] += line["variance"] or 0.0
            elif line["status"] == KIT_AS_CASH:
                entry[KIT_AS_CASH] += line["kit"] or 0.0
            elif line["status"] == UNKNOWN:
                entry[UNKNOWN] += line["given"]
    total_given = sum(_given(f) for f in billed)
    key_rows = sorted(({
        "key": k["key"], "label": k["label"], "deals": k["deals"], "given": _rupees(k["given"]),
        "sharePercent": _pct(k["given"], total_given), "over": _rupees(k[OVER]),
        "kitAsCash": _rupees(k[KIT_AS_CASH]), "unknown": _rupees(k[UNKNOWN]),
    } for k in keys.values()), key=lambda r: (-(r["given"] or 0), r["label"]))

    reasons: dict[tuple[str, str], dict[str, Any]] = {}
    for fact in billed:
        for line in fact["verdicts"]:
            if line["status"] != UNKNOWN or line["given"] <= 0:
                continue
            reason = (line["reason"] or "no reason recorded").strip()
            entry = reasons.setdefault((reason, str(line["key"])), {
                "reason": reason, "key": line["key"], "label": line["label"], "lines": 0, "amount": 0.0})
            entry["lines"] += 1
            entry["amount"] += line["given"]
    reason_rows = sorted(({**r, "amount": _rupees(r["amount"])} for r in reasons.values()),
                         key=lambda r: (-r["amount"], r["reason"]))

    outliers = []
    for fact in billed:
        given = _given(fact)
        share = 100.0 * given / fact["exShowroom"] if sound_price(fact) else None
        if given < OUTLIER_AMOUNT and (share is None or share <= OUTLIER_PERCENT):
            continue
        if given <= 0:
            continue
        outliers.append({
            "journeyId": fact["journeyId"], "reference": fact["reference"], "customer": fact["customer"],
            "dealer": fact["dealer"], "outlet": fact["outlet"], "pc": fact["pc"],
            "model": fact["model"] or MODEL_NOT_RESOLVED, "variant": fact["variant"],
            "bookingDate": None if fact["bookingDate"] is None else fact["bookingDate"].isoformat(),
            "exShowroom": _rupees(fact["exShowroom"]), "given": _rupees(given),
            "percent": None if share is None else round(share, 2),
            "lines": [{"key": d["key"], "label": discount_label(str(d["key"])), "amount": _rupees(d["amount"])}
                      for d in sorted(fact["discounts"], key=lambda d: -d["amount"])],
            "verdicts": sorted({line["status"] for line in fact["verdicts"]}),
        })
    outliers.sort(key=lambda r: -r["given"])
    above_mark = [f for f in priced if _given(f) > 0 and 100.0 * _given(f) / f["exShowroom"] > OUTLIER_PERCENT]
    discounted_priced = [f for f in priced if _given(f) > 0]

    scope = {f["journeyId"] for f in facts}
    decisions = {rule: {"rule": rule, "raised": 0, "open": 0, "closedOnRecheck": 0, "confirmedBreach": 0,
                        "falsePositive": 0, "other": 0} for rule in _DISCOUNT_RULES}
    remarks = []
    for finding in findings:
        if str(finding["journey_id"]) not in scope or finding["rule_key"] not in decisions:
            continue
        cell = decisions[finding["rule_key"]]
        status, disposition = (finding["finding_status"] or "").upper(), (finding["disposition"] or "").upper()
        if status == "VOIDED":
            continue
        cell["raised"] += 1
        if status == "OPEN":
            cell["open"] += 1
        elif disposition == "FIXED":
            cell["closedOnRecheck"] += 1
        elif disposition == "CONFIRMED_BREACH":
            cell["confirmedBreach"] += 1
        elif disposition == "FALSE_POSITIVE":
            cell["falsePositive"] += 1
        else:
            cell["other"] += 1
        if disposition in ("CONFIRMED_BREACH", "FALSE_POSITIVE") and finding["resolution_reason"]:
            remarks.append({"journeyId": str(finding["journey_id"]), "rule": finding["rule_key"],
                            "decision": disposition, "remark": finding["resolution_reason"]})
    decision_rows = list(decisions.values())
    decided = sum(d["confirmedBreach"] + d["falsePositive"] for d in decision_rows)

    return {
        "basis": {
            "liveJourneys": len(facts), "billedDeals": len(billed), "pricedDeals": len(priced),
            "priceInDoubt": len(billed) - len(priced), "notYetCalculated": not_yet_calculated,
            "thinBelow": THIN_SAMPLE, "outlierPercent": OUTLIER_PERCENT, "outlierAmount": OUTLIER_AMOUNT,
            "toleranceRupees": LINE_TOLERANCE,
        },
        "verdictLabels": VERDICT_LABELS,
        "network": block(billed, like_for_like=False),
        "concentration": {
            "discountedDeals": len(discounted_priced), "aboveMark": len(above_mark),
            "aboveMarkGiven": _rupees(sum(_given(f) for f in above_mark)),
            "aboveMarkSharePercent": _pct(sum(_given(f) for f in above_mark),
                                          sum(_given(f) for f in discounted_priced)),
        },
        "byDiscount": key_rows,
        "unknownByReason": reason_rows,
        "dealers": grouped(lambda f: (f["dealerId"] or "-", f["dealer"], None), like_for_like=True),
        "outlets": grouped(lambda f: (f["outletId"] or "-", f["outlet"], f["dealer"]), like_for_like=True),
        "models": grouped(
            lambda f: (f["model"] or MODEL_NOT_RESOLVED, f["model"] or MODEL_NOT_RESOLVED, f["category"]),
            like_for_like=False),
        "outliers": outliers[:200],
        "decisions": {"rules": decision_rows, "raised": sum(d["raised"] for d in decision_rows),
                      "decidedByAPerson": decided, "remarks": remarks[:50]},
        "notes": [
            ("Billed deals are live Journeys with the dealer's statement or cost sheet on file. Discount given "
            "is the Deal's discount lines on them."),
            ("The verdicts are the Deal's own, the same lines the Excess Discount screen lists: over entitlement "
            "(the excess), kit given as cash (the kit's value), entitlement unknown (the amount given with "
            "nothing to compare with), under entitlement (the shortfall)."),
            ("Given % is discount as a share of the billed ex-showroom. A deal whose ex-showroom is far from "
            "what its model bills at is counted in rupees and left out of the percentage."),
            ("Expected is what the group would give at the network's rate for each of its models; index is "
            "actual over expected (1.00 is the network)."),
            f"Outliers: discount above {OUTLIER_PERCENT:g}% of ex-showroom, or ₹{OUTLIER_AMOUNT:,} and above on one deal.",
            ("Decisions: tasks raised on excess discount and kit given as cash. 'Closed on re-check' closed "
            "when the system re-read the deal and the excess was gone; only 'confirmed breach' and 'false "
            "positive' are a person's decision."),
        ],
    }


# --------------------------------------------------------------------------- the endpoint

def _filtered(
    facts: list[dict[str, Any]], *, outlet_ids: set[str] | None, dealer_id: UUID | None, outlet_id: UUID | None,
    first: date | None, last: date | None,
) -> list[dict[str, Any]]:
    out = []
    for fact in facts:
        if outlet_ids is not None and fact["outletId"] not in outlet_ids:
            continue
        if dealer_id is not None and fact["dealerId"] != str(dealer_id):
            continue
        if outlet_id is not None and fact["outletId"] != str(outlet_id):
            continue
        day = fact["bookingDate"]
        if first is not None and (day is None or day < first):
            continue
        if last is not None and (day is None or day > last):
            continue
        out.append(fact)
    return out


def _findings(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    return _rows(connection, """
        SELECT journey_id, rule_key, finding_status, disposition, resolution_reason
        FROM auditcore.audit_findings WHERE tenant_id = :t AND rule_key = ANY(CAST(:rules AS text[]))
    """, t=tenant_id, rules=list(_DISCOUNT_RULES))


def _home_states(connection: Connection, tenant_id: str) -> set[str]:
    return {str(s).strip().lower() for s in connection.execute(text(
        "SELECT DISTINCT state_region FROM auditcore.dealer_outlets "
        "WHERE tenant_id = :t AND NULLIF(trim(state_region), '') IS NOT NULL"), {"t": tenant_id}).scalars().all()}


def _not_yet_calculated(connection: Connection, tenant_id: str) -> int:
    return int(connection.execute(text("""
        SELECT count(*) FROM auditcore.p2_journey_runtime
        WHERE tenant_id = :t AND (discount_variance IS NULL
           OR price_variance_fact_version IS NULL OR price_variance_fact_version <> fact_version)
    """), {"t": tenant_id}).scalar_one())


def build_insight(
    connection: Connection, *, tenant_id: str, report: str, actor_id: str, admin: bool = False,
    dealer_id: UUID | None = None, outlet_id: UUID | None = None, first: date | None = None,
    last: date | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    wide, outlets = _scope(connection, tenant_id=tenant_id, actor_id=actor_id, admin=admin)
    in_scope = {str(o["outlet_id"]) for o in outlets}
    if outlet_id is not None and str(outlet_id) not in in_scope:
        # Do not disclose outlets outside the caller's scope.
        raise HTTPException(status_code=404, detail="Outlet not found in your current Project scope.")
    if first is not None and last is not None and first > last:
        raise HTTPException(status_code=422, detail="The 'from' date is after the 'to' date.")
    facts = _filtered(
        load_facts(connection, tenant_id=tenant_id), outlet_ids=None if wide else in_scope,
        dealer_id=dealer_id, outlet_id=outlet_id, first=first, last=last)
    if report == "penetration":
        body = penetration_report(facts)
    elif report == "insurance":
        body = insurance_report(facts, today=now.date())
    elif report == "scrappage":
        body = scrappage_report(facts, home_states=_home_states(connection, tenant_id))
    elif report == "finance":
        body = finance_report(facts)
    elif report == "catchment":
        body = catchment_report(facts)
    elif report == "sales":
        body = sales_report(facts)
    elif report == "payments":
        body = payments_report(facts)
    else:
        body = discount_report(facts, _findings(connection, tenant_id),
                               not_yet_calculated=_not_yet_calculated(connection, tenant_id))
    dealers: dict[str, dict[str, Any]] = {}
    for o in outlets:
        entry = dealers.setdefault(str(o["dealer_id"]), {
            "dealerId": str(o["dealer_id"]), "dealer": o["dealer_name"], "outlets": []})
        entry["outlets"].append({"outletId": str(o["outlet_id"]), "outlet": o["outlet_name"]})
    dates = [f["bookingDate"] for f in facts if f["bookingDate"] is not None]
    return {
        "report": report, "asOfUtc": now.isoformat(),
        "filters": {
            "dealerId": None if dealer_id is None else str(dealer_id),
            "outletId": None if outlet_id is None else str(outlet_id),
            "from": None if first is None else first.isoformat(), "to": None if last is None else last.isoformat(),
            "dateBasis": "Booking date; the day the Journey was opened where no booking date stands",
        },
        "scope": {"projectWide": wide, "dealers": list(dealers.values())},
        "period": {"first": min(dates).isoformat() if dates else None, "last": max(dates).isoformat() if dates else None},
        **body,
    }


@router.get("/insights/{report}")
def read_insight(
    tenant_id: str,
    report: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    dealer_id: UUID | None = None,
    outlet_id: UUID | None = None,
    first: Annotated[date | None, Query(alias="from")] = None,
    last: Annotated[date | None, Query(alias="to")] = None,
) -> dict[str, Any]:
    """Business insights (product owner 2026-11-10): sales, discount control, product
    penetration, insurance, finance, scrappage, payments and customer catchment, read
    live from the Project's Journeys. Team Lead, PM, Executive (and an administrator), over the outlets
    they are mapped to."""
    if report not in REPORTS:
        raise HTTPException(status_code=404, detail="No such insight report.")
    access = authorize_p2(
        connection, tenant_id=tenant_id, journey_id=None, human_principal=human_principal,
        authorization_client=authorization_client, permission_key=_READ_PERMISSION,
    )
    _require_report_role(connection, tenant_id=tenant_id, actor_id=human_principal.subject,
                         functional_role=access.functional_role)
    admin = (access.functional_role or "").upper() in _ADMIN_ROLES
    return build_insight(
        connection, tenant_id=tenant_id, report=report, actor_id=human_principal.subject, admin=admin,
        dealer_id=dealer_id, outlet_id=outlet_id, first=first, last=last)
