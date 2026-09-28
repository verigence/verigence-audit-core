"""Project / Dealer / Outlet onboarding workbook: template, export, parsing
and the import plan.

One workbook, two data sheets:

- Projects:    Code, Name, OEM, Active, Start Date, End Date, Segments,
               Timezone, Project ID
- Dealerships: one row per outlet -- Code (outlet code), OEM, Project Code,
               Dealership, Outlet Name, Location, State, City, Active,
               PC Presence (Onsite / Satellite), Monthly Car Sales Volume,
               Dealer Code, Postal Code, Latitude, Longitude, Outlet ID,
               Dealer ID

The same layout is the blank template and the export of current data, so a
downloaded file can be edited and uploaded again. Rows are matched by the
read-only ID columns when present, else by code (and, for dealers and
outlets created before codes existed, by name). Nothing is ever deleted:
Active = No deactivates.

A dealer's code is the dealership's initials plus the OEM abbreviation
(Aditya Motors + Mahindra -> AM-MAH), unless the Dealer Code column says
otherwise.

Everything here is pure (no database): ``plan_import`` takes the parsed
rows and the current state and returns the plan the preview shows and the
apply step executes.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

PROJECT_SHEET = "Projects"
DEALERSHIP_SHEET = "Dealerships"
INSTRUCTIONS_SHEET = "Instructions"

PROJECT_COLUMNS = ("Code", "Name", "OEM", "Active", "Start Date", "End Date", "Segments", "Timezone", "Project ID")
DEALERSHIP_COLUMNS = (
    "Code", "OEM", "Project Code", "Dealership", "Outlet Name", "Location", "State", "City", "Active",
    "PC Presence", "Monthly Car Sales Volume", "Dealer Code", "Postal Code", "Latitude", "Longitude",
    "Outlet ID", "Dealer ID",
)
_READ_ONLY_COLUMNS = {"Project ID", "Outlet ID", "Dealer ID"}
_REQUIRED = {
    PROJECT_SHEET: ("Code", "Name", "OEM", "Start Date"),
    DEALERSHIP_SHEET: ("Code", "Project Code", "Dealership", "Outlet Name"),
}
PC_PRESENCE = {"ONSITE": "ONSITE", "SATELLITE": "SATELLITE"}
PC_PRESENCE_LABEL = {"ONSITE": "Onsite", "SATELLITE": "Satellite"}
DEFAULT_TIMEZONE = "Asia/Kolkata"
MAX_ROWS = 5000

# OEM code -> the abbreviation used in dealer and outlet codes.
_OEM_ABBREVIATIONS = {
    "MAHINDRA": "MAH", "HYUNDAI": "HYU", "MARUTI": "MAR", "TATA_MOTORS": "TAT", "MERCEDES_BENZ": "MB",
    "BMW": "BMW", "SKODA": "SKO", "VOLKSWAGEN": "VW", "KIA": "KIA", "TOYOTA": "TOY", "HONDA": "HON",
}
_NAME_NOISE = {"PVT", "PRIVATE", "LTD", "LIMITED", "LLP", "INC", "CO", "COMPANY", "AND", "THE", "OF"}
_YES = {"YES", "Y", "TRUE", "1", "ACTIVE"}
_NO = {"NO", "N", "FALSE", "0", "INACTIVE"}
_AUTO_CODE = re.compile(r"^[0-9a-f]{32}$")


def oem_abbreviation(oem_code: str) -> str:
    code = oem_code.strip().upper()
    return _OEM_ABBREVIATIONS.get(code) or re.sub(r"[^A-Z0-9]", "", code)[:3]


def derive_dealer_code(dealer_name: str, oem_code: str) -> str:
    """Initials of the dealership name + the OEM abbreviation: AM-MAH."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", dealer_name.upper()) if w and w not in _NAME_NOISE]
    initials = "".join(w[0] for w in words) if len(words) > 1 else (words[0][:2] if words else "D")
    return f"{initials}-{oem_abbreviation(oem_code)}"


def is_generated_code(code: str | None) -> bool:
    """Codes created before onboarding codes existed are random hex."""
    return bool(code and _AUTO_CODE.match(code))


# --------------------------------------------------------------------- parse


def _norm(header: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(header or "").lower())


