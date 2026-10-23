"""The masters as a reader sees them: what has been loaded, and every offer
standing on a date (2026-10-23).

Two read-only answers for the DealGuard Price Masters tab, beside the price
book that ``uc03_sku_standard.price_master`` already serves:

  /masters-loaded   every price list, scheme bulletin, corporate policy and
                    dealer discount grid version that has been published,
                    with the dates it answers for and the document it was
                    read from. Read from the version tables, not the upload
                    log, so a master loaded without an upload row still
                    shows (and says it has no document).
  /offers           the whole book's offers standing on one date: consumer
                    scheme lines, exchange / scrappage / welcome lines,
                    corporate privilege by category, and the dealer grid --
                    the bulletin as data, for every model at once.

The same reading rules as the deal apply, so this view cannot disagree with
what a deal is checked against:

* a scheme answers from the NEWEST published version of it effective on or
  before the date, and not at all once that version has ended
  (``uc03_sku_standard._scheme_rows``);
* a model-level line stored for variants the price book did not carry is not
  an offer (review of PR 472, S9) -- it is listed under ``notApplied`` with
  the variant texts, never silently dropped;
* only the grid version in force on the date answers (R5).

Nothing is netted off any price here and there is no "net" figure.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.oem_price_masters import grid_variant_group, grid_version_for_date
from audit_core.price_lists import OEM_NATIVE_PRICE_LIST_CODE
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import authorize_p2
from audit_core.uc03_sku_standard import BENEFIT_LABELS

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}/standard", tags=["uc03-masters-overview"])

_READ = "audit.journey.read"

# The upload kind each scheme category is read from.
KIND_OF_CATEGORY: dict[str, str] = {
    "CONSUMER": "CONSUMER_SCHEME",
    "EXCHANGE": "EXCHANGE_SCHEME",
    "SCRAPPAGE": "EXCHANGE_SCHEME",
    "WELCOME": "EXCHANGE_SCHEME",
    "CORPORATE": "CORPORATE_POLICY",
}
CATEGORIES_OF_KIND: dict[str, tuple[str, ...]] = {
    "CONSUMER_SCHEME": ("CONSUMER",),
    "EXCHANGE_SCHEME": ("EXCHANGE", "SCRAPPAGE", "WELCOME"),
    "CORPORATE_POLICY": ("CORPORATE",),
}


def _auth(connection: Connection, tenant_id: str, principal: HumanPrincipal,
          client: SecurityAuthorizationClient) -> None:
    authorize_p2(connection, tenant_id=tenant_id, journey_id=None, human_principal=principal,
                 authorization_client=client, permission_key=_READ)


def _money(value: Any) -> str | None:
    return None if value is None else str(Decimal(str(value)).quantize(Decimal("0.01")))


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ── documents ─────────────────────────────────────────────────────────────────
def published_uploads(connection: Connection, *, tenant_id: str) -> list[dict[str, Any]]:
    """Every PUBLISHED master document of the tenant, oldest first."""
    return [dict(r) for r in connection.execute(
        text(
            """
            SELECT upload_id, master_kind, effective_from, source_filename, price_list_version_id,
                   uploaded_at_utc, published_at_utc, (source_object_key IS NOT NULL) AS openable
            FROM auditcore.oem_master_uploads
            WHERE tenant_id = :t AND status = 'PUBLISHED'
            ORDER BY effective_from, published_at_utc NULLS FIRST, uploaded_at_utc
            """
        ),
        {"t": tenant_id},
    ).mappings().all()]


def _document(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "uploadId": str(row["upload_id"]),
        "fileName": row["source_filename"],
        "effectiveFrom": row["effective_from"],
        "openable": bool(row["openable"]),
    }


def _latest_document(uploads: list[dict[str, Any]], kind: str, effective_from: date) -> dict[str, Any] | None:
    """The newest published document of a kind dated exactly ``effective_from``:
    a re-upload on a date replaces the earlier one on it."""
    matching = [u for u in uploads if u["master_kind"] == kind and u["effective_from"] == effective_from]
    return matching[-1] if matching else None


# ── what has been loaded ──────────────────────────────────────────────────────
def price_list_versions(connection: Connection, *, tenant_id: str) -> list[dict[str, Any]]:
    """Every published (or retired) version of the book that prices deals --
    the OEM native list when the tenant has one -- with the last day it is
    in force (the day before the next date a version starts)."""
    rows = [dict(r) for r in connection.execute(
        text(
            """
            SELECT plv.price_list_version_id, plv.version_no, plv.effective_from, plv.effective_to,
                   plv.lifecycle_status, pl.price_list_name, pl.price_list_code,
                   (SELECT count(DISTINCT pli.product_sku_id) FROM auditcore.price_list_items pli
                     WHERE pli.tenant_id = plv.tenant_id
                       AND pli.price_list_version_id = plv.price_list_version_id) AS skus
            FROM auditcore.price_list_versions plv
            JOIN auditcore.price_lists pl ON pl.tenant_id = plv.tenant_id AND pl.price_list_id = plv.price_list_id
            WHERE plv.tenant_id = :t
              AND plv.lifecycle_status IN ('PUBLISHED', 'RETIRED')
              AND (
                  pl.price_list_code = :native
                  OR NOT EXISTS (SELECT 1 FROM auditcore.price_lists own
                                 WHERE own.tenant_id = plv.tenant_id AND own.price_list_code = :native)
              )
            ORDER BY plv.effective_from, plv.version_no
            """
        ),
        {"t": tenant_id, "native": OEM_NATIVE_PRICE_LIST_CODE},
    ).mappings().all()]
    starts = sorted({r["effective_from"] for r in rows})
    newest_on: dict[date, int] = {}
    for row in rows:
        newest_on[row["effective_from"]] = max(newest_on.get(row["effective_from"], 0), row["version_no"])
    for row in rows:
        later = [d for d in starts if d > row["effective_from"]]
        in_force_to = later[0] - timedelta(days=1) if later else None
        if row["effective_to"] is not None and (in_force_to is None or row["effective_to"] < in_force_to):
            in_force_to = row["effective_to"]
        row["in_force_to"] = in_force_to
        # Several versions can be published on one date (a corrected re-upload):
        # only the highest version number on the date answers.
        row["answers"] = row["version_no"] == newest_on[row["effective_from"]]
    return rows


def scheme_windows(connection: Connection, *, tenant_id: str) -> list[dict[str, Any]]:
    """Published scheme versions grouped by the document kind they came from
    and the window they print: one entry per bulletin date."""
    rows = connection.execute(
        text(
            """
            SELECT ds.scheme_category, dsv.effective_from, dsv.effective_to,
                   count(DISTINCT dsv.discount_scheme_id) AS schemes,
                   count(DISTINCT dsv.discount_scheme_id) FILTER (WHERE NOT EXISTS (
                       SELECT 1 FROM auditcore.discount_scheme_benefits b
                       WHERE b.tenant_id = dsv.tenant_id
                         AND b.discount_scheme_version_id = dsv.discount_scheme_version_id)) AS withdrawn
            FROM auditcore.discount_scheme_versions dsv
            JOIN auditcore.discount_schemes ds
              ON ds.tenant_id = dsv.tenant_id AND ds.discount_scheme_id = dsv.discount_scheme_id
            WHERE dsv.tenant_id = :t AND dsv.lifecycle_status = 'PUBLISHED'
            GROUP BY ds.scheme_category, dsv.effective_from, dsv.effective_to
            ORDER BY dsv.effective_from, ds.scheme_category
            """
        ),
        {"t": tenant_id},
    ).mappings().all()
    merged: dict[tuple[str, date, date | None], dict[str, Any]] = {}
    for row in rows:
        kind = KIND_OF_CATEGORY.get(row["scheme_category"] or "")
        if kind is None:
            continue
        key = (kind, row["effective_from"], row["effective_to"])
        entry = merged.setdefault(key, {"kind": kind, "effective_from": row["effective_from"],
                                        "effective_to": row["effective_to"], "categories": [],
                                        "schemes": 0, "withdrawn": 0})
        entry["categories"].append(row["scheme_category"])
        entry["schemes"] += int(row["schemes"])
        entry["withdrawn"] += int(row["withdrawn"])
    return sorted(merged.values(), key=lambda e: (e["effective_from"], e["kind"]))


def grid_versions(connection: Connection, *, tenant_id: str) -> list[dict[str, Any]]:
    rows = [dict(r) for r in connection.execute(
        text(
            """
            SELECT g.grid_version_id, g.version_no, g.effective_from, g.effective_to, g.lifecycle_status,
                   g.source_upload_id,
                   (SELECT count(*) FROM auditcore.dealer_discount_grid_rows r
                     WHERE r.tenant_id = g.tenant_id AND r.grid_version_id = g.grid_version_id) AS lines
            FROM auditcore.dealer_discount_grid_versions g
            WHERE g.tenant_id = :t AND g.lifecycle_status IN ('PUBLISHED', 'RETIRED')
            ORDER BY g.effective_from, g.version_no
            """
        ),
        {"t": tenant_id},
    ).mappings().all()]
    newest_on: dict[date, int] = {}
    for row in rows:
        newest_on[row["effective_from"]] = max(newest_on.get(row["effective_from"], 0), row["version_no"])
    for row in rows:
        row["answers"] = row["version_no"] == newest_on[row["effective_from"]]
    return rows


def masters_loaded_payload(connection: Connection, *, tenant_id: str) -> dict[str, Any]:
    uploads = published_uploads(connection, tenant_id=tenant_id)
    by_upload = {str(u["upload_id"]): u for u in uploads}
    by_price_version: dict[str, dict[str, Any]] = {}
    for upload in uploads:
        if upload["price_list_version_id"] is not None:
            by_price_version[str(upload["price_list_version_id"])] = upload

    price_lists = []
    for row in price_list_versions(connection, tenant_id=tenant_id):
        upload = by_price_version.get(str(row["price_list_version_id"])) or (
            _latest_document(uploads, "PRICE_LIST", row["effective_from"]) if row["answers"] else None)
        price_lists.append({
            "priceListVersionId": str(row["price_list_version_id"]),
            "priceList": row["price_list_name"] or row["price_list_code"],
            "version": row["version_no"],
            "effectiveFrom": row["effective_from"],
            "inForceTo": row["in_force_to"],
            "status": row["lifecycle_status"],
            "answers": row["answers"],
            "skus": int(row["skus"] or 0),
            "document": _document(upload),
        })

    schemes = []
    for entry in scheme_windows(connection, tenant_id=tenant_id):
        schemes.append({
            "kind": entry["kind"],
            "categories": entry["categories"],
            "effectiveFrom": entry["effective_from"],
            "effectiveTo": entry["effective_to"],
            "schemes": entry["schemes"],
            "withdrawn": entry["withdrawn"],
            "document": _document(_latest_document(uploads, entry["kind"], entry["effective_from"])),
        })

    grids = []
    for row in grid_versions(connection, tenant_id=tenant_id):
        upload = by_upload.get(str(row["source_upload_id"])) if row["source_upload_id"] else None
        upload = upload or (_latest_document(uploads, "DISCOUNT_GRID", row["effective_from"]) if row["answers"] else None)
        grids.append({
            "gridVersionId": str(row["grid_version_id"]),
            "version": row["version_no"],
            "effectiveFrom": row["effective_from"],
            "effectiveTo": row["effective_to"],
            "status": row["lifecycle_status"],
            "answers": row["answers"],
            "lines": int(row["lines"] or 0),
            "document": _document(upload),
        })
    return {"priceLists": price_lists, "schemes": schemes, "grids": grids}


@router.get("/masters-loaded")
def masters_loaded(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Every master version published for the Project and the dates it
    answers for, with the document each was read from (``document`` null when
    no published upload is on record for it)."""
    _auth(connection, tenant_id, human_principal, authorization_client)
    return masters_loaded_payload(connection, tenant_id=tenant_id)


