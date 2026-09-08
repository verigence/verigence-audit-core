"""uc03_deal_reconciliation.py — materialise the Mahindra masters for a journey.

Once ``uc03_model_resolution`` has pinned ``journey_products.product_sku_id``,
this fills the *standard* side of the two per-journey reconciliation tables from
the OEM native masters, deterministically:

  commercial_lines.standard_amount
      = the SKU's ``price_list_items`` for the effective published version,
        mapped OEM component_key -> booking component_key via
        ``uc03_masters_alignment`` (registration variant chosen by the buyer's
        basis; the two extended-warranty tiers sum).

  discount_applications  (one row per applicable scheme benefit, keyed by the
                          canonical discount key, actual_source_kind CALCULATED)
      .standard_eligible_amount = the benefit amount from every discount scheme
        the customer is eligible for (``discount_scheme_eligibility`` matched on
        model / variant / customer type, version PUBLISHED and effective on the
        booking date).
      .actual_discount_amount   = the reviewed booking / invoice value for the
        matching discount field (so the panel shows entitled vs given on one row,
        and an eligible-but-unclaimed benefit shows standard with no actual).

Deterministic, idempotent, never raises. Runs from the same producer hook as
``sync_model_resolution`` and self-heals on the overview read.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_masters_alignment import (
    DISCOUNT_ACTUAL_FIELD_TO_BENEFIT_KEY,
    canonical_discount_key,
    commercial_amounts_are_additive,
    commercial_key_for_price_component,
    registration_basis,
)
from audit_core.uc03_sku_candidates import _price_plan_for_journey

logger = logging.getLogger(__name__)

_CALCULATED = "CALCULATED"


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


# ── context ───────────────────────────────────────────────────────────────────
def _context(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any] | None:
    row = connection.execute(
        text(
            """
            SELECT jp.product_sku_id,
                   s.model_id,
                   s.variant_id,
                   COALESCE(b.booking_date, CURRENT_DATE)   AS effective_on,
                   cu.customer_type_code,
                   rr.registration_type_code
            FROM auditcore.journey_products jp
            JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id
            JOIN auditcore.journeys j
              ON j.tenant_id = jp.tenant_id AND j.journey_id = jp.journey_id
            LEFT JOIN auditcore.bookings b
              ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
            LEFT JOIN auditcore.customers cu
              ON cu.tenant_id = j.tenant_id AND cu.customer_id = j.customer_id
            LEFT JOIN auditcore.registration_records rr
              ON rr.tenant_id = j.tenant_id AND rr.journey_id = j.journey_id
            WHERE jp.tenant_id = :tenant_id AND jp.journey_id = :journey_id
              AND jp.product_sku_id IS NOT NULL
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        return None
    data = dict(row)
    data["basis"] = registration_basis(
        customer_type_code=data["customer_type_code"],
        registration_type_code=data["registration_type_code"],
    )
    return data


