"""oem_price_masters.py — Super Admin intake for an OEM's native price / discount
documents into the tenant-scoped versioned masters.

  POST /v1/admin/oem-masters/uploads?dryRun=true    parse + preview, no writes
  POST /v1/admin/oem-masters/uploads?dryRun=false   parse + ingest + publish
  GET  /v1/admin/oem-masters/uploads?tenantId=       upload history for a project
  GET  /v1/admin/oem-masters/uploads/{uploadId}?tenantId=

Storage is entirely tenant-scoped: everything lands under the target project's
``tenant_id`` in the existing ``price_lists`` / ``discount_schemes`` /
``product_*`` tables. There is no OEM-level master store — a project that has
not been given a project-specific master keeps using whichever version is
effective on the business date (``find_effective_price_plan``), so the last
uploaded master is the default until a newer one supersedes it.

Money is only trusted when it reconciles against the document's own control
totals (see ``oem_master_parsers``). Model / variant resolution is deterministic:
an alias maps the document's model name to the price list's model, variants are
matched exactly, and anything that does not resolve is reported — never guessed.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.db import set_platform_super_admin_context, set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_super_admin_request,
)
from audit_core.discount_schemes import (
    add_discount_benefit,
    create_discount_scheme,
    create_discount_scheme_version,
    publish_discount_scheme_version,
)
from audit_core.errors import NotFoundError, ValidationError
from audit_core.oem_master_parsers import (
    MasterParseError,
    ParseResult,
    parse_master,
)
from audit_core.price_lists import (
    create_price_list,
    create_price_list_version,
    publish_price_list_version,
)

router = APIRouter(prefix="/v1/admin/oem-masters", tags=["admin-oem-masters"])

MasterKind = Literal["PRICE_LIST", "CONSUMER_SCHEME", "EXCHANGE_SCHEME", "CORPORATE_POLICY"]
_MAX_BYTES = 25 * 1024 * 1024
_PRICE_LIST_CODE = "OEM_NATIVE_PRICE_LIST"
_SCHEME_CATEGORY_BY_KIND = {
    "CONSUMER_SCHEME": ("CONSUMER",),
    "EXCHANGE_SCHEME": ("EXCHANGE", "SCRAPPAGE", "WELCOME"),
    "CORPORATE_POLICY": ("CORPORATE",),
}


# ── response shapes ─────────────────────────────────────────────────────────────
class MasterUploadPreview(BaseModel):
    uploadId: UUID | None
    tenantId: str
    oemCode: str
    masterKind: str
    effectiveFrom: date
    sourceFilename: str
    sourceSha256: str
    status: str
    rowCounts: dict[str, Any]
    warnings: list[str]
    errors: list[str]
    unresolved: list[str]
    sample: list[dict[str, Any]]
    priceListVersionId: UUID | None = None
    discountSchemeSummary: dict[str, Any] = {}


class MasterUploadRow(BaseModel):
    uploadId: UUID
    masterKind: str
    effectiveFrom: date
    sourceFilename: str
    sourceSha256: str
    status: str
    rowCounts: dict[str, Any]
    uploadedAtUtc: str
    publishedAtUtc: str | None


# ── helpers ─────────────────────────────────────────────────────────────────────
def _slug_model(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.strip().upper()).strip("_")


def _norm(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", (value or "").upper()).strip()


def _scheme_code(*parts: str) -> str:
    raw = "_".join(_slug_model(p) for p in parts if p)
    if len(raw) <= 110:
        return raw
    digest = hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:8]
    return f"{raw[:100]}_{digest}"


def _project_oem(connection: Connection, tenant_id: str) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT p.tenant_id, p.oem_id, o.oem_code, o.oem_name
            FROM auditcore.projects p
            JOIN auditcore.oems o ON o.oem_id = p.oem_id
            WHERE p.tenant_id = :tenant_id
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-030",
            title="Project not found",
            detail=f"No project exists for tenant '{tenant_id}'.",
        )
    return dict(row)


def _alias_map(connection: Connection, oem_code: str) -> dict[str, str]:
    rows = connection.execute(
        text(
            "SELECT alias_text, canonical_model_name FROM auditcore.oem_model_aliases "
            "WHERE oem_code = :oem_code"
        ),
        {"oem_code": oem_code},
    ).mappings().all()
    return {_norm(r["alias_text"]): r["canonical_model_name"] for r in rows}


def _resolve_models(
    alias_map: dict[str, str], model_alias: str
) -> tuple[list[str], list[str]]:
    """A cell like 'XUV 7XO | THAR ROXX' -> ([canonical model names], [unresolved])."""
    resolved: list[str] = []
    unresolved: list[str] = []
    for part in re.split(r"[|/]", model_alias or ""):
        part = part.strip()
        if not part:
            continue
        cleaned = _norm(re.sub(r"\*+|\(.*?\)|against old.*|including.*", "", part, flags=re.IGNORECASE))
        canonical = alias_map.get(cleaned) or alias_map.get(_norm(part))
        if canonical:
            if canonical not in resolved:
                resolved.append(canonical)
        elif part not in unresolved:
            unresolved.append(part)
    return resolved, unresolved


# ── catalogue upserts ───────────────────────────────────────────────────────────
def _ensure_oem_model(
    connection: Connection, *, oem_id: UUID, model_name: str
) -> UUID:
    code = _slug_model(model_name)[:100]
    existing = connection.execute(
        text(
            "SELECT model_id FROM auditcore.product_models "
            "WHERE oem_id = :oem_id AND model_code = :code"
        ),
        {"oem_id": oem_id, "code": code},
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    return connection.execute(
        text(
            "INSERT INTO auditcore.product_models (oem_id, model_code, model_name) "
            "VALUES (:oem_id, :code, :name) RETURNING model_id"
        ),
        {"oem_id": oem_id, "code": code, "name": model_name.strip()[:200]},
    ).scalar_one()


def _ensure_variant(
    connection: Connection, *, model_id: UUID, variant_name: str, basis: str, attrs: dict[str, Any]
) -> UUID:
    suffix = "" if basis == "STANDARD" else f"__{basis[:3]}"
    code = (_slug_model(variant_name)[:110] + suffix)[:120]
    display = (
        variant_name.strip()
        if basis == "STANDARD"
        else f"{variant_name.strip()} ({basis.title()})"
    )[:240]
    existing = connection.execute(
        text(
            "SELECT variant_id FROM auditcore.product_variants "
            "WHERE model_id = :model_id AND variant_code = :code"
        ),
        {"model_id": model_id, "code": code},
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.product_variants (
                model_id, variant_code, variant_name, fuel_powertrain, transmission,
                body_type, attributes
            ) VALUES (
                :model_id, :code, :name, :fuel, :transmission, :body_type,
                CAST(:attributes AS jsonb)
            ) RETURNING variant_id
            """
        ),
        {
            "model_id": model_id,
            "code": code,
            "name": display,
            "fuel": attrs.get("fuel"),
            "transmission": attrs.get("transmission"),
            "body_type": attrs.get("bodyType"),
            "attributes": json.dumps(attrs),
        },
    ).scalar_one()