# ── every offer standing on a date ────────────────────────────────────────────
def standing_scheme_rows(connection: Connection, *, tenant_id: str, on: date) -> list[dict[str, Any]]:
    """Every benefit line of every scheme standing on the date, for every
    model: ``_scheme_rows`` without the model filter, so the two agree."""
    return [dict(r) for r in connection.execute(
        text(
            """
            WITH started AS (
                SELECT DISTINCT ON (dsv.discount_scheme_id)
                       dsv.discount_scheme_id, dsv.discount_scheme_version_id, dsv.effective_to
                FROM auditcore.discount_scheme_versions dsv
                WHERE dsv.tenant_id = :t AND dsv.lifecycle_status = 'PUBLISHED' AND dsv.effective_from <= :on
                ORDER BY dsv.discount_scheme_id, dsv.effective_from DESC, dsv.version_no DESC
            ), standing AS (
                SELECT * FROM started WHERE effective_to IS NULL OR effective_to >= :on
            )
            SELECT b.benefit_key, b.amount_value, b.percentage_value,
                   ds.scheme_code, ds.scheme_name, ds.scheme_category,
                   dsv.version_no, dsv.effective_from, dsv.effective_to,
                   e.customer_type_code, e.criteria,
                   pm.model_id, pm.model_name, e.variant_id, pv.variant_name
            FROM standing
            JOIN auditcore.discount_scheme_versions dsv
              ON dsv.tenant_id = :t AND dsv.discount_scheme_version_id = standing.discount_scheme_version_id
            JOIN auditcore.discount_scheme_eligibility e
              ON e.tenant_id = dsv.tenant_id AND e.discount_scheme_version_id = dsv.discount_scheme_version_id
            JOIN auditcore.discount_schemes ds
              ON ds.tenant_id = dsv.tenant_id AND ds.discount_scheme_id = dsv.discount_scheme_id
            JOIN auditcore.discount_scheme_benefits b
              ON b.tenant_id = dsv.tenant_id AND b.discount_scheme_version_id = dsv.discount_scheme_version_id
            JOIN auditcore.product_models pm ON pm.model_id = e.model_id
            LEFT JOIN auditcore.product_variants pv ON pv.variant_id = e.variant_id
            ORDER BY pm.model_name, pv.variant_name NULLS FIRST, ds.scheme_category, ds.scheme_code, b.benefit_key
            """
        ),
        {"t": tenant_id, "on": on},
    ).mappings().all()]


