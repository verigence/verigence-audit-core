"""uc03_p2_insurance_dumps.py -- the dealers' monthly insurance dumps, the OD discount
grid and their two reports (product owner 2026-11-16).

"This is next dump which will come monthly .. insurance dump .. two type of data to be
fetched: as per monthly grid, on insurance, we have to find the records where OD
discount is given more than the agreed discount ... the OD discount matrix has to be
updated for each month, you can give an option to key in the same as well ...
essentially it will have how much OD discount across three different segments for
yearly insurance, and how much for 3+3 insurance, any additional OD discount for
certain models. It could be one or more ... once these inputs provided a list should
be generated wherein discounts are given more than the defined limit ... we have to
capture both data excel upload per dealership and then the grid ... second report ...
find which deliveries in the verigence is showing self insurance, but it has been
insured from the dealership .. matched on basis of VIN, or customer name, or some
other common number."

The dump (samples of September 2026, all three dealers): the Mahindra insurance
"Policy Details Report" -- Shivnath as .csv, Aditya and Utkal as .xlsx -- one policy a
row: Policy No, Insurance Company, Insured Name, Customer Phone, Model, Variant, VIN,
Engine No, Created Date, Policy Effective Date, Vehicle Class (PCP / GCV / PCV), Plan
Type (`1-3 = OD 1 year + TP 3 years, `3-3 = 3+3, `1-1), Policy Tenure, OD Discount(%).,
Total IDV, Net OD Premium A, MISP Code. What the files do to it:

* values carry a leading backtick (`MA1..., `3311/...) -- dropped;
* Excel turned some Plan Types into dates (1-3 -> 1 March, 3-3 -> 3 March, 1-1 -> 1 Jan):
  read back as day-month;
* Aditya's file has 35 policies pasted a second time below the first copy, without the
  backticks: one policy is kept per policy number;
* total lines at the bottom (a premium sum with no policy number) are not policies.

The grid is per month (the policy's month): a SEGMENT row is the agreed maximum OD
discount for a vehicle class and tenure (1Y: OD for one year; 3+3: OD for three); a
MODEL row adds its points for that model (and tenure, when it names one). A dealership's
own rows for a month, when it has any, replace the common grid for it. Allowed = the
segment's limit + the model's addition (the largest, when several match); a policy
over it is reported with the excess. A policy whose segment and tenure have no limit
in the month's grid is listed apart ("no limit in the grid"), never as compliant.

Self vs dealership: each policy is matched to a Journey of the same dealership as a
DMS delivery row is (uc03_p2_dms_dumps.match_row): the chassis / VIN (last 8), then the
customer's mobile, then the same name and model. A Journey Verigence shows as Self
insurance (journey_classifications.insurance_type = SELF) while the dealership's own
dump carries its policy is reported, with what decided Self (the PC, a default, not yet
confirmed).

``GET  /p2/v1/tenants/{t}/insurance-dumps/setup``
``POST /p2/v1/tenants/{t}/insurance-dumps``                      (file, dealerId)
``GET  /p2/v1/tenants/{t}/insurance-dumps?month=YYYY-MM``
``GET  /p2/v1/tenants/{t}/insurance-dumps/od-grid?month=&dealerId=``
``PUT  /p2/v1/tenants/{t}/insurance-dumps/od-grid``              (Team Lead)
``POST /p2/v1/tenants/{t}/insurance-dumps/od-grid/file``         (Team Lead; the grid as Excel)
``GET  /p2/v1/tenants/{t}/insurance-dumps/od-grid/template``
``GET  /p2/v1/tenants/{t}/insurance-dumps/report?month=``
``GET  /p2/v1/tenants/{t}/insurance-dumps/report/file?month=``
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections import defaultdict
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
    _access,
    _dealers,
    _month,
    _name_of,
    _today,
    build_index,
    match_row,
    mobile_key,
    parse_date,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-insurance-dumps"])

# Who keys in or uploads the OD discount grid: the Team Lead (and an administrator).
GRID_ROLES = ("TL",)
MAX_BYTES = 15 * 1024 * 1024
MAX_ROWS = 20000
TENURES = ("1Y", "3+3")
TENURE_LABELS = {"1Y": "1 year", "3+3": "3+3"}
SEGMENT_LABELS = {"PCP": "Personal (PCP)", "GCV": "Goods commercial (GCV)", "PCV": "Passenger commercial (PCV)"}
DEFAULT_SEGMENTS = ("PCP", "GCV", "PCV")
INSURANCE_TYPE = {"key": "INSURANCE", "label": "Insurance dump", "frequency": "MONTHLY"}


# --------------------------------------------------------------------------- reading a dump

def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9%]", "", str(value or "").lower())


_COLUMNS: dict[str, tuple[str, ...]] = {
    "policy_number": ("policyno", "policynumber"),
    "insurer": ("insurancecompany", "insurer"),
    "insured_name": ("insuredname", "customername"),
    "mobile": ("customerphone", "mobile", "mobileno", "customermobile"),
    "vin": ("vin", "chassisno", "chassisnumber"),
    "engine_number": ("engineno", "enginenumber"),
    "model": ("model",),
    "variant": ("variant",),
    "segment": ("vehicleclass",),
    "plan_type": ("plantype",),
    "policy_tenure": ("policytenure",),
    "od_discount_pct": ("oddiscount%", "oddiscount"),
    "created": ("createddate",),
    "effective": ("policyeffectivedate",),
    "od_start": ("periodofowndamagecoverstart",),
    "od_end": ("periodofowndamagecoverend",),
    "idv": ("totalidv",),
    "net_od_premium": ("netodpremiuma", "netodpremium"),
    "misp_code": ("mispcode",),
    "location": ("location",),
}
_REQUIRED = ("policy_number", "od_discount_pct", "vin")


@dataclass
class ParsedInsurance:
    rows: list[dict[str, Any]] = field(default_factory=list)
    duplicates: int = 0
    file_format: str = ""


def _clean(value: Any) -> Any:
    """A cell as the dealer meant it: no leading backtick, no padding."""
    if isinstance(value, str):
        value = value.strip().lstrip("`").strip()
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
    if value is None or value == "":
        return None
    if isinstance(value, int | float | Decimal):
        return Decimal(str(value))
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    try:
        return Decimal(cleaned) if cleaned not in ("", "-", ".") else None
    except InvalidOperation:
        return None


def plan_type(value: Any) -> str | None:
    """'1-3', '3-3', '1-1' -- also when Excel turned it into a date (1 March is '1-3')."""
    value = _clean(value)
    if value is None:
        return None
    if isinstance(value, datetime | date):
        return f"{value.day}-{value.month}"
    # an Excel date printed as text ('2026-03-01 00:00:00')
    as_date = re.match(r"^\d{4}-(\d{2})-(\d{2})", str(value))
    if as_date:
        return f"{int(as_date.group(2))}-{int(as_date.group(1))}"
    match = re.search(r"(\d)\s*[-+/]\s*(\d)", str(value))
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return None


def od_years(plan: str | None, policy_tenure: Any, od_start: date | None, od_end: date | None) -> int | None:
    """Years of Own Damage cover: the Plan Type's first figure, else the OD cover period,
    else the policy tenure."""
    if plan and plan[0].isdigit():
        return int(plan[0])
    if od_start and od_end and od_end > od_start:
        return max(1, round((od_end - od_start).days / 365))
    number = _number(policy_tenure)
    return int(number) if number is not None and number > 0 else None


def tenure_of(years: int | None) -> str | None:
    if years is None:
        return None
    return "3+3" if years >= 3 else "1Y"


def _policy_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _table(headers: list[Any], body: list[tuple[Any, ...]], file_format: str) -> ParsedInsurance:
    normalized = [_norm(h) for h in headers]
    columns: dict[str, int] = {}
    for key, names in _COLUMNS.items():
        for i, h in enumerate(normalized):
            if h in names and key not in columns:  # a repeated header: its first column
                columns[key] = i
    missing = [k for k in _REQUIRED if k not in columns]
    if missing:
        raise HTTPException(status_code=422, detail=(
            "This is not an insurance policy report: the columns Policy No, VIN and OD Discount(%) are needed."))
    out = ParsedInsurance(file_format=file_format)
    seen: dict[str, int] = {}
    for r in body[:MAX_ROWS]:
        def cell(key: str, r: tuple[Any, ...] = r) -> Any:
            i = columns.get(key)
            return r[i] if i is not None and i < len(r) else None

        policy = _text(cell("policy_number"), 80)
        if not policy or not _policy_key(policy):
            continue  # a blank or a total line
        plan = plan_type(cell("plan_type"))
        od_start, od_end = parse_date(_clean(cell("od_start"))), parse_date(_clean(cell("od_end")))
        years = od_years(plan, cell("policy_tenure"), od_start, od_end)
        vin = _text(cell("vin"), 40)
        row = {
            "policy_number": policy,
            "policy_date": parse_date(_clean(cell("created"))) or parse_date(_clean(cell("effective"))) or od_start,
            "insurer": _text(cell("insurer"), 200),
            "insured_name": _text(cell("insured_name"), 300),
            "mobile": mobile_key(_clean(cell("mobile"))),
            "vin": re.sub(r"[^A-Z0-9]", "", vin.upper()) if vin else None,
            "engine_number": _text(cell("engine_number"), 40),
            "model": _text(cell("model"), 200),
            "variant": _text(cell("variant"), 200),
            "segment": (_text(cell("segment"), 20) or "").upper() or None,
            "plan_type": plan,
            "od_years": years,
            "od_discount_pct": _number(cell("od_discount_pct")),
            "idv": _number(cell("idv")),
            "net_od_premium": _number(cell("net_od_premium")),
            "misp_code": _text(cell("misp_code"), 120),
            "location": _text(cell("location"), 200),
            "raw": {str(h): _jsonable(_clean(v)) for h, v in zip(headers, r, strict=False)
                    if h not in (None, "") and _clean(v) not in (None, "")},
        }
        key = _policy_key(policy)
        if key in seen:
            out.duplicates += 1  # the same policy pasted twice: the first is kept
            continue
        seen[key] = len(out.rows)
        out.rows.append(row)
    if not out.rows:
        raise HTTPException(status_code=422, detail="No policies were found in this file.")
    return out


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _header_at(rows: list[tuple[Any, ...]]) -> int | None:
    for at in range(min(12, len(rows))):
        normalized = {_norm(v) for v in rows[at]}
        if "policyno" in normalized or "policynumber" in normalized:
            return at
    return None


def parse_insurance_file(name: str, data: bytes) -> ParsedInsurance:
    """The policies of a .csv or .xlsx insurance report."""
    lower = name.lower()
    if lower.endswith(".csv"):
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                content = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        rows = [tuple(r) for r in csv.reader(io.StringIO(content))]
        at = _header_at(rows)
        if at is None:
            raise HTTPException(status_code=422, detail="No 'Policy No' header was found in this file.")
        return _table(list(rows[at]), rows[at + 1:], "CSV")
    if not lower.endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=422, detail="Upload the insurance report as .xlsx or .csv.")
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="The file could not be read as an Excel (.xlsx) workbook.") from exc
    try:
        for sheet in workbook.worksheets:
            values: list[tuple[Any, ...]] = []
            for n, row in enumerate(sheet.iter_rows(values_only=True)):
                values.append(row)
                if n > MAX_ROWS + 20:
                    break
            at = _header_at(values)
            if at is not None:
                return _table(list(values[at]), values[at + 1:], "XLSX")
    finally:
        workbook.close()
    raise HTTPException(status_code=422, detail="No 'Policy No' header was found in this workbook.")


# --------------------------------------------------------------------------- the grid

def model_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


@dataclass
class Grid:
    segments: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    models: list[tuple[str, str | None, Decimal]] = field(default_factory=list)  # (model key, tenure, extra)

    def limit(self, segment: str | None, tenure: str | None, model: str | None
              ) -> tuple[Decimal | None, Decimal, Decimal | None]:
        """(the segment's limit, the model's addition, allowed) -- allowed None when the
        grid has no limit for the segment and tenure."""
        base = self.segments.get(((segment or "").upper(), tenure or ""))
        key = model_key(model)
        extras = [pct for m, t, pct in self.models if m and m == key and (t is None or t == tenure)]
        extra = max(extras, default=Decimal(0))
        return base, extra, (base + extra if base is not None else None)


def grid_from_rows(rows: list[dict[str, Any]]) -> Grid:
    grid = Grid()
    for r in rows:
        pct = Decimal(str(r["pct"]))
        if r["limit_kind"] == "SEGMENT" and r["segment"] and r["tenure"]:
            grid.segments[(str(r["segment"]).upper(), r["tenure"])] = pct
        elif r["limit_kind"] == "MODEL" and r["model"]:
            grid.models.append((model_key(r["model"]), r["tenure"], pct))
    return grid


def _grid_rows(connection: Connection, tenant_id: str, first: date) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(
        text("""
            SELECT limit_id, dealer_id, limit_kind, segment, tenure, model, pct, updated_by, updated_at_utc
            FROM auditcore.p2_od_discount_limits WHERE tenant_id = :t AND period_month = :m
            ORDER BY limit_kind DESC, segment NULLS LAST, tenure NULLS LAST, model NULLS LAST
        """),
        {"t": tenant_id, "m": first},
    ).mappings()]


def grids_for_month(rows: list[dict[str, Any]]) -> tuple[Grid | None, dict[str, Grid]]:
    """(the common grid, each dealership's own grid) of the month's rows."""
    common = [r for r in rows if r["dealer_id"] is None]
    own: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["dealer_id"] is not None:
            own[str(r["dealer_id"])].append(r)
    return (grid_from_rows(common) if common else None), {d: grid_from_rows(rs) for d, rs in own.items()}


def _pct(value: Any, what: str) -> Decimal:
    number = _number(value)
    if number is None or number < 0 or number > 100:
        raise HTTPException(status_code=422, detail=f"{what}: the OD discount is a percentage from 0 to 100.")
    return number.quantize(Decimal("0.01"))


def _tenure(value: Any, *, optional: bool) -> str | None:
    raw = re.sub(r"\s+", "", str(value or "").upper())
    if not raw:
        if optional:
            return None
        raise HTTPException(status_code=422, detail="Tenure is 1 year or 3+3.")
    if raw in ("1Y", "1", "1YEAR", "1YR", "YEARLY", "ANNUAL", "1-1", "1-3", "1+3", "1+1"):
        return "1Y"
    if raw in ("3+3", "3-3", "3", "3YEAR", "3YEARS", "3Y"):
        return "3+3"
    if optional and raw in ("ALL", "ANY", "BOTH"):
        return None
    raise HTTPException(status_code=422, detail=f"'{value}' is not a tenure: use 1 year or 3+3.")


def validate_grid(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The grid as keyed in: segments [{segment, tenure, pct}], models [{model, tenure?, pct}]."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in body.get("segments") or []:
        if item.get("pct") in (None, ""):
            continue  # a cell left empty: no limit
        segment = re.sub(r"[^A-Z0-9]", "", str(item.get("segment") or "").upper())[:20]
        if not segment:
            raise HTTPException(status_code=422, detail="A segment row needs its vehicle class (PCP, GCV, PCV).")
        tenure = _tenure(item.get("tenure"), optional=False)
        if (segment, tenure) in seen:
            raise HTTPException(status_code=422, detail=f"{segment} {TENURE_LABELS[tenure]} is given twice.")
        seen.add((segment, tenure))
        out.append({"limit_kind": "SEGMENT", "segment": segment, "tenure": tenure, "model": None,
                    "pct": _pct(item.get("pct"), f"{segment} {TENURE_LABELS[tenure]}")})
    for item in body.get("models") or []:
        model = re.sub(r"\s+", " ", str(item.get("model") or "")).strip()[:200]
        if not model and item.get("pct") in (None, ""):
            continue
        if not model:
            raise HTTPException(status_code=422, detail="A model addition needs the model's name.")
        out.append({"limit_kind": "MODEL", "segment": None, "tenure": _tenure(item.get("tenure"), optional=True),
                    "model": model, "pct": _pct(item.get("pct"), model)})
    return out


def grid_from_workbook(data: bytes) -> dict[str, Any]:
    """The grid from an Excel sheet: a header row with Segment / Model, Tenure and
    OD discount; a row naming a model is a model addition, else a segment limit."""
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="The grid could not be read as an Excel (.xlsx) workbook.") from exc
    try:
        rows = list(workbook.worksheets[0].iter_rows(values_only=True))
    finally:
        workbook.close()
    at = next((i for i, r in enumerate(rows[:10]) if any("tenure" in _norm(v) for v in r)), None)
    if at is None:
        raise HTTPException(status_code=422, detail="The grid needs a header row with Segment, Model, Tenure, OD discount %.")
    heads = [_norm(v) for v in rows[at]]

    def col(*names: str) -> int | None:
        return next((i for i, h in enumerate(heads) if any(n in h for n in names)), None)

    c_seg, c_model, c_ten = col("segment", "vehicleclass"), col("model"), col("tenure")
    c_pct = col("oddiscount", "discount", "%", "limit")
    if c_pct is None or c_ten is None:
        raise HTTPException(status_code=422, detail="The grid needs Tenure and OD discount % columns.")
    segments, models = [], []
    for r in rows[at + 1:]:
        def get(i: int | None, r: tuple[Any, ...] = r) -> Any:
            return r[i] if i is not None and i < len(r) else None

        if all(v in (None, "") for v in r):
            continue
        model, segment = _text(get(c_model), 200), _text(get(c_seg), 20)
        if model:
            models.append({"model": model, "tenure": get(c_ten), "pct": get(c_pct)})
        elif segment:
            segments.append({"segment": segment, "tenure": get(c_ten), "pct": get(c_pct)})
    return {"segments": segments, "models": models}


def save_grid(connection: Connection, *, tenant_id: str, first: date, dealer_id: str | None,
              rows: list[dict[str, Any]], actor: str) -> None:
    connection.execute(
        text("""
            DELETE FROM auditcore.p2_od_discount_limits
            WHERE tenant_id = :t AND period_month = :m AND dealer_id IS NOT DISTINCT FROM CAST(:d AS uuid)
        """),
        {"t": tenant_id, "m": first, "d": dealer_id},
    )
    if rows:
        connection.execute(
            text("""
                INSERT INTO auditcore.p2_od_discount_limits (tenant_id, limit_id, period_month, dealer_id, limit_kind,
                  segment, tenure, model, pct, updated_by)
                VALUES (:t, :id, :m, CAST(:d AS uuid), :limit_kind, :segment, :tenure, :model, :pct, :by)
            """),
            [{"t": tenant_id, "id": str(uuid4()), "m": first, "d": dealer_id, "by": actor, **r} for r in rows],
        )


def _grid_json(rows: list[dict[str, Any]], dealer_id: str | None) -> dict[str, Any]:
    mine = [r for r in rows if (str(r["dealer_id"]) if r["dealer_id"] else None) == dealer_id]
    return {
        "dealerId": dealer_id,
        "segments": [{"segment": r["segment"], "tenure": r["tenure"], "pct": float(r["pct"])}
                     for r in mine if r["limit_kind"] == "SEGMENT"],
        "models": [{"model": r["model"], "tenure": r["tenure"], "pct": float(r["pct"])}
                   for r in mine if r["limit_kind"] == "MODEL"],
        "updatedAtUtc": max((r["updated_at_utc"] for r in mine), default=None),
    }


# --------------------------------------------------------------------------- matching

def match_insurance_dump(connection: Connection, *, tenant_id: str, dump_id: UUID) -> int:
    """Match every policy of a dump to a Journey of the dealership (as a delivery row)."""
    dealer_id = connection.execute(
        text("SELECT dealer_id FROM auditcore.p2_insurance_dumps WHERE tenant_id = :t AND dump_id = :id"),
        {"t": tenant_id, "id": str(dump_id)},
    ).scalar_one()
    index = build_index(connection, tenant_id=tenant_id, dealer_id=dealer_id)
    rows = connection.execute(
        text("""
            SELECT row_no, vin, mobile, insured_name, model FROM auditcore.p2_insurance_dump_rows
            WHERE tenant_id = :t AND dump_id = :id ORDER BY row_no
        """),
        {"t": tenant_id, "id": str(dump_id)},
    ).mappings().all()
    updates, matched = [], 0
    for r in rows:
        jid, basis = match_row(index, DELIVERY, {
            "chassis": r["vin"], "mobile": r["mobile"], "customer_name": _bare_name(r["insured_name"]),
            "model": r["model"]})
        matched += jid is not None
        updates.append({"t": tenant_id, "id": str(dump_id), "n": r["row_no"], "j": jid,
                        "b": {"CHASSIS": "VIN"}.get(basis or "", basis)})
    if updates:
        connection.execute(
            text("""
                UPDATE auditcore.p2_insurance_dump_rows SET journey_id = CAST(:j AS uuid), match_basis = :b
                WHERE tenant_id = :t AND dump_id = :id AND row_no = :n
            """),
            updates,
        )
    connection.execute(
        text("UPDATE auditcore.p2_insurance_dumps SET matched_count = :m, matched_at_utc = now() "
             "WHERE tenant_id = :t AND dump_id = :id"),
        {"t": tenant_id, "id": str(dump_id), "m": matched},
    )
    return matched


_TITLES = re.compile(r"^(MR|MRS|MS|M/S|MISS|DR|SHRI|SMT)\.?\s+", re.IGNORECASE)


def _bare_name(value: Any) -> str | None:
    """'MRS. NIVEDITA  MAHANTY' -> 'NIVEDITA MAHANTY'."""
    name = re.sub(r"\s+", " ", str(value or "")).strip()
    while True:
        stripped = _TITLES.sub("", name).strip()
        if stripped == name:
            return name or None
        name = stripped


# --------------------------------------------------------------------------- reports

_ROWS_SQL = """
    SELECT r.*, u.period_month, d.dealer_name, j.journey_reference,
           c.insurance_type, c.insurance_decided_by, c.insurance_basis,
           COALESCE(NULLIF(cu.legal_name, ''), cu.display_name) AS journey_customer
    FROM auditcore.p2_insurance_dump_rows r
    JOIN auditcore.p2_insurance_dumps u ON u.tenant_id = r.tenant_id AND u.dump_id = r.dump_id AND u.status = 'ACTIVE'
    JOIN auditcore.dealers d ON d.tenant_id = r.tenant_id AND d.dealer_id = r.dealer_id
    LEFT JOIN auditcore.journeys j ON j.tenant_id = r.tenant_id AND j.journey_id = r.journey_id
    LEFT JOIN auditcore.customers cu ON cu.tenant_id = j.tenant_id AND cu.customer_id = j.customer_id
    LEFT JOIN auditcore.journey_classifications c ON c.tenant_id = r.tenant_id AND c.journey_id = r.journey_id
    WHERE r.tenant_id = :t AND u.period_month = :m AND CAST(r.dealer_id AS text) = ANY(:ds)
    ORDER BY d.dealer_name, r.policy_date NULLS LAST, r.row_no
"""

DECIDED_BY = {"PC": "Confirmed by the PC", "DEFAULT": "No policy on file (default)",
              "UNCONFIRMED": "Not confirmed yet", "DOCUMENT": "From the documents"}


def evaluate(row: dict[str, Any], grid: Grid | None) -> dict[str, Any]:
    """A policy against the grid: its tenure, limit, allowed and excess."""
    tenure = tenure_of(row.get("od_years"))
    given = row.get("od_discount_pct")
    if grid is None:
        return {"tenure": tenure, "base": None, "extra": None, "allowed": None, "excess": None, "status": "NO_GRID"}
    base, extra, allowed = grid.limit(row.get("segment"), tenure, row.get("model"))
    if allowed is None or given is None:
        return {"tenure": tenure, "base": base, "extra": extra, "allowed": allowed, "excess": None,
                "status": "NO_LIMIT" if allowed is None else "NO_DISCOUNT"}
    excess = Decimal(str(given)) - allowed
    return {"tenure": tenure, "base": base, "extra": extra, "allowed": allowed, "excess": excess if excess > 0 else None,
            "status": "OVER" if excess > 0 else "WITHIN"}


def _f(value: Any) -> float | None:
    return float(value) if value is not None else None


def build_insurance_report(connection: Connection, *, tenant_id: str, dealers: list[dict[str, Any]], first: date
                           ) -> dict[str, Any]:
    dealer_ids = [d["dealerId"] for d in dealers]
    common, own = grids_for_month(_grid_rows(connection, tenant_id, first))
    rows = [dict(r) for r in connection.execute(text(_ROWS_SQL), {"t": tenant_id, "m": first, "ds": dealer_ids}).mappings()]
    over, no_limit, self_but_dealer = [], [], []
    per_dealer: dict[str, dict[str, Any]] = {
        d["dealerId"]: {"dealerId": d["dealerId"], "dealerName": d["dealerName"], "policies": 0, "over": 0,
                        "within": 0, "noLimit": 0, "matched": 0, "selfButDealer": 0} for d in dealers}
    for r in rows:
        did = str(r["dealer_id"])
        grid = own.get(did) or common
        verdict = evaluate(r, grid)
        stats = per_dealer[did]
        stats["policies"] += 1
        stats["matched"] += r["journey_id"] is not None
        base = {
            "dealerId": did, "dealerName": r["dealer_name"], "policyNumber": r["policy_number"],
            "policyDate": r["policy_date"].isoformat() if r["policy_date"] else None, "insurer": r["insurer"],
            "insuredName": r["insured_name"], "mobile": r["mobile"], "vin": r["vin"], "model": r["model"],
            "variant": r["variant"], "segment": r["segment"], "planType": r["plan_type"], "tenure": verdict["tenure"],
            "odDiscount": _f(r["od_discount_pct"]), "netOdPremium": _f(r["net_od_premium"]), "idv": _f(r["idv"]),
            "journeyId": str(r["journey_id"]) if r["journey_id"] else None,
            "journeyReference": r["journey_reference"], "matchBasis": r["match_basis"],
        }
        if verdict["status"] == "OVER":
            stats["over"] += 1
            over.append({**base, "segmentLimit": _f(verdict["base"]), "modelExtra": _f(verdict["extra"]),
                         "allowed": _f(verdict["allowed"]), "excess": _f(verdict["excess"])})
        elif verdict["status"] in ("NO_LIMIT", "NO_GRID"):
            stats["noLimit"] += 1
            no_limit.append({**base, "reason": "No grid for the month" if verdict["status"] == "NO_GRID"
                             else f"No limit for {r['segment'] or '?'} {TENURE_LABELS.get(verdict['tenure'] or '', '?')}"})
        elif verdict["status"] == "WITHIN":
            stats["within"] += 1
        if r["journey_id"] is not None and r["insurance_type"] == "SELF":
            stats["selfButDealer"] += 1
            self_but_dealer.append({
                **base, "journeyCustomer": r["journey_customer"],
                "decidedBy": r["insurance_decided_by"],
                "decidedByLabel": DECIDED_BY.get(str(r["insurance_decided_by"] or ""), r["insurance_decided_by"]),
            })
    over.sort(key=lambda x: (-(x["excess"] or 0), x["dealerName"], x["policyDate"] or ""))
    return {
        "month": first.strftime("%Y-%m"),
        "gridSet": common is not None or bool(own),
        "dealers": list(per_dealer.values()),
        "odOverLimit": over, "noLimit": no_limit, "selfButDealer": self_but_dealer,
    }


# --------------------------------------------------------------------------- endpoints

def _dump_json(r: Any) -> dict[str, Any]:
    return {
        "dumpId": str(r["dump_id"]), "dealerId": str(r["dealer_id"]), "dealerName": r.get("dealer_name"),
        "periodMonth": r["period_month"].strftime("%Y-%m"), "fileName": r["file_name"], "status": r["status"],
        "rows": r["row_count"], "duplicates": r["duplicate_count"], "matched": r["matched_count"],
        "uploadedBy": r["uploaded_by_name"] or r["uploaded_by"], "uploadedAtUtc": r["uploaded_at_utc"].isoformat(),
    }


@router.get("/insurance-dumps/setup")
def insurance_setup(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    held = _access(connection, tenant_id, principal, client, _READ, READ_ROLES)
    admin = "ADMIN" in held
    return {"type": INSURANCE_TYPE, "dealers": _dealers(connection, tenant_id, principal.subject, admin),
            "canUpload": admin or bool(held & set(UPLOAD_ROLES)), "canEditGrid": admin or bool(held & set(GRID_ROLES)),
            "segments": [{"code": s, "label": SEGMENT_LABELS[s]} for s in DEFAULT_SEGMENTS],
            "tenures": [{"code": t, "label": TENURE_LABELS[t]} for t in TENURES],
            "today": _today(connection, tenant_id).isoformat()}


@router.post("/insurance-dumps", status_code=201)
def upload_insurance_dump(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    file: Annotated[UploadFile, File()],
    dealerId: Annotated[UUID, Form()],
) -> dict[str, Any]:
    """One dealership's insurance report for a month (.xlsx or .csv). The month is the
    policies' own (their latest policy date); a second upload for it supersedes the first."""
    held = _access(connection, tenant_id, principal, client, _UPDATE, UPLOAD_ROLES)
    admin = "ADMIN" in held
    if str(dealerId) not in {d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject, admin)}:
        raise HTTPException(status_code=403, detail="That dealership is not one you cover.")
    data = file.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=422, detail="The file is larger than 15 MB.")
    name = (file.filename or "insurance.xlsx")[:300]
    parsed = parse_insurance_file(name, data)
    dated = sorted(r["policy_date"] for r in parsed.rows if r["policy_date"])
    today = _today(connection, tenant_id)
    if not dated:
        raise HTTPException(status_code=422, detail="The policies carry no date (Created Date / Policy Effective Date).")
    # The month most policies fall in (a report of September may hold a policy of 1 October).
    months: dict[date, int] = defaultdict(int)
    for d in dated:
        months[d.replace(day=1)] += 1
    period = max(months, key=lambda m: (months[m], m))
    if period > today.replace(day=1):
        raise HTTPException(status_code=422, detail="The policies are dated in a future month.")
    connection.execute(
        text("UPDATE auditcore.p2_insurance_dumps SET status = 'SUPERSEDED' "
             "WHERE tenant_id = :t AND dealer_id = :d AND period_month = :m AND status = 'ACTIVE'"),
        {"t": tenant_id, "d": str(dealerId), "m": period},
    )
    dump_id = uuid4()
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_insurance_dumps (tenant_id, dump_id, dealer_id, period_month, file_name, row_count,
              duplicate_count, uploaded_by, uploaded_by_name)
            VALUES (:t, :id, :d, :m, :f, :n, :dup, :by, :byn)
        """),
        {"t": tenant_id, "id": str(dump_id), "d": str(dealerId), "m": period, "f": name, "n": len(parsed.rows),
         "dup": parsed.duplicates, "by": principal.subject, "byn": _name_of(connection, principal.subject)},
    )
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_insurance_dump_rows (tenant_id, dump_id, row_no, dealer_id, policy_number,
              policy_date, insurer, insured_name, mobile, vin, engine_number, model, variant, segment, plan_type,
              od_years, od_discount_pct, idv, net_od_premium, misp_code, location, raw)
            VALUES (:t, :id, :n, :d, :policy_number, :policy_date, :insurer, :insured_name, :mobile, :vin,
              :engine_number, :model, :variant, :segment, :plan_type, :od_years, :od_discount_pct, :idv,
              :net_od_premium, :misp_code, :location, CAST(:raw AS jsonb))
        """),
        [{"t": tenant_id, "id": str(dump_id), "n": n, "d": str(dealerId),
          **{k: v for k, v in row.items() if k != "raw"}, "raw": json.dumps(row["raw"], default=str)}
         for n, row in enumerate(parsed.rows, start=1)],
    )
    matched = match_insurance_dump(connection, tenant_id=tenant_id, dump_id=dump_id)
    logger.info("insurance_dump_uploaded", tenant_id=tenant_id, dump_id=str(dump_id), rows=len(parsed.rows),
                duplicates=parsed.duplicates, matched=matched)
    row = connection.execute(
        text("""
            SELECT u.*, d.dealer_name FROM auditcore.p2_insurance_dumps u
            JOIN auditcore.dealers d ON d.tenant_id = u.tenant_id AND d.dealer_id = u.dealer_id
            WHERE u.tenant_id = :t AND u.dump_id = :id
        """),
        {"t": tenant_id, "id": str(dump_id)},
    ).mappings().one()
    return _dump_json(row)


