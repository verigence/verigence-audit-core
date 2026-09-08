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
  3. one SKU  -> pin ``journey_products.product_sku_id``, resolve any open flag
  4. zero SKU -> MODEL_NOT_IDENTIFIED "no matching model"
  5. >1  SKU  -> MODEL_NOT_IDENTIFIED "matched multiple models" (+ candidates)

No fuzzy matching, no price tolerance (``_label_similarity`` is normalised
equality only). Idempotent, self-heals on read, never raises.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_delivery_commands import _machine_flag
from audit_core.uc03_masters_alignment import registration_basis
from audit_core.uc03_sku_candidates import (
    _label_similarity,
    _price_plan_for_journey,
)

logger = logging.getLogger(__name__)

_FINDING_TYPE = "MODEL_NOT_IDENTIFIED"
_RULE_KEY = "MODEL_NOT_IDENTIFIED:BOOKING"
_STAGE = "BOOKING"
_SELECTION_METHOD = "MODEL_RESOLUTION_SYNC_V1"
_EX_SHOWROOM_COMPONENT = "EX_SHOWROOM"


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


# ── inputs ────────────────────────────────────────────────────────────────────
def _resolution_inputs(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> dict[str, Any] | None:
    """Reviewed model / offered total / offered ex-showroom for this journey.

    Returns None when there is nothing to resolve yet (no model snapshot).
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
    if jp is None or jp["model_name"] is None:
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

    booking_total = connection.execute(
        text(
            """
            SELECT NULLIF(regexp_replace(COALESCE(total_price::text, ''), '[^0-9.\\-]', '', 'g'), '')
            FROM auditcore.booking_form_review_values
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY reviewed_at_utc DESC NULLS LAST
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()

    offered_total = (
        by_key.get("total_price")
        or _to_decimal(booking_total)
    )
    offered_ex_showroom = by_key.get("ex_showroom_price")

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
    """
    rows = connection.execute(
        text(
            """
            SELECT s.product_sku_id,
                   s.sku_code,
                   pm.model_name,
                   pv.variant_name,
                   c.colour_name,
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
            GROUP BY s.product_sku_id, s.sku_code, pm.model_name, pv.variant_name, c.colour_name
            """
        ),
        {"tenant_id": tenant_id, "plv": price_list_version_id, "exkey": _EX_SHOWROOM_COMPONENT},
    ).mappings().all()
    return [dict(row) for row in rows]


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


def _match(
    rows: list[dict[str, Any]], inputs: dict[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    """Return (matched rows, match_stage). match_stage is TOTAL, EX_SHOWROOM or NONE."""
    model = inputs["model_name"]
    basis = inputs["registration_basis"]
    model_rows = [r for r in rows if _label_similarity(model, str(r["model_name"])) == Decimal(1)]
    if not model_rows:
        return [], "NONE"

    total = inputs["offered_total"]
    if total is not None:
        by_total = [r for r in model_rows if _master_total(r, basis) == total]
        by_total = _narrow(by_total, variant=inputs["variant_name"], colour=inputs["colour_name"])
        if len(by_total) == 1:
            return by_total, "TOTAL"
        if len(by_total) > 1:
            model_rows = by_total  # keep the ambiguity for reporting unless ex-showroom disambiguates

    ex = inputs["offered_ex_showroom"]
    if ex is not None:
        by_ex = [r for r in model_rows if _to_decimal(r["master_ex_showroom"]) == ex]
        by_ex = _narrow(by_ex, variant=inputs["variant_name"], colour=inputs["colour_name"])
        if len(by_ex) == 1:
            return by_ex, "EX_SHOWROOM"
        if len(by_ex) > 1:
            return by_ex, "EX_SHOWROOM"

    return model_rows, "TOTAL" if total is not None else "NONE"


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
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str
) -> int:
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
                    'RESOLVED', NULL, 'SYSTEM', CAST(:payload AS jsonb), :correlation_id
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "fid": finding_id,
                "journey_id": journey_id,
                "stage": _STAGE,
                "payload": json.dumps({"disposition": "FIXED", "note": "Model resolved to a single SKU."}),
                "correlation_id": correlation_id,
            },
        )
    return len(open_ids)


# ── producer ──────────────────────────────────────────────────────────────────
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
            return {"resolved": True, "flagsResolved": resolved}

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

        matched, stage = _match(rows, inputs)

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
            # P2: sync_deal_reconciliation(...) materialises standards here.
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
                    }
                    for r in matched[:10]
                ],
            },
        )
        return {"raised": True, "matchStage": stage, "candidateCount": len(matched)}
    except Exception:
        logger.warning("sync_model_resolution failed", exc_info=True)
        return {"error": True}


__all__ = ["sync_model_resolution"]