def _scheme_ref(row: dict[str, Any]) -> dict[str, Any]:
    return {"code": row["scheme_code"], "name": row["scheme_name"], "category": row["scheme_category"],
            "version": row["version_no"], "validFrom": row["effective_from"], "validTo": row["effective_to"]}


def _line(row: dict[str, Any]) -> dict[str, Any]:
    criteria = _json(row["criteria"])
    return {
        "key": row["benefit_key"],
        "label": BENEFIT_LABELS.get(row["benefit_key"], row["benefit_key"].replace("_", " ").title()),
        "amount": _money(row["amount_value"]),
        "percentage": None if row["percentage_value"] is None else str(row["percentage_value"]),
        "section": criteria.get("section"),
        "schemeType": criteria.get("schemeType"),
        "oldVehicleModel": criteria.get("oldVehicleModel"),
        "description": criteria.get("description"),
        "contributions": {k: criteria.get(k) for k in ("mAndMContribution", "dealerContribution",
                                                       "creditNoteWithoutGst") if criteria.get(k) is not None}
        or None,
    }


def offers_on(connection: Connection, *, tenant_id: str, on: date) -> dict[str, Any]:
    consumer: dict[tuple[Any, ...], dict[str, Any]] = {}
    exchange: list[dict[str, Any]] = []
    corporate: dict[str, dict[str, Any]] = {}
    not_applied: dict[tuple[Any, ...], dict[str, Any]] = {}
    seen_exchange: set[tuple[Any, ...]] = set()

    for row in standing_scheme_rows(connection, tenant_id=tenant_id, on=on):
        criteria = _json(row["criteria"])
        unresolved = row["variant_id"] is None and (
            criteria.get("scope") == "UNRESOLVED" or criteria.get("unresolvedVariantTexts"))
        if unresolved:
            # Not an offer on any priced variant (S9); say which and why.
            key = (row["model_name"], row["scheme_code"])
            entry = not_applied.setdefault(key, {
                "model": row["model_name"], "scheme": _scheme_ref(row),
                "variantTexts": criteria.get("unresolvedVariantTexts") or [], "lines": []})
            entry["lines"].append(_line(row))
            continue
        category = row["scheme_category"]
        if category == "CONSUMER":
            if (row["customer_type_code"] or "").startswith("CORPORATE"):
                continue
            key = (row["model_id"], row["variant_id"], row["scheme_code"])
            entry = consumer.setdefault(key, {
                "modelId": str(row["model_id"]), "model": row["model_name"],
                "variant": row["variant_name"], "scheme": _scheme_ref(row), "benefits": []})
            if all(b["key"] != row["benefit_key"] for b in entry["benefits"]):
                entry["benefits"].append(_line(row))
        elif category in ("EXCHANGE", "SCRAPPAGE", "WELCOME"):
            marker = (row["model_id"], row["variant_id"], row["scheme_code"], row["benefit_key"],
                      criteria.get("oldVehicleModel"), criteria.get("schemeType"), criteria.get("section"))
            if marker in seen_exchange:
                continue
            seen_exchange.add(marker)
            exchange.append({"modelId": str(row["model_id"]), "model": row["model_name"],
                             "variant": row["variant_name"], "scheme": _scheme_ref(row), **_line(row)})
        elif category == "CORPORATE":
            privilege = str(row["customer_type_code"] or "").split(":")[-1] or "?"
            entry = corporate.setdefault(str(row["model_id"]), {
                "modelId": str(row["model_id"]), "model": row["model_name"], "scheme": _scheme_ref(row),
                "byCategory": {}})
            entry["byCategory"].setdefault(privilege, _money(row["amount_value"]))

    for entry in consumer.values():
        entry["total"] = _money(sum((Decimal(b["amount"]) for b in entry["benefits"] if b["amount"]), Decimal(0)))

    return {
        "on": on,
        "consumer": list(consumer.values()),
        "exchange": exchange,
        "corporate": sorted(corporate.values(), key=lambda e: e["model"]),
        "corporateCategories": sorted({c for e in corporate.values() for c in e["byCategory"]}),
        "grid": grid_on(connection, tenant_id=tenant_id, on=on),
        "notApplied": list(not_applied.values()),
    }


