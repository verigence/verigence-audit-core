"""Parsers for an OEM's *native* price / discount documents.

Four inputs, each in the OEM's own layout (not a Verigence template):

  PRICE_LIST       .xlsx  consolidated price list  -> one row per sellable SKU,
                          or the dealer's per-model price sheets (one sheet per
                          model, PV/CV and EV layouts; decision 2026-09-30)
  CONSUMER_SCHEME  .pdf   monthly consumer scheme bulletin
  EXCHANGE_SCHEME  .pdf   Xmart exchange / scrappage / welcome ready reckoner
  CORPORATE_POLICY .xlsx  Corporate Privilege Policy (matrix + company list)

Every parser returns a :class:`ParseResult`. Money figures are only trusted when
they reconcile against the document's own control totals (on-road price, A+B =
total, cash+other = total consumer offer); a row that does not reconcile is put
in ``errors`` and never ingested. Nothing is inferred.

Pure module: no DB, no network. ``pdfplumber`` is imported lazily so the two
xlsx parsers work without it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from io import BytesIO
from typing import Any

from openpyxl import load_workbook

# ── vocabulary ──────────────────────────────────────────────────────────────────
# price_list_items.component_key — the standard line items a vehicle is priced on.
PRICE_COMPONENT_KEYS: tuple[str, ...] = (
    "EX_SHOWROOM",
    "TCS",
    "INSURANCE",
    "EXT_WARRANTY_4TH_YR",
    "EXT_WARRANTY_4TH_5TH_YR",
    "ACCESSORIES_KIT",
    "RSA_1YR",
    "FASTAG",
    "REGISTRATION_INDIVIDUAL",
    "REGISTRATION_CORPORATE",
)

# discount_scheme_benefits.benefit_key — a discount is "how much is taken off a
# line item" (component-aligned) plus the trade-in / privilege buckets.
DISCOUNT_BENEFIT_KEYS: frozenset[str] = frozenset(
    {
        "CASH_DISCOUNT",
        "ACCESSORIES_KIT",
        "EXT_WARRANTY_4TH_YR",
        "EXT_WARRANTY_4TH_5TH_YR",
        "INSURANCE",
        "OTHER_SCHEME",
        "EXCHANGE_BONUS",
        "SCRAPPAGE_BONUS_DEALER",
        "SCRAPPAGE_BONUS_COD",
        "WELCOME_BONUS",
        "CORPORATE_PRIVILEGE",
    }
)

SCHEME_CATEGORIES: frozenset[str] = frozenset(
    {"CONSUMER", "EXCHANGE", "SCRAPPAGE", "WELCOME", "CORPORATE"}
)

_MONEY_TOLERANCE = Decimal("1.00")  # source figures carry sub-rupee float noise
_TWO_PLACES = Decimal("0.01")


# ── result shapes ───────────────────────────────────────────────────────────────
@dataclass
class PriceRow:
    row_no: int
    category: str  # PV / CV / BEV
    model_name: str
    variant_name: str
    trim: str | None
    fuel: str | None
    transmission: str | None
    drive: str | None
    seater: str | None
    components: dict[str, Decimal]
    onroad_individual: Decimal
    onroad_corporate: Decimal
    registration_basis: str = "STANDARD"  # STANDARD / PRIVATE / COMMERCIAL
    source_sheet: str | None = None
    # the dealer's own wording for a line that is a note rather than a column (an extra charge, a derived hypothecation)
    component_notes: dict[str, str] = field(default_factory=dict)


@dataclass
class DiscountRow:
    row_no: int
    scheme_category: str
    model_alias: str
    variant_texts: list[str]  # [] => whole model / brand
    benefits: list[tuple[str, Decimal]]  # (benefit_key, amount)
    total_customer_offer: Decimal
    config: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class CorporateBenefitRow:
    row_no: int
    privilege_category: str  # Z / Y / F / A / B
    brand_alias: str
    m_and_m: Decimal
    dealer: Decimal
    total: Decimal


@dataclass
class CorporateCompany:
    corporate_code: str
    corporate_name: str
    corporate_type: str | None
    privilege_category: str  # Z / Y / F / A / B


@dataclass
class GridRow:
    """One model line of the dealer's discount grid (decision 2026-09-30):
    booking protection, the agreed buffer, the insurance OD percentage
    (shown as a maximum for now) and the out-of-territory addition."""

    row_no: int
    model_alias: str
    model_aliases: list[str]
    in_scope: bool
    booking_protection_days: int | None
    agreed_buffer_amount: Decimal | None
    insurance_od_percent: Decimal | None
    out_of_territory_amount: Decimal | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseResult:
    kind: str
    effective_from_hint: date | None = None
    price_rows: list[PriceRow] = field(default_factory=list)
    discount_rows: list[DiscountRow] = field(default_factory=list)
    corporate_benefits: list[CorporateBenefitRow] = field(default_factory=list)
    corporate_companies: list[CorporateCompany] = field(default_factory=list)
    grid_rows: list[GridRow] = field(default_factory=list)
    grid_parameters: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


class MasterParseError(ValueError):
    """The file could not be recognised as the declared master kind."""


# ── shared helpers ──────────────────────────────────────────────────────────────
def _money(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = str(value).strip()
    text = text.replace("₹", "").replace("Rs.", "").replace("Rs", "")
    text = text.replace(",", "").replace("/-", "")
    text = re.sub(r"\s+", "", text)  # OEM PDFs split digits: "2 1,186" -> "21186"
    if text in ("", "-", "NA", "N/A"):
        return Decimal(0)
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _q2(value: Decimal) -> Decimal:
    return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


_HEADER_NOISE_RE = re.compile(r"[₹$€£]+$")


def _header_text(value: Any) -> str:
    """Header-cell text, with trailing currency-symbol noise stripped.

    Confirmed live against a real uploaded master: a header cell literally
    reads "Model₹" (the Rupee symbol stuck to the label, not a stray column
    -- Sl. No./Category/Variant/Trim/... on either side are all otherwise
    unaffected), which fails parse_price_list's strict header-layout check
    outright and rejects the whole file -- with a real Trim column the
    picker needs, but nothing downstream ever saw it. A header cell is a
    label, never itself a currency value (the monetary *columns* are
    validated separately via _money on the data rows), so stripping a
    trailing currency glyph here can never mask a genuine layout mismatch.
    """
    return _HEADER_NOISE_RE.sub("", _text(value) or "")


def _slug_model(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.strip().upper()).strip("_")


_WEF_RE = re.compile(
    r"(?:w\.?e\.?f\.?|valid\s*from|from)\s*:?\s*(?:dt\.?\s*)?(\d{1,2})\s*(?:st|nd|rd|th)?\s*"
    r"[.\-/ ]*\s*(\d{1,2}|[A-Za-z]{3,9})\.?\s*[.\-/ '’;]*\s*(\d{2,4})",
    re.IGNORECASE,
)
_MONTHS = {
    m[:3].lower(): i
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        start=1,
    )
}


def _date_hint(blob: str) -> date | None:
    match = _WEF_RE.search(blob or "")
    if not match:
        return None
    day_raw, month_raw, year_raw = match.groups()
    try:
        day = int(day_raw)
        month = (
            int(month_raw)
            if month_raw.isdigit()
            else _MONTHS.get(month_raw[:3].lower())
        )
        year = int(year_raw)
        if year < 100:
            year += 2000
        if not month:
            return None
        return date(year, month, day)
    except (ValueError, TypeError):
        return None


_LOOSE_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:st|nd|rd|th)?[\s_.\-]*([A-Za-z]{3,9})[\s_.\-]*'?(\d{2,4})(?!\d)"
)


def _loose_date_hint(blob: str) -> date | None:
    """A date written the way a file is named: "03Sep2026", "01th_Sept26",
    "1_Aug_2026". Month by name only, so a version number never reads as one."""
    for match in _LOOSE_DATE_RE.finditer(blob or ""):
        day_raw, month_raw, year_raw = match.groups()
        month = _MONTHS.get(month_raw[:3].lower())
        if not month:
            continue
        year = int(year_raw)
        if year < 100:
            year += 2000
        try:
            return date(year, month, int(day_raw))
        except ValueError:
            continue
    return None


# ── 1. price list ───────────────────────────────────────────────────────────────
_PRICE_HEADER = (
    "Sl. No.",
    "Category",
    "Model",
    "Variant",
    "Trim",
    "Fuel",
    "Transmission",
    "Drive",
    "Seater",
    "Ex-Showroom Price",
    "TCS",
    "Insurance",
)
_PRICE_COL = {
    "EX_SHOWROOM": 9,
    "TCS": 10,
    "INSURANCE": 11,
    "EXT_WARRANTY_4TH_YR": 12,
    "EXT_WARRANTY_4TH_5TH_YR": 13,
    "ACCESSORIES_KIT": 14,
    "RSA_1YR": 15,
    "FASTAG": 16,
    "REGISTRATION_INDIVIDUAL": 17,
    "REGISTRATION_CORPORATE": 19,
}
_ONROAD_INDIVIDUAL_COL = 18
_ONROAD_CORPORATE_COL = 20
_SOURCE_SHEET_COL = 21
_VALID_CATEGORIES = {"PV", "CV", "BEV"}
# on-road = Σ(all components) with REGISTRATION_INDIVIDUAL for the individual total
_ONROAD_INDIVIDUAL_PARTS = [
    k for k in PRICE_COMPONENT_KEYS if k != "REGISTRATION_CORPORATE"
]
_ONROAD_CORPORATE_PARTS = [
    k for k in PRICE_COMPONENT_KEYS if k != "REGISTRATION_INDIVIDUAL"
]


def parse_price_list(content: bytes, *, filename: str | None = None) -> ParseResult:
    """The OEM's consolidated list, or the dealer's per-model sheets: the
    layout is recognised from the workbook, never declared."""
    workbook = load_workbook(BytesIO(content), data_only=True, read_only=True)
    sheet = None
    for name in workbook.sheetnames:
        if name.strip().lower() in {"price list", "pricelist", "consolidated price list"}:
            sheet = workbook[name]
            break
    if sheet is None:
        sheet = workbook[workbook.sheetnames[0]]

    if STANDARD_PRICE_META_SHEET in workbook.sheetnames and (
        workbook[STANDARD_PRICE_META_SHEET]["B1"].value == STANDARD_PRICE_META_KEY
    ):
        # the Verigence template: read strictly, never by guessing
        result = _parse_standard_template(workbook, sheet)
        return _with_file_date(result, filename)
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        raise MasterParseError("Price list sheet is empty.")
    header = [_header_text(c) for c in rows[0][: len(_PRICE_HEADER)]]
    if header != list(_PRICE_HEADER):
        result = _parse_dealer_sheets(workbook, header_seen=header)
    else:
        result = _parse_consolidated(rows)
    return _with_file_date(result, filename)


def _with_file_date(result: ParseResult, filename: str | None) -> ParseResult:
    from_name = _loose_date_hint(filename or "")
    if result.effective_from_hint is None and from_name is not None:
        result.effective_from_hint = from_name
        result.meta["effectiveFromSource"] = "FILENAME"
    elif result.effective_from_hint is not None:
        result.meta["effectiveFromSource"] = "SHEET"
        if from_name is not None and from_name != result.effective_from_hint:
            result.warnings.append(
                f"The sheet says effective {result.effective_from_hint.isoformat()} but the file name says "
                f"{from_name.isoformat()}; enter the date to be sure."
            )
    return result


# ── the Verigence price list template: one row per vehicle, exact columns ───────
STANDARD_PRICE_META_SHEET = "_meta"
STANDARD_PRICE_META_KEY = "VERIGENCE_PRICE_LIST"
STANDARD_PRICE_VERSION = "1"
STANDARD_PRICE_COLUMNS = (
    "Model", "Variant", "Trim", "Fuel", "Transmission", "Drive", "Seater", "Insurance Type",
    "Ex-Showroom Price", "TCS", "Insurance", "Extended Warranty (4th Year)", "Extended Warranty (4th & 5th Year)",
    "Accessories Kit", "Essential Accessories", "RSA (1 Year)", "FASTag", "Registration (Without Hypothecation)",
    "Hypothecation Charge", "On-Road Price (Without Hypothecation, Without Extended Warranty)",
    "Minimum Booking Amount", "Extra Charge 1 Name", "Extra Charge 1 Amount", "Extra Charge 2 Name", "Extra Charge 2 Amount",
)
_STANDARD_REQUIRED = (
    "Model", "Variant", "Ex-Showroom Price", "Insurance", "Registration (Without Hypothecation)",
    "On-Road Price (Without Hypothecation, Without Extended Warranty)",
)
# the price lines that add to the on-road price in the template, with the component each becomes
_STANDARD_COMPONENT_COLUMNS = (
    ("EX_SHOWROOM", "Ex-Showroom Price"), ("TCS", "TCS"), ("INSURANCE", "Insurance"),
    ("EXT_WARRANTY_4TH_YR", "Extended Warranty (4th Year)"), ("EXT_WARRANTY_4TH_5TH_YR", "Extended Warranty (4th & 5th Year)"),
    ("ACCESSORIES_KIT", "Accessories Kit"), ("ESSENTIAL_ACCESSORIES", "Essential Accessories"),
    ("RSA_1YR", "RSA (1 Year)"), ("FASTAG", "FASTag"),
)
_STANDARD_NOT_IN_ON_ROAD = ("EXT_WARRANTY_4TH_YR", "EXT_WARRANTY_4TH_5TH_YR")


def _norm_header(value: Any) -> str:
    return re.sub(r"\s+", " ", (_text(value) or "")).strip().upper()


def _standard_wef(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    body = _text(value)
    if not body:
        return None
    return _date_hint(f"w.e.f. {body}") or _loose_date_hint(body)


def _parse_standard_template(workbook: Any, sheet: Any) -> ParseResult:
    result = ParseResult(kind="PRICE_LIST", meta={"layout": "VERIGENCE_TEMPLATE"})
    if str(workbook[STANDARD_PRICE_META_SHEET]["B2"].value) != STANDARD_PRICE_VERSION:
        raise MasterParseError("This price list template is an old version. Download the current template and copy the rows into it.")
    rows = [list(r) for r in sheet.iter_rows(values_only=True)]
    wanted = [_norm_header(c) for c in STANDARD_PRICE_COLUMNS]
    header_row = next((i for i, raw in enumerate(rows[:6]) if "MODEL" in [_norm_header(c) for c in raw] and "VARIANT" in [_norm_header(c) for c in raw]), None)
    if header_row is None:
        raise MasterParseError("The header row of the template was not found. Do not rename, move or delete the template's header row.")
    for raw in rows[:header_row]:
        for col, cell in enumerate(raw):
            if "WEF" in _norm_header(cell) and result.effective_from_hint is None:
                result.effective_from_hint = next((_standard_wef(v) for v in raw[col + 1:] if _text(v) or isinstance(v, (date, datetime))), None)
    headers = [_norm_header(c) for c in rows[header_row]]
    while headers and not headers[-1]:
        headers.pop()
    unknown = [h for h in headers if h not in wanted]
    missing = [STANDARD_PRICE_COLUMNS[i] for i, h in enumerate(wanted) if h not in headers]
    if unknown or missing:
        raise MasterParseError(
            "The columns are not those of the template"
            + (f"; not in the template: {', '.join(unknown)}" if unknown else "")
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + ". Use the template as downloaded, with no column added, renamed or removed."
        )
    if len(headers) != len(set(headers)):
        raise MasterParseError("A column appears twice in the template's header row.")
    col_of = {h: i for i, h in enumerate(headers)}

    def get(raw: list[Any], name: str) -> Any:
        i = col_of[_norm_header(name)]
        return raw[i] if i < len(raw) else None

    seen: dict[tuple[str, ...], int] = {}
    blank_as_zero = 0
    for idx, raw in enumerate(rows[header_row + 1:], start=header_row + 2):
        if all(_text(c) is None for c in raw):
            continue
        model_name, variant_name = _text(get(raw, "Model")), _text(get(raw, "Variant"))
        if not model_name or not variant_name:
            result.errors.append(f"row {idx}: the model and the variant are both required")
            continue
        label = f"row {idx} ({model_name} / {variant_name})"
        insurance_type = (_text(get(raw, "Insurance Type")) or "STANDARD").upper()
        if insurance_type not in {"STANDARD", "PRIVATE", "COMMERCIAL"}:
            result.errors.append(f"{label}: Insurance Type must be blank, PRIVATE or COMMERCIAL, not '{insurance_type}'")
            continue
        values: dict[str, Decimal] = {}
        bad = False
        for key, column in _STANDARD_COMPONENT_COLUMNS + (("REGISTRATION_INDIVIDUAL", "Registration (Without Hypothecation)"),):
            raw_value = get(raw, column)
            number = _money(raw_value)
            if number is None and _text(raw_value) is None:
                if column in _STANDARD_REQUIRED:
                    result.errors.append(f"{label}: {column} is required")
                    bad = True
                    break
                number = Decimal(0)
                blank_as_zero += 1
            if number is None:
                result.errors.append(f"{label}: {column} must be a number, not '{_text(raw_value)}'")
                bad = True
                break
            if number < 0:
                result.errors.append(f"{label}: {column} cannot be negative")
                bad = True
                break
            values[key] = number
        if bad:
            continue
        if values["EX_SHOWROOM"] <= 0:
            result.errors.append(f"{label}: Ex-Showroom Price must be more than zero")
            continue
        onroad = _money(get(raw, _STANDARD_REQUIRED[-1]))
        if onroad is None:
            result.errors.append(f"{label}: the on-road price is required and must be a number")
            continue
        calculated = sum((v for k, v in values.items() if k not in _STANDARD_NOT_IN_ON_ROAD), Decimal(0))
        if abs(calculated - onroad) > _MONEY_TOLERANCE:
            result.errors.append(
                f"{label}: Ex-showroom, TCS, Insurance, Accessories Kit, Essential Accessories, RSA, FASTag and Registration add up to "
                f"{_q2(calculated)} but the on-road price says {_q2(onroad)} (the on-road price carries no extended warranty)"
            )
            continue
        registration = values["REGISTRATION_INDIVIDUAL"]
        components = dict(values)
        components["REGISTRATION_CORPORATE"] = registration
        notes: dict[str, str] = {}
        hypo_raw = get(raw, "Hypothecation Charge")
        hypo = _money(hypo_raw)
        if hypo is None and _text(hypo_raw) is not None:
            result.errors.append(f"{label}: Hypothecation Charge must be a number, not '{_text(hypo_raw)}'")
            continue
        if hypo is not None:
            if hypo < 0:
                result.errors.append(f"{label}: Hypothecation Charge cannot be negative")
                continue
            components["HYPOTHECATION_CHARGE"] = hypo
            components["REGISTRATION_WITH_HYPO"] = registration + hypo
        min_raw = get(raw, "Minimum Booking Amount")
        minimum = _money(min_raw)
        if minimum is None and _text(min_raw) is not None:
            result.errors.append(f"{label}: Minimum Booking Amount must be a number, not '{_text(min_raw)}'")
            continue
        if minimum is not None:
            components["MIN_BOOKING_AMOUNT"] = minimum
        extras_ok = True
        for n in (1, 2):
            name, amount_raw = _text(get(raw, f"Extra Charge {n} Name")), get(raw, f"Extra Charge {n} Amount")
            amount = _money(amount_raw)
            if name is None and _text(amount_raw) is None:
                continue
            if name is None or amount is None or amount < 0:
                result.errors.append(f"{label}: Extra Charge {n} needs both a name and an amount (a number)")
                extras_ok = False
                break
            slug = _slug_model(name)[:70]
            components[f"EXTRA_CHARGE_{slug}"] = amount
            notes[f"EXTRA_CHARGE_{slug}"] = name
        if not extras_ok:
            continue
        fuel = _fuel_from(get(raw, "Fuel"))
        key = (_slug_model(model_name), variant_name.upper(), fuel or "", (_text(get(raw, "Transmission")) or "").upper(),
               (_text(get(raw, "Drive")) or "").upper(), _seater_from(get(raw, "Seater")) or "", insurance_type)
        if key in seen:
            result.errors.append(f"{label}: repeats the vehicle on row {seen[key]}; nothing is loaded until it is fixed")
            continue
        seen[key] = idx
        result.price_rows.append(
            PriceRow(
                row_no=idx,
                category="BEV" if fuel == "ELECTRIC" else "ICE" if fuel else "UNSPECIFIED",
                model_name=model_name,
                variant_name=variant_name,
                trim=_text(get(raw, "Trim")),
                fuel=fuel,
                transmission=(_text(get(raw, "Transmission")) or "").upper() or None,
                drive=(_text(get(raw, "Drive")) or "").upper() or None,
                seater=_seater_from(get(raw, "Seater")),
                components={k: _q2(v) for k, v in components.items()},
                onroad_individual=_q2(onroad),
                onroad_corporate=_q2(onroad),
                registration_basis=insurance_type,
                source_sheet=sheet.title,
                component_notes=notes,
            )
        )
    if blank_as_zero:
        result.warnings.append(f"{blank_as_zero} empty price cell(s) were read as nil (no such line for that vehicle)")
    result.meta["models"] = sorted({r.model_name for r in result.price_rows})
    result.meta["categoryCounts"] = _counts(r.category for r in result.price_rows)
    if not result.price_rows and not result.errors:
        raise MasterParseError("The template contains no vehicle rows.")
    return result


def _parse_consolidated(rows: list[Any]) -> ParseResult:
    result = ParseResult(kind="PRICE_LIST", meta={"layout": "OEM_CONSOLIDATED"})

    hint_blob = " ".join(
        str(c)
        for r in rows[-14:]
        for c in r
        if isinstance(c, str)
    )
    result.effective_from_hint = _date_hint(hint_blob)

    seen: dict[tuple[str, ...], Any] = {}
    for idx, raw in enumerate(rows[1:], start=2):
        category = _text(raw[1]) if len(raw) > 1 else None
        if category not in _VALID_CATEGORIES:
            continue
        ex_showroom = _money(raw[9]) if len(raw) > 9 else None
        if ex_showroom is None or ex_showroom <= 0:
            continue
        model_name = _text(raw[2])
        variant_name = _text(raw[3])
        if not model_name or not variant_name:
            result.errors.append(f"row {idx}: missing model/variant")
            continue

        components: dict[str, Decimal] = {}
        bad = False
        for comp_key, col in _PRICE_COL.items():
            value = _money(raw[col]) if len(raw) > col else None
            if value is None:
                result.errors.append(
                    f"row {idx}: non-numeric {comp_key} '{raw[col] if len(raw) > col else None}'"
                )
                bad = True
                break
            components[comp_key] = value
        if bad:
            continue

        onroad_ind = _money(raw[_ONROAD_INDIVIDUAL_COL]) if len(raw) > _ONROAD_INDIVIDUAL_COL else None
        onroad_corp = _money(raw[_ONROAD_CORPORATE_COL]) if len(raw) > _ONROAD_CORPORATE_COL else None
        if onroad_ind is None or onroad_corp is None:
            result.errors.append(f"row {idx}: missing on-road price")
            continue

        calc_ind = sum((components[k] for k in _ONROAD_INDIVIDUAL_PARTS), Decimal(0))
        calc_corp = sum((components[k] for k in _ONROAD_CORPORATE_PARTS), Decimal(0))
        if abs(calc_ind - onroad_ind) > _MONEY_TOLERANCE:
            result.errors.append(
                f"row {idx} ({model_name} / {variant_name}): components sum to "
                f"{_q2(calc_ind)} but on-road (individual) is {_q2(onroad_ind)}"
            )
            continue
        if abs(calc_corp - onroad_corp) > _MONEY_TOLERANCE:
            result.errors.append(
                f"row {idx} ({model_name} / {variant_name}): components sum to "
                f"{_q2(calc_corp)} but on-road (corporate) is {_q2(onroad_corp)}"
            )
            continue

        fuel = _text(raw[5]) if len(raw) > 5 else None
        transmission = _text(raw[6]) if len(raw) > 6 else None
        drive = _text(raw[7]) if len(raw) > 7 else None
        seater = _text(raw[8]) if len(raw) > 8 else None
        source_sheet = _text(raw[_SOURCE_SHEET_COL]) if len(raw) > _SOURCE_SHEET_COL else None
        registration_basis = _registration_basis(source_sheet)
        signature = (
            _q2(onroad_ind),
            _q2(onroad_corp),
            tuple(_q2(components[k]) for k in PRICE_COMPONENT_KEYS),
        )
        key = (
            _slug_model(model_name),
            variant_name.upper(),
            (fuel or "").upper(),
            (transmission or "").upper(),
            (drive or "").upper(),
            (seater or "").upper(),
            registration_basis,
        )
        if key in seen:
            if seen[key] != signature:
                result.errors.append(
                    f"row {idx} ({model_name} / {variant_name}): conflicting prices for the "
                    "same model/variant/fuel/transmission/drive"
                )
            else:
                result.meta["duplicateRowsCollapsed"] = (
                    result.meta.get("duplicateRowsCollapsed", 0) + 1
                )
            continue
        seen[key] = signature

        result.price_rows.append(
            PriceRow(
                row_no=idx,
                category=category,
                model_name=model_name,
                variant_name=variant_name,
                trim=_text(raw[4]) if len(raw) > 4 else None,
                fuel=fuel,
                transmission=transmission,
                drive=drive,
                seater=seater,
                components={k: _q2(v) for k, v in components.items()},
                onroad_individual=_q2(onroad_ind),
                onroad_corporate=_q2(onroad_corp),
                registration_basis=registration_basis,
                source_sheet=source_sheet,
            )
        )

    result.meta["models"] = sorted({r.model_name for r in result.price_rows})
    result.meta["categoryCounts"] = _counts(r.category for r in result.price_rows)
    if not result.price_rows and not result.errors:
        raise MasterParseError("Price list contained no recognisable vehicle rows.")
    return result


def _registration_basis(source_sheet: str | None) -> str:
    blob = (source_sheet or "").upper()
    if re.search(r"\bCOM\b|-COM|COMMERCIAL", blob):
        return "COMMERCIAL"
    if "PVT" in blob or "PRIVATE" in blob:
        return "PRIVATE"
    return "STANDARD"


def _counts(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


# ── 1b. the dealer's per-model price sheets ─────────────────────────────────────
# One worksheet per model. A "MODEL NAME" cell names it, a "Model & Variant"
# header row starts the table, a row of column letters "(A) (B) ... (I)=(A+B+
# C+D+E+F+G+H)" carries the sheet's own on-road formula, and the data rows
# follow. Component columns are recognised by their label; the registration
# and on-road columns come in pairs, the first pair individual (without
# hypothecation), the second corporate (with). Every row must reconcile
# against the sheet's own formula or it is not ingested.
_LETTER_RE = re.compile(r"^\(([A-Z])\)\s*(?:=\s*\(?\s*([A-Z+\s]+?)\s*\)?\s*)?$")
_TRANSMISSION_TOKENS = {"MT", "AT", "AMT", "CVT", "DCT", "IVT"}
_DRIVE_TOKENS = {"2WD", "4WD", "AWD", "4X4", "4X2"}
_FUEL_TOKENS = {"PETROL": "PETROL", "DIESEL": "DIESEL", "CNG": "CNG", "ELECTRIC": "ELECTRIC", "EV": "ELECTRIC"}
_SEATER_RE = re.compile(r"\b(\d{1,2})\s*(?:STR|SEATER|SEAT)\b", re.IGNORECASE)


def _component_for_label(label: str) -> str | None:
    up = label.upper()
    if "EX-SHOWROOM" in up or "EX SHOWROOM" in up or "EXSHOWROOM" in up:
        return "EX_SHOWROOM"
    if "TAX COLLECTION" in up or re.search(r"\bTCS\b", up):
        return "TCS"
    if "INSURANCE" in up:
        return "INSURANCE"
    if "WARRANTY" in up:
        return "EXT_WARRANTY_4TH_5TH_YR" if re.search(r"5\s*TH", up) else "EXT_WARRANTY_4TH_YR"
    if "ESSENTIAL" in up and "ACCESSOR" in up:
        return "ESSENTIAL_ACCESSORIES"
    if "ACCESSOR" in up:
        return "ACCESSORIES_KIT"
    if re.search(r"\bRSA\b", up) or "ROAD SIDE" in up or "ROADSIDE" in up:
        return "RSA_1YR"
    if "FASTAG" in up or "FAST TAG" in up:
        return "FASTAG"
    return None


def _variant_attributes(variant: str, *, fuel_hint: str | None, electric: bool) -> dict[str, str | None]:
    tokens = [t for t in re.split(r"[\s\-/,()]+", variant.upper()) if t]
    fuel = "ELECTRIC" if electric else (fuel_hint or None)
    if fuel is None:
        fuel = next((_FUEL_TOKENS[t] for t in tokens if t in _FUEL_TOKENS), None)
    transmission = next((t for t in tokens if t in _TRANSMISSION_TOKENS), None)
    drive = next((t for t in tokens if t in _DRIVE_TOKENS), None)
    seater_match = _SEATER_RE.search(variant)
    return {
        "fuel": fuel,
        "transmission": transmission,
        "drive": drive,
        "seater": seater_match.group(1) if seater_match else None,
    }


def _cell_reader(raw: list[Any]) -> Any:
    def cell(col: int) -> Any:
        return raw[col] if col < len(raw) else None
    return cell


def _labelled_cell_after(rows: list[Any], label: str) -> str | None:
    """The first non-empty cell to the right of a cell that reads ``label``."""
    for raw in rows:
        for col, cell in enumerate(raw):
            if _text(cell) and _text(cell).upper().rstrip(":") == label:
                for value in raw[col + 1:]:
                    # a sheet may repeat the label cell (merged cells); the value is the next different text
                    if _text(value) and _text(value).upper().rstrip(":") != label:
                        return _text(value)
    return None


# ── dealer per-model sheets ─────────────────────────────────────────────────────
# Header-driven: columns are found by their printed labels, never by position, so a
# sheet that adds, drops or re-orders a column is still read correctly -- or refused.
_ATTRIBUTE_HEADERS = {
    "FUEL": "fuel",
    "TRANSMISSION": "transmission",
    "DRIVE": "drive",
    "SEATER": "seater",
    "SEATERS": "seater",
    "SEATS": "seater",
    "SEATING": "seater",
}
_FUEL_TYPOS = {"DISEL": "DIESEL", "DEISEL": "DIESEL", "ELE": "ELECTRIC", "ELECTR": "ELECTRIC", "ELEC": "ELECTRIC"}
_WITH_HYPO_RE = re.compile(r"\bWITH\s+HYPO", re.IGNORECASE)
_HYPO_NOTE_RE = re.compile(r"HYPOTH\w*\s+CHARGES?\s*(?:RS\.?|₹)?\s*([\d,]+)", re.IGNORECASE)
_MIN_BOOKING_RE = re.compile(r"MINIMUM\s+BOOKING\s+AMOUNT\s*:?\s*(?:RS\.?|₹)?\s*([\d,]+)", re.IGNORECASE)
_EXTRA_CHARGE_RE = re.compile(
    r"^\s*(?:\d+\s*[.)]\s*)?(?P<label>[A-Za-z][^=]*?CHARGES?[^=]*?)\s*=?\s*(?:RS\.?|₹)\s*(?P<amount>[\d,]+)\s*/?-?\s*$",
    re.IGNORECASE,
)
# printed with the dealer's own wording; nothing here is an amount that adds to the on-road price
NON_ADDITIVE_KEYS = frozenset(
    {
        "REGISTRATION_INDIVIDUAL",
        "REGISTRATION_CORPORATE",
        "REGISTRATION_WITH_HYPO",
        "HYPOTHECATION_CHARGE",
        "MIN_BOOKING_AMOUNT",
    }
)


def is_extra_charge_key(key: str) -> bool:
    return key.startswith("EXTRA_CHARGE_")


def _header_label(rows: list[Any], header_row: int, letter_row: int, col: int) -> str:
    return " ".join(
        _text(rows[i][col]) or ""
        for i in range(header_row, letter_row)
        if col < len(rows[i]) and _text(rows[i][col])
    )


def _dealer_sheet_layout(rows: list[Any]) -> dict[str, Any] | None:
    letter_row = next(
        (i for i, raw in enumerate(rows)
         if sum(1 for c in raw if _text(c) and _LETTER_RE.match(_text(c).replace(" ", "").upper())) >= 3),
        None,
    )
    if letter_row is None:
        return None

    def _upper_cells(i: int) -> list[str]:
        return [(_text(c) or "").upper() for c in rows[i]]

    header_row = next(
        (i for i in range(letter_row - 1, -1, -1) if any("VARIANT" in c for c in _upper_cells(i))),
        None,
    )
    variant_col: int | None = None
    trim_col: int | None = None
    if header_row is not None:
        variant_col = next(col for col, c in enumerate(_upper_cells(header_row)) if "VARIANT" in c)
    else:
        # the TRIM layout: either "TRIM | ... | TRIM | FUEL ..." (a full variant name, then the trim code)
        # or a single TRIM column (the variant is then built from trim, fuel, transmission, drive, seats)
        header_row = next(
            (i for i in range(letter_row - 1, -1, -1)
             if "TRIM" in _upper_cells(i) and any(re.search(r"EX[\s\-]?SHOWROOM", c) for c in _upper_cells(i))),
            None,
        )
        if header_row is None:
            return None
        trim_cols = [col for col, c in enumerate(_upper_cells(header_row)) if c == "TRIM"]
        if len(trim_cols) >= 2:
            variant_col, trim_col = trim_cols[0], trim_cols[1]
        else:
            trim_col = trim_cols[0]

    attribute_cols: dict[str, int] = {}
    for col, c in enumerate(_upper_cells(header_row)):
        name = _ATTRIBUTE_HEADERS.get(re.sub(r"\s+", "", c))
        if name and name not in attribute_cols:
            attribute_cols[name] = col
    if variant_col is None and not {"fuel", "transmission", "drive", "seater"} <= set(attribute_cols):
        return None  # no variant column and not enough to build one: refuse, never guess

    letter_by_col: dict[int, str] = {}
    formula_by_letter: dict[str, list[str]] = {}
    for col, cell in enumerate(rows[letter_row]):
        match = _LETTER_RE.match((_text(cell) or "").replace(" ", "").upper())
        if not match:
            continue
        letter_by_col[col] = match.group(1)
        if match.group(2):
            formula_by_letter[match.group(1)] = [x for x in match.group(2).replace(" ", "").split("+") if x]

    components: dict[str, int] = {}
    registration_cols: list[int] = []
    onroad_cols: list[int] = []
    for col in sorted(letter_by_col):
        text_label = _header_label(rows, header_row, letter_row, col).upper()
        if "REGISTRATION" in text_label:
            registration_cols.append(col)
        elif "ON ROAD" in text_label or "ON-ROAD" in text_label or "ONROAD" in text_label:
            onroad_cols.append(col)
        else:
            key = _component_for_label(text_label)
            if key and key not in components:
                components[key] = col
    if "EX_SHOWROOM" not in components or not registration_cols or not onroad_cols:
        return None

    pairs: list[dict[str, Any]] = []
    for reg_col in registration_cols:
        onroad_col = next((c for c in onroad_cols if c > reg_col), None)
        if onroad_col is None:
            return None
        combined = (
            _header_label(rows, header_row, letter_row, reg_col)
            + " " + _header_label(rows, header_row, letter_row, onroad_col)
        )
        if _WITH_HYPO_RE.search(combined):
            kind = "WITH_HYPO"
        elif "CORPORATE" in combined.upper():
            kind = "CORPORATE"
        else:
            kind = "INDIVIDUAL"
        pairs.append({"reg_col": reg_col, "onroad_col": onroad_col, "kind": kind, "letter": letter_by_col.get(onroad_col, "")})
    if not any(p["kind"] == "INDIVIDUAL" for p in pairs):
        pairs[0]["kind"] = "INDIVIDUAL"
    return {
        "letter_row": letter_row,
        "variant_col": variant_col,
        "trim_col": trim_col,
        "attribute_cols": attribute_cols,
        "components": components,
        "pairs": pairs,
        "letter_by_col": letter_by_col,
        "formula_by_letter": formula_by_letter,
    }


def _sheet_notes(rows: list[Any], start: int) -> dict[str, Any]:
    """What the dealer prints under the table: the hypothecation charge, the minimum booking
    amount and any other charge that carries an amount. A note with no amount is kept as text."""
    found: dict[str, Any] = {"hypothecation": None, "minBooking": None, "extras": {}, "unpriced": []}
    seen_text: set[str] = set()
    for raw in rows[start:]:
        for cell in raw:
            body = _text(cell)
            if not body or not isinstance(cell, str) or body in seen_text:
                continue
            seen_text.add(body)
            if hit := _HYPO_NOTE_RE.search(body):
                found["hypothecation"] = Decimal(hit.group(1).replace(",", ""))
                continue
            if hit := _MIN_BOOKING_RE.search(body):
                found["minBooking"] = Decimal(hit.group(1).replace(",", ""))
                continue
            if hit := _EXTRA_CHARGE_RE.match(body):
                label = _text(hit.group("label")) or ""
                slug = _slug_model(label)[:70]
                if slug:
                    found["extras"][f"EXTRA_CHARGE_{slug}"] = (Decimal(hit.group("amount").replace(",", "")), label.rstrip(" =:-"))
                continue
            if re.search(r"\bCHARGES\b", body, re.IGNORECASE) and len(body) < 160:
                found["unpriced"].append(body)
    return found


def _fuel_from(value: Any) -> str | None:
    word = (_text(value) or "").upper()
    if not word:
        return None
    word = _FUEL_TYPOS.get(word, word)
    return _FUEL_TOKENS.get(word, word)


def _seater_from(value: Any) -> str | None:
    if value is None:
        return None
    match = re.search(r"\d{1,2}", str(value))
    return match.group(0) if match else None


def _check_pair(
    result: ParseResult, *, cell: Any, pair: dict[str, Any], sheet_title: str, idx: int,
    model_name: str, variant_name: str, sheet_sum: Decimal,
) -> tuple[Decimal, Decimal] | None:
    """One registration / on-road pair of a row: both read, and the on-road price equals the sheet's
    own components plus that registration. Anything else is an error and the row is not loaded."""
    registration = _money(cell(pair["reg_col"]))
    onroad = _money(cell(pair["onroad_col"]))
    if registration is None or onroad is None:
        result.errors.append(f"sheet '{sheet_title}' row {idx} ({variant_name}): missing registration or on-road price")
        return None
    if abs(sheet_sum + registration - onroad) > _MONEY_TOLERANCE:
        result.errors.append(
            f"sheet '{sheet_title}' row {idx} ({model_name} / {variant_name}): components sum to "
            f"{_q2(sheet_sum + registration)} but on-road is {_q2(onroad)}"
        )
        return None
    return registration, onroad


def _parse_dealer_sheets(workbook: Any, *, header_seen: list[str]) -> ParseResult:
    result = ParseResult(kind="PRICE_LIST", meta={"layout": "DEALER_PER_MODEL"})
    seen: dict[tuple[str, ...], tuple[str, int]] = {}
    seen_signatures: dict[tuple[str, ...], Any] = {}
    sheets_read = 0
    blank_as_zero = 0
    for sheet in workbook.worksheets:
        rows = [list(r) for r in sheet.iter_rows(values_only=True)]
        layout = _dealer_sheet_layout(rows)
        if layout is None:
            continue
        sheets_read += 1
        model_name = _labelled_cell_after(rows[: layout["letter_row"]], "MODEL NAME") or _text(sheet.title)
        if not model_name:
            result.errors.append(f"sheet '{sheet.title}': no model name")
            continue
        fuel_hint = _fuel_from(_labelled_cell_after(rows[: layout["letter_row"]], "FUEL TYPE"))
        electric = bool(re.search(r"\bEV\b|ELECTRIC|\bBEV\b", f"{model_name} {sheet.title} {fuel_hint or ''}".upper()))
        if result.effective_from_hint is None:
            blob = " ".join(str(c) for raw in rows[: layout["letter_row"]] for c in raw if isinstance(c, str))
            result.effective_from_hint = _date_hint(blob)
        notes = _sheet_notes(rows, layout["letter_row"] + 1)
        for line in notes["unpriced"]:
            result.meta.setdefault("notesWithoutAnAmount", []).append(f"{sheet.title}: {line}")
        comp_cols: dict[str, int] = layout["components"]
        pairs: list[dict[str, Any]] = layout["pairs"]
        individual = next(p for p in pairs if p["kind"] == "INDIVIDUAL")
        corporate = next((p for p in pairs if p["kind"] == "CORPORATE"), None)
        with_hypo = next((p for p in pairs if p["kind"] == "WITH_HYPO"), None)
        attr_cols: dict[str, int] = layout["attribute_cols"]

        for idx, raw in enumerate(rows[layout["letter_row"] + 1:], start=layout["letter_row"] + 2):
            cell = _cell_reader(raw)
            trim = _text(cell(layout["trim_col"])) if layout["trim_col"] is not None else None
            attrs_from_cells = {
                name: (_fuel_from(cell(col)) if name == "fuel" else _seater_from(cell(col)) if name == "seater"
                       else (_text(cell(col)) or "").upper() or None)
                for name, col in attr_cols.items()
            }
            if layout["variant_col"] is not None:
                variant_name = _text(cell(layout["variant_col"]))
            else:
                parts = [trim, attrs_from_cells.get("fuel"), attrs_from_cells.get("transmission"),
                         attrs_from_cells.get("drive"),
                         f"{attrs_from_cells['seater']} STR" if attrs_from_cells.get("seater") else None]
                variant_name = " ".join(p for p in parts if p) if trim else None
            ex_showroom = _money(cell(comp_cols["EX_SHOWROOM"]))
            if not variant_name or ex_showroom is None or ex_showroom <= 0:
                continue
            components: dict[str, Decimal] = {}
            bad = False
            for key, col in comp_cols.items():
                raw_value = cell(col)
                value = _money(raw_value)
                if value is None and raw_value is None:
                    if key == "INSURANCE":
                        result.errors.append(f"sheet '{sheet.title}' row {idx} ({variant_name}): Insurance is blank")
                        bad = True
                        break
                    value = Decimal(0)  # a component the sheet leaves empty is nil for that vehicle
                    blank_as_zero += 1
                if value is None:
                    result.errors.append(f"sheet '{sheet.title}' row {idx}: non-numeric {key} '{cell(col)}'")
                    bad = True
                    break
                components[key] = value
            if bad:
                continue
            sheet_sum = sum(components.values(), Decimal(0))

            individual_values = _check_pair(
                result, cell=cell, pair=individual, sheet_title=sheet.title, idx=idx,
                model_name=model_name, variant_name=variant_name, sheet_sum=sheet_sum,
            )
            if individual_values is None:
                continue
            (reg_ind, onroad_ind) = individual_values
            reg_corp, onroad_corp = reg_ind, onroad_ind
            if corporate is not None:
                corporate_values = _check_pair(
                result, cell=cell, pair=corporate, sheet_title=sheet.title, idx=idx,
                model_name=model_name, variant_name=variant_name, sheet_sum=sheet_sum,
            )
                if corporate_values is None:
                    continue
                (reg_corp, onroad_corp) = corporate_values
                if (reg_corp, onroad_corp) != (reg_ind, onroad_ind):
                    result.warnings.append(
                        f"sheet '{sheet.title}' row {idx} ({variant_name}): the corporate columns differ from the individual ones"
                    )
            hypo: Decimal | None = notes["hypothecation"]
            hypo_source = "NOTE" if hypo is not None else None
            if with_hypo is not None:
                hypo_values = _check_pair(
                result, cell=cell, pair=with_hypo, sheet_title=sheet.title, idx=idx,
                model_name=model_name, variant_name=variant_name, sheet_sum=sheet_sum,
            )
                if hypo_values is None:
                    continue
                (reg_with, onroad_with) = hypo_values
                column_charge = reg_with - reg_ind
                if abs((onroad_with - onroad_ind) - column_charge) > _MONEY_TOLERANCE or column_charge <= 0:
                    result.errors.append(
                        f"sheet '{sheet.title}' row {idx} ({variant_name}): the with-hypothecation columns do not differ "
                        "from the without columns by one consistent charge"
                    )
                    continue
                if hypo is not None and abs(hypo - column_charge) > _MONEY_TOLERANCE:
                    result.errors.append(
                        f"sheet '{sheet.title}' row {idx} ({variant_name}): the note says hypothecation is {_q2(hypo)} "
                        f"but the columns differ by {_q2(column_charge)}"
                    )
                    continue
                hypo, hypo_source = column_charge, "COLUMNS"
            if attrs_from_cells.get("fuel"):
                attrs = _variant_attributes(variant_name, fuel_hint=attrs_from_cells["fuel"], electric=electric)
                attrs["fuel"] = "ELECTRIC" if electric else attrs_from_cells["fuel"]
            else:
                attrs = _variant_attributes(variant_name, fuel_hint=fuel_hint, electric=electric)
            for name in ("transmission", "drive", "seater"):
                if attrs_from_cells.get(name):
                    attrs[name] = attrs_from_cells[name]
            components["REGISTRATION_INDIVIDUAL"] = reg_ind
            components["REGISTRATION_CORPORATE"] = reg_corp
            notes_by_key: dict[str, str] = {}
            if hypo is not None:
                components["HYPOTHECATION_CHARGE"] = hypo
                components["REGISTRATION_WITH_HYPO"] = reg_ind + hypo
                notes_by_key["HYPOTHECATION_CHARGE"] = f"read from the sheet's {hypo_source.lower()}"
            if notes["minBooking"] is not None:
                components["MIN_BOOKING_AMOUNT"] = notes["minBooking"]
            for extra_key, (extra_amount, extra_label) in notes["extras"].items():
                components[extra_key] = extra_amount
                notes_by_key[extra_key] = extra_label
            signature = (_q2(onroad_ind), _q2(onroad_corp), tuple(sorted((k, _q2(v)) for k, v in components.items())))
            key = (_slug_model(model_name), variant_name.upper(), attrs["fuel"] or "", attrs["transmission"] or "",
                   attrs["drive"] or "", attrs["seater"] or "", "STANDARD")
            if key in seen:
                first_sheet, first_row = seen[key]
                same = "identical" if signature == seen_signatures.get(key) else "different"
                result.errors.append(
                    f"sheet '{sheet.title}' row {idx} ({model_name} / {variant_name}): repeats the vehicle on "
                    f"sheet '{first_sheet}' row {first_row} with {same} prices; nothing is loaded until it is fixed"
                )
                continue
            seen[key] = (sheet.title, idx)
            seen_signatures[key] = signature
            result.price_rows.append(
                PriceRow(
                    row_no=idx,
                    category="BEV" if attrs["fuel"] == "ELECTRIC" else "ICE" if attrs["fuel"] else "UNSPECIFIED",
                    model_name=model_name,
                    variant_name=variant_name,
                    trim=trim,
                    fuel=attrs["fuel"],
                    transmission=attrs["transmission"],
                    drive=attrs["drive"],
                    seater=attrs["seater"],
                    components={k: _q2(v) for k, v in components.items()},
                    onroad_individual=_q2(onroad_ind),
                    onroad_corporate=_q2(onroad_corp),
                    registration_basis="STANDARD",
                    source_sheet=sheet.title,
                    component_notes=notes_by_key,
                )
            )
    if not sheets_read:
        raise MasterParseError(
            "Price list not recognised: neither the consolidated layout (header got "
            f"{header_seen}) nor a dealer per-model sheet (a Model & Variant or Trim header with column letters)."
        )
    if blank_as_zero:
        result.warnings.append(f"{blank_as_zero} empty price cell(s) were read as nil (no such line for that vehicle)")
    result.meta["sheets"] = sheets_read
    result.meta["models"] = sorted({r.model_name for r in result.price_rows})
    result.meta["categoryCounts"] = _counts(r.category for r in result.price_rows)
    if not result.price_rows and not result.errors:
        raise MasterParseError("Price sheets contained no recognisable vehicle rows.")
    return result


# ── 2. consumer scheme (PDF) ────────────────────────────────────────────────────
_CASH_RE = re.compile(r"cash\s*discount\s*of\s*rs\.?\s*([\d,\s]+)", re.IGNORECASE)
_ACC_RE = re.compile(r"access(?:ories|ory)?\s*worth\s*of\s*rs\.?\s*([\d,\s]+)", re.IGNORECASE)
_EW_RE = re.compile(
    r"(\d)\s*(?:st|nd|rd|th)?\s*year\s*extended\s*warranty\s*rs\.?\s*([\d,\s]+)", re.IGNORECASE
)
_INS_RE = re.compile(r"insurance\s*(?:worth|of)?\s*(?:rs\.?)?\s*([\d,\s]+)", re.IGNORECASE)


def _pdf_pages(content: bytes) -> tuple[list[list[list[list[Any]]]], str]:
    try:
        import pdfplumber
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise MasterParseError(
            "pdfplumber is required to read this PDF scheme document."
        ) from exc
    with pdfplumber.open(BytesIO(content)) as pdf:
        tables = [page.extract_tables() for page in pdf.pages]
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    return tables, text


def parse_consumer_scheme(content: bytes) -> ParseResult:
    result = ParseResult(kind="CONSUMER_SCHEME")
    page_tables, full_text = _pdf_pages(content)
    result.effective_from_hint = _date_hint(full_text)

    carry_product: str | None = None
    row_no = 0
    found_header = False
    for tables in page_tables:
        for table in tables:
            for raw in table:
                if not raw or len(raw) < 10:
                    continue
                cells = [(_text(c) or "") for c in raw[:10]]
                if cells[0].lower().startswith("product") and "variant" in cells[1].lower():
                    found_header = True
                    continue
                total = _money(raw[6])
                if total is None or "₹" not in str(raw[6] or ""):
                    continue  # not a data row (title / legend)
                row_no += 1
                product = cells[0] or carry_product
                carry_product = product
                cash = _money(raw[7]) or Decimal(0)
                other = _money(raw[8]) or Decimal(0)
                if abs((cash + other) - total) > _MONEY_TOLERANCE:
                    result.errors.append(
                        f"consumer row {row_no} ({product}): cash {_q2(cash)} + other "
                        f"{_q2(other)} != total consumer offer {_q2(total)}"
                    )
                    continue

                variant_texts = [
                    v.strip()
                    for v in re.split(r"\n|/(?=\s)", str(raw[1] or ""))
                    if v.strip()
                ]
                description = re.sub(r"\s+", " ", str(raw[9] or "")).strip()
                benefits, warnings = _consumer_benefits(cash, other, description)
                result.discount_rows.append(
                    DiscountRow(
                        row_no=row_no,
                        scheme_category="CONSUMER",
                        model_alias=product or "",
                        variant_texts=variant_texts,
                        benefits=benefits,
                        total_customer_offer=_q2(total),
                        config={
                            "description": description,
                            "cashDiscount": str(_q2(cash)),
                            "otherSchemes": str(_q2(other)),
                            "mAndMContribution": str(_q2(_money(raw[4]) or Decimal(0))),
                            "dealerContribution": str(_q2(_money(raw[5]) or Decimal(0))),
                        },
                        warnings=warnings,
                    )
                )
    if not found_header:
        raise MasterParseError("Not a consumer scheme bulletin (header row not found).")
    result.meta["schemeRows"] = len(result.discount_rows)
    return result


def _consumer_benefits(
    cash: Decimal, other: Decimal, description: str
) -> tuple[list[tuple[str, Decimal]], list[str]]:
    benefits: list[tuple[str, Decimal]] = []
    warnings: list[str] = []
    if cash > 0:
        benefits.append(("CASH_DISCOUNT", _q2(cash)))
        d_cash = _amount(_CASH_RE.search(description))
        if d_cash is not None and abs(d_cash - cash) > _MONEY_TOLERANCE:
            warnings.append(
                f"description cash {d_cash} disagrees with cash column {_q2(cash)}"
            )
    if other > 0:
        parts: list[tuple[str, Decimal]] = []
        acc = _amount(_ACC_RE.search(description))
        if acc is not None:
            parts.append(("ACCESSORIES_KIT", acc))
        ew_match = _EW_RE.search(description)
        if ew_match is not None:
            ew_key = (
                "EXT_WARRANTY_4TH_YR"
                if ew_match.group(1) == "4"
                else "EXT_WARRANTY_4TH_5TH_YR"
            )
            parts.append((ew_key, _amount_str(ew_match.group(2))))
        ins = _amount(_INS_RE.search(description)) if "insurance" in description.lower() else None
        if ins is not None:
            parts.append(("INSURANCE", ins))
        split_total = sum((amount for _, amount in parts), Decimal(0))
        if parts and abs(split_total - other) <= _MONEY_TOLERANCE:
            benefits.extend((key, _q2(amount)) for key, amount in parts)
        else:
            benefits.append(("OTHER_SCHEME", _q2(other)))
            if parts:
                warnings.append(
                    f"could not split 'other schemes' {_q2(other)} from description "
                    f"(parsed {_q2(split_total)}); kept as OTHER_SCHEME"
                )
    return benefits, warnings


def _amount(match: re.Match[str] | None) -> Decimal | None:
    if match is None:
        return None
    return _amount_str(match.group(1))


def _amount_str(raw: str) -> Decimal:
    return Decimal(re.sub(r"[,\s]", "", raw))


# ── 3. exchange / scrappage / welcome (PDF) ─────────────────────────────────────
def parse_exchange_scheme(content: bytes) -> ParseResult:
    result = ParseResult(kind="EXCHANGE_SCHEME")
    page_tables, full_text = _pdf_pages(content)
    result.effective_from_hint = _date_hint(
        full_text.replace("valid from", "w.e.f.")
    )

    row_no = 0
    recognised = 0
    for page_index, tables in enumerate(page_tables, start=1):
        section = _exchange_section(full_text, page_index)
        for table in tables:
            if not table or len(table) < 2:
                continue
            header = [(_text(c) or "").lower() for c in table[0]]
            if "brand" not in " ".join(header):
                continue
            width = len(table[0])
            if width == 5:
                brand_i, a_i, b_i, total_i, note_i = 0, 1, 2, 3, 4
                old_i = scheme_i = None
            elif width >= 7:
                brand_i, old_i, scheme_i, a_i, b_i, total_i, note_i = 0, 1, 2, 3, 4, 5, 6
            else:
                continue
            recognised += 1
            for raw in table[1:]:
                brand = _text(raw[brand_i]) if len(raw) > brand_i else None
                total = _money(raw[total_i]) if len(raw) > total_i else None
                if not brand or total is None:
                    continue
                a_val = _money(raw[a_i]) or Decimal(0)
                b_val = _money(raw[b_i]) or Decimal(0)
                if abs((a_val + b_val) - total) > _MONEY_TOLERANCE:
                    result.errors.append(
                        f"{section} row '{brand}': A {_q2(a_val)} + B {_q2(b_val)} "
                        f"!= total {_q2(total)}"
                    )
                    continue
                row_no += 1
                benefit_key, scheme_category = _EXCHANGE_BENEFIT[section]
                config: dict[str, Any] = {
                    "section": section,
                    "mAndMContribution": str(_q2(a_val)),
                    "dealerContribution": str(_q2(b_val)),
                    "creditNoteWithoutGst": str(
                        _q2(_money(raw[note_i]) or Decimal(0))
                    )
                    if len(raw) > note_i
                    else None,
                }
                if old_i is not None and len(raw) > old_i and _text(raw[old_i]):
                    config["oldVehicleModel"] = _text(raw[old_i])
                if scheme_i is not None and len(raw) > scheme_i and _text(raw[scheme_i]):
                    config["schemeType"] = _text(raw[scheme_i])
                result.discount_rows.append(
                    DiscountRow(
                        row_no=row_no,
                        scheme_category=scheme_category,
                        model_alias=brand,
                        variant_texts=[],
                        benefits=[(benefit_key, _q2(total))],
                        total_customer_offer=_q2(total),
                        config=config,
                    )
                )
    if not recognised:
        raise MasterParseError("No exchange/scrappage ready-reckoner tables found.")
    result.meta["schemeRows"] = len(result.discount_rows)
    result.meta["sections"] = sorted({r.config.get("section") for r in result.discount_rows})
    return result


_EXCHANGE_BENEFIT = {
    "EXCHANGE_PERSONAL": ("EXCHANGE_BONUS", "EXCHANGE"),
    "EXCHANGE_COMMERCIAL": ("EXCHANGE_BONUS", "EXCHANGE"),
    "WELCOME_BONUS": ("WELCOME_BONUS", "WELCOME"),
    "SCRAPPAGE_DEALER": ("SCRAPPAGE_BONUS_DEALER", "SCRAPPAGE"),
    "SCRAPPAGE_COD": ("SCRAPPAGE_BONUS_COD", "SCRAPPAGE"),
}


def _exchange_section(full_text: str, page_index: int) -> str:
    lines = full_text.splitlines()
    # crude page->section map keyed off the section headings present in the deck
    joined = full_text.lower()
    markers = [
        ("exchange scheme – personal brands", "EXCHANGE_PERSONAL"),
        ("exchange scheme – commercial brands", "EXCHANGE_COMMERCIAL"),
        ("welcome bonus", "WELCOME_BONUS"),
        ("customer scrapped the vehicle through m&m", "SCRAPPAGE_DEALER"),
        ("customer approached m&m dealer with cod", "SCRAPPAGE_COD"),
    ]
    order: list[str] = []
    for line in lines:
        low = line.strip().lower()
        for needle, tag in markers:
            if needle in low and (not order or order[-1] != tag):
                order.append(tag)
    del joined
    if page_index - 2 < len(order) and page_index >= 2:
        return order[page_index - 2]
    return order[-1] if order else "EXCHANGE_PERSONAL"


# ── 4. corporate privilege policy (xlsx) ────────────────────────────────────────
_CAT_ROW_LABELS = {
    "cat-b": "B",
    "cat-a": "A",
    "cat-f": "F",
    "cat-y (premium)": "Y",
    "cat-y(premium)": "Y",
    "cat-z (signature)": "Z",
    "cat-z(signature)": "Z",
}
_COMPANY_BLOCK_CATEGORY = {
    '"z" signature': "Z",
    '"y" premium': "Y",
    '"f" focus': "F",
    '"a"': "A",
    '"b"': "B",
}


def parse_corporate_policy(content: bytes) -> ParseResult:
    result = ParseResult(kind="CORPORATE_POLICY")
    workbook = load_workbook(BytesIO(content), data_only=True, read_only=True)
    names = {n.strip().lower(): n for n in workbook.sheetnames}
    policy_name = names.get("corporate policy")
    companies_name = names.get("companies list")
    if not policy_name or not companies_name:
        raise MasterParseError(
            "Corporate policy workbook must have 'Corporate Policy' and 'Companies List' sheets."
        )

    _parse_corporate_matrix(workbook[policy_name], result)
    _parse_company_list(workbook[companies_name], result)

    hint = " ".join(
        str(c)
        for r in list(workbook[policy_name].iter_rows(values_only=True))[:2]
        for c in r
        if isinstance(c, str)
    )
    result.effective_from_hint = _date_hint(hint.replace("From", "w.e.f."))
    result.meta["benefitRows"] = len(result.corporate_benefits)
    result.meta["companies"] = len(result.corporate_companies)
    return result


def _parse_corporate_matrix(sheet: Any, result: ParseResult) -> None:
    rows = list(sheet.iter_rows(values_only=True))
    block: list[tuple[str, int]] = []  # (brand_alias, m&m column index)
    for raw in rows:
        cells = [_text(c) for c in raw]
        label = (cells[1] or "").lower() if len(cells) > 1 else ""
        # a brand-header row: many 3-col groups of M&M/Dealer/Total after col B
        if label == "corporate category":
            block = []
            for col in range(2, len(cells)):
                name = cells[col]
                if name and name.lower() not in {"m&m", "dealer", "total"}:
                    block.append((name, col))
            continue
        category = _CAT_ROW_LABELS.get(label.replace(" ", "")) or _CAT_ROW_LABELS.get(label)
        if not category or not block:
            continue
        for brand_alias, col in block:
            m_and_m = _money(raw[col]) if len(raw) > col else None
            dealer = _money(raw[col + 1]) if len(raw) > col + 1 else None
            total = _money(raw[col + 2]) if len(raw) > col + 2 else None
            if None in (m_and_m, dealer, total):
                continue
            if total == 0:
                continue
            if abs((m_and_m + dealer) - total) > _MONEY_TOLERANCE:
                result.errors.append(
                    f"corporate {category} / {brand_alias}: M&M {m_and_m} + dealer "
                    f"{dealer} != total {total}"
                )
                continue
            result.corporate_benefits.append(
                CorporateBenefitRow(
                    row_no=len(result.corporate_benefits) + 1,
                    privilege_category=category,
                    brand_alias=brand_alias,
                    m_and_m=_q2(m_and_m),
                    dealer=_q2(dealer),
                    total=_q2(total),
                )
            )


def _parse_company_list(sheet: Any, result: ParseResult) -> None:
    rows = list(sheet.iter_rows(values_only=True))
    if len(rows) < 3:
        return
    # row 1 holds block titles; row 2 holds "S. No./Corporate Type/Description/Code"
    header = [(_text(c) or "").lower() for c in rows[1]]
    blocks: list[tuple[str, int]] = []  # (category, code_col_index)
    for col, value in enumerate(header):
        if value == "corporate code":
            # walk left to the block title on row 0
            title = None
            for back in range(col, -1, -1):
                cand = _text(rows[0][back]) if back < len(rows[0]) else None
                if cand:
                    title = cand.lower()
                    break
            category = _COMPANY_BLOCK_CATEGORY.get((title or "").strip())
            if category:
                blocks.append((category, col))
    seen: set[str] = set()
    for raw in rows[2:]:
        for category, code_col in blocks:
            code = raw[code_col] if len(raw) > code_col else None
            name = _text(raw[code_col - 1]) if code_col >= 1 and len(raw) > code_col - 1 else None
            ctype = _text(raw[code_col - 2]) if code_col >= 2 and len(raw) > code_col - 2 else None
            if code is None or not name:
                continue
            code_str = str(int(code)) if isinstance(code, float) and code.is_integer() else str(code).strip()
            if not code_str or code_str in seen:
                continue
            seen.add(code_str)
            result.corporate_companies.append(
                CorporateCompany(
                    corporate_code=code_str,
                    corporate_name=name,
                    corporate_type=ctype,
                    privilege_category=category,
                )
            )


# ── 5. the dealer's discount grid (xlsx) ────────────────────────────────────────
# One sheet: an "Effective ..." line, a header (Model | Booking Protection |
# Agreed Buffer | Insurance OD % | Out of Territory), one row per model (a cell
# may name several models: "PICKUP, MAXX, MAXX HD"), then a Parameter | Notes
# block with the policy wording. Values as written: "30 days", "Nil", "Out of
# scope", 0.6, "Additional 3K".
_GRID_HEADER = {
    "model": ("MODEL",),
    "booking_protection": ("BOOKING PROTECTION",),
    "agreed_buffer": ("AGREED BUFFER", "BUFFER"),
    "insurance_od": ("INSURANCE OD", "OD %"),
    "out_of_territory": ("OUT OF TERRITORY",),
}
_OUT_OF_SCOPE = {"OUT OF SCOPE", "NOT APPLICABLE", "N/A", "NA"}
_NIL = {"NIL", "NONE", "-", "0", "ZERO"}


def _grid_days(value: Any) -> tuple[int | None, str | None]:
    text = (_text(value) or "").upper()
    if not text or text in _OUT_OF_SCOPE:
        return None, None
    if text in _NIL:
        return 0, None
    match = re.search(r"(\d+)", text)
    if match:
        return int(match.group(1)), None
    return None, f"unreadable days '{_text(value)}'"


def _grid_amount(value: Any) -> tuple[Decimal | None, str | None]:
    if isinstance(value, (int, float, Decimal)):
        return _q2(Decimal(str(value))), None
    text = (_text(value) or "").upper()
    if not text or text in _OUT_OF_SCOPE:
        return None, None
    if text in _NIL:
        return Decimal("0.00"), None
    match = re.search(r"(\d+(?:\.\d+)?)\s*(K|L|LAKH|LAC)?", text.replace(",", ""))
    if not match:
        return None, f"unreadable amount '{_text(value)}'"
    amount = Decimal(match.group(1))
    unit = match.group(2) or ""
    if unit == "K":
        amount *= 1000
    elif unit:
        amount *= 100000
    return _q2(amount), None


def _grid_percent(value: Any) -> tuple[Decimal | None, str | None]:
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        return _q2(number * 100 if number <= 1 else number), None
    text = (_text(value) or "").upper()
    if not text or text in _OUT_OF_SCOPE:
        return None, None
    if text in _NIL:
        return Decimal("0.00"), None
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None, f"unreadable percentage '{_text(value)}'"
    number = Decimal(match.group(1))
    return _q2(number * 100 if number <= 1 and "%" not in text else number), None


def parse_discount_grid(content: bytes, *, filename: str | None = None) -> ParseResult:
    result = ParseResult(kind="DISCOUNT_GRID", meta={"layout": "DEALER_DISCOUNT_GRID"})
    workbook = load_workbook(BytesIO(content), data_only=True, read_only=True)
    sheet = workbook[workbook.sheetnames[0]]
    rows = [list(r) for r in sheet.iter_rows(values_only=True)]
    header_row = next(
        (i for i, raw in enumerate(rows)
         if any(_text(c) and _text(c).upper() == "MODEL" for c in raw)
         and any(_text(c) and "BOOKING PROTECTION" in _text(c).upper() for c in raw)),
        None,
    )
    if header_row is None:
        raise MasterParseError(
            "Discount grid not recognised: no 'Model | Booking Protection | ...' header row."
        )
    columns: dict[str, int] = {}
    for col, cell in enumerate(rows[header_row]):
        label = (_text(cell) or "").upper()
        for key, needles in _GRID_HEADER.items():
            if key not in columns and label and any(n in label for n in needles):
                columns[key] = col
    for key in ("model", "booking_protection", "agreed_buffer", "insurance_od", "out_of_territory"):
        if key not in columns:
            raise MasterParseError(f"Discount grid header lacks the {key.replace('_', ' ')} column.")

    blob = " ".join(str(c) for raw in rows[:header_row] for c in raw if isinstance(c, str))
    result.effective_from_hint = _loose_date_hint(blob)
    if result.effective_from_hint is not None:
        result.meta["effectiveFromSource"] = "SHEET"
    elif filename and _loose_date_hint(filename):
        result.effective_from_hint = _loose_date_hint(filename)
        result.meta["effectiveFromSource"] = "FILENAME"

    in_parameters = False
    for idx, raw in enumerate(rows[header_row + 1:], start=header_row + 2):
        cell = _cell_reader(raw)
        first = _text(cell(columns["model"]))
        if not first:
            continue
        if first.upper() == "PARAMETER":
            in_parameters = True
            continue
        if in_parameters:
            note = next((_text(v) for v in raw[columns["model"] + 1:] if _text(v)), None)
            result.grid_parameters.append({"parameter": first, "note": note or ""})
            continue
        values = [cell(columns[k]) for k in ("booking_protection", "agreed_buffer", "insurance_od", "out_of_territory")]
        in_scope = not all((_text(v) or "").upper() in _OUT_OF_SCOPE for v in values)
        days, e1 = _grid_days(values[0])
        buffer, e2 = _grid_amount(values[1])
        od, e3 = _grid_percent(values[2])
        territory, e4 = _grid_amount(values[3])
        problems = [e for e in (e1, e2, e3, e4) if e]
        if problems:
            result.errors.append(f"grid row {idx} ({first}): " + "; ".join(problems))
            continue
        aliases = [a.strip() for a in re.split(r"[,&/|]|\band\b", first, flags=re.IGNORECASE) if a.strip()]
        result.grid_rows.append(
            GridRow(
                row_no=idx,
                model_alias=first,
                model_aliases=aliases or [first],
                in_scope=in_scope,
                booking_protection_days=days,
                agreed_buffer_amount=buffer,
                insurance_od_percent=od,
                out_of_territory_amount=territory,
                raw={
                    "bookingProtection": _text(values[0]),
                    "agreedBuffer": _text(values[1]),
                    "insuranceOd": _text(values[2]),
                    "outOfTerritory": _text(values[3]),
                },
            )
        )
    if not result.grid_rows and not result.errors:
        raise MasterParseError("Discount grid contained no model rows.")
    result.meta["gridRows"] = len(result.grid_rows)
    result.meta["parameters"] = len(result.grid_parameters)
    result.meta["models"] = [r.model_alias for r in result.grid_rows]
    return result


# ── dispatch ────────────────────────────────────────────────────────────────────
_PARSERS = {
    "PRICE_LIST": parse_price_list,
    "CONSUMER_SCHEME": parse_consumer_scheme,
    "EXCHANGE_SCHEME": parse_exchange_scheme,
    "CORPORATE_POLICY": parse_corporate_policy,
    "DISCOUNT_GRID": parse_discount_grid,
}


def parse_master(kind: str, content: bytes, *, filename: str | None = None) -> ParseResult:
    parser = _PARSERS.get(kind)
    if parser is None:
        raise MasterParseError(f"Unknown master kind '{kind}'.")
    if kind in ("PRICE_LIST", "CORPORATE_POLICY", "DISCOUNT_GRID"):
        from audit_core.oem_master_templates import refuse_template_samples

        sample = refuse_template_samples(content)
        if sample:
            raise MasterParseError(sample)
    if kind in ("PRICE_LIST", "DISCOUNT_GRID"):
        return parser(content, filename=filename)
    return parser(content)