@router.get("/insurance-dumps")
def list_insurance_dumps(
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
            SELECT u.*, d.dealer_name FROM auditcore.p2_insurance_dumps u
            JOIN auditcore.dealers d ON d.tenant_id = u.tenant_id AND d.dealer_id = u.dealer_id
            WHERE u.tenant_id = :t AND u.period_month = :m AND CAST(u.dealer_id AS text) = ANY(:ds)
            ORDER BY u.uploaded_at_utc DESC
        """),
        {"t": tenant_id, "m": first, "ds": dealer_ids},
    ).mappings().all()
    return {"month": first.strftime("%Y-%m"), "uploads": [_dump_json(r) for r in rows]}


@router.get("/insurance-dumps/od-grid")
def get_od_grid(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    month: str | None = None,
    dealerId: UUID | None = None,
) -> dict[str, Any]:
    """The month's grid (common, or one dealership's own), and whether the previous
    month has one to start from."""
    _access(connection, tenant_id, principal, client, _READ, READ_ROLES)
    first = _month(month, _today(connection, tenant_id))
    rows = _grid_rows(connection, tenant_id, first)
    previous = (first - timedelta(days=1)).replace(day=1)
    has_previous = bool(_grid_rows(connection, tenant_id, previous))
    dealer = str(dealerId) if dealerId else None
    return {"month": first.strftime("%Y-%m"), **_grid_json(rows, dealer),
            "dealersWithOwnGrid": sorted({str(r["dealer_id"]) for r in rows if r["dealer_id"]}),
            "previousMonth": previous.strftime("%Y-%m") if has_previous else None}


def _grid_access(connection: Connection, tenant_id: str, principal: HumanPrincipal, client: SecurityAuthorizationClient,
                 dealer_id: str | None) -> None:
    held = _access(connection, tenant_id, principal, client, _UPDATE, GRID_ROLES)
    if dealer_id and dealer_id not in {d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject,
                                                                       "ADMIN" in held)}:
        raise HTTPException(status_code=403, detail="That dealership is not one you cover.")


@router.put("/insurance-dumps/od-grid")
def put_od_grid(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Key in the month's grid: {month, dealerId?, segments: [{segment, tenure, pct}],
    models: [{model, tenure?, pct}]}. Saving replaces that month's grid (for every
    dealership, or the one named); empty lists remove it."""
    dealer = str(body["dealerId"]) if body.get("dealerId") else None
    _grid_access(connection, tenant_id, principal, client, dealer)
    first = _month(str(body.get("month") or ""), _today(connection, tenant_id))
    rows = validate_grid(body)
    save_grid(connection, tenant_id=tenant_id, first=first, dealer_id=dealer, rows=rows, actor=principal.subject)
    return {"month": first.strftime("%Y-%m"), **_grid_json(_grid_rows(connection, tenant_id, first), dealer)}