def grid_on(connection: Connection, *, tenant_id: str, on: date) -> dict[str, Any] | None:
    """The dealer discount grid version in force on the date, every line."""
    version = grid_version_for_date(connection, tenant_id=tenant_id, effective_on=on)
    if version is None:
        return None
    rows = connection.execute(
        text(
            """
            SELECT r.model_alias, pm.model_name, r.in_scope, r.booking_protection_days, r.agreed_buffer_amount,
                   r.insurance_od_percent, r.out_of_territory_amount, r.raw
            FROM auditcore.dealer_discount_grid_rows r
            LEFT JOIN auditcore.product_models pm ON pm.model_id = r.model_id
            WHERE r.tenant_id = :t AND r.grid_version_id = :v
            ORDER BY r.model_alias, r.grid_row_id
            """
        ),
        {"t": tenant_id, "v": version["grid_version_id"]},
    ).mappings().all()
    lines = []
    for row in rows:
        raw = _json(row["raw"])
        group = raw.get("variantGroup")
        if group is None and row["model_name"]:
            group = grid_variant_group(row["model_alias"], row["model_name"])
        lines.append({
            "modelAsWritten": row["model_alias"],
            "model": row["model_name"],
            "variantGroup": group,
            "inScope": row["in_scope"],
            "bookingProtectionDays": row["booking_protection_days"],
            "agreedBuffer": _money(row["agreed_buffer_amount"]),
            # Shown as the maximum discount for now (decision 2026-09-30).
            "insuranceOdPercentMax": None if row["insurance_od_percent"] is None else str(row["insurance_od_percent"]),
            "outOfTerritory": _money(row["out_of_territory_amount"]),
        })
    parameters = version.get("parameters")
    if isinstance(parameters, str):
        try:
            parameters = json.loads(parameters)
        except ValueError:
            parameters = []
    return {
        "version": version["version_no"],
        "effectiveFrom": version["effective_from"],
        "effectiveTo": version["effective_to"],
        "parameters": parameters or [],
        "lines": lines,
    }