def _reviewed_booking(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT to_jsonb(v) AS payload
            FROM auditcore.booking_form_review_values v
            WHERE v.tenant_id = :tenant_id AND v.journey_id = :journey_id
            ORDER BY v.reviewed_at_utc DESC NULLS LAST
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()
    return dict(row) if row else {}


# ── price standards ───────────────────────────────────────────────────────────
def _materialize_price_standards(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    product_sku_id: UUID,
    price_list_version_id: UUID,
    basis: str,
) -> int:
    items = connection.execute(
        text(
            """
            SELECT pli.price_list_item_id, pli.component_key, pli.standard_amount,
                   plv.currency_code
            FROM auditcore.price_list_items pli
            JOIN auditcore.price_list_versions plv
              ON plv.tenant_id = pli.tenant_id
             AND plv.price_list_version_id = pli.price_list_version_id
            WHERE pli.tenant_id = :tenant_id
              AND pli.price_list_version_id = :plv
              AND pli.product_sku_id = :sku
            """
        ),
        {"tenant_id": tenant_id, "plv": price_list_version_id, "sku": product_sku_id},
    ).mappings().all()

    # OEM component -> Audit Core commercial key; sum where several fold onto one.
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        commercial_key = commercial_key_for_price_component(item["component_key"], basis=basis)
        if commercial_key is None:
            continue
        amount = _to_decimal(item["standard_amount"])
        if amount is None:
            continue
        entry = grouped.setdefault(
            commercial_key,
            {"amount": Decimal(0), "price_list_item_id": item["price_list_item_id"],
             "currency": str(item["currency_code"] or "INR")},
        )
        if commercial_amounts_are_additive(commercial_key) or entry["amount"] == 0:
            entry["amount"] += amount
        else:
            entry["amount"] = amount
        entry["price_list_item_id"] = item["price_list_item_id"]

    written = 0
    for commercial_key, entry in grouped.items():
        connection.execute(
            text(
                """
                INSERT INTO auditcore.commercial_lines (
                    tenant_id, journey_id, component_key, standard_amount,
                    price_list_item_id, currency_code
                ) VALUES (
                    :tenant_id, :journey_id, :component_key, :standard_amount,
                    :price_list_item_id, :currency_code
                )
                ON CONFLICT (tenant_id, journey_id, component_key) DO UPDATE SET
                    standard_amount = EXCLUDED.standard_amount,
                    price_list_item_id = EXCLUDED.price_list_item_id,
                    currency_code = COALESCE(auditcore.commercial_lines.currency_code, EXCLUDED.currency_code),
                    updated_at_utc = now()
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "component_key": commercial_key,
                "standard_amount": entry["amount"],
                "price_list_item_id": entry["price_list_item_id"],
                "currency_code": entry["currency"],
            },
        )
        written += 1
    return written


# ── discount standards ────────────────────────────────────────────────────────
def _applicable_benefits(
    connection: Connection,
    *,
    tenant_id: str,
    model_id: UUID,
    variant_id: UUID | None,
    effective_on: date,
    basis: str,
) -> list[dict[str, Any]]:
    """Every discount-scheme benefit the customer is eligible for."""
    corporate = basis == "CORPORATE"
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (b.benefit_key, ds.scheme_category)
                   b.benefit_key,
                   b.benefit_type,
                   b.amount_value,
                   b.percentage_value,
                   dsv.discount_scheme_version_id,
                   ds.scheme_code,
                   ds.scheme_category,
                   e.criteria
            FROM auditcore.discount_scheme_eligibility e
            JOIN auditcore.discount_scheme_versions dsv
              ON dsv.tenant_id = e.tenant_id
             AND dsv.discount_scheme_version_id = e.discount_scheme_version_id
            JOIN auditcore.discount_schemes ds
              ON ds.tenant_id = dsv.tenant_id
             AND ds.discount_scheme_id = dsv.discount_scheme_id
            JOIN auditcore.discount_scheme_benefits b
              ON b.tenant_id = dsv.tenant_id
             AND b.discount_scheme_version_id = dsv.discount_scheme_version_id
            WHERE e.tenant_id = :tenant_id
              AND e.model_id = :model_id
              AND (e.variant_id IS NULL OR e.variant_id = :variant_id)
              AND dsv.lifecycle_status = 'PUBLISHED'
              AND dsv.effective_from <= :effective_on
              AND (dsv.effective_to IS NULL OR dsv.effective_to >= :effective_on)
              AND (
                    e.customer_type_code IS NULL
                    OR (:corporate AND e.customer_type_code LIKE 'CORPORATE%')
                    OR (NOT :corporate AND e.customer_type_code NOT LIKE 'CORPORATE%')
              )
            ORDER BY b.benefit_key, ds.scheme_category, dsv.effective_from DESC
            """
        ),
        {
            "tenant_id": tenant_id,
            "model_id": model_id,
            "variant_id": variant_id,
            "effective_on": effective_on,
            "corporate": corporate,
        },
    ).mappings().all()
    return [dict(row) for row in rows]


def _actual_discounts_by_benefit(reviewed_booking: dict[str, Any]) -> dict[str, Decimal]:
    """Reviewed booking discount fields collapsed onto OEM benefit keys."""
    out: dict[str, Decimal] = {}
    for field, benefit_key in DISCOUNT_ACTUAL_FIELD_TO_BENEFIT_KEY.items():
        amount = _to_decimal(reviewed_booking.get(field))
        if amount is None or amount == 0:
            continue
        out[benefit_key] = out.get(benefit_key, Decimal(0)) + amount
    return out


def _materialize_discount_standards(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    benefits: list[dict[str, Any]],
    actuals: dict[str, Decimal],
) -> dict[str, int]:
    seen_keys: set[str] = set()
    eligible = 0
    unclaimed = 0
    for benefit in benefits:
        benefit_key = str(benefit["benefit_key"]).strip().upper()
        canonical = canonical_discount_key(benefit_key)
        if canonical in seen_keys:
            continue
        seen_keys.add(canonical)

        standard = _to_decimal(benefit["amount_value"]) if str(benefit["benefit_type"]).upper() == "AMOUNT" else None
        actual = actuals.get(benefit_key)
        result = "ELIGIBLE" if actual is not None else "ELIGIBLE_UNCLAIMED"
        if actual is None:
            unclaimed += 1
        eligible += 1

        details = json.dumps(
            {
                "origin": "DEAL_RECONCILIATION",
                "schemeCategory": benefit["scheme_category"],
                "schemeCode": benefit["scheme_code"],
                "benefitKey": benefit_key,
                "benefitType": benefit["benefit_type"],
                "percentageValue": (
                    str(benefit["percentage_value"]) if benefit["percentage_value"] is not None else None
                ),
            }
        )
        connection.execute(
            text(
                """
                WITH upd AS (
                    UPDATE auditcore.discount_applications
                    SET standard_eligible_amount = :standard,
                        actual_discount_amount = COALESCE(:actual, actual_discount_amount),
                        discount_scheme_version_id = :dsv,
                        eligibility_result = :result,
                        actual_source_kind = COALESCE(actual_source_kind, :calc),
                        details = CAST(:details AS jsonb),
                        updated_at_utc = now()
                    WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                      AND discount_key = :discount_key
                    RETURNING discount_application_id
                )
                INSERT INTO auditcore.discount_applications (
                    tenant_id, journey_id, discount_key,
                    standard_eligible_amount, actual_discount_amount,
                    discount_scheme_version_id, eligibility_result,
                    actual_source_kind, details
                )
                SELECT :tenant_id, :journey_id, :discount_key,
                       :standard, :actual, :dsv, :result, :calc, CAST(:details AS jsonb)
                WHERE NOT EXISTS (SELECT 1 FROM upd)
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "discount_key": canonical,
                "standard": standard,
                "actual": actual,
                "dsv": benefit["discount_scheme_version_id"],
                "result": result,
                "calc": _CALCULATED,
                "details": details,
            },
        )

    # a discount actually given for which the customer has no scheme entitlement
    # (an over-grant) — record it so the panel can flag it
    over = 0
    for benefit_key, amount in actuals.items():
        canonical = canonical_discount_key(benefit_key)
        if canonical in seen_keys:
            continue
        seen_keys.add(canonical)
        over += 1
        connection.execute(
            text(
                """
                INSERT INTO auditcore.discount_applications (
                    tenant_id, journey_id, discount_key,
                    standard_eligible_amount, actual_discount_amount,
                    eligibility_result, actual_source_kind, details
                )
                SELECT CAST(:tenant_id AS varchar), CAST(:journey_id AS uuid),
                       CAST(:discount_key AS varchar),
                       CAST(NULL AS numeric), CAST(:actual AS numeric),
                       'NOT_ELIGIBLE', CAST(:calc AS varchar),
                       CAST(:details AS jsonb)
                WHERE NOT EXISTS (
                    SELECT 1 FROM auditcore.discount_applications
                    WHERE tenant_id = CAST(:tenant_id AS varchar)
                      AND journey_id = CAST(:journey_id AS uuid)
                      AND discount_key = CAST(:discount_key AS varchar)
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "discount_key": canonical,
                "actual": amount,
                "calc": _CALCULATED,
                "details": json.dumps({"origin": "DEAL_RECONCILIATION", "benefitKey": benefit_key}),
            },
        )
    return {"eligible": eligible, "unclaimed": unclaimed, "overGranted": over}


# ── producer ──────────────────────────────────────────────────────────────────
def sync_deal_reconciliation(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str
) -> dict[str, Any]:
    """Materialise price + discount standards from the OEM masters. Never raises."""
    try:
        ctx = _context(connection, tenant_id=tenant_id, journey_id=journey_id)
        if ctx is None:
            return {"skipped": True, "reason": "no_resolved_sku"}

        effective_on = ctx["effective_on"]
        if not isinstance(effective_on, date):
            effective_on = date.fromisoformat(str(effective_on))

        try:
            plan = _price_plan_for_journey(
                connection, tenant_id=tenant_id, journey_id=journey_id, effective_on=effective_on
            )
        except Exception:  # noqa: BLE001 - no effective price list yet
            return {"skipped": True, "reason": "no_effective_price_list"}

        price_lines = _materialize_price_standards(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            product_sku_id=ctx["product_sku_id"],
            price_list_version_id=plan["price_list_version_id"],
            basis=ctx["basis"],
        )

        benefits = _applicable_benefits(
            connection,
            tenant_id=tenant_id,
            model_id=ctx["model_id"],
            variant_id=ctx["variant_id"],
            effective_on=effective_on,
            basis=ctx["basis"],
        )
        actuals = _actual_discounts_by_benefit(
            _reviewed_booking(connection, tenant_id=tenant_id, journey_id=journey_id)
        )
        discount = _materialize_discount_standards(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            benefits=benefits,
            actuals=actuals,
        )
        return {"priceLines": price_lines, **discount}
    except Exception:
        logger.warning("sync_deal_reconciliation failed", exc_info=True)
        return {"error": True}


__all__ = ["sync_deal_reconciliation"]