def _ensure_sku(
    connection: Connection,
    *,
    oem_id: UUID,
    model_id: UUID,
    variant_id: UUID,
    sku_code: str,
) -> UUID:
    sku_code = sku_code[:160]
    existing = connection.execute(
        text(
            "SELECT product_sku_id, model_id, variant_id FROM auditcore.product_skus "
            "WHERE oem_id = :oem_id AND sku_code = :sku_code"
        ),
        {"oem_id": oem_id, "sku_code": sku_code},
    ).mappings().one_or_none()
    if existing is not None:
        if existing["model_id"] != model_id or existing["variant_id"] != variant_id:
            raise ValidationError(
                detail=f"SKU '{sku_code}' already maps to a different model/variant."
            )
        return existing["product_sku_id"]
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.product_skus (
                oem_id, model_id, variant_id, sku_code, attributes
            ) VALUES (
                :oem_id, :model_id, :variant_id, :sku_code,
                jsonb_build_object('source', 'OEM_NATIVE_PRICE_LIST')
            ) RETURNING product_sku_id
            """
        ),
        {"oem_id": oem_id, "model_id": model_id, "variant_id": variant_id, "sku_code": sku_code},
    ).scalar_one()


# ── price list ingestion ────────────────────────────────────────────────────────
def _next_version_no(connection: Connection, table: str, id_col: str, tenant_id: str, parent_id: UUID) -> int:
    return int(
        connection.execute(
            text(
                f"SELECT COALESCE(MAX(version_no), 0) + 1 FROM auditcore.{table} "
                f"WHERE tenant_id = :tenant_id AND {id_col} = :parent_id"
            ),
            {"tenant_id": tenant_id, "parent_id": parent_id},
        ).scalar_one()
    )


def ingest_price_list(
    connection: Connection,
    *,
    tenant_id: str,
    oem_id: UUID,
    effective_from: date,
    parsed: ParseResult,
    actor_id: str,
) -> tuple[UUID, dict[str, UUID]]:
    """Load a parsed price list; returns (price_list_version_id, {sku_code: sku_id})."""
    price_list_id = connection.execute(
        text(
            "SELECT price_list_id FROM auditcore.price_lists "
            "WHERE tenant_id = :tenant_id AND price_list_code = :code"
        ),
        {"tenant_id": tenant_id, "code": _PRICE_LIST_CODE},
    ).scalar_one_or_none()
    if price_list_id is None:
        price_list_id = create_price_list(
            connection,
            tenant_id=tenant_id,
            code=_PRICE_LIST_CODE,
            name="OEM native consolidated price list",
            actor_id=actor_id,
        )

    version_id = create_price_list_version(
        connection,
        tenant_id=tenant_id,
        price_list_id=price_list_id,
        version_no=_next_version_no(
            connection, "price_list_versions", "price_list_id", tenant_id, price_list_id
        ),
        effective_from=effective_from,
        actor_id=actor_id,
    )

    sku_ids: dict[str, UUID] = {}
    for row in parsed.price_rows:
        model_id = _ensure_oem_model(connection, oem_id=oem_id, model_name=row.model_name)
        variant_id = _ensure_variant(
            connection,
            model_id=model_id,
            variant_name=row.variant_name,
            basis=row.registration_basis,
            attrs={
                "fuel": row.fuel,
                "transmission": row.transmission,
                "drive": row.drive,
                "seater": row.seater,
                "category": row.category,
                "sourceSheet": row.source_sheet,
            },
        )
        suffix = "" if row.registration_basis == "STANDARD" else f"::{row.registration_basis[:3]}"
        sku_code = f"{_slug_model(row.model_name)}::{row.variant_name}{suffix}"
        sku_id = _ensure_sku(
            connection, oem_id=oem_id, model_id=model_id, variant_id=variant_id, sku_code=sku_code
        )
        sku_ids[sku_code] = sku_id
        for component_key, amount in row.components.items():
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.price_list_items (
                        tenant_id, price_list_version_id, product_sku_id,
                        component_key, standard_amount, metadata
                    ) VALUES (
                        :tenant_id, :version_id, :sku_id, :component_key, :amount,
                        CAST(:metadata AS jsonb)
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "version_id": version_id,
                    "sku_id": sku_id,
                    "component_key": component_key,
                    "amount": amount,
                    "metadata": json.dumps(
                        {
                            "source": "OEM_NATIVE_PRICE_LIST",
                            "category": row.category,
                            "onRoadIndividual": str(row.onroad_individual),
                            "onRoadCorporate": str(row.onroad_corporate),
                        }
                    ),
                },
            )

    publish_price_list_version(
        connection, tenant_id=tenant_id, price_list_version_id=version_id, actor_id=actor_id
    )
    return version_id, sku_ids


# ── discount ingestion ──────────────────────────────────────────────────────────
def _model_lookup(connection: Connection, oem_id: UUID) -> dict[str, tuple[UUID, list[tuple[str, UUID]]]]:
    """canonical model name (upper) -> (model_id, [(variant_name_norm, variant_id)])."""
    rows = connection.execute(
        text(
            """
            SELECT m.model_id, m.model_name, v.variant_id, v.variant_name
            FROM auditcore.product_models m
            LEFT JOIN auditcore.product_variants v ON v.model_id = m.model_id
            WHERE m.oem_id = :oem_id
            """
        ),
        {"oem_id": oem_id},
    ).mappings().all()
    out: dict[str, tuple[UUID, list[tuple[str, UUID]]]] = {}
    for row in rows:
        key = row["model_name"].upper()
        entry = out.setdefault(key, (row["model_id"], []))
        if row["variant_id"] is not None:
            entry[1].append((_norm(row["variant_name"]), row["variant_id"]))
    return out


def ingest_discount_document(
    connection: Connection,
    *,
    tenant_id: str,
    oem_id: UUID,
    oem_code: str,
    master_kind: str,
    effective_from: date,
    parsed: ParseResult,
    actor_id: str,
) -> dict[str, Any]:
    alias_map = _alias_map(connection, oem_code)
    models = _model_lookup(connection, oem_id)
    categories = _SCHEME_CATEGORY_BY_KIND[master_kind]
    summary: dict[str, Any] = {"published": 0, "tombstoned": 0, "unresolved": [], "warnings": []}
    live_codes: set[str] = set()

    for drow in parsed.discount_rows:
        resolved_names, unresolved = _resolve_models(alias_map, drow.model_alias)
        if unresolved:
            summary["unresolved"].append(
                f"{drow.scheme_category} row {drow.row_no}: unresolved model(s) "
                f"{unresolved} in '{drow.model_alias}'"
            )
        if not resolved_names:
            continue

        model_targets = [models[name.upper()] for name in resolved_names if name.upper() in models]
        if not model_targets:
            summary["unresolved"].append(
                f"{drow.scheme_category} row {drow.row_no}: model(s) {resolved_names} "
                "not in the loaded price list — load the price list first"
            )
            continue

        code = _scheme_code(
            "MAH",
            drow.scheme_category,
            drow.config.get("section", ""),
            "_".join(_slug_model(n) for n in resolved_names),
            drow.config.get("schemeType", ""),
            "-".join(sorted(v.upper() for v in drow.variant_texts))[:40],
        )
        live_codes.add(code)
        scheme_id = _ensure_scheme(
            connection,
            tenant_id=tenant_id,
            code=code,
            name=f"Mahindra {drow.scheme_category.title()} — {drow.model_alias}"[:240],
            category=drow.scheme_category,
            actor_id=actor_id,
        )
        version_id = create_discount_scheme_version(
            connection,
            tenant_id=tenant_id,
            discount_scheme_id=scheme_id,
            version_no=_next_version_no(
                connection, "discount_scheme_versions", "discount_scheme_id", tenant_id, scheme_id
            ),
            effective_from=effective_from,
            actor_id=actor_id,
        )
        for benefit_key, amount in drow.benefits:
            if amount <= 0:
                continue
            add_discount_benefit(
                connection,
                tenant_id=tenant_id,
                discount_scheme_version_id=version_id,
                benefit_key=benefit_key,
                benefit_type="AMOUNT",
                amount_value=amount,
            )
        for model_id, variants in model_targets:
            _add_eligibility(
                connection,
                tenant_id=tenant_id,
                version_id=version_id,
                model_id=model_id,
                variant_matches=_match_variants(drow.variant_texts, variants),
                drow=drow,
                summary=summary,
            )
        publish_discount_scheme_version(
            connection, tenant_id=tenant_id, discount_scheme_version_id=version_id, actor_id=actor_id
        )
        summary["published"] += 1
        summary["warnings"].extend(drow.warnings)

    summary["tombstoned"] = _tombstone_absent_schemes(
        connection,
        tenant_id=tenant_id,
        categories=categories,
        live_codes=live_codes,
        effective_from=effective_from,
        actor_id=actor_id,
    )
    return summary


def _ensure_scheme(
    connection: Connection, *, tenant_id: str, code: str, name: str, category: str, actor_id: str
) -> UUID:
    existing = connection.execute(
        text(
            "SELECT discount_scheme_id FROM auditcore.discount_schemes "
            "WHERE tenant_id = :tenant_id AND scheme_code = :code"
        ),
        {"tenant_id": tenant_id, "code": code},
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    return create_discount_scheme(
        connection, tenant_id=tenant_id, code=code, name=name, category=category, actor_id=actor_id
    )


def _match_variants(
    variant_texts: list[str], variants: list[tuple[str, UUID]]
) -> list[tuple[str, UUID | None]]:
    by_norm = dict(variants)
    out: list[tuple[str, UUID | None]] = []
    for raw in variant_texts:
        out.append((raw, by_norm.get(_norm(raw))))
    return out


def _add_eligibility(
    connection: Connection,
    *,
    tenant_id: str,
    version_id: UUID,
    model_id: UUID,
    variant_matches: list[tuple[str, UUID | None]],
    drow: Any,
    summary: dict[str, Any],
) -> None:
    customer_type = None
    if drow.scheme_category == "CORPORATE":
        customer_type = f"CORPORATE:{drow.config.get('privilegeCategory')}"

    matched = [(raw, vid) for raw, vid in variant_matches if vid is not None]
    if variant_matches and not matched:
        summary["warnings"].append(
            f"{drow.scheme_category} row {drow.row_no} ({', '.join(t for t, _ in variant_matches)[:80]}): "
            "no price-list variant matched — applied to the whole model"
        )
    criteria = {
        "section": drow.config.get("section"),
        "schemeType": drow.config.get("schemeType"),
        "oldVehicleModel": drow.config.get("oldVehicleModel"),
        "variantTexts": [t for t, _ in variant_matches],
        "unresolvedVariantTexts": [t for t, vid in variant_matches if vid is None],
        "mAndMContribution": drow.config.get("mAndMContribution"),
        "dealerContribution": drow.config.get("dealerContribution"),
        "creditNoteWithoutGst": drow.config.get("creditNoteWithoutGst"),
        "description": drow.config.get("description"),
    }
    criteria = {k: v for k, v in criteria.items() if v not in (None, [], "")}

    if matched:
        for raw, variant_id in matched:
            _insert_eligibility(
                connection,
                tenant_id=tenant_id,
                version_id=version_id,
                model_id=model_id,
                variant_id=variant_id,
                customer_type=customer_type,
                criteria={**criteria, "variantText": raw},
            )
    else:
        _insert_eligibility(
            connection,
            tenant_id=tenant_id,
            version_id=version_id,
            model_id=model_id,
            variant_id=None,
            customer_type=customer_type,
            criteria={**criteria, "scope": "MODEL"},
        )


def _insert_eligibility(
    connection: Connection,
    *,
    tenant_id: str,
    version_id: UUID,
    model_id: UUID,
    variant_id: UUID | None,
    customer_type: str | None,
    criteria: dict[str, Any],
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.discount_scheme_eligibility (
                tenant_id, discount_scheme_version_id, model_id, variant_id,
                customer_type_code, criteria
            ) VALUES (
                :tenant_id, :version_id, :model_id, :variant_id,
                :customer_type, CAST(:criteria AS jsonb)
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "version_id": version_id,
            "model_id": model_id,
            "variant_id": variant_id,
            "customer_type": customer_type,
            "criteria": json.dumps(criteria),
        },
    )


def _tombstone_absent_schemes(
    connection: Connection,
    *,
    tenant_id: str,
    categories: tuple[str, ...],
    live_codes: set[str],
    effective_from: date,
    actor_id: str,
) -> int:
    rows = connection.execute(
        text(
            """
            SELECT s.discount_scheme_id, s.scheme_code
            FROM auditcore.discount_schemes s
            WHERE s.tenant_id = :tenant_id
              AND s.scheme_category = ANY(:categories)
              AND EXISTS (
                  SELECT 1 FROM auditcore.discount_scheme_versions v
                  JOIN auditcore.discount_scheme_benefits b
                    ON b.tenant_id = v.tenant_id
                   AND b.discount_scheme_version_id = v.discount_scheme_version_id
                  WHERE v.tenant_id = s.tenant_id
                    AND v.discount_scheme_id = s.discount_scheme_id
                    AND v.lifecycle_status = 'PUBLISHED'
                    AND v.version_no = (
                        SELECT MAX(v2.version_no) FROM auditcore.discount_scheme_versions v2
                        WHERE v2.tenant_id = v.tenant_id
                          AND v2.discount_scheme_id = v.discount_scheme_id
                    )
              )
            """
        ),
        {"tenant_id": tenant_id, "categories": list(categories)},
    ).mappings().all()

    tombstoned = 0
    for row in rows:
        if row["scheme_code"] in live_codes:
            continue
        version_id = create_discount_scheme_version(
            connection,
            tenant_id=tenant_id,
            discount_scheme_id=row["discount_scheme_id"],
            version_no=_next_version_no(
                connection,
                "discount_scheme_versions",
                "discount_scheme_id",
                tenant_id,
                row["discount_scheme_id"],
            ),
            effective_from=effective_from,
            actor_id=actor_id,
        )
        publish_discount_scheme_version(
            connection, tenant_id=tenant_id, discount_scheme_version_id=version_id, actor_id=actor_id
        )
        tombstoned += 1
    return tombstoned


# ── corporate policy ingestion ──────────────────────────────────────────────────
def ingest_corporate_policy(
    connection: Connection,
    *,
    tenant_id: str,
    oem_id: UUID,
    oem_code: str,
    effective_from: date,
    parsed: ParseResult,
    upload_id: UUID,
    actor_id: str,
) -> dict[str, Any]:
    alias_map = _alias_map(connection, oem_code)
    models = _model_lookup(connection, oem_id)
    summary: dict[str, Any] = {"published": 0, "tombstoned": 0, "unresolved": [], "warnings": [], "companies": 0}
    live_codes: set[str] = set()

    for brow in parsed.corporate_benefits:
        resolved, unresolved = _resolve_models(alias_map, brow.brand_alias)
        if unresolved:
            summary["unresolved"].append(
                f"corporate {brow.privilege_category}: unresolved brand '{brow.brand_alias}'"
            )
        for name in resolved:
            entry = models.get(name.upper())
            if entry is None:
                summary["unresolved"].append(
                    f"corporate {brow.privilege_category} / {name}: not in the price list"
                )
                continue
            model_id, _ = entry
            code = _scheme_code("MAH", "CORPORATE", brow.privilege_category, _slug_model(name))
            live_codes.add(code)
            scheme_id = _ensure_scheme(
                connection,
                tenant_id=tenant_id,
                code=code,
                name=f"Mahindra Corporate Privilege {brow.privilege_category} — {name}"[:240],
                category="CORPORATE",
                actor_id=actor_id,
            )
            version_id = create_discount_scheme_version(
                connection,
                tenant_id=tenant_id,
                discount_scheme_id=scheme_id,
                version_no=_next_version_no(
                    connection, "discount_scheme_versions", "discount_scheme_id", tenant_id, scheme_id
                ),
                effective_from=effective_from,
                actor_id=actor_id,
            )
            add_discount_benefit(
                connection,
                tenant_id=tenant_id,
                discount_scheme_version_id=version_id,
                benefit_key="CORPORATE_PRIVILEGE",
                benefit_type="AMOUNT",
                amount_value=brow.total,
            )
            _insert_eligibility(
                connection,
                tenant_id=tenant_id,
                version_id=version_id,
                model_id=model_id,
                variant_id=None,
                customer_type=f"CORPORATE:{brow.privilege_category}",
                criteria={
                    "privilegeCategory": brow.privilege_category,
                    "mAndMContribution": str(brow.m_and_m),
                    "dealerContribution": str(brow.dealer),
                    "scope": "MODEL",
                },
            )
            publish_discount_scheme_version(
                connection, tenant_id=tenant_id, discount_scheme_version_id=version_id, actor_id=actor_id
            )
            summary["published"] += 1

    summary["tombstoned"] = _tombstone_absent_schemes(
        connection,
        tenant_id=tenant_id,
        categories=("CORPORATE",),
        live_codes=live_codes,
        effective_from=effective_from,
        actor_id=actor_id,
    )

    # company registry — full replace for this OEM
    connection.execute(
        text("DELETE FROM auditcore.corporate_privilege_registry WHERE oem_code = :oem_code"),
        {"oem_code": oem_code},
    )
    for company in parsed.corporate_companies:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.corporate_privilege_registry (
                    oem_code, corporate_code, corporate_name, corporate_type,
                    privilege_category, source_upload_id, effective_from
                ) VALUES (
                    :oem_code, :code, :name, :ctype, :category, :upload_id, :eff
                )
                ON CONFLICT (oem_code, corporate_code) DO UPDATE SET
                    corporate_name = EXCLUDED.corporate_name,
                    corporate_type = EXCLUDED.corporate_type,
                    privilege_category = EXCLUDED.privilege_category,
                    source_upload_id = EXCLUDED.source_upload_id,
                    effective_from = EXCLUDED.effective_from
                """
            ),
            {
                "oem_code": oem_code,
                "code": company.corporate_code[:60],
                "name": company.corporate_name[:400],
                "ctype": (company.corporate_type or None) and company.corporate_type[:200],
                "category": company.privilege_category,
                "upload_id": upload_id,
                "eff": effective_from,
            },
        )
    summary["companies"] = len(parsed.corporate_companies)
    return summary