_HEADER_ALIASES = {
    PROJECT_SHEET: {_norm(c): c for c in PROJECT_COLUMNS} | {
        "projectcode": "Code", "projectname": "Name", "startdate": "Start Date", "enddate": "End Date",
        "tenantid": "Project ID",
    },
    DEALERSHIP_SHEET: {_norm(c): c for c in DEALERSHIP_COLUMNS} | {
        "outletcode": "Code", "dealer": "Dealership", "dealershipname": "Dealership", "dealername": "Dealership",
        "address": "Location", "outlet": "Outlet Name", "pcpresence": "PC Presence",
        "monthlycarsales": "Monthly Car Sales Volume", "monthlysalesvolume": "Monthly Car Sales Volume",
        "monthlyvehiclevolume": "Monthly Car Sales Volume", "pincode": "Postal Code", "lat": "Latitude",
        "long": "Longitude", "lng": "Longitude",
    },
}


@dataclass
class ParsedSheet:
    rows: list[dict[str, Any]] = field(default_factory=list)  # each has "_row" (Excel row number)
    errors: list[str] = field(default_factory=list)


@dataclass
class ParsedWorkbook:
    projects: ParsedSheet
    dealerships: ParsedSheet

    @property
    def errors(self) -> list[str]:
        return self.projects.errors + self.dealerships.errors


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _find_sheet(workbook: Any, name: str) -> Any:
    for sheet in workbook.worksheets:
        if _norm(sheet.title) == _norm(name) or (_norm(name) == "dealerships" and _norm(sheet.title) in {"dealers", "outlets", "dealership"}):
            return sheet
    return None


def _parse_sheet(workbook: Any, name: str) -> ParsedSheet:
    parsed = ParsedSheet()
    sheet = _find_sheet(workbook, name)
    if sheet is None:
        return parsed
    rows = sheet.iter_rows(values_only=True)
    header = next(rows, None) or ()
    aliases = _HEADER_ALIASES[name]
    columns: dict[int, str] = {}
    for index, title in enumerate(header):
        canonical = aliases.get(_norm(title))
        if canonical and canonical not in columns.values():
            columns[index] = canonical
    missing = [c for c in _REQUIRED[name] if c not in columns.values()]
    if missing:
        parsed.errors.append(f"{name} sheet is missing column(s): {', '.join(missing)}.")
        return parsed
    for number, values in enumerate(rows, start=2):
        record = {columns[i]: _clean(values[i]) for i in columns if i < len(values)}
        if not any(v is not None for v in record.values()):
            continue
        record["_row"] = number
        parsed.rows.append(record)
        if len(parsed.rows) > MAX_ROWS:
            parsed.errors.append(f"{name} sheet has more than {MAX_ROWS} rows; split the file.")
            break
    return parsed


def parse_workbook(content: bytes) -> ParsedWorkbook:
    try:
        workbook = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as exc:
        raise ValueError("The file is not a readable Excel workbook (.xlsx).") from exc
    parsed = ParsedWorkbook(projects=_parse_sheet(workbook, PROJECT_SHEET),
                            dealerships=_parse_sheet(workbook, DEALERSHIP_SHEET))
    if not parsed.projects.rows and not parsed.dealerships.rows and not parsed.errors:
        parsed.projects.errors.append("The workbook has no Projects or Dealerships rows.")
    return parsed


# ------------------------------------------------------------------- values


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip() or None


def _active(value: Any, messages: list[str]) -> bool:
    if value is None:
        return True
    token = str(value).strip().upper()
    if token in _YES:
        return True
    if token in _NO:
        return False
    messages.append(f"Active must be Yes or No, not '{value}'.")
    return True


def _date(value: Any, label: str, messages: list[str]) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:  # ISO (also how a stored preview keeps dates)
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        pass
    for pattern in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%d-%b-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(value).strip(), pattern).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    messages.append(f"{label} '{value}' is not a date (use DD-MM-YYYY).")
    return None


def _int(value: Any, label: str, messages: list[str]) -> int | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        messages.append(f"{label} must be a whole number, not '{value}'.")
        return None
    if number < 0 or number != number.to_integral_value():
        messages.append(f"{label} must be a whole number of 0 or more.")
        return None
    return int(number)


def _coordinate(value: Any, label: str, limit: int, messages: list[str]) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        messages.append(f"{label} must be a number.")
        return None
    if not -limit <= number <= limit:
        messages.append(f"{label} must be between -{limit} and {limit}.")
        return None
    return number.quantize(Decimal("0.0000001"))


