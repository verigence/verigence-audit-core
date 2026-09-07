"""Parsers for an OEM's *native* price / discount documents.

Four inputs, each in the OEM's own layout (not a Verigence template):

  PRICE_LIST       .xlsx  consolidated price list  -> one row per sellable SKU
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
from datetime import date
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
class ParseResult:
    kind: str
    effective_from_hint: date | None = None
    price_rows: list[PriceRow] = field(default_factory=list)
    discount_rows: list[DiscountRow] = field(default_factory=list)
    corporate_benefits: list[CorporateBenefitRow] = field(default_factory=list)
    corporate_companies: list[CorporateCompany] = field(default_factory=list)
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


def _slug_model(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.strip().upper()).strip("_")


_WEF_RE = re.compile(
    r"(?:w\.?e\.?f\.?|valid\s*from|from)\s*:?\s*(\d{1,2})\s*(?:st|nd|rd|th)?\s*"
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


def parse_price_list(content: bytes) -> ParseResult:
    result = ParseResult(kind="PRICE_LIST")
    workbook = load_workbook(BytesIO(content), data_only=True, read_only=True)
    sheet = None
    for name in workbook.sheetnames:
        if name.strip().lower() in {"price list", "pricelist", "consolidated price list"}:
            sheet = workbook[name]
            break
    if sheet is None:
        sheet = workbook[workbook.sheetnames[0]]

    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        raise MasterParseError("Price list sheet is empty.")
    header = [(_text(c) or "") for c in rows[0][: len(_PRICE_HEADER)]]
    if header != list(_PRICE_HEADER):
        raise MasterParseError(
            "Price list header does not match the expected consolidated layout: "
            f"got {header}"
        )

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


# ── dispatch ────────────────────────────────────────────────────────────────────
_PARSERS = {
    "PRICE_LIST": parse_price_list,
    "CONSUMER_SCHEME": parse_consumer_scheme,
    "EXCHANGE_SCHEME": parse_exchange_scheme,
    "CORPORATE_POLICY": parse_corporate_policy,
}


def parse_master(kind: str, content: bytes) -> ParseResult:
    parser = _PARSERS.get(kind)
    if parser is None:
        raise MasterParseError(f"Unknown master kind '{kind}'.")
    return parser(content)