# ── preview builder ─────────────────────────────────────────────────────────────
def _build_preview(parsed: ParseResult, kind: str) -> dict[str, Any]:
    row_counts: dict[str, Any] = dict(parsed.meta)
    sample: list[dict[str, Any]] = []
    if kind == "PRICE_LIST":
        row_counts["priceRows"] = len(parsed.price_rows)
        sample = [
            {
                "model": r.model_name,
                "variant": r.variant_name,
                "category": r.category,
                "registrationBasis": r.registration_basis,
                "exShowroom": str(r.components["EX_SHOWROOM"]),
                "onRoadIndividual": str(r.onroad_individual),
            }
            for r in parsed.price_rows[:25]
        ]
    elif kind == "CORPORATE_POLICY":
        row_counts["benefitRows"] = len(parsed.corporate_benefits)
        row_counts["companies"] = len(parsed.corporate_companies)
        sample = [
            {
                "privilegeCategory": b.privilege_category,
                "brand": b.brand_alias,
                "mAndM": str(b.m_and_m),
                "dealer": str(b.dealer),
                "total": str(b.total),
            }
            for b in parsed.corporate_benefits[:25]
        ]
    else:
        row_counts["schemeRows"] = len(parsed.discount_rows)
        sample = [
            {
                "schemeCategory": d.scheme_category,
                "model": d.model_alias,
                "variants": d.variant_texts,
                "benefits": [[k, str(v)] for k, v in d.benefits],
                "totalCustomerOffer": str(d.total_customer_offer),
            }
            for d in parsed.discount_rows[:25]
        ]
    warnings = list(parsed.warnings) + [
        w for d in parsed.discount_rows for w in d.warnings
    ]
    return {"rowCounts": row_counts, "sample": sample, "warnings": warnings}