def _same(a: Any, b: Any) -> bool:
    def norm(v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().lower() or None
        if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
            return Decimal(str(v)).normalize()
        return v
    return norm(a) == norm(b)


# ------------------------------------------------------------------- state


@dataclass
class ExistingState:
    """What exists today, read by the caller."""
    oems: list[dict[str, Any]]            # oem_id, oem_code, oem_name
    segments: list[dict[str, Any]]        # segment_id, segment_code, segment_name
    projects: list[dict[str, Any]]        # tenant_id, business_code, project_code, project_name, oem_code,
                                          # effective_start_date, effective_end_date, timezone_name, project_status
    dealers: dict[str, list[dict[str, Any]]]   # tenant_id -> dealer_id, dealer_code, dealer_name, status
    outlets: dict[str, list[dict[str, Any]]]   # tenant_id -> outlet_id, dealer_id, outlet_code, outlet_name, ...


def _oem_lookup(state: ExistingState) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for oem in state.oems:
        for key in (oem["oem_code"], oem["oem_name"], oem_abbreviation(oem["oem_code"])):
            lookup[_norm(key)] = oem
    return lookup


# -------------------------------------------------------------------- plan


def _project_changes(existing: dict[str, Any], row: dict[str, Any]) -> dict[str, list[Any]]:
    changes: dict[str, list[Any]] = {}
    for key, column in (("project_name", "name"), ("effective_end_date", "endDate"),
                        ("timezone_name", "timezone"), ("business_code", "code")):
        old, new = existing.get(key), row.get(column)
        if column == "endDate":
            old = old.isoformat() if isinstance(old, date) else old
        if not _same(old, new):
            changes[column] = [old, new]
    return changes


def plan_import(parsed: ParsedWorkbook, state: ExistingState) -> dict[str, Any]:
    oems = _oem_lookup(state)
    segments = {_norm(s["segment_code"]): s for s in state.segments} | {_norm(s["segment_name"]): s for s in state.segments}
    by_tenant = {p["tenant_id"]: p for p in state.projects}
    by_code = {str(p["business_code"]).upper(): p for p in state.projects if p.get("business_code")}

    projects: list[dict[str, Any]] = []
    file_codes: dict[str, dict[str, Any]] = {}
    for raw in parsed.projects.rows:
        messages: list[str] = []
        code = _text(raw.get("Code"))
        name = _text(raw.get("Name"))
        oem = oems.get(_norm(raw.get("OEM"))) if raw.get("OEM") else None
        start = _date(raw.get("Start Date"), "Start Date", messages)
        end = _date(raw.get("End Date"), "End Date", messages)
        entry: dict[str, Any] = {
            "row": raw["_row"], "code": code, "name": name,
            "oemCode": oem["oem_code"] if oem else _text(raw.get("OEM")),
            "active": _active(raw.get("Active"), messages),
            "startDate": start.isoformat() if start else None, "endDate": end.isoformat() if end else None,
            "timezone": _text(raw.get("Timezone")) or DEFAULT_TIMEZONE,
            "segments": None, "tenantId": None, "projectStatus": None, "activate": False,
            "changes": {}, "messages": messages,
        }
        if not code:
            messages.append("Code is required.")
        elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}", code):
            messages.append("Code may use letters, digits, '-', '_' and '.', up to 40 characters.")
        elif code.upper() in file_codes:
            messages.append(f"Code {code} appears more than once (row {file_codes[code.upper()]['row']}).")
        if not name:
            messages.append("Name is required.")
        if raw.get("OEM") and oem is None:
            messages.append(f"OEM '{raw.get('OEM')}' is not a known OEM.")
        if start and end and end < start:
            messages.append("End Date cannot be earlier than Start Date.")
        if raw.get("Segments"):
            chosen = []
            for token in re.split(r"[,;/]", str(raw["Segments"])):
                if token.strip():
                    match = segments.get(_norm(token))
                    if match is None:
                        messages.append(f"Segment '{token.strip()}' is not a known segment.")
                    else:
                        chosen.append(match["segment_code"])
            entry["segments"] = chosen

        existing = None
        project_id = _text(raw.get("Project ID"))
        if project_id:
            existing = by_tenant.get(project_id)
            if existing is None:
                messages.append("Project ID does not match any project; clear it to create a new one.")
        elif code and code.upper() in by_code:
            existing = by_code[code.upper()]
        if existing is not None:
            entry["tenantId"] = existing["tenant_id"]
            entry["projectStatus"] = existing["project_status"]
            other = by_code.get((code or "").upper())
            if other is not None and other["tenant_id"] != existing["tenant_id"]:
                messages.append(f"Code {code} already belongs to project {other['project_name']}.")
            if oem is not None and existing.get("oem_code") and oem["oem_code"] != existing["oem_code"]:
                messages.append("OEM cannot be changed by import once the project exists.")
            existing_start = existing.get("effective_start_date")
            if start and existing_start and start != existing_start:
                messages.append("Start Date cannot be changed by import once the project exists.")
            entry["changes"] = _project_changes(existing, entry)
        else:
            if raw.get("OEM") is None:
                messages.append("OEM is required for a new project.")
            if start is None and not any("Start Date" in m for m in messages):
                messages.append("Start Date is required for a new project.")
        if entry["active"] and (entry["projectStatus"] != "ACTIVE"):
            entry["activate"] = True
        if not entry["active"] and entry["projectStatus"] == "ACTIVE":
            entry["warnings"] = ["Deactivating a project is not supported yet; it stays active."]
        entry["action"] = (
            "ERROR" if messages else "CREATE" if existing is None
            else "UPDATE" if entry["changes"] or entry["activate"] else "UNCHANGED"
        )
        projects.append(entry)
        if code:
            file_codes.setdefault(code.upper(), entry)

    # Dealerships: resolve projects (file first, then existing), dealers, outlets.
    outlets: list[dict[str, Any]] = []
    dealers: dict[tuple[str, str], dict[str, Any]] = {}
    outlet_codes: dict[tuple[str, str], int] = {}
    for raw in parsed.dealerships.rows:
        messages = []
        project_code = _text(raw.get("Project Code"))
        project = file_codes.get((project_code or "").upper())
        existing_project = None
        if project is not None and project.get("tenantId"):
            existing_project = by_tenant.get(project["tenantId"])
        elif project is None and project_code:
            existing_project = by_code.get(project_code.upper())
        tenant_id = (project or {}).get("tenantId") or (existing_project or {}).get("tenant_id")
        project_oem = (project or {}).get("oemCode") or (existing_project or {}).get("oem_code")
        if not project_code:
            messages.append("Project Code is required.")
        elif project is None and existing_project is None:
            messages.append(f"Project Code {project_code} is not in the Projects sheet or the system.")
        elif project is not None and project["action"] == "ERROR":
            messages.append(f"Project {project_code} has errors in the Projects sheet.")
        row_oem = oems.get(_norm(raw.get("OEM"))) if raw.get("OEM") else None
        if raw.get("OEM") and row_oem is None:
            messages.append(f"OEM '{raw.get('OEM')}' is not a known OEM.")
        elif row_oem and project_oem and row_oem["oem_code"] != project_oem:
            messages.append(f"OEM {row_oem['oem_name']} does not match the project's OEM.")

        dealer_name = _text(raw.get("Dealership"))
        outlet_code = _text(raw.get("Code"))
        outlet_name = _text(raw.get("Outlet Name"))
        if not dealer_name:
            messages.append("Dealership is required.")
        if not outlet_name:
            messages.append("Outlet Name is required.")
        if not outlet_code:
            messages.append("Code (outlet code) is required.")
        elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", outlet_code):
            messages.append("Outlet code may use letters, digits, '-', '_' and '.', up to 80 characters.")
        presence_raw = _text(raw.get("PC Presence"))
        presence = PC_PRESENCE.get((presence_raw or "Onsite").upper())
        if presence is None:
            messages.append("PC Presence must be Onsite or Satellite.")
        volume = _int(raw.get("Monthly Car Sales Volume"), "Monthly Car Sales Volume", messages)
        latitude = _coordinate(raw.get("Latitude"), "Latitude", 90, messages)
        longitude = _coordinate(raw.get("Longitude"), "Longitude", 180, messages)
        dealer_code = (_text(raw.get("Dealer Code")) or
                       (derive_dealer_code(dealer_name, project_oem) if dealer_name and project_oem else None))
        entry = {
            "row": raw["_row"], "outletCode": outlet_code, "projectCode": project_code, "tenantId": tenant_id,
            "dealerName": dealer_name, "dealerCode": dealer_code, "outletName": outlet_name,
            "addressText": _text(raw.get("Location")), "stateRegion": _text(raw.get("State")),
            "city": _text(raw.get("City")), "postalCode": _text(raw.get("Postal Code")),
            "outletClassification": presence or "ONSITE", "monthlyVehicleVolume": volume,
            "latitude": str(latitude) if latitude is not None else None,
            "longitude": str(longitude) if longitude is not None else None,
            "active": _active(raw.get("Active"), messages),
            "dealerId": None, "outletId": None, "changes": {}, "messages": messages,
        }
        scope = (project_code or "").upper()
        if outlet_code:
            key = (scope, outlet_code.upper())
            if key in outlet_codes:
                messages.append(f"Outlet code {outlet_code} appears more than once in project {project_code} "
                                f"(row {outlet_codes[key]}).")
            outlet_codes.setdefault(key, entry["row"])

        # dealer: one per (project, dealer code); every row of it names the same dealership
        if dealer_code and dealer_name:
            dealer_key = (scope, dealer_code.upper())
            dealer = dealers.get(dealer_key)
            if dealer is None:
                dealer = {"projectCode": project_code, "tenantId": tenant_id, "dealerCode": dealer_code,
                          "dealerName": dealer_name, "dealerId": None, "action": "CREATE", "changes": {},
                          "rows": []}
                dealers[dealer_key] = dealer
                existing_dealers = state.dealers.get(tenant_id or "", [])
                dealer_id = _text(raw.get("Dealer ID"))
                match = (next((d for d in existing_dealers if str(d["dealer_id"]) == dealer_id), None) if dealer_id
                         else next((d for d in existing_dealers if str(d["dealer_code"]).upper() == dealer_code.upper()), None)
                         or next((d for d in existing_dealers if _same(d["dealer_name"], dealer_name)), None))
                if match is not None:
                    dealer["dealerId"] = str(match["dealer_id"])
                    if not _same(match["dealer_name"], dealer_name):
                        dealer["changes"]["dealerName"] = [match["dealer_name"], dealer_name]
                    if str(match["dealer_code"]).upper() != dealer_code.upper() and (
                        is_generated_code(match["dealer_code"]) or dealer_id
                    ):
                        dealer["changes"]["dealerCode"] = [match["dealer_code"], dealer_code]
                    dealer["action"] = "UPDATE" if dealer["changes"] else "UNCHANGED"
            elif not _same(dealer["dealerName"], dealer_name):
                messages.append(f"Dealer code {dealer_code} is used by '{dealer['dealerName']}' and "
                                f"'{dealer_name}'; give one of them its own Dealer Code.")
            dealer["rows"].append(entry["row"])
            entry["dealerId"] = dealer["dealerId"]

            existing_outlets = state.outlets.get(tenant_id or "", [])
            outlet_id = _text(raw.get("Outlet ID"))
            match = None
            if outlet_id:
                match = next((o for o in existing_outlets if str(o["outlet_id"]) == outlet_id), None)
                if match is None:
                    messages.append("Outlet ID does not match any outlet; clear it to create a new one.")
            elif outlet_code:
                match = next((o for o in existing_outlets if str(o["outlet_code"]).upper() == outlet_code.upper()), None)
                if match is None and dealer["dealerId"]:
                    match = next((o for o in existing_outlets
                                  if str(o["dealer_id"]) == dealer["dealerId"] and _same(o["outlet_name"], outlet_name)
                                  and is_generated_code(o["outlet_code"])), None)
            if match is not None:
                entry["outletId"] = str(match["outlet_id"])
                if dealer["dealerId"] and str(match["dealer_id"]) != dealer["dealerId"]:
                    messages.append("Moving an outlet to another dealer is not supported by import.")
                for column, key in (("outletCode", "outlet_code"), ("outletName", "outlet_name"),
                                    ("addressText", "address_text"), ("stateRegion", "state_region"),
                                    ("city", "city"), ("postalCode", "postal_code"),
                                    ("outletClassification", "outlet_classification"),
                                    ("monthlyVehicleVolume", "monthly_vehicle_volume"),
                                    ("latitude", "latitude"), ("longitude", "longitude")):
                    new = entry[column]
                    if column in ("latitude", "longitude", "postalCode", "monthlyVehicleVolume") and new is None:
                        continue  # a blank optional cell keeps the current value
                    if not _same(match.get(key), new):
                        old = match.get(key)
                        entry["changes"][column] = [str(old) if isinstance(old, Decimal) else old, new]
                if (match.get("status") == "ACTIVE") != entry["active"]:
                    entry["changes"]["active"] = [match.get("status") == "ACTIVE", entry["active"]]
        entry["action"] = (
            "ERROR" if messages else "CREATE" if entry["outletId"] is None
            else "UPDATE" if entry["changes"] else "UNCHANGED"
        )
        outlets.append(entry)

    dealer_list = [{k: v for k, v in d.items()} for d in dealers.values()]
    errors = (len(parsed.errors) + sum(1 for p in projects if p["action"] == "ERROR")
              + sum(1 for o in outlets if o["action"] == "ERROR"))

    def counts(items: list[dict[str, Any]]) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in items:
            result[item["action"]] = result.get(item["action"], 0) + 1
        return result

    return {
        "fileErrors": parsed.errors,
        "projects": projects,
        "dealers": dealer_list,
        "outlets": outlets,
        "summary": {"projects": counts(projects), "dealers": counts(dealer_list), "outlets": counts(outlets),
                    "errors": errors},
    }


