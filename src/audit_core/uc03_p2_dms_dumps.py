"""uc03_p2_dms_dumps.py -- the dealers' DMS booking and delivery dumps, matched
against the Journeys on file (product owner 2026-11-13).

"Need to create a tab to upload different type of documents. It can be
daily/weekly/monthly ... First to start is daily DMS Booking and Delivery data ...
these dumps carries data for given month. What we need to do is to match the
already upload journeys with given dumps for booking and delivery. One which are
not found, a report has to be created for missing booking and missing deliveries.
This report can be downloadable from Export to Excel, there should be tab for
booking dump variance, delivery dump variance, then with each date wise when the
dump was uploaded report should be available. This task can be done by Reviewer
or TL. We need to also mark the journey both booking and delivery which gets
synced with these dumps."

The formats (samples of 5 Nov 2026, all three dealers):

* Booking -- one format, the Mahindra DMS "FETB_EDP" booking report (Aditya,
  Shivnath, Utkal alike): Booking Number (B-13455387), DMS Booking Number (the OTF,
  OTF27A090950), Created Date, Booking Stage, Customer Name, Customer Mobile Phone,
  Dealer Location, Product Family, Variant Description, Sales Consultant.
* Delivery -- each dealer keeps its own register:
    Aditya    slno, Dealer Name, Date (delivery), Chassis no, Customer Name, Model, SC name,
              Branch, Invoice no, Date (invoice), Varient, Color, Phone No, Booking No.
    Shivnath  DATE OF DELIVERY, Location, CX NAME, CX NAME (the phone), SEGMENT, LOCATION
              (the outlet), MODEL, VARIENT, COLOUR, VIN, REGD.NO, CONSULTANT
    Utkal     a Google Form's responses ("Form Responses 1"; the first sheet is a pivot):
              Location, CUSTOMER NAME, CONT NO, DELIVERY DATE, MODEL, Chassis No.
  Read by header, not by position: the header row is found on whichever sheet holds
  one, a column by any of its known names, a repeated name by its first column (the
  delivery "Date" before the invoice's), a phone column with no phone name by its
  values (Shivnath's second "CX NAME").

A dump row is matched to a Journey of the same dealership, in this order:
  BOOKING_NUMBER  the booking form's booking number is the row's B-number or its OTF
                  (whole, without "OTF", or its numeric tail: Utkal's forms carry
                  "OTF NO-22672" for OTF27C022672)
  CHASSIS         (delivery) the last 8 characters of the chassis / VIN read from the
                  Journey's invoice, NDC, Form 21, RC, gate pass, accessory or tax
                  invoice, insurance cover or RTO challan (CHASSIS_DOCUMENT_TYPES)
  MOBILE          the customer's mobile (last 10 digits) on the booking form, a receipt
                  or the KYC
  NAME_DATE       (booking) the same customer name, the Journey booked within 3 days of the
                  DMS booking date, only when one Journey has it (2026-11-18)
  NAME_MODEL      the same customer name and model (same_model: spacing aside, one name
                  within the other), only when one Journey has it
Outcome per row: MATCHED; for a delivery row, NOT_IN_TOOL when its Journey is found but
no delivery file is on it yet; MISSING when no Journey has it; CANCELLED for a booking
the DMS shows cancelled (not counted as missing).

The matched Journeys are marked (p2_journey_dms_sync): booking synced / delivery synced,
the first time and by which upload.
"""
from __future__ import annotations

import io
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import authorize_p2_any
from audit_core.uc03_p2_names import same_person

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-dms-dumps"])

_READ: tuple[str, ...] = ("audit.journey.read",)
# A Reviewer's bundle carries audit.journey.update, a Team Lead's audit.work.update and not
# journey.update (uc03_p2_access, 2026-10-20). Asking for journey.update alone refused every
# Team Lead's upload (2026-11-14, VAC-AUTH-002 on all six files): either one will do; who may
# upload is then decided by the role (UPLOAD_ROLES).
_UPDATE: tuple[str, ...] = ("audit.journey.update", "audit.work.update")
_ADMIN_ROLES = ("SUPER_ADMIN", "TENANT_ADMIN")
# Who uploads (product owner: "this task can be done by Reviewer or TL").
UPLOAD_ROLES = ("TL", "REVIEWER")
# Who reads the uploads and the reports.
READ_ROLES = ("TL", "REVIEWER", "PM", "EXECUTIVE")
# The dump status in the Project Tracker is the Team Lead's (product owner: "only for TL").
TRACKER_ROLES = ("TL",)
MAX_BYTES = 15 * 1024 * 1024
MAX_ROWS = 20000
BOOKING, DELIVERY = "BOOKING", "DELIVERY"
MATCHED, NOT_IN_TOOL, MISSING, CANCELLED = "MATCHED", "NOT_IN_TOOL", "MISSING", "CANCELLED"

# The upload types offered on the tab (one list; the first two to start with).
UPLOAD_TYPES = [
    {"key": "DMS_BOOKING", "label": "DMS booking dump", "kind": BOOKING, "frequency": "DAILY"},
    {"key": "DMS_DELIVERY", "label": "DMS delivery dump", "kind": DELIVERY, "frequency": "DAILY"},
]


# --------------------------------------------------------------------------- reading the file

def _norm_header(value: Any) -> str:
    text_ = re.sub(r"\s+", " ", str(value or "")).strip().upper()
    return text_.strip(" :.-_")


# canonical -> the header names it goes by (normalised)
_BOOKING_COLUMNS: dict[str, tuple[str, ...]] = {
    "booking_number": ("BOOKING NUMBER", "BOOKING NO", "BOOKING NO."),
    "dms_booking_number": ("DMS BOOKING NUMBER", "OTF NUMBER", "OTF NO"),
    "event_date": ("CREATED DATE", "BOOKING DATE"),
    "stage": ("BOOKING STAGE",),
    "customer_name": ("CUSTOMER NAME", "CX NAME"),
    "mobile": ("CUSTOMER MOBILE PHONE", "MOBILE", "MOBILE NO", "PHONE NO", "CONTACT NO", "CONT NO"),
    "outlet_label": ("DEALER LOCATION", "BRANCH", "LOCATION"),
    "model": ("PRODUCT FAMILY", "MODEL"),
    "variant": ("VARIANT DESCRIPTION", "VARIANT", "VARIENT"),
    "consultant": ("SALES CONSULTANT", "SC NAME", "CONSULTANT", "FSC"),
}
_DELIVERY_COLUMNS: dict[str, tuple[str, ...]] = {
    "event_date": ("DATE OF DELIVERY", "DELIVERY DATE", "DATE"),
    "chassis": ("CHASSIS NO", "CHASSIS NUMBER", "CHASSIS", "VIN", "VIN NO"),
    "customer_name": ("CUSTOMER NAME", "CX NAME"),
    "mobile": ("PHONE NO", "MOBILE", "MOBILE NO", "CONT NO", "CONTACT NO", "CUSTOMER MOBILE PHONE"),
    "outlet_label": ("BRANCH", "LOCATION", "DEALER LOCATION"),
    "model": ("MODEL",),
    "variant": ("VARIENT", "VARIANT"),
    "invoice_number": ("INVOICE NO", "INVOICE NUMBER"),
    # Aditya's delivery report heads its booking number plain BOOKING (B-13421434).
    "booking_number": ("BOOKING NO", "BOOKING NUMBER", "BOOKING"),
    # Utkal's prints the booking date: the Deal Type rule reads it (2026-11-19).
    "booking_date": ("BOOKING DATE",),
    "registration_number": ("REGD.NO", "REGD NO", "REGISTRATION NO", "REG NO"),
    "consultant": ("SC NAME", "CONSULTANT", "FSC", "SALES CONSULTANT"),
}


@dataclass
class ParsedDump:
    kind: str
    file_format: str
    sheet_name: str
    rows: list[dict[str, Any]] = field(default_factory=list)


def detect_kind(headers: list[str]) -> str | None:
    names = set(headers)
    if "DMS BOOKING NUMBER" in names or {"BOOKING NUMBER", "BOOKING STAGE"} <= names:
        return BOOKING
    has_chassis = any(h in names for h in ("CHASSIS NO", "CHASSIS NUMBER", "CHASSIS", "VIN", "VIN NO"))
    has_date = any(h in names for h in ("DATE OF DELIVERY", "DELIVERY DATE", "DATE"))
    if has_chassis and has_date:
        return DELIVERY
    return None


def _format_name(kind: str, headers: list[str]) -> str:
    names = set(headers)
    if kind == BOOKING:
        return "DMS_BOOKING_REPORT"
    if "DATE OF DELIVERY" in names:
        return "DELIVERY_REGISTER_SEGMENT"  # Shivnath
    if "TIMESTAMP" in names:
        return "DELIVERY_FORM_RESPONSES"  # Utkal (Google Form)
    if "INVOICE NO" in names:
        return "DELIVERY_REPORT_INVOICED"  # Aditya
    return "DELIVERY_REGISTER"


_DIGITS = re.compile(r"\d")


def _is_phone(value: Any) -> bool:
    digits = re.sub(r"\D", "", str(value or ""))
    return 10 <= len(digits) <= 12 and digits[-10] in "6789"