@router.post("/insurance-dumps/od-grid/file")
def upload_od_grid(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    file: Annotated[UploadFile, File()],
    month: Annotated[str, Form()],
    dealerId: Annotated[UUID | None, Form()] = None,
) -> dict[str, Any]:
    """The month's grid from an Excel sheet (the template's layout)."""
    dealer = str(dealerId) if dealerId else None
    _grid_access(connection, tenant_id, principal, client, dealer)
    first = _month(month, _today(connection, tenant_id))
    data = file.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=422, detail="The file is larger than 15 MB.")
    rows = validate_grid(grid_from_workbook(data))
    if not rows:
        raise HTTPException(status_code=422, detail="The sheet carries no OD discount limits.")
    save_grid(connection, tenant_id=tenant_id, first=first, dealer_id=dealer, rows=rows, actor=principal.subject)
    return {"month": first.strftime("%Y-%m"), **_grid_json(_grid_rows(connection, tenant_id, first), dealer)}


def build_grid_template(grid: dict[str, Any] | None = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "OD discount grid"
    ws.append(["Segment", "Model", "Tenure", "OD discount %"])
    for cell in ws[1]:
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="161616")
    segments = {(s["segment"], s["tenure"]): s["pct"] for s in (grid or {}).get("segments", [])}
    for seg in DEFAULT_SEGMENTS:
        for ten in TENURES:
            ws.append([seg, None, TENURE_LABELS[ten], segments.get((seg, ten))])
    for m in (grid or {}).get("models", []):
        ws.append([None, m["model"], TENURE_LABELS.get(m["tenure"] or "", "All"), m["pct"]])
    if not (grid or {}).get("models"):
        ws.append([None, "(model name, e.g. XEV 9S)", "All", None])
    for col, width in zip("ABCD", (14, 30, 12, 16), strict=True):
        ws.column_dimensions[col].width = width
    notes = wb.create_sheet("How to fill")
    for line in (
        ("One row per segment and tenure: the agreed maximum OD discount %. Segment is the Vehicle Class of the "
         "insurance report: PCP (personal), GCV (goods commercial), PCV (passenger commercial)."),
        "Tenure: '1 year' (OD for one year, plans 1-1 and 1-3) or '3+3' (OD for three years).",
        "A row naming a Model adds its % to the segment's limit for that model; Tenure 'All' applies it to both.",
        "Leave a % empty when there is no agreed limit; such policies are listed as 'no limit in the grid'.",
    ):
        notes.append([line])
    notes.column_dimensions["A"].width = 130
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _xlsx(data: bytes, name: str) -> StreamingResponse:
    return StreamingResponse(io.BytesIO(data), media_type=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/insurance-dumps/od-grid/template")
def od_grid_template(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    month: str | None = None,
    dealerId: UUID | None = None,
) -> StreamingResponse:
    """The grid as an Excel sheet to fill in: the month's own when it has one."""
    _access(connection, tenant_id, principal, client, _READ, READ_ROLES)
    first = _month(month, _today(connection, tenant_id))
    grid = _grid_json(_grid_rows(connection, tenant_id, first), str(dealerId) if dealerId else None)
    return _xlsx(build_grid_template(grid), f"OD-Discount-Grid_{first:%Y-%m}.xlsx")


@router.get("/insurance-dumps/report")
def insurance_report(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    month: str | None = None,
) -> dict[str, Any]:
    held = _access(connection, tenant_id, principal, client, _READ, READ_ROLES)
    first = _month(month, _today(connection, tenant_id))
    dealers = _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)
    return build_insurance_report(connection, tenant_id=tenant_id, dealers=dealers, first=first)