@router.get("/offers")
def offers(
    tenant_id: str,
    on: date,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Every consumer, exchange / scrappage / welcome and corporate offer and
    the dealer grid standing on a date, for every model."""
    _auth(connection, tenant_id, human_principal, authorization_client)
    return offers_on(connection, tenant_id=tenant_id, on=on)


def kinds_standing_on(connection: Connection, *, tenant_id: str, on: date) -> dict[str, date]:
    """For each master kind, the date the version standing on ``on`` started
    -- what a month carries in from before it began."""
    from audit_core.uc03_sku_standard import price_version_on

    standing: dict[str, date] = {}
    book = price_version_on(connection, tenant_id=tenant_id, on=on)
    if book is not None and book["effectiveFrom"] is not None:
        standing["PRICE_LIST"] = book["effectiveFrom"]
    grid = grid_version_for_date(connection, tenant_id=tenant_id, effective_on=on)
    if grid is not None:
        standing["DISCOUNT_GRID"] = grid["effective_from"]
    rows = connection.execute(
        text(
            """
            WITH started AS (
                SELECT DISTINCT ON (dsv.discount_scheme_id)
                       dsv.discount_scheme_id, dsv.discount_scheme_version_id, dsv.effective_from, dsv.effective_to
                FROM auditcore.discount_scheme_versions dsv
                WHERE dsv.tenant_id = :t AND dsv.lifecycle_status = 'PUBLISHED' AND dsv.effective_from <= :on
                ORDER BY dsv.discount_scheme_id, dsv.effective_from DESC, dsv.version_no DESC
            )
            SELECT ds.scheme_category, max(s.effective_from) AS since
            FROM started s
            JOIN auditcore.discount_schemes ds
              ON ds.tenant_id = :t AND ds.discount_scheme_id = s.discount_scheme_id
            WHERE (s.effective_to IS NULL OR s.effective_to >= :on)
              AND EXISTS (SELECT 1 FROM auditcore.discount_scheme_benefits b
                          WHERE b.tenant_id = :t AND b.discount_scheme_version_id = s.discount_scheme_version_id)
            GROUP BY ds.scheme_category
            """
        ),
        {"t": tenant_id, "on": on},
    ).mappings().all()
    for row in rows:
        kind = KIND_OF_CATEGORY.get(row["scheme_category"] or "")
        if kind is not None and (kind not in standing or row["since"] > standing[kind]):
            standing[kind] = row["since"]
    return standing


__all__ = [
    "CATEGORIES_OF_KIND", "KIND_OF_CATEGORY", "grid_on", "kinds_standing_on", "masters_loaded_payload",
    "offers_on", "router",
]
