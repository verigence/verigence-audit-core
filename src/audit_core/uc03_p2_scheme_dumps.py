"""uc03_p2_scheme_dumps.py -- the dealers' monthly extended warranty (EW) and RSA dumps,
checked against what Verigence shows the customer bought (product owner 2026-11-17).

"upload use case, now it is Extended warranty and RSA ... this is monthly upload, what we
need to do is compare the name of the customers / mobile / vin from the excel sheet and
found matching record in verigence and then need to check if they have bought EW and RSA
... we need to report which all have bought and in sync, second is customer who didn't
bought as per verigence, and third is customers who are not in this list."

The dumps (samples of October 2026, all three dealers): Mahindra's scheme registers, one
scheme a row, as .xls (Aditya) or .xlsx (Shivnath, Utkal).

* EW  ("Shield"): SCHEME REGISTRATION ID, Scheme Reg. Date, Customer ID, Model Family,
  Scheme Desc., Sold By (SALS / SERV), Category (PAID / FOC ...), Vehicle Reg. Number,
  Chasiss Number, Km, Final Scheme Amount, Scheme Status, Cancelled Date, Location.
* RSA: Scheme Invoice No, Scheme Reg Date, Customer ID, Model Family, Scheme desc, Vehicle
  Reg No, Chassis No, KM, Employee Designation, Sold By, Status, RSA Cancelled Date, Amount,
  Total RSA amount, Location.

They print no customer name and no mobile: the vehicle's short chassis (the last 8 of the
VIN), the DMS customer id and the registration number are what there is. A row is matched
to a Journey of the same dealership on, in this order:

  CHASSIS        the last 8 characters of the chassis / VIN on the Journey's own papers
                 (uc03_p2_dms_dumps.CHASSIS_DOCUMENT_TYPES)
  CUSTOMER_ID    the DMS customer id the dealer's invoice prints (R270905009), when one
                 Journey has it
  REGISTRATION   the vehicle's registration number, when one Journey has it
  MOBILE / NAME_MODEL  only when a dump does carry the customer's mobile or name
                 (uc03_p2_dms_dumps.match_row)

Half of a dump is not a new-vehicle sale at all: schemes the workshop sells on older
vehicles (Sold By SERV, or a Service Manager's entry). Those are kept and counted, and
shown apart: no Journey is expected for them.

What Verigence shows is the one rule for "RSA / EW sold" (product owner 2026-11-02): the
customer was charged for it on the NDC / customer ledger, the cost sheet or a customer
invoice -- uc03_p2_stage.condition_reasons, never the booking form. It is decided per
Journey and kept (p2_scheme_journey_checks) when a dump is uploaded, on "Check again" and
every night with the nightly review, because it takes several reads a Journey.

The report, per type and month:

  IN_SYNC             in the dump, and Verigence shows the customer was charged
  NOT_BOUGHT          in the dump, the Journey found, but Verigence shows no charge --
                      apart: the NDC / ledger is on file and has no such line, or it is
                      not on file yet
  NOT_IN_DUMP         Verigence shows the customer was charged, the vehicle is in no
                      current dump of the dealership (a cancelled registration does not
                      count). Journeys of the month (invoice date, else the gate pass's
                      delivery date, else the delivery upload) at the outlets the month's
                      dumps cover -- a dump naming its Location covers that DMS location's
                      outlets only (Aditya uploads Jajpur apart from the rest)
  NOT_IN_VERIGENCE    in the dump, sold by sales, no Journey found
  (sold by service, no Journey: counted and exported, not a finding)

``GET  /p2/v1/tenants/{t}/scheme-dumps/setup``
``POST /p2/v1/tenants/{t}/scheme-dumps``                    (file, dealerId, kind?)
``GET  /p2/v1/tenants/{t}/scheme-dumps?month=YYYY-MM``
``POST /p2/v1/tenants/{t}/scheme-dumps/recheck``            ({month, dealerId?})
``GET  /p2/v1/tenants/{t}/scheme-dumps/report?month=&kind=EW|RSA``
``GET  /p2/v1/tenants/{t}/scheme-dumps/report/file?month=`` (Excel, both types)
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_dms_dumps import (
    _READ,
    _UPDATE,
    DELIVERY,
    READ_ROLES,
    UPLOAD_ROLES,
    JourneyIndex,
    _access,
    _dealers,
    _month,
    _name_of,
    _today,
    build_index,
    chassis_key,
    customer_code_key,
    match_row,
    mobile_key,
    parse_date,
    registration_key,
)
from audit_core.uc03_p2_dms_outlet_report import label_key, outlet_map, resolve_outlet

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-scheme-dumps"])

MAX_BYTES = 15 * 1024 * 1024
MAX_ROWS = 20000
EW, RSA = "EW", "RSA"
KINDS = (EW, RSA)
KIND_LABELS = {EW: "Extended warranty", RSA: "RSA"}
SCHEME_TYPES = [
    {"key": EW, "label": "Extended warranty dump", "frequency": "MONTHLY"},
    {"key": RSA, "label": "RSA dump", "frequency": "MONTHLY"},
]
SALES, SERVICE, UNKNOWN = "SALES", "SERVICE", "UNKNOWN"
CHANNEL_LABELS = {SALES: "Sales", SERVICE: "Service", UNKNOWN: "Not stated"}
IN_SYNC, NOT_BOUGHT, NOT_IN_DUMP, NOT_IN_VERIGENCE, SERVICE_ONLY, CANCELLED = (
    "IN_SYNC", "NOT_BOUGHT", "NOT_IN_DUMP", "NOT_IN_VERIGENCE", "SERVICE_ONLY", "CANCELLED")
BASIS_LABELS = {"CHASSIS": "Chassis", "CUSTOMER_ID": "DMS customer id", "REGISTRATION": "Registration number",
                "MOBILE": "Mobile", "NAME_MODEL": "Name and model"}
# The condition of the one "sold" rule each type stands for (uc03_p2_stage.CHARGED_ONLY_CONDITIONS).
CONDITIONS = {EW: "ewSold", RSA: "rsaSold"}
# How far back the nightly check looks: the dumps of this month and the two before it.
RECENT_MONTHS = 3


# --------------------------------------------------------------------------- reading a dump

def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


_COLUMNS: dict[str, tuple[str, ...]] = {
    "scheme_number": ("schemeregistrationid", "schemeinvoiceno", "schemeinvoicenumber", "schemeregistrationno"),
    "scheme_date": ("schemeregdate", "schemeregistrationdate"),
    "customer_code": ("customerid", "customercode"),
    "gstin": ("gstinuinofcustomer", "customergstin", "gstin"),
    "customer_name": ("customername", "nameofcustomer", "insuredname"),
    "mobile": ("mobile", "mobileno", "mobilenumber", "customermobile", "customerphone", "customermobilephone",
               "phoneno", "contactno"),
    "model_family": ("modelfamily",),
    "model": ("modeldesc", "modeldescription"),
    "scheme": ("schemedesc", "schemedescription"),
    "sold_by": ("soldby",),
    "category": ("category",),
    "registration_number": ("vehicleregno", "vehicleregnumber", "registrationno", "registrationnumber", "regno"),
    "chassis": ("chassisno", "chasissnumber", "chassisnumber", "chasisno", "vin", "vinno"),
    "km": ("km", "kms"),
    "employee": ("employeename",),
    "designation": ("employeedesignation",),
    "status": ("schemestatus", "status"),
    "amount": ("finalschemeamount", "amount"),
    "cancelled_date": ("rsacancelleddate", "cancelleddate"),
    "location": ("location",),
}
_TAX_COLUMNS = ("sgstamount", "cgstamount", "igstamount")


@dataclass
class ParsedScheme:
    kind: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    duplicates: int = 0
    file_format: str = ""
    locations: list[str] = field(default_factory=list)


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        value = value.replace("\xa0", " ").strip().lstrip("`").strip()
        return value or None
    return value


def _text(value: Any, limit: int) -> str | None:
    value = _clean(value)
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    out = re.sub(r"\s+", " ", str(value)).strip()
    return out[:limit] or None


def _number(value: Any) -> Decimal | None:
    value = _clean(value)
    if value is None or value == "" or isinstance(value, datetime | date):
        return None
    if isinstance(value, int | float | Decimal):
        return Decimal(str(value))
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    try:
        return Decimal(cleaned) if cleaned not in ("", "-", ".") else None
    except InvalidOperation:
        return None


def scheme_day(value: Any) -> date | None:
    """A registration's date: a date, a date as text, or the day number Excel keeps for one
    (46296 is 1 October 2026) where the export left the column unformatted."""
    value = _clean(value)
    if isinstance(value, int | float) and not isinstance(value, bool) and 20000 <= value <= 80000:
        return date(1899, 12, 30) + timedelta(days=int(value))
    return parse_date(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def channel_of(sold_by: Any, designation: Any) -> str:
    """Who sold the scheme: the showroom with a new vehicle (SALS) or the workshop on a
    vehicle already on the road (SERV). Where Sold By is empty (the RSA register leaves
    it so for the workshop's entries) the employee's designation says it."""
    sold = _norm(sold_by)
    if sold.startswith("sal"):
        return SALES
    if sold.startswith("ser"):
        return SERVICE
    role = str(designation or "").upper()
    if any(w in role for w in ("SERVICE", "BODYSHOP", "BODY SHOP", "WORKSHOP", "WORKS MANAGER", "PARTS")):
        return SERVICE
    if any(w in role for w in ("SALES", "CONSULTANT", "SHOWROOM", "SC-")):
        return SALES
    return UNKNOWN


def detect_kind(headers: list[Any], schemes: list[Any]) -> str | None:
    """EW or RSA, from the register's own columns, else from what the schemes are called."""
    normalized = [_norm(h) for h in headers]
    if any("rsa" in h for h in normalized):
        return RSA
    if "schemeregistrationid" in normalized or any("coolingperiod" in h or "warranty" in h for h in normalized):
        return EW
    names = " ".join(str(s or "").upper() for s in schemes[:200])
    ew, rsa = names.count("WARRANTY") + names.count("SHIELD"), names.count("RSA")
    if ew or rsa:
        return EW if ew >= rsa else RSA
    return None


def _model_name(model: str | None, family: str | None) -> str | None:
    """'XUV7XO AX5 DSL MT 7 STR EWHT', else the family without its code ('J71 - XUV 7XO Diesel MT')."""
    if model:
        return model
    if family:
        return re.sub(r"^[A-Z0-9]{2,4}\s*-\s*", "", family).strip() or family
    return None


def _table(headers: list[Any], body: list[list[Any]], file_format: str, kind: str | None) -> ParsedScheme:
    normalized = [_norm(h) for h in headers]
    columns: dict[str, int] = {}
    for key, names in _COLUMNS.items():
        for name in names:  # the names in their order of preference; a repeated header: its first column
            if name in normalized:
                columns[key] = normalized.index(name)
                break
    if "chassis" not in columns and "registration_number" not in columns:
        raise HTTPException(status_code=422, detail=(
            "This is not an extended warranty or RSA dump: a Chassis No or Vehicle Reg No column is needed."))

    def cell(r: list[Any], key: str) -> Any:
        i = columns.get(key)
        return r[i] if i is not None and i < len(r) else None

    found = detect_kind(headers, [cell(r, "scheme") for r in body])
    if found is None and kind is None:
        raise HTTPException(status_code=422, detail=(
            "Could not tell whether this is the extended warranty or the RSA dump: choose the type."))
    if kind and found and kind != found:
        raise HTTPException(status_code=422, detail=(
            f"This file reads as the {KIND_LABELS[found]} dump, not the {KIND_LABELS[kind]} dump."))
    out = ParsedScheme(kind=kind or found or "", file_format=file_format)
    # The scheme's own tax columns are the ones that follow its amount (the register
    # repeats SGST / CGST / IGST for the dealer's incentive).
    tax_at: list[int] = []
    if "amount" in columns:
        tax_at = [i for i in range(columns["amount"] + 1, min(columns["amount"] + 6, len(normalized)))
                  if normalized[i] in _TAX_COLUMNS]
    seen: set[str] = set()
    locations: dict[str, str] = {}
    for r in body[:MAX_ROWS]:
        chassis = _text(cell(r, "chassis"), 40)
        registration = _text(cell(r, "registration_number"), 40)
        number = _text(cell(r, "scheme_number"), 80)
        if not (chassis_key(chassis) or registration_key(registration)):
            continue  # a blank or a total line
        scheme = _text(cell(r, "scheme"), 300)
        designation = _text(cell(r, "designation"), 200)
        status = _text(cell(r, "status"), 80)
        cancelled_on = scheme_day(cell(r, "cancelled_date"))
        tax = [n for n in (_number(r[i]) if i < len(r) else None for i in tax_at) if n is not None]
        location = _text(cell(r, "location"), 200)
        row = {
            "scheme_number": number,
            "scheme_date": scheme_day(cell(r, "scheme_date")),
            "scheme": scheme,
            "customer_code": customer_code_key(cell(r, "customer_code")),
            "gstin": _text(cell(r, "gstin"), 20),
            "customer_name": _text(cell(r, "customer_name"), 300),
            "mobile": mobile_key(_clean(cell(r, "mobile"))),
            "chassis": re.sub(r"[^A-Z0-9]", "", chassis.upper()) if chassis else None,
            "registration_number": registration_key(registration),
            "model": _model_name(_text(cell(r, "model"), 200), _text(cell(r, "model_family"), 200)),
            "sold_by": _text(cell(r, "sold_by"), 40),
            "channel": channel_of(cell(r, "sold_by"), designation),
            "category": _text(cell(r, "category"), 80),
            "km": int(km) if (km := _number(cell(r, "km"))) is not None and 0 <= km < 10_000_000 else None,
            "employee": _text(cell(r, "employee"), 200),
            "scheme_status": status,
            "cancelled": cancelled_on is not None or "CANCEL" in (status or "").upper(),
            "cancelled_on": cancelled_on,
            "scheme_amount": _number(cell(r, "amount")),
            "tax_amount": sum(tax, Decimal(0)) if tax else None,
            "location": location,
            "raw": {str(h): _jsonable(_clean(v)) for h, v in zip(headers, r, strict=False)
                    if h not in (None, "") and _clean(v) not in (None, "")},
        }
        key = _norm(number) or f"{row['chassis']}|{_norm(scheme)}|{row['scheme_date']}"
        if key in seen:
            out.duplicates += 1  # the same registration pasted twice: the first is kept
            continue
        seen.add(key)
        out.rows.append(row)
        if location and label_key(location) not in locations:
            locations[label_key(location)] = location
    if not out.rows:
        raise HTTPException(status_code=422, detail="No scheme registrations were found in this file.")
    out.locations = sorted(locations.values())
    return out


def _header_at(rows: list[list[Any]]) -> int | None:
    for at in range(min(15, len(rows))):
        normalized = {_norm(v) for v in rows[at]}
        if normalized & set(_COLUMNS["chassis"]) and normalized & (set(_COLUMNS["scheme"]) | set(_COLUMNS["scheme_number"])):
            return at
    return None


def _sheets(name: str, data: bytes) -> tuple[str, list[list[list[Any]]]]:
    """(format, the rows of each sheet): .xlsx, the old .xls Aditya's DMS exports, or .csv."""
    lower = name.lower()
    if data[:4] == b"PK\x03\x04":
        from openpyxl import load_workbook

        try:
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:
            raise HTTPException(status_code=422, detail="The file could not be read as an Excel (.xlsx) workbook.") from exc
        try:
            sheets = []
            for sheet in workbook.worksheets:
                values: list[list[Any]] = []
                for n, row in enumerate(sheet.iter_rows(values_only=True)):
                    values.append(list(row))
                    if n > MAX_ROWS + 20:
                        break
                sheets.append(values)
            return "XLSX", sheets
        finally:
            workbook.close()
    if data[:4] == b"\xd0\xcf\x11\xe0":
        import xlrd

        try:
            book = xlrd.open_workbook(file_contents=data)
        except Exception as exc:
            raise HTTPException(status_code=422, detail="The file could not be read as an Excel (.xls) workbook.") from exc
        sheets = []
        for sheet in book.sheets():
            values = []
            for r in range(min(sheet.nrows, MAX_ROWS + 20)):
                row = []
                for c in sheet.row(r):
                    value = c.value
                    if c.ctype == xlrd.XL_CELL_DATE:
                        try:
                            value = xlrd.xldate.xldate_as_datetime(c.value, book.datemode)
                        except (ValueError, OverflowError, xlrd.xldate.XLDateError):
                            value = c.value
                    row.append(value)
                values.append(row)
            sheets.append(values)
        return "XLS", sheets
    if lower.endswith((".csv", ".txt")):
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                content = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        return "CSV", [[list(r) for r in csv.reader(io.StringIO(content))]]
    raise HTTPException(status_code=422, detail="Upload the dump as Excel (.xlsx or .xls) or CSV.")


def parse_scheme_file(name: str, data: bytes, kind: str | None = None) -> ParsedScheme:
    """The scheme registrations of an EW or RSA dump; the type from its own columns."""
    file_format, sheets = _sheets(name, data)
    for rows in sheets:
        at = _header_at(rows)
        if at is not None:
            return _table(rows[at], rows[at + 1:], file_format, kind)
    raise HTTPException(status_code=422, detail=(
        "No header row with a Chassis No and a Scheme description was found: this is not an "
        "extended warranty or RSA dump."))


def period_of(rows: list[dict[str, Any]]) -> date | None:
    """The month most registrations fall in (a dump of October may hold one of 1 November)."""
    months = Counter(r["scheme_date"].replace(day=1) for r in rows if r["scheme_date"])
    return max(months, key=lambda m: (months[m], m)) if months else None


# --------------------------------------------------------------------------- matching

def match_scheme_row(index: JourneyIndex, row: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """(journey id, basis, note). A chassis two Journeys carry goes to the one with a
    delivery file; where that does not settle it, to neither (note SEVERAL_JOURNEYS)."""
    note = None
    if (key := chassis_key(row.get("chassis"))) is not None and (found := index.by_chassis.get(key)):
        if len(found) == 1:
            return next(iter(found)), "CHASSIS", None
        delivered = {j for j in found if index.journeys[j]["hasDelivery"]}
        if len(delivered) == 1:
            return next(iter(delivered)), "CHASSIS", None
        note = "SEVERAL_JOURNEYS"
    if (key := row.get("customer_code")) and len(found := index.by_customer_code.get(key, set())) == 1:
        return next(iter(found)), "CUSTOMER_ID", None
    if (key := row.get("registration_number")) and len(found := index.by_registration.get(key, set())) == 1:
        return next(iter(found)), "REGISTRATION", None
    if row.get("mobile") or row.get("customer_name"):
        jid, basis = match_row(index, DELIVERY, {"mobile": row.get("mobile"), "customer_name": row.get("customer_name"),
                                                 "model": row.get("model")})
        if jid is not None:
            return jid, basis, None
    return None, None, note


def match_scheme_dump(connection: Connection, *, tenant_id: str, dump_id: UUID | str,
                      index: JourneyIndex | None = None) -> int:
    """Match (again) every row of one dump to a Journey of its dealership."""
    dealer_id = connection.execute(
        text("SELECT dealer_id FROM auditcore.p2_scheme_dumps WHERE tenant_id = :t AND dump_id = :id"),
        {"t": tenant_id, "id": str(dump_id)},
    ).scalar_one()
    if index is None:
        index = build_index(connection, tenant_id=tenant_id, dealer_id=dealer_id)
    rows = connection.execute(
        text("""
            SELECT row_no, chassis, customer_code, registration_number, mobile, customer_name, model
            FROM auditcore.p2_scheme_dump_rows WHERE tenant_id = :t AND dump_id = :id ORDER BY row_no
        """),
        {"t": tenant_id, "id": str(dump_id)},
    ).mappings().all()
    updates, matched = [], 0
    for r in rows:
        jid, basis, note = match_scheme_row(index, dict(r))
        matched += jid is not None
        updates.append({"t": tenant_id, "id": str(dump_id), "n": r["row_no"], "j": jid, "b": basis, "note": note})
    if updates:
        connection.execute(
            text("""
                UPDATE auditcore.p2_scheme_dump_rows
                SET journey_id = CAST(:j AS uuid), match_basis = :b, match_note = :note
                WHERE tenant_id = :t AND dump_id = :id AND row_no = :n
            """),
            updates,
        )
    connection.execute(
        text("UPDATE auditcore.p2_scheme_dumps SET matched_count = :m, matched_at_utc = now() "
             "WHERE tenant_id = :t AND dump_id = :id"),
        {"t": tenant_id, "id": str(dump_id), "m": matched},
    )
    return matched


# --------------------------------------------------------------------------- what Verigence shows

def _evidence_keys() -> list[str]:
    """Every figure the "sold" rule reads for EW and RSA (uc03_p2_stage._AMOUNT_EVIDENCE)."""
    from audit_core.uc03_p2_stage import _AMOUNT_EVIDENCE

    return sorted({k for kind in KINDS for k in _AMOUNT_EVIDENCE[CONDITIONS[kind]]})


def charge_candidates(connection: Connection, *, tenant_id: str, dealer_id: str) -> set[str]:
    """The dealership's Journeys that carry an EW or RSA figure on a document the "sold"
    rule accepts (the rule's own keys, compared as it compares them). Only these can come
    out sold, so only these are put to the rule (condition_reasons reads a Journey in several
    queries): a wider net than the rule, never the rule itself."""
    from audit_core.uc03_p2_stage import counts_as_evidence

    rows = connection.execute(
        text("""
            SELECT DISTINCT CAST(s.journey_id AS text) AS journey_id, s.source_document_type AS doc
            FROM auditcore.commercial_line_source_values s
            JOIN auditcore.journeys j ON j.tenant_id = s.tenant_id AND j.journey_id = s.journey_id
            WHERE s.tenant_id = :t AND j.dealer_id = CAST(:d AS uuid) AND s.withdrawn_at_utc IS NULL
              AND s.amount > 0 AND s.component_key = ANY(:keys)
            UNION
            SELECT DISTINCT CAST(f.journey_id AS text), e.document_type_key
            FROM auditcore.journey_document_extracted_fields f
            JOIN auditcore.evidence e
              ON e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id
             AND e.di_document_id = f.di_document_id AND e.association_status = 'ACTIVE'
            JOIN auditcore.journeys j ON j.tenant_id = f.tenant_id AND j.journey_id = f.journey_id
            WHERE f.tenant_id = :t AND j.dealer_id = CAST(:d AS uuid) AND f.field_key = ANY(:keys)
        """),
        {"t": tenant_id, "d": str(dealer_id), "keys": _evidence_keys()},
    ).all()
    return {jid for jid, doc in rows if any(counts_as_evidence(CONDITIONS[kind], doc) for kind in KINDS)}


def refresh_checks(connection: Connection, *, tenant_id: str, dealer_id: str) -> int:
    """Decide (again) what Verigence shows for the dealership's Journeys the report reads:
    those a current dump row is matched to, those that may have been charged an EW or RSA,
    and those decided before (a charge since withdrawn). Returns how many were decided."""
    from audit_core.uc03_account_statement import COST_SHEET_TYPE
    from audit_core.uc03_deal_source_policy import ACCOUNT_STATEMENT_TYPE
    from audit_core.uc03_p2_stage import condition_reasons

    dealer = str(dealer_id)
    candidates = charge_candidates(connection, tenant_id=tenant_id, dealer_id=dealer)
    matched = set(connection.execute(
        text("""
            SELECT DISTINCT CAST(r.journey_id AS text) FROM auditcore.p2_scheme_dump_rows r
            JOIN auditcore.p2_scheme_dumps u ON u.tenant_id = r.tenant_id AND u.dump_id = r.dump_id AND u.status = 'ACTIVE'
            WHERE r.tenant_id = :t AND r.dealer_id = CAST(:d AS uuid) AND r.journey_id IS NOT NULL
        """),
        {"t": tenant_id, "d": dealer},
    ).scalars())
    before = set(connection.execute(
        text("SELECT CAST(journey_id AS text) FROM auditcore.p2_scheme_journey_checks "
             "WHERE tenant_id = :t AND dealer_id = CAST(:d AS uuid)"),
        {"t": tenant_id, "d": dealer},
    ).scalars())
    on_file = set(connection.execute(
        text("""
            SELECT DISTINCT CAST(e.journey_id AS text) FROM auditcore.evidence e
            JOIN auditcore.journeys j ON j.tenant_id = e.tenant_id AND j.journey_id = e.journey_id
            WHERE e.tenant_id = :t AND j.dealer_id = CAST(:d AS uuid) AND e.association_status = 'ACTIVE'
              AND e.document_type_key = ANY(:types)
        """),
        {"t": tenant_id, "d": dealer, "types": [ACCOUNT_STATEMENT_TYPE, COST_SHEET_TYPE]},
    ).scalars())
    rows = []
    for jid in sorted(candidates | matched | before):
        reasons = (condition_reasons(connection, tenant_id=tenant_id, journey_id=UUID(jid))
                   if jid in candidates else {})
        rows.append({
            "t": tenant_id, "j": jid, "d": dealer,
            "ew": CONDITIONS[EW] in reasons, "ewr": reasons.get(CONDITIONS[EW]),
            "rsa": CONDITIONS[RSA] in reasons, "rsar": reasons.get(CONDITIONS[RSA]),
            "papers": jid in on_file,
        })
    if rows:
        connection.execute(
            text("""
                INSERT INTO auditcore.p2_scheme_journey_checks (tenant_id, journey_id, dealer_id, ew_sold, ew_reason,
                  rsa_sold, rsa_reason, papers_on_file, checked_at_utc)
                VALUES (:t, CAST(:j AS uuid), CAST(:d AS uuid), :ew, :ewr, :rsa, :rsar, :papers, now())
                ON CONFLICT (tenant_id, journey_id) DO UPDATE
                  SET ew_sold = EXCLUDED.ew_sold, ew_reason = EXCLUDED.ew_reason, rsa_sold = EXCLUDED.rsa_sold,
                      rsa_reason = EXCLUDED.rsa_reason, papers_on_file = EXCLUDED.papers_on_file,
                      dealer_id = EXCLUDED.dealer_id, checked_at_utc = now()
            """),
            rows,
        )
    return len(rows)


def _recent_from(today: date) -> date:
    """The first of the month RECENT_MONTHS - 1 months before this one."""
    first = today.replace(day=1)
    for _ in range(RECENT_MONTHS - 1):
        first = (first - timedelta(days=1)).replace(day=1)
    return first


def recheck_dealer(connection: Connection, *, tenant_id: str, dealer_id: str, first: date | None = None) -> dict[str, int]:
    """Match the dealership's current dumps again (of one month, or the recent ones) and
    decide again what Verigence shows."""
    since = first or _recent_from(_today(connection, tenant_id))
    dumps = connection.execute(
        text(f"""
            SELECT dump_id FROM auditcore.p2_scheme_dumps
            WHERE tenant_id = :t AND dealer_id = CAST(:d AS uuid) AND status = 'ACTIVE'
              AND period_month {'=' if first is not None else '>='} :m
        """),
        {"t": tenant_id, "d": str(dealer_id), "m": since},
    ).scalars().all()
    if not dumps:
        return {"dumps": 0, "journeys": 0}
    index = build_index(connection, tenant_id=tenant_id, dealer_id=UUID(str(dealer_id)))
    for dump_id in dumps:
        match_scheme_dump(connection, tenant_id=tenant_id, dump_id=dump_id, index=index)
    return {"dumps": len(dumps),
            "journeys": refresh_checks(connection, tenant_id=tenant_id, dealer_id=str(dealer_id))}


def refresh_recent(connection: Connection, *, tenant_id: str) -> dict[str, int]:
    """The nightly look (with the nightly review): every dealership with a current dump of
    the last months is matched and decided again, so a Journey whose NDC came in after the
    upload moves on its own."""
    since = _recent_from(_today(connection, tenant_id))
    dealers = connection.execute(
        text("SELECT DISTINCT CAST(dealer_id AS text) FROM auditcore.p2_scheme_dumps "
             "WHERE tenant_id = :t AND status = 'ACTIVE' AND period_month >= :m"),
        {"t": tenant_id, "m": since},
    ).scalars().all()
    total = {"dealers": 0, "dumps": 0, "journeys": 0}
    for dealer_id in dealers:
        done = recheck_dealer(connection, tenant_id=tenant_id, dealer_id=dealer_id)
        total["dealers"] += 1
        total["dumps"] += done["dumps"]
        total["journeys"] += done["journeys"]
    return total


# --------------------------------------------------------------------------- the report

def covered_outlets(location_sets: list[list[str]], outlets: list[dict[str, Any]]) -> set[str] | None:
    """The outlets a dealership's dumps of a month cover; None for the whole dealership (a
    dump naming no Location, or a Location no outlet goes by). A Location covers every
    outlet standing under its DMS location (p2_dms_outlet_map: Aditya's CUTTACK is every
    outlet but JAJPUR)."""
    by_id = {str(o["outlet_id"]): o for o in outlets}
    covered: set[str] = set()
    for labels in location_sets:
        if not labels:
            return None
        for label in labels:
            found = resolve_outlet(label, outlets)
            if found is None:
                return None
            anchor = str(by_id[found]["dms_outlet_id"])
            covered |= {str(o["outlet_id"]) for o in outlets if str(o["dms_outlet_id"]) == anchor}
    return covered


def unmapped_locations(labels: list[str], outlets: list[dict[str, Any]]) -> list[str]:
    return [label for label in labels if resolve_outlet(label, outlets) is None]


def classify(row: dict[str, Any], kind: str) -> str:
    """Where a dump row stands (see the module's note)."""
    if row["cancelled"]:
        return CANCELLED
    if row["journey_id"] is None:
        return SERVICE_ONLY if row["channel"] == SERVICE else NOT_IN_VERIGENCE
    return IN_SYNC if row[f"{kind.lower()}_sold"] else NOT_BOUGHT


_NOT_UUID_NAME = "CASE WHEN cu.display_name ~* '^[0-9a-f]{8}-[0-9a-f]{4}-' THEN NULL ELSE cu.display_name END"

_ROWS_SQL = f"""
    SELECT r.*, u.period_month, d.dealer_name, j.journey_reference, o.outlet_name,
           COALESCE(NULLIF(cu.legal_name, ''), {_NOT_UUID_NAME}) AS journey_customer,
           COALESCE(k.ew_sold, false) AS ew_sold, k.ew_reason, COALESCE(k.rsa_sold, false) AS rsa_sold, k.rsa_reason,
           COALESCE(k.papers_on_file, false) AS papers_on_file, k.checked_at_utc
    FROM auditcore.p2_scheme_dump_rows r
    JOIN auditcore.p2_scheme_dumps u ON u.tenant_id = r.tenant_id AND u.dump_id = r.dump_id AND u.status = 'ACTIVE'
    JOIN auditcore.dealers d ON d.tenant_id = r.tenant_id AND d.dealer_id = r.dealer_id
    LEFT JOIN auditcore.journeys j ON j.tenant_id = r.tenant_id AND j.journey_id = r.journey_id
    LEFT JOIN auditcore.dealer_outlets o ON o.tenant_id = j.tenant_id AND o.outlet_id = j.outlet_id
    LEFT JOIN auditcore.customers cu ON cu.tenant_id = j.tenant_id AND cu.customer_id = j.customer_id
    LEFT JOIN auditcore.p2_scheme_journey_checks k ON k.tenant_id = r.tenant_id AND k.journey_id = r.journey_id
    WHERE r.tenant_id = :t AND u.period_month = :m AND u.dump_kind = :k AND CAST(r.dealer_id AS text) = ANY(:ds)
    ORDER BY d.dealer_name, r.scheme_date NULLS LAST, r.row_no
"""

_DATE = "'^\\d{4}-\\d{2}-\\d{2}$'"
# The Journeys Verigence shows as charged, with the day they belong to and whether any
# current dump of the type carries the vehicle (a cancelled registration apart).
_SOLD_SQL = f"""
    SELECT CAST(k.journey_id AS text) AS journey_id, CAST(j.dealer_id AS text) AS dealer_id, d.dealer_name,
           CAST(j.outlet_id AS text) AS outlet_id, o.outlet_name, j.journey_reference,
           COALESCE(NULLIF(cu.legal_name, ''), {_NOT_UUID_NAME}) AS journey_customer,
           k.ew_reason, k.rsa_reason, k.checked_at_utc,
           (SELECT max(jp.model_name_snapshot) FROM auditcore.journey_products jp
             WHERE jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id) AS model,
           (SELECT max(COALESCE(vr.vin, vr.chassis_number)) FROM auditcore.vehicle_records vr
             WHERE vr.tenant_id = j.tenant_id AND vr.journey_id = j.journey_id) AS vin,
           (SELECT max(f.effective_value #>> '{{}}') FROM auditcore.journey_document_extracted_fields f
             WHERE f.tenant_id = j.tenant_id AND f.journey_id = j.journey_id AND f.field_key = 'invoice_date'
               AND f.source_document_type_key IN ('customer_invoice_dms', 'customer_invoice_tally', 'tax_invoice_tally')
               AND (f.effective_value #>> '{{}}') ~ {_DATE}) AS invoice_date,
           (SELECT max(f.effective_value #>> '{{}}') FROM auditcore.journey_document_extracted_fields f
             WHERE f.tenant_id = j.tenant_id AND f.journey_id = j.journey_id AND f.field_key = 'delivery_date'
               AND f.source_document_type_key = 'gate_pass'
               AND (f.effective_value #>> '{{}}') ~ {_DATE}) AS delivery_date,
           (SELECT CAST((max(COALESCE(dl.actual_delivered_at, ds.first_started_at_utc)) AT TIME ZONE :tz) AS date)
              FROM auditcore.journey_stage_states ds
              LEFT JOIN auditcore.deliveries dl ON dl.tenant_id = ds.tenant_id AND dl.journey_id = ds.journey_id
             WHERE ds.tenant_id = j.tenant_id AND ds.journey_id = j.journey_id AND ds.stage_code = 'DELIVERY'
           ) AS delivery_started,
           EXISTS (SELECT 1 FROM auditcore.p2_scheme_dump_rows r
                   JOIN auditcore.p2_scheme_dumps u
                     ON u.tenant_id = r.tenant_id AND u.dump_id = r.dump_id AND u.status = 'ACTIVE' AND u.dump_kind = :k
                   WHERE r.tenant_id = k.tenant_id AND r.journey_id = k.journey_id AND NOT r.cancelled) AS in_dump,
           (SELECT max(r.cancelled_on) FROM auditcore.p2_scheme_dump_rows r
              JOIN auditcore.p2_scheme_dumps u
                ON u.tenant_id = r.tenant_id AND u.dump_id = r.dump_id AND u.status = 'ACTIVE' AND u.dump_kind = :k
             WHERE r.tenant_id = k.tenant_id AND r.journey_id = k.journey_id AND r.cancelled) AS cancelled_on,
           EXISTS (SELECT 1 FROM auditcore.p2_scheme_dump_rows r
                   JOIN auditcore.p2_scheme_dumps u
                     ON u.tenant_id = r.tenant_id AND u.dump_id = r.dump_id AND u.status = 'ACTIVE' AND u.dump_kind = :k
                   WHERE r.tenant_id = k.tenant_id AND r.journey_id = k.journey_id AND r.cancelled) AS cancelled_in_dump
    FROM auditcore.p2_scheme_journey_checks k
    JOIN auditcore.journeys j ON j.tenant_id = k.tenant_id AND j.journey_id = k.journey_id
    JOIN auditcore.dealers d ON d.tenant_id = j.tenant_id AND d.dealer_id = j.dealer_id
    LEFT JOIN auditcore.dealer_outlets o ON o.tenant_id = j.tenant_id AND o.outlet_id = j.outlet_id
    LEFT JOIN auditcore.customers cu ON cu.tenant_id = j.tenant_id AND cu.customer_id = j.customer_id
    WHERE k.tenant_id = :t AND CAST(j.dealer_id AS text) = ANY(:ds)
      AND ((:k = 'EW' AND k.ew_sold) OR (:k = 'RSA' AND k.rsa_sold))
    ORDER BY d.dealer_name, j.journey_reference
"""


def journey_day(row: dict[str, Any]) -> date | None:
    """The day a Journey's sale belongs to: the vehicle invoice, else the gate pass's
    delivery date, else the day its delivery file was started."""
    for key in ("invoice_date", "delivery_date"):
        if row.get(key):
            try:
                return date.fromisoformat(str(row[key]))
            except ValueError:
                continue
    return row.get("delivery_started")


def _f(value: Any) -> float | None:
    return float(value) if value is not None else None


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _row_json(r: dict[str, Any], kind: str) -> dict[str, Any]:
    low = kind.lower()
    foc = "FOC" in str(r["category"] or "").upper()
    return {
        "dealerId": str(r["dealer_id"]), "dealerName": r["dealer_name"], "kind": kind,
        "schemeNumber": r["scheme_number"], "schemeDate": _iso(r["scheme_date"]), "scheme": r["scheme"],
        "customerCode": r["customer_code"], "customerName": r["customer_name"], "mobile": r["mobile"],
        "chassis": r["chassis"], "registrationNumber": r["registration_number"], "model": r["model"],
        "channel": r["channel"], "channelLabel": CHANNEL_LABELS.get(r["channel"], r["channel"]),
        "category": r["category"], "foc": foc, "km": r["km"], "employee": r["employee"],
        "schemeStatus": r["scheme_status"], "cancelled": bool(r["cancelled"]), "cancelledOn": _iso(r["cancelled_on"]),
        "schemeAmount": _f(r["scheme_amount"]), "taxAmount": _f(r["tax_amount"]), "location": r["location"],
        "journeyId": str(r["journey_id"]) if r["journey_id"] else None, "journeyReference": r["journey_reference"],
        "journeyCustomer": r["journey_customer"], "outletName": r["outlet_name"],
        "matchBasis": r["match_basis"], "matchBasisLabel": BASIS_LABELS.get(r["match_basis"] or "", r["match_basis"]),
        "severalJourneys": r["match_note"] == "SEVERAL_JOURNEYS",
        "verigenceSold": bool(r[f"{low}_sold"]), "verigenceReason": r[f"{low}_reason"],
        "papersOnFile": bool(r["papers_on_file"]),
        # A free scheme (FOC) the customer's papers nevertheless charge for.
        "focCharged": foc and bool(r[f"{low}_sold"]),
    }


def build_scheme_report(connection: Connection, *, tenant_id: str, dealers: list[dict[str, Any]], first: date,
                        kind: str, timezone_name: str = "Asia/Kolkata") -> dict[str, Any]:
    dealer_ids = [d["dealerId"] for d in dealers]
    params = {"t": tenant_id, "m": first, "k": kind, "ds": dealer_ids}
    rows = [dict(r) for r in connection.execute(text(_ROWS_SQL), params).mappings()]
    per_dealer: dict[str, dict[str, Any]] = {
        d["dealerId"]: {"dealerId": d["dealerId"], "dealerName": d["dealerName"], "uploaded": False, "rows": 0,
                        "sales": 0, "service": 0, "cancelled": 0, "matched": 0, "inSync": 0, "notBought": 0,
                        "papersPending": 0, "notInDump": 0, "notInVerigence": 0, "serviceOnly": 0,
                        "coverage": None} for d in dealers}
    lists: dict[str, list[dict[str, Any]]] = {IN_SYNC: [], NOT_BOUGHT: [], NOT_IN_VERIGENCE: [], SERVICE_ONLY: [],
                                               CANCELLED: [], NOT_IN_DUMP: []}
    checked: list[datetime] = []
    for r in rows:
        stats = per_dealer[str(r["dealer_id"])]
        where = classify(r, kind)
        item = {**_row_json(r, kind), "outcome": where}
        stats["rows"] += 1
        stats["sales"] += r["channel"] == SALES
        stats["service"] += r["channel"] == SERVICE
        stats["matched"] += r["journey_id"] is not None
        stats[{IN_SYNC: "inSync", NOT_BOUGHT: "notBought", NOT_IN_VERIGENCE: "notInVerigence",
               SERVICE_ONLY: "serviceOnly", CANCELLED: "cancelled"}[where]] += 1
        if where == NOT_BOUGHT and not r["papers_on_file"]:
            stats["papersPending"] += 1
        if r["checked_at_utc"] is not None:
            checked.append(r["checked_at_utc"])
        lists[where].append(item)

    # The month's dumps per dealership, and the outlets they cover.
    dumps = connection.execute(
        text("""
            SELECT CAST(dealer_id AS text) AS dealer_id, locations FROM auditcore.p2_scheme_dumps
            WHERE tenant_id = :t AND period_month = :m AND dump_kind = :k AND status = 'ACTIVE'
              AND CAST(dealer_id AS text) = ANY(:ds)
        """),
        params,
    ).mappings().all()
    location_sets: dict[str, list[list[str]]] = defaultdict(list)
    for u in dumps:
        location_sets[u["dealer_id"]].append(list(u["locations"] or []))
    coverage: dict[str, set[str] | None] = {}
    for dealer_id, sets in location_sets.items():
        outlets = outlet_map(connection, tenant_id, dealer_id)
        covered = covered_outlets(sets, outlets)
        coverage[dealer_id] = covered
        per_dealer[dealer_id]["uploaded"] = True
        per_dealer[dealer_id]["coverage"] = (
            None if covered is None
            else sorted(str(o["outlet_name"]) for o in outlets if str(o["outlet_id"]) in covered))

    if coverage:
        month_end = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        low = kind.lower()
        sold_params = {"t": tenant_id, "k": kind, "ds": sorted(coverage), "tz": timezone_name}
        for s in connection.execute(text(_SOLD_SQL), sold_params).mappings():
            s = dict(s)
            if s["in_dump"]:
                continue
            day = journey_day(s)
            if day is None or not first <= day <= month_end:
                continue
            covered = coverage[s["dealer_id"]]
            if covered is not None and s["outlet_id"] not in covered:
                continue
            per_dealer[s["dealer_id"]]["notInDump"] += 1
            if s["checked_at_utc"] is not None:
                checked.append(s["checked_at_utc"])
            lists[NOT_IN_DUMP].append({
                "dealerId": s["dealer_id"], "dealerName": s["dealer_name"], "kind": kind, "outcome": NOT_IN_DUMP,
                "journeyId": s["journey_id"], "journeyReference": s["journey_reference"],
                "journeyCustomer": s["journey_customer"], "outletName": s["outlet_name"], "model": s["model"],
                "chassis": s["vin"], "journeyDate": day.isoformat(),
                "verigenceReason": s[f"{low}_reason"],
                "cancelledInDump": bool(s["cancelled_in_dump"]), "cancelledOn": _iso(s["cancelled_on"]),
                # Sold in the month's last days: the OEM may register it in the next month's dump.
                "monthEnd": (month_end - day).days < 5,
            })
    return {
        "month": first.strftime("%Y-%m"), "kind": kind, "kindLabel": KIND_LABELS[kind],
        "checkedAtUtc": min(checked).isoformat() if checked else None,
        "dealers": list(per_dealer.values()),
        "inSync": lists[IN_SYNC], "notBought": lists[NOT_BOUGHT], "notInDump": lists[NOT_IN_DUMP],
        "notInVerigence": lists[NOT_IN_VERIGENCE], "serviceOnly": lists[SERVICE_ONLY], "cancelled": lists[CANCELLED],
    }


# --------------------------------------------------------------------------- endpoints

def _dump_json(r: Any, outlets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    locations = list(r["locations"] or [])
    return {
        "dumpId": str(r["dump_id"]), "kind": r["dump_kind"], "kindLabel": KIND_LABELS[r["dump_kind"]],
        "dealerId": str(r["dealer_id"]), "dealerName": r.get("dealer_name"),
        "periodMonth": r["period_month"].strftime("%Y-%m"), "fileName": r["file_name"], "format": r["file_format"],
        "status": r["status"], "rows": r["row_count"], "duplicates": r["duplicate_count"],
        "sales": r["sales_count"], "service": r["service_count"], "cancelled": r["cancelled_count"],
        "matched": r["matched_count"], "locations": locations,
        "unmappedLocations": unmapped_locations(locations, outlets) if outlets is not None else [],
        "uploadedBy": r["uploaded_by_name"] or r["uploaded_by"], "uploadedAtUtc": r["uploaded_at_utc"].isoformat(),
        "matchedAtUtc": r["matched_at_utc"].isoformat() if r["matched_at_utc"] else None,
    }


def _timezone(connection: Connection, tenant_id: str) -> str:
    return connection.execute(text("SELECT timezone_name FROM auditcore.projects WHERE tenant_id = :t"),
                              {"t": tenant_id}).scalar_one_or_none() or "Asia/Kolkata"


@router.get("/scheme-dumps/setup")
def scheme_setup(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    held = _access(connection, tenant_id, principal, client, _READ, READ_ROLES)
    admin = "ADMIN" in held
    return {"types": SCHEME_TYPES, "dealers": _dealers(connection, tenant_id, principal.subject, admin),
            "canUpload": admin or bool(held & set(UPLOAD_ROLES)),
            "today": _today(connection, tenant_id).isoformat()}


def _same_scope(a: list[str], b: list[str]) -> bool:
    """Two dumps stand for the same part of a dealership: either names no Location (the
    whole dealership), or they share one."""
    if not a or not b:
        return True
    return bool({label_key(x) for x in a} & {label_key(x) for x in b})


@router.post("/scheme-dumps", status_code=201)
def upload_scheme_dump(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    file: Annotated[UploadFile, File()],
    dealerId: Annotated[UUID, Form()],
    kind: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """One dealership's extended warranty or RSA dump for a month (.xlsx, .xls or .csv).
    The type is read from the file's columns and the month from its registrations; a second
    upload for the same dealership, type, month and Location supersedes the first."""
    held = _access(connection, tenant_id, principal, client, _UPDATE, UPLOAD_ROLES)
    admin = "ADMIN" in held
    if str(dealerId) not in {d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject, admin)}:
        raise HTTPException(status_code=403, detail="That dealership is not one you cover.")
    wanted = (kind or "").strip().upper() or None
    if wanted is not None and wanted not in KINDS:
        raise HTTPException(status_code=422, detail="The type is EW or RSA.")
    data = file.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=422, detail="The file is larger than 15 MB.")
    name = (file.filename or "scheme-dump.xlsx")[:300]
    parsed = parse_scheme_file(name, data, wanted)
    period = period_of(parsed.rows)
    if period is None:
        raise HTTPException(status_code=422, detail="The registrations carry no date (Scheme Reg Date).")
    if period > _today(connection, tenant_id).replace(day=1):
        raise HTTPException(status_code=422, detail="The registrations are dated in a future month.")
    for before in connection.execute(
        text("""
            SELECT dump_id, locations FROM auditcore.p2_scheme_dumps
            WHERE tenant_id = :t AND dealer_id = :d AND dump_kind = :k AND period_month = :m AND status = 'ACTIVE'
        """),
        {"t": tenant_id, "d": str(dealerId), "k": parsed.kind, "m": period},
    ).mappings().all():
        if _same_scope(list(before["locations"] or []), parsed.locations):
            connection.execute(
                text("UPDATE auditcore.p2_scheme_dumps SET status = 'SUPERSEDED' WHERE tenant_id = :t AND dump_id = :id"),
                {"t": tenant_id, "id": str(before["dump_id"])},
            )
    dump_id = uuid4()
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_scheme_dumps (tenant_id, dump_id, dump_kind, dealer_id, period_month, file_name,
              file_format, locations, row_count, duplicate_count, sales_count, service_count, cancelled_count,
              uploaded_by, uploaded_by_name)
            VALUES (:t, :id, :k, :d, :m, :f, :ff, :loc, :n, :dup, :sales, :service, :cancelled, :by, :byn)
        """),
        {"t": tenant_id, "id": str(dump_id), "k": parsed.kind, "d": str(dealerId), "m": period, "f": name,
         "ff": parsed.file_format, "loc": parsed.locations, "n": len(parsed.rows), "dup": parsed.duplicates,
         "sales": sum(1 for r in parsed.rows if r["channel"] == SALES),
         "service": sum(1 for r in parsed.rows if r["channel"] == SERVICE),
         "cancelled": sum(1 for r in parsed.rows if r["cancelled"]),
         "by": principal.subject, "byn": _name_of(connection, principal.subject)},
    )
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_scheme_dump_rows (tenant_id, dump_id, row_no, dealer_id, dump_kind, scheme_number,
              scheme_date, scheme, customer_code, gstin, customer_name, mobile, chassis, registration_number, model,
              sold_by, channel, category, km, employee, scheme_status, cancelled, cancelled_on, scheme_amount,
              tax_amount, location, raw)
            VALUES (:t, :id, :n, :d, :k, :scheme_number, :scheme_date, :scheme, :customer_code, :gstin,
              :customer_name, :mobile, :chassis, :registration_number, :model, :sold_by, :channel, :category, :km,
              :employee, :scheme_status, :cancelled, :cancelled_on, :scheme_amount, :tax_amount, :location,
              CAST(:raw AS jsonb))
        """),
        [{"t": tenant_id, "id": str(dump_id), "n": n, "d": str(dealerId), "k": parsed.kind,
          **{k: v for k, v in row.items() if k != "raw"}, "raw": json.dumps(row["raw"], default=str)}
         for n, row in enumerate(parsed.rows, start=1)],
    )
    matched = match_scheme_dump(connection, tenant_id=tenant_id, dump_id=dump_id)
    decided = refresh_checks(connection, tenant_id=tenant_id, dealer_id=str(dealerId))
    logger.info("scheme_dump_uploaded", tenant_id=tenant_id, dump_id=str(dump_id), kind=parsed.kind,
                rows=len(parsed.rows), duplicates=parsed.duplicates, matched=matched, journeys_decided=decided)
    row = connection.execute(
        text("""
            SELECT u.*, d.dealer_name FROM auditcore.p2_scheme_dumps u
            JOIN auditcore.dealers d ON d.tenant_id = u.tenant_id AND d.dealer_id = u.dealer_id
            WHERE u.tenant_id = :t AND u.dump_id = :id
        """),
        {"t": tenant_id, "id": str(dump_id)},
    ).mappings().one()
    return _dump_json(row, outlet_map(connection, tenant_id, str(dealerId)))


@router.get("/scheme-dumps")
def list_scheme_dumps(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    month: str | None = None,
) -> dict[str, Any]:
    held = _access(connection, tenant_id, principal, client, _READ, READ_ROLES)
    first = _month(month, _today(connection, tenant_id))
    dealer_ids = [d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)]
    rows = connection.execute(
        text("""
            SELECT u.*, d.dealer_name FROM auditcore.p2_scheme_dumps u
            JOIN auditcore.dealers d ON d.tenant_id = u.tenant_id AND d.dealer_id = u.dealer_id
            WHERE u.tenant_id = :t AND u.period_month = :m AND CAST(u.dealer_id AS text) = ANY(:ds)
            ORDER BY u.uploaded_at_utc DESC
        """),
        {"t": tenant_id, "m": first, "ds": dealer_ids},
    ).mappings().all()
    maps: dict[str, list[dict[str, Any]]] = {}
    uploads = []
    for r in rows:
        dealer = str(r["dealer_id"])
        if dealer not in maps:
            maps[dealer] = outlet_map(connection, tenant_id, dealer)
        uploads.append(_dump_json(r, maps[dealer]))
    return {"month": first.strftime("%Y-%m"), "uploads": uploads}


