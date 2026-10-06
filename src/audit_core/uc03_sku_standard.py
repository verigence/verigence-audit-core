"""The SKU standard: every master line that applies to one vehicle on one date.

Decision 2026-09-30. A caller names the date and the vehicle (a SKU code,
or model / variant / trim / fuel / transmission / drive / seater) and gets
back, in one answer, what the masters of that tenant (project) say for it:

  priceList        the price-list version standing on the date, every
                   component with the date its figure has held since, and
                   the on-road total for the registration basis
  consumerScheme   the consumer scheme benefits the vehicle is entitled to
  exchangeScheme   the exchange / scrappage / welcome benefits, each with
                   the scenario it belongs to; the requested scenario picked
  corporate        the corporate privilege: exact for a named corporate, else
                   the range across privilege categories
  grid             the dealer discount grid line for the model
  unknown          the blocks no master answers on that date

Nothing is guessed: a vehicle the masters do not price says so, an
ambiguous search lists its candidates, a block without a master is named
in ``unknown``. Read-only; the same function serves the Deal tab.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.oem_master_parsers import NON_ADDITIVE_KEYS, is_extra_charge_key
from audit_core.oem_price_masters import _alias_map, _resolve_models, grid_row_for_model
from audit_core.price_lists import find_effective_price_plan
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_masters_alignment import commercial_key_for_price_component
from audit_core.uc03_p2_access import authorize_p2

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}/standard", tags=["uc03-sku-standard"])

_READ = "audit.journey.read"

COMPONENT_LABELS: dict[str, str] = {
    "EX_SHOWROOM": "Ex-showroom price",
    "TCS": "TCS",
    "INSURANCE": "Insurance premium",
    "EXT_WARRANTY_4TH_YR": "Extended warranty (4th year)",
    "EXT_WARRANTY_4TH_5TH_YR": "Extended warranty (4th and 5th year)",
    "ACCESSORIES_KIT": "Accessories kit",
    "ESSENTIAL_ACCESSORIES": "Essential accessories",
    "RSA_1YR": "Road-side assistance (1 year)",
    "FASTAG": "FASTag",
    "REGISTRATION_INDIVIDUAL": "Registration (individual)",
    "REGISTRATION_CORPORATE": "Registration (corporate)",
}
BENEFIT_LABELS: dict[str, str] = {
    "CASH_DISCOUNT": "Consumer / cash discount",
    "ACCESSORIES_KIT": "Free accessories",
    "EXT_WARRANTY_4TH_YR": "Free extended warranty (4th year)",
    "EXT_WARRANTY_4TH_5TH_YR": "Free extended warranty (4th and 5th year)",
    "INSURANCE": "Insurance discount",
    "OTHER_SCHEME": "Other scheme",
    "EXCHANGE_BONUS": "Exchange bonus",
    "SCRAPPAGE_BONUS_DEALER": "Scrappage bonus (dealer)",
    "SCRAPPAGE_BONUS_COD": "Scrappage bonus (certificate of deposit)",
    "WELCOME_BONUS": "Loyalty / welcome bonus",
    "CORPORATE_PRIVILEGE": "Corporate privilege",
    "MANAGEMENT_REFERRAL": "Management referral (MR)",
}
_EXCHANGE_CATEGORIES = ("EXCHANGE", "SCRAPPAGE", "WELCOME")
ExchangeScenario = Literal["NONE", "EXCHANGE", "SCRAPPAGE_DEALER", "SCRAPPAGE_COD", "WELCOME"]
_SCENARIO_SECTIONS = {
    "EXCHANGE": ("EXCHANGE_PERSONAL", "EXCHANGE_COMMERCIAL"),
    "SCRAPPAGE_DEALER": ("SCRAPPAGE_DEALER",),
    "SCRAPPAGE_COD": ("SCRAPPAGE_COD",),
    "WELCOME": ("WELCOME_BONUS",),
}
RegistrationBasis = Literal["INDIVIDUAL", "CORPORATE"]
EwOption = Literal["NONE", "4TH", "4TH_5TH"]
InsuranceType = Literal["PRIVATE", "COMMERCIAL"]
_EW_KEY = {"4TH": "EXT_WARRANTY_4TH_YR", "4TH_5TH": "EXT_WARRANTY_4TH_5TH_YR"}
# A customer takes at most one warranty tier, so neither is part of the on-road base; the chosen one is added.
# Registration lines, the hypothecation charge, the minimum booking amount and the dealer's extra charges are
# not summed as price lines either.
_NOT_IN_BASE = frozenset({"EXT_WARRANTY_4TH_YR", "EXT_WARRANTY_4TH_5TH_YR"}) | NON_ADDITIVE_KEYS
_NOT_A_PRICE_LINE = frozenset({"REGISTRATION_WITH_HYPO", "HYPOTHECATION_CHARGE", "MIN_BOOKING_AMOUNT"})
_INSURANCE_SUFFIX = re.compile(r"::(PRI|COM)$")  # the suffix ingest_price_list gives a private / commercial price


def _norm(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", (value or "").upper()).strip()


def _money(value: Any) -> str | None:
    return None if value is None else str(Decimal(str(value)).quantize(Decimal("0.01")))


# ── the catalogue standing on a date ────────────────────────────────────────────
def price_version_on(connection: Connection, *, tenant_id: str, on: date) -> dict[str, Any] | None:
    plan = find_effective_price_plan(connection, tenant_id=tenant_id, effective_on=on)
    if plan is None:
        return None
    return {
        "priceListVersionId": str(plan["price_list_version_id"]),
        "priceList": plan.get("price_list_name") or plan.get("price_list_code"),
        "version": plan.get("version_no"),
        "effectiveFrom": plan.get("effective_from"),
        "effectiveTo": plan.get("effective_to"),
    }


def catalogue_rows(connection: Connection, *, tenant_id: str, price_list_version_id: str) -> list[dict[str, Any]]:
    """Every SKU the version prices, with its attributes and components."""
    rows = connection.execute(
        text(
            """
            SELECT s.product_sku_id, s.sku_code, pm.model_id, pm.model_name, pv.variant_id, pv.variant_name,
                   pv.fuel_powertrain AS fuel, pv.transmission,
                   pv.attributes ->> 'drive' AS drive, pv.attributes ->> 'seater' AS seater,
                   pv.attributes ->> 'trim' AS trim, pv.attributes ->> 'category' AS category,
                   json_agg(json_build_object('key', pli.component_key, 'amount', pli.standard_amount,
                                              'priceSince', pli.price_since, 'note', pli.metadata ->> 'note')
                            ORDER BY pli.component_key) AS components
            FROM auditcore.price_list_items pli
            JOIN auditcore.product_skus s ON s.product_sku_id = pli.product_sku_id
            JOIN auditcore.product_models pm ON pm.model_id = s.model_id
            JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
            WHERE pli.tenant_id = :t AND pli.price_list_version_id = :v
              AND s.is_active AND pm.is_active AND pv.is_active
            GROUP BY s.product_sku_id, s.sku_code, pm.model_id, pm.model_name, pv.variant_id, pv.variant_name,
                     pv.fuel_powertrain, pv.transmission, pv.attributes
            ORDER BY pm.model_name, pv.variant_name
            """
        ),
        {"t": tenant_id, "v": price_list_version_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def _sku_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "productSkuId": str(row["product_sku_id"]),
        "skuCode": row["sku_code"],
        "model": row["model_name"],
        "variant": row["variant_name"],
        "trim": row.get("trim"),
        "fuel": row.get("fuel"),
        "transmission": row.get("transmission"),
        "drive": row.get("drive"),
        "seater": row.get("seater"),
        "category": row.get("category"),
    }


def _base_code(sku_code: str) -> str:
    return _INSURANCE_SUFFIX.sub("", sku_code)


def _insurance_type_of(sku_code: str) -> str | None:
    match = _INSURANCE_SUFFIX.search(sku_code)
    return {"PRI": "PRIVATE", "COM": "COMMERCIAL"}[match.group(1)] if match else None


def pick_insurance_row(rows: list[dict[str, Any]], insurance_type: str | None) -> dict[str, Any]:
    """Among the private / commercial prices of one vehicle: the one asked for, else the private one."""
    for wanted in ([insurance_type] if insurance_type else []) + ["PRIVATE", None]:
        hit = next((r for r in rows if _insurance_type_of(r["sku_code"]) == wanted), None)
        if hit is not None:
            return hit
    return rows[0]


def search_sku(
    rows: list[dict[str, Any]], *, alias_map: dict[str, str], sku_code: str | None = None, model: str | None = None,
    variant: str | None = None, trim: str | None = None, fuel: str | None = None, transmission: str | None = None,
    drive: str | None = None, seater: str | None = None, insurance_type: str | None = None,
) -> dict[str, Any]:
    """EXACT on a SKU code; else the rows matching every attribute given
    (model through the alias map, variant by normalised text, the rest
    by normalised equality): UNIQUE, AMBIGUOUS (candidates listed) or NONE."""
    if sku_code:
        hit = next((r for r in rows if r["sku_code"] == sku_code), None)
        return {"matched": "EXACT" if hit else "NONE", "sku": _sku_summary(hit) if hit else None, "candidates": [], "row": hit}
    matches = rows
    if model:
        canonical, _ = _resolve_models(alias_map, model)
        wanted = {_norm(n) for n in canonical} | {_norm(model)}
        matches = [r for r in matches if _norm(r["model_name"]) in wanted]
    if variant:
        exact = [r for r in matches if _norm(r["variant_name"]) == _norm(variant)]
        matches = exact or [r for r in matches if _norm(variant) in _norm(r["variant_name"])]
    for key, wanted_value in (("trim", trim), ("fuel", fuel), ("transmission", transmission), ("drive", drive), ("seater", seater)):
        if wanted_value:
            matches = [r for r in matches if _norm(r.get(key)) == _norm(wanted_value)]
    if len(matches) > 1 and len({_base_code(r["sku_code"]) for r in matches}) == 1:
        # one vehicle priced twice, for private and for commercial insurance: not ambiguous, the booking form chooses
        matches = [pick_insurance_row(matches, insurance_type)]
    if len(matches) == 1:
        return {"matched": "UNIQUE", "sku": _sku_summary(matches[0]), "candidates": [], "row": matches[0]}
    return {
        "matched": "AMBIGUOUS" if matches else "NONE",
        "sku": None,
        "candidates": [_sku_summary(r) for r in matches[:25]],
        "row": None,
    }


# ── the blocks ──────────────────────────────────────────────────────────────────
def _amounts(row: dict[str, Any]) -> dict[str, Decimal]:
    return {str(item["key"]): Decimal(str(item["amount"])) for item in row["components"]}


def on_road_for(amounts: dict[str, Decimal], *, ew: str, basis: str = "INDIVIDUAL") -> dict[str, Decimal | None]:
    """The on-road price with ONE warranty tier (or none): every price line except the two tiers, the
    registration lines and the notes, plus the chosen tier, plus registration. The with-hypothecation
    figure is added only where the charge is known."""
    base = sum((v for k, v in amounts.items() if k not in _NOT_IN_BASE and not is_extra_charge_key(k)), Decimal(0))
    warranty = amounts.get(_EW_KEY[ew], Decimal(0)) if ew in _EW_KEY else Decimal(0)
    individual = amounts.get("REGISTRATION_INDIVIDUAL", Decimal(0))
    registration = amounts.get("REGISTRATION_CORPORATE", individual) if basis == "CORPORATE" else individual
    without = base + warranty + registration
    hypo = amounts.get("HYPOTHECATION_CHARGE")
    return {"withoutHypo": without, "withHypo": None if hypo is None else without + hypo}


def _price_block(row: dict[str, Any], version: dict[str, Any], basis: str, ew: str = "NONE") -> dict[str, Any]:
    amounts = _amounts(row)
    components = []
    for item in row["components"]:
        key = str(item["key"])
        if key in _NOT_A_PRICE_LINE or is_extra_charge_key(key):
            continue  # shown in the standard format block, never as a price line
        components.append({
            "key": key,
            "label": COMPONENT_LABELS.get(key, key.replace("_", " ").title()),
            "commercialKey": commercial_key_for_price_component(key, basis=basis),  # type: ignore[arg-type]
            "amount": _money(Decimal(str(item["amount"]))),
            "priceSince": item.get("priceSince"),
        })
    individual = on_road_for(amounts, ew=ew, basis="INDIVIDUAL")["withoutHypo"]
    corporate = on_road_for(amounts, ew=ew, basis="CORPORATE")["withoutHypo"]
    on_road = {"individual": _money(individual), "corporate": _money(corporate)}
    return {
        **version,
        "components": components,
        "onRoad": {**on_road, "basis": basis, "ew": ew, "amount": on_road["corporate" if basis == "CORPORATE" else "individual"]},
    }


def _version_hypothecation(connection: Connection, *, tenant_id: str, price_list_version_id: str) -> Decimal | None:
    """The hypothecation charge most of this version's vehicles carry (the dealer prints one charge for
    every vehicle), for a vehicle whose own sheet did not state it."""
    value = connection.execute(
        text(
            """
            SELECT standard_amount FROM auditcore.price_list_items
            WHERE tenant_id = :t AND price_list_version_id = :v AND component_key = 'HYPOTHECATION_CHARGE'
            GROUP BY standard_amount ORDER BY COUNT(*) DESC, standard_amount LIMIT 1
            """
        ),
        {"t": tenant_id, "v": price_list_version_id},
    ).scalar_one_or_none()
    return None if value is None else Decimal(str(value))


def standard_format(
    row: dict[str, Any], version: dict[str, Any], *, basis: str, ew: str,
    version_hypothecation: Decimal | None, catalogue: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """The full standard output for one vehicle (decision 2026-10-06), the same shape for every OEM:
    ex-showroom, TCS, insurance (every option the vehicle has), both warranty tiers, accessories kit and
    essential accessories, RSA, FASTag, registration and on-road without and with hypothecation (one warranty
    tier at most), the hypothecation charge, the minimum booking amount, the dealer's priced extra charges and
    the WEF date. A line the price list does not carry for the vehicle is nil."""
    amounts = _amounts(row)
    notes = {str(i["key"]): i.get("note") for i in row["components"]}
    since = {str(i["key"]): i.get("priceSince") for i in row["components"]}
    own_hypo = amounts.get("HYPOTHECATION_CHARGE")
    hypo, hypo_source = (own_hypo, "PRICE_LIST") if own_hypo is not None else (
        (version_hypothecation, "VERSION_DEFAULT") if version_hypothecation is not None else (None, None))
    working = dict(amounts)
    if hypo is not None:
        working["HYPOTHECATION_CHARGE"] = hypo

    def figures(by_ew: str) -> dict[str, str | None]:
        got = on_road_for(working, ew=by_ew, basis=basis)
        return {"withoutHypo": _money(got["withoutHypo"]), "withHypo": _money(got["withHypo"])}

    individual = amounts.get("REGISTRATION_INDIVIDUAL", Decimal(0))
    registration = amounts.get("REGISTRATION_CORPORATE", individual) if basis == "CORPORATE" else individual
    options = []
    siblings = [r for r in (catalogue or []) if _base_code(r["sku_code"]) == _base_code(row["sku_code"])] or [row]
    for sibling in siblings:
        sib = _amounts(sibling)
        if hypo is not None:
            sib["HYPOTHECATION_CHARGE"] = hypo
        got = on_road_for(sib, ew=ew, basis=basis)
        options.append({
            "type": _insurance_type_of(sibling["sku_code"]) or "STANDARD", "skuCode": sibling["sku_code"],
            "amount": _money(sib.get("INSURANCE", Decimal(0))), "selected": sibling["sku_code"] == row["sku_code"],
            "onRoadWithoutHypo": _money(got["withoutHypo"]), "onRoadWithHypo": _money(got["withHypo"]),
        })
    zero = Decimal(0)
    return {
        "wefDate": version.get("effectiveFrom"),
        "priceListVersion": version.get("version"),
        "ewOption": ew,
        "exShowroom": _money(amounts.get("EX_SHOWROOM", zero)),
        "tcs": _money(amounts.get("TCS", zero)),
        "insurance": {"inHouse": _money(amounts.get("INSURANCE", zero)), "options": options,
                      "selectionNeeded": len(options) > 1 and ew is not None},
        "ewFourthYear": _money(amounts.get("EXT_WARRANTY_4TH_YR", zero)),
        "ewFourthAndFifthYear": _money(amounts.get("EXT_WARRANTY_4TH_5TH_YR", zero)),
        "accessoriesKit": _money(amounts.get("ACCESSORIES_KIT", zero)),
        "essentialAccessories": _money(amounts.get("ESSENTIAL_ACCESSORIES", zero)),
        "rsa": _money(amounts.get("RSA_1YR", zero)),
        "fastag": _money(amounts.get("FASTAG", zero)),
        "registrationWithoutHypo": _money(registration),
        "registrationWithHypo": _money(registration + hypo) if hypo is not None else None,
        "hypothecationCharge": _money(hypo),
        "hypothecationSource": hypo_source,
        "onRoadWithoutHypo": figures(ew)["withoutHypo"],
        "onRoadWithHypo": figures(ew)["withHypo"],
        "onRoadByEw": {name: figures(name) for name in ("NONE", "4TH", "4TH_5TH")},
        "minimumBookingAmount": _money(amounts.get("MIN_BOOKING_AMOUNT")),
        "extraCharges": [
            {"key": k, "label": notes.get(k) or k, "amount": _money(v)} for k, v in sorted(amounts.items()) if is_extra_charge_key(k)
        ],
        "priceSince": {k: v for k, v in since.items() if v is not None and k not in _NOT_A_PRICE_LINE and not is_extra_charge_key(k)},
    }


def _scheme_rows(connection: Connection, *, tenant_id: str, model_id: UUID, variant_id: UUID | None, on: date) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT b.benefit_key, b.benefit_type, b.amount_value, b.percentage_value,
                   ds.scheme_code, ds.scheme_name, ds.scheme_category,
                   dsv.discount_scheme_version_id, dsv.version_no, dsv.effective_from, dsv.effective_to,
                   dsv.combinability_code, e.customer_type_code, e.variant_id, e.criteria
            FROM auditcore.discount_scheme_eligibility e
            JOIN auditcore.discount_scheme_versions dsv
              ON dsv.tenant_id = e.tenant_id AND dsv.discount_scheme_version_id = e.discount_scheme_version_id
            JOIN auditcore.discount_schemes ds
              ON ds.tenant_id = dsv.tenant_id AND ds.discount_scheme_id = dsv.discount_scheme_id
            JOIN auditcore.discount_scheme_benefits b
              ON b.tenant_id = dsv.tenant_id AND b.discount_scheme_version_id = dsv.discount_scheme_version_id
            WHERE e.tenant_id = :t AND e.model_id = :model_id
              AND (e.variant_id IS NULL OR e.variant_id = :variant_id)
              AND dsv.lifecycle_status = 'PUBLISHED'
              AND dsv.effective_from <= :on AND (dsv.effective_to IS NULL OR dsv.effective_to >= :on)
            ORDER BY ds.scheme_category, b.benefit_key, dsv.effective_from DESC, e.variant_id DESC NULLS LAST
            """
        ),
        {"t": tenant_id, "model_id": model_id, "variant_id": variant_id, "on": on},
    ).mappings().all()
    return [dict(r) for r in rows]