_OVER_COLUMNS = (
    ("Dealership", "dealerName"), ("Policy date", "policyDate"), ("Policy no", "policyNumber"),
    ("Insurer", "insurer"), ("Insured name", "insuredName"), ("Model", "model"), ("Variant", "variant"),
    ("Segment", "segment"), ("Plan", "planType"), ("Tenure", "tenure"), ("OD discount given %", "odDiscount"),
    ("Segment limit %", "segmentLimit"), ("Model addition %", "modelExtra"), ("Allowed %", "allowed"),
    ("Excess (points)", "excess"), ("Net OD premium", "netOdPremium"), ("VIN", "vin"),
    ("Journey", "journeyReference"),
)
_SELF_COLUMNS = (
    ("Dealership", "dealerName"), ("Journey", "journeyReference"), ("Customer (Verigence)", "journeyCustomer"),
    ("Insured name (dump)", "insuredName"), ("Model", "model"), ("VIN", "vin"), ("Matched on", "matchBasis"),
    ("Verigence says", "decidedByLabel"), ("Policy no", "policyNumber"), ("Insurer", "insurer"),
    ("Policy date", "policyDate"), ("OD discount %", "odDiscount"),
)
_NO_LIMIT_COLUMNS = (
    ("Dealership", "dealerName"), ("Policy date", "policyDate"), ("Policy no", "policyNumber"),
    ("Model", "model"), ("Segment", "segment"), ("Plan", "planType"), ("Tenure", "tenure"),
    ("OD discount given %", "odDiscount"), ("Why", "reason"),
)