def _columns(headers: list[str], names: dict[str, tuple[str, ...]], rows: list[tuple[Any, ...]]) -> dict[str, int]:
    """Each canonical field's column: the first column under any of its names. Of
    several location-like columns the one with the most distinct values (the outlet,
    not the dealer's own name); a name column whose values are phones is the phone."""
    out: dict[str, int] = {}
    used: set[int] = set()
    for key, aliases in names.items():
        found = [i for i, h in enumerate(headers) if h in aliases and i not in used]
        if not found:
            continue
        # Of columns under the same name, the ones that carry values (2026-11-20: Utkal's delivery
        # sheet heads two columns BOOKING DATE, the first empty; the empty one was read, every
        # row lost its booking date and bookings of August were marked Inscope).
        filled = [i for i in found if any(i < len(r) and r[i] not in (None, "") for r in rows)]
        found = filled or found
        if key == "customer_name":
            found = [i for i in found if not _mostly(rows, i, _is_phone)] or found
        if key == "outlet_label" and len(found) > 1:
            found.sort(key=lambda i: -len({str(r[i]).strip().upper() for r in rows if i < len(r) and r[i]}))
        if key == "event_date" and len(found) > 1:
            # the first named of its aliases (DATE OF DELIVERY before a plain DATE), then the first column
            found.sort(key=lambda i: (aliases.index(headers[i]), i))
        out[key] = found[0]
        used.add(found[0])
    if "mobile" not in out:
        for i in range(len(headers)):
            if i not in used and _mostly(rows, i, _is_phone):
                out["mobile"] = i
                break
    return out


def _mostly(rows: list[tuple[Any, ...]], i: int, test: Any) -> bool:
    values = [r[i] for r in rows[:200] if i < len(r) and r[i] not in (None, "")]
    return bool(values) and sum(1 for v in values if test(v)) >= 0.8 * len(values)


_DATE_FORMATS = ("%d-%b-%Y %I:%M %p", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y", "%d-%b-%y",
                 "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S")


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = re.sub(r"\s+", " ", str(value)).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()  # noqa: DTZ007 -- a calendar day
        except ValueError:
            continue
    try:
        # ISO as the stored raw row keeps a date ("2026-08-11T00:00:00").
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def _text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    out = re.sub(r"\s+", " ", str(value)).strip()
    return out[:limit] or None


def mobile_key(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if isinstance(value, float) and float(value).is_integer():
        digits = str(int(value))
    return digits[-10:] if len(digits) >= 10 else None


def chassis_key(value: Any) -> str | None:
    """The last 8 characters of a chassis / VIN: dealers' registers print the short
    chassis (T6K14763), documents the full VIN (MA1...T6K14763)."""
    raw = re.sub(r"\([^)]*\)", " ", str(value or "")).upper()
    token = max(re.findall(r"[A-Z0-9]{6,}", raw), key=len, default="")
    return token[-8:] if len(token) >= 6 else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def parse_workbook(data: bytes) -> ParsedDump:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="The file could not be read as an Excel (.xlsx) workbook.") from exc
    best: ParsedDump | None = None
    try:
        for sheet in workbook.worksheets:
            values = []
            for n, row in enumerate(sheet.iter_rows(values_only=True)):
                values.append(row)
                if n > MAX_ROWS + 20:
                    break
            for at in range(min(12, len(values))):
                headers = [_norm_header(v) for v in values[at]]
                kind = detect_kind(headers)
                if kind is None:
                    continue
                body = [r for r in values[at + 1:] if any(v not in (None, "") for v in r)]
                parsed = _rows(kind, headers, body, sheet.title)
                if parsed.rows and (best is None or len(parsed.rows) > len(best.rows)):
                    best = parsed
                break
    finally:
        workbook.close()
    if best is None:
        raise HTTPException(status_code=422, detail=(
            "No DMS booking or delivery report was found in this file: a booking report has a "
            "'DMS Booking Number' column; a delivery report a chassis / VIN and a delivery date."))
    return best


def _rows(kind: str, headers: list[str], body: list[tuple[Any, ...]], sheet: str) -> ParsedDump:
    columns = _columns(headers, _BOOKING_COLUMNS if kind == BOOKING else _DELIVERY_COLUMNS, body)
    out = ParsedDump(kind=kind, file_format=_format_name(kind, headers), sheet_name=sheet[:120])
    for r in body[:MAX_ROWS]:
        def cell(key: str, r: tuple[Any, ...] = r) -> Any:
            i = columns.get(key)
            return r[i] if i is not None and i < len(r) else None

        row = {
            "event_date": parse_date(cell("event_date")),
            "booking_date": parse_date(cell("booking_date")) if kind == DELIVERY else None,
            "booking_number": _text(cell("booking_number"), 60),
            "dms_booking_number": _text(cell("dms_booking_number"), 60),
            "customer_name": _text(cell("customer_name"), 300),
            "mobile": mobile_key(cell("mobile")),
            "chassis": _text(cell("chassis"), 40),
            "invoice_number": _text(cell("invoice_number"), 60),
            "registration_number": _text(cell("registration_number"), 40),
            "outlet_label": _text(cell("outlet_label"), 200),
            "model": _text(cell("model"), 200),
            "variant": _text(cell("variant"), 200),
            "stage": _text(cell("stage"), 60),
            "consultant": _text(cell("consultant"), 200),
            "raw": {h: _jsonable(v) for h, v in zip(headers, r, strict=False) if h and v not in (None, "")},
        }
        if kind == DELIVERY and row["booking_date"] is None:
            # The sheet's BOOKING DATE, however its columns were laid out: it decides Inscope /
            # Outscope (product owner 2026-11-19).
            row["booking_date"] = parse_date(row["raw"].get("BOOKING DATE"))
        keyed = row["chassis"] if kind == DELIVERY else (row["booking_number"] or row["dms_booking_number"])
        if not keyed and not (row["customer_name"] and (row["mobile"] or row["event_date"])):
            continue  # a blank or total line
        out.rows.append(row)
    return out


# --------------------------------------------------------------------------- matching

