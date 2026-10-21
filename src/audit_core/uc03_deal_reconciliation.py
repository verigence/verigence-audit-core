"""uc03_deal_reconciliation.py — materialise the Mahindra masters for a journey.

Once ``uc03_model_resolution`` has pinned ``journey_products.product_sku_id``,
this fills the *standard* side of the two per-journey reconciliation tables from
the OEM native masters, deterministically:

  commercial_lines.standard_amount
      = the SKU's ``price_list_items`` for the effective published version,
        mapped OEM component_key -> booking component_key via
        ``uc03_masters_alignment`` (registration variant chosen by the buyer's
        basis; the two extended-warranty tiers both map to one line but are
        never summed -- a customer takes at most one tier, so whichever
        tier's price matches the already-invoice-prioritized actual amount
        on that line wins, falling back to a deterministic pick until an
        actual is known).

  discount_applications  (one row per canonical discount key, actual_source_kind
                          CALCULATED unless an invoice / the booking form wrote it)
      .standard_eligible_amount = the one benefit per key the deal is entitled
        to on its pricing date (2026-10-20 product owner rules): the newest
        version of each scheme in force on the date, the most specific
        eligibility (SKU, variant, outlet / dealer, a met condition) winning
        deterministically, exclusions and old-vehicle conditions honoured.
        Exchange / scrappage / welcome / corporate count only when claimed or
        proven -- otherwise they are shown for information with no standard.
        The dealer's own discount is entitled up to the dealer discount grid's
        buffer; Management Referral up to what a Team Lead approved.
      .actual_discount_amount   = the reviewed booking / invoice value for the
        matching discount field (so the panel shows entitled vs given on one row).
  Standards this run no longer produces are cleared on both tables (the
  actuals stay), so a corrected master or a re-pinned SKU never leaves an old
  entitlement or price standing.

Deterministic, idempotent, never raises. Runs from the same producer hook as
``sync_model_resolution`` and self-heals on the overview read.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.errors import AuditCoreError
from audit_core.oem_master_parsers import variant_excluded
from audit_core.uc03_booking_confirmation_rules import _has_active_document
from audit_core.uc03_delivery_commands import _machine_flag
from audit_core.uc03_manual_verification import _resolve_finding
from audit_core.uc03_masters_alignment import (
    CANONICAL_DISCOUNT_KEYS,
    CONDITIONAL_DISCOUNT_EVIDENCE_DOCUMENT,
    CONDITIONAL_DISCOUNT_LABEL,
    DISCOUNT_ACTUAL_FIELD_TO_BENEFIT_KEY,
    canonical_discount_key,
    commercial_key_for_price_component,
    registration_basis,
)
from audit_core.uc03_p2_runtime import record_activity
from audit_core.uc03_sku_candidates import (
    _price_plan_for_journey,
    journey_pricing_date,
    sync_price_master_in_force_task,
    sync_sku_in_price_master_task,
)
from audit_core.workflow import create_workflow_task

logger = logging.getLogger(__name__)

_CALCULATED = "CALCULATED"
_DISCOUNT_EVIDENCE_FINDING_TYPE = "DOCUMENT_EXCEPTION"

# Total Standard vs Total Actual (net of discounts) across the whole deal.
# COMMERCIAL_EXCEPTION already classifies VIOLATION via
# uc03_finding_routing._VIOLATION_TYPES -- no rule_key prefix registration
# needed, same as BK_MIN_BOOKING_AMOUNT_NOT_MET.
_TOTAL_VARIANCE_FINDING_TYPE = "COMMERCIAL_EXCEPTION"
_TOTAL_VARIANCE_RULE_KEY = "DEAL_TOTAL_VARIANCE_NEGATIVE"
_TOTAL_VARIANCE_WORKFLOW_TYPE = "UC03_DEAL_TOTAL_VARIANCE"
_TOTAL_VARIANCE_TASK_TYPE = "DEAL_TOTAL_VARIANCE_REVIEW"


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
                   pv.variant_name,
                   pm.model_name,
                   jp.colour_code_snapshot,
                   jp.colour_name_snapshot,
                   j.dealer_id,
                   j.outlet_id,
                   b.pricing_effective_on,
                   b.booking_date AS manual_booking_date,
                   cu.customer_type_code,
                   cu.display_name AS customer_name,
                   rr.registration_type_code,
                   o.oem_code
            FROM auditcore.journey_products jp
            JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id
            LEFT JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
            LEFT JOIN auditcore.product_models pm ON pm.model_id = s.model_id
            JOIN auditcore.journeys j
              ON j.tenant_id = jp.tenant_id AND j.journey_id = jp.journey_id
            LEFT JOIN auditcore.projects p ON p.tenant_id = j.tenant_id
            LEFT JOIN auditcore.oems o ON o.oem_id = p.oem_id
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
    # One function for the pricing date everywhere (2026-10-20 product owner
    # rules): applied date, else booking-form date, else manual booking date.
    data["effective_on"] = journey_pricing_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    data.pop("pricing_effective_on", None)
    data.pop("manual_booking_date", None)
    data["colour"] = data.get("colour_code_snapshot") or data.get("colour_name_snapshot")
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
    correlation_id: str = "",
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
            ORDER BY pli.component_key
            """
        ),
        {"tenant_id": tenant_id, "plv": price_list_version_id, "sku": product_sku_id},
    ).mappings().all()

    # OEM component -> Audit Core commercial key. commercial_amounts_are_
    # additive is always False today (see its own docstring: the two
    # extended-warranty tiers used to be summed here, which the user
    # corrected -- a customer takes at most one tier, never both).
    #
    # Direct user instruction (2026-09-23): when several OEM rows map to
    # the same commercial_key (today, only the two warranty tiers), pick
    # whichever tier's master price matches the amount already recorded
    # as this journey's actual for that commercial_key -- invoice already
    # wins over booking form there (commercial_lines.actual_amount is
    # populated by _upsert_commercial_line's own source_priority ranking,
    # _COMMERCIAL_SOURCES = invoice > deal sheet > booking form; nothing
    # new needed here, just read what it already resolved). Falls back to
    # the last candidate (by the deterministic ORDER BY above) only when
    # no actual is known yet -- self-corrects on the next reconciliation
    # pass once one is. Never a PC-editable choice: purely a match against
    # already-extracted evidence.
    candidates: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        commercial_key = commercial_key_for_price_component(item["component_key"], basis=basis)
        if commercial_key is None:
            continue
        amount = _to_decimal(item["standard_amount"])
        if amount is None:
            continue
        candidates.setdefault(commercial_key, []).append(
            {"amount": amount, "price_list_item_id": item["price_list_item_id"],
             "currency": str(item["currency_code"] or "INR")},
        )

    actual_amounts: dict[str, Decimal] = {}
    if any(len(rows) > 1 for rows in candidates.values()):
        actual_rows = connection.execute(
            text(
                """
                SELECT component_key, actual_amount FROM auditcore.commercial_lines
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND component_key = ANY(:keys) AND actual_amount IS NOT NULL
                """
            ),
            {
                "tenant_id": tenant_id, "journey_id": journey_id,
                "keys": [key for key, rows in candidates.items() if len(rows) > 1],
            },
        ).mappings().all()
        actual_amounts = {row["component_key"]: _to_decimal(row["actual_amount"]) for row in actual_rows}

    grouped: dict[str, dict[str, Any]] = {}
    for commercial_key, rows in candidates.items():
        actual = actual_amounts.get(commercial_key)
        if actual is None or actual == 0:
            # No actual known yet (or a genuinely zero one, which a percentage
            # tolerance can't meaningfully apply to) -- deterministic pick by
            # the ORDER BY above, self-corrects once an actual exists.
            chosen = rows[-1]
        else:
            # Direct user correction (2026-09-23): always select the tier
            # whose price is NEAREST the actual amount -- never leave it on
            # an arbitrary deterministic pick just because nothing matched
            # exactly (the previous version required an exact ±₹1 match,
            # far stricter than the OCR/rounding noise between an invoice's
            # printed total and the master's own paisa-precise price
            # actually warrants, and silently fell back to an unrelated
            # candidate whenever that failed). With only two tiers today,
            # "nearest" always resolves unambiguously with no threshold
            # needed; it self-corrects on the next pass if a better actual
            # amount becomes known.
            chosen = min(rows, key=lambda row: abs(row["amount"] - actual))
        grouped[commercial_key] = chosen

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
    _clear_stale_price_standards(
        connection, tenant_id=tenant_id, journey_id=journey_id, produced=set(grouped), correlation_id=correlation_id,
    )
    return written