# ── routes ──────────────────────────────────────────────────────────────────────
@router.post("/uploads", response_model=MasterUploadPreview)
async def upload_oem_master(
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    tenant_id: Annotated[str, Form(alias="tenantId")],
    master_kind: Annotated[MasterKind, Form(alias="masterKind")],
    effective_from: Annotated[date, Form(alias="effectiveFrom")],
    file: Annotated[UploadFile, File()],
    dry_run: Annotated[bool, Query(alias="dryRun")] = True,
) -> MasterUploadPreview:
    content = await file.read()
    if not content:
        raise ValidationError(detail="Uploaded file is empty.")
    if len(content) > _MAX_BYTES:
        raise ValidationError(detail="Uploaded file exceeds 25 MB.")
    sha256 = hashlib.sha256(content).hexdigest()

    set_platform_super_admin_context(connection)
    set_tenant_context(connection, tenant_id)
    project = _project_oem(connection, tenant_id)

    try:
        parsed = parse_master(master_kind, content)
    except MasterParseError as exc:
        raise ValidationError(detail=str(exc)) from exc

    preview = _build_preview(parsed, master_kind)
    unresolved: list[str] = []
    actor_id = admin_request.user_id

    upload_id: UUID | None = None
    price_version_id: UUID | None = None
    discount_summary: dict[str, Any] = {}
    status = "PREVIEW"

    if not dry_run:
        if parsed.errors:
            raise ValidationError(
                detail="File has "
                f"{len(parsed.errors)} unreconciled row(s); fix the source and re-upload. "
                f"First: {parsed.errors[0]}"
            )
        upload_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oem_master_uploads (
                    tenant_id, oem_code, master_kind, source_filename, source_sha256,
                    effective_from, status, row_counts, preview, uploaded_by_actor_id
                ) VALUES (
                    :tenant_id, :oem_code, :kind, :filename, :sha, :eff, 'STAGED',
                    CAST(:row_counts AS jsonb), CAST(:preview AS jsonb), :actor
                ) RETURNING upload_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "oem_code": project["oem_code"],
                "kind": master_kind,
                "filename": (file.filename or "upload")[:400],
                "sha": sha256,
                "eff": effective_from,
                "row_counts": json.dumps(preview["rowCounts"]),
                "preview": json.dumps({"sample": preview["sample"], "warnings": preview["warnings"]}),
                "actor": actor_id,
            },
        ).scalar_one()

        if master_kind == "PRICE_LIST":
            price_version_id, _ = ingest_price_list(
                connection,
                tenant_id=tenant_id,
                oem_id=project["oem_id"],
                effective_from=effective_from,
                parsed=parsed,
                actor_id=actor_id,
            )
        elif master_kind == "CORPORATE_POLICY":
            discount_summary = ingest_corporate_policy(
                connection,
                tenant_id=tenant_id,
                oem_id=project["oem_id"],
                oem_code=project["oem_code"],
                effective_from=effective_from,
                parsed=parsed,
                upload_id=upload_id,
                actor_id=actor_id,
            )
        else:
            discount_summary = ingest_discount_document(
                connection,
                tenant_id=tenant_id,
                oem_id=project["oem_id"],
                oem_code=project["oem_code"],
                master_kind=master_kind,
                effective_from=effective_from,
                parsed=parsed,
                actor_id=actor_id,
            )
        unresolved = list(discount_summary.get("unresolved", []))
        connection.execute(
            text(
                """
                UPDATE auditcore.oem_master_uploads
                SET status = 'PUBLISHED', published_at_utc = now(),
                    price_list_version_id = :pv,
                    discount_scheme_summary = CAST(:summary AS jsonb)
                WHERE tenant_id = :tenant_id AND upload_id = :upload_id
                """
            ),
            {
                "pv": price_version_id,
                "summary": json.dumps(discount_summary),
                "tenant_id": tenant_id,
                "upload_id": upload_id,
            },
        )
        status = "PUBLISHED"

    return MasterUploadPreview(
        uploadId=upload_id,
        tenantId=tenant_id,
        oemCode=project["oem_code"],
        masterKind=master_kind,
        effectiveFrom=effective_from,
        sourceFilename=file.filename or "upload",
        sourceSha256=sha256,
        status=status,
        rowCounts=preview["rowCounts"],
        warnings=preview["warnings"] + list(discount_summary.get("warnings", [])),
        errors=list(parsed.errors),
        unresolved=unresolved,
        sample=preview["sample"],
        priceListVersionId=price_version_id,
        discountSchemeSummary=discount_summary,
    )