def _alnum(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def booking_keys(booking_number: Any, dms_booking_number: Any) -> set[str]:
    """The keys a dump row's booking is known by: the B-number's digits, the OTF
    without "OTF", and the OTF's numeric tail (also without leading zeros)."""
    keys: set[str] = set()
    b = _alnum(booking_number)
    if b:
        keys.add(b.removeprefix("B") if re.fullmatch(r"B\d{5,}", b) else b)
    otf = _alnum(dms_booking_number)
    if otf:
        bare = otf.removeprefix("OTF")
        keys.add(bare)
        tail = re.search(r"(\d{4,})$", bare)
        if tail:
            keys.add(tail.group(1))
            keys.add(tail.group(1).lstrip("0"))
    return {k for k in keys if _usable_key(k)}


def strong_booking_keys(booking_number: Any, dms_booking_number: Any) -> set[str]:
    """The keys that name one booking on their own: the B-number, and the OTF with its series
    (27F021913). The OTF's numeric tail alone (21913) is a running number that repeats across
    OTF series -- Utkal's OTF27F021913 (Sahoo Agency) and Doolla Raju's booking form "21913"
    (product owner 2026-11-19) -- so a match on it needs the customer to agree."""
    keys: set[str] = set()
    b = _alnum(booking_number)
    if b:
        keys.add(b.removeprefix("B") if re.fullmatch(r"B\d{5,}", b) else b)
    otf = _alnum(dms_booking_number)
    if otf:
        keys.add(otf.removeprefix("OTF"))
    return {k for k in keys if _usable_key(k)}


def _same_customer(index: JourneyIndex, jid: str, row: dict[str, Any]) -> bool:
    """The dump row's customer is the Journey's: the same mobile, or the same person's name."""
    mobile = row.get("mobile")
    if mobile and jid in index.by_mobile.get(mobile, set()):
        return True
    name = row.get("customer_name")
    return bool(name) and any(same_person(name, n) for n in index.journeys[jid]["names"])


def journey_ref_keys(reference: Any) -> set[str]:
    """The keys a Journey's booking number (as the booking form prints it) is known by."""
    raw = _alnum(reference)
    if not raw:
        return set()
    raw = re.sub(r"^(OTFNO|OTF|BOOKINGNO|BKNO)", "", raw)
    keys = {raw}
    if re.fullmatch(r"B\d{5,}", raw):
        keys.add(raw[1:])
    if raw.isdigit():
        keys.add(raw.lstrip("0"))
    return {k for k in keys if _usable_key(k)}


def _usable_key(key: str) -> bool:
    """A number short enough to be anything (a receipt's "116") is not a booking's key."""
    return len(key) >= (5 if key.isdigit() else 6)


def _model_tokens(value: Any) -> set[str]:
    return {t for t in re.findall(r"[A-Z0-9]+", str(value or "").upper().replace("-", "")) if len(t) >= 3}


def same_model(a: Any, b: Any) -> bool:
    """The same car, however the DMS and the papers space or extend its name (2026-11-18:
    "XEV9S" / "XEV 9S", "BE 6" / "BE6 Pack Three 79", "3XO" / "XUV3XO EV" were taken for
    different models and the bookings reported "not in Verigence"): the names compared with
    spaces and punctuation taken out, one within the other (3 characters at least), or a
    word in common."""
    if _model_tokens(a) & _model_tokens(b):
        return True
    x, y = re.sub(r"[^A-Z0-9]", "", str(a or "").upper()), re.sub(r"[^A-Z0-9]", "", str(b or "").upper())
    shorter, longer = sorted((x, y), key=len)
    return len(shorter) >= 3 and shorter in longer


@dataclass
class JourneyIndex:
    by_ref: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    by_chassis: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    by_mobile: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    # The DMS customer id the dealer's invoices print (R270905009) and the vehicle's
    # registration number: what the OEM's scheme dumps (extended warranty, RSA) carry
    # beside the chassis -- they print no customer name or mobile (2026-11-17).
    by_customer_code: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    by_registration: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    journeys: dict[str, dict[str, Any]] = field(default_factory=dict)


# The papers of the vehicle being sold that print its chassis / VIN. Until 2026-11-17 only
# the first six were read: of 59 vehicles of 2026 in Utkal's October extended warranty dump,
# 9 were on a Journey only through its accessory invoice, tax invoice, insurance cover or
# RTO challan and so were "not in Verigence". Never the exchange vehicle's papers.
CHASSIS_DOCUMENT_TYPES = (
    "customer_invoice_dms", "customer_invoice_tally", "customer_ledger", "form_21", "vehicle_rc", "gate_pass",
    "tax_invoice_tally", "accessory_invoice_dms", "accessory_invoice_tally", "insurance_cover", "rto_challan",
    "ew_invoice", "rsa_invoice", "delivery_order_cover",
)
# The same vehicle's papers that print its registration number (not the RC, valuation,
# transfer or no-dues papers: those are the exchange vehicle's).
REGISTRATION_DOCUMENT_TYPES = (
    "rto_challan", "gate_pass", "customer_invoice_dms", "tax_invoice_tally", "insurance_cover", "form_21",
    "accessory_invoice_dms", "accessory_invoice_tally", "ew_invoice", "rsa_invoice", "delivery_order_cover",
)
_REGISTRATION = re.compile(r"^(?:[A-Z]{2}\d{1,2}[A-Z]{0,3}\d{4}|\d{2}BH\d{4}[A-Z]{1,2})$")


def registration_key(value: Any) -> str | None:
    """A registration number as compared (OD02DV8990, 23BH5495F); None for 'New',
    'OD02 BHUBANESWAR' (the RTO, not a number) or a chassis printed in its place."""
    key = _alnum(value)
    return key if _REGISTRATION.fullmatch(key) else None


def customer_code_key(value: Any) -> str | None:
    """The DMS customer id (R270905009, C240333566): a letter and at least six digits."""
    key = _alnum(value)
    return key if re.fullmatch(r"[A-Z]{1,3}\d{6,}", key) else None


_INDEX_SQL = """
    WITH j AS (
        SELECT j.journey_id, j.journey_reference, j.outlet_id,
               COALESCE(NULLIF(c.legal_name, ''),
                        CASE WHEN c.display_name ~* '^[0-9a-f]{8}-[0-9a-f]{4}-' THEN NULL ELSE c.display_name END)
                 AS customer_name,
               c.mobile_number, b.booking_reference, b.booking_date, jp.model_name_snapshot AS model,
               (dl.actual_delivered_at IS NOT NULL OR ds.first_started_at_utc IS NOT NULL) AS has_delivery,
               vr.vin, vr.chassis_number
        FROM auditcore.journeys j
        LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
        LEFT JOIN auditcore.bookings b ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
        LEFT JOIN auditcore.journey_products jp ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
        LEFT JOIN auditcore.deliveries dl ON dl.tenant_id = j.tenant_id AND dl.journey_id = j.journey_id
        LEFT JOIN auditcore.journey_stage_states ds
          ON ds.tenant_id = j.tenant_id AND ds.journey_id = j.journey_id AND ds.stage_code = 'DELIVERY'
        LEFT JOIN auditcore.vehicle_records vr ON vr.tenant_id = j.tenant_id AND vr.journey_id = j.journey_id
        WHERE j.tenant_id = :t AND j.dealer_id = :d
    )
    SELECT j.*, f.field_key, f.source_document_type_key AS doc, f.effective_value #>> '{}' AS value
    FROM j
    LEFT JOIN auditcore.journey_document_extracted_fields f
      ON f.tenant_id = :t AND f.journey_id = j.journey_id
     AND jsonb_typeof(f.effective_value) IN ('string', 'number')
     AND (
          (f.field_key = 'booking_reference_number' AND f.source_document_type_key = 'booking_form')
       OR (f.field_key IN ('customer_phone', 'phone_number')
           AND f.source_document_type_key IN ('booking_form', 'dealer_receipt', 'customer_kyc'))
       OR (f.field_key IN ('vin_number', 'chassis_number') AND f.source_document_type_key = ANY(:chassis_types))
       OR (f.field_key = 'customer_name' AND f.source_document_type_key IN ('customer_kyc', 'booking_form'))
       OR f.field_key = 'buyer_customer_id'
       OR (f.field_key IN ('registration_number', 'vehicle_registration_number')
           AND f.source_document_type_key = ANY(:registration_types))
     )
"""


def build_index(connection: Connection, *, tenant_id: str, dealer_id: UUID) -> JourneyIndex:
    index = JourneyIndex()
    for r in connection.execute(text(_INDEX_SQL), {
            "t": tenant_id, "d": str(dealer_id), "chassis_types": list(CHASSIS_DOCUMENT_TYPES),
            "registration_types": list(REGISTRATION_DOCUMENT_TYPES)}).mappings():
        jid = str(r["journey_id"])
        journey = index.journeys.get(jid)
        if journey is None:
            journey = index.journeys[jid] = {
                "journeyId": jid, "reference": r["journey_reference"], "outletId": r["outlet_id"],
                "customer": r["customer_name"], "names": set(), "model": r["model"],
                "bookingDate": r["booking_date"],
                "hasDelivery": bool(r["has_delivery"]),
            }
            for key in journey_ref_keys(r["booking_reference"]):
                index.by_ref[key].add(jid)
            for value in (r["vin"], r["chassis_number"]):
                if (key := chassis_key(value)) is not None:
                    index.by_chassis[key].add(jid)
            if (key := mobile_key(r["mobile_number"])) is not None:
                index.by_mobile[key].add(jid)
            if r["customer_name"]:
                journey["names"].add(r["customer_name"])
        field_key, value = r["field_key"], r["value"]
        if not field_key or not value:
            continue
        if field_key == "booking_reference_number":
            for key in journey_ref_keys(value):
                index.by_ref[key].add(jid)
        elif field_key in ("customer_phone", "phone_number"):
            if (key := mobile_key(value)) is not None:
                index.by_mobile[key].add(jid)
        elif field_key in ("vin_number", "chassis_number"):
            if (key := chassis_key(value)) is not None:
                index.by_chassis[key].add(jid)
        elif field_key == "customer_name":
            journey["names"].add(value)
        elif field_key == "buyer_customer_id":
            if (key := customer_code_key(value)) is not None:
                index.by_customer_code[key].add(jid)
        elif (field_key in ("registration_number", "vehicle_registration_number")
              and (key := registration_key(value)) is not None):
            index.by_registration[key].add(jid)
    return index


NAME_DATE_DAYS = 3


def _one(found: set[str]) -> str | None:
    return next(iter(found)) if len(found) == 1 else None


def match_row(index: JourneyIndex, kind: str, row: dict[str, Any], taken: frozenset[str] | set[str] = frozenset()
              ) -> tuple[str | None, str | None]:
    """(journey id, basis) or (None, None). ``taken``: Journeys already claimed by another row
    of the same dump -- a Journey is one vehicle, one row (2026-11-19)."""

    def one(found: set[str]) -> str | None:
        return _one(set(found) - set(taken))

    strong = strong_booking_keys(row.get("booking_number"), row.get("dms_booking_number"))
    for key in sorted(booking_keys(row.get("booking_number"), row.get("dms_booking_number")), key=lambda k: k not in strong):
        if (jid := one(index.by_ref.get(key, set()))) is not None and (key in strong or _same_customer(index, jid, row)):
            return jid, "BOOKING_NUMBER"
    if kind == DELIVERY and (key := chassis_key(row.get("chassis"))) is not None \
            and (jid := one(index.by_chassis.get(key, set()))) is not None:
        return jid, "CHASSIS"
    if (key := row.get("mobile")) and (found := set(index.by_mobile.get(key, set())) - set(taken)):
        if (jid := one(found)) is not None:
            return jid, "MOBILE"
        # one customer, two bookings: the one of the same model
        same_model_ = {j for j in found if same_model(index.journeys[j]["model"], row.get("model"))}
        if (jid := one(same_model_)) is not None:
            return jid, "MOBILE"
    name = row.get("customer_name")
    if name:
        named = {jid for jid, j in index.journeys.items()
                 if jid not in taken and any(same_person(name, n) for n in j["names"])}
        # A booking row: the customer's name and the booking date the DMS prints, within
        # NAME_DATE_DAYS of the Journey's (2026-11-18: papers often carry no OTF or a misread
        # mobile; the date, not the model, tells two customers of one name apart).
        day = row.get("event_date")
        if kind == BOOKING and isinstance(day, date):
            dated = {j for j in named if index.journeys[j].get("bookingDate") is not None
                     and abs((index.journeys[j]["bookingDate"] - day).days) <= NAME_DATE_DAYS}
            if (jid := one(dated)) is not None:
                return jid, "NAME_DATE"
        if (jid := one({j for j in named if same_model(index.journeys[j]["model"], row.get("model"))})) is not None:
            return jid, "NAME_MODEL"
    return None, None


# The order rows claim Journeys in: the surest match first (2026-11-19).
_BASIS_RANK = {"CHASSIS": 0, "BOOKING_NUMBER": 1, "MOBILE": 2, "NAME_DATE": 3, "NAME_MODEL": 4}


def _line_key(kind: str, row: dict[str, Any]) -> str | None:
    """What makes two lines of one dump the same vehicle: the chassis (delivery), the OTF /
    booking number (booking)."""
    if kind == DELIVERY:
        return chassis_key(row.get("chassis"))
    key = _alnum(row.get("dms_booking_number")) or _alnum(row.get("booking_number"))
    return key if len(key) >= 6 else None


def outcome_of(kind: str, row: dict[str, Any], journey: dict[str, Any] | None) -> str:
    if kind == BOOKING and "CANCEL" in str(row.get("stage") or "").upper():
        return CANCELLED if journey is None else MATCHED
    if journey is None:
        return MISSING
    if kind == DELIVERY and not journey["hasDelivery"]:
        return NOT_IN_TOOL
    return MATCHED


def outlet_for(label: Any, outlets: list[dict[str, Any]]) -> str | None:
    """The dealer's outlet a dump's location names ("CUTTACK", "KHORDHA_SZ",
    "PURI_BY_PASS_SH", "MANCHESWAR FULLRANGE ..."): one outlet whose name carries the
    location's first word (5 letters are enough: BHUBANESHW / Bhubaneswar)."""
    words = [w for w in re.findall(r"[A-Z]+", str(label or "").upper()) if len(w) >= 3]
    if not words:
        return None
    head = words[0][:5]
    found = [o for o in outlets
             if any(w.startswith(head) for w in re.findall(r"[A-Z]+", str(o["outlet_name"]).upper().split(" - ", 1)[-1]))]
    return str(found[0]["outlet_id"]) if len(found) == 1 else None


# --------------------------------------------------------------------------- access

def _roles(connection: Connection, tenant_id: str, actor_id: str) -> set[str]:
    rows = connection.execute(
        text("""
            SELECT DISTINCT upper(business_role_code) FROM auditcore.business_assignments
            WHERE tenant_id = :t AND security_actor_id = :a AND assignment_status = 'ACTIVE'
              AND effective_from <= now() AND (effective_to IS NULL OR effective_to >= now())
        """),
        {"t": tenant_id, "a": actor_id},
    ).scalars().all()
    out = set()
    for role in rows:
        role = str(role or "")
        out.add("TL" if role in ("TL", "TEAM_LEAD", "TEAMLEAD") else "PM" if role in ("PM", "PROJECT_MANAGER") else role)
    return out


def _access(connection: Connection, tenant_id: str, principal: HumanPrincipal, client: SecurityAuthorizationClient,
            permissions: tuple[str, ...], roles: tuple[str, ...]) -> set[str]:
    access = authorize_p2_any(connection, tenant_id=tenant_id, journey_id=None, human_principal=principal,
                              authorization_client=client, permission_keys=permissions)
    if (access.functional_role or "").upper() in _ADMIN_ROLES:
        return {"ADMIN"}
    held = _roles(connection, tenant_id, principal.subject)
    if not held & set(roles):
        raise HTTPException(status_code=403, detail="This is for " + " / ".join(
            {"TL": "Team Leads", "REVIEWER": "Reviewers", "PM": "Project Managers", "EXECUTIVE": "Executives"}[r]
            for r in roles) + ".")
    return held


def _dealers(connection: Connection, tenant_id: str, actor_id: str, admin: bool) -> list[dict[str, Any]]:
    """The dealerships the caller covers: all for an administrator or a Project-wide
    mapping, else those of their mapped outlets."""
    rows = connection.execute(
        text("""
            SELECT d.dealer_id, d.dealer_name FROM auditcore.dealers d
            WHERE d.tenant_id = :t AND (
              :admin OR EXISTS (
                SELECT 1 FROM auditcore.business_assignments ba
                WHERE ba.tenant_id = :t AND ba.security_actor_id = :a AND ba.assignment_status = 'ACTIVE'
                  AND ba.effective_from <= now() AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                  AND (ba.dealer_id IS NULL OR ba.dealer_id = d.dealer_id)))
            ORDER BY lower(d.dealer_name)
        """),
        {"t": tenant_id, "a": actor_id, "admin": admin},
    ).mappings().all()
    return [{"dealerId": str(r["dealer_id"]), "dealerName": r["dealer_name"]} for r in rows]


def _today(connection: Connection, tenant_id: str) -> date:
    from zoneinfo import ZoneInfo

    name = connection.execute(text("SELECT timezone_name FROM auditcore.projects WHERE tenant_id = :t"),
                              {"t": tenant_id}).scalar_one_or_none() or "Asia/Kolkata"
    return datetime.now(UTC).astimezone(ZoneInfo(name)).date()


def _month(value: str | None, today: date) -> date:
    if not value:
        return today.replace(day=1)
    try:
        return datetime.strptime(value, "%Y-%m").date()  # noqa: DTZ007 -- a calendar month
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Month is YYYY-MM.") from exc


def _name_of(connection: Connection, actor_id: str) -> str | None:
    return connection.execute(
        text("SELECT display_name FROM auditcore.people_names WHERE actor_id = :a LIMIT 1"), {"a": actor_id},
    ).scalar_one_or_none()


# --------------------------------------------------------------------------- matching a stored dump

OUT_OF_PERIOD = "OUT_OF_PERIOD"
# A line repeating another line of the same dump (the same chassis / OTF), 2026-11-19: kept,
# listed, not counted.
DUPLICATE = "DUPLICATE"
# A row of a model out of scope (product owner 2026-11-19): kept in the file as uploaded, never
# matched, counted, shown or reported -- as if it were not in the dump.
EXCLUDED = "EXCLUDED"
# Mahindra's last-mile and small commercial vehicles are not audited (product owner
# 2026-11-19: "these models are out of scope for us.. so dont show them or remove them from
# the DMS dumps for mahindra whenever gets uploaded").
OUT_OF_SCOPE_MODELS = ("ALFA LOAD", "ALFA PASSENGER", "JEETO STRONG", "TREO", "TREO AUTO", "TREO PLUS", "UDO", "ZEO",
                       "ZOR GRAND")


def out_of_scope_model(*values: Any) -> str | None:
    """The out-of-scope model a row's model (or product family) names, if any: the name itself,
    or the name followed by more words (TREO PLUS HILOAD)."""
    for value in values:
        name = re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", str(value or "").upper())).strip()
        if not name:
            continue
        for model in sorted(OUT_OF_SCOPE_MODELS, key=len, reverse=True):  # TREO PLUS before TREO
            if name == model or name.startswith(model + " "):
                return model
    return None


def row_out_of_scope(row: dict[str, Any]) -> str | None:
    raw = row.get("raw") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    return out_of_scope_model(row.get("model"), raw.get("PRODUCT FAMILY"), raw.get("MODEL"))


def dump_window(period_month: date, business_date: date) -> tuple[date, date]:
    """The days a dump is compared for (product owner 2026-11-18: "till the day of upload from
    the start of the month"): the 1st of its month to its upload day, within the month."""
    month_end = _next_month(period_month) - timedelta(days=1)
    return period_month, min(business_date, month_end)


def dms_dates(connection: Connection, tenant_id: str, dealer_id: str, upto: date) -> dict[str, dict[str, date]]:
    """kind -> Journey -> the date the DMS gives it. The DMS is the source of truth for the date
    of a booking or a delivery (product owner 2026-11-19: "you have to stick to the DMS date ...
    DMS will always be source of truth.. no matter"): a Journey any of the dealership's dumps
    found is dated by that dump's row, whatever date Verigence reads from the papers. From the
    dealership's active dumps uploaded up to ``upto``; the latest dump's row decides."""
    out: dict[str, dict[str, date]] = {BOOKING: {}, DELIVERY: {}}
    for r in connection.execute(
        text("""
            SELECT r.dump_kind, CAST(r.journey_id AS text) AS journey_id, r.event_date
            FROM auditcore.p2_dms_dump_rows r
            JOIN auditcore.p2_dms_dumps u ON u.tenant_id = r.tenant_id AND u.dump_id = r.dump_id
            WHERE r.tenant_id = :t AND CAST(u.dealer_id AS text) = :d AND u.status = 'ACTIVE'
              AND u.business_date <= :upto AND r.journey_id IS NOT NULL AND r.event_date IS NOT NULL
              AND r.outcome NOT IN ('OUT_OF_PERIOD', 'EXCLUDED', 'DUPLICATE')
            ORDER BY u.business_date, u.uploaded_at_utc, r.row_no
        """),
        {"t": tenant_id, "d": str(dealer_id), "upto": upto},
    ).mappings():
        if r["dump_kind"] in out:
            out[r["dump_kind"]][r["journey_id"]] = r["event_date"]
    return out


def chassis_manufacture_month(value: Any, *, today: date | None = None) -> date | None:
    """The 1st of the month a Mahindra vehicle was made in, from a full VIN or from the last
    eight characters a delivery report prints (T2H76332: year T = 2026, plant 2, month H =
    August, serial) -- checked against the VINs of matched Journeys on 2026-11-19."""
    from audit_core.uc03_p2_journey_classification import (
        _MONTH_CODES,
        _YEAR_CODES,
        vin_manufacture_month,
    )

    full = vin_manufacture_month(value, today=today)
    if full is not None:
        return full
    short = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    if len(short) != 8 or not short[3:].isdigit():
        return None
    year, month = _YEAR_CODES.get(short[0]), _MONTH_CODES.get(short[2])
    if year is None or month is None:
        return None
    made = date(year, month, 1)
    return None if today is not None and made > today else made


class DeliveryScope:
    """The Deal Type rule (uc03_p2_journey_classification) for a delivery dump row.

    * A Team Lead's Deal Type on the row's Journey (MR, Lease, Bulk or an override) stands.
    * The dealership's sheet prints a booking date (Utkal): that booking date and the VIN
      decide -- it is the DMS's booking date, where Verigence may hold only the day the
      booking papers were uploaded (product owner 2026-11-19: "Delivery file has booking date,
      and you still marked the transactions whose booking date are earlier to 1st sep 2026 as
      in-scope"). A printed booking date after the delivery is not one.
    * Otherwise the Journey in Verigence: its own Deal Type ("if it exist in the verigence"),
      or, not settled yet, the rule with its booking date and the dump's chassis.
    * Not in Verigence: the VIN alone ("if not then VIN").
    * No readable chassis: pending."""

    def __init__(self, connection: Connection, tenant_id: str, dealer_id: Any) -> None:
        self.today = _today(connection, tenant_id)
        self.journeys = {
            str(r["journey_id"]): dict(r) for r in connection.execute(
                text("""
                    SELECT c.journey_id, c.deal_type, c.deal_type_basis, c.tl_deal_type, c.tl_reason, c.vin_used
                    FROM auditcore.journey_classifications c
                    JOIN auditcore.journeys j ON j.tenant_id = c.tenant_id AND j.journey_id = c.journey_id
                    WHERE c.tenant_id = :t AND j.dealer_id = CAST(:d AS uuid)
                """),
                {"t": tenant_id, "d": str(dealer_id)},
            ).mappings()}

    def decide(self, row: dict[str, Any], journey_id: str | None, journey: dict[str, Any] | None
               ) -> tuple[str | None, str | None]:
        from audit_core.uc03_p2_journey_classification import LABELS, decide_deal_type

        known = self.journeys.get(journey_id or "")
        if known and known["tl_deal_type"]:
            label = LABELS.get(known["tl_deal_type"], known["tl_deal_type"])
            return known["tl_deal_type"], f"Set by the Team Lead in Verigence ({label}): {known['tl_reason'] or ''}".strip()
        delivered = row.get("event_date")
        printed = row.get("booking_date")
        if printed is not None and delivered is not None and printed > delivered:
            printed = None
        # The sheet's chassis; where it cannot be read (T1, 7653946740), the Journey's VIN.
        vin, made = None, None
        for candidate in (row.get("chassis"), (known or {}).get("vin_used")):
            value = re.sub(r"[^A-Z0-9]", "", str(candidate or "").upper()) or None
            if value and (month := chassis_manufacture_month(value, today=self.today)) is not None:
                vin, made = value, month
                break
            vin = vin or value
        if printed is None and known and known["deal_type"]:
            deal_type = known["deal_type"]
            return deal_type, f"As in Verigence ({LABELS.get(deal_type, deal_type)}): {known['deal_type_basis'] or ''}".strip()
        booked = printed if printed is not None else (journey.get("bookingDate") if journey is not None else None)
        decided = decide_deal_type(booking_date=booked, vin=vin, manufacture_month=made,
                                   reference_date=delivered if made is not None else None,
                                   reference_kind="delivery")
        basis = decided["deal_type_basis"] or ""
        if printed is not None:
            basis = "Booking date as on the delivery sheet. " + basis
        elif journey is None:
            basis = "Not in Verigence: on the VIN alone. " + basis.replace("No booking date yet. ", "").replace(
                "No booking date yet; ", "")
        return decided["deal_type"], basis or None


def match_lock_key(tenant_id: str, dump_id: Any) -> str:
    return f"dms-match:{tenant_id}:{dump_id}"


@dataclass
class DumpPlan:
    """What matching a dump would store: one update per row, the Journeys synced, the counts."""
    dump: dict[str, Any]
    updates: list[dict[str, Any]]
    synced: list[str]
    counts: dict[str, int]


def plan_dump(connection: Connection, *, tenant_id: str, dump_id: UUID) -> DumpPlan:
    """Match every row of one dump with Verigence as it is now, reading only: the outcome each
    row would get. match_dump stores it; tools/dms_replay.py compares it across code versions
    on the live dumps before a change is merged (product owner 2026-11-21)."""
    dump = connection.execute(
        text("SELECT dump_kind, dealer_id, period_month, business_date FROM auditcore.p2_dms_dumps "
             "WHERE tenant_id = :t AND dump_id = :id"),
        {"t": tenant_id, "id": str(dump_id)},
    ).mappings().one()
    # Matching is Python work between statements of one transaction: past the pool's 60 s
    # idle-in-transaction limit Postgres ends the session and the upload fails (2026-11-20,
    # Utkal's booking dump). This transaction alone may wait longer (dependencies.py).
    connection.execute(text("SET LOCAL idle_in_transaction_session_timeout = '5min'"))
    first, last = dump_window(dump["period_month"], dump["business_date"])
    kind, dealer_id = dump["dump_kind"], dump["dealer_id"]
    index = build_index(connection, tenant_id=tenant_id, dealer_id=dealer_id)
    # The dealership's DMS location names (p2_dms_outlet_map, 2026-11-16), else the outlet names.
    from audit_core.uc03_p2_dms_outlet_report import outlet_map, resolve_outlet

    outlets = outlet_map(connection, tenant_id, str(dealer_id))
    rows = connection.execute(
        text("SELECT * FROM auditcore.p2_dms_dump_rows WHERE tenant_id = :t AND dump_id = :id ORDER BY row_no"),
        {"t": tenant_id, "id": str(dump_id)},
    ).mappings().all()
    counts = {MATCHED: 0, NOT_IN_TOOL: 0, MISSING: 0, CANCELLED: 0}
    updates, synced = [], []
    scope = DeliveryScope(connection, tenant_id, dealer_id) if kind == DELIVERY else None
    # One Journey per row (2026-11-19: one Journey was counted for seven OTFs of one customer):
    # the rows claim Journeys surest match first -- the matching basis, then the customer's name
    # agreeing, then a booking not cancelled. A later line with the same chassis / OTF as an
    # earlier one is a DUPLICATE of it.
    compared, first_choice = [], {}
    for row in rows:
        if row_out_of_scope(dict(row)) is None and not (row["event_date"] is not None
                                                         and not first <= row["event_date"] <= last):
            jid0, basis0 = match_row(index, kind, dict(row))
            agrees0 = bool(jid0) and bool(row["customer_name"]) and any(
                same_person(row["customer_name"], n) for n in index.journeys[jid0]["names"])
            first_choice[row["row_no"]] = (_BASIS_RANK.get(basis0 or "", 9), not agrees0,
                                           "CANCEL" in str(row.get("stage") or "").upper(), row["row_no"])
            compared.append(row["row_no"])
    order = {n: i for i, n in enumerate(sorted(compared, key=lambda n: first_choice[n]))}
    rows = sorted(rows, key=lambda r: order.get(r["row_no"], -1))
    taken: set[str] = set()
    seen_lines: dict[str, int] = {}
    for row in rows:
        if (model := row_out_of_scope(dict(row))) is not None:
            updates.append({"t": tenant_id, "id": str(dump_id), "n": row["row_no"], "j": None, "b": model,
                            "o": EXCLUDED, "ol": resolve_outlet(row["outlet_label"], outlets), "dt": None, "dtb": None,
                            "jc": None, "na": None})
            continue
        if row["event_date"] is not None and not first <= row["event_date"] <= last:
            updates.append({"t": tenant_id, "id": str(dump_id), "n": row["row_no"], "j": None, "b": None,
                            "o": OUT_OF_PERIOD, "ol": resolve_outlet(row["outlet_label"], outlets),
                            "dt": None, "dtb": None, "jc": None, "na": None})
            continue
        if (line := _line_key(kind, dict(row))) is not None and line in seen_lines:
            updates.append({"t": tenant_id, "id": str(dump_id), "n": row["row_no"], "j": None,
                            "b": f"ROW {seen_lines[line]}", "o": DUPLICATE,
                            "ol": resolve_outlet(row["outlet_label"], outlets), "dt": None, "dtb": None, "jc": None,
                            "na": None})
            continue
        if line is not None:
            seen_lines[line] = row["row_no"]
        jid, basis = match_row(index, kind, dict(row), taken)
        if jid:
            taken.add(jid)
        journey = index.journeys.get(jid) if jid else None
        outcome = outcome_of(kind, dict(row), journey)
        counts[outcome] += 1
        outlet_id = (str(journey["outletId"]) if journey and journey["outletId"]
                     else resolve_outlet(row["outlet_label"], outlets))
        deal_type, deal_basis = (scope.decide(dict(row), jid, journey) if scope is not None and outcome != CANCELLED
                                 else (None, None))
        # The customer the dump names is the Journey's? A match on the chassis, booking number or
        # mobile where it is not is listed for review (2026-11-19).
        name = row["customer_name"]
        agrees = (any(same_person(name, n) for n in journey["names"]) if journey is not None and name and journey["names"]
                  else None)
        updates.append({"t": tenant_id, "id": str(dump_id), "n": row["row_no"], "j": jid, "b": basis, "o": outcome,
                        "ol": outlet_id, "dt": deal_type, "dtb": deal_basis,
                        "jc": (journey or {}).get("customer"), "na": agrees})
        if jid and outcome != CANCELLED:
            synced.append(jid)
    return DumpPlan(dict(dump), updates, synced, counts)


def match_dump(connection: Connection, *, tenant_id: str, dump_id: UUID) -> dict[str, int]:
    """Match (again) every row of one dump, store the outcome and mark the Journeys found.
    A row dated outside dump_window is OUT_OF_PERIOD: kept, not compared, not counted.
    One match at a time per dump, whoever asks (upload, Match again, Refresh from Verigence,
    report generation or export, DMS_REMATCH): the others wait for it."""
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                       {"k": match_lock_key(tenant_id, dump_id)})
    plan = plan_dump(connection, tenant_id=tenant_id, dump_id=dump_id)
    kind, updates, synced, counts = plan.dump["dump_kind"], plan.updates, plan.synced, plan.counts
    if updates:
        connection.execute(
            text("""
                UPDATE auditcore.p2_dms_dump_rows SET journey_id = CAST(:j AS uuid), match_basis = :b, outcome = :o,
                       outlet_id = CAST(:ol AS uuid), deal_type = :dt, deal_type_basis = :dtb,
                       journey_customer = :jc, name_agrees = :na
                WHERE tenant_id = :t AND dump_id = :id AND row_no = :n
            """),
            updates,
        )
    if synced:
        column = "booking" if kind == BOOKING else "delivery"
        connection.execute(
            text(f"""
                INSERT INTO auditcore.p2_journey_dms_sync (tenant_id, journey_id, dms_{column}_synced_at, dms_{column}_dump_id)
                SELECT :t, CAST(j AS uuid), now(), :id FROM unnest(CAST(:js AS text[])) AS j
                ON CONFLICT (tenant_id, journey_id) DO UPDATE
                  SET dms_{column}_synced_at = COALESCE(auditcore.p2_journey_dms_sync.dms_{column}_synced_at, now()),
                      dms_{column}_dump_id = COALESCE(auditcore.p2_journey_dms_sync.dms_{column}_dump_id, EXCLUDED.dms_{column}_dump_id)
            """),
            {"t": tenant_id, "id": str(dump_id), "js": sorted(set(synced))},
        )
    connection.execute(
        text("""
            UPDATE auditcore.p2_dms_dumps SET matched_count = :m, not_in_tool_count = :nt, missing_count = :mi,
                   cancelled_count = :c, matched_at_utc = now()
            WHERE tenant_id = :t AND dump_id = :id
        """),
        {"t": tenant_id, "id": str(dump_id), "m": counts[MATCHED], "nt": counts[NOT_IN_TOOL],
         "mi": counts[MISSING], "c": counts[CANCELLED]},
    )
    return counts


