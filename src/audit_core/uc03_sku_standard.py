"""The SKU standard: every master line that applies to one vehicle on one date.

Decision 2026-09-30. A caller names the date and the vehicle (a SKU code,
or model / variant / trim / fuel / transmission / drive / seater) and gets
back, in one answer, what the masters of that tenant (project) say for it:

  priceList        the price-list version standing on the date, every
                   component with the date its figure has held since, and
                   the on-road total for the registration basis
  consumerScheme   the consumer scheme benefits the vehicle is entitled to
  exchangeScheme   the exchange / scrappage / welcome benefits, grouped by
                   every scenario they belong to -- not one chosen scenario
  corporate        the corporate privilege: exact for a named corporate, else
                   the range across privilege categories
  grid             the dealer discount grid line for the model
  unknown          the blocks no master answers on that date

This is the master of what is POSSIBLE, not a quotation. The price is the
price list's own figure, carried through untouched; every scheme sits
beside it as a separate optional or conditional line with its own amount,
and nothing is netted off (decision 2026-10-18). What a customer actually
took is settled from the deal's invoice, elsewhere.

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
# Every scenario is answered, so the caller no longer names one.
_SCENARIO_SECTIONS = {
    "EXCHANGE": ("EXCHANGE_PERSONAL", "EXCHANGE_COMMERCIAL"),
    "SCRAPPAGE_DEALER": ("SCRAPPAGE_DEALER",),
    "SCRAPPAGE_COD": ("SCRAPPAGE_COD",),
    "WELCOME": ("WELCOME_BONUS",),
}
RegistrationBasis = Literal["INDIVIDUAL", "CORPORATE"]


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
        # In force until the day before the next version starts (2026-10-20).
        "effectiveTo": plan.get("in_force_to"),
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
                                              'priceSince', pli.price_since)
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


def search_sku(
    rows: list[dict[str, Any]], *, alias_map: dict[str, str], sku_code: str | None = None, model: str | None = None,
    variant: str | None = None, trim: str | None = None, fuel: str | None = None, transmission: str | None = None,
    drive: str | None = None, seater: str | None = None,
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
    if len(matches) == 1:
        return {"matched": "UNIQUE", "sku": _sku_summary(matches[0]), "candidates": [], "row": matches[0]}
    return {
        "matched": "AMBIGUOUS" if matches else "NONE",
        "sku": None,
        "candidates": [_sku_summary(r) for r in matches[:25]],
        "row": None,
    }


# ── the blocks ──────────────────────────────────────────────────────────────────
def _price_block(row: dict[str, Any], version: dict[str, Any], basis: str) -> dict[str, Any]:
    components = []
    total_individual = Decimal(0)
    total_corporate = Decimal(0)
    for item in row["components"]:
        amount = Decimal(str(item["amount"]))
        key = str(item["key"])
        if key != "REGISTRATION_CORPORATE":
            total_individual += amount
        if key != "REGISTRATION_INDIVIDUAL":
            total_corporate += amount
        components.append({
            "key": key,
            "label": COMPONENT_LABELS.get(key, key.replace("_", " ").title()),
            "commercialKey": commercial_key_for_price_component(key, basis=basis),  # type: ignore[arg-type]
            "amount": _money(amount),
            "priceSince": item.get("priceSince"),
        })
    on_road = {"individual": _money(total_individual), "corporate": _money(total_corporate)}
    return {
        **version,
        "components": components,
        "onRoad": {**on_road, "basis": basis, "amount": on_road["corporate" if basis == "CORPORATE" else "individual"]},
    }


def _scheme_rows(connection: Connection, *, tenant_id: str, model_id: UUID, variant_id: UUID | None, on: date) -> list[dict[str, Any]]:
    """Every scheme benefit standing for this vehicle on this date.

    Only the NEWEST published version of each scheme is read. A new month's
    bulletin replaces the last one outright -- the dealer is offered what
    the current bulletin says and nothing else -- so once October's version
    stands, September's benefits are no longer on offer.

    Reading every still-open version instead (the previous behaviour) left
    a withdrawn scheme on offer for ever: ``_tombstone_absent_schemes``
    marks a withdrawal by publishing a version carrying no benefit rows, and
    an empty version shadows nothing, so the superseded version's benefits
    kept matching. The same held for a benefit dropped from a scheme that
    otherwise continued. Versions are immutable once published
    (``protect_published_version``), so the bound is applied on read rather
    than by closing ``effective_to`` on the old row.

    History still answers correctly: a September booking reads the version
    standing in September, because the pick is the newest version effective
    ON OR BEFORE the date asked for, not the newest version outright.
    """
    rows = connection.execute(
        text(
            """
            WITH started AS (
                -- The newest version started on the date answers; when its
                -- bulletin has ended, nothing does (review of PR 472, S1).
                SELECT DISTINCT ON (dsv.discount_scheme_id)
                       dsv.discount_scheme_id, dsv.discount_scheme_version_id, dsv.effective_to
                FROM auditcore.discount_scheme_versions dsv
                WHERE dsv.tenant_id = :t
                  AND dsv.lifecycle_status = 'PUBLISHED'
                  AND dsv.effective_from <= :on
                ORDER BY dsv.discount_scheme_id, dsv.effective_from DESC, dsv.version_no DESC
            ), standing AS (
                SELECT * FROM started WHERE effective_to IS NULL OR effective_to >= :on
            )
            SELECT b.benefit_key, b.benefit_type, b.amount_value, b.percentage_value,
                   ds.scheme_code, ds.scheme_name, ds.scheme_category,
                   dsv.discount_scheme_version_id, dsv.version_no, dsv.effective_from, dsv.effective_to,
                   dsv.combinability_code, e.customer_type_code, e.variant_id, e.criteria
            FROM standing
            JOIN auditcore.discount_scheme_versions dsv
              ON dsv.tenant_id = :t AND dsv.discount_scheme_version_id = standing.discount_scheme_version_id
            JOIN auditcore.discount_scheme_eligibility e
              ON e.tenant_id = dsv.tenant_id AND e.discount_scheme_version_id = dsv.discount_scheme_version_id
            JOIN auditcore.discount_schemes ds
              ON ds.tenant_id = dsv.tenant_id AND ds.discount_scheme_id = dsv.discount_scheme_id
            JOIN auditcore.discount_scheme_benefits b
              ON b.tenant_id = dsv.tenant_id AND b.discount_scheme_version_id = dsv.discount_scheme_version_id
            WHERE e.model_id = :model_id
              AND (e.variant_id IS NULL OR e.variant_id = :variant_id)
            ORDER BY ds.scheme_category, b.benefit_key, dsv.effective_from DESC, e.variant_id DESC NULLS LAST
            """
        ),
        {"t": tenant_id, "model_id": model_id, "variant_id": variant_id, "on": on},
    ).mappings().all()
    # 2026-10-20 (review of PR 472, S9): the same skip as the deal's
    # reconciliation -- a model-level row stored for named variants the price
    # list did not carry is not an offer on this variant.
    return [
        dict(r) for r in rows
        if not (r["variant_id"] is None and (
            (r["criteria"] or {}).get("scope") == "UNRESOLVED" or (r["criteria"] or {}).get("unresolvedVariantTexts")
        ))
    ]


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


def _exchange_block(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Every exchange, scrappage and welcome benefit standing for the
    vehicle, grouped by the scenario each belongs to.

    All four scenarios are returned, not the one the caller named. This
    answers what the customer COULD take; which one they did take is read
    from the deal later. Previously a single ``exchange`` argument chose one
    scenario, and the Deal tab only ever passed EXCHANGE or NONE -- so
    scrappage and the welcome bonus could not be seen from a journey at all.
    """
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
    by_scenario: dict[str, Any] = {}
    for name, sections in _SCENARIO_SECTIONS.items():
        matching = [b for b in benefits if b["section"] in sections]
        by_scenario[name] = {
            "benefits": matching,
            # The scenario's own best line. Lines within one scenario are
            # alternatives (one old vehicle, one bonus), so this is the most
            # the scenario can be worth -- never a sum.
            "best": _money(max((Decimal(b["amount"]) for b in matching if b["amount"]), default=None))
            if matching else None,
        }
    return {"benefits": benefits, "byScenario": by_scenario}