@router.post("/scheme-dumps/recheck")
def recheck_scheme_dumps(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Check a month again: {month, dealerId?}. The month's current dumps are matched to
    the Journeys as they stand now and what Verigence shows is decided again."""
    held = _access(connection, tenant_id, principal, client, _UPDATE, UPLOAD_ROLES)
    first = _month(str(body.get("month") or ""), _today(connection, tenant_id))
    mine = {d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)}
    wanted = str(body["dealerId"]) if body.get("dealerId") else None
    if wanted is not None and wanted not in mine:
        raise HTTPException(status_code=403, detail="That dealership is not one you cover.")
    total = {"dealers": 0, "dumps": 0, "journeys": 0}
    for dealer_id in sorted({wanted} if wanted else mine):
        done = recheck_dealer(connection, tenant_id=tenant_id, dealer_id=dealer_id, first=first)
        if done["dumps"]:
            total["dealers"] += 1
            total["dumps"] += done["dumps"]
            total["journeys"] += done["journeys"]
    return {"month": first.strftime("%Y-%m"), **total}


def _kind(value: str | None) -> str:
    kind = (value or EW).strip().upper()
    if kind not in KINDS:
        raise HTTPException(status_code=422, detail="The type is EW or RSA.")
    return kind


@router.get("/scheme-dumps/report")
def scheme_report(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    month: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    held = _access(connection, tenant_id, principal, client, _READ, READ_ROLES)
    first = _month(month, _today(connection, tenant_id))
    dealers = _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)
    return build_scheme_report(connection, tenant_id=tenant_id, dealers=dealers, first=first, kind=_kind(kind),
                               timezone_name=_timezone(connection, tenant_id))


_DUMP_COLUMNS = (
    ("Type", "kind"), ("Dealership", "dealerName"), ("Outlet (Verigence)", "outletName"),
    ("Journey", "journeyReference"), ("Customer (Verigence)", "journeyCustomer"), ("Chassis", "chassis"),
    ("Registration no", "registrationNumber"), ("DMS customer id", "customerCode"), ("Model", "model"),
    ("Scheme", "scheme"), ("Scheme no", "schemeNumber"), ("Scheme date", "schemeDate"), ("Sold by", "channelLabel"),
    ("Category", "category"), ("Status in dump", "schemeStatus"), ("Scheme amount", "schemeAmount"),
    ("Tax", "taxAmount"), ("Location (dump)", "location"), ("Matched on", "matchBasisLabel"),
    ("Verigence shows", "verigenceReason"), ("Note", "note"),
)
_JOURNEY_COLUMNS = (
    ("Type", "kind"), ("Dealership", "dealerName"), ("Outlet", "outletName"), ("Journey", "journeyReference"),
    ("Customer", "journeyCustomer"), ("Model", "model"), ("VIN", "chassis"), ("Sale date", "journeyDate"),
    ("Verigence shows", "verigenceReason"), ("Note", "note"),
)


def _note(item: dict[str, Any]) -> str | None:
    """The line a reader needs beside a row."""
    where = item["outcome"]
    if where == NOT_IN_DUMP:
        if item.get("cancelledInDump"):
            return f"Registered and cancelled in the dump{' on ' + item['cancelledOn'] if item.get('cancelledOn') else ''}."
        return "Sold at the month's end: may be in next month's dump." if item.get("monthEnd") else None
    if where == NOT_BOUGHT:
        if item.get("foc"):
            return "Free of cost (FOC) in the dump: no charge is expected."
        return ("The NDC / customer ledger is on file and shows no such charge." if item.get("papersOnFile")
                else "The NDC / customer ledger is not on file yet.")
    if where == IN_SYNC and item.get("focCharged"):
        return "Free of cost (FOC) in the dump, yet charged to the customer."
    if where in (NOT_IN_VERIGENCE, SERVICE_ONLY) and item.get("severalJourneys"):
        return "More than one Journey carries this chassis."
    if where == CANCELLED:
        return f"Cancelled{' on ' + item['cancelledOn'] if item.get('cancelledOn') else ''}."
    return None


def build_scheme_excel(reports: list[dict[str, Any]]) -> bytes:
    """Both types of a month in one workbook: Summary, then a sheet per list."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="161616")

    def sheet(title: str, columns: tuple[tuple[str, str], ...], key: str, empty: str) -> None:
        ws = wb.create_sheet(title)
        ws.append([c for c, _ in columns])
        for cell in ws[1]:
            cell.font, cell.fill = head_font, head_fill
        any_row = False
        for report in reports:
            for item in report[key]:
                any_row = True
                item = {**item, "note": _note(item)}
                ws.append([item.get(k) for _, k in columns])
        if not any_row:
            ws.append([empty])
        ws.freeze_panes = "A2"
        for i in range(1, len(columns) + 1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = 20

    summary = wb.active
    summary.title = "Summary"
    month = reports[0]["month"] if reports else ""
    summary.append([f"Extended warranty and RSA dumps against Verigence - {month}"])
    summary["A1"].font = Font(bold=True, size=13)
    summary.append(["Type", "Dealership", "Dump uploaded", "Outlets covered", "Rows in dump", "Sold by sales",
                    "Sold by service", "Cancelled", "Matched to a journey", "In sync",
                    "In dump, not bought per Verigence", "of which NDC / ledger not on file",
                    "Bought per Verigence, not in dump", "In dump (sales), not in Verigence",
                    "Service, no journey expected"])
    for cell in summary[2]:
        cell.font, cell.fill = head_font, head_fill
    for report in reports:
        for d in report["dealers"]:
            summary.append([report["kindLabel"], d["dealerName"], "Yes" if d["uploaded"] else "No",
                            "" if not d["uploaded"] else "All" if d["coverage"] is None else ", ".join(d["coverage"]),
                            d["rows"], d["sales"], d["service"], d["cancelled"], d["matched"], d["inSync"],
                            d["notBought"], d["papersPending"], d["notInDump"], d["notInVerigence"], d["serviceOnly"]])
    summary.column_dimensions["A"].width = 20
    summary.column_dimensions["B"].width = 24
    summary.column_dimensions["D"].width = 40
    sheet("In sync", _DUMP_COLUMNS, "inSync", "No scheme in the dumps is on a journey that shows the charge.")
    sheet("Not bought per Verigence", _DUMP_COLUMNS, "notBought", "Every matched journey shows the charge.")
    sheet("Bought, not in dump", _JOURNEY_COLUMNS, "notInDump",
          "No journey shows a charge for a vehicle the dumps do not carry.")
    sheet("Not in Verigence", _DUMP_COLUMNS, "notInVerigence", "Every scheme sold by sales is on a journey.")
    sheet("Sold by service", _DUMP_COLUMNS, "serviceOnly", "No scheme sold by service without a journey.")
    sheet("Cancelled", _DUMP_COLUMNS, "cancelled", "No cancelled registration in the dumps.")
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


@router.get("/scheme-dumps/report/file")
def scheme_report_file(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    month: str | None = None,
) -> StreamingResponse:
    held = _access(connection, tenant_id, principal, client, _READ, READ_ROLES)
    first = _month(month, _today(connection, tenant_id))
    dealers = _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)
    zone = _timezone(connection, tenant_id)
    reports = [build_scheme_report(connection, tenant_id=tenant_id, dealers=dealers, first=first, kind=kind,
                                   timezone_name=zone) for kind in KINDS]
    return StreamingResponse(
        io.BytesIO(build_scheme_excel(reports)),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="EW-RSA-Report_{first:%Y-%m}.xlsx"'})


__all__ = ["build_scheme_excel", "build_scheme_report", "channel_of", "classify", "covered_outlets", "detect_kind",
           "match_scheme_dump", "match_scheme_row", "parse_scheme_file", "period_of", "recheck_dealer",
           "refresh_checks", "refresh_recent", "router"]