# --------------------------------------------------------------------------- endpoints

def _dump_json(r: Any) -> dict[str, Any]:
    return {
        "dumpId": str(r["dump_id"]), "kind": r["dump_kind"], "dealerId": str(r["dealer_id"]),
        "dealerName": r.get("dealer_name"), "businessDate": r["business_date"].isoformat(),
        "periodMonth": r["period_month"].strftime("%Y-%m"), "fileName": r["file_name"], "format": r["file_format"],
        # The rows compared: an out-of-scope model or a day outside the dump's days is not one.
        "status": r["status"],
        "rows": sum(int(r[k] or 0) for k in ("matched_count", "not_in_tool_count", "missing_count", "cancelled_count"))
        if r["matched_at_utc"] else r["row_count"],
        "fileRows": r["row_count"], "matched": r["matched_count"],
        "notInTool": r["not_in_tool_count"], "missing": r["missing_count"], "cancelled": r["cancelled_count"],
        "uploadedBy": r["uploaded_by_name"] or r["uploaded_by"], "uploadedAtUtc": r["uploaded_at_utc"].isoformat(),
        "matchedAtUtc": r["matched_at_utc"].isoformat() if r["matched_at_utc"] else None,
    }


@router.get("/dms-dumps/setup")
def dms_dump_setup(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The upload types, the dealerships the caller covers and whether they may upload."""
    held = _access(connection, tenant_id, principal, client, _READ, READ_ROLES)
    admin = "ADMIN" in held
    return {"types": UPLOAD_TYPES, "dealers": _dealers(connection, tenant_id, principal.subject, admin),
            "canUpload": admin or bool(held & set(UPLOAD_ROLES)), "today": _today(connection, tenant_id).isoformat()}


@router.post("/dms-dumps", status_code=201)
def upload_dms_dump(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    file: Annotated[UploadFile, File()],
    dealerId: Annotated[UUID, Form()],
    businessDate: Annotated[date | None, Form()] = None,
    kind: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """One dealership's DMS booking or delivery dump (.xlsx). The kind is read from the
    file's columns (``kind`` only checks it); the rows are matched at once."""
    held = _access(connection, tenant_id, principal, client, _UPDATE, UPLOAD_ROLES)
    admin = "ADMIN" in held
    if str(dealerId) not in {d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject, admin)}:
        raise HTTPException(status_code=403, detail="That dealership is not one you cover.")
    data = file.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=422, detail="The file is larger than 15 MB.")
    name = (file.filename or "dump.xlsx")[:300]
    if not name.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=422, detail="Upload the report as an Excel .xlsx file.")
    parsed = parse_workbook(data)
    if kind and kind.upper() in (BOOKING, DELIVERY) and kind.upper() != parsed.kind:
        raise HTTPException(status_code=422, detail=(
            f"This file is a {parsed.kind.lower()} report, not a {kind.lower()} report."))
    today = _today(connection, tenant_id)
    business_date = businessDate or today
    if business_date > today:
        raise HTTPException(status_code=422, detail="The dump's date cannot be in the future.")
    if business_date < today - timedelta(days=62):
        raise HTTPException(status_code=422, detail="The dump's date is more than two months back.")
    dated = [r["event_date"] for r in parsed.rows if r["event_date"]]
    # The month the dump is for: its rows' latest date (a dump of 1 Nov may still be October's).
    period = (max(dated) if dated else business_date).replace(day=1)
    dump_id = uuid4()
    connection.execute(
        text("""
            UPDATE auditcore.p2_dms_dumps SET status = 'SUPERSEDED'
            WHERE tenant_id = :t AND dealer_id = :d AND dump_kind = :k AND business_date = :b AND status = 'ACTIVE'
        """),
        {"t": tenant_id, "d": str(dealerId), "k": parsed.kind, "b": business_date},
    )
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_dms_dumps (tenant_id, dump_id, dump_kind, dealer_id, business_date, period_month,
              file_name, file_format, sheet_name, row_count, uploaded_by, uploaded_by_name)
            VALUES (:t, :id, :k, :d, :b, :p, :f, :fmt, :s, :n, :by, :byn)
        """),
        {"t": tenant_id, "id": str(dump_id), "k": parsed.kind, "d": str(dealerId), "b": business_date, "p": period,
         "f": name, "fmt": parsed.file_format, "s": parsed.sheet_name, "n": len(parsed.rows),
         "by": principal.subject, "byn": _name_of(connection, principal.subject)},
    )
    import json

    connection.execute(
        text("""
            INSERT INTO auditcore.p2_dms_dump_rows (tenant_id, dump_id, row_no, dump_kind, dealer_id, outlet_label,
              event_date, booking_date, booking_number, dms_booking_number, customer_name, mobile, chassis,
              invoice_number, registration_number, model, variant, stage, consultant, raw)
            VALUES (:t, :id, :n, :k, :d, :outlet_label, :event_date, :booking_date, :booking_number, :dms_booking_number,
              :customer_name, :mobile, :chassis, :invoice_number, :registration_number, :model, :variant, :stage,
              :consultant, CAST(:raw AS jsonb))
        """),
        [{"t": tenant_id, "id": str(dump_id), "n": n, "k": parsed.kind, "d": str(dealerId),
          **{k: v for k, v in row.items() if k != "raw"}, "raw": json.dumps(row["raw"], default=str)}
         for n, row in enumerate(parsed.rows, start=1)],
    )
    counts = match_dump(connection, tenant_id=tenant_id, dump_id=dump_id)
    # The PCs are told at once to mark what was intimated to them; the report is generated
    # later, from the Reports section (product owner 2026-11-18).
    from audit_core.uc03_p2_dms_intimations import publish_dump_alert

    dealer_name = next(d["dealerName"] for d in _dealers(connection, tenant_id, principal.subject, admin)
                       if d["dealerId"] == str(dealerId))
    publish_dump_alert(connection, tenant_id=tenant_id, dealer_id=str(dealerId), dealer_name=dealer_name,
                       kind=parsed.kind, business_date=business_date)
    logger.info("dms_dump_uploaded", tenant_id=tenant_id, dump_id=str(dump_id), kind=parsed.kind,
                rows=len(parsed.rows), **{k.lower(): v for k, v in counts.items()})
    return _read_dump(connection, tenant_id, dump_id)


def _read_dump(connection: Connection, tenant_id: str, dump_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        text("""
            SELECT u.*, d.dealer_name FROM auditcore.p2_dms_dumps u
            JOIN auditcore.dealers d ON d.tenant_id = u.tenant_id AND d.dealer_id = u.dealer_id
            WHERE u.tenant_id = :t AND u.dump_id = :id
        """),
        {"t": tenant_id, "id": str(dump_id)},
    ).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No such upload.")
    return _dump_json(row)


@router.get("/dms-dumps")
def list_dms_dumps(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    month: str | None = None,
    dealer_id: UUID | None = None,
) -> dict[str, Any]:
    """The uploads of a month (current by default), newest day first; superseded ones
    are kept and marked."""
    held = _access(connection, tenant_id, principal, client, _READ, READ_ROLES)
    first = _month(month, _today(connection, tenant_id))
    dealer_ids = [d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)]
    rows = connection.execute(
        text("""
            SELECT u.*, d.dealer_name FROM auditcore.p2_dms_dumps u
            JOIN auditcore.dealers d ON d.tenant_id = u.tenant_id AND d.dealer_id = u.dealer_id
            WHERE u.tenant_id = :t AND u.business_date >= :f AND u.business_date < :l
              AND CAST(u.dealer_id AS text) = ANY(:ds)
              AND (CAST(:d AS uuid) IS NULL OR u.dealer_id = CAST(:d AS uuid))
            ORDER BY u.business_date DESC, lower(d.dealer_name), u.dump_kind, u.uploaded_at_utc DESC
        """),
        {"t": tenant_id, "f": first, "l": _next_month(first), "ds": dealer_ids,
         "d": str(dealer_id) if dealer_id else None},
    ).mappings().all()
    return {"month": first.strftime("%Y-%m"), "items": [_dump_json(r) for r in rows]}


def _next_month(first: date) -> date:
    return (first.replace(day=28) + timedelta(days=4)).replace(day=1)


_ROW_SQL = """
    SELECT r.*, u.business_date, u.status AS dump_status, d.dealer_name, o.outlet_name,
           j.journey_reference
    FROM auditcore.p2_dms_dump_rows r
    JOIN auditcore.p2_dms_dumps u ON u.tenant_id = r.tenant_id AND u.dump_id = r.dump_id
    JOIN auditcore.dealers d ON d.tenant_id = r.tenant_id AND d.dealer_id = r.dealer_id
    LEFT JOIN auditcore.dealer_outlets o ON o.tenant_id = r.tenant_id AND o.outlet_id = r.outlet_id
    LEFT JOIN auditcore.journeys j ON j.tenant_id = r.tenant_id AND j.journey_id = r.journey_id
    WHERE r.tenant_id = :t
