"""uc03_model_resolution.py — resolve a booking's SKU against the OEM price
masters, or raise a ``MODEL_NOT_IDENTIFIED`` finding for the PC.

The V2 booking flow materialises the reviewed model / variant / colour onto
``journey_products`` but never sets ``product_sku_id`` — so ``_sku_pricing_panel``
(the master-vs-booking Deal panel) silently returns nothing and the deal is
never checked against the masters.

This module closes that gap deterministically, against the OEM *native* price
list (``price_list_items`` — the one ``oem_price_masters`` ingests from Mahindra's
own documents), which the older ``uc03_sku_candidates`` resolver does not see
(it reads ``project_product_master_items``):

  1. exact model + exact ``total`` (offered on-road)               -> SKU
  2. if (1) is 0 or >1 : exact model + exact ``ex_showroom``       -> SKU
  2b. if the whole-string model match found nothing at all: fall back to
      ``uc03_model_attribute_matching`` — resolve the model via
      ``oem_model_aliases`` and re-filter by the variant's own structured
      fuel/transmission/drive/seater attributes, since a real Booking Form
      folds trim + those attributes into the same free-text field as the
      model name (e.g. ``"SCORPIO N Z8 (S)"`` / ``"DAT 2WD 7STR"``).
  3. one SKU  -> pin ``journey_products.product_sku_id``, resolve any open flag
  4. zero SKU -> MODEL_NOT_IDENTIFIED "no matching model"
  5. >1  SKU  -> MODEL_NOT_IDENTIFIED "matched multiple models" (+ candidates)

No fuzzy matching, no price tolerance (``_label_similarity`` is normalised
equality only; the attribute fallback is exact-per-attribute, never a fuzzy
score). Idempotent, self-heals on read, never raises.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError, NotFoundError
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_capture import _scope
from audit_core.uc03_delivery_commands import _machine_flag
from audit_core.uc03_masters_alignment import registration_basis
from audit_core.uc03_model_attribute_matching import (
    has_qualifying_signal,
    match_by_attributes,
    resolve_model_via_aliases,
)
from audit_core.uc03_sku_candidates import (
    _label_similarity,
    _normalize_label,
    _price_plan_for_journey,
)
from audit_core.uc03_v2_review_materialization import (
    _INVOICE_DOCUMENT_TYPES as _DELIVERY_INVOICE_DOCUMENT_TYPES,
)

logger = logging.getLogger(__name__)

_FINDING_TYPE = "MODEL_NOT_IDENTIFIED"
_RULE_KEY = "MODEL_NOT_IDENTIFIED:BOOKING"
_STAGE = "BOOKING"
_SELECTION_METHOD = "MODEL_RESOLUTION_SYNC_V1"
_EX_SHOWROOM_COMPONENT = "EX_SHOWROOM"

# Delivery-side fallback (see sync_model_resolution_from_invoice below): DI's
# generalized invoice schema (verigence-di schemas/invoice.py) never maps
# these to a master -- "exactly as printed", "do not map to a master" -- so
# this module still does that resolution, deterministically, the same way it
# already does for the Booking Form.
_INVOICE_SKU_CODE_FIELD_KEYS = ("sku_code",)
_INVOICE_MODEL_FIELD_KEYS = ("model_name_raw",)
_INVOICE_VARIANT_FIELD_KEYS = ("variant_raw",)
_INVOICE_SELECTION_METHOD = "MODEL_RESOLUTION_INVOICE_FALLBACK_V1"


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


# ── inputs ────────────────────────────────────────────────────────────────────
def _resolution_inputs(
    connection: Connection, *, tenant_id: str, journey_id: UUID, require_model: bool = True
) -> dict[str, Any] | None:
    """Reviewed model / offered total / offered ex-showroom for this journey.

    Returns None when there is nothing to resolve yet (no model snapshot) --
    unless ``require_model=False``. That's sync_model_resolution_from_
    invoice's own case: it exists SPECIFICALLY for when the Booking side
    never captured a usable model at all (no journey_products row yet, or
    one with no model_name_snapshot), substituting the Delivery invoice's
    own model/sku_code text instead. Bailing out here just because that
    exact condition is true was a real, confirmed bug: the one caller this
    was built for could never actually reach its own fallback logic, since
    it always hit this same early return first -- "resolve from the invoice
    when the Booking model isn't found" silently never ran for the one case
    it names. Only the registration/commercial inputs below are genuinely
    needed by that caller; model_name/variant_name/colour_name/
    product_sku_id/selection_status all come back None when there's no row,
    which is exactly what it wants (it overwrites model_name/variant_name
    from the invoice regardless).
    """
    jp = connection.execute(
        text(
            """
            SELECT product_sku_id,
                   NULLIF(TRIM(model_name_snapshot), '')   AS model_name,
                   NULLIF(TRIM(variant_name_snapshot), '') AS variant_name,
                   NULLIF(TRIM(colour_name_snapshot), '')  AS colour_name,
                   selection_status
            FROM auditcore.journey_products
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if jp is None:
        if require_model:
            return None
        jp = {
            "product_sku_id": None,
            "model_name": None,
            "variant_name": None,
            "colour_name": None,
            "selection_status": None,
        }
    elif require_model and jp["model_name"] is None:
        return None

    reg = connection.execute(
        text(
            """
            SELECT cu.customer_type_code AS customer_type_code,
                   rr.registration_type_code AS registration_type_code
            FROM auditcore.journeys j
            LEFT JOIN auditcore.customers cu
              ON cu.tenant_id = j.tenant_id AND cu.customer_id = j.customer_id
            LEFT JOIN auditcore.registration_records rr
              ON rr.tenant_id = j.tenant_id AND rr.journey_id = j.journey_id
            WHERE j.tenant_id = :tenant_id AND j.journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none() or {}
    basis = registration_basis(
        customer_type_code=reg.get("customer_type_code"),
        registration_type_code=reg.get("registration_type_code"),
    )

    commercials = connection.execute(
        text(
            """
            SELECT component_key, actual_amount
            FROM auditcore.commercial_lines
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND actual_amount IS NOT NULL
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    by_key = {row["component_key"]: _to_decimal(row["actual_amount"]) for row in commercials}

    booking_prices = connection.execute(
        text(
            """
            SELECT NULLIF(regexp_replace(COALESCE(total_price::text, ''), '[^0-9.\\-]', '', 'g'), '')
                       AS total_price,
                   NULLIF(regexp_replace(COALESCE(ex_showroom_price::text, ''), '[^0-9.\\-]', '', 'g'), '')
                       AS ex_showroom_price
            FROM auditcore.booking_form_review_values
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY reviewed_at_utc DESC NULLS LAST
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none() or {}

    offered_total = (
        by_key.get("total_price")
        or _to_decimal(booking_prices.get("total_price"))
    )
    # commercial_lines is only written once the Booking Form document has
    # actually finished materializing (see uc03_v2_review_materialization.py
    # ::_materialize_commercial_lines) -- until then this fell back to None
    # even when booking_form_review_values.ex_showroom_price itself was
    # already populated, unlike offered_total above (which has always had
    # this same fallback). A live journey with 24 model-name-only price-
    # master matches and a legible ex-showroom price on its Booking Form
    # still failed to disambiguate for exactly this reason.
    offered_ex_showroom = (
        by_key.get("ex_showroom_price")
        or _to_decimal(booking_prices.get("ex_showroom_price"))
    )

    return {
        "product_sku_id": jp["product_sku_id"],
        "selection_status": jp["selection_status"],
        "model_name": jp["model_name"],
        "variant_name": jp["variant_name"],
        "colour_name": jp["colour_name"],
        "offered_total": offered_total,
        "offered_ex_showroom": offered_ex_showroom,
        "registration_basis": basis,
    }


def _sku_rows_for_version(
    connection: Connection, *, tenant_id: str, price_list_version_id: UUID
) -> list[dict[str, Any]]:
    """SKU rows with on-road totals.

    An OEM price row carries both REGISTRATION_INDIVIDUAL and REGISTRATION_CORPORATE
    components, so a naive SUM double-counts registration. ``master_total_individual``
    excludes the corporate line, ``master_total_corporate`` excludes the individual
    line — the resolver matches against whichever the buyer's basis selects.

    ``fuel_powertrain``/``transmission``/``drive``/``seater`` are the variant's own
    already-clean structured attributes (populated verbatim from the OEM's price-list
    columns at ingestion) — unused by ``_match``'s whole-string comparison, but read
    here for ``uc03_model_attribute_matching``'s decomposition fallback.
    """
    rows = connection.execute(
        text(
            """
            SELECT s.product_sku_id,
                   s.sku_code,
                   pm.model_name,
                   pv.variant_name,
                   c.colour_name,
                   pv.fuel_powertrain,
                   pv.transmission,
                   pv.attributes ->> 'drive'  AS drive,
                   pv.attributes ->> 'seater' AS seater,
                   SUM(pli.standard_amount) FILTER (WHERE pli.component_key <> 'REGISTRATION_CORPORATE')
                                                                                AS master_total_individual,
                   SUM(pli.standard_amount) FILTER (WHERE pli.component_key <> 'REGISTRATION_INDIVIDUAL')
                                                                                AS master_total_corporate,
                   SUM(pli.standard_amount) FILTER (WHERE pli.component_key = :exkey)
                                                                                AS master_ex_showroom
            FROM auditcore.price_list_items pli
            JOIN auditcore.product_skus s      ON s.product_sku_id = pli.product_sku_id
            JOIN auditcore.product_models pm   ON pm.model_id  = s.model_id
            JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
            LEFT JOIN auditcore.colours c      ON c.colour_id  = s.colour_id
            WHERE pli.tenant_id = :tenant_id
              AND pli.price_list_version_id = :plv
              AND s.is_active = true
              AND pm.is_active = true
              AND pv.is_active = true
            GROUP BY s.product_sku_id, s.sku_code, pm.model_name, pv.variant_name, c.colour_name,
                     pv.fuel_powertrain, pv.transmission,
                     pv.attributes ->> 'drive', pv.attributes ->> 'seater'
            """
        ),
        {"tenant_id": tenant_id, "plv": price_list_version_id, "exkey": _EX_SHOWROOM_COMPONENT},
    ).mappings().all()
    return [dict(row) for row in rows]


def _oem_code_for_tenant(connection: Connection, *, tenant_id: str) -> str | None:
    return connection.execute(
        text(
            """
            SELECT o.oem_code
            FROM auditcore.projects p
            JOIN auditcore.oems o ON o.oem_id = p.oem_id
            WHERE p.tenant_id = :tenant_id
            """
        ),
        {"tenant_id": tenant_id},
    ).scalar_one_or_none()


def _oem_model_aliases(connection: Connection, *, oem_code: str) -> list[tuple[str, str]]:
    rows = connection.execute(
        text(
            "SELECT alias_text, canonical_model_name FROM auditcore.oem_model_aliases "
            "WHERE oem_code = :oem_code"
        ),
        {"oem_code": oem_code},
    ).all()
    return [(str(r[0]), str(r[1])) for r in rows]


# ── matching ──────────────────────────────────────────────────────────────────
def _narrow(rows: list[dict[str, Any]], *, variant: str | None, colour: str | None) -> list[dict[str, Any]]:
    if len(rows) > 1 and variant:
        v = [r for r in rows if r["variant_name"] and _label_similarity(variant, str(r["variant_name"])) == Decimal(1)]
        if v:
            rows = v
    if len(rows) > 1 and colour:
        c = [r for r in rows if r["colour_name"] and _label_similarity(colour, str(r["colour_name"])) == Decimal(1)]
        if c:
            rows = c
    return rows


def _master_total(row: dict[str, Any], basis: str) -> Decimal | None:
    key = "master_total_corporate" if basis == "CORPORATE" else "master_total_individual"
    return _to_decimal(row.get(key))


def _strip_new_prefix(name: str) -> str:
    """``"New Scorpio N"`` -> ``"SCORPIO N"``; a plain name is returned
    normalised, unchanged."""
    words = _normalize_label(name).split()
    if words and words[0] == "NEW":
        words = words[1:]
    return " ".join(words)


def _generation_sibling_rows(
    rows: list[dict[str, Any]], model_rows: list[dict[str, Any]], *, model_name: str
) -> list[dict[str, Any]]:
    """Rows for the OEM's "New <Model>" refresh of the same nameplate, or vice
    versa, when the Booking Form's own model text omits that qualifier.

    Confirmed directly against a real ingested Mahindra price list: a
    generation refresh keeps selling under a name that's the old one with a
    "New " prefix (``"SCORPIO N"`` -- an ADAS-trim generation still on sale --
    alongside ``"NEW SCORPIO N"``, a Refresh-trim generation, in the exact
    same currently-effective price list; ``"THAR"``/``"NEW THAR ..."`` is the
    same pattern). Real paperwork very often just writes the bare name,
    which otherwise permanently walls off the refresh generation from ever
    matching, however exactly its price agrees with what's on the form.

    Exact-per-name only, after the same normalisation ``_label_similarity``
    already applies (never fuzzy) -- and this list is only ever consulted
    to arbitrate by an EXACT price match (see ``_match``), never to widen
    what gets reported as an ambiguous candidate set on its own.

    Computed even when ``model_rows`` is empty -- the tenant's currently
    effective price list may carry only the refresh generation at all (the
    old one fully retired), which is exactly the case this exists for.
    """
    base = _strip_new_prefix(model_name)
    if not base:
        return []
    already = {r["product_sku_id"] for r in model_rows}
    return [
        r
        for r in rows
        if r["product_sku_id"] not in already and _strip_new_prefix(str(r["model_name"])) == base
    ]


def _match(
    rows: list[dict[str, Any]], inputs: dict[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    """Return (matched rows, match_stage). match_stage is TOTAL, EX_SHOWROOM or NONE."""
    model = inputs["model_name"]
    basis = inputs["registration_basis"]
    model_rows = [r for r in rows if _label_similarity(model, str(r["model_name"])) == Decimal(1)]
    sibling_rows = _generation_sibling_rows(rows, model_rows, model_name=model)
    if not model_rows and not sibling_rows:
        return [], "NONE"

    total = inputs["offered_total"]
    if total is not None:
        by_total = [r for r in model_rows if _master_total(r, basis) == total]
        by_total = _narrow(by_total, variant=inputs["variant_name"], colour=inputs["colour_name"])
        if len(by_total) == 1:
            return by_total, "TOTAL"
        if not by_total and sibling_rows:
            sibling_total = [r for r in sibling_rows if _master_total(r, basis) == total]
            if len(sibling_total) == 1:
                return sibling_total, "TOTAL"
        if len(by_total) > 1:
            model_rows = by_total  # keep the ambiguity for reporting unless ex-showroom disambiguates

    ex = inputs["offered_ex_showroom"]
    if ex is not None:
        by_ex = [r for r in model_rows if _to_decimal(r["master_ex_showroom"]) == ex]
        by_ex = _narrow(by_ex, variant=inputs["variant_name"], colour=inputs["colour_name"])
        if len(by_ex) == 1:
            return by_ex, "EX_SHOWROOM"
        if not by_ex and sibling_rows:
            sibling_ex = [r for r in sibling_rows if _to_decimal(r["master_ex_showroom"]) == ex]
            if len(sibling_ex) == 1:
                return sibling_ex, "EX_SHOWROOM"
        if len(by_ex) > 1:
            return by_ex, "EX_SHOWROOM"

    if not model_rows:
        return [], "NONE"
    return model_rows, "TOTAL" if total is not None else "NONE"


def _price_disambiguate(
    rows: list[dict[str, Any]], inputs: dict[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    """Narrow an already-established candidate set to one, purely by an
    exact price match -- no name/variant/model filtering of its own (the
    caller has already decided which rows are plausible). Total first, then
    ex-showroom; never a fuzzy tolerance. Used as the deliberate *last*
    resort in ``_current_match``, after attribute decomposition has had its
    chance -- price can coincidentally collide between unrelated SKUs and
    can legitimately drift for reasons that have nothing to do with which
    vehicle was actually sold (accessories, discounts, rounding), unlike a
    stated fuel/transmission/drive/seater fact.
    """
    basis = inputs["registration_basis"]
    total = inputs["offered_total"]
    if total is not None:
        by_total = [r for r in rows if _master_total(r, basis) == total]
        by_total = _narrow(by_total, variant=inputs["variant_name"], colour=inputs["colour_name"])
        if len(by_total) == 1:
            return by_total, "TOTAL"
        if len(by_total) > 1:
            rows = by_total

    ex = inputs["offered_ex_showroom"]
    if ex is not None:
        by_ex = [r for r in rows if _to_decimal(r["master_ex_showroom"]) == ex]
        by_ex = _narrow(by_ex, variant=inputs["variant_name"], colour=inputs["colour_name"])
        if by_ex:
            return by_ex, "EX_SHOWROOM"

    return rows, "TOTAL" if total is not None else "NONE"


# ── persistence ───────────────────────────────────────────────────────────────
def _pin_sku(connection: Connection, *, tenant_id: str, journey_id: UUID, product_sku_id: UUID) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_products (
                tenant_id, journey_id, product_sku_id,
                model_code_snapshot, model_name_snapshot,
                variant_code_snapshot, variant_name_snapshot,
                colour_code_snapshot, colour_name_snapshot,
                selection_source, selection_status, selection_method
            )
            SELECT
                :tenant_id, :journey_id, s.product_sku_id,
                pm.model_code, pm.model_name,
                pv.variant_code, pv.variant_name,
                c.colour_code, c.colour_name,
                'EVIDENCE', 'CONFIRMED', :method
            FROM auditcore.product_skus s
            JOIN auditcore.product_models pm ON pm.model_id = s.model_id
            JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
            LEFT JOIN auditcore.colours c ON c.colour_id = s.colour_id
            WHERE s.product_sku_id = :product_sku_id
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                product_sku_id = EXCLUDED.product_sku_id,
                model_code_snapshot = EXCLUDED.model_code_snapshot,
                model_name_snapshot = EXCLUDED.model_name_snapshot,
                variant_code_snapshot = EXCLUDED.variant_code_snapshot,
                variant_name_snapshot = EXCLUDED.variant_name_snapshot,
                colour_code_snapshot = EXCLUDED.colour_code_snapshot,
                colour_name_snapshot = EXCLUDED.colour_name_snapshot,
                selection_source = 'EVIDENCE',
                selection_status = 'CONFIRMED',
                selection_method = :method,
                updated_at_utc = now()
            WHERE auditcore.journey_products.selection_status IS DISTINCT FROM 'CONFIRMED'
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "product_sku_id": product_sku_id,
            "method": _SELECTION_METHOD,
        },
    )


def _resolve_open_flag(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    correlation_id: str,
    actor_id: str | None = None,
) -> int:
    """Resolve any open MODEL_NOT_IDENTIFIED finding for this journey.

    Never needs to touch the linked Task itself: ``sync_finding_work_item``'s
    trigger (migration 0098) already cancels every still-open Task for a
    finding the instant its ``finding_status`` flips to RESOLVED/VOIDED, as
    a codebase-wide guardrail covering every resolve path, not just this
    one -- adding a second, Python-side completion here would only race
    that trigger and lose (confirmed: the trigger fires synchronously as
    part of the very UPDATE below, before any later statement in this same
    transaction runs).

    ``actor_id`` -- set only by the PC-driven confirm path (the automatic
    match keeps this NULL/SYSTEM) -- records who actually resolved it in
    the finding's own event history, instead of every resolution reading
    as an anonymous system action.
    """
    open_ids = connection.execute(
        text(
            """
            SELECT audit_finding_id
            FROM auditcore.audit_findings
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND finding_type_code = :ft
              AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "ft": _FINDING_TYPE},
    ).scalars().all()
    for finding_id in open_ids:
        connection.execute(
            text(
                """
                UPDATE auditcore.audit_findings
                SET finding_status = 'RESOLVED', disposition = 'FIXED',
                    resolved_at_utc = now(), updated_at_utc = now()
                WHERE tenant_id = :tenant_id AND audit_finding_id = :fid
                  AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
                """
            ),
            {"tenant_id": tenant_id, "fid": finding_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_finding_events (
                    tenant_id, audit_finding_id, journey_id, stage_code,
                    event_type, actor_id, actor_role_snapshot, safe_payload, correlation_id
                ) VALUES (
                    :tenant_id, :fid, :journey_id, :stage,
                    'RESOLVED', :actor_id, :actor_role, CAST(:payload AS jsonb), :correlation_id
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "fid": finding_id,
                "journey_id": journey_id,
                "stage": _STAGE,
                "actor_id": actor_id,
                "actor_role": "HUMAN" if actor_id else "SYSTEM",
                "payload": json.dumps({"disposition": "FIXED", "note": "Model resolved to a single SKU."}),
                "correlation_id": correlation_id,
            },
        )
    return len(open_ids)


def _run_deal_reconciliation(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str
) -> None:
    """Materialise price + discount standards for a resolved SKU (never raises)."""
    from audit_core.uc03_deal_reconciliation import sync_deal_reconciliation

    sync_deal_reconciliation(
        connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
    )


def _attribute_decomposition_fallback(
    connection: Connection, *, tenant_id: str, rows: list[dict[str, Any]], inputs: dict[str, Any]
) -> tuple[list[dict[str, Any]], str, bool]:
    """Narrow by decomposed fuel/transmission/drive/seater signal, tried
    whether or not the whole-string model match already succeeded.

    Booking Forms fold trim/fuel/transmission/drive/seater into the same
    free-text field as the model name (see ``uc03_model_attribute_matching``);
    resolve the model via ``oem_model_aliases`` and re-filter by the variant's
    own structured attributes instead of comparing one flat string. Still
    zero fuzzy text matching — every check is exact, on a decomposed signal.
    Returns ``([], "NONE", False)`` (the caller's existing empty case) when
    this OEM has no alias/vocabulary coverage or nothing survives the
    filters.

    The third element is whether the result is trustworthy enough to
    out-rank an exact price match on its own: only when the starting
    candidate set actually had more than one row (real ambiguity to
    resolve) *and* the combined text supplied a genuine fuel/transmission/
    drive/seater signal (``has_qualifying_signal``) -- not a bare trim code,
    which only "matches" via a loose prefix/suffix check because there was
    nothing else in the candidate set to eliminate it against, and carries
    no more certainty than a plain name/variant equality check.
    """
    oem_code = _oem_code_for_tenant(connection, tenant_id=tenant_id)
    if not oem_code:
        return [], "NONE", False

    aliases = _oem_model_aliases(connection, oem_code=oem_code)
    resolved = resolve_model_via_aliases(model_name=inputs["model_name"], oem_aliases=aliases)
    if resolved is None:
        return [], "NONE", False

    canonical_model, remainder = resolved
    model_rows = [r for r in rows if r["model_name"] == canonical_model]
    if not model_rows:
        return [], "NONE", False

    matched = match_by_attributes(
        model_rows,
        oem_code=oem_code,
        model_remainder=remainder,
        variant_text=inputs["variant_name"],
    )
    trustworthy = len(model_rows) > 1 and has_qualifying_signal(
        oem_code=oem_code, model_remainder=remainder, variant_text=inputs["variant_name"]
    )
    return matched, "ATTRIBUTE_DECOMPOSITION", trustworthy


def _latest_invoice_field(
    connection: Connection, *, tenant_id: str, journey_id: UUID, field_keys: tuple[str, ...]
) -> str | None:
    """Highest-confidence, most-recent value for ``field_keys`` off any Delivery
    invoice document, read from durable storage (never DI directly)."""
    value = connection.execute(
        text(
            """
            SELECT effective_value
            FROM auditcore.journey_document_extracted_fields
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND stage_code = 'DELIVERY'
              AND source_document_type_key = ANY(:document_types)
              AND field_key = ANY(:field_keys)
              AND effective_value IS NOT NULL
            ORDER BY confidence_score DESC NULLS LAST, updated_at_utc DESC
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_types": list(_DELIVERY_INVOICE_DOCUMENT_TYPES),
            "field_keys": list(field_keys),
        },
    ).scalar_one_or_none()
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


# ── producer ──────────────────────────────────────────────────────────────────
def sync_model_resolution_from_invoice(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str
) -> dict[str, Any]:
    """Delivery-side fallback for a Booking that never pinned a SKU.

    ``sync_model_resolution`` already covers the Booking Form, including using
    its ex-showroom price to break a tie when the model text alone matches
    more than one price-master SKU. When that still leaves no SKU pinned by
    the time a Delivery invoice is confirmed, this reads the invoice's own
    ``sku_code`` (an explicit master code, when the invoice prints one -- the
    strongest possible signal) or, failing that, its ``model_name_raw`` /
    ``variant_raw`` free text, and resolves against the same price masters
    the Booking resolver uses. Idempotent, self-heals on read, never raises;
    a no-op once a SKU is already pinned, by either path.
    """
    try:
        already_resolved = connection.execute(
            text(
                """
                SELECT product_sku_id FROM auditcore.journey_products
                WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one_or_none()
        if already_resolved is not None:
            return {"skipped": True, "reason": "already_resolved"}

        sku_code = _latest_invoice_field(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            field_keys=_INVOICE_SKU_CODE_FIELD_KEYS,
        )
        invoice_model = _latest_invoice_field(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            field_keys=_INVOICE_MODEL_FIELD_KEYS,
        )
        if sku_code is None and invoice_model is None:
            return {"skipped": True, "reason": "no_invoice_model_data"}

        # require_model=False: this function's whole purpose is resolving a
        # SKU when the Booking side never captured a usable model at all --
        # requiring one here first would defeat it. See _resolution_inputs'
        # own docstring for the bug this fixes.
        inputs = _resolution_inputs(
            connection, tenant_id=tenant_id, journey_id=journey_id, require_model=False
        )
        if inputs is None:
            return {"skipped": True, "reason": "no_booking_snapshot"}

        effective_on = date.fromisoformat(
            connection.execute(
                text(
                    """
                    SELECT COALESCE(b.booking_date, CURRENT_DATE)
                    FROM auditcore.journeys j
                    LEFT JOIN auditcore.bookings b
                      ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
                    WHERE j.tenant_id = :tenant_id AND j.journey_id = :journey_id
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id},
            ).scalar_one().isoformat()
        )
        try:
            plan = _price_plan_for_journey(
                connection, tenant_id=tenant_id, journey_id=journey_id, effective_on=effective_on
            )
        except Exception:  # noqa: BLE001 - no effective price list yet
            return {"skipped": True, "reason": "no_effective_price_list"}

        rows = _sku_rows_for_version(
            connection, tenant_id=tenant_id, price_list_version_id=plan["price_list_version_id"]
        )
        if not rows:
            return {"skipped": True, "reason": "empty_price_list"}

        matched: list[dict[str, Any]] = []
        stage = "NONE"
        if sku_code is not None:
            matched = [
                r for r in rows if str(r["sku_code"]).strip().casefold() == sku_code.casefold()
            ]
            stage = "INVOICE_SKU_CODE"

        if len(matched) != 1 and invoice_model is not None:
            invoice_variant = _latest_invoice_field(
                connection, tenant_id=tenant_id, journey_id=journey_id,
                field_keys=_INVOICE_VARIANT_FIELD_KEYS,
            )
            invoice_inputs = dict(inputs)
            invoice_inputs["model_name"] = invoice_model
            invoice_inputs["variant_name"] = invoice_variant or inputs["variant_name"]
            matched, stage = _match(rows, invoice_inputs)

        if len(matched) == 1:
            _pin_sku(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                product_sku_id=matched[0]["product_sku_id"],
            )
            _resolve_open_flag(
                connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
            )
            _run_deal_reconciliation(
                connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
            )
            return {"resolved": True, "skuCode": matched[0]["sku_code"], "matchStage": stage}

        # Deliberately does not raise MODEL_NOT_IDENTIFIED here -- this is an
        # extra chance to resolve, not a new alerting path. If Booking already
        # raised the finding, it stays open until a human resolves it.
        return {"skipped": True, "reason": "unresolved_from_invoice", "matchStage": stage, "candidateCount": len(matched)}
    except Exception:
        logger.warning("sync_model_resolution_from_invoice failed", exc_info=True)
        return {"error": True}


def _current_match(
    connection: Connection, *, tenant_id: str, journey_id: UUID, inputs: dict[str, Any]
) -> dict[str, Any]:
    """Compute today's candidate SKU rows for a journey's reviewed model text
    against the tenant's current effective price list.

    Returns ``{"skipped": True, "reason": ...}`` when there is nothing to
    compute against (no effective price list, or an empty one), else
    ``{"matched": [...], "matchStage": ...}``. Shared by ``sync_model_
    resolution`` (which pins/raises from this) and the read-only candidates
    endpoint (``get_model_resolution_candidates``, which never mutates
    anything) -- one matching implementation, not two that could drift.
    """
    effective_on = date.fromisoformat(
        connection.execute(
            text(
                """
                SELECT COALESCE(b.booking_date, CURRENT_DATE)
                FROM auditcore.journeys j
                LEFT JOIN auditcore.bookings b
                  ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
                WHERE j.tenant_id = :tenant_id AND j.journey_id = :journey_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one().isoformat()
    )
    try:
        plan = _price_plan_for_journey(
            connection, tenant_id=tenant_id, journey_id=journey_id, effective_on=effective_on
        )
    except Exception:  # noqa: BLE001 - no effective price list yet
        return {"skipped": True, "reason": "no_effective_price_list"}

    rows = _sku_rows_for_version(
        connection, tenant_id=tenant_id, price_list_version_id=plan["price_list_version_id"]
    )
    if not rows:
        return {"skipped": True, "reason": "empty_price_list"}

    # Attribute decomposition first, always attempted -- not only once a
    # price-based match has already failed. A fuel/transmission/drive/
    # seater token the Booking Form actually states is a literal fact
    # about the specific vehicle; price is tried only afterward, as the
    # deliberate last resort, because it can coincidentally collide
    # between unrelated SKUs and can legitimately drift for reasons that
    # have nothing to do with which vehicle was actually sold (accessories,
    # discounts, rounding). Only trusted to decide on its own when it
    # actually resolved real ambiguity with a genuine signal -- see
    # ``_attribute_decomposition_fallback``'s own docstring for why a bare
    # trim-code match doesn't qualify.
    attr_matched, attr_stage, attr_trustworthy = _attribute_decomposition_fallback(
        connection, tenant_id=tenant_id, rows=rows, inputs=inputs
    )
    if len(attr_matched) == 1 and attr_trustworthy:
        return {"matched": attr_matched, "matchStage": attr_stage}

    if attr_trustworthy:
        # attr_matched genuinely narrowed a real ambiguity using a stated
        # fuel/transmission/drive/seater fact (len > 1 here, since the ==1
        # case already returned above) -- price arbitrates only *within*
        # it, never a wider, independent search that could contradict a
        # signal already confirmed as reliable.
        price_matched, price_stage = _price_disambiguate(attr_matched, inputs)
        if len(price_matched) == 1:
            return {"matched": price_matched, "matchStage": price_stage}
        if len(price_matched) < len(attr_matched):
            return {"matched": price_matched, "matchStage": price_stage}
        return {"matched": attr_matched, "matchStage": attr_stage}

    # No trustworthy attribute signal at all (no OEM vocabulary, no
    # qualifying token, or too little starting ambiguity to mean anything)
    # -- price is the deciding mechanism, exactly as before, including the
    # generation-refresh price bridge (see _generation_sibling_rows) for a
    # nameplate that never matched literally in the first place.
    matched, stage = _match(rows, inputs)
    if len(matched) == 1:
        return {"matched": matched, "matchStage": stage}

    # Neither pass reached exactly one on its own -- report whichever
    # leaves the smaller, more defensible shortlist.
    if attr_matched and (not matched or len(attr_matched) < len(matched)):
        matched, stage = attr_matched, attr_stage

    return {"matched": matched, "matchStage": stage}


def _format_candidate_line(row: dict[str, Any], basis: str) -> str:
    label = str(row["model_name"])
    if row.get("variant_name"):
        label += f" {row['variant_name']}"
    if row.get("colour_name"):
        label += f" ({row['colour_name']})"
    ex_showroom = _to_decimal(row.get("master_ex_showroom"))
    total = _master_total(row, basis)
    price_bits = []
    if ex_showroom is not None:
        price_bits.append(f"ex-showroom {ex_showroom:,.2f}")
    if total is not None:
        price_bits.append(f"on-road total {total:,.2f}")
    price_text = f" — {', '.join(price_bits)}" if price_bits else ""
    return f"{row['sku_code']} · {label}{price_text}"


def _candidate_task_payload(
    inputs: dict[str, Any], matched: list[dict[str, Any]], *, multiple: bool
) -> dict[str, Any]:
    """The PC-facing shortlist for the auto-spawned Task: business-readable
    comment text naming each candidate model/variant/colour with its
    ex-showroom price, plus the same list structured for a UI picker."""
    basis = inputs["registration_basis"]
    shortlist = matched[:5]
    if multiple:
        intro = (
            f"The booking model text matched {len(matched)} possible vehicle SKUs and "
            "could not be narrowed to one automatically. Open the scanned Booking Form "
            "on the Journey Documents page and select the SKU that matches it:"
        )
    else:
        intro = (
            "The booking model text did not match any SKU in the current price masters. "
            "Open the scanned Booking Form on the Journey Documents page, check the exact "
            "model/variant/colour printed on it, and select the matching SKU there."
        )
    lines = [f"{i}. {_format_candidate_line(r, basis)}" for i, r in enumerate(shortlist, start=1)]
    comment = intro if not lines else intro + "\n" + "\n".join(lines)
    return {
        "comment": comment,
        "candidates": [
            {
                "productSkuId": str(r["product_sku_id"]),
                "skuCode": r["sku_code"],
                "modelName": r["model_name"],
                "variantName": r["variant_name"],
                "colourName": r["colour_name"],
                "exShowroomPrice": (
                    str(_to_decimal(r.get("master_ex_showroom")))
                    if _to_decimal(r.get("master_ex_showroom")) is not None
                    else None
                ),
                "totalPrice": str(_master_total(r, basis)) if _master_total(r, basis) is not None else None,
            }
            for r in shortlist
        ],
    }


def get_model_resolution_candidates(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> dict[str, Any]:
    """Read-only: today's shortlist for an open MODEL_NOT_IDENTIFIED gap,
    computed fresh against the tenant's current effective price list (never
    a stale snapshot from whenever the finding/Task was originally raised)."""
    finding_id = connection.execute(
        text(
            """
            SELECT audit_finding_id
            FROM auditcore.audit_findings
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND finding_type_code = :ft
              AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
            ORDER BY created_at_utc DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "ft": _FINDING_TYPE},
    ).scalar_one_or_none()
    if finding_id is None:
        raise NotFoundError(
            error_code="VAC-NF-010",
            title="No open model-resolution gap",
            detail="There is no open vehicle-model gap to resolve for this Journey.",
        )

    inputs = _resolution_inputs(connection, tenant_id=tenant_id, journey_id=journey_id)
    if inputs is None:
        raise NotFoundError(
            error_code="VAC-NF-010",
            title="No reviewed booking model",
            detail="The Booking Form has not been reviewed yet -- nothing to shortlist.",
        )

    outcome = _current_match(connection, tenant_id=tenant_id, journey_id=journey_id, inputs=inputs)
    matched = outcome.get("matched", [])
    stage = outcome.get("matchStage", "NONE")
    basis = inputs["registration_basis"]
    return {
        "journeyId": journey_id,
        "findingId": finding_id,
        "reviewedModelName": inputs["model_name"],
        "reviewedVariantName": inputs["variant_name"],
        "reviewedColourName": inputs["colour_name"],
        "matchStage": stage,
        "candidates": [
            {
                "productSkuId": r["product_sku_id"],
                "skuCode": r["sku_code"],
                "modelName": r["model_name"],
                "variantName": r["variant_name"],
                "colourName": r["colour_name"],
                "exShowroomPrice": _to_decimal(r.get("master_ex_showroom")),
                "totalPrice": _master_total(r, basis),
            }
            for r in matched[:10]
        ],
    }


def confirm_model_resolution_sku(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    product_sku_id: UUID,
    actor_id: str,
    correlation_id: str,
) -> dict[str, Any]:
    """A PC's manual pick when MODEL_NOT_IDENTIFIED could not auto-resolve.

    Validates the chosen SKU against the same tenant-scoped, currently-
    effective price-list rows the shortlist itself is built from (never
    trusts a client-supplied id blindly), pins it, resolves the open
    finding, completes its linked Task, and runs deal reconciliation -- the
    same closing sequence an automatic match runs.
    """
    inputs = _resolution_inputs(connection, tenant_id=tenant_id, journey_id=journey_id)
    if inputs is None:
        raise NotFoundError(
            error_code="VAC-NF-010",
            title="No reviewed booking model",
            detail="The Booking Form has not been reviewed yet -- nothing to confirm.",
        )

    effective_on = date.fromisoformat(
        connection.execute(
            text(
                """
                SELECT COALESCE(b.booking_date, CURRENT_DATE)
                FROM auditcore.journeys j
                LEFT JOIN auditcore.bookings b
                  ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
                WHERE j.tenant_id = :tenant_id AND j.journey_id = :journey_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one().isoformat()
    )
    try:
        plan = _price_plan_for_journey(
            connection, tenant_id=tenant_id, journey_id=journey_id, effective_on=effective_on
        )
    except Exception as exc:
        raise AuditCoreError(
            error_code="VAC-SKU-003",
            status_code=422,
            title="No effective price list",
            detail="There is no effective price list for this Journey to confirm a SKU against.",
        ) from exc

    rows = _sku_rows_for_version(
        connection, tenant_id=tenant_id, price_list_version_id=plan["price_list_version_id"]
    )
    row = next((r for r in rows if str(r["product_sku_id"]) == str(product_sku_id)), None)
    if row is None:
        raise AuditCoreError(
            error_code="VAC-SKU-002",
            status_code=422,
            title="Unknown or inactive SKU",
            detail="The selected SKU is not in this Journey's current effective price list.",
        )

    _pin_sku(connection, tenant_id=tenant_id, journey_id=journey_id, product_sku_id=product_sku_id)
    resolved = _resolve_open_flag(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        correlation_id=correlation_id,
        actor_id=actor_id,
    )
    _run_deal_reconciliation(
        connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
    )
    return {
        "resolved": True,
        "flagsResolved": resolved,
        "productSkuId": row["product_sku_id"],
        "skuCode": row["sku_code"],
        "modelName": row["model_name"],
        "variantName": row["variant_name"],
        "colourName": row["colour_name"],
    }


router = APIRouter(
    prefix="/v2/tenants/{tenant_id}/journeys/{journey_id}/booking/model-resolution",
    tags=["uc03-model-resolution"],
)


class ModelResolutionCandidateOut(BaseModel):
    productSkuId: UUID
    skuCode: str
    modelName: str
    variantName: str | None = None
    colourName: str | None = None
    exShowroomPrice: Decimal | None = None
    totalPrice: Decimal | None = None


class ModelResolutionCandidatesResponse(BaseModel):
    journeyId: UUID
    findingId: UUID
    reviewedModelName: str | None = None
    reviewedVariantName: str | None = None
    reviewedColourName: str | None = None
    matchStage: str
    candidates: list[ModelResolutionCandidateOut]


class ConfirmModelResolutionSkuRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    productSkuId: UUID


class ConfirmModelResolutionSkuResponse(BaseModel):
    journeyId: UUID
    productSkuId: UUID
    skuCode: str
    modelName: str
    variantName: str | None = None
    colourName: str | None = None
    flagsResolved: int


@router.get("", response_model=ModelResolutionCandidatesResponse)
def read_model_resolution_candidates(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ModelResolutionCandidatesResponse:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    return ModelResolutionCandidatesResponse(
        **get_model_resolution_candidates(connection, tenant_id=tenant_id, journey_id=journey_id)
    )


@router.post("/confirm-sku", response_model=ConfirmModelResolutionSkuResponse)
def confirm_model_resolution_sku_endpoint(
    tenant_id: str,
    journey_id: UUID,
    payload: ConfirmModelResolutionSkuRequest,
    request: Request,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ConfirmModelResolutionSkuResponse:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    result = confirm_model_resolution_sku(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        product_sku_id=payload.productSkuId,
        actor_id=human_principal.subject,
        correlation_id=get_correlation_id(request),
    )
    return ConfirmModelResolutionSkuResponse(
        journeyId=journey_id,
        productSkuId=result["productSkuId"],
        skuCode=result["skuCode"],
        modelName=result["modelName"],
        variantName=result["variantName"],
        colourName=result["colourName"],
        flagsResolved=result["flagsResolved"],
    )


def sync_model_resolution(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str
) -> dict[str, Any]:
    """Resolve the SKU or raise MODEL_NOT_IDENTIFIED. Idempotent; never raises."""
    try:
        inputs = _resolution_inputs(connection, tenant_id=tenant_id, journey_id=journey_id)
        if inputs is None:
            return {"skipped": True}

        if inputs["product_sku_id"] is not None:
            resolved = _resolve_open_flag(
                connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
            )
            _run_deal_reconciliation(
                connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
            )
            return {"resolved": True, "flagsResolved": resolved}

        outcome = _current_match(connection, tenant_id=tenant_id, journey_id=journey_id, inputs=inputs)
        if outcome.get("skipped"):
            return outcome
        matched, stage = outcome["matched"], outcome["matchStage"]

        if len(matched) == 1:
            _pin_sku(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                product_sku_id=matched[0]["product_sku_id"],
            )
            _resolve_open_flag(
                connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
            )
            _run_deal_reconciliation(
                connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
            )
            return {"resolved": True, "skuCode": matched[0]["sku_code"], "matchStage": stage}

        multiple = len(matched) > 1
        _machine_flag(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=_STAGE,
            rule_key=_RULE_KEY,
            finding_type=_FINDING_TYPE,
            severity="MEDIUM",
            title=(
                "Vehicle model matched multiple price-master SKUs"
                if multiple
                else "Vehicle model could not be matched to the price masters"
            ),
            description=(
                f"Booking model '{inputs['model_name']}'"
                + (f" / '{inputs['variant_name']}'" if inputs["variant_name"] else "")
                + (
                    f" matched {len(matched)} price-master SKUs"
                    if multiple
                    else " did not match any price-master SKU"
                )
                + " on model, on-road total or ex-showroom price. Confirm the model so "
                "the deal can be checked against the price and discount masters."
            ),
            correlation_id=correlation_id,
            safe_payload={
                "modelName": inputs["model_name"],
                "variantName": inputs["variant_name"],
                "colourName": inputs["colour_name"],
                "offeredTotal": str(inputs["offered_total"]) if inputs["offered_total"] is not None else None,
                "offeredExShowroom": (
                    str(inputs["offered_ex_showroom"]) if inputs["offered_ex_showroom"] is not None else None
                ),
                "matchStage": stage,
                "candidateCount": len(matched),
                "candidates": [
                    {
                        "skuCode": r["sku_code"],
                        "modelName": r["model_name"],
                        "variantName": r["variant_name"],
                        "colourName": r["colour_name"],
                        "masterTotal": (
                            str(_master_total(r, inputs["registration_basis"]))
                            if _master_total(r, inputs["registration_basis"]) is not None
                            else None
                        ),
                        "exShowroom": (
                            str(_to_decimal(r.get("master_ex_showroom")))
                            if _to_decimal(r.get("master_ex_showroom")) is not None
                            else None
                        ),
                    }
                    for r in matched[:10]
                ],
            },
            task_payload_extra=_candidate_task_payload(inputs, matched, multiple=multiple),
        )
        return {"raised": True, "matchStage": stage, "candidateCount": len(matched)}
    except Exception:
        logger.warning("sync_model_resolution failed", exc_info=True)
        return {"error": True}


__all__ = ["sync_model_resolution", "sync_model_resolution_from_invoice"]