@router.get("/uploads", response_model=list[MasterUploadRow])
def list_oem_master_uploads(
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    tenant_id: Annotated[str, Query(alias="tenantId")],
) -> list[MasterUploadRow]:
    del admin_request
    set_platform_super_admin_context(connection)
    set_tenant_context(connection, tenant_id)
    rows = connection.execute(
        text(
            """
            SELECT upload_id, master_kind, effective_from, source_filename, source_sha256,
                   status, row_counts, uploaded_at_utc, published_at_utc
            FROM auditcore.oem_master_uploads
            WHERE tenant_id = :tenant_id
            ORDER BY uploaded_at_utc DESC
            LIMIT 200
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().all()
    return [
        MasterUploadRow(
            uploadId=r["upload_id"],
            masterKind=r["master_kind"],
            effectiveFrom=r["effective_from"],
            sourceFilename=r["source_filename"],
            sourceSha256=r["source_sha256"],
            status=r["status"],
            rowCounts=r["row_counts"],
            uploadedAtUtc=r["uploaded_at_utc"].isoformat(),
            publishedAtUtc=r["published_at_utc"].isoformat() if r["published_at_utc"] else None,
        )
        for r in rows
    ]


@router.get("/uploads/{upload_id}", response_model=MasterUploadPreview)
def get_oem_master_upload(
    upload_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    tenant_id: Annotated[str, Query(alias="tenantId")],
) -> MasterUploadPreview:
    del admin_request
    set_platform_super_admin_context(connection)
    set_tenant_context(connection, tenant_id)
    row = connection.execute(
        text(
            """
            SELECT upload_id, oem_code, master_kind, effective_from, source_filename,
                   source_sha256, status, row_counts, preview, discount_scheme_summary,
                   price_list_version_id
            FROM auditcore.oem_master_uploads
            WHERE tenant_id = :tenant_id AND upload_id = :upload_id
            """
        ),
        {"tenant_id": tenant_id, "upload_id": upload_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-031",
            title="Upload not found",
            detail="No OEM master upload with that id for this project.",
        )
    preview = row["preview"] or {}
    summary = row["discount_scheme_summary"] or {}
    return MasterUploadPreview(
        uploadId=row["upload_id"],
        tenantId=tenant_id,
        oemCode=row["oem_code"],
        masterKind=row["master_kind"],
        effectiveFrom=row["effective_from"],
        sourceFilename=row["source_filename"],
        sourceSha256=row["source_sha256"],
        status=row["status"],
        rowCounts=row["row_counts"] or {},
        warnings=list(preview.get("warnings", [])) + list(summary.get("warnings", [])),
        errors=[],
        unresolved=list(summary.get("unresolved", [])),
        sample=list(preview.get("sample", [])),
        priceListVersionId=row["price_list_version_id"],
        discountSchemeSummary=summary,
    )


__all__ = [
    "ingest_corporate_policy",
    "ingest_discount_document",
    "ingest_price_list",
    "router",
]