"""


def _row_json(r: Any) -> dict[str, Any]:
    return {
        "rowNo": r["row_no"], "kind": r["dump_kind"], "outcome": r["outcome"], "matchBasis": r["match_basis"],
        "dealerName": r["dealer_name"], "outletName": r["outlet_name"], "outletLabel": r["outlet_label"],
        "eventDate": r["event_date"].isoformat() if r["event_date"] else None,
        "bookingNumber": r["booking_number"], "otf": r["dms_booking_number"], "customerName": r["customer_name"],
        "mobile": r["mobile"], "chassis": r["chassis"], "invoiceNumber": r["invoice_number"],
        "registrationNumber": r["registration_number"], "model": r["model"], "variant": r["variant"],
        "stage": r["stage"], "consultant": r["consultant"],
        "journeyId": str(r["journey_id"]) if r["journey_id"] else None, "journeyReference": r["journey_reference"],
        "dumpDate": r["business_date"].isoformat(),
        "dealType": r.get("deal_type"), "dealTypeBasis": r.get("deal_type_basis"),
    }


@router.get("/dms-dumps/{dump_id}")
def read_dms_dump(
    tenant_id: str,
    dump_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    outcome: Annotated[str | None, Query(pattern="^(MATCHED|NOT_IN_TOOL|MISSING|CANCELLED|NOT_IN_DMS)$")] = None,
) -> dict[str, Any]:
    """One upload and its rows (the not found first); for a delivery dump also the other side,
    the delivery Journeys dated in the days it covers that it does not carry (NOT_IN_DMS)."""
    _access(connection, tenant_id, principal, client, _READ, READ_ROLES)
    dump = _read_dump(connection, tenant_id, dump_id)
    extra = (_not_in_dump(connection, tenant_id, [dump_id])
             if dump["kind"] == DELIVERY and outcome in (None, "NOT_IN_DMS") else [])
    if outcome == "NOT_IN_DMS":
        return {**dump, "items": extra}
    rows = connection.execute(
        text(_ROW_SQL + """
            AND r.dump_id = :id AND (CAST(:o AS text) IS NULL OR r.outcome = CAST(:o AS text))
            AND r.outcome NOT IN ('OUT_OF_PERIOD', 'EXCLUDED', 'DUPLICATE')
            ORDER BY CASE r.outcome WHEN 'MISSING' THEN 0 WHEN 'NOT_IN_TOOL' THEN 1 WHEN 'CANCELLED' THEN 2 ELSE 3 END,
                     r.event_date DESC NULLS LAST, r.row_no
        """),
        {"t": tenant_id, "id": str(dump_id), "o": outcome},
    ).mappings().all()
    return {**dump, "items": [_row_json(r) for r in rows] + extra}


def _not_in_dump(connection: Connection, tenant_id: str, dump_ids: list[Any]) -> list[dict[str, Any]]:
    """The delivery Journeys of the days each delivery dump covers that it does not carry
    (product owner 2026-11-17: a daily dump is compared with Verigence's deliveries from the
    1st of its month to its day only)."""
    from audit_core.uc03_p2_delivery_dates import SOURCE_LABELS, verigence_not_in_dump

    today = _today(connection, tenant_id)
    out = []
    for dump in connection.execute(
        text("""
            SELECT u.dump_id, u.dealer_id, u.period_month, u.business_date, d.dealer_name
            FROM auditcore.p2_dms_dumps u
            JOIN auditcore.dealers d ON d.tenant_id = u.tenant_id AND d.dealer_id = u.dealer_id
            WHERE u.tenant_id = :t AND u.dump_kind = 'DELIVERY' AND CAST(u.dump_id AS text) = ANY(:ids)
        """),
        {"t": tenant_id, "ids": [str(i) for i in dump_ids]},
    ).mappings():
        for row in verigence_not_in_dump(connection, tenant_id=tenant_id, dump=dict(dump), today=today):
            row["dealerName"] = dump["dealer_name"]
            row["datedBy"] = SOURCE_LABELS.get(row["matchBasis"], row["matchBasis"])
            out.append(row)
    return out


@router.post("/dms-dumps/{dump_id}/rematch")
def rematch_dms_dump(
    tenant_id: str,
    dump_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Match the rows again (Journeys uploaded since are found)."""
    _access(connection, tenant_id, principal, client, _UPDATE, UPLOAD_ROLES)
    _read_dump(connection, tenant_id, dump_id)
    match_dump(connection, tenant_id=tenant_id, dump_id=dump_id)
    return _read_dump(connection, tenant_id, dump_id)