# ----------------------------------------------------------------- workbook

_HEADER_FILL = PatternFill("solid", fgColor="0E7490")
_READ_ONLY_FILL = PatternFill("solid", fgColor="E2E8F0")
_INSTRUCTIONS = (
    (
        ("How to use this workbook"),
        True,
    ),
    (
        ("Projects sheet: one row per Project. Dealerships sheet: one row per outlet."),
        False,
    ),
    (
        ("Download the current data from Project Admin, edit it, and upload it again. Nothing changes until you "
     "confirm the preview."),
        False,
    ),
    (
        ("Codes: Project Code (e.g. JBR-01) and outlet Code (e.g. AM-MAH-CUBE) must be unique. The Dealer Code is "
     "filled in automatically from the dealership's initials and the OEM (Aditya Motors + Mahindra = AM-MAH); "
     "type one only when two dealerships would get the same code."),
        False,
    ),
    (
        ("Active: Yes or No. A project marked Yes is activated once its readiness checks pass (masters, dealers, "
     "outlets); otherwise it stays in setup and the preview says what is missing. No deactivates a dealer "
     "or outlet. Nothing is ever deleted."),
        False,
    ),
    (
        ("PC Presence: Onsite or Satellite. Monthly Car Sales Volume: a whole number."),
        False,
    ),
    (
        ("Segments (optional): comma-separated segment codes; blank = all segments. Timezone: blank = Asia/Kolkata. "
     "OEM and Start Date cannot change once a project exists."),
        False,
    ),
    (
        ("Project ID / Outlet ID / Dealer ID (grey): filled in by the export so edits update the right record. "
     "Leave them as they are; leave them blank for new rows."),
        False,
    ),
    (
        ("Dates: DD-MM-YYYY."),
        False,
    ),
)