def _clear_stale_price_standards(
    connection: Connection, *, tenant_id: str, journey_id: UUID, produced: set[str], correlation_id: str = "",
) -> list[str]:
    """2026-10-20 product owner rules: a price-master standard this run did
    not produce (the SKU or the price version changed, the component is not
    in it) is reset -- standard_amount and price_list_item_id -- so it never
    stands as the comparison for the deal. Only standards that came from a
    price list (price_list_item_id set) are touched; actuals never are."""
    rows = connection.execute(
        text(
            """
            UPDATE auditcore.commercial_lines
            SET standard_amount = NULL, price_list_item_id = NULL, updated_at_utc = now()
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND price_list_item_id IS NOT NULL
              AND NOT (component_key = ANY(:produced))
            RETURNING component_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "produced": sorted(produced)},
    ).scalars().all()
    cleared = sorted(str(k) for k in rows)
    if cleared:
        logger.info("deal_reconciliation_stale_prices_cleared journey_id=%s keys=%s", journey_id, cleared)
        _note_cleared(connection, tenant_id=tenant_id, journey_id=journey_id, kind="PRICE", keys=cleared,
                      correlation_id=correlation_id)
    return cleared


# ── discount standards ────────────────────────────────────────────────────────
# 2026-10-20 product owner rules. Exchange, scrappage (through a dealer or with
# a COD), the welcome bonus and corporate privilege are owed only when the deal
# claims them or proves them (old vehicle RC / COD / a corporate on the
# registry). Unclaimed and unproven, they are shown for information and never
# counted in a standard. The consumer offer (cash, accessories, EW, insurance)
# is owed to every buyer of the variant.
_CONDITIONAL_KEYS = frozenset(
    {"EXCHANGE_BONUS", "SCRAPPAGE_BONUS_DEALER", "SCRAPPAGE_BONUS_COD", "WELCOME_BONUS", "CORPORATE_PRIVILEGE"}
)
_DEALER_DISCOUNT_KEY = "ADDITIONAL_DISCOUNT"
# An invoice discount the printed words do not classify (uc03_invoice_materialization).
_UNCLASSIFIED_KEY = "UNCLASSIFIED_DISCOUNT"
_MR_KEY = "MANAGEMENT_REFERRAL"
# Every key this module owns on discount_applications. A legacy key written by
# the older booking-form materialiser (SALES, BUFFER, TOTAL ...) is never
# touched here and never counted in a total.
_RECONCILED_KEYS = frozenset(CANONICAL_DISCOUNT_KEYS | {_MR_KEY})

_OLD_VEHICLE_DOCUMENTS = ("vehicle_rc", "valuation_report", "scrappage_certificate_of_deposit")
_OLD_VEHICLE_MAKE_FIELDS = ("old_vehicle_make", "make", "maker", "maker_name", "manufacturer", "vehicle_make")
_OLD_VEHICLE_MODEL_FIELDS = ("old_vehicle_model", "model", "vehicle_model", "maker_model")


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _old_vehicle_facts(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, str]]:
    """The old vehicle's make and model as the RC, the valuation report or the
    scrappage COD on file print them (one entry per document)."""
    rows = connection.execute(
        text(
            """
            SELECT e.di_document_id, f.field_key, f.effective_value
            FROM auditcore.evidence e
            JOIN auditcore.journey_document_extracted_fields f
              ON f.tenant_id = e.tenant_id AND f.journey_id = e.journey_id AND f.di_document_id = e.di_document_id
            WHERE e.tenant_id = :t AND e.journey_id = :j AND e.association_status = 'ACTIVE'
              AND e.document_type_key = ANY(:types)
              AND f.field_key = ANY(:fields)
            """
        ),
        {"t": tenant_id, "j": journey_id, "types": list(_OLD_VEHICLE_DOCUMENTS),
         "fields": list(_OLD_VEHICLE_MAKE_FIELDS + _OLD_VEHICLE_MODEL_FIELDS)},
    ).mappings().all()
    documents: dict[str, dict[str, str]] = {}
    for row in rows:
        value = str(row["effective_value"] or "").strip().strip('"')
        if not value or value.lower() == "null":
            continue
        doc = documents.setdefault(str(row["di_document_id"]), {})
        field = str(row["field_key"])
        if field in _OLD_VEHICLE_MAKE_FIELDS and "make" not in doc:
            doc["make"] = value
        elif field in _OLD_VEHICLE_MODEL_FIELDS and "model" not in doc:
            doc["model"] = value
    return list(documents.values())


def _old_vehicle_meets(requirement: dict[str, Any] | None, facts: list[dict[str, str]]) -> bool:
    """Whether an old vehicle on file meets a scheme's old-vehicle condition
    ("against old Maruti", "Tata Ace Family"). No document, no match: the
    condition is never assumed."""
    if not requirement:
        return True
    brand = str(requirement.get("brand") or "").upper()
    model_words = set(str(requirement.get("model") or "").upper().split())
    for fact in facts:
        make = re.sub(r"[^A-Z0-9 ]+", " ", fact.get("make", "").upper()).split()
        model = re.sub(r"[^A-Z0-9 ]+", " ", f"{fact.get('make', '')} {fact.get('model', '')}".upper()).split()
        if brand and brand not in make:
            continue
        if model_words and not model_words <= set(model):
            continue
        return True
    return False


def _corporate_category(
    connection: Connection, *, tenant_id: str, journey_id: UUID, oem_code: str | None,
    basis: str, customer_name: str | None,
) -> dict[str, Any] | None:
    """The corporate privilege category this deal is entitled to, from the
    OEM's corporate registry, or None when nothing on file names a listed
    corporate.

    2026-10-20 product owner rules: corporate privilege follows the corporate
    company's category (Z / Y / F / A / B), for a company buying in its own
    name or an employee of it -- not the buyer's registration type. Looked up
    by the corporate code the Corporate ID quotes, else by the employer's
    exact name, else (a company buyer) by the customer's own exact name.
    Never a partial-name guess."""
    if not oem_code:
        return None
    fields = connection.execute(
        text(
            """
            SELECT f.field_key, f.effective_value
            FROM auditcore.evidence e
            JOIN auditcore.journey_document_extracted_fields f
              ON f.tenant_id = e.tenant_id AND f.journey_id = e.journey_id AND f.di_document_id = e.di_document_id
            WHERE e.tenant_id = :t AND e.journey_id = :j AND e.association_status = 'ACTIVE'
              AND e.document_type_key = 'corporate_id'
              AND f.field_key IN ('corporate_scheme_code_referenced', 'employer_name')
            ORDER BY e.linked_at_utc DESC NULLS LAST
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    codes = [str(r["effective_value"]).strip().strip('"') for r in fields
             if r["field_key"] == "corporate_scheme_code_referenced" and r["effective_value"]]
    names = [str(r["effective_value"]).strip().strip('"') for r in fields
             if r["field_key"] == "employer_name" and r["effective_value"]]
    if basis == "CORPORATE" and customer_name:
        names.append(customer_name)
    for code in codes:
        row = connection.execute(
            text(
                "SELECT corporate_code, corporate_name, privilege_category FROM auditcore.corporate_privilege_registry "
                "WHERE oem_code = :oem AND upper(corporate_code) = upper(:code)"
            ),
            {"oem": oem_code, "code": code},
        ).mappings().first()
        if row is not None:
            return {"category": row["privilege_category"], "code": row["corporate_code"],
                    "name": row["corporate_name"], "matchedOn": "CODE"}
    for name in names:
        matches = connection.execute(
            text(
                """
                SELECT corporate_code, corporate_name, privilege_category FROM auditcore.corporate_privilege_registry
                WHERE oem_code = :oem
                  AND lower(regexp_replace(corporate_name, '\\s+', ' ', 'g')) = lower(regexp_replace(:name, '\\s+', ' ', 'g'))
                ORDER BY corporate_code
                """
            ),
            {"oem": oem_code, "name": name.strip()},
        ).mappings().all()
        # One listed name in two categories is not decided by guesswork.
        if matches and len({m["privilege_category"] for m in matches}) == 1:
            first = matches[0]
            return {"category": first["privilege_category"], "code": first["corporate_code"],
                    "name": first["corporate_name"], "matchedOn": "NAME"}
    return None


def _candidate_rows(
    connection: Connection, *, tenant_id: str, model_id: UUID, variant_id: UUID | None,
    product_sku_id: UUID | None, effective_on: date,
) -> list[dict[str, Any]]:
    """Every benefit of every scheme version standing on the date that is
    offered on this model.

    2026-10-20 product owner rules (R5): one version per scheme answers for a
    date -- the newest in force on it. A newer version retires the older one,
    and a withdrawn scheme's empty "tombstone" version means no benefit (the
    older version no longer answers). Versions of one scheme with the same
    date that one upload created together are kept together (they are rows the
    old ingestion collided into one scheme); the selection then takes the
    lower amount and says the choice was ambiguous."""
    rows = connection.execute(
        text(
            """
            WITH in_force AS (
                SELECT v.discount_scheme_id, v.discount_scheme_version_id, v.version_no,
                       v.effective_from, v.created_at_utc
                FROM auditcore.discount_scheme_versions v
                WHERE v.tenant_id = :tenant_id AND v.lifecycle_status = 'PUBLISHED'
                  AND v.effective_from <= :effective_on
                  AND (v.effective_to IS NULL OR v.effective_to >= :effective_on)
            ), latest AS (
                SELECT discount_scheme_id, max(effective_from) AS effective_from
                FROM in_force GROUP BY discount_scheme_id
            ), batch AS (
                SELECT f.discount_scheme_id, max(f.created_at_utc) AS created_at_utc
                FROM in_force f JOIN latest l
                  ON l.discount_scheme_id = f.discount_scheme_id AND l.effective_from = f.effective_from
                GROUP BY f.discount_scheme_id
            ), standing AS (
                SELECT f.* FROM in_force f
                JOIN latest l ON l.discount_scheme_id = f.discount_scheme_id AND l.effective_from = f.effective_from
                JOIN batch b ON b.discount_scheme_id = f.discount_scheme_id AND b.created_at_utc = f.created_at_utc
            )
            SELECT b.benefit_key, b.benefit_type, b.amount_value, b.percentage_value,
                   s.discount_scheme_version_id, s.version_no, s.effective_from,
                   ds.discount_scheme_id, ds.scheme_code, ds.scheme_category,
                   e.eligibility_id, e.variant_id, e.product_sku_id, e.dealer_id, e.outlet_id,
                   e.colour_id, c.colour_code, c.colour_name, e.customer_type_code, e.criteria
            FROM standing s
            JOIN auditcore.discount_schemes ds
              ON ds.tenant_id = :tenant_id AND ds.discount_scheme_id = s.discount_scheme_id
            JOIN auditcore.discount_scheme_eligibility e
              ON e.tenant_id = :tenant_id AND e.discount_scheme_version_id = s.discount_scheme_version_id
            JOIN auditcore.discount_scheme_benefits b
              ON b.tenant_id = :tenant_id AND b.discount_scheme_version_id = s.discount_scheme_version_id
            LEFT JOIN auditcore.colours c ON c.colour_id = e.colour_id
            WHERE (e.model_id = :model_id OR (e.model_id IS NULL AND e.product_sku_id = :sku))
              AND (e.variant_id IS NULL OR e.variant_id = :variant_id)
            ORDER BY ds.scheme_code, s.version_no, e.eligibility_id, b.benefit_key
            """
        ),
        {
            "tenant_id": tenant_id,
            "model_id": model_id,
            "variant_id": variant_id,
            "sku": product_sku_id,
            "effective_on": effective_on,
        },
    ).mappings().all()
    return [dict(row) for row in rows]


def _select_benefits(
    rows: list[dict[str, Any]],
    *,
    variant_id: UUID | None,
    variant_name: str | None,
    product_sku_id: UUID | None,
    dealer_id: UUID | None,
    outlet_id: UUID | None,
    colour: str | None,
    basis: str,
    corporate_category: str | None,
    old_vehicles: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], dict[str, Decimal]]:
    """One benefit per canonical discount key, chosen deterministically, and
    the corporate amounts by category when the category is not known.

    2026-10-20 product owner rules:
      * a model-level row stored for a scheme whose named variants did not
        resolve (unresolvedVariantTexts) was meant for specific variants and
        is never applied;
      * the eligibility's dealer / outlet / SKU / colour and its exclusions
        ("Not on Star Edition") and old-vehicle condition ("against old
        Maruti") are honoured -- a condition is never assumed met;
      * the most specific row wins (SKU, then variant, then outlet / dealer,
        then a met condition), then the newest date, then the lower amount,
        then the newer version and the scheme code: the same data always
        gives the same entitlement."""
    eligible: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    corporate_candidates: dict[str, Decimal] = {}
    colour_key = (colour or "").strip().upper()
    for row in rows:
        criteria = _json(row.get("criteria"))
        if row.get("variant_id") is None and (
            criteria.get("scope") == "UNRESOLVED" or criteria.get("unresolvedVariantTexts")
        ):
            continue
        if row.get("variant_id") is not None and row["variant_id"] != variant_id:
            continue
        if row.get("product_sku_id") is not None and row["product_sku_id"] != product_sku_id:
            continue
        if row.get("dealer_id") is not None and row["dealer_id"] != dealer_id:
            continue
        if row.get("outlet_id") is not None and row["outlet_id"] != outlet_id:
            continue
        if row.get("colour_id") is not None and colour_key not in {
            str(row.get("colour_code") or "").strip().upper(), str(row.get("colour_name") or "").strip().upper(),
        } - {""}:
            continue
        if variant_excluded(variant_name, criteria.get("excludedVariantTexts")):
            continue
        amount = _to_decimal(row.get("amount_value"))
        customer_type = str(row.get("customer_type_code") or "").strip().upper()
        if customer_type.startswith("CORPORATE"):
            category = customer_type.split(":", 1)[1] if ":" in customer_type else ""
            if corporate_category is None:
                if amount is not None:
                    label = category or "ANY"
                    corporate_candidates[label] = min(amount, corporate_candidates.get(label, amount))
                continue
            if category and category != corporate_category:
                continue
        elif customer_type and basis == "CORPORATE":
            continue
        requirement = criteria.get("requiredOldVehicle")
        if requirement and not _old_vehicle_meets(requirement, old_vehicles):
            continue
        outlet_rank = 0 if row.get("outlet_id") else (1 if row.get("dealer_id") else 2)
        rank = (
            0 if row.get("product_sku_id") else 1,
            0 if row.get("variant_id") else 1,
            outlet_rank,
            0 if requirement else 1,
            -row["effective_from"].toordinal() if row.get("effective_from") else 0,
            amount if amount is not None else Decimal(0),
            -int(row.get("version_no") or 0),
            str(row.get("scheme_code") or ""),
            str(row.get("eligibility_id") or ""),
        )
        eligible.append((rank, row))

    by_key: dict[str, list[tuple[tuple[Any, ...], dict[str, Any]]]] = {}
    for rank, row in eligible:
        key = canonical_discount_key(str(row["benefit_key"]))
        by_key.setdefault(key, []).append((rank, row))
    chosen: list[dict[str, Any]] = []
    for key in sorted(by_key):
        candidates = sorted(by_key[key], key=lambda item: item[0])
        best_rank, best = candidates[0]
        tied = [
            row for rank, row in candidates
            if rank[:5] == best_rank[:5] and _to_decimal(row.get("amount_value")) != _to_decimal(best.get("amount_value"))
        ]
        chosen.append({
            **best,
            "canonical_key": key,
            "alternatives": len(candidates) - 1,
            "ambiguous_amounts": sorted({str(_to_decimal(r.get("amount_value"))) for r in tied}),
        })
    return chosen, corporate_candidates


def _applicable_benefits(
    connection: Connection,
    *,
    tenant_id: str,
    model_id: UUID,
    variant_id: UUID | None,
    effective_on: date,
    basis: str,
    variant_name: str | None = None,
    product_sku_id: UUID | None = None,
    dealer_id: UUID | None = None,
    outlet_id: UUID | None = None,
    colour: str | None = None,
    corporate_category: str | None = None,
    old_vehicles: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """The one benefit per discount key the deal is entitled to on the date."""
    rows = _candidate_rows(
        connection, tenant_id=tenant_id, model_id=model_id, variant_id=variant_id,
        product_sku_id=product_sku_id, effective_on=effective_on,
    )
    chosen, _ = _select_benefits(
        rows, variant_id=variant_id, variant_name=variant_name, product_sku_id=product_sku_id,
        dealer_id=dealer_id, outlet_id=outlet_id, colour=colour, basis=basis,
        corporate_category=corporate_category, old_vehicles=old_vehicles or [],
    )
    return chosen


def _actual_discounts_by_benefit(reviewed_booking: dict[str, Any]) -> dict[str, Decimal]:
    """Reviewed booking discount fields collapsed onto OEM benefit keys.

    The form's declared totals (discount_amount, bonus_amount) are not
    components and are not added to them (2026-10-20)."""
    out: dict[str, Decimal] = {}
    for field, benefit_key in DISCOUNT_ACTUAL_FIELD_TO_BENEFIT_KEY.items():
        amount = _to_decimal(reviewed_booking.get(field))
        if amount is None or amount == 0:
            continue
        out[benefit_key] = out.get(benefit_key, Decimal(0)) + amount
    return out


def _discount_rows(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, dict[str, Any]]:
    """The deal's current row per discount key (the newest when an old run left two)."""
    rows = connection.execute(
        text(
            """
            SELECT discount_application_id, discount_key, details, actual_discount_amount,
                   standard_eligible_amount, eligibility_result, discount_scheme_version_id
            FROM auditcore.discount_applications
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY discount_key, updated_at_utc DESC, created_at_utc DESC
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        data = dict(row)
        data["details"] = _json(data.get("details"))
        out.setdefault(str(row["discount_key"]), data)
    return out


def _write_discount_row(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    key: str,
    existing: dict[str, Any] | None,
    standard: Decimal | None,
    result: str | None,
    dsv: UUID | None,
    details: dict[str, Any],
    booking_actual: Decimal | None,
) -> None:
    """One canonical row per key. An invoice-won actual is never replaced by a
    booking-form value (uc03_invoice_materialization); the standard side
    (entitlement, scheme version, eligibility) is refreshed every run."""
    if existing is not None:
        # An older run may have left a second row under the same key: only
        # one row carries the entitlement, so a total never counts it twice.
        connection.execute(
            text(
                """
                UPDATE auditcore.discount_applications
                SET standard_eligible_amount = NULL, discount_scheme_version_id = NULL, updated_at_utc = now()
                WHERE tenant_id = :tenant_id AND journey_id = :journey_id AND discount_key = :key
                  AND discount_application_id <> :id
                  AND (standard_eligible_amount IS NOT NULL OR discount_scheme_version_id IS NOT NULL)
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "key": key, "id": existing["discount_application_id"]},
        )
    if existing is not None and existing["details"].get("origin") == "INVOICE_MATERIALIZATION":
        connection.execute(
            text(
                """
                UPDATE auditcore.discount_applications
                SET standard_eligible_amount = :standard,
                    discount_scheme_version_id = :dsv,
                    eligibility_result = :result,
                    details = details || CAST(:recon AS jsonb),
                    updated_at_utc = now()
                WHERE tenant_id = :tenant_id AND discount_application_id = :id
                """
            ),
            {"tenant_id": tenant_id, "id": existing["discount_application_id"], "standard": standard,
             "dsv": dsv, "result": result, "recon": json.dumps({"reconciliation": details}, default=str)},
        )
        return
    payload = json.dumps({"origin": "DEAL_RECONCILIATION", **details}, default=str)
    if existing is not None:
        connection.execute(
            text(
                """
                UPDATE auditcore.discount_applications
                SET standard_eligible_amount = :standard,
                    actual_discount_amount = COALESCE(:actual, actual_discount_amount),
                    discount_scheme_version_id = :dsv,
                    eligibility_result = :result,
                    actual_source_kind = COALESCE(actual_source_kind, :calc),
                    details = CAST(:details AS jsonb),
                    updated_at_utc = now()
                WHERE tenant_id = :tenant_id AND discount_application_id = :id
                """
            ),
            {"tenant_id": tenant_id, "id": existing["discount_application_id"], "standard": standard,
             "actual": booking_actual, "dsv": dsv, "result": result, "calc": _CALCULATED, "details": payload},
        )
        return
    connection.execute(
        text(
            """
            INSERT INTO auditcore.discount_applications (
                tenant_id, journey_id, discount_key,
                standard_eligible_amount, actual_discount_amount,
                discount_scheme_version_id, eligibility_result,
                actual_source_kind, details
            ) VALUES (
                :tenant_id, :journey_id, :key, :standard, :actual, :dsv, :result, :calc, CAST(:details AS jsonb)
            )
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "key": key, "standard": standard,
         "actual": booking_actual, "dsv": dsv, "result": result, "calc": _CALCULATED, "details": payload},
    )


def _grid_tier(connection: Connection, *, tenant_id: str, journey_id: UUID) -> str:
    """Which dealer-grid tier the deal sits in: BASE (no in-house insurance or
    EW), WITH_INSURANCE, or WITH_EW_INSURANCE. In-house insurance only when
    the PC confirmed Inhouse or the deal bills a premium; EW only when the deal
    bills one -- never assumed (the grid is a cap on the dealer's discount)."""
    row = connection.execute(
        text(
            """
            SELECT
              (SELECT insurance_source FROM auditcore.insurance_records
                WHERE tenant_id = :t AND journey_id = :j AND insurance_source IS NOT NULL
                ORDER BY updated_at_utc DESC LIMIT 1) AS insurance_source,
              (SELECT actual_amount FROM auditcore.commercial_lines
                WHERE tenant_id = :t AND journey_id = :j AND component_key = 'insurance_amount') AS insurance,
              (SELECT actual_amount FROM auditcore.commercial_lines
                WHERE tenant_id = :t AND journey_id = :j AND component_key = 'additional_warranty_amount') AS ew
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    source = str(row["insurance_source"] or "").upper()
    inhouse = source == "INHOUSE" or (source != "SELF" and (_to_decimal(row["insurance"]) or 0) > 0)
    ew = (_to_decimal(row["ew"]) or 0) > 0
    if inhouse and ew:
        return "WITH_EW_INSURANCE"
    return "WITH_INSURANCE" if inhouse else "BASE"


def _grid_entitlement(
    connection: Connection, *, tenant_id: str, journey_id: UUID, model_id: UUID, variant_name: str | None,
    effective_on: date,
) -> dict[str, Any] | None:
    """The dealer discount grid's allowance for the deal's dealer discount on
    the pricing date (2026-10-20): one grid for every outlet, by tier."""
    # imported here: oem_price_masters pulls in the admin routes
    from audit_core.oem_price_masters import grid_row_for_model

    grid = grid_row_for_model(
        connection, tenant_id=tenant_id, model_id=model_id, effective_on=effective_on, variant_name=variant_name,
    )
    if grid is None:
        return None
    raw = _json(grid.get("raw"))
    tiers = {k: _to_decimal(v) for k, v in (raw.get("tiers") or {}).items()}
    tier = _grid_tier(connection, tenant_id=tenant_id, journey_id=journey_id) if tiers else None
    buffer = tiers.get(tier) if tier else _to_decimal(grid.get("agreed_buffer_amount"))
    return {
        "inScope": bool(grid.get("in_scope")),
        "buffer": buffer,
        "tier": tier,
        "tiers": {k: str(v) for k, v in tiers.items()},
        "gridVersionId": str(grid.get("grid_version_id")),
        "gridVersionNo": grid.get("version_no"),
        "gridEffectiveFrom": str(grid.get("effective_from")),
        "modelAlias": grid.get("model_alias"),
        "outOfTerritory": str(grid.get("out_of_territory_amount")) if grid.get("out_of_territory_amount") is not None else None,
        "bookingProtectionDays": grid.get("booking_protection_days"),
    }


def _management_referral(connection: Connection, *, tenant_id: str, journey_id: UUID) -> Decimal | None:
    """The MR amount a Team Lead approved for the deal, or None."""
    row = connection.execute(
        text("SELECT opted, amount FROM auditcore.p2_management_referrals WHERE tenant_id = :t AND journey_id = :j"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().first()
    if row is None or not row["opted"]:
        return None
    return _to_decimal(row["amount"])


def _materialize_discount_standards(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    benefits: list[dict[str, Any]],
    actuals: dict[str, Decimal],
    corporate_candidates: dict[str, Decimal] | None = None,
    corporate: dict[str, Any] | None = None,
    grid: dict[str, Any] | None = None,
    management_referral: Decimal | None = None,
    correlation_id: str = "",
) -> dict[str, Any]:
    """Write the entitlement side of every discount key, then clear the keys
    this run no longer produces (their actuals stay)."""
    existing = _discount_rows(connection, tenant_id=tenant_id, journey_id=journey_id)
    produced: set[str] = set()
    counts = {"eligible": 0, "unclaimed": 0, "informational": 0, "overGranted": 0, "needsCorporateEvidence": 0}

    def invoice_actual(key: str) -> Decimal | None:
        row = existing.get(key)
        if row is not None and row["details"].get("origin") == "INVOICE_MATERIALIZATION":
            return _to_decimal(row["actual_discount_amount"])
        return None

    def claimed(key: str) -> bool:
        return actuals.get(key) is not None or invoice_actual(key) is not None

    def evidenced(key: str) -> bool:
        if key == "CORPORATE_PRIVILEGE":
            return corporate is not None
        document = CONDITIONAL_DISCOUNT_EVIDENCE_DOCUMENT.get(key)
        return bool(document) and _has_active_document(
            connection, tenant_id=tenant_id, journey_id=journey_id, document_type_key=document
        )

    def write(key: str, *, standard: Decimal | None, result: str | None, dsv: UUID | None,
              details: dict[str, Any]) -> None:
        produced.add(key)
        _write_discount_row(
            connection, tenant_id=tenant_id, journey_id=journey_id, key=key, existing=existing.get(key),
            standard=standard, result=result, dsv=dsv, details=details, booking_actual=actuals.get(key),
        )

    for benefit in benefits:
        key = benefit.get("canonical_key") or canonical_discount_key(str(benefit["benefit_key"]))
        if key in produced:
            continue
        amount = _to_decimal(benefit["amount_value"]) if str(benefit["benefit_type"]).upper() == "AMOUNT" else None
        details: dict[str, Any] = {
            "schemeCategory": benefit["scheme_category"],
            "schemeCode": benefit["scheme_code"],
            "benefitKey": str(benefit["benefit_key"]).strip().upper(),
            "benefitType": benefit["benefit_type"],
            "percentageValue": str(benefit["percentage_value"]) if benefit.get("percentage_value") is not None else None,
            "alternatives": benefit.get("alternatives", 0),
        }
        if benefit.get("ambiguous_amounts"):
            details["ambiguousAmounts"] = benefit["ambiguous_amounts"]
        if key == "CORPORATE_PRIVILEGE" and corporate:
            details["corporate"] = corporate
        is_claimed = claimed(key)
        if key in _CONDITIONAL_KEYS and not is_claimed and not evidenced(key):
            # Shown, never owed: no claim and no proof on file.
            details.update({"potentialAmount": str(amount) if amount is not None else None, "informational": True})
            write(key, standard=None, result="ELIGIBLE_UNCLAIMED", dsv=benefit["discount_scheme_version_id"],
                  details=details)
            counts["informational"] += 1
            continue
        result = "ELIGIBLE" if is_claimed else "ELIGIBLE_UNCLAIMED"
        write(key, standard=amount, result=result, dsv=benefit["discount_scheme_version_id"], details=details)
        counts["eligible"] += 1
        if not is_claimed:
            counts["unclaimed"] += 1

    # Corporate privilege claimed but no listed corporate on file: the
    # category -- and so the amount -- cannot be decided. Never assumed.
    if "CORPORATE_PRIVILEGE" not in produced and corporate_candidates and claimed("CORPORATE_PRIVILEGE"):
        write("CORPORATE_PRIVILEGE", standard=None, result="NEEDS_CORPORATE_EVIDENCE", dsv=None, details={
            "benefitKey": "CORPORATE_PRIVILEGE",
            "candidatesByCategory": {k: str(v) for k, v in sorted(corporate_candidates.items())},
            "reason": "No corporate on the OEM's corporate list is named on file (Corporate ID code or employer).",
        })
        counts["needsCorporateEvidence"] += 1

    # The dealer's own discount: its allowance is the dealer discount grid.
    if _DEALER_DISCOUNT_KEY not in produced and (grid is not None or claimed(_DEALER_DISCOUNT_KEY)):
        allowance = grid["buffer"] if grid and grid["inScope"] else None
        details = {"benefitKey": _DEALER_DISCOUNT_KEY, "source": "DEALER_DISCOUNT_GRID", "grid": grid}
        if allowance is None:
            if claimed(_DEALER_DISCOUNT_KEY):
                details["reason"] = "No dealer discount grid line allows a discount on this model on the date."
                write(_DEALER_DISCOUNT_KEY, standard=None, result="NOT_ELIGIBLE", dsv=None, details=details)
                counts["overGranted"] += 1
        elif claimed(_DEALER_DISCOUNT_KEY):
            write(_DEALER_DISCOUNT_KEY, standard=allowance, result="ELIGIBLE", dsv=None, details=details)
            counts["eligible"] += 1
        else:
            details.update({"potentialAmount": str(allowance), "informational": True})
            write(_DEALER_DISCOUNT_KEY, standard=None, result="ELIGIBLE_UNCLAIMED", dsv=None, details=details)
            counts["informational"] += 1

    # Management Referral: its own line; the entitlement is what a Team Lead
    # approved.
    if _MR_KEY not in produced and (management_referral is not None or claimed(_MR_KEY)):
        if management_referral is not None:
            write(_MR_KEY, standard=management_referral, result="ELIGIBLE" if claimed(_MR_KEY) else "ELIGIBLE_UNCLAIMED",
                  dsv=None, details={"benefitKey": _MR_KEY, "source": "TL_APPROVAL"})
            counts["eligible"] += 1
        else:
            write(_MR_KEY, standard=None, result="NOT_ELIGIBLE", dsv=None,
                  details={"benefitKey": _MR_KEY, "reason": "No Team Lead approval for a Management Referral."})
            counts["overGranted"] += 1

    # A discount given with no entitlement at all: an over-grant.
    for key in sorted(actuals):
        if key in produced:
            continue
        write(key, standard=None, result="NOT_ELIGIBLE", dsv=None, details={"benefitKey": key})
        counts["overGranted"] += 1

    counts["cleared"] = _clear_stale_discount_rows(
        connection, tenant_id=tenant_id, journey_id=journey_id, produced=produced, correlation_id=correlation_id,
    )
    counts["unclassified"] = _assess_unclassified_discount(connection, tenant_id=tenant_id, journey_id=journey_id)
    return counts


def _assess_unclassified_discount(connection: Connection, *, tenant_id: str, journey_id: UUID) -> str | None:
    """An invoice discount whose printed words name no type (a header-only
    discount, say) is not "given with no entitlement": it can be any of the
    deal's entitlements. 2026-10-20: it is compared with what the deal is
    entitled to and not already given under a named type -- within that
    headroom, or exceeding it by how much. It carries no entitlement of its
    own, so a total never counts an entitlement twice."""
    rows = connection.execute(
        text(
            """
            SELECT discount_key, standard_eligible_amount, actual_discount_amount, discount_application_id
            FROM auditcore.discount_applications
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND (discount_key = ANY(:keys) OR discount_key = :unclassified)
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "keys": sorted(_RECONCILED_KEYS),
         "unclassified": _UNCLASSIFIED_KEY},
    ).mappings().all()
    unclassified = [r for r in rows if r["discount_key"] == _UNCLASSIFIED_KEY and r["actual_discount_amount"] is not None]
    if not unclassified:
        return None
    entitled = sum((_to_decimal(r["standard_eligible_amount"]) or Decimal(0)
                    for r in rows if r["discount_key"] != _UNCLASSIFIED_KEY), Decimal(0))
    given = sum((_to_decimal(r["actual_discount_amount"]) or Decimal(0)
                 for r in rows if r["discount_key"] != _UNCLASSIFIED_KEY), Decimal(0))
    amount = sum((_to_decimal(r["actual_discount_amount"]) or Decimal(0) for r in unclassified), Decimal(0))
    headroom = max(entitled - given, Decimal(0))
    result = "WITHIN_ENTITLEMENT" if amount <= headroom else "EXCEEDS_ENTITLEMENT"
    assessment = {"unclassified": {
        "amount": str(amount), "totalEntitlement": str(entitled), "givenUnderNamedTypes": str(given),
        "headroom": str(headroom), "excess": str(max(amount - headroom, Decimal(0))),
    }}
    for row in unclassified:
        connection.execute(
            text(
                """
                UPDATE auditcore.discount_applications
                SET standard_eligible_amount = NULL, discount_scheme_version_id = NULL,
                    eligibility_result = :result, details = details || CAST(:details AS jsonb), updated_at_utc = now()
                WHERE tenant_id = :tenant_id AND discount_application_id = :id
                """
            ),
            {"tenant_id": tenant_id, "id": row["discount_application_id"], "result": result,
             "details": json.dumps(assessment)},
        )
    return result


def _clear_stale_discount_rows(
    connection: Connection, *, tenant_id: str, journey_id: UUID, produced: set[str], correlation_id: str = "",
) -> list[str]:
    """2026-10-20 product owner rules: an entitlement this run did not produce
    (the SKU, the pricing date or a master changed) is not left standing. Its
    entitlement, scheme version and eligibility are reset; a discount still
    given on it is now an over-grant. Actuals are never touched. Logged."""
    stale = [key for key in sorted(_RECONCILED_KEYS) if key not in produced]
    rows = connection.execute(
        text(
            """
            UPDATE auditcore.discount_applications
            SET standard_eligible_amount = NULL,
                discount_scheme_version_id = NULL,
                eligibility_result = CASE WHEN actual_discount_amount IS NOT NULL THEN 'NOT_ELIGIBLE' END,
                updated_at_utc = now()
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND discount_key = ANY(:keys)
              AND (standard_eligible_amount IS NOT NULL
                   OR discount_scheme_version_id IS NOT NULL
                   OR eligibility_result IS DISTINCT FROM
                      (CASE WHEN actual_discount_amount IS NOT NULL THEN 'NOT_ELIGIBLE' END))
            RETURNING discount_key, actual_discount_amount
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "keys": stale},
    ).mappings().all()
    cleared = sorted({str(r["discount_key"]) for r in rows})
    if cleared:
        logger.info("deal_reconciliation_stale_discounts_cleared journey_id=%s keys=%s", journey_id, cleared)
        _note_cleared(connection, tenant_id=tenant_id, journey_id=journey_id, kind="DISCOUNT", keys=cleared,
                      correlation_id=correlation_id)
    return cleared


def _note_cleared(
    connection: Connection, *, tenant_id: str, journey_id: UUID, kind: str, keys: list[str], correlation_id: str,
) -> None:
    try:
        with connection.begin_nested():
            record_activity(
                connection, tenant_id=tenant_id, journey_id=journey_id, event_type="DEAL_STANDARDS_CLEARED",
                subject_type=kind, details={"keys": keys, "reason": "no longer produced by the masters"},
                correlation_id=correlation_id or None,
            )
    except Exception:  # the activity feed must never block reconciliation
        logger.warning("deal_reconciliation_activity_not_recorded", exc_info=True)



# ── conditional-discount evidence status ────────────────────────────────────
def _sync_conditional_discount_evidence(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str
) -> int:
    """Set ``evidence_status`` on every discount row for this journey, and
    raise/resolve the shared missing-evidence finding for a conditional
    discount that's claimed without its proof document on file.

    Reads the canonical, already invoice-first-resolved
    ``actual_discount_amount`` -- not any one document's raw field -- so a
    corporate/exchange/scrappage discount revealed by an invoice is checked
    exactly the same way as one the Booking Form itself shows (the rule_key
    matches uc03_booking_confirmation_rules.py's own
    BK_DISCOUNT_EVIDENCE_MISSING:<label> exactly, so both converge on one
    finding per category rather than raising two for the same gap).
    """
    rows = connection.execute(
        text(
            """
            SELECT discount_application_id, discount_key, actual_discount_amount
            FROM auditcore.discount_applications
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()

    updated = 0
    for row in rows:
        discount_key = row["discount_key"]
        required_document = CONDITIONAL_DISCOUNT_EVIDENCE_DOCUMENT.get(discount_key)
        claimed = row["actual_discount_amount"]

        if required_document is None:
            status = "NOT_REQUIRED"
        elif claimed is None:
            status = "PENDING"
        elif _has_active_document(
            connection, tenant_id=tenant_id, journey_id=journey_id, document_type_key=required_document
        ):
            status = "VERIFIED"
        else:
            status = "MISSING"

        connection.execute(
            text(
                """
                UPDATE auditcore.discount_applications
                SET evidence_status = :status
                WHERE tenant_id = :tenant_id AND discount_application_id = :id
                  AND evidence_status IS DISTINCT FROM :status
                """
            ),
            {"tenant_id": tenant_id, "id": row["discount_application_id"], "status": status},
        )
        updated += 1

        if required_document is None:
            continue
        label = CONDITIONAL_DISCOUNT_LABEL.get(discount_key)
        if label is None:
            continue
        rule_key = f"BK_DISCOUNT_EVIDENCE_MISSING:{label}"
        if status == "MISSING":
            _machine_flag(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                stage_code="BOOKING",
                rule_key=rule_key,
                finding_type=_DISCOUNT_EVIDENCE_FINDING_TYPE,
                severity="HIGH",
                title=f"{label.replace('_', ' ').title()} evidence is missing",
                description=(
                    f"A {label.replace('_', ' ')} of ₹{claimed:,.2f} is on record for this "
                    f"deal, but no supporting document is on file."
                ),
                correlation_id=correlation_id,
                safe_payload={
                    "trigger": "DEAL_RECONCILIATION",
                    "discountKey": discount_key,
                    "amount": str(claimed),
                    "requiredDocumentType": required_document,
                },
                blocking_completion=False,
            )
        else:
            finding_id = connection.execute(
                text(
                    """
                    SELECT audit_finding_id FROM auditcore.audit_findings
                    WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                      AND rule_key=:rule_key AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id, "rule_key": rule_key},
            ).scalar_one_or_none()
            if finding_id is not None:
                _resolve_finding(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    stage_code="BOOKING",
                    finding_id=finding_id,
                    actor_id=None,
                    correlation_id=correlation_id,
                    note="Supporting document is now on file.",
                )
    return updated


# ── producer ──────────────────────────────────────────────────────────────────
def _sync_total_variance(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str
) -> dict[str, Any]:
    """Deal total below the standard: the Deal sheet's own variance.

    Rebased 2026-10-20 (product owner rules; the retro's deal-maths defect
    16). The old check summed every commercial line -- the booking form's
    declared totals, net amount and amounts paid included -- and every
    discount row, legacy keys and their canonical twins both, so the same
    rupees were counted several times and unclaimed exchange / scrappage was
    subtracted as owed. It now reads the one figure the Deal sheet shows,
    ``summary.variance.currentVsStandard`` (Current net − Standard net), so
    the finding and the page can never disagree. A positive variance
    (paying more than standard) is informational only. A negative one
    (undercollection / over-discounting) raises a HIGH finding + Team Lead
    task. Never raises; self-heals (resolves) once it is no longer negative.
    """
    # imported here: the Deal sheet module reads this module's tables only
    from audit_core.uc03_p2_journey360 import deal as deal_sheet

    summary = deal_sheet(connection, tenant_id=tenant_id, journey_id=journey_id).get("summary") or {}
    variance = _to_decimal((summary.get("variance") or {}).get("currentVsStandard"))
    if variance is None:
        return {"skipped": True, "reason": "no_comparable_lines_yet"}
    net = summary.get("net") or {}
    standard_net = _to_decimal(net.get("standard")) or Decimal(0)
    actual_net = _to_decimal(net.get("current"))
    if actual_net is None:
        actual_net = standard_net + variance

    existing_finding_id = connection.execute(
        text(
            """
            SELECT audit_finding_id FROM auditcore.audit_findings
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND rule_key = :rule_key AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "rule_key": _TOTAL_VARIANCE_RULE_KEY},
    ).scalar_one_or_none()

    if variance < 0:
        finding_id = _machine_flag(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code="BOOKING",
            rule_key=_TOTAL_VARIANCE_RULE_KEY,
            finding_type=_TOTAL_VARIANCE_FINDING_TYPE,
            severity="HIGH",
            title="Deal total is below the standard price",
            description=(
                f"This deal's actual net total (₹{actual_net:,.2f}) is below the standard net "
                f"total per the price masters (₹{standard_net:,.2f}) by ₹{-variance:,.2f}. "
                f"A Team Lead must validate this shortfall."
            ),
            correlation_id=correlation_id,
            safe_payload={
                "standardNet": str(standard_net), "actualNet": str(actual_net), "variance": str(variance),
            },
            blocking_completion=False,
        )
        open_task_count = connection.execute(
            text(
                """
                SELECT count(*) FROM auditcore.workflow_tasks
                WHERE tenant_id=:tenant_id AND related_finding_id=:fid
                  AND task_status NOT IN ('COMPLETED', 'CANCELLED', 'FAILED', 'DEAD_LETTER')
                """
            ),
            {"tenant_id": tenant_id, "fid": finding_id},
        ).scalar_one()
        # A Phase 2 Journey gets its Team Lead task from the DEAL_UNDERCHARGED
        # control (per component and net); only the finding is kept here so
        # the Deal page still highlights the shortfall.
        phase2 = connection.execute(
            text("SELECT EXISTS (SELECT 1 FROM auditcore.p2_journey_runtime WHERE tenant_id=:tenant_id "
                 "AND journey_id=:journey_id)"),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
        if open_task_count == 0 and not phase2:
            create_workflow_task(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                workflow_type=_TOTAL_VARIANCE_WORKFLOW_TYPE,
                process_area="BOOKING",
                task_type=_TOTAL_VARIANCE_TASK_TYPE,
                assigned_role_code="TL",
                related_finding_id=finding_id,
                severity="HIGH",
                task_payload={
                    "ruleKey": _TOTAL_VARIANCE_RULE_KEY,
                    "findingId": str(finding_id),
                    "standardNet": str(standard_net),
                    "actualNet": str(actual_net),
                },
                effect_key=f"task:{finding_id}:total-variance",
                correlation_id=correlation_id,
            )
    elif existing_finding_id is not None:
        _resolve_finding(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code="BOOKING",
            finding_id=existing_finding_id,
            actor_id=None,
            correlation_id=correlation_id,
            note="The deal total no longer falls below the standard price.",
        )

    return {"standardNet": str(standard_net), "actualNet": str(actual_net), "variance": str(variance)}


def sync_deal_reconciliation(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    correlation_id: str,
    raise_findings: bool = True,
) -> dict[str, Any]:
    """Materialise price + discount standards from the OEM masters. Never raises.

    ``raise_findings=False`` skips the two evidence/variance checks below
    (still materialises standard/actual values) -- every real caller that
    triggers off an actual document-sync/review event keeps the default,
    genuinely re-checking evidence and total variance when the underlying
    facts change. The one caller that runs on every Journey Overview page
    view (self-heals SKU resolution on every read) passes False: those two
    checks each do a handful of sequential DB round-trips per discount row
    (has_active_document + a finding raise/resolve chain), and running that
    on every page view rather than on the events that actually change
    these facts was a real, measured latency regression -- confirmed via a
    live 23s overview load -- not a hypothetical one.
    """
    try:
        # A savepoint: a failure here rolls back only this reconciliation,
        # never the caller's transaction.
        with connection.begin_nested():
            return _reconcile(
                connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
                raise_findings=raise_findings,
            )
    except Exception:
        logger.warning("sync_deal_reconciliation failed", exc_info=True)
        return {"error": True}


def _skip(connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str, reason: str) -> dict[str, Any]:
    """Nothing can be priced: no standard from an earlier run is left standing."""
    _clear_stale_price_standards(
        connection, tenant_id=tenant_id, journey_id=journey_id, produced=set(), correlation_id=correlation_id,
    )
    _clear_stale_discount_rows(
        connection, tenant_id=tenant_id, journey_id=journey_id, produced=set(), correlation_id=correlation_id,
    )
    return {"skipped": True, "reason": reason}


def _reconcile(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str, raise_findings: bool,
) -> dict[str, Any]:
    if raise_findings:
        # No book in force on the pricing date: a task says so (R5, #468),
        # whether or not a SKU is resolved yet. Event-driven runs only.
        sync_price_master_in_force_task(connection, tenant_id=tenant_id, journey_id=journey_id)
    ctx = _context(connection, tenant_id=tenant_id, journey_id=journey_id)
    if ctx is None:
        return _skip(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
                     reason="no_resolved_sku")

    effective_on = ctx["effective_on"]
    if effective_on is None:
        return _skip(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
                     reason="no_booking_date")
    if not isinstance(effective_on, date):
        effective_on = date.fromisoformat(str(effective_on))

    try:
        plan = _price_plan_for_journey(
            connection, tenant_id=tenant_id, journey_id=journey_id, effective_on=effective_on
        )
    except AuditCoreError:  # no effective price list yet
        return _skip(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
                     reason="no_effective_price_list")
    # The SKU withdrawn from the book in force (#468): a PC task, and the old
    # book's standards cleared rather than left standing.
    if sync_sku_in_price_master_task(
        connection, tenant_id=tenant_id, journey_id=journey_id,
        found={"onDate": effective_on, "plan": plan, "reason": None},
    ):
        return _skip(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
                     reason="sku_not_in_price_list")

    price_lines = _materialize_price_standards(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        product_sku_id=ctx["product_sku_id"],
        price_list_version_id=plan["price_list_version_id"],
        basis=ctx["basis"],
        correlation_id=correlation_id,
    )

    corporate = _corporate_category(
        connection, tenant_id=tenant_id, journey_id=journey_id, oem_code=ctx.get("oem_code"),
        basis=ctx["basis"], customer_name=ctx.get("customer_name"),
    )
    rows = _candidate_rows(
        connection, tenant_id=tenant_id, model_id=ctx["model_id"], variant_id=ctx["variant_id"],
        product_sku_id=ctx["product_sku_id"], effective_on=effective_on,
    )
    benefits, corporate_candidates = _select_benefits(
        rows,
        variant_id=ctx["variant_id"],
        variant_name=ctx.get("variant_name"),
        product_sku_id=ctx["product_sku_id"],
        dealer_id=ctx.get("dealer_id"),
        outlet_id=ctx.get("outlet_id"),
        colour=ctx.get("colour"),
        basis=ctx["basis"],
        corporate_category=corporate["category"] if corporate else None,
        old_vehicles=_old_vehicle_facts(connection, tenant_id=tenant_id, journey_id=journey_id),
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
        corporate_candidates=corporate_candidates,
        corporate=corporate,
        grid=_grid_entitlement(
            connection, tenant_id=tenant_id, journey_id=journey_id, model_id=ctx["model_id"],
            variant_name=ctx.get("variant_name"), effective_on=effective_on,
        ),
        management_referral=_management_referral(connection, tenant_id=tenant_id, journey_id=journey_id),
        correlation_id=correlation_id,
    )
    discount["pricingDate"] = effective_on.isoformat()
    if not raise_findings:
        return {"priceLines": price_lines, **discount}

    evidence_rows = _sync_conditional_discount_evidence(
        connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
    )
    totals = _sync_total_variance(
        connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
    )
    return {
        "priceLines": price_lines,
        "evidenceRowsChecked": evidence_rows,
        "totals": totals,
        **discount,
    }


__all__ = ["sync_deal_reconciliation"]