# --------------------------------------------------------------------------- Excel

_COLUMNS = (
    ("Dump date", "dumpDate"), ("Dealership", "dealerName"), ("Outlet", "outletName"),
    ("Location (as in dump)", "outletLabel"), ("Status", "outcomeLabel"), ("Date", "eventDate"),
    ("Booking number", "bookingNumber"), ("OTF", "otf"), ("Customer", "customerName"), ("Mobile", "mobile"),
    ("Chassis", "chassis"), ("Invoice no.", "invoiceNumber"), ("Regn. no.", "registrationNumber"),
    ("Model", "model"), ("Variant", "variant"), ("DMS stage", "stage"), ("Consultant", "consultant"),
    ("Journey", "journeyReference"), ("Matched on", "matchBasis"),
)
NOT_IN_DMS = "NOT_IN_DMS"
OUTCOME_LABELS = {
    MISSING: "Not in Verigence", NOT_IN_TOOL: "Booking on file, delivery not uploaded",
    MATCHED: "Synced", CANCELLED: "Cancelled in DMS", NOT_IN_DMS: "In Verigence, not in DMS dump",
}


def build_report(connection: Connection, *, tenant_id: str, dealer_ids: list[str], first: date,
                 dump_id: UUID | None = None, day: date | None = None) -> bytes:
    """The variance workbook of the month, one upload, or (``day``) the uploads of one day only
    (product owner 2026-11-18: the download reported every earlier run of the month; each
    daily dump is month to date, so one day's report is the whole picture as of that day)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    dumps = connection.execute(
        text("""
            SELECT u.*, d.dealer_name FROM auditcore.p2_dms_dumps u
            JOIN auditcore.dealers d ON d.tenant_id = u.tenant_id AND d.dealer_id = u.dealer_id
            WHERE u.tenant_id = :t AND u.business_date >= :f AND u.business_date < :l
              AND CAST(u.dealer_id AS text) = ANY(:ds) AND (CAST(:id AS uuid) IS NULL OR u.dump_id = CAST(:id AS uuid))
              AND (CAST(:day AS date) IS NULL OR u.business_date = CAST(:day AS date))
            ORDER BY u.business_date, lower(d.dealer_name), u.dump_kind
        """),
        {"t": tenant_id, "f": first, "l": _next_month(first), "ds": dealer_ids,
         "id": str(dump_id) if dump_id else None, "day": day},
    ).mappings().all()
    active = [str(d["dump_id"]) for d in dumps if d["status"] == "ACTIVE" or dump_id is not None]
    rows = connection.execute(
        text(_ROW_SQL + """
            AND CAST(r.dump_id AS text) = ANY(:ids) AND r.outcome IN ('MISSING', 'NOT_IN_TOOL')
            ORDER BY u.business_date, lower(d.dealer_name), r.event_date NULLS LAST, r.row_no
        """),
        {"t": tenant_id, "ids": active},
    ).mappings().all()
    workbook = Workbook()
    bold, head = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="161616")

    def header(sheet: Any, names: list[str]) -> None:
        sheet.append(names)
        for cell in sheet[1]:
            cell.font, cell.fill = bold, head
        sheet.freeze_panes = "A2"

    items = [_row_json(r) for r in rows]
    # The other side of each delivery dump: Verigence's deliveries of the days it covers
    # that it does not carry (2026-11-17).
    items += _not_in_dump(connection, tenant_id, [d["dump_id"] for d in dumps if str(d["dump_id"]) in active
                                                   and d["dump_kind"] == DELIVERY])
    not_in_dump: dict[tuple[str, str | None], int] = defaultdict(int)
    for item in items:
        if item["outcome"] == NOT_IN_DMS:
            not_in_dump[(item["dumpDate"], item["dealerName"])] += 1
    summary = workbook.active
    summary.title = "Summary"
    header(summary, ["Dump date", "Dealership", "Dump", "File", "Rows", "Synced", "Delivery not uploaded",
                     "Not in Verigence", "Cancelled in DMS", "In Verigence, not in dump", "Uploaded by",
                     "Uploaded at (UTC)", "Status"])
    for d in dumps:
        summary.append([d["business_date"], d["dealer_name"], d["dump_kind"].title(), d["file_name"], d["row_count"],
                        d["matched_count"], d["not_in_tool_count"], d["missing_count"], d["cancelled_count"],
                        not_in_dump.get((d["business_date"].isoformat(), d["dealer_name"]), 0)
                        if d["dump_kind"] == DELIVERY and str(d["dump_id"]) in active else None,
                        d["uploaded_by_name"] or d["uploaded_by"], d["uploaded_at_utc"].replace(tzinfo=None),
                        "Current" if d["status"] == "ACTIVE" else "Replaced"])
    for item in items:
        item["outcomeLabel"] = OUTCOME_LABELS[item["outcome"]]
        if item["outcome"] == NOT_IN_DMS:
            item["matchBasis"] = f"Dated by {item['datedBy'].lower()}"
    for title, kind in (("Booking dump variance", BOOKING), ("Delivery dump variance", DELIVERY)):
        sheet = workbook.create_sheet(title)
        header(sheet, [c for c, _ in _COLUMNS])
        for item in items:
            if item["kind"] == kind:
                sheet.append([item.get(key) for _, key in _COLUMNS])
    # One sheet per day a dump was uploaded (product owner: "with each date wise when the
    # dump was uploaded report should be available").
    for sheet_day in sorted({d["business_date"] for d in dumps if str(d["dump_id"]) in active}):
        sheet = workbook.create_sheet(f"{sheet_day:%d-%b}")
        header(sheet, [c for c, _ in _COLUMNS])
        for item in items:
            if item["dumpDate"] == sheet_day.isoformat():
                sheet.append([item.get(key) for _, key in _COLUMNS])
    for sheet in workbook.worksheets:
        for column in sheet.columns:
            width = max((len(str(c.value)) for c in column if c.value is not None), default=8)
            sheet.column_dimensions[column[0].column_letter].width = min(max(width + 2, 10), 48)
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


@router.get("/dms-dumps/report/file")
def export_dms_dump_report(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    month: str | None = None,
    dump_id: UUID | None = None,
    day: date | None = None,
) -> StreamingResponse:
    """Export to Excel: the missing bookings and deliveries of one day's uploads (``day``), one
    upload, or the month -- Summary, Booking dump variance, Delivery dump variance, and a
    sheet per dump day."""
    held = _access(connection, tenant_id, principal, client, _READ, READ_ROLES)
    first = day.replace(day=1) if day else _month(month, _today(connection, tenant_id))
    dealer_ids = [d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)]
    if day is not None:
        # Matched again first: Journeys uploaded (or read) since the dump came in are found,
        # so the day's report is as of now, not as of its upload (2026-11-18).
        for dump in connection.execute(
            text("SELECT dump_id FROM auditcore.p2_dms_dumps WHERE tenant_id = :t AND status = 'ACTIVE' "
                 "AND business_date = :d AND CAST(dealer_id AS text) = ANY(:ds)"),
            {"t": tenant_id, "d": day, "ds": dealer_ids},
        ).scalars().all():
            match_dump(connection, tenant_id=tenant_id, dump_id=dump)
    data = build_report(connection, tenant_id=tenant_id, dealer_ids=dealer_ids, first=first, dump_id=dump_id,
                        day=day)
    name = f"DMS-Dump-Variance_{day:%Y-%m-%d}.xlsx" if day else f"DMS-Dump-Variance_{first:%Y-%m}.xlsx"
    return StreamingResponse(io.BytesIO(data), media_type=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


# --------------------------------------------------------------------------- the tracker's calendar

def dump_calendar(connection: Connection, *, tenant_id: str, dealer_ids: list[str], first: date, today: date
                  ) -> dict[str, Any]:
    """Per dealership and day of the month: the booking and delivery dump on file (with
    its not-found counts), MISSED for a past day with none, DUE today, nothing ahead.
    The dumps are daily (product owner); every day counts, Sunday included."""
    last = _next_month(first) - timedelta(days=1)
    dealers = connection.execute(
        text("SELECT dealer_id, dealer_name FROM auditcore.dealers WHERE tenant_id = :t "
             "AND CAST(dealer_id AS text) = ANY(:ds) ORDER BY lower(dealer_name)"),
        {"t": tenant_id, "ds": dealer_ids},
    ).mappings().all()
    dumps = connection.execute(
        text("""
            SELECT dealer_id, dump_kind, business_date, dump_id, missing_count, not_in_tool_count,
                   COALESCE(matched_count + not_in_tool_count + missing_count + cancelled_count, row_count) AS row_count
            FROM auditcore.p2_dms_dumps
            WHERE tenant_id = :t AND status = 'ACTIVE' AND business_date BETWEEN :f AND :l
              AND CAST(dealer_id AS text) = ANY(:ds)
        """),
        {"t": tenant_id, "f": first, "l": last, "ds": dealer_ids},
    ).mappings().all()
    on_file = {(str(d["dealer_id"]), d["dump_kind"], d["business_date"]): d for d in dumps}
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    out = []
    totals = {"due": 0, "uploaded": 0, "missed": 0}
    for dealer in dealers:
        cells = []
        for day in days:
            cell: dict[str, Any] = {"date": day.isoformat()}
            for kind in (BOOKING, DELIVERY):
                dump = on_file.get((str(dealer["dealer_id"]), kind, day))
                if dump is not None:
                    status = "UPLOADED"
                elif day < today:
                    status = "MISSED"
                elif day == today:
                    status = "DUE"
                else:
                    status = "AHEAD"
                if status in ("UPLOADED", "MISSED"):
                    totals["due"] += 1
                totals["uploaded"] += status == "UPLOADED"
                totals["missed"] += status == "MISSED"
                cell[kind.lower()] = {"status": status,
                                      **({"dumpId": str(dump["dump_id"]), "rows": dump["row_count"],
                                          "missing": dump["missing_count"], "notInTool": dump["not_in_tool_count"]}
                                         if dump is not None else {})}
            cells.append(cell)
        out.append({"dealerId": str(dealer["dealer_id"]), "dealerName": dealer["dealer_name"], "days": cells})
    return {"month": first.strftime("%Y-%m"), "today": today.isoformat(), "totals": totals, "dealers": out}


@router.get("/reports/dms-dump-calendar")
def get_dms_dump_calendar(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    month: str | None = None,
) -> dict[str, Any]:
    """The DMS dumps' daily status for the Project Tracker, like the gate register
    (product owner 2026-11-13: "this is supposed to come daily. But this is only for TL")."""
    held = _access(connection, tenant_id, principal, client, _READ, TRACKER_ROLES)
    today = _today(connection, tenant_id)
    first = _month(month, today)
    dealer_ids = [d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)]
    return dump_calendar(connection, tenant_id=tenant_id, dealer_ids=dealer_ids, first=first, today=today)


__all__ = [
    "booking_keys",
    "build_index",
    "chassis_key",
    "detect_kind",
    "dms_dates",
    "dump_calendar",
    "journey_ref_keys",
    "match_dump",
    "match_row",
    "mobile_key",
    "outcome_of",
    "outlet_for",
    "parse_date",
    "parse_workbook",
    "router",
]