def _style_header(sheet: Any, columns: tuple[str, ...]) -> None:
    for index, title in enumerate(columns, start=1):
        cell = sheet.cell(row=1, column=index, value=title)
        cell.font = Font(bold=True, color="FFFFFF" if title not in _READ_ONLY_COLUMNS else "334155")
        cell.fill = _HEADER_FILL if title not in _READ_ONLY_COLUMNS else _READ_ONLY_FILL
        cell.alignment = Alignment(vertical="center")
        width = max(12, min(44, len(title) + 6))
        if title in ("Location", "Outlet Name", "Name", "Dealership"):
            width = 34
        if title.endswith(" ID"):
            width = 38
        sheet.column_dimensions[cell.column_letter].width = width
    sheet.freeze_panes = "A2"


def _list_validation(sheet: Any, column_index: int, values: list[str], title: str) -> None:
    if not values:
        return
    letter = sheet.cell(row=1, column=column_index).column_letter
    validation = DataValidation(type="list", formula1='"' + ",".join(values) + '"', allow_blank=True,
                                showErrorMessage=True, errorTitle=title,
                                error=f"Choose one of: {', '.join(values)}")
    validation.add(f"{letter}2:{letter}{MAX_ROWS + 1}")
    sheet.add_data_validation(validation)


def build_workbook(*, oems: list[dict[str, Any]], projects: list[dict[str, Any]] | None = None,
                   dealerships: list[dict[str, Any]] | None = None) -> bytes:
    """The template (no rows) or an export (rows as ``{column: value}``)."""
    workbook = Workbook()
    instructions = workbook.active
    instructions.title = INSTRUCTIONS_SHEET
    for number, (line, bold) in enumerate(_INSTRUCTIONS, start=1):
        cell = instructions.cell(row=number, column=1, value=line)
        cell.font = Font(bold=bold, size=13 if bold else 11)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    instructions.column_dimensions["A"].width = 120

    oem_names = [str(o["oem_name"]) for o in oems]
    for title, columns, rows in ((PROJECT_SHEET, PROJECT_COLUMNS, projects or []),
                                 (DEALERSHIP_SHEET, DEALERSHIP_COLUMNS, dealerships or [])):
        sheet = workbook.create_sheet(title)
        _style_header(sheet, columns)
        for number, row in enumerate(rows, start=2):
            for index, column in enumerate(columns, start=1):
                value = row.get(column)
                cell = sheet.cell(row=number, column=index, value=value)
                if isinstance(value, date):
                    cell.number_format = "DD-MM-YYYY"
                if column in _READ_ONLY_COLUMNS:
                    cell.font = Font(color="64748B")
        _list_validation(sheet, columns.index("OEM") + 1, oem_names, "OEM")
        _list_validation(sheet, columns.index("Active") + 1, ["Yes", "No"], "Active")
        if "PC Presence" in columns:
            _list_validation(sheet, columns.index("PC Presence") + 1, ["Onsite", "Satellite"], "PC Presence")
    workbook.active = 1
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def export_rows(state: ExistingState) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Current data in workbook layout."""
    oem_names = {o["oem_code"]: o["oem_name"] for o in state.oems}
    projects = []
    code_of: dict[str, str] = {}
    for p in sorted(state.projects, key=lambda p: (str(p.get("business_code") or "~"), str(p["project_name"]).lower())):
        code_of[p["tenant_id"]] = p.get("business_code") or ""
        projects.append({
            "Code": p.get("business_code"), "Name": p["project_name"], "OEM": oem_names.get(p.get("oem_code"), p.get("oem_code")),
            "Active": "Yes" if p["project_status"] == "ACTIVE" else "No",
            "Start Date": p.get("effective_start_date"), "End Date": p.get("effective_end_date"),
            "Segments": ", ".join(p.get("segment_codes") or []) or None,
            "Timezone": p.get("timezone_name"), "Project ID": p["tenant_id"],
        })
    dealerships = []
    project_by_tenant = {p["tenant_id"]: p for p in state.projects}
    for tenant_id, outlets in state.outlets.items():
        project = project_by_tenant.get(tenant_id) or {}
        dealers = {str(d["dealer_id"]): d for d in state.dealers.get(tenant_id, [])}
        for o in sorted(outlets, key=lambda o: (str(dealers.get(str(o["dealer_id"]), {}).get("dealer_name", "")).lower(),
                                                str(o["outlet_name"]).lower())):
            dealer = dealers.get(str(o["dealer_id"]), {})
            dealerships.append({
                "Code": None if is_generated_code(o.get("outlet_code")) else o.get("outlet_code"),
                "OEM": oem_names.get(project.get("oem_code"), project.get("oem_code")),
                "Project Code": code_of.get(tenant_id) or None, "Dealership": dealer.get("dealer_name"),
                "Outlet Name": o["outlet_name"], "Location": o.get("address_text"),
                "State": o.get("state_region"), "City": o.get("city"),
                "Active": "Yes" if o.get("status") == "ACTIVE" and dealer.get("status") == "ACTIVE" else "No",
                "PC Presence": PC_PRESENCE_LABEL.get(str(o.get("outlet_classification")), "Onsite"),
                "Monthly Car Sales Volume": o.get("monthly_vehicle_volume"),
                "Dealer Code": None if is_generated_code(dealer.get("dealer_code")) else dealer.get("dealer_code"),
                "Postal Code": o.get("postal_code"),
                "Latitude": float(o["latitude"]) if o.get("latitude") is not None else None,
                "Longitude": float(o["longitude"]) if o.get("longitude") is not None else None,
                "Outlet ID": str(o["outlet_id"]), "Dealer ID": str(o["dealer_id"]),
            })
    return projects, dealerships