def build_insurance_excel(report: dict[str, Any]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="161616")

    def sheet(ws: Any, columns: tuple[tuple[str, str], ...], rows: list[dict[str, Any]], empty: str) -> None:
        ws.append([c for c, _ in columns])
        for cell in ws[1]:
            cell.font, cell.fill = head_font, head_fill
        for r in rows:
            ws.append([{"1Y": "1 year"}.get(r.get(k), r.get(k)) if k == "tenure" else r.get(k) for _, k in columns])
        if not rows:
            ws.append([empty])
        ws.freeze_panes = "A2"
        for i in range(1, len(columns) + 1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = 18

    summary = wb.active
    summary.title = "Summary"
    summary.append([f"Insurance dump report - {report['month']}"])
    summary["A1"].font = Font(bold=True, size=13)
    summary.append(["Dealership", "Policies", "OD over the limit", "Within the limit", "No limit in grid",
                    "Matched to a journey", "Self in Verigence, dealer policy"])
    for cell in summary[2]:
        cell.font, cell.fill = head_font, head_fill
    for d in report["dealers"]:
        summary.append([d["dealerName"], d["policies"], d["over"], d["within"], d["noLimit"], d["matched"],
                        d["selfButDealer"]])
    summary.column_dimensions["A"].width = 26
    sheet(wb.create_sheet("OD over the limit"), _OVER_COLUMNS, report["odOverLimit"], "None over the limit.")
    sheet(wb.create_sheet("Self vs dealership"), _SELF_COLUMNS, report["selfButDealer"],
          "No journey shows Self while the dealership insured it.")
    sheet(wb.create_sheet("No limit in grid"), _NO_LIMIT_COLUMNS, report["noLimit"], "Every policy had a limit.")
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


@router.get("/insurance-dumps/report/file")
def insurance_report_file(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    month: str | None = None,
) -> StreamingResponse:
    held = _access(connection, tenant_id, principal, client, _READ, READ_ROLES)
    first = _month(month, _today(connection, tenant_id))
    dealers = _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)
    report = build_insurance_report(connection, tenant_id=tenant_id, dealers=dealers, first=first)
    return _xlsx(build_insurance_excel(report), f"Insurance-Report_{first:%Y-%m}.xlsx")


__all__ = ["Grid", "build_grid_template", "build_insurance_excel", "build_insurance_report", "evaluate",
           "grid_from_rows", "grid_from_workbook", "od_years", "parse_insurance_file", "plan_type", "router",
           "tenure_of", "validate_grid"]