def _corporate_block(
    connection: Connection, rows: list[dict[str, Any]], *, tenant_id: str, on: date, oem_code: str | None,
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
        # The deal's own lookup (the tenant's list in force on the date; a
        # code, else an exact name; never a partial-name match).
        from audit_core.uc03_deal_reconciliation import listed_corporate

        registry = listed_corporate(
            connection, tenant_id=tenant_id, oem_code=oem_code, effective_on=on,
            codes=[corporate_code.strip()] if corporate_code and corporate_code.strip() else [],
            names=[corporate_name.strip()] if corporate_name and corporate_name.strip() else [],
        )
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
            category = registry["category"]
            block["corporate"] = {
                "lookedUp": corporate_code or corporate_name, "found": True, "code": registry["code"],
                "name": registry["name"], "type": registry["type"], "privilegeCategory": category,
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


def _squash_model(value: str | None) -> str:
    """A model name with its spacing and punctuation taken out, so the same
    model spelt two ways across two books compares equal."""
    return re.sub(r"[^A-Z0-9]+", "", (value or "").upper())


def _previous_version(connection: Connection, *, tenant_id: str, version: dict[str, Any]) -> dict[str, Any] | None:
    """The price-list version standing before this one, if any."""
    row = connection.execute(
        text(
            """
            SELECT v.price_list_version_id, v.version_no, v.effective_from, v.effective_to,
                   l.price_list_name, l.price_list_code
            FROM auditcore.price_list_versions v
            JOIN auditcore.price_lists l
              ON l.tenant_id = v.tenant_id AND l.price_list_id = v.price_list_id
            WHERE v.tenant_id = :t
              AND v.lifecycle_status IN ('PUBLISHED', 'RETIRED')
              AND (v.effective_from, v.version_no) < (:eff, :no)
              -- the same book only (2026-10-20, defect 14)
              AND v.price_list_id = (
                  SELECT cur.price_list_id FROM auditcore.price_list_versions cur
                  WHERE cur.tenant_id = :t AND cur.price_list_version_id = CAST(:vid AS uuid)
              )
            ORDER BY v.effective_from DESC, v.version_no DESC
            LIMIT 1
            """
        ),
        {"t": tenant_id, "eff": version["effectiveFrom"], "no": version["version"],
         "vid": str(version["priceListVersionId"])},
    ).mappings().first()
    if row is None:
        return None
    return {
        "priceListVersionId": str(row["price_list_version_id"]),
        "priceList": row["price_list_name"] or row["price_list_code"],
        "version": row["version_no"],
        "effectiveFrom": row["effective_from"],
        "effectiveTo": row["effective_to"],
    }


def _oem_code(connection: Connection, tenant_id: str) -> str | None:
    return connection.execute(
        text("SELECT o.oem_code FROM auditcore.projects p JOIN auditcore.oems o ON o.oem_id = p.oem_id WHERE p.tenant_id = :t"),
        {"t": tenant_id},
    ).scalar_one_or_none()


def standard_for_sku(
    connection: Connection, *, tenant_id: str, on: date, row: dict[str, Any], version: dict[str, Any],
    basis: str = "INDIVIDUAL", corporate_code: str | None = None, corporate_name: str | None = None,
    quantity: int = 1,
) -> dict[str, Any]:
    """The master of everything possible for one priced SKU on one date.

    The price is the price list's own figure, carried through untouched:
    nothing is added to it and no benefit is netted off it. Every scheme,
    privilege and grid line is listed beside it as a separate, optional or
    conditional entry with its own amount. Which of them a customer
    actually took is a question for the deal's invoice, not for the master,
    and the two must not be confused -- so there is deliberately no "net"
    figure here (decision 2026-10-18).

    Note the price list's on-road is itself a fully loaded figure: Mahindra
    prices both extended-warranty tiers, the accessories kit, RSA and
    FASTag into it, and every October row reconciles to the OEM's own
    on-road column on that basis. It is a ceiling, not an expected price.

    ``unknown`` names the blocks no master answers on the date.
    """
    scheme_rows = _scheme_rows(connection, tenant_id=tenant_id, model_id=row["model_id"], variant_id=row["variant_id"], on=on)
    price = _price_block(row, version, basis)
    consumer = _consumer_block(scheme_rows)
    exchange_block = _exchange_block(scheme_rows)
    corporate = _corporate_block(connection, scheme_rows, tenant_id=tenant_id, on=on,
                                 oem_code=_oem_code(connection, tenant_id),
                                 corporate_code=corporate_code, corporate_name=corporate_name)
    grid = _grid_block(connection, tenant_id=tenant_id, model_id=row["model_id"], on=on)
    return {
        "on": on,
        "sku": _sku_summary(row),
        "basis": basis,
        "quantity": quantity,
        "priceList": price,
        "consumerScheme": consumer,
        "exchangeScheme": exchange_block,
        "corporate": corporate,
        "grid": grid,
        "summary": {
            # The price as the price list states it, for the chosen
            # registration basis. Nothing is deducted from it here.
            "onRoad": price["onRoad"]["amount"],
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


@router.get("/price-master")
def price_master(
    tenant_id: str,
    on: date,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    registrationBasis: RegistrationBasis = "INDIVIDUAL",
) -> dict[str, Any]:
    """The whole price book standing on a date: every priced vehicle with
    its components and on-road figure, for reading a month's master.

    ``previousName`` carries the name the same model was priced under in
    the version before this one, where that differs. Mahindra respells
    models between books -- September's XUV3XO is October's 'XUV 3XO' --
    and the two are separate catalogue rows, so seeing the old name beside
    the new one is how a reader tells a rename from a new model.
    """
    _auth(connection, tenant_id, human_principal, authorization_client)
    version = price_version_on(connection, tenant_id=tenant_id, on=on)
    if version is None:
        return {"on": on, "priceList": None, "models": [], "previous": None}
    rows = catalogue_rows(connection, tenant_id=tenant_id, price_list_version_id=version["priceListVersionId"])
    previous = _previous_version(connection, tenant_id=tenant_id, version=version)
    previous_names = (
        {_squash_model(r["model_name"]): r["model_name"]
         for r in catalogue_rows(connection, tenant_id=tenant_id,
                                 price_list_version_id=previous["priceListVersionId"])}
        if previous else {}
    )
    models: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = models.setdefault(str(row["model_id"]), {
            "modelId": str(row["model_id"]),
            "model": row["model_name"],
            "previousName": None,
            "variants": [],
        })
        if entry["previousName"] is None:
            was = previous_names.get(_squash_model(row["model_name"]))
            if was and was != row["model_name"]:
                entry["previousName"] = was
        price = _price_block(row, version, registrationBasis)
        entry["variants"].append({
            **_sku_summary(row),
            "components": price["components"],
            "onRoad": price["onRoad"],
        })
    return {
        "on": on,
        "basis": registrationBasis,
        "priceList": version,
        "previous": previous,
        "models": sorted(models.values(), key=lambda m: m["model"]),
    }


@router.get("/master-documents")
def master_documents(
    tenant_id: str,
    month: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The OEM master documents standing for one month, so anyone working a
    deal can check what the figures were read from.

    ``month`` is YYYY-MM and selects on the document's own effective date,
    not when it was uploaded: a book is filed under the month it applies
    to. The upload history endpoint beside the ingestion API answers the
    same table but is SuperAdmin-only and tenant-wide; a PC checking why a
    price looks wrong needs the month's documents and nothing more.

    Read-only, and it never serves the file itself -- only what was
    recorded about it. ``errorDetail`` is carried through because a
    document that failed to publish is exactly what a reader is looking for
    when the figures are missing.
    """
    _auth(connection, tenant_id, human_principal, authorization_client)
    try:
        first = date.fromisoformat(f"{month}-01")
    except ValueError:
        raise HTTPException(status_code=422, detail="Name the month as YYYY-MM, for example 2026-10.") from None
    nxt = date(first.year + (first.month == 12), 1 if first.month == 12 else first.month + 1, 1)
    rows = connection.execute(
        text(
            """
            SELECT upload_id, oem_code, master_kind, effective_from, source_filename, source_sha256,
                   status, row_counts, error_detail, uploaded_by_actor_id, uploaded_at_utc,
                   published_at_utc, source_object_key
            FROM auditcore.oem_master_uploads
            WHERE tenant_id = :t AND effective_from >= :first AND effective_from < :next
            ORDER BY effective_from, master_kind, uploaded_at_utc
            """
        ),
        {"t": tenant_id, "first": first, "next": nxt},
    ).mappings().all()
    documents = [
        {
            "uploadId": str(row["upload_id"]),
            "oemCode": row["oem_code"],
            "masterKind": row["master_kind"],
            "effectiveFrom": row["effective_from"],
            "fileName": row["source_filename"],
            "sha256": row["source_sha256"],
            "status": row["status"],
            "rowCounts": row["row_counts"] or {},
            "errorDetail": row["error_detail"],
            "uploadedBy": row["uploaded_by_actor_id"],
            "uploadedAtUtc": row["uploaded_at_utc"],
            "publishedAtUtc": row["published_at_utc"],
            # False for anything uploaded before the document itself was
            # kept: those files were never stored and cannot be recovered.
            "openable": bool(row["source_object_key"]),
        }
        for row in rows
    ]
    # The kinds a month is expected to carry. DISCOUNT_GRID is deliberately
    # absent: oem_master_uploads' own CHECK does not allow it, so a grid
    # upload is never recorded here and claiming it is missing would be a
    # lie about the data rather than about the month.
    expected = ("PRICE_LIST", "CONSUMER_SCHEME", "EXCHANGE_SCHEME", "CORPORATE_POLICY")
    present = {d["masterKind"] for d in documents if d["status"] == "PUBLISHED"}
    return {
        "month": month,
        "documents": documents,
        "missingKinds": [kind for kind in expected if kind not in present],
    }


@router.get("/master-documents/{upload_id}/open")
def open_master_document(
    tenant_id: str,
    upload_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """A short-lived link to the master document itself, to open or save.

    The bytes are not served through here: a signed URL lets the browser
    fetch them from object storage directly, the same way a Journey's
    documents are read.
    """
    from audit_core.uc03_p2_storage import (
        P2DocumentStorageError,
        get_p2_document_storage,
    )

    _auth(connection, tenant_id, human_principal, authorization_client)
    row = connection.execute(
        text(
            """
            SELECT source_filename, source_object_key, master_kind, effective_from
            FROM auditcore.oem_master_uploads
            WHERE tenant_id = :t AND upload_id = :u
            """
        ),
        {"t": tenant_id, "u": upload_id},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="No such master document.")
    if not row["source_object_key"]:
        raise HTTPException(
            status_code=404,
            detail="This document was uploaded before the file itself was kept, so it cannot be opened.",
        )
    try:
        url = get_p2_document_storage().presign_get(row["source_object_key"])
    except P2DocumentStorageError as exc:
        raise HTTPException(status_code=502, detail="Document storage is unavailable.") from exc
    return {
        "uploadId": str(upload_id),
        "fileName": row["source_filename"],
        "masterKind": row["master_kind"],
        "effectiveFrom": row["effective_from"],
        "url": url,
        "expiresInSeconds": 1800,
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
    quantity: int = Query(default=1, ge=1, le=1000),
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
    )
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
            corporate_code=corporateCode, corporate_name=corporateName, quantity=quantity,
        ),
    }