def _benefit(row: dict[str, Any]) -> dict[str, Any]:
    criteria = row.get("criteria") or {}
    return {
        "key": row["benefit_key"],
        "label": BENEFIT_LABELS.get(row["benefit_key"], row["benefit_key"].replace("_", " ").title()),
        "amount": _money(row["amount_value"]),
        "percentage": None if row["percentage_value"] is None else str(row["percentage_value"]),
        "scheme": {"code": row["scheme_code"], "name": row["scheme_name"], "category": row["scheme_category"],
                   "version": row["version_no"], "validFrom": row["effective_from"], "validTo": row["effective_to"],
                   "combinability": row["combinability_code"]},
        "scope": "VARIANT" if row.get("variant_id") else "MODEL",
        "section": criteria.get("section"),
        "schemeType": criteria.get("schemeType"),
        "oldVehicleModel": criteria.get("oldVehicleModel"),
        "description": criteria.get("description"),
        "contributions": {k: criteria.get(k) for k in ("mAndMContribution", "dealerContribution", "creditNoteWithoutGst")
                          if criteria.get(k) is not None} or None,
    }


def _consumer_block(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    seen: set[tuple[str, str]] = set()
    benefits = []
    for row in rows:
        if row["scheme_category"] != "CONSUMER":
            continue
        if (row.get("customer_type_code") or "").startswith("CORPORATE"):
            continue
        marker = (row["benefit_key"], row["scheme_code"])
        if marker in seen:
            continue
        seen.add(marker)
        benefits.append(_benefit(row))
    if not benefits:
        return None
    total = sum((Decimal(b["amount"]) for b in benefits if b["amount"]), Decimal(0))
    return {"benefits": benefits, "total": _money(total)}


def _exchange_block(rows: list[dict[str, Any]], scenario: str) -> dict[str, Any] | None:
    seen: set[tuple[Any, ...]] = set()
    benefits = []
    for row in rows:
        if row["scheme_category"] not in _EXCHANGE_CATEGORIES:
            continue
        criteria = row.get("criteria") or {}
        marker = (row["benefit_key"], row["scheme_code"], criteria.get("oldVehicleModel"), criteria.get("schemeType"))
        if marker in seen:
            continue
        seen.add(marker)
        benefits.append(_benefit(row))
    if not benefits:
        return None
    sections = _SCENARIO_SECTIONS.get(scenario, ())
    applicable = [b for b in benefits if b["section"] in sections] if sections else []
    return {
        "scenario": scenario,
        "benefits": benefits,
        "applicable": applicable,
        "applicableMax": _money(max((Decimal(b["amount"]) for b in applicable if b["amount"]), default=None))
        if applicable else None,
    }


def _corporate_block(
    connection: Connection, rows: list[dict[str, Any]], *, oem_code: str | None,
    corporate_code: str | None, corporate_name: str | None,
) -> dict[str, Any] | None:
    by_category: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["scheme_category"] != "CORPORATE":
            continue
        category = str(row.get("customer_type_code") or "").split(":")[-1] or "?"
        by_category.setdefault(category, _benefit(row))
    if not by_category:
        return None
    registry = None
    if (corporate_code or corporate_name) and oem_code:
        registry = connection.execute(
            text(
                """
                SELECT corporate_code, corporate_name, corporate_type, privilege_category
                FROM auditcore.corporate_privilege_registry
                WHERE oem_code = :oem AND (
                      (:code <> '' AND upper(corporate_code) = upper(:code))
                   OR (:name <> '' AND lower(corporate_name) = lower(:name))
                   OR (:name <> '' AND lower(corporate_name) LIKE '%' || lower(:name) || '%'))
                ORDER BY (upper(corporate_code) = upper(:code)) DESC, (lower(corporate_name) = lower(:name)) DESC
                LIMIT 1
                """
            ),
            {"oem": oem_code, "code": corporate_code or "", "name": corporate_name or ""},
        ).mappings().first()
    amounts = [Decimal(b["amount"]) for b in by_category.values() if b["amount"]]
    block: dict[str, Any] = {
        "byCategory": {c: by_category[c] for c in sorted(by_category)},
        "range": {"min": _money(min(amounts)), "max": _money(max(amounts))} if amounts else None,
        "corporate": None,
        "exact": None,
    }
    if corporate_code or corporate_name:
        if registry is None:
            block["corporate"] = {"lookedUp": corporate_code or corporate_name, "found": False}
        else:
            category = registry["privilege_category"]
            block["corporate"] = {
                "lookedUp": corporate_code or corporate_name, "found": True, "code": registry["corporate_code"],
                "name": registry["corporate_name"], "type": registry["corporate_type"], "privilegeCategory": category,
            }
            block["exact"] = by_category.get(category)
    return block


def _grid_block(connection: Connection, *, tenant_id: str, model_id: UUID, on: date) -> dict[str, Any] | None:
    row = grid_row_for_model(connection, tenant_id=tenant_id, model_id=model_id, effective_on=on)
    if row is None:
        return None
    return {
        "version": row["version_no"],
        "effectiveFrom": row["effective_from"],
        "effectiveTo": row["effective_to"],
        "modelAsWritten": row["model_alias"],
        "inScope": row["in_scope"],
        "bookingProtectionDays": row["booking_protection_days"],
        "agreedBuffer": _money(row["agreed_buffer_amount"]),
        # Shown as the maximum discount for now (decision 2026-09-30).
        "insuranceOdPercentMax": None if row["insurance_od_percent"] is None else str(row["insurance_od_percent"]),
        "outOfTerritory": _money(row["out_of_territory_amount"]),
        "parameters": row["parameters"] or [],
    }


def _oem_code(connection: Connection, tenant_id: str) -> str | None:
    return connection.execute(
        text("SELECT o.oem_code FROM auditcore.projects p JOIN auditcore.oems o ON o.oem_id = p.oem_id WHERE p.tenant_id = :t"),
        {"t": tenant_id},
    ).scalar_one_or_none()


def standard_for_sku(
    connection: Connection, *, tenant_id: str, on: date, row: dict[str, Any], version: dict[str, Any],
    basis: str = "INDIVIDUAL", corporate_code: str | None = None, corporate_name: str | None = None,
    exchange: str = "NONE", quantity: int = 1, ew: str = "NONE", catalogue: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Every block for one priced SKU; ``unknown`` names the blocks no master answers."""
    scheme_rows = _scheme_rows(connection, tenant_id=tenant_id, model_id=row["model_id"], variant_id=row["variant_id"], on=on)
    price = _price_block(row, version, basis, ew)
    standard = standard_format(
        row, version, basis=basis, ew=ew, catalogue=catalogue,
        version_hypothecation=None if "HYPOTHECATION_CHARGE" in _amounts(row)
        else _version_hypothecation(connection, tenant_id=tenant_id, price_list_version_id=version["priceListVersionId"]),
    )
    consumer = _consumer_block(scheme_rows)
    exchange_block = _exchange_block(scheme_rows, exchange)
    corporate = _corporate_block(connection, scheme_rows, oem_code=_oem_code(connection, tenant_id),
                                 corporate_code=corporate_code, corporate_name=corporate_name)
    grid = _grid_block(connection, tenant_id=tenant_id, model_id=row["model_id"], on=on)
    on_road = Decimal(price["onRoad"]["amount"])
    consumer_total = Decimal(consumer["total"]) if consumer else Decimal(0)
    exchange_amount = Decimal(exchange_block["applicableMax"]) if exchange_block and exchange_block.get("applicableMax") else Decimal(0)
    corporate_amount = Decimal(corporate["exact"]["amount"]) if corporate and corporate.get("exact") and corporate["exact"]["amount"] else Decimal(0)
    net = on_road - consumer_total - exchange_amount - corporate_amount
    return {
        "on": on,
        "sku": _sku_summary(row),
        "basis": basis,
        "quantity": quantity,
        "priceList": price,
        "standard": standard,
        "consumerScheme": consumer,
        "exchangeScheme": exchange_block,
        "corporate": corporate,
        "grid": grid,
        "summary": {
            "onRoad": _money(on_road),
            "consumerBenefits": _money(consumer_total),
            "exchangeBenefit": _money(exchange_amount) if exchange != "NONE" else None,
            "corporateBenefit": _money(corporate_amount) if corporate and corporate.get("exact") else None,
            "standardNet": _money(net),
            "standardNetForQuantity": _money(net * quantity),
        },
        "unknown": [name for name, block in (("consumerScheme", consumer), ("exchangeScheme", exchange_block),
                                              ("corporate", corporate), ("grid", grid)) if block is None],
    }


# ── routes ──────────────────────────────────────────────────────────────────────
def _auth(connection, tenant_id, principal, client) -> None:
    authorize_p2(connection, tenant_id=tenant_id, journey_id=None, human_principal=principal,
                 authorization_client=client, permission_key=_READ)


@router.get("/catalogue")
def catalogue(
    tenant_id: str,
    on: date,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The models and variants priced on a date, for pickers."""
    _auth(connection, tenant_id, human_principal, authorization_client)
    version = price_version_on(connection, tenant_id=tenant_id, on=on)
    if version is None:
        return {"on": on, "priceList": None, "models": []}
    models: dict[str, dict[str, Any]] = {}
    for row in catalogue_rows(connection, tenant_id=tenant_id, price_list_version_id=version["priceListVersionId"]):
        entry = models.setdefault(str(row["model_id"]), {"modelId": str(row["model_id"]), "model": row["model_name"], "variants": []})
        entry["variants"].append(_sku_summary(row))
    return {"on": on, "priceList": version, "models": list(models.values())}


_PRICE_SHEET_LIMIT = 300


def _price_sheet_source(connection: Connection, *, tenant_id: str, price_list_version_id: str) -> list[str]:
    """The original names of the files this price list version was loaded from."""
    rows = connection.execute(
        text(
            """
            SELECT source_filename FROM auditcore.oem_master_uploads
            WHERE tenant_id = :t AND price_list_version_id = :v AND master_kind = 'PRICE_LIST' AND status = 'PUBLISHED'
            ORDER BY uploaded_at_utc
            """
        ),
        {"t": tenant_id, "v": price_list_version_id},
    ).scalars().all()
    return [str(name) for name in rows]


_PRICE_VERSIONS_LIMIT = 60


@router.get("/price-versions")
def price_versions(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Every price list version the project has (its WEF dates), newest first, with the files each was
    loaded from. The browse screen lists these, so a person can pick a date and compare dates. Read only."""
    _auth(connection, tenant_id, human_principal, authorization_client)
    rows = connection.execute(
        text(
            """
            SELECT pl.price_list_code, pl.price_list_name, plv.price_list_version_id, plv.version_no,
                   plv.effective_from, plv.effective_to, plv.lifecycle_status
            FROM auditcore.price_list_versions plv
            JOIN auditcore.price_lists pl
              ON pl.tenant_id = plv.tenant_id AND pl.price_list_id = plv.price_list_id
            WHERE plv.tenant_id = :t AND plv.lifecycle_status IN ('PUBLISHED', 'RETIRED')
            ORDER BY plv.effective_from DESC, plv.version_no DESC, plv.price_list_version_id DESC
            LIMIT :limit
            """
        ),
        {"t": tenant_id, "limit": _PRICE_VERSIONS_LIMIT},
    ).mappings().all()
    return {
        "versions": [
            {
                "priceListVersionId": str(row["price_list_version_id"]),
                "priceList": row["price_list_name"] or row["price_list_code"],
                "version": row["version_no"],
                "effectiveFrom": row["effective_from"],
                "effectiveTo": row["effective_to"],
                "status": row["lifecycle_status"],
                "sourceFiles": _price_sheet_source(
                    connection, tenant_id=tenant_id, price_list_version_id=str(row["price_list_version_id"])
                ),
            }
            for row in rows
        ]
    }


@router.get("/price-sheet")
def price_sheet(
    tenant_id: str,
    on: date,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    model: str | None = Query(default=None, max_length=200),
    trim: str | None = Query(default=None, max_length=100),
    variant: str | None = Query(default=None, max_length=240),
    fuel: str | None = Query(default=None, max_length=100),
    seater: str | None = Query(default=None, max_length=10),
) -> dict[str, Any]:
    """Browse the price master standing on a date: every vehicle matching the words given (any part of the
    model, trim, variant or fuel; the seating exactly), each in the full standard format. Read only."""
    _auth(connection, tenant_id, human_principal, authorization_client)
    version = price_version_on(connection, tenant_id=tenant_id, on=on)
    if version is None:
        return {"on": on, "priceList": None, "sourceFiles": [], "total": 0, "truncated": False, "vehicles": []}
    rows = catalogue_rows(connection, tenant_id=tenant_id, price_list_version_id=version["priceListVersionId"])
    for key, wanted in (("model_name", model), ("trim", trim), ("variant_name", variant), ("fuel", fuel)):
        if wanted:
            rows = [r for r in rows if _norm(wanted) in _norm(r.get(key))]
    if seater:
        rows = [r for r in rows if _norm(r.get("seater")) == _norm(seater)]
    by_vehicle: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_vehicle.setdefault(_base_code(row["sku_code"]), []).append(row)
    hypo = _version_hypothecation(connection, tenant_id=tenant_id, price_list_version_id=version["priceListVersionId"])
    vehicles = []
    for siblings in list(by_vehicle.values())[:_PRICE_SHEET_LIMIT]:
        chosen = pick_insurance_row(siblings, None)
        vehicles.append({
            "sku": _sku_summary(chosen),
            "standard": standard_format(chosen, version, basis="INDIVIDUAL", ew="NONE", version_hypothecation=hypo,
                                        catalogue=siblings),
        })
    return {
        "on": on, "priceList": version,
        "sourceFiles": _price_sheet_source(connection, tenant_id=tenant_id, price_list_version_id=version["priceListVersionId"]),
        "total": len(by_vehicle), "truncated": len(by_vehicle) > _PRICE_SHEET_LIMIT, "vehicles": vehicles,
    }


@router.get("/sku")
def sku_standard(
    tenant_id: str,
    on: date,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    skuCode: str | None = Query(default=None, max_length=160),
    model: str | None = Query(default=None, max_length=200),
    variant: str | None = Query(default=None, max_length=240),
    trim: str | None = Query(default=None, max_length=100),
    fuel: str | None = Query(default=None, max_length=100),
    transmission: str | None = Query(default=None, max_length=100),
    drive: str | None = Query(default=None, max_length=50),
    seater: str | None = Query(default=None, max_length=10),
    registrationBasis: RegistrationBasis = "INDIVIDUAL",
    corporateCode: str | None = Query(default=None, max_length=60),
    corporateName: str | None = Query(default=None, max_length=400),
    exchange: ExchangeScenario = "NONE",
    quantity: int = Query(default=1, ge=1, le=1000),
    ew: EwOption = "NONE",
    insuranceType: InsuranceType | None = None,
) -> dict[str, Any]:
    _auth(connection, tenant_id, human_principal, authorization_client)
    if not skuCode and not (model or variant):
        raise HTTPException(status_code=422, detail="Name the vehicle: a SKU code, or at least its model or variant.")
    version = price_version_on(connection, tenant_id=tenant_id, on=on)
    if version is None:
        return {"on": on, "sku": None, "matched": "NONE", "candidates": [], "priceList": None,
                "unknown": ["priceList", "consumerScheme", "exchangeScheme", "corporate", "grid"],
                "reason": f"No price list is effective on {on.isoformat()}."}
    rows = catalogue_rows(connection, tenant_id=tenant_id, price_list_version_id=version["priceListVersionId"])
    found = search_sku(
        rows, alias_map=_alias_map(connection, _oem_code(connection, tenant_id) or ""), sku_code=skuCode, model=model,
        variant=variant, trim=trim, fuel=fuel, transmission=transmission, drive=drive, seater=seater,
        insurance_type=insuranceType,
    )
    if found["row"] is not None and insuranceType:
        siblings = [r for r in rows if _base_code(r["sku_code"]) == _base_code(found["row"]["sku_code"])]
        found["row"] = pick_insurance_row(siblings, insuranceType)
    if found["row"] is None:
        return {"on": on, "sku": None, "matched": found["matched"], "candidates": found["candidates"],
                "priceList": version, "unknown": ["priceList", "consumerScheme", "exchangeScheme", "corporate", "grid"],
                "reason": "No priced vehicle matches." if found["matched"] == "NONE"
                else f"{len(found['candidates'])} priced vehicles match; narrow the search or pick a candidate."}
    return {
        "matched": found["matched"],
        "candidates": [],
        **standard_for_sku(
            connection, tenant_id=tenant_id, on=on, row=found["row"], version=version, basis=registrationBasis,
            corporate_code=corporateCode, corporate_name=corporateName, exchange=exchange, quantity=quantity,
            ew=ew, catalogue=rows,
        ),
    }
