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
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import structlog
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
    PRICE_COMPONENT_KEYS,
    DiscountRow,
    MasterParseError,
    ParseResult,
    PriceRow,
    is_ev_model_text,
    parse_master,
    parse_model_qualifiers,
    split_model_cell,
    variant_excluded,
    variant_key,
    variant_loose_key,
)
from audit_core.price_lists import (
    create_price_list,
    create_price_list_version,
    publish_price_list_version,
)
from audit_core.uc03_p2_storage import get_p2_document_storage

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/admin/oem-masters", tags=["admin-oem-masters"])

_MASTER_CONTENT_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pdf": "application/pdf",
}


def _master_content_type(filename: str | None) -> str:
    for suffix, content_type in _MASTER_CONTENT_TYPES.items():
        if (filename or "").lower().endswith(suffix):
            return content_type
    return "application/octet-stream"


def _master_object_key(
    *, tenant_id: str, oem_code: str, master_kind: str, effective_from: date,
    upload_id: UUID, filename: str | None,
) -> str:
    """Where a master document is kept.

        {tenant_id}/oem-masters/{OEM}/{YYYY-MM}/{KIND}/{upload_id}_{filename}

    Filed under the month the document is effective for, not the day it was
    uploaded -- an August-named grid whose sheet says "Effective 1st Sep"
    belongs to September, and the parsed date is the one to trust. The
    upload id keeps a re-upload of the same month from overwriting the file
    it supersedes: uploads are not idempotent, so two files for one month
    is ordinary.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", (filename or "upload").strip())[:160] or "upload"
    return (
        f"{tenant_id}/oem-masters/{oem_code.upper()}/{effective_from:%Y-%m}/"
        f"{master_kind}/{upload_id}_{safe}"
    )

MasterKind = Literal["PRICE_LIST", "CONSUMER_SCHEME", "EXCHANGE_SCHEME", "CORPORATE_POLICY", "DISCOUNT_GRID"]
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
    """Where the applied date came from: ADMIN, SHEET or FILENAME."""
    effectiveFromSource: str = "ADMIN"
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


# ── price-list model and variant identity (2026-10-20 product owner rules) ──
# Mahindra respells models between months (September's XUV3XO is October's
# "XUV 3XO", XEV9S is "XEV 9S", SCORPIO CLASSIC is "SCORPIO", NEW THAR 2WD &
# 4WD is "NEW THAR", New SCORPIO N is "SCORPIO-N"). One vehicle must keep one
# model and one SKU code across months, so a model is identified by its name
# with spacing and punctuation squashed out, case-insensitive, plus the
# aliases below. The model keeps the name and code it was first loaded
# under; the spelling a later file printed is kept on each price row.
#
# Exact spellings (upper-cased, single-spaced) that name another model. Only
# the hyphenated "SCORPIO-N" is here: squashed it reads SCORPION, which is
# the OLD Scorpio N of September ("SCORPIO N"), while the October book's
# SCORPIO-N rows come from the "SCORPIO N ... New" sheets.
_MODEL_IDENTITY_BY_SPELLING: dict[str, str] = {
    "SCORPIO-N": "NEWSCORPION",
}
# Squashed name -> squashed identity of the model it is.
_MODEL_IDENTITY_BY_KEY: dict[str, str] = {
    "NEWTHAR": "NEWTHAR2WD4WD",
    "SCORPIO": "SCORPIOCLASSIC",
}
# A model created under an identity is named this way (so October's
# "SCORPIO-N" never mints a code that collides with the old SCORPIO_N).
_MODEL_DISPLAY_BY_IDENTITY: dict[str, str] = {
    "NEWSCORPION": "NEW SCORPIO N",
    "NEWTHAR2WD4WD": "NEW THAR 2WD & 4WD",
    "SCORPIOCLASSIC": "SCORPIO CLASSIC",
    "BOLERO": "BOLERO",
    "BOLERONEO": "BOLERO NEO",
    "BOLERONEOPLUS": "BOLERO NEO PLUS",
}
# September prints the Bolero, the Bolero Neo and the Bolero Neo Plus as one
# model, "THE BOSS- BOLERO+NEO"; October prints them as three ("Bolero",
# "Bolero neo", "Bolero Neo Plus"), and the October schemes and corporate
# policy give them different benefits. Both months follow the October
# split: the variant's own series letter says which one it is (B4/B6/B8 the
# Bolero, N4..N11 the Neo, P4/P10 the Neo Plus).
_BOLERO_FAMILY = frozenset({"THEBOSSBOLERONEO", "BOLERO", "BOLERONEO", "BOLERONEOPLUS"})
_BOLERO_BY_SERIES = {"B": "BOLERO", "N": "BOLERONEO", "P": "BOLERONEOPLUS"}
_SERIES_RE = re.compile(r"^\s*([BNP])\s*\d")


def _squash(value: str | None) -> str:
    """A model name with its spacing and punctuation taken out.

    'XUV 3XO' and 'XUV3XO' are one model; the spaces and hyphens in OEM
    model names move between bulletins and carry no meaning.
    """
    return re.sub(r"[^A-Z0-9]+", "", (value or "").upper())


def price_list_model_key(model_name: str | None, variant_name: str | None = None) -> str:
    """The identity of the model a price row (or a document) names: the
    squashed name, through the aliases above, with the Bolero family split
    by the variant's series letter. Pure; the same rule for loading a book
    and for looking a vehicle up in one."""
    spelled = re.sub(r"\s+", " ", (model_name or "").strip().upper())
    key = _MODEL_IDENTITY_BY_SPELLING.get(spelled) or _squash(model_name)
    key = _MODEL_IDENTITY_BY_KEY.get(key, key)
    if key in _BOLERO_FAMILY and variant_name:
        match = _SERIES_RE.match(variant_name.upper())
        if match:
            key = _BOLERO_BY_SERIES[match.group(1)]
    return key


# Release markers the OEM appends to a variant and changes between months:
# September's new Scorpio N "Z8 S D AT 2WD 7 STR BS6.2 - Refresh" is
# October's "Z8 S D AT 2WD 7 STR BS6.2 - N". Taken off only to recognise the
# same variant; never when two rows of one file differ by nothing else
# (October's NEW THAR has "LXT D MT 4WD 4S HT BS6.2" and the same "- Refresh",
# two vehicles).
_RELEASE_MARKER_RE = re.compile(r"\s*-\s*(?:NEW|REFRESH|N)\s*$")


def variant_core(variant_name: str | None) -> str:
    text_value = re.sub(r"\s+", " ", (variant_name or "").strip().upper())
    while True:
        stripped = _RELEASE_MARKER_RE.sub("", text_value)
        if stripped == text_value:
            return text_value
        text_value = stripped


def _variant_slug(variant_name: str) -> str:
    """A variant's code. "+" is a word ("THREE+" is not "THREE": the BE 6
    SPORTEQ THREE+ is a different, Rs 1,00,000 dearer variant), so it reads
    PLUS rather than being stripped."""
    return _slug_model(variant_name.replace("+", " PLUS "))


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


@dataclass
class _ModelPart:
    """One model a scheme's model cell names, with what its wording adds
    (exclusions, an old-vehicle condition, "all variants")."""

    raw: str
    base: str
    canonical: str | None
    qualifiers: dict[str, Any] = field(default_factory=dict)


def _model_parts(alias_map: dict[str, str], model_alias: str) -> list[_ModelPart]:
    """2026-10-20 product owner rules: the cell is split on "|" and "/" only
    outside brackets, and each part's brackets are stripped balanced, so
    "XUV 3XO (Excluding EDGE & REVX M(O) PM AT )" is XUV 3XO with two
    excluded variants instead of a part nothing resolves."""
    parts: list[_ModelPart] = []
    for part in split_model_cell(model_alias):
        base, qualifiers = parse_model_qualifiers(part)
        canonical = alias_map.get(_norm(base)) or alias_map.get(_norm(part))
        parts.append(_ModelPart(raw=part, base=base, canonical=canonical, qualifiers=qualifiers))
    return parts


def _resolve_models(
    alias_map: dict[str, str], model_alias: str
) -> tuple[list[str], list[str]]:
    """A cell like 'XUV 7XO | THAR ROXX' -> ([canonical model names], [unresolved])."""
    resolved: list[str] = []
    unresolved: list[str] = []
    for part in _model_parts(alias_map, model_alias):
        if part.canonical:
            if part.canonical not in resolved:
                resolved.append(part.canonical)
        elif part.raw not in unresolved:
            unresolved.append(part.raw)
    return resolved, unresolved


# ── catalogue upserts ───────────────────────────────────────────────────────────
# A native consolidated price list is 300+ rows; issuing one SELECT/INSERT per row
# (or worse, per row per price component) is enough sequential round trips to
# exceed the edge proxy's request timeout. Every ``_bulk_ensure_*`` helper below
# resolves its whole catalogue in a small, fixed number of round trips: one SELECT
# to load everything that already exists for this OEM, then at most one multi-row
# INSERT (chunked) for whatever is missing.
def _chunked_insert_returning(
    connection: Connection,
    *,
    table: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    returning: list[str],
    jsonb_columns: frozenset[str] = frozenset(),
    chunk_size: int = 500,
) -> list[dict[str, Any]]:
    """INSERT ``rows`` as a small number of multi-row statements (one per chunk),
    each with its own placeholders — never string-interpolates a value. ``table``,
    ``columns`` and ``returning`` are internal identifiers, never user input."""
    if not rows:
        return []
    out: list[dict[str, Any]] = []
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start : start + chunk_size]
        params: dict[str, Any] = {}
        values_sql: list[str] = []
        for i, row in enumerate(chunk):
            placeholders = []
            for col in columns:
                key = f"{col}_{i}"
                params[key] = row[col]
                placeholders.append(f"CAST(:{key} AS jsonb)" if col in jsonb_columns else f":{key}")
            values_sql.append(f"({', '.join(placeholders)})")
        sql = f"INSERT INTO auditcore.{table} ({', '.join(columns)}) VALUES {', '.join(values_sql)}"
        if returning:
            sql += f" RETURNING {', '.join(returning)}"
        result = connection.execute(text(sql), params)
        if returning:
            out.extend(dict(r) for r in result.mappings().all())
    return out


_SEGMENT_BY_CATEGORY = {"PV": "PASSENGER_VEHICLE", "CV": "COMMERCIAL", "BEV": "BATTERY_ELECTRIC", "EV": "BATTERY_ELECTRIC"}


def _row_segment(row: PriceRow) -> str | None:
    """The segment a price row states: its PV / CV / BEV category, or an
    electric fuel. A dealer per-model sheet says only ICE -- not whether it
    is a passenger or commercial vehicle -- and is left unstated."""
    if str(row.fuel or "").strip().upper() in {"ELECTRIC", "EV", "BEV"}:
        return "BATTERY_ELECTRIC"
    return _SEGMENT_BY_CATEGORY.get(str(row.category or "").strip().upper())


def _set_model_segments(connection: Connection, *, rows: list[PriceRow], model_ids: list[UUID]) -> None:
    """The vehicle category (PV / CV / EV) on each model, from the rows'
    own Category column (decision 2026-10-20). Set only where every row of
    the model agrees; a model already in a segment is never moved."""
    stated: dict[UUID, set[str]] = {}
    for row, model_id in zip(rows, model_ids):
        segment = _row_segment(row)
        if segment is not None:
            stated.setdefault(model_id, set()).add(segment)
    for model_id, segments in stated.items():
        if len(segments) != 1:
            continue
        connection.execute(
            text(
                """
                UPDATE auditcore.product_models pm SET segment_id = s.segment_id, updated_at_utc = now()
                FROM auditcore.segments s
                WHERE s.segment_code = :code AND pm.model_id = :model_id AND pm.segment_id IS NULL
                """
            ),
            {"code": next(iter(segments)), "model_id": model_id},
        )


def _bulk_ensure_model_identities(
    connection: Connection, *, oem_id: UUID, rows: list[PriceRow], conflicts: list[str] | None = None,
) -> dict[str, tuple[UUID, str]]:
    """identity key -> (model_id, model_code) for every row's model.

    A model the OEM already carries under any spelling of the same identity
    is reused, with its name and code (2026-10-20 product owner rules: one
    vehicle, one SKU code across months). Where several existing models
    share an identity, the one that owns this OEM's native price-list SKUs
    wins (the most of them), then the one named as the identity's canonical
    model, then the oldest: a model some other import created under a
    similar name (a project master's "Scorpio") never captures the vehicle
    and orphans the SKUs Journeys are pinned to. Two candidates that both
    own price-list SKUs are reported in ``conflicts``. One SELECT and at
    most one batched INSERT."""
    wanted: dict[str, str] = {}
    for row in rows:
        key = price_list_model_key(row.model_name, row.variant_name)
        wanted.setdefault(key, _MODEL_DISPLAY_BY_IDENTITY.get(key, row.model_name.strip()))

    existing = connection.execute(
        text(
            """
            SELECT pm.model_id, pm.model_code, pm.model_name,
                   (SELECT count(*) FROM auditcore.product_skus s
                     WHERE s.model_id = pm.model_id
                       AND s.attributes ->> 'source' = 'OEM_NATIVE_PRICE_LIST') AS native_skus
            FROM auditcore.product_models pm
            WHERE pm.oem_id = :oem_id
            ORDER BY pm.created_at_utc, pm.model_code
            """
        ),
        {"oem_id": oem_id},
    ).all()
    candidates: dict[str, list[tuple[int, int, int, UUID, str]]] = {}
    codes: set[str] = set()
    for order, (model_id, model_code, model_name, native_skus) in enumerate(existing):
        codes.add(model_code)
        key = price_list_model_key(model_name)
        # Named as the vehicle itself, not only through an alias ("Scorpio"
        # is SCORPIO CLASSIC only by alias).
        canonical = _squash(model_name) in (key, _squash(_MODEL_DISPLAY_BY_IDENTITY.get(key, "")))
        if not native_skus and not canonical:
            # Owns no price-list SKU and is not named as the vehicle: some
            # other import's model, never the vehicle's identity.
            continue
        candidates.setdefault(key, []).append((-int(native_skus), 0 if canonical else 1, order, model_id, model_code))
    by_key: dict[str, tuple[UUID, str]] = {}
    for key, found in candidates.items():
        found.sort()
        by_key[key] = (found[0][3], found[0][4])
        owners = [f for f in found if f[0] < 0]
        if key in wanted and len(owners) > 1 and conflicts is not None:
            conflicts.append(
                f"{len(owners)} models own price-list SKUs for one vehicle ({', '.join(f[4] for f in owners)}); "
                f"{found[0][4]} was used"
            )

    to_insert: list[dict[str, Any]] = []
    for key, name in wanted.items():
        if key in by_key:
            continue
        code = _slug_model(name)[:100]
        if code in codes:  # the code belongs to another vehicle (another identity)
            code = f"{code[:90]}_{hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:8]}"
        codes.add(code)
        to_insert.append({"oem_id": oem_id, "model_code": code, "model_name": name[:200], "key": key})
    inserted = _chunked_insert_returning(
        connection,
        table="product_models",
        columns=["oem_id", "model_code", "model_name"],
        rows=to_insert,
        returning=["model_code", "model_id"],
    )
    id_by_code = {r["model_code"]: r["model_id"] for r in inserted}
    for entry in to_insert:
        by_key[entry["key"]] = (id_by_code[entry["model_code"]], entry["model_code"])
    return {key: by_key[key] for key in wanted}


def _sku_name_part(sku_code: str) -> tuple[str, str]:
    """(variant name, registration suffix) from ``MODEL::VARIANT[::PRI|::COM]``."""
    name = sku_code.split("::", 1)[1] if "::" in sku_code else sku_code
    for suffix in ("::PRI", "::COM"):
        if name.endswith(suffix):
            return name[: -len(suffix)], suffix
    return name, ""


def _identity_variant_names(
    connection: Connection, *, oem_id: UUID, rows: list[PriceRow], model_ids: list[UUID],
) -> list[tuple[str, str | None]]:
    """For each row, the variant name its SKU is keyed on and the code of the
    SKU the model already has for that vehicle (None for a new vehicle).

    Same vehicle: the same name (spacing and case aside), or the same name
    once a release marker ("- New", "- Refresh", "- N") is taken off, when
    exactly one existing SKU of the model and registration basis matches,
    that SKU's own name is not in this file, and no other row of the file
    shares the stripped name. Price never decides. The existing code is
    reused as it stands, so a SKU moved onto another model (the Bolero
    split, migration 0151) keeps the code Journeys are pinned to; where a
    model holds two SKUs for one name, the older one wins."""
    existing = connection.execute(
        text(
            "SELECT sku_code, model_id FROM auditcore.product_skus WHERE oem_id = :o AND model_id = ANY(:m) "
            "ORDER BY created_at_utc, sku_code"
        ),
        {"o": oem_id, "m": list(set(model_ids))},
    ).all()
    names_by_group: dict[tuple[UUID, str], list[tuple[str, str]]] = {}
    for sku_code, model_id in existing:
        name, suffix = _sku_name_part(sku_code)
        names_by_group.setdefault((model_id, suffix), []).append((name, sku_code))

    def norm(value: str) -> str:
        return re.sub(r"\s+", " ", value.strip().upper())

    def suffix_of(row: PriceRow) -> str:
        return "" if row.registration_basis == "STANDARD" else f"::{row.registration_basis[:3]}"

    in_file: dict[tuple[UUID, str], set[str]] = {}
    cores_in_file: dict[tuple[UUID, str], dict[str, int]] = {}
    for row, model_id in zip(rows, model_ids):
        group = (model_id, suffix_of(row))
        in_file.setdefault(group, set()).add(norm(row.variant_name))
        cores = cores_in_file.setdefault(group, {})
        core = variant_core(row.variant_name)
        cores[core] = cores.get(core, 0) + 1

    out: list[tuple[str, str | None]] = []
    for row, model_id in zip(rows, model_ids):
        group = (model_id, suffix_of(row))
        known = names_by_group.get(group, [])
        exact = [k for k in known if k[0] == row.variant_name] or [k for k in known if norm(k[0]) == norm(row.variant_name)]
        if exact:
            out.append(exact[0])
            continue
        core = variant_core(row.variant_name)
        same = [k for k in known if variant_core(k[0]) == core and norm(k[0]) not in in_file[group]]
        if len({k[0] for k in same}) == 1 and cores_in_file[group][core] == 1:
            out.append(same[0])
        else:
            out.append((row.variant_name, None))
    return out


# A category from a dealer sheet only says electric or not: it never
# replaces the consolidated book's PV / CV / BEV, and nothing replaces a
# known value with UNSPECIFIED or blank (2026-10-20 product owner rules).
_WEAK_CATEGORIES = frozenset({"UNSPECIFIED", "ICE"})


def _merge_attributes(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current or {})
    for key, value in incoming.items():
        if value in (None, ""):
            continue
        if key == "category" and value in _WEAK_CATEGORIES and merged.get("category") not in (None, "", "UNSPECIFIED"):
            continue
        merged[key] = value
    return merged


def _name_fits_code(display_name: str, variant_code: str) -> bool:
    """Whether a variant's display name is the one its code was made from."""
    base = re.sub(r"\s*\((?:Private|Commercial)\)\s*$", "", display_name or "")
    code = re.sub(r"__(?:PRI|COM)$", "", variant_code or "")
    return _variant_slug(base)[:110] == code or _slug_model(base)[:110] == code and "+" not in base


def _bulk_ensure_variants(
    connection: Connection, *, entries: list[tuple[UUID, str, str, dict[str, Any]]]
) -> dict[tuple[UUID, str], UUID]:
    """(model_id, variant_code) -> variant_id for every ``(model_id, code, display_name,
    attrs)`` entry. One SELECT across every model involved, one batched INSERT for new
    variants, and one UPDATE per already-existing one refreshing its attributes.

    The UPDATE matters: re-uploading the same OEM masters file (a genuinely routine
    admin action, not a one-off) must actually refresh a variant's attributes when the
    parser starts extracting something it didn't before (``trim`` was once missing).
    It refreshes what the file knows and never blanks or downgrades what an earlier,
    richer file recorded: a blank never overwrites, and a dealer sheet's ICE /
    UNSPECIFIED category never replaces PV / CV / BEV (2026-10-20: the EV sheets
    turned every BE 6 / XEV variant from BEV to UNSPECIFIED).
    """
    model_ids = list({model_id for model_id, *_ in entries})
    if not model_ids:
        return {}

    existing = connection.execute(
        text(
            "SELECT model_id, variant_code, variant_id, attributes, variant_name FROM auditcore.product_variants "
            "WHERE model_id = ANY(:model_ids)"
        ),
        {"model_ids": model_ids},
    ).all()
    id_by_key: dict[tuple[UUID, str], UUID] = {(row[0], row[1]): row[2] for row in existing}
    attrs_by_key: dict[tuple[UUID, str], dict[str, Any]] = {(row[0], row[1]): dict(row[3] or {}) for row in existing}
    name_by_key: dict[tuple[UUID, str], str] = {(row[0], row[1]): row[4] for row in existing}

    to_insert: dict[tuple[UUID, str], dict[str, Any]] = {}
    to_update: dict[tuple[UUID, str], dict[str, Any]] = {}
    for model_id, code, display_name, attrs in entries:
        key = (model_id, code)
        clean = {k: v for k, v in attrs.items() if v not in (None, "")}
        row = {
            "model_id": model_id,
            "variant_code": code,
            "variant_name": display_name,
            "fuel_powertrain": attrs.get("fuel"),
            "transmission": attrs.get("transmission"),
            "body_type": attrs.get("bodyType"),
            "attributes": clean,
        }
        if key in id_by_key:
            to_update.setdefault(key, row)
        elif key not in to_insert:
            to_insert[key] = row

    inserted = _chunked_insert_returning(
        connection,
        table="product_variants",
        columns=[
            "model_id", "variant_code", "variant_name", "fuel_powertrain",
            "transmission", "body_type", "attributes",
        ],
        rows=[{**r, "attributes": json.dumps(r["attributes"])} for r in to_insert.values()],
        returning=["model_id", "variant_code", "variant_id"],
        jsonb_columns=frozenset({"attributes"}),
    )
    for row in inserted:
        id_by_key[(row["model_id"], row["variant_code"])] = row["variant_id"]

    for key, row in to_update.items():
        merged = _merge_attributes(attrs_by_key.get(key, {}), row["attributes"])
        # A name that no longer fits its code is the old "+"-stripping at
        # work (2026-10-20): the THREE variant row was named "THREE+ ..."
        # because a THREE+ row reached it first. It takes this row's name.
        current = name_by_key.get(key) or ""
        name = current if _name_fits_code(current, key[1]) else row["variant_name"]
        connection.execute(
            text(
                """
                UPDATE auditcore.product_variants
                SET variant_name = :name,
                    fuel_powertrain = COALESCE(:fuel_powertrain, fuel_powertrain),
                    transmission = COALESCE(:transmission, transmission),
                    body_type = COALESCE(:body_type, body_type),
                    attributes = CAST(:attributes AS jsonb)
                WHERE variant_id = :variant_id
                """
            ),
            {**row, "attributes": json.dumps(merged), "variant_id": id_by_key[key], "name": name},
        )

    return id_by_key


def _bulk_ensure_skus(
    connection: Connection, *, oem_id: UUID, entries: list[tuple[str, UUID, UUID, str | None]]
) -> dict[str, UUID]:
    """sku_code -> product_sku_id for every ``(sku_code, model_id, variant_id,
    legacy_variant_code)`` entry, raising on the same "already maps to a
    different model/variant" conflict the old per-row ``_ensure_sku`` raised —
    checked both against the OEM's existing catalogue and against the rest of
    this same upload.

    One repair is made instead of raising: a SKU that sits on the variant
    whose code is this name's *legacy* code (the code before "+" was kept as
    PLUS, 2026-10-20) is moved to the right variant. THREE+ SKUs were filed
    under the THREE variant that way."""
    existing = connection.execute(
        text(
            "SELECT s.sku_code, s.product_sku_id, s.model_id, s.variant_id, v.variant_code "
            "FROM auditcore.product_skus s "
            "JOIN auditcore.product_variants v ON v.variant_id = s.variant_id "
            "WHERE s.oem_id = :oem_id"
        ),
        {"oem_id": oem_id},
    ).all()
    by_code: dict[str, tuple[UUID, UUID, UUID, str]] = {row[0]: (row[1], row[2], row[3], row[4]) for row in existing}

    id_by_code: dict[str, UUID] = {}
    to_insert: dict[str, dict[str, Any]] = {}
    for sku_code, model_id, variant_id, legacy_variant_code in entries:
        found = by_code.get(sku_code)
        if found is not None:
            existing_sku_id, existing_model_id, existing_variant_id, existing_variant_code = found
            if existing_model_id == model_id and existing_variant_id != variant_id and (
                legacy_variant_code is not None and existing_variant_code == legacy_variant_code
            ):
                connection.execute(
                    text("UPDATE auditcore.product_skus SET variant_id = :v WHERE product_sku_id = :s"),
                    {"v": variant_id, "s": existing_sku_id},
                )
                by_code[sku_code] = (existing_sku_id, model_id, variant_id, "")
            elif existing_model_id != model_id or existing_variant_id != variant_id:
                raise ValidationError(
                    detail=f"SKU '{sku_code}' already maps to a different model/variant."
                )
            id_by_code[sku_code] = existing_sku_id
            continue
        queued = to_insert.get(sku_code)
        if queued is not None:
            if queued["model_id"] != model_id or queued["variant_id"] != variant_id:
                raise ValidationError(
                    detail=f"SKU '{sku_code}' already maps to a different model/variant."
                )
            continue
        to_insert[sku_code] = {
            "oem_id": oem_id,
            "model_id": model_id,
            "variant_id": variant_id,
            "sku_code": sku_code,
            "attributes": json.dumps({"source": "OEM_NATIVE_PRICE_LIST"}),
        }

    inserted = _chunked_insert_returning(
        connection,
        table="product_skus",
        columns=["oem_id", "model_id", "variant_id", "sku_code", "attributes"],
        rows=list(to_insert.values()),
        returning=["sku_code", "product_sku_id"],
        jsonb_columns=frozenset({"attributes"}),
    )
    for row in inserted:
        id_by_code[row["sku_code"]] = row["product_sku_id"]

    return id_by_code


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


def _standing_items(
    connection: Connection, *, tenant_id: str, price_list_id: UUID, effective_from: date,
) -> tuple[UUID | None, dict[tuple[UUID, str], dict[str, Any]]]:
    """The version standing on the new effective date (the latest published
    or retired one on or before it) and its items by (sku, component), each
    with its SKU's model."""
    version_id = connection.execute(
        text(
            """
            SELECT price_list_version_id FROM auditcore.price_list_versions
            WHERE tenant_id = :tenant_id AND price_list_id = :price_list_id
              AND lifecycle_status IN ('PUBLISHED', 'RETIRED') AND effective_from <= :effective_from
            ORDER BY effective_from DESC, version_no DESC LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "price_list_id": price_list_id, "effective_from": effective_from},
    ).scalar_one_or_none()
    if version_id is None:
        return None, {}
    rows = connection.execute(
        text(
            """
            SELECT i.product_sku_id, i.component_key, i.standard_amount, i.tax_inclusive, i.metadata,
                   i.price_since, s.model_id
            FROM auditcore.price_list_items i
            JOIN auditcore.product_skus s ON s.product_sku_id = i.product_sku_id
            WHERE i.tenant_id = :tenant_id AND i.price_list_version_id = :version_id
            """
        ),
        {"tenant_id": tenant_id, "version_id": version_id},
    ).mappings().all()
    return version_id, {(r["product_sku_id"], str(r["component_key"])): dict(r) for r in rows}


def _is_complete_book(parsed: ParseResult) -> bool:
    """The OEM's consolidated list is the whole book for its date; the
    dealer's per-model sheets (the EV sheets) price only their own models."""
    return parsed.meta.get("layout") == "OEM_CONSOLIDATED"


def ingest_price_list(
    connection: Connection,
    *,
    tenant_id: str,
    oem_id: UUID,
    effective_from: date,
    parsed: ParseResult,
    actor_id: str,
    stats: dict[str, Any] | None = None,
) -> tuple[UUID, dict[str, UUID]]:
    """Load a parsed price list; returns (price_list_version_id, {sku_code: sku_id}).

    The OEM's consolidated list is a complete book (2026-10-20 product owner
    rules): the new version holds exactly its rows, and a SKU it does not
    carry is withdrawn from that date, never carried forward at an older
    month's price. A dealer per-model sheet prices only its own models, so
    it is merged over the version standing on the effective date: SKUs of
    the models it does not cover are carried forward at their standing
    price; SKUs of a model it covers but no longer lists are dropped. Each
    row keeps ``price_since``: the effective date when its figure changed,
    else the date it already held. ``stats`` (when given) receives the
    counts: uploaded, changed, unchanged, carriedForward, withdrawn."""
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

    rows = parsed.price_rows

    # 1) models — one identity per vehicle, whatever this month's spelling.
    conflicts: list[str] = []
    model_by_key = _bulk_ensure_model_identities(connection, oem_id=oem_id, rows=rows, conflicts=conflicts)
    row_models = [model_by_key[price_list_model_key(row.model_name, row.variant_name)] for row in rows]
    model_ids = [model_id for model_id, _ in row_models]
    _set_model_segments(connection, rows=rows, model_ids=model_ids)

    # 2) variants — keyed on the name the model already has for the vehicle.
    identities = _identity_variant_names(connection, oem_id=oem_id, rows=rows, model_ids=model_ids)
    names = [name for name, _ in identities]

    def _basis_suffix(row: PriceRow, sep: str) -> str:
        return "" if row.registration_basis == "STANDARD" else f"{sep}{row.registration_basis[:3]}"

    def _variant_code(row: PriceRow, name: str) -> str:
        return (_variant_slug(name)[:110] + _basis_suffix(row, "__"))[:120]

    def _legacy_variant_code(row: PriceRow, name: str) -> str:
        return (_slug_model(name)[:110] + _basis_suffix(row, "__"))[:120]

    def _variant_display(row: PriceRow, name: str) -> str:
        return (
            name.strip()
            if row.registration_basis == "STANDARD"
            else f"{name.strip()} ({row.registration_basis.title()})"
        )[:240]

    variant_entries = [
        (
            model_id,
            _variant_code(row, name),
            _variant_display(row, name),
            {
                "trim": row.trim,
                "fuel": row.fuel,
                "transmission": row.transmission,
                "drive": row.drive,
                "seater": row.seater,
                "category": row.category,
                "sourceSheet": row.source_sheet,
            },
        )
        for row, model_id, name in zip(rows, model_ids, names)
    ]
    variant_id_by_key = _bulk_ensure_variants(connection, entries=variant_entries)

    # 3) SKUs — the stable model code plus the vehicle's variant name.
    sku_codes = [
        existing_code or f"{model_code}::{name}{_basis_suffix(row, '::')}"[:160]
        for row, (_, model_code), (name, existing_code) in zip(rows, row_models, identities)
    ]
    sku_entries = [
        (
            sku_code,
            model_id,
            variant_id_by_key[(model_id, _variant_code(row, name))],
            _legacy_variant_code(row, name) if _legacy_variant_code(row, name) != _variant_code(row, name) else None,
        )
        for row, model_id, name, sku_code in zip(rows, model_ids, names, sku_codes)
    ]
    sku_id_by_code = _bulk_ensure_skus(connection, oem_id=oem_id, entries=sku_entries)

    # 4) price_list_items — every component of every uploaded row, each
    # dated from when its figure last changed; then (dealer sheets only)
    # every SKU of a model the sheet does not cover, carried forward.
    standing_version, standing = _standing_items(
        connection, tenant_id=tenant_id, price_list_id=price_list_id, effective_from=effective_from,
    )
    uploaded_skus = {sku_id_by_code[code] for code in sku_codes}
    file_models = set(model_ids)
    complete = _is_complete_book(parsed)
    changed_skus: set[UUID] = set()
    item_rows: list[dict[str, Any]] = []
    for row, sku_code in zip(rows, sku_codes):
        sku_id = sku_id_by_code[sku_code]
        for component_key, amount in row.components.items():
            before = standing.get((sku_id, component_key))
            unchanged = before is not None and Decimal(str(before["standard_amount"])) == amount
            if not unchanged:
                changed_skus.add(sku_id)
            metadata: dict[str, Any] = {
                "source": "OEM_NATIVE_PRICE_LIST",
                "layout": parsed.meta.get("layout"),
                "category": row.category,
                "onRoadIndividual": str(row.onroad_individual),
                "onRoadCorporate": str(row.onroad_corporate),
                # How this file printed the vehicle: a respelt model or a
                # changed release marker keeps the SKU it already had.
                "printedAs": {"model": row.model_name, "variant": row.variant_name},
            }
            item_rows.append({
                "tenant_id": tenant_id,
                "price_list_version_id": version_id,
                "product_sku_id": sku_id,
                "component_key": component_key,
                "standard_amount": amount,
                "price_since": (before["price_since"] or effective_from) if unchanged else effective_from,
                "metadata": json.dumps(metadata),
            })
        # a component the standing version priced but this file no longer
        # carries for the SKU is dropped for that SKU: the file is the SKU's
        # whole line
    carried_skus: set[UUID] = set()
    withdrawn_skus: set[UUID] = set()
    for (sku_id, component_key), before in standing.items():
        if sku_id in uploaded_skus:
            continue
        if complete or before["model_id"] in file_models:
            withdrawn_skus.add(sku_id)
            continue
        carried_skus.add(sku_id)
        item_rows.append({
            "tenant_id": tenant_id,
            "price_list_version_id": version_id,
            "product_sku_id": sku_id,
            "component_key": component_key,
            "standard_amount": before["standard_amount"],
            "price_since": before["price_since"],
            "metadata": json.dumps({**(before["metadata"] or {}), "carriedForwardFrom": str(standing_version)}),
        })
    _chunked_insert_returning(
        connection,
        table="price_list_items",
        columns=[
            "tenant_id", "price_list_version_id", "product_sku_id",
            "component_key", "standard_amount", "price_since", "metadata",
        ],
        rows=item_rows,
        returning=[],
        jsonb_columns=frozenset({"metadata"}),
    )
    if stats is not None:
        stats.update({
            "uploaded": len(uploaded_skus),
            "changed": len(changed_skus),
            "unchanged": len(uploaded_skus - changed_skus),
            "carriedForward": len(carried_skus),
            "withdrawn": len(withdrawn_skus),
            "completeBook": complete,
            "standingVersionId": str(standing_version) if standing_version else None,
        })
        if conflicts:
            stats["modelIdentityConflicts"] = conflicts

    publish_price_list_version(
        connection, tenant_id=tenant_id, price_list_version_id=version_id, actor_id=actor_id
    )
    rederived = _carry_into_later_versions(
        connection, tenant_id=tenant_id, price_list_id=price_list_id, effective_from=effective_from, actor_id=actor_id,
    )
    if stats is not None:
        stats["laterVersionsRederived"] = len(rederived)
    # Journeys waiting on a book for their date, or on their SKU being in
    # one, are re-checked now rather than on their next event (2026-10-20).
    from audit_core.uc03_sku_candidates import sweep_price_master_tasks

    swept = sweep_price_master_tasks(connection, tenant_id=tenant_id)
    if stats is not None:
        stats["journeysRechecked"] = swept
    return version_id, sku_id_by_code


def _carry_into_later_versions(
    connection: Connection, *, tenant_id: str, price_list_id: UUID, effective_from: date, actor_id: str,
) -> list[UUID]:
    """Uploads arrive in any order (the EV sheet dated the 1st after the
    consolidated list dated the 3rd): a version published for a LATER date
    was built before this file existed. A later complete book (the OEM's
    consolidated list) is left as it is: what it does not list is not sold
    from its date (2026-10-20 product owner rules). A later dealer-sheet
    version priced only its own models, so it gains the SKUs of the models
    it does not cover from the version standing just before its date: a
    re-derived version is published on that same date with its own rows
    plus those SKUs carried forward. The old version stays (published
    versions are immutable) and is superseded by the higher version number.
    Returns the re-derived version ids."""
    later_dates = connection.execute(
        text(
            """
            SELECT DISTINCT effective_from FROM auditcore.price_list_versions
            WHERE tenant_id = :tenant_id AND price_list_id = :price_list_id
              AND lifecycle_status IN ('PUBLISHED', 'RETIRED') AND effective_from > :effective_from
            ORDER BY effective_from
            """
        ),
        {"tenant_id": tenant_id, "price_list_id": price_list_id, "effective_from": effective_from},
    ).scalars().all()
    rederived: list[UUID] = []
    for later_on in later_dates:
        later_version, own = _standing_items(
            connection, tenant_id=tenant_id, price_list_id=price_list_id, effective_from=later_on,
        )
        before_version, before = _standing_items(
            connection, tenant_id=tenant_id, price_list_id=price_list_id,
            effective_from=later_on - timedelta(days=1),
        )
        if later_version is None or before_version is None:
            continue
        uploaded = [row for row in own.values() if "carriedForwardFrom" not in (row["metadata"] or {})]
        if any((row["metadata"] or {}).get("layout") == "OEM_CONSOLIDATED" for row in uploaded):
            continue
        covered_models = {row["model_id"] for row in uploaded}
        priced = {sku for sku, _ in own}
        missing = [
            (key, row) for key, row in before.items()
            if key[0] not in priced and row["model_id"] not in covered_models
        ]
        if not missing:
            continue
        new_version = create_price_list_version(
            connection,
            tenant_id=tenant_id,
            price_list_id=price_list_id,
            version_no=_next_version_no(
                connection, "price_list_versions", "price_list_id", tenant_id, price_list_id
            ),
            effective_from=later_on,
            actor_id=actor_id,
        )
        item_rows = [
            {
                "tenant_id": tenant_id,
                "price_list_version_id": new_version,
                "product_sku_id": key[0],
                "component_key": key[1],
                "standard_amount": row["standard_amount"],
                "price_since": row["price_since"],
                "metadata": json.dumps(
                    {**(row["metadata"] or {}), "rederivedFrom": str(later_version)} if key not in dict(missing)
                    else {**(row["metadata"] or {}), "carriedForwardFrom": str(before_version)}
                ),
            }
            for key, row in [*own.items(), *missing]
        ]
        _chunked_insert_returning(
            connection,
            table="price_list_items",
            columns=[
                "tenant_id", "price_list_version_id", "product_sku_id",
                "component_key", "standard_amount", "price_since", "metadata",
            ],
            rows=item_rows,
            returning=[],
            jsonb_columns=frozenset({"metadata"}),
        )
        publish_price_list_version(
            connection, tenant_id=tenant_id, price_list_version_id=new_version, actor_id=actor_id
        )
        rederived.append(new_version)
    return rederived


# ── looking a vehicle up in the book in force (2026-10-20 product owner rules) ──
# A spelling that, once the old model has left the book, names its successor:
# October's book has no old Scorpio N, and a document dated October that
# says "SCORPIO N" means the Scorpio N on sale then. Tried after the model
# the words name, never instead of it.
_SUCCESSOR_IDENTITY: dict[str, str] = {"SCORPION": "NEWSCORPION"}


def model_identity_keys(model_text: str | None, variant_text: str | None = None) -> list[str]:
    """THE model-identity rule for a document's wording (for the SKU
    resolver): the identities a model text can name, in the order to try
    them against the book in force on the pricing date. A book row matches
    when ``price_list_model_key(row.model_name)`` is in this list. The first
    key is the model the words name under any month's spelling (squashed,
    aliased, the Bolero family split by the variant's series letter); a
    second one is the successor that the same words mean once the old
    model has left the book (SCORPIO N -> the new Scorpio N). Filter the
    book in force by the first key that has rows; never by price."""
    key = price_list_model_key(model_text, variant_text)
    return [key] + ([_SUCCESSOR_IDENTITY[key]] if key in _SUCCESSOR_IDENTITY else [])


def find_book_sku(
    connection: Connection,
    *,
    tenant_id: str,
    model_text: str,
    variant_text: str | None,
    effective_on: date,
    registration_basis: str | None = None,
) -> dict[str, Any]:
    """The SKU the words name in the price master in force on the date.

    Words only (R3): the model by its identity (any month's spelling), then
    the variant by its exact name (spacing and case aside), else by its name
    without a release marker when that leaves exactly one SKU. Price is
    never used. ``registration_basis`` (PRIVATE / COMMERCIAL) narrows the
    books that print both. Returns {"onDate", "plan", "sku", "candidates",
    "reason"}: ``sku`` None with the shortlist when the words do not name
    exactly one, and ``plan`` None with the reason when no book is in force.
    """
    from audit_core.price_lists import find_effective_price_plan, no_price_master_detail

    out: dict[str, Any] = {"onDate": effective_on, "plan": None, "sku": None, "candidates": [], "reason": None}
    plan = find_effective_price_plan(connection, tenant_id=tenant_id, effective_on=effective_on)
    if plan is None:
        out["reason"] = no_price_master_detail(effective_on)
        return out
    out["plan"] = plan
    rows = connection.execute(
        text(
            """
            SELECT s.product_sku_id, s.sku_code, pm.model_name, pv.variant_name,
                   pv.attributes ->> 'category' AS category, pv.fuel_powertrain,
                   max(i.standard_amount) FILTER (WHERE i.component_key = 'EX_SHOWROOM') AS ex_showroom,
                   bool_or(i.metadata ? 'carriedReason') AS carried
            FROM auditcore.price_list_items i
            JOIN auditcore.product_skus s ON s.product_sku_id = i.product_sku_id
            JOIN auditcore.product_models pm ON pm.model_id = s.model_id
            JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
            WHERE i.tenant_id = :tenant_id AND i.price_list_version_id = :version_id
            GROUP BY s.product_sku_id, s.sku_code, pm.model_name, pv.variant_name, pv.attributes, pv.fuel_powertrain
            """
        ),
        {"tenant_id": tenant_id, "version_id": plan["price_list_version_id"]},
    ).mappings().all()
    suffix = {"PRIVATE": "::PRI", "COMMERCIAL": "::COM"}.get((registration_basis or "").upper())

    def norm(value: str | None) -> str:
        return re.sub(r"\s+", " ", (value or "").strip().upper())

    keys = model_identity_keys(model_text, variant_text)
    every = [dict(r) for r in rows if price_list_model_key(r["model_name"]) in keys]
    if suffix:
        every = [r for r in every if _sku_name_part(r["sku_code"])[1] in ("", suffix)]
    wanted = norm(variant_text)

    def naming(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        named = [r for r in pool if norm(_sku_name_part(r["sku_code"])[0]) == wanted]
        if not named and wanted:
            named = [r for r in pool if variant_core(_sku_name_part(r["sku_code"])[0]) == variant_core(variant_text)]
        return named

    # The rows the list prints first; a carried row (the previous month's
    # price for a variant the list no longer prints, 2026-11-01) only when
    # the words name none of them (the same rule as the SKU resolver).
    pool = [r for r in every if not r["carried"]]
    named = naming(pool)
    if not named:
        pool = every
        named = naming([r for r in every if r["carried"]])
    out["candidates"] = named or pool
    if len(named) == 1:
        out["sku"] = named[0]
    elif not pool:
        out["reason"] = f"No model '{model_text}' in the price master in force on {effective_on.isoformat()}"
    else:
        out["reason"] = "The words do not name exactly one SKU"
    return out


# ── discount ingestion ──────────────────────────────────────────────────────────
def _model_catalogue(connection: Connection, oem_id: UUID) -> list[tuple[str, UUID, list[tuple[str, UUID]]]]:
    """Every model the OEM carries, as (name, model_id, [(variant name, variant_id)]).

    A model is keyed on its slug (``_slug_model``), which preserves spacing --
    'XUV 3XO' slugs to XUV_3XO and 'XUV3XO' to XUV3XO -- so a model the OEM
    has respelt genuinely exists twice, and a scheme has to be able to find
    both. Nothing is collapsed here. Variant names are kept as printed: the
    variant groups below read them ("2.0L", "CAMPER", "REVX").
    """
    rows = connection.execute(
        text(
            """
            SELECT m.model_id, m.model_name, v.variant_id, v.variant_name
            FROM auditcore.product_models m
            LEFT JOIN auditcore.product_variants v ON v.model_id = m.model_id
            WHERE m.oem_id = :oem_id
            ORDER BY m.model_name, m.model_id, v.variant_name, v.variant_id
            """
        ),
        {"oem_id": oem_id},
    ).mappings().all()
    out: dict[UUID, tuple[str, UUID, list[tuple[str, UUID]]]] = {}
    for row in rows:
        entry = out.setdefault(row["model_id"], (row["model_name"], row["model_id"], []))
        if row["variant_id"] is not None:
            entry[2].append((str(row["variant_name"]), row["variant_id"]))
    return list(out.values())


# 2026-10-20 product owner rules: an alias must never merge two models' offers.
# September's price list sells Bolero, Bolero Neo and Bolero Neo Plus as one
# model ("THE BOSS- BOLERO+NEO"), Camper and Pick-Up as one ("PICK UP"), and
# the REVX inside XUV 3XO. A scheme naming one of them reaches only that
# group's variants, never the whole combined model.
_VARIANT_GROUP_TESTS: dict[str, Any] = {
    "BOLERO": lambda name: re.match(r"^B\d", name.strip().upper()) is not None,
    "BOLERO_NEO": lambda name: re.match(r"^N\d", name.strip().upper()) is not None,
    "BOLERO_NEO_PLUS": lambda name: re.match(r"^P\d", name.strip().upper()) is not None,
    "CAMPER": lambda name: "CAMPER" in name.upper(),
    "PICKUP": lambda name: "CAMPER" not in name.upper(),
    "REVX": lambda name: "REVX" in name.upper(),
    "MAXX_HD_2_0": lambda name: re.search(r"\b2\.0", name) is not None,
}


def _variant_group_key(value: str | None) -> str:
    """A model wording squashed for the variant-group tests, with a trailing
    "+" read as PLUS ("BOLERO NEO+" is the Neo Plus, never the Neo). Not the
    model identity: that is ``price_list_model_key`` (#468), whose squash
    drops the "+"; a wording goes through ``_plus_spelled`` before it."""
    return _squash(_plus_spelled(value))


def _plus_spelled(value: str | None) -> str:
    """"BOLERO NEO+" -> "BOLERO NEO PLUS"; an inner "+" ("BOLERO+NEO", the
    combined September model) is left as printed."""
    return re.sub(r"(\w)\s*\+(?=\s|$|[)|/,])", r"\1 PLUS", value or "")


def _variant_group(part: _ModelPart, model_name: str) -> str | None:
    """The group of a combined model's variants the wording names, or None
    for the whole model (2026-10-20 product owner rules)."""
    identity = price_list_model_key(model_name)
    words = _variant_group_key(part.raw)
    if identity == "THEBOSSBOLERONEO":
        # Only a database the Bolero split (0151) has not reached still
        # carries variants on the combined model.
        if "BOSS" in words:
            return None
        if "NEOPLUS" in words:
            return "BOLERO_NEO_PLUS"
        if "NEO" in words:
            return "BOLERO_NEO"
        if "BOLERO" in words:
            return "BOLERO"
    if identity == "PICKUP":
        upper = part.raw.upper()
        if "CAMPER" in upper:
            return "CAMPER"
        if re.search(r"PICK\s*-?\s*UP|PIK\s*-?\s*UP", upper):
            return "PICKUP"
    if identity.startswith("XUV3XO") and not is_ev_model_text(model_name) and "REVX" in part.base.upper():
        return "REVX"
    if identity == "MAXXHD" and re.search(r"\b2\.0", part.base):
        return "MAXX_HD_2_0"
    return None


def grid_variant_group(model_alias: str | None, model_name: str) -> str | None:
    """The variant group a grid line's own wording names ("Camper", "MAXX HD
    2.0"), for a line stored before the group was recorded on it."""
    base, qualifiers = parse_model_qualifiers(model_alias or "")
    return _variant_group(_ModelPart(raw=model_alias or "", base=base, canonical=None, qualifiers=qualifiers),
                          model_name)


def variant_in_group(group: str | None, variant_name: str) -> bool:
    """Whether a variant belongs to a scheme's variant group (None: all)."""
    test = _VARIANT_GROUP_TESTS.get(group or "")
    return True if test is None else bool(test(variant_name))


@dataclass
class _Target:
    model_id: UUID
    model_name: str
    variants: list[tuple[str, UUID]]
    group: str | None
    qualifiers: dict[str, Any]

    @property
    def code_name(self) -> str:
        return _slug_model(self.model_name) + (f"_{self.group}" if self.group else "")


def _scheme_identity_keys(wording: str | None) -> list[str]:
    """The model identities a scheme's wording can name, most literal first.

    #468 owns model identity (``price_list_model_key`` /
    ``model_identity_keys``); a bulletin is read through it with one
    difference of order: the squashed wording comes before the spelling
    alias, because "SCORPIO-N" in the September bulletin is September's
    Scorpio N while the alias reads it as October's new one. The book in
    force on the scheme's date then says which of them the words mean."""
    spelled = _plus_spelled(wording)
    squashed = _squash(spelled)
    keys = [_MODEL_IDENTITY_BY_KEY.get(squashed, squashed), *model_identity_keys(spelled)]
    for key in list(keys):
        if key in _SUCCESSOR_IDENTITY:
            keys.append(_SUCCESSOR_IDENTITY[key])
    if keys and keys[0] == "THEBOSSBOLERONEO":
        # The combined September model is the three models since 0151.
        keys.extend(["BOLERO", "BOLERONEO", "BOLERONEOPLUS"])
    return [k for i, k in enumerate(keys) if k and k not in keys[:i]]


_REFRESH_RE = re.compile(r"\bREFRESH\b", re.IGNORECASE)


def _model_targets(
    models: list[tuple[str, UUID, list[tuple[str, UUID]]]],
    alias_map: dict[str, str],
    model_alias: str,
    extra_aliases: dict[str, str] | None = None,
    *,
    in_book: set[UUID] | None = None,
    named_variants: bool = False,
) -> tuple[list[_Target], list[str], list[str], list[str]]:
    """The loaded models a scheme's model cell names: (targets, matched model
    names, parts nothing knows, parts known by name but not loaded).

    2026-10-20 product owner rules and the review of PR 472 (B4): a part
    names the models whose identity (#468's ``price_list_model_key``) is one
    its wording can mean (``_scheme_identity_keys``), tried on the part's own
    words first and on its alias's canonical name only when they reach
    nothing. ``in_book`` (the models the book in force on the scheme's date
    prices) picks which identity a model-wide row means: the first one on
    sale then. A row naming variants keeps every identity, and its variant
    words decide. "including Refresh" adds the successor (the new Scorpio N
    beside the Scorpio N). The scheme version is dated, so a row reaching a
    model no longer sold only ever answers for dates that model is priced on.
    """
    aliases = {**alias_map, **{_norm(k): v for k, v in (extra_aliases or {}).items()}}
    by_identity: dict[str, list[tuple[str, UUID, list[tuple[str, UUID]]]]] = {}
    for entry in models:
        by_identity.setdefault(price_list_model_key(entry[0]), []).append(entry)
    targets: list[_Target] = []
    matched: list[str] = []
    unknown: list[str] = []
    not_loaded: list[str] = []
    seen: set[tuple[UUID, str | None]] = set()
    for part in _model_parts(aliases, model_alias):
        refresh = any(_REFRESH_RE.search(str(note)) for note in part.qualifiers.get("includes") or [])
        hits: list[tuple[str, UUID, list[tuple[str, UUID]]]] = []
        for wording in (part.base, part.canonical):
            if not wording:
                continue
            keys = [k for k in _scheme_identity_keys(wording) if k in by_identity]
            if not keys:
                continue
            if not (named_variants or refresh):
                on_sale = [k for k in keys if in_book and any(m[1] in in_book for m in by_identity[k])]
                first = on_sale[0] if on_sale else keys[0]
                keys = [first] + [k for k in keys[1:] if first == "THEBOSSBOLERONEO"
                                  and k in ("BOLERO", "BOLERONEO", "BOLERONEOPLUS")]
            hits = [entry for k in keys for entry in by_identity[k]]
            break
        if not hits:
            # Unreadable wording -- a leftover bracket, or a loaded model's
            # name followed by words nothing understood ("XUV 3XO EDGE & REVX
            # ...") -- is reported as unknown (the caller blocks the upload).
            # A clean name the price list does not carry (XUV300, Marazzo)
            # is reported as not loaded: there is nothing to apply it to.
            squashed = _squash(part.base)
            unreadable = (
                not squashed or "(" in part.base or ")" in part.base
                or any(len(_squash(name)) >= 4 and squashed.startswith(_squash(name)) for name, _, _ in models)
            )
            (unknown if unreadable and not part.canonical else not_loaded).append(part.raw)
            continue
        for name, model_id, variants in hits:
            if price_list_model_key(name) == "THEBOSSBOLERONEO" and not variants:
                continue  # the combined model, emptied by the Bolero split
            group = _variant_group(part, name)
            if (model_id, group) in seen:
                continue
            seen.add((model_id, group))
            targets.append(_Target(model_id=model_id, model_name=name, variants=variants, group=group,
                                   qualifiers=part.qualifiers))
            if name not in matched:
                matched.append(name)
    return targets, matched, unknown, not_loaded


def _models_in_book(connection: Connection, *, tenant_id: str, effective_on: date) -> set[UUID] | None:
    """The models the price master in force on a date sells (None: no book).
    A variant carried at the previous month's price (``carriedReason``; a
    dealer sheet's ``carriedForwardFrom`` rows are the book itself, on sale,
    2026-11-01) is priced, not on sale: it never makes its model one a
    month's scheme or grid wording names (the October "SCORPIO-N" stays the
    new Scorpio N with the old series carried)."""
    from audit_core.price_lists import find_effective_price_plan

    plan = find_effective_price_plan(connection, tenant_id=tenant_id, effective_on=effective_on)
    if plan is None:
        return None
    return {
        row[0] for row in connection.execute(
            text(
                """
                SELECT DISTINCT s.model_id FROM auditcore.price_list_items i
                JOIN auditcore.product_skus s ON s.product_sku_id = i.product_sku_id
                WHERE i.tenant_id = :t AND i.price_list_version_id = :v
                  AND NOT (i.metadata ? 'carriedReason')
                """
            ),
            {"t": tenant_id, "v": plan["price_list_version_id"]},
        ).all()
    }


def _books_in_use(connection: Connection, *, tenant_id: str, start: date, end: date | None = None) -> list[set[UUID]]:
    """The models of every book in force at some date in [start, end): the
    book standing on ``start`` and each later book that starts before
    ``end`` (None: no end). A master dated before the book it will mostly be
    used with (the October grid, dated 1 Oct, against the October book,
    dated 3 Oct) is used with each of them."""
    dates = [start, *connection.execute(
        text(
            """
            SELECT DISTINCT effective_from FROM auditcore.price_list_versions
            WHERE tenant_id = :t AND lifecycle_status IN ('PUBLISHED', 'RETIRED') AND effective_from > :s
              AND (CAST(:e AS date) IS NULL OR effective_from < :e)
            ORDER BY 1
            """
        ),
        {"t": tenant_id, "s": start, "e": end},
    ).scalars().all()]
    books: list[set[UUID]] = []
    for day in dates:
        in_book = _models_in_book(connection, tenant_id=tenant_id, effective_on=day)
        if in_book is not None and in_book not in books:
            books.append(in_book)
    return books


def _match_variants(
    variant_texts: list[str], variants: list[tuple[str, UUID]]
) -> list[tuple[str, list[UUID]]]:
    """Each named variant and the price-list variants it is, word for word,
    else by its distinguishing words (``variant_loose_key``).

    Matched on the name as two spellings share it (``variant_key``): without
    the "(Private)" / "(Commercial)" registration suffix, so "P10 BS6.2"
    reaches both registrations of that variant. Never a nearest guess."""
    by_key: dict[str, list[UUID]] = {}
    by_loose: dict[str, list[UUID]] = {}
    for name, variant_id in variants:
        by_key.setdefault(variant_key(name), []).append(variant_id)
        by_loose.setdefault(variant_loose_key(name), []).append(variant_id)
    # 2026-10-20 (review of PR 472, B1): a name that differs from the price
    # list's only by the emission norm, the "- E" / release markers, "(O)"
    # for OPT or "& Mocha Interiors" is the same variant.
    return [
        (raw, list(by_key.get(variant_key(raw)) or by_loose.get(variant_loose_key(raw)) or []))
        for raw in variant_texts
    ]


def _condition_code(qualifiers: dict[str, Any], config: dict[str, Any]) -> str:
    requirement = qualifiers.get("requiredOldVehicle") or config.get("requiredOldVehicle")
    if not requirement:
        return ""
    return "OLD_" + "_".join(str(v) for v in (requirement.get("brand"), requirement.get("model")) if v)


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
    effective_to: date | None = None,
) -> dict[str, Any]:
    alias_map = _alias_map(connection, oem_code)
    models = _model_catalogue(connection, oem_id)
    in_book = _models_in_book(connection, tenant_id=tenant_id, effective_on=effective_from)
    categories = _SCHEME_CATEGORY_BY_KIND[master_kind]
    summary: dict[str, Any] = {
        "published": 0, "tombstoned": 0, "unresolved": [], "unresolvedVariants": [], "warnings": [],
    }
    effective_to = _valid_to(parsed, effective_from, effective_to, summary)
    live_codes: set[str] = set()
    blocking: list[str] = []

    for drow in parsed.discount_rows:
        offers = any(amount > 0 for _, amount in drow.benefits)
        targets, matched_names, unknown, not_loaded = _model_targets(
            models, alias_map, drow.model_alias, in_book=in_book, named_variants=bool(drow.variant_texts),
        )
        # 2026-10-20 product owner rules (R4): an EV row reaches EV models
        # only, an ICE row ICE models only.
        category = drow.config.get("vehicleCategory")
        if category in ("EV", "ICE"):
            wrong = [t for t in targets if is_ev_model_text(t.model_name) != (category == "EV")]
            for target in wrong:
                summary["warnings"].append(
                    f"{drow.scheme_category} row {drow.row_no}: '{drow.model_alias}' is an {category} offer; "
                    f"not applied to {target.model_name}"
                )
            targets = [t for t in targets if t not in wrong]
            if wrong and not targets and offers:
                blocking.append(
                    f"{drow.scheme_category} row {drow.row_no}: an {category} offer named against "
                    f"'{drow.model_alias}', which is not an {category} model"
                )
                continue
            matched_names = [n for n in matched_names if any(t.model_name == n for t in targets)]
        if unknown and offers:
            # A row that names a model nothing resolves is never dropped
            # quietly: the upload stops until the wording is mapped.
            blocking.append(f"{drow.scheme_category} row {drow.row_no}: no model for {unknown} in '{drow.model_alias}'")
        elif unknown:
            summary["unresolved"].append(
                f"{drow.scheme_category} row {drow.row_no}: unresolved model(s) {unknown} in '{drow.model_alias}' "
                "(a row with no offer; nothing to apply)"
            )
        for part in not_loaded:
            summary["unresolved"].append(
                f"{drow.scheme_category} row {drow.row_no}: '{part}' is not in the loaded price list — not applied"
            )
        if not targets:
            if offers and not unknown and not not_loaded:
                blocking.append(f"{drow.scheme_category} row {drow.row_no}: '{drow.model_alias}' reaches no model")
            continue
        # A model matched under a name other than the one the bulletin used
        # has been renamed between months: say so rather than leave it to be
        # noticed when the offer fails to appear against a deal.
        renamed = [n for n in matched_names if _squash(n) != _squash(drow.model_alias)]
        if renamed and len(targets) == 1:
            summary["warnings"].append(
                f"{drow.scheme_category} row {drow.row_no}: '{drow.model_alias}' "
                f"matched the loaded model '{renamed[0]}'"
            )

        # Scheme identity: category, section, the models (with their variant
        # group), scheme type, the named variants and -- 2026-10-20 -- the
        # old-vehicle condition, so the 50,000 exchange and the 70,000
        # "against old Maruti" exchange are two schemes, not two versions.
        condition = next((c for c in (_condition_code(t.qualifiers, drow.config) for t in targets) if c), "")
        code = _scheme_code(
            "MAH",
            drow.scheme_category,
            drow.config.get("section", ""),
            "_".join(t.code_name for t in targets),
            drow.config.get("schemeType", ""),
            "-".join(sorted(v.upper() for v in drow.variant_texts))[:40],
            condition,
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
            effective_to=effective_to,
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
        # A named variant is unresolved only when no target model carries
        # it: "Scorpio-N" rows reach the old and the new Scorpio N, and a
        # variant on one of them is not an unmatched text of the other.
        unmatched = [_unmatched_texts(target, drow) for target in targets]
        nowhere = [t for t in (unmatched[0] if unmatched else []) if all(t in other for other in unmatched[1:])]
        for target in targets:
            _add_eligibility(
                connection,
                tenant_id=tenant_id,
                version_id=version_id,
                target=target,
                drow=drow,
                summary=summary,
                unresolved_texts=nowhere,
            )
        _report_unresolved(summary, drow, unmatched, [t.model_name for t in targets])
        publish_discount_scheme_version(
            connection, tenant_id=tenant_id, discount_scheme_version_id=version_id, actor_id=actor_id
        )
        summary["published"] += 1
        summary["warnings"].extend(drow.warnings)

    if blocking:
        raise ValidationError(
            detail=(
                f"{len(blocking)} scheme row(s) name a model that is not known; nothing was published. "
                "Add the wording as a model alias or correct the document. " + " | ".join(blocking[:10])
            )
        )
    summary["tombstoned"] = _tombstone_absent_schemes(
        connection,
        tenant_id=tenant_id,
        categories=categories,
        live_codes=live_codes,
        effective_from=effective_from,
        actor_id=actor_id,
    )
    summary["mappingTasksClosed"] = _close_superseded_master_tasks(connection, tenant_id=tenant_id)
    return summary


def _close_superseded_master_tasks(connection: Connection, *, tenant_id: str) -> int:
    # imported here: the reconciliation module imports this one lazily too
    from audit_core.uc03_deal_reconciliation import close_superseded_master_tasks

    return close_superseded_master_tasks(connection, tenant_id=tenant_id)


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


def _add_eligibility(
    connection: Connection,
    *,
    tenant_id: str,
    version_id: UUID,
    target: _Target,
    drow: Any,
    summary: dict[str, Any],
    customer_type: str | None = None,
    extra_criteria: dict[str, Any] | None = None,
    unresolved_texts: list[str] | None = None,
) -> list[str]:
    """Who a scheme version applies to on one model; returns the named
    variants this model does not carry (the caller reports those no target
    model carries).

    2026-10-20 product owner rules:
      * a row naming variants applies to exactly those variants; a named
        variant the price list does not carry is stored UNRESOLVED (never
        applied) and reported for an admin to map -- never the whole model;
      * only a row naming no variant ("All Variants", the exchange tables)
        is model-wide, and then only for its variant group when the model
        combines several (Bolero / Neo / Neo Plus, Camper / Pick-Up, REVX);
      * excluded variants ("Not on Star Edition") are left out, and the
        exclusion is kept on a model-wide row for the reconciliation."""
    if customer_type is None and drow.scheme_category == "CORPORATE":
        customer_type = f"CORPORATE:{drow.config.get('privilegeCategory')}"
    qualifiers = target.qualifiers
    excluded = list(qualifiers.get("excludedVariantTexts") or [])
    requirement = qualifiers.get("requiredOldVehicle") or drow.config.get("requiredOldVehicle")
    criteria = {
        "section": drow.config.get("section"),
        "schemeType": drow.config.get("schemeType"),
        "oldVehicleModel": drow.config.get("oldVehicleModel"),
        "requiredOldVehicle": requirement,
        "excludedVariantTexts": excluded,
        "variantGroup": target.group,
        "vehicleCategory": drow.config.get("vehicleCategory"),
        "modelCell": drow.model_alias,
        "mAndMContribution": drow.config.get("mAndMContribution"),
        "dealerContribution": drow.config.get("dealerContribution"),
        "creditNoteWithoutGst": drow.config.get("creditNoteWithoutGst"),
        "description": drow.config.get("description"),
        **(extra_criteria or {}),
    }
    criteria = {k: v for k, v in criteria.items() if v not in (None, [], "", {})}

    pool = [(name, vid) for name, vid in target.variants
            if variant_in_group(target.group, name) and not variant_excluded(name, excluded)]
    texts = [] if qualifiers.get("allVariants") else list(drow.variant_texts)

    def insert(variant_id: UUID | None, extra: dict[str, Any]) -> None:
        _insert_eligibility(
            connection, tenant_id=tenant_id, version_id=version_id, model_id=target.model_id,
            variant_id=variant_id, customer_type=customer_type, criteria={**criteria, **extra},
        )

    if not texts:
        if target.group is None:
            insert(None, {"scope": "MODEL"})
            return []
        if not pool:
            insert(None, {"scope": "UNRESOLVED", "unresolvedVariantTexts": [f"group {target.group}"]})
            return [f"{target.group} variants of {target.model_name}"]
        for name, variant_id in pool:
            insert(variant_id, {"scope": "VARIANT_GROUP", "variantText": name})
        return []

    matches = _match_variants(texts, pool)
    unmatched = [raw for raw, ids in matches if not ids]
    if unresolved_texts is not None:
        unmatched = [raw for raw in unmatched if raw in unresolved_texts]
    for raw, ids in matches:
        for variant_id in ids:
            insert(variant_id, {"scope": "VARIANT", "variantText": raw})
    if unmatched:
        insert(None, {"scope": "UNRESOLVED", "unresolvedVariantTexts": unmatched,
                      "variantTexts": texts})
    return unmatched


def _target_pool(target: _Target, drow: Any) -> tuple[list[tuple[str, UUID]], list[str]]:
    excluded = list(target.qualifiers.get("excludedVariantTexts") or [])
    pool = [(name, vid) for name, vid in target.variants
            if variant_in_group(target.group, name) and not variant_excluded(name, excluded)]
    texts = [] if target.qualifiers.get("allVariants") else list(drow.variant_texts)
    return pool, texts


def _unmatched_texts(target: _Target, drow: Any) -> list[str]:
    """The named variants this target model does not carry (pure)."""
    pool, texts = _target_pool(target, drow)
    if not texts:
        return [f"{target.group} variants of {target.model_name}"] if target.group and not pool else []
    return [raw for raw, ids in _match_variants(texts, pool) if not ids]


def _valid_to(parsed: ParseResult, effective_from: date, effective_to: date | None,
              summary: dict[str, Any]) -> date | None:
    """The last day a scheme upload stands on (2026-10-20 product owner
    rules, R5; the review of PR 472, S1): as entered, else the validity the
    bulletin prints ("valid from 3rd Sept'26 to 30th Sept'26"; a monthly
    bulletin its month), so September's schemes never answer for October."""
    if effective_to is None:
        effective_to = parsed.effective_to_hint
    if effective_to is not None and effective_to < effective_from:
        summary["warnings"].append(
            f"The document says it is valid to {effective_to.isoformat()}, before the effective date "
            f"{effective_from.isoformat()} applied; no end date set."
        )
        return None
    return effective_to


def _report_unresolved(summary: dict[str, Any], drow: Any, per_target: list[list[str]], models: list[str]) -> None:
    """Named variants no target model carries: stored unresolved (never
    applied) and listed for an admin to map to price-list variants."""
    if not per_target:
        return
    nowhere = [text for text in per_target[0] if all(text in other for other in per_target[1:])]
    if not nowhere:
        return
    summary["unresolvedVariants"].append({
        "row": drow.row_no, "category": drow.scheme_category, "models": models, "variantTexts": nowhere,
    })
    summary["warnings"].append(
        f"{drow.scheme_category} row {drow.row_no} ({', '.join(models)}): variant(s) "
        f"{', '.join(nowhere)[:160]} not in the price list — stored unresolved, not applied; "
        "map them to price-list variants"
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
    effective_to: date | None = None,
) -> dict[str, Any]:
    """One scheme per (privilege category, model and variant group).

    2026-10-20 product owner rules: the brand cell resolves like every other
    scheme cell (``_model_targets``): "3XO REVX (Not on REVX Edge)" reaches
    the REVX variants other than Edge, "BOLERO NEO+" the Neo Plus variants,
    so BOLERO / NEO / NEO+ and XUV 3XO / REVX are separate schemes instead of
    competing versions of one."""
    alias_map = _alias_map(connection, oem_code)
    models = _model_catalogue(connection, oem_id)
    in_book = _models_in_book(connection, tenant_id=tenant_id, effective_on=effective_from)
    summary: dict[str, Any] = {
        "published": 0, "tombstoned": 0, "unresolved": [], "unresolvedVariants": [], "warnings": [], "companies": 0,
    }
    effective_to = _valid_to(parsed, effective_from, effective_to, summary)
    live_codes: set[str] = set()
    blocking: list[str] = []

    for brow in parsed.corporate_benefits:
        targets, _, unknown, not_loaded = _model_targets(models, alias_map, brow.brand_alias, in_book=in_book)
        if unknown:
            blocking.append(f"corporate {brow.privilege_category}: no model for {unknown} in '{brow.brand_alias}'")
        for part in not_loaded:
            summary["unresolved"].append(
                f"corporate {brow.privilege_category} / {part}: not in the loaded price list — not applied"
            )
        drow = DiscountRow(
            row_no=brow.row_no,
            scheme_category="CORPORATE",
            model_alias=brow.brand_alias,
            variant_texts=[],
            benefits=[("CORPORATE_PRIVILEGE", brow.total)],
            total_customer_offer=brow.total,
            config={
                "privilegeCategory": brow.privilege_category,
                "mAndMContribution": str(brow.m_and_m),
                "dealerContribution": str(brow.dealer),
            },
        )
        # One scheme per code: two loaded models of one name share it.
        by_code: dict[str, list[_Target]] = {}
        for target in targets:
            by_code.setdefault(_scheme_code("MAH", "CORPORATE", brow.privilege_category, target.code_name), []).append(
                target
            )
        for code, same_code in by_code.items():
            live_codes.add(code)
            scheme_id = _ensure_scheme(
                connection,
                tenant_id=tenant_id,
                code=code,
                name=f"Mahindra Corporate Privilege {brow.privilege_category} — {brow.brand_alias}"[:240],
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
                effective_to=effective_to,
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
            _report_unresolved(summary, drow, [_add_eligibility(
                connection,
                tenant_id=tenant_id,
                version_id=version_id,
                target=one,
                drow=drow,
                summary=summary,
                customer_type=f"CORPORATE:{brow.privilege_category}",
                extra_criteria={"privilegeCategory": brow.privilege_category},
            ) for one in same_code], [one.model_name for one in same_code])
            publish_discount_scheme_version(
                connection, tenant_id=tenant_id, discount_scheme_version_id=version_id, actor_id=actor_id
            )
            summary["published"] += 1

    if blocking:
        raise ValidationError(
            detail=(
                f"{len(blocking)} corporate row(s) name a model that is not known; nothing was published. "
                "Add the wording as a model alias or correct the document. " + " | ".join(blocking[:10])
            )
        )
    summary["tombstoned"] = _tombstone_absent_schemes(
        connection,
        tenant_id=tenant_id,
        categories=("CORPORATE",),
        live_codes=live_codes,
        effective_from=effective_from,
        actor_id=actor_id,
    )

    # company registry — full replace for this OEM. The registry can run to
    # hundreds/thousands of companies; upsert it in a handful of chunked
    # multi-row statements rather than one round trip per company.
    connection.execute(
        text("DELETE FROM auditcore.corporate_privilege_registry WHERE oem_code = :oem_code"),
        {"oem_code": oem_code},
    )
    _bulk_upsert_corporate_companies(
        connection,
        rows=[
            {
                "oem_code": oem_code,
                "corporate_code": company.corporate_code[:60],
                "corporate_name": company.corporate_name[:400],
                "corporate_type": (company.corporate_type or None) and company.corporate_type[:200],
                "privilege_category": company.privilege_category,
                "source_upload_id": upload_id,
                "effective_from": effective_from,
            }
            for company in parsed.corporate_companies
        ],
    )
    _insert_registry_entries(
        connection, tenant_id=tenant_id, oem_code=oem_code, upload_id=upload_id,
        effective_from=effective_from, companies=parsed.corporate_companies,
    )
    summary["companies"] = len(parsed.corporate_companies)
    summary["mappingTasksClosed"] = _close_superseded_master_tasks(connection, tenant_id=tenant_id)
    return summary


def _insert_registry_entries(
    connection: Connection, *, tenant_id: str, oem_code: str, upload_id: UUID, effective_from: date,
    companies: list[Any], chunk_size: int = 500,
) -> None:
    """2026-10-20 product owner rules (R5; review of PR 472, S5): the
    corporate list a deal is categorised by is the tenant's own upload in
    force on the deal's pricing date -- every upload is kept under its date,
    so a later list never re-categorises an earlier deal."""
    # An ingest called without an upload (a test, a script) still gets one
    # list of its own.
    upload_id = upload_id or uuid4()
    deduped = {company.corporate_code[:60]: company for company in companies}
    rows = [
        {
            "tenant_id": tenant_id, "oem_code": oem_code, "source_upload_id": upload_id,
            "effective_from": effective_from, "corporate_code": code,
            "corporate_name": company.corporate_name[:400],
            "corporate_type": (company.corporate_type or None) and company.corporate_type[:200],
            "privilege_category": company.privilege_category,
        }
        for code, company in deduped.items()
    ]
    columns = ["tenant_id", "oem_code", "source_upload_id", "effective_from", "corporate_code",
               "corporate_name", "corporate_type", "privilege_category"]
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start: start + chunk_size]
        params: dict[str, Any] = {}
        values_sql: list[str] = []
        for i, row in enumerate(chunk):
            for col in columns:
                params[f"{col}_{i}"] = row[col]
            values_sql.append("(" + ", ".join(f":{col}_{i}" for col in columns) + ")")
        connection.execute(
            text(
                f"INSERT INTO auditcore.corporate_registry_entries ({', '.join(columns)}) "
                f"VALUES {', '.join(values_sql)} ON CONFLICT DO NOTHING"
            ),
            params,
        )



def _bulk_upsert_corporate_companies(
    connection: Connection, *, rows: list[dict[str, Any]], chunk_size: int = 500
) -> None:
    if not rows:
        return
    # a single multi-row INSERT ... ON CONFLICT cannot affect the same conflict
    # target twice; keep the last row for a repeated (oem_code, corporate_code),
    # matching the previous sequential upsert's "last one wins" behaviour.
    deduped: dict[tuple[str, str], dict[str, Any]] = {
        (row["oem_code"], row["corporate_code"]): row for row in rows
    }
    rows = list(deduped.values())
    columns = [
        "oem_code", "corporate_code", "corporate_name", "corporate_type",
        "privilege_category", "source_upload_id", "effective_from",
    ]
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start : start + chunk_size]
        params: dict[str, Any] = {}
        values_sql: list[str] = []
        for i, row in enumerate(chunk):
            placeholders = [f":{col}_{i}" for col in columns]
            for col in columns:
                params[f"{col}_{i}"] = row[col]
            values_sql.append(f"({', '.join(placeholders)})")
        connection.execute(
            text(
                f"""
                INSERT INTO auditcore.corporate_privilege_registry ({', '.join(columns)})
                VALUES {', '.join(values_sql)}
                ON CONFLICT (oem_code, corporate_code) DO UPDATE SET
                    corporate_name = EXCLUDED.corporate_name,
                    corporate_type = EXCLUDED.corporate_type,
                    privilege_category = EXCLUDED.privilege_category,
                    source_upload_id = EXCLUDED.source_upload_id,
                    effective_from = EXCLUDED.effective_from
                """
            ),
            params,
        )


# ── preview builder ─────────────────────────────────────────────────────────────
_PRICE_COMPONENT_PREVIEW_LABELS: dict[str, str] = {
    "EX_SHOWROOM": "exShowroom",
    "TCS": "tcs",
    "INSURANCE": "insurance",
    "EXT_WARRANTY_4TH_YR": "extWarranty4thYr",
    "EXT_WARRANTY_4TH_5TH_YR": "extWarranty4th5thYr",
    "ACCESSORIES_KIT": "accessoriesKit",
    "RSA_1YR": "rsa1Yr",
    "FASTAG": "fastag",
    "REGISTRATION_INDIVIDUAL": "registrationIndividual",
    "REGISTRATION_CORPORATE": "registrationCorporate",
}


# ── the dealer's discount grid (fifth master) ───────────────────────────────────
# How the dealer's grid spells models (2026-10-20). They name the price list's
# own models; "MAXX" is "MAXX City" (the October grid spells the same row
# "PICKUP, MAXX City, MAXX HD"); "MAXX HD 2.0" is the 2.0 L variants of MAXX
# HD (a variant group). The September grid's "SCORPIO N Old" / "SCORPIO N
# New" are the two models the September book sells (2026-11-01): "SCORPIO N"
# -- the series the October list no longer prints, the OLD Scorpio N of the
# model identity above -- and "NEW SCORPIO N" (the "New SCORPIO N" sheets,
# which October prints as SCORPIO-N). Until then the two lines had no model
# and a September New Scorpio N deal had no grid line.
_GRID_MODEL_ALIASES: dict[str, str] = {
    "SCORPIO N OLD": "SCORPIO N",
    "SCORPIO N NEW": "NEW SCORPIO N",
    "3XO": "XUV3XO",
    "7XO": "XUV 7XO",
    "9E": "XEV 9E",
    "9S": "XEV9S",
    "3XO EV": "XUV3XO EV",
    "XUV400 EV": "XUV400",
    "MAXX": "MAXX CITY",
    "MAXX HD 2.0": "MAXX HD",
    # The Camper is a variant group of PICK UP (review of PR 472, B1).
    "CAMPER": "PICK UP",
}


# Models a grid line covers besides the one it names (grid only; see
# ``_grid_targets``): the identity named -> the identities it also covers.
_GRID_ALSO_COVERS: dict[str, tuple[str, ...]] = {
    "BE6": ("BE6SPORTEQ",),
    "BOLERONEO": ("BOLERONEOPLUS",),
}


def _next_grid_start(connection: Connection, *, tenant_id: str, after: date) -> date | None:
    """The day the next grid version starts (it takes over from then), or None."""
    return connection.execute(
        text(
            "SELECT MIN(effective_from) FROM auditcore.dealer_discount_grid_versions "
            "WHERE tenant_id = :t AND lifecycle_status IN ('PUBLISHED', 'RETIRED') AND effective_from > :a"
        ),
        {"t": tenant_id, "a": after},
    ).scalar_one()


def _grid_targets(
    models: list[tuple[str, UUID, list[tuple[str, UUID]]]], alias_map: dict[str, str], alias: str,
    books: list[set[UUID]],
) -> list[_Target]:
    """The models a grid line names, read against each book the grid is used
    with: the book in force says which model the words mean (review of
    PR 472: the October grid, dated 1 Oct, names the new Scorpio N, which
    only the October book of 3 Oct prices). A model several books resolve
    to is one target."""
    targets: list[_Target] = []
    seen: set[tuple[UUID, str | None]] = set()
    read: list[set[str]] = []
    for in_book in books or [None]:  # type: ignore[list-item]
        found, _, _, _ = _model_targets(models, alias_map, alias, extra_aliases=_GRID_MODEL_ALIASES, in_book=in_book)
        read.append({price_list_model_key(t.model_name) for t in found})
        for target in found:
            if (target.model_id, target.group) not in seen:
                seen.add((target.model_id, target.group))
                targets.append(target)
    # A grid is for its month's book (2026-11-02): where an earlier book in
    # its window reads the words as a model the latest book has replaced
    # with its successor (the October grid's "SCORPIO-N", dated 1 Oct, read
    # against the September book of 1-2 Oct as the OLD Scorpio N), the line
    # is the successor's only. The old series then takes the previous
    # month's line (``grid_row_for_model``): September's "SCORPIO N Old".
    if len(read) > 1:
        named = {price_list_model_key(t.model_name) for t in targets}
        replaced = {old for old, new in _SUCCESSOR_IDENTITY.items()
                    if old in named and new in read[-1] and old not in read[-1]}
        targets = [t for t in targets if price_list_model_key(t.model_name) not in replaced]
    # A line also covers the models the OEM sells as a variant line of the
    # one it names (product owner, 2026-11-02; review of #468): the "BE6"
    # line covers the BE 6 SPORTEQ, the "BOLERO NEO" line the Bolero Neo
    # Plus (September's "THE BOSS- BOLERO+NEO" is all three, and the grid's
    # "BOLERO & BOLERO NEO" line covered it). Grid only: a scheme's wording
    # never reaches another model (2026-10-20 rules).
    by_identity: dict[str, list[tuple[str, UUID, list[tuple[str, UUID]]]]] = {}
    for entry in models:
        by_identity.setdefault(price_list_model_key(entry[0]), []).append(entry)
    for target in list(targets):
        for extra in _GRID_ALSO_COVERS.get(price_list_model_key(target.model_name), ()):
            for name, model_id, variants in by_identity.get(extra, []):
                if (model_id, None) not in seen:
                    seen.add((model_id, None))
                    targets.append(_Target(model_id=model_id, model_name=name, variants=variants, group=None,
                                           qualifiers=target.qualifiers))
    return targets


def grid_lines_unbound_to_books(
    connection: Connection, *, tenant_id: str, oem_id: UUID, oem_code: str, grid_version_id: UUID,
) -> list[str]:
    """Grid lines whose model is not the one the book they are used with
    prices: for each book in force during the grid's validity, a line whose
    wording names a model that book prices (read against that book) must be
    bound to a model in it. Empty when every line is. A line is never
    "matched" by a model of an earlier book alone."""
    alias_map = _alias_map(connection, oem_code)
    models = _model_catalogue(connection, oem_id)
    version = connection.execute(
        text("SELECT version_no, effective_from FROM auditcore.dealer_discount_grid_versions "
             "WHERE tenant_id = :t AND grid_version_id = :v"),
        {"t": tenant_id, "v": grid_version_id},
    ).one()
    stored: dict[str, set[UUID]] = {}
    for alias, model_id in connection.execute(
        text("SELECT model_alias, model_id FROM auditcore.dealer_discount_grid_rows "
             "WHERE tenant_id = :t AND grid_version_id = :v"),
        {"t": tenant_id, "v": grid_version_id},
    ).all():
        stored.setdefault(alias, set())
        if model_id is not None:
            stored[alias].add(model_id)
    names = {model_id: name for name, model_id, _ in models}
    books = _books_in_use(connection, tenant_id=tenant_id, start=version[1],
                          end=_next_grid_start(connection, tenant_id=tenant_id, after=version[1]))
    # What a book reads the words as, less a model the latest book replaced
    # with its successor (``_grid_targets``: the October grid's "SCORPIO-N"
    # is never the old Scorpio N the book of 1-2 Oct still prices).
    replaced: dict[str, set[str]] = {}
    for alias in stored:
        read = [{price_list_model_key(t.model_name) for t in _model_targets(
            models, alias_map, alias, extra_aliases=_GRID_MODEL_ALIASES, in_book=in_book)[0]} for in_book in books]
        named = set().union(*read) if read else set()
        replaced[alias] = {old for old, new in _SUCCESSOR_IDENTITY.items()
                           if len(read) > 1 and old in named and new in read[-1] and old not in read[-1]}
    gaps: list[str] = []
    for in_book in books:
        for alias, bound in stored.items():
            found, _, _, _ = _model_targets(models, alias_map, alias, extra_aliases=_GRID_MODEL_ALIASES, in_book=in_book)
            wanted = {t.model_id for t in found if price_list_model_key(t.model_name) not in replaced[alias]} & in_book
            if wanted and not bound & in_book:
                gaps.append(
                    f"grid v{version[0]} line '{alias}' names {sorted(names[m] for m in wanted)} in a book in force "
                    f"during the grid, but is bound to {sorted(names.get(m, '?') for m in bound) or 'no model'}"
                )
    return list(dict.fromkeys(gaps))


def ingest_discount_grid(
    connection: Connection,
    *,
    tenant_id: str,
    oem_id: UUID,
    oem_code: str,
    effective_from: date,
    parsed: ParseResult,
    upload_id: UUID | None,
    actor_id: str,
) -> dict[str, Any]:
    """One published grid version per upload, effective-dated; every row keeps
    the model text as written and, where the alias map or the catalogue
    knows it, the model it names. A cell naming several models ("PICKUP,
    MAXX, MAXX HD") becomes one row per model; a model several grid lines
    share (Camper and Pick-Up on PICK UP) keeps each line's variant group,
    so the grid line for a deal is the one for its variant."""
    alias_map = _alias_map(connection, oem_code)
    models = _model_catalogue(connection, oem_id)
    books = _books_in_use(connection, tenant_id=tenant_id, start=effective_from,
                          end=_next_grid_start(connection, tenant_id=tenant_id, after=effective_from))
    summary: dict[str, Any] = {"published": 0, "resolved": 0, "unresolved": [], "warnings": [], "parameters": len(parsed.grid_parameters)}
    version_no = int(connection.execute(
        text("SELECT COALESCE(MAX(version_no), 0) + 1 FROM auditcore.dealer_discount_grid_versions WHERE tenant_id = :t"),
        {"t": tenant_id},
    ).scalar_one())
    parameters = list(parsed.grid_parameters)
    if parsed.meta.get("odCapsPercent"):
        parameters.append({"parameter": "OD caps (parsed)", "note": json.dumps(parsed.meta["odCapsPercent"])})
    version_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.dealer_discount_grid_versions (
                tenant_id, version_no, effective_from, source_upload_id, parameters, created_by_actor_id
            ) VALUES (:t, :no, :eff, :upload, CAST(:params AS jsonb), :actor)
            RETURNING grid_version_id
            """
        ),
        {"t": tenant_id, "no": version_no, "eff": effective_from, "upload": upload_id,
         "params": json.dumps(parameters), "actor": actor_id},
    ).scalar_one()
    rows: list[dict[str, Any]] = []
    for grow in parsed.grid_rows:
        for alias in grow.model_aliases:
            targets = _grid_targets(models, alias_map, alias, books)
            if not targets:
                summary["unresolved"].append(f"grid row {grow.row_no}: model '{alias}' is not in the catalogue yet")
            else:
                summary["resolved"] += 1
            row_base = {
                "tenant_id": tenant_id, "grid_version_id": version_id, "model_alias": alias[:240],
                "in_scope": grow.in_scope, "booking_protection_days": grow.booking_protection_days,
                "agreed_buffer_amount": grow.agreed_buffer_amount, "insurance_od_percent": grow.insurance_od_percent,
                "out_of_territory_amount": grow.out_of_territory_amount,
            }
            raw = {**grow.raw, "modelCell": grow.model_alias}
            if grow.tiers:
                raw["tiers"] = {tier: str(amount) for tier, amount in grow.tiers.items()}
            for target in targets or [None]:
                rows.append({
                    **row_base,
                    "model_id": target.model_id if target else None,
                    "raw": json.dumps({**raw, **({"variantGroup": target.group} if target and target.group else {})}),
                })
    _chunked_insert_returning(
        connection,
        table="dealer_discount_grid_rows",
        columns=["tenant_id", "grid_version_id", "model_alias", "model_id", "in_scope", "booking_protection_days",
                 "agreed_buffer_amount", "insurance_od_percent", "out_of_territory_amount", "raw"],
        rows=rows,
        returning=[],
        jsonb_columns=frozenset({"raw"}),
    )
    summary["published"] = len(rows)
    summary["gridVersionId"] = str(version_id)
    summary["mappingTasksClosed"] = _close_superseded_master_tasks(connection, tenant_id=tenant_id)
    return summary


def grid_version_for_date(connection: Connection, *, tenant_id: str, effective_on: date) -> dict[str, Any] | None:
    """The dealer discount grid version standing on a date, or None."""
    row = connection.execute(
        text(
            """
            SELECT grid_version_id, version_no, effective_from, effective_to, parameters
            FROM auditcore.dealer_discount_grid_versions
            WHERE tenant_id = :t AND lifecycle_status IN ('PUBLISHED', 'RETIRED')
              AND effective_from <= :on AND (effective_to IS NULL OR effective_to >= :on)
            ORDER BY effective_from DESC, version_no DESC
            LIMIT 1
            """
        ),
        {"t": tenant_id, "on": effective_on},
    ).mappings().first()
    return dict(row) if row is not None else None


def _grid_alias_identity(alias: str | None) -> str:
    base, _ = parse_model_qualifiers(alias or "")
    mapped = _GRID_MODEL_ALIASES.get(re.sub(r"\s+", " ", base.strip().upper()), base)
    return price_list_model_key(mapped)


def grid_row_for_model(
    connection: Connection, *, tenant_id: str, model_id: UUID, effective_on: date,
    variant_name: str | None = None,
) -> dict[str, Any] | None:
    """The grid line for a model from the grid version standing on a date.

    The grid in force on the date answers; a model it does not cover takes
    the previous month's line (product owner, 2026-11-02, replacing R5 of
    2026-10-20, under which it had none). Where a model has several lines
    (Camper and Pick-Up, MAXX HD and MAXX HD 2.0) the line whose variant
    group holds the deal's variant wins over the model's general line, and
    a line for another group never answers (a Pick-Up never takes the
    Camper line). A line stored before its model resolved (no model) is
    read by its wording -- the grid's own spellings ("3XO", "MAXX") -- and
    a line stored without its group gets the group its wording names
    (review of PR 472, B1)."""
    version = grid_version_for_date(connection, tenant_id=tenant_id, effective_on=effective_on)
    if version is None:
        return None
    found = _grid_line_in_version(connection, tenant_id=tenant_id, version=version, model_id=model_id,
                                  variant_name=variant_name)
    if found is not None:
        return found
    # Product owner, 2026-11-02 (replaces R5's "no fallback"): what a month's
    # grid does not cover is taken from the previous month's grid -- the
    # October grid has no XUV400 line (September's "XUV400 EV" answers),
    # and the old Scorpio N, sold in October from the September series, takes
    # September's "SCORPIO N Old" line. One month back only.
    previous = grid_version_for_date(connection, tenant_id=tenant_id,
                                     effective_on=version["effective_from"] - timedelta(days=1))
    if previous is None or previous["grid_version_id"] == version["grid_version_id"]:
        return None
    carried = _grid_line_in_version(connection, tenant_id=tenant_id, version=previous, model_id=model_id,
                                    variant_name=variant_name)
    if carried is None:
        return None
    return {**carried, "carriedFromPreviousGrid": True, "gridInForceVersionNo": version["version_no"],
            "gridInForceEffectiveFrom": str(version["effective_from"])}


def _grid_line_in_version(
    connection: Connection, *, tenant_id: str, version: dict[str, Any], model_id: UUID, variant_name: str | None,
) -> dict[str, Any] | None:
    """The grid line for a model in one grid version (``grid_row_for_model``)."""
    model_name = connection.execute(
        text("SELECT model_name FROM auditcore.product_models WHERE model_id = :m"), {"m": model_id},
    ).scalar_one_or_none()
    identity = price_list_model_key(model_name) if model_name else None
    rows = connection.execute(
        text(
            """
            SELECT r.grid_row_id, r.model_alias, r.model_id, r.in_scope, r.booking_protection_days,
                   r.agreed_buffer_amount, r.insurance_od_percent, r.out_of_territory_amount, r.raw
            FROM auditcore.dealer_discount_grid_rows r
            WHERE r.tenant_id = :t AND r.grid_version_id = :v AND (r.model_id = :model_id OR r.model_id IS NULL)
            ORDER BY r.model_alias, r.grid_row_id
            """
        ),
        {"t": tenant_id, "v": version["grid_version_id"], "model_id": model_id},
    ).mappings().all()
    specific: list[dict[str, Any]] = []
    general: list[dict[str, Any]] = []
    for row in rows:
        if row["model_id"] is None and (identity is None or _grid_alias_identity(row["model_alias"]) != identity):
            continue
        raw = row["raw"] if isinstance(row["raw"], dict) else json.loads(row["raw"] or "{}")
        group = raw.get("variantGroup")
        if group is None and model_name:
            group = grid_variant_group(row["model_alias"], model_name)
        if group is None:
            general.append(dict(row))
        elif variant_name is not None and variant_in_group(group, variant_name):
            specific.append(dict(row))
    chosen = (specific or general or [None])[0]
    if chosen is None:
        return None
    chosen.pop("model_id", None)
    return {**version, **chosen}



def _build_preview(parsed: ParseResult, kind: str) -> dict[str, Any]:
    row_counts: dict[str, Any] = dict(parsed.meta)
    sample: list[dict[str, Any]] = []
    if kind == "PRICE_LIST":
        row_counts["priceRows"] = len(parsed.price_rows)
        # Every price component that will be written to price_list_items on
        # Publish, not just Ex-Showroom — an admin must be able to see the whole
        # line-item breakdown before publishing, not only the headline price.
        sample = [
            {
                "model": r.model_name,
                "variant": r.variant_name,
                "trim": r.trim,
                "fuel": r.fuel,
                "transmission": r.transmission,
                "drive": r.drive,
                "seater": r.seater,
                "category": r.category,
                "registrationBasis": r.registration_basis,
                **{
                    _PRICE_COMPONENT_PREVIEW_LABELS[key]: (str(r.components[key]) if key in r.components else None)
                    for key in PRICE_COMPONENT_KEYS
                },
                "onRoadIndividual": str(r.onroad_individual),
                "onRoadCorporate": str(r.onroad_corporate),
            }
            for r in parsed.price_rows[:25]
        ]
    elif kind == "DISCOUNT_GRID":
        sample = [
            {
                "model": g.model_alias,
                "inScope": g.in_scope,
                "bookingProtectionDays": g.booking_protection_days,
                "agreedBuffer": str(g.agreed_buffer_amount) if g.agreed_buffer_amount is not None else None,
                "insuranceOdPercent": str(g.insurance_od_percent) if g.insurance_od_percent is not None else None,
                "outOfTerritory": str(g.out_of_territory_amount) if g.out_of_territory_amount is not None else None,
                # the October 2026 grid's buffer by tier (2026-10-20)
                "tiers": {tier: str(amount) for tier, amount in g.tiers.items()} or None,
            }
            for g in parsed.grid_rows[:25]
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


_FAILED_UPLOAD_SQL = """
    INSERT INTO auditcore.oem_master_uploads (
        tenant_id, oem_code, master_kind, source_filename, source_sha256,
        effective_from, status, row_counts, preview, error_detail, uploaded_by_actor_id
    ) VALUES (
        :tenant_id, :oem_code, :kind, :filename, :sha, :eff, 'FAILED',
        CAST(:row_counts AS jsonb), CAST(:preview AS jsonb), :error, :actor
    ) RETURNING upload_id
"""


def _record_failed_upload(connection: Connection, failure: dict[str, Any]) -> UUID:
    return connection.execute(text(_FAILED_UPLOAD_SQL), failure).scalar_one()


def _record_failed_upload_separately(tenant_id: str, failure: dict[str, Any]) -> None:
    """Best effort: a failure to record the failure never hides the real error."""
    from audit_core.dependencies import get_engine

    try:
        with get_engine().begin() as side:
            set_platform_super_admin_context(side)
            set_tenant_context(side, tenant_id)
            _record_failed_upload(side, failure)
    except Exception:  # noqa: BLE001 - recording is secondary to the refusal itself
        logger.warning("oem_master_failed_upload_not_recorded", tenant_id=tenant_id)


# ── routes ──────────────────────────────────────────────────────────────────────
@router.post("/uploads", response_model=MasterUploadPreview)
async def upload_oem_master(
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    tenant_id: Annotated[str, Form(alias="tenantId")],
    master_kind: Annotated[MasterKind, Form(alias="masterKind")],
    file: Annotated[UploadFile, File()],
    effective_from: Annotated[date | None, Form(alias="effectiveFrom")] = None,
    dry_run: Annotated[bool, Query(alias="dryRun")] = True,
) -> MasterUploadPreview:
    """Effective date: the file's own (its sheet, else its name) unless the
    admin enters one, which wins (decision 2026-09-30)."""
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
        parsed = parse_master(master_kind, content, filename=file.filename)
    except MasterParseError as exc:
        raise ValidationError(detail=str(exc)) from exc

    preview = _build_preview(parsed, master_kind)
    date_source = "ADMIN"
    if effective_from is None:
        if parsed.effective_from_hint is None:
            raise ValidationError(detail="The file carries no effective date; enter one.")
        effective_from = parsed.effective_from_hint
        date_source = str(parsed.meta.get("effectiveFromSource") or "SHEET")
    elif parsed.effective_from_hint is not None and parsed.effective_from_hint != effective_from:
        preview["warnings"].append(
            f"Applied {effective_from.isoformat()} as entered; the file says {parsed.effective_from_hint.isoformat()}."
        )
    price_stats: dict[str, Any] = {}
    unresolved: list[str] = []
    actor_id = admin_request.user_id

    upload_id: UUID | None = None
    price_version_id: UUID | None = None
    discount_summary: dict[str, Any] = {}
    status = "PREVIEW"

    if parsed.errors:
        # Keep the failed attempt, with why (2026-10-20 product owner rules):
        # the master documents view showed nothing at all for October, whose
        # upload had been refused. The table's status CHECK allows FAILED.
        failure = {
            "tenant_id": tenant_id, "oem_code": project["oem_code"], "kind": master_kind,
            "filename": (file.filename or "upload")[:400], "sha": sha256, "eff": effective_from,
            "row_counts": json.dumps(preview["rowCounts"]),
            "preview": json.dumps({"sample": preview["sample"], "warnings": preview["warnings"], "dryRun": dry_run}),
            "actor": actor_id,
            # A preview is marked as such: it is not a refused publish.
            "error": ("PREVIEW: " if dry_run else "")
            + f"{len(parsed.errors)} unreconciled row(s): " + "; ".join(parsed.errors[:50]),
        }
        if dry_run:
            # A preview commits with the request; its response is unchanged.
            _record_failed_upload(connection, failure)
        else:
            # Publishing is refused and the request rolls back, so the record
            # is written in a transaction of its own.
            _record_failed_upload_separately(tenant_id, failure)
            raise ValidationError(
                detail="File has "
                f"{len(parsed.errors)} unreconciled row(s); fix the source and re-upload. "
                f"First: {parsed.errors[0]}"
            )

    if not dry_run:
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

        # Keep the document itself, so the figures can be checked against
        # what was actually sent. Storage failing must not cost the month's
        # prices: the upload still publishes, with no key and a warning, and
        # a reader is told the source was not stored rather than handed a
        # broken link.
        object_key = _master_object_key(
            tenant_id=tenant_id, oem_code=project["oem_code"], master_kind=master_kind,
            effective_from=effective_from, upload_id=upload_id, filename=file.filename,
        )
        try:
            get_p2_document_storage().put_object(
                object_key, content, content_type=_master_content_type(file.filename)
            )
        except Exception as exc:  # noqa: BLE001 - archiving must never block publishing
            preview["warnings"].append(
                f"The document itself could not be stored ({exc.__class__.__name__}); "
                "the figures were published but the file cannot be opened later."
            )
            logger.warning("oem_master_source_not_stored", upload_id=str(upload_id), key=object_key)
        else:
            connection.execute(
                text(
                    "UPDATE auditcore.oem_master_uploads SET source_object_key = :key "
                    "WHERE tenant_id = :t AND upload_id = :u"
                ),
                {"key": object_key, "t": tenant_id, "u": upload_id},
            )

        if master_kind == "PRICE_LIST":
            price_version_id, _ = ingest_price_list(
                connection,
                tenant_id=tenant_id,
                oem_id=project["oem_id"],
                effective_from=effective_from,
                parsed=parsed,
                actor_id=actor_id,
                stats=price_stats,
            )
            preview["rowCounts"].update(price_stats)
        elif master_kind == "DISCOUNT_GRID":
            discount_summary = ingest_discount_grid(
                connection,
                tenant_id=tenant_id,
                oem_id=project["oem_id"],
                oem_code=project["oem_code"],
                effective_from=effective_from,
                parsed=parsed,
                upload_id=upload_id,
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
        effectiveFromSource=date_source,
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
    "find_book_sku",
    "ingest_corporate_policy",
    "ingest_discount_document",
    "ingest_price_list",
    "model_identity_keys",
    "price_list_model_key",
    "router",
    "variant_core",
]
