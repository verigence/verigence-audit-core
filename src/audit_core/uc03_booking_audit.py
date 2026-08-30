from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.product_masters import resolve_effective_project_product_master_version

logger = structlog.get_logger(__name__)

_AUDIT_EVENT_TYPE = "BOOKING_AUDIT_REQUESTED"
_MINIMUM_BOOKING_AMOUNT_KEY = "MINIMUM_BOOKING_AMOUNT"
_SKU_AMBIGUOUS_REMARK = (
    "Multiple matching SKUs identified. Exact model/SKU could not be determined "
    "from the Booking documents."
)
_SKU_NOT_FOUND_REMARK = (
    "No matching SKU identified in Product Master for the Model/Variant identified "
    "from the Booking documents."
)
_TITLE_TOKENS = {"MR", "MRS", "MS", "MISS", "DR", "SHRI", "SMT"}
_RELATION_MARKER = re.compile(r"\b(?:S\s*/?\s*O|D\s*/?\s*O|W\s*/?\s*O)\b", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
_SCOPE_PRIORITY = {"PROJECT": 1, "SEGMENT": 2, "MODEL": 3, "TRIM": 4}


@dataclass(frozen=True)
class Receipt:
    payment_id: UUID
    amount: Decimal
    receipt_date: date | None


@dataclass(frozen=True)
class AuditIssue:
    rule_key: str
    title: str
    description: str
    severity: str = "HIGH"
    expected_summary: str | None = None
    observed_summary: str | None = None


@dataclass(frozen=True)
class ProductIdentity:
    model_id: UUID
    variant_id: UUID
    segment_id: UUID | None
    model_code: str
    model_name: str
    variant_code: str
    variant_name: str


@dataclass(frozen=True)
class SkuResolution:
    status: str
    product_sku_id: UUID | None
    product_master_version_id: UUID | None
    remarks: str | None
    issue: AuditIssue | None


def _json_value(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def _normalise_name(value: str) -> list[str]:
    primary = _RELATION_MARKER.split(value, maxsplit=1)[0]
    tokens = [token for token in _NON_ALNUM.sub(" ", primary.upper()).split() if token]
    return [token for token in tokens if token not in _TITLE_TOKENS]


def _token_equivalent(left: str, right: str) -> bool:
    return left == right or (len(left) == 1 and right.startswith(left)) or (
        len(right) == 1 and left.startswith(right)
    )


def names_logically_equal(left: str, right: str) -> bool:
    """Conservative deterministic name comparison; no fuzzy or probabilistic matching."""
    a = _normalise_name(left)
    b = _normalise_name(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if not _token_equivalent(a[0], b[0]) or not _token_equivalent(a[-1], b[-1]):
        return False
    middle_a = a[1:-1]
    middle_b = b[1:-1]
    shorter, longer = (middle_a, middle_b) if len(middle_a) <= len(middle_b) else (middle_b, middle_a)
    cursor = 0
    for token in shorter:
        while cursor < len(longer) and not _token_equivalent(token, longer[cursor]):
            cursor += 1
        if cursor == len(longer):
            return False
        cursor += 1
    return True


def _scope_matches(row: dict[str, Any], identity: ProductIdentity | None) -> bool:
    scope_type = str(row.get("scope_type") or "").upper()
    if scope_type == "PROJECT":
        return True
    if identity is None:
        return False
    if scope_type == "SEGMENT":
        return row.get("segment_id") is not None and row.get("segment_id") == identity.segment_id
    scope_key = str(row.get("scope_key") or "").strip().upper()
    if not scope_key:
        return False
    if scope_type == "MODEL":
        return scope_key in {identity.model_code.upper(), identity.model_name.upper()}
    if scope_type == "TRIM":
        return scope_key in {identity.variant_code.upper(), identity.variant_name.upper()}
    return False


def _minimum_amount_on(
    policy_rows: list[dict[str, Any]],
    *,
    effective_on: date,
    identity: ProductIdentity | None,
) -> tuple[Decimal | None, str | None]:
    active = [
        row
        for row in policy_rows
        if row["effective_from"] <= effective_on
        and (row["effective_to"] is None or row["effective_to"] >= effective_on)
    ]
    if not active:
        return None, "No published Minimum Booking Amount master is effective on the receipt date."

    latest_from = max(row["effective_from"] for row in active)
    version_ids = {
        row["discount_policy_version_id"]
        for row in active
        if row["effective_from"] == latest_from
    }
    if len(version_ids) != 1:
        return None, "Multiple published Minimum Booking Amount master versions share the applicable WEF."
    version_id = next(iter(version_ids))
    candidates = [
        row
        for row in active
        if row["discount_policy_version_id"] == version_id
        and row.get("parameter_id") is not None
        and _scope_matches(row, identity)
    ]
    if not candidates:
        return None, "The effective master has no applicable Minimum Booking Amount parameter."

    best_priority = max(
        _SCOPE_PRIORITY.get(str(row["scope_type"]).upper(), 0) for row in candidates
    )
    winners = [
        row
        for row in candidates
        if _SCOPE_PRIORITY.get(str(row["scope_type"]).upper(), 0) == best_priority
    ]
    if len(winners) != 1:
        return None, "The effective master has multiple equally specific Minimum Booking Amount parameters."
    amount = winners[0].get("value_number")
    if amount is None or Decimal(amount) <= 0:
        return None, "The applicable Minimum Booking Amount is missing or not positive."
    return Decimal(amount), None


def derive_booking_confirmation_date(
    receipts: list[Receipt],
    policy_rows: list[dict[str, Any]],
    *,
    identity: ProductIdentity | None,
) -> tuple[date | None, Decimal | None, Decimal, AuditIssue | None]:
    verified = sorted(
        receipts,
        key=lambda item: (item.receipt_date or date.max, str(item.payment_id)),
    )
    if any(item.receipt_date is None for item in verified):
        return None, None, Decimal("0"), AuditIssue(
            rule_key="BK_PAYMENT_RECEIPT_DATE_MISSING",
            title="Booking payment receipt date missing",
            description=(
                "A verified Booking payment has no receipt date, so the system cannot determine "
                "the date on which the minimum Booking amount was first achieved."
            ),
        )

    cumulative = Decimal("0")
    last_required: Decimal | None = None
    for receipt in verified:
        assert receipt.receipt_date is not None
        required, error = _minimum_amount_on(
            policy_rows,
            effective_on=receipt.receipt_date,
            identity=identity,
        )
        if error is not None or required is None:
            return None, None, cumulative, AuditIssue(
                rule_key="BK_MINIMUM_BOOKING_MASTER_UNAVAILABLE",
                title="Minimum Booking Amount master unavailable",
                description=error or "Minimum Booking Amount master could not be resolved.",
            )
        last_required = required
        cumulative += receipt.amount
        if cumulative >= required:
            return receipt.receipt_date, required, cumulative, None

    required_text = str(last_required) if last_required is not None else "unresolved"
    return None, last_required, cumulative, AuditIssue(
        rule_key="BK_MINIMUM_BOOKING_AMOUNT_NOT_MET",
        title="Minimum Booking Amount not received",
        description=(
            "Verified Booking payments have not cumulatively reached the applicable minimum "
            "Booking amount."
        ),
        expected_summary=f"Minimum Booking Amount: {required_text}",
        observed_summary=f"Verified cumulative Booking payments: {cumulative}",
    )


def enqueue_booking_audit(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    review_version: int,
    correlation_id: str,
    actor_id: str,
) -> UUID:
    """One durable row in the existing outbox; caller's idempotent verify transaction owns dedupe."""
    event_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.outbox_events (
                tenant_id, event_type, aggregate_type, aggregate_id, journey_id,
                event_payload, correlation_id, actor_id
            ) VALUES (
                :tenant_id, :event_type, 'BOOKING', :aggregate_id, :journey_id,
                CAST(:payload AS jsonb), :correlation_id, :actor_id
            )
            RETURNING outbox_event_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "event_type": _AUDIT_EVENT_TYPE,
            "aggregate_id": str(journey_id),
            "journey_id": journey_id,
            "payload": json.dumps(
                {"journeyId": str(journey_id), "reviewVersion": review_version}
            ),
            "correlation_id": correlation_id,
            "actor_id": actor_id,
        },
    ).scalar_one()
    logger.info(
        "uc03_booking_audit_enqueued",
        tenant_id=tenant_id,
        journey_id=str(journey_id),
        outbox_event_id=str(event_id),
        review_version=review_version,
    )
    return event_id


def _load_context(
    connection: Connection,
    tenant_id: str,
    journey_id: UUID,
) -> dict[str, Any] | None:
    row = connection.execute(
        text(
            """
            SELECT b.booking_id, b.booking_date, b.booking_confirmation_date,
                   b.created_at_utc AS booking_created_at_utc,
                   j.customer_id, j.price_list_version_id,
                   p.oem_id,
                   c.display_name AS customer_name,
                   jp.product_sku_id, jp.product_master_version_id,
                   jp.model_code_snapshot, jp.model_name_snapshot,
                   jp.variant_code_snapshot, jp.variant_name_snapshot,
                   jp.colour_code_snapshot, jp.colour_name_snapshot,
                   jp.sku_resolution_remarks
            FROM auditcore.bookings b
            JOIN auditcore.journeys j
              ON j.tenant_id=b.tenant_id AND j.journey_id=b.journey_id
            JOIN auditcore.projects p ON p.tenant_id=b.tenant_id
            JOIN auditcore.customers c
              ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
            LEFT JOIN auditcore.journey_products jp
              ON jp.tenant_id=b.tenant_id AND jp.journey_id=b.journey_id
            WHERE b.tenant_id=:tenant_id AND b.journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def _load_product_identity(
    connection: Connection,
    *,
    context: dict[str, Any],
) -> tuple[ProductIdentity | None, AuditIssue | None]:
    model_code = context.get("model_code_snapshot")
    model_name = context.get("model_name_snapshot")
    variant_code = context.get("variant_code_snapshot")
    variant_name = context.get("variant_name_snapshot")
    if not (model_code or model_name) or not (variant_code or variant_name):
        return None, AuditIssue(
            rule_key="BK_SKU_NOT_FOUND",
            title="Model/SKU not identified from Booking documents",
            description=(
                "Reviewed Booking documents do not contain enough Model/Variant information "
                "for deterministic master resolution."
            ),
        )

    rows = connection.execute(
        text(
            """
            SELECT DISTINCT m.model_id, m.segment_id, m.model_code, m.model_name,
                            v.variant_id, v.variant_code, v.variant_name
            FROM auditcore.product_models m
            JOIN auditcore.product_variants v ON v.model_id=m.model_id
            WHERE m.oem_id=:oem_id
              AND m.is_active=true AND v.is_active=true
              AND (
                    (:model_code IS NOT NULL AND upper(m.model_code)=upper(:model_code))
                    OR (:model_code IS NULL AND :model_name IS NOT NULL
                        AND upper(m.model_name)=upper(:model_name))
                  )
              AND (
                    (:variant_code IS NOT NULL AND upper(v.variant_code)=upper(:variant_code))
                    OR (:variant_code IS NULL AND :variant_name IS NOT NULL
                        AND upper(v.variant_name)=upper(:variant_name))
                  )
            """
        ),
        {
            "oem_id": context["oem_id"],
            "model_code": model_code,
            "model_name": model_name,
            "variant_code": variant_code,
            "variant_name": variant_name,
        },
    ).mappings().all()
    if len(rows) != 1:
        return None, AuditIssue(
            rule_key="BK_SKU_AMBIGUOUS" if rows else "BK_SKU_NOT_FOUND",
            title="Model/SKU could not be uniquely identified from Booking documents",
            description=_SKU_AMBIGUOUS_REMARK if rows else _SKU_NOT_FOUND_REMARK,
        )
    row = rows[0]
    return ProductIdentity(
        model_id=row["model_id"],
        variant_id=row["variant_id"],
        segment_id=row["segment_id"],
        model_code=row["model_code"],
        model_name=row["model_name"],
        variant_code=row["variant_code"],
        variant_name=row["variant_name"],
    ), None


def _load_minimum_booking_policy(
    connection: Connection,
    tenant_id: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT v.discount_policy_version_id, v.effective_from, v.effective_to,
                   p.parameter_id, p.scope_type, p.segment_id, p.scope_key,
                   p.value_number
            FROM auditcore.discount_policy_versions v
            LEFT JOIN auditcore.discount_policy_parameters p
              ON p.tenant_id=v.tenant_id
             AND p.discount_policy_version_id=v.discount_policy_version_id
             AND upper(p.parameter_key)=:parameter_key
             AND p.value_type='NUMBER'
            WHERE v.tenant_id=:tenant_id AND v.lifecycle_status='PUBLISHED'
            ORDER BY v.effective_from, v.version_no
            """
        ),
        {"tenant_id": tenant_id, "parameter_key": _MINIMUM_BOOKING_AMOUNT_KEY},
    ).mappings().all()
    return [dict(row) for row in rows]


def _load_verified_receipts(
    connection: Connection,
    tenant_id: str,
    journey_id: UUID,
) -> list[Receipt]:
    rows = connection.execute(
        text(
            """
            SELECT p.payment_id, p.amount, p.receipt_date
            FROM auditcore.payments p
            JOIN LATERAL (
                SELECT e.verification_result
                FROM auditcore.payment_verification_events e
                WHERE e.tenant_id=p.tenant_id AND e.payment_id=p.payment_id
                ORDER BY e.occurred_at_utc DESC, e.payment_verification_event_id DESC
                LIMIT 1
            ) latest ON latest.verification_result='VERIFIED'
            WHERE p.tenant_id=:tenant_id AND p.journey_id=:journey_id
            ORDER BY p.receipt_date NULLS LAST, p.payment_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [
        Receipt(row["payment_id"], Decimal(row["amount"]), row["receipt_date"])
        for row in rows
    ]


def _resolution_from_identity_issue(issue: AuditIssue) -> SkuResolution:
    ambiguous = issue.rule_key == "BK_SKU_AMBIGUOUS"
    return SkuResolution(
        "MULTIPLE_MATCH" if ambiguous else "NOT_FOUND",
        None,
        None,
        _SKU_AMBIGUOUS_REMARK if ambiguous else _SKU_NOT_FOUND_REMARK,
        issue,
    )


def _resolve_sku(
    connection: Connection,
    *,
    tenant_id: str,
    confirmation_date: date | None,
    identity: ProductIdentity,
    colour_code: str | None,
    colour_name: str | None,
) -> SkuResolution:
    if confirmation_date is None:
        return SkuResolution("PENDING_CONFIRMATION", None, None, None, None)
    try:
        version_id = resolve_effective_project_product_master_version(
            connection,
            tenant_id=tenant_id,
            effective_on=confirmation_date,
            segment_id=identity.segment_id,
        )
    except Exception:
        issue = AuditIssue(
            rule_key="BK_SKU_NOT_FOUND",
            title="Applicable Product Master unavailable",
            description=(
                "No unique published Product Master could be resolved for the confirmed "
                "Booking date."
            ),
        )
        return SkuResolution(
            "MASTER_UNAVAILABLE",
            None,
            None,
            _SKU_NOT_FOUND_REMARK,
            issue,
        )

    rows = connection.execute(
        text(
            """
            SELECT DISTINCT s.product_sku_id
            FROM auditcore.project_product_master_items i
            JOIN auditcore.product_skus s ON s.product_sku_id=i.product_sku_id
            LEFT JOIN auditcore.colours c ON c.colour_id=s.colour_id
            WHERE i.tenant_id=:tenant_id AND i.version_id=:version_id
              AND s.is_active=true
              AND s.model_id=:model_id AND s.variant_id=:variant_id
              AND (
                    (:colour_code IS NULL AND :colour_name IS NULL)
                    OR (:colour_code IS NOT NULL AND upper(c.colour_code)=upper(:colour_code))
                    OR (:colour_code IS NULL AND :colour_name IS NOT NULL
                        AND upper(c.colour_name)=upper(:colour_name))
                  )
            """
        ),
        {
            "tenant_id": tenant_id,
            "version_id": version_id,
            "model_id": identity.model_id,
            "variant_id": identity.variant_id,
            "colour_code": colour_code,
            "colour_name": colour_name,
        },
    ).scalars().all()
    if len(rows) == 1:
        return SkuResolution("RESOLVED", rows[0], version_id, None, None)
    if len(rows) > 1:
        issue = AuditIssue(
            rule_key="BK_SKU_AMBIGUOUS",
            title="Model/SKU could not be uniquely identified from Booking documents",
            description=_SKU_AMBIGUOUS_REMARK,
        )
        return SkuResolution(
            "MULTIPLE_MATCH",
            None,
            version_id,
            _SKU_AMBIGUOUS_REMARK,
            issue,
        )
    issue = AuditIssue(
        rule_key="BK_SKU_NOT_FOUND",
        title="Model/SKU not identified from Booking documents",
        description=_SKU_NOT_FOUND_REMARK,
    )
    return SkuResolution(
        "NOT_FOUND",
        None,
        version_id,
        _SKU_NOT_FOUND_REMARK,
        issue,
    )


def _persist_derived_fields(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    confirmation_date: date | None,
    confirmation_is_determinate: bool,
    sku: SkuResolution,
) -> None:
    if confirmation_is_determinate:
        connection.execute(
            text(
                """
                UPDATE auditcore.bookings
                SET booking_confirmation_date=:confirmation_date,
                    updated_at_utc=now(),
                    version_no=version_no+1
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND booking_confirmation_date IS DISTINCT FROM :confirmation_date
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "confirmation_date": confirmation_date,
            },
        )
    if sku.status == "PENDING_CONFIRMATION":
        return
    connection.execute(
        text(
            """
            UPDATE auditcore.journey_products
            SET product_sku_id=:sku_id,
                product_master_version_id=:master_version_id,
                sku_resolution_remarks=:remarks,
                updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND (
                    product_sku_id IS DISTINCT FROM :sku_id
                    OR product_master_version_id IS DISTINCT FROM :master_version_id
                    OR sku_resolution_remarks IS DISTINCT FROM :remarks
                  )
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "sku_id": sku.product_sku_id,
            "master_version_id": sku.product_master_version_id,
            "remarks": sku.remarks,
        },
    )


def _price_issue(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    current_price_version_id: UUID | None,
    confirmation_date: date | None,
    sku_id: UUID | None,
) -> AuditIssue | None:
    if confirmation_date is None or sku_id is None:
        return None
    if current_price_version_id is None:
        return AuditIssue(
            rule_key="BK_PRICE_MASTER_UNAVAILABLE",
            title="Price Master unavailable",
            description=(
                "The Booking has no Price List identity from which an effective Price Master "
                "can be resolved."
            ),
        )
    price_list_id = connection.execute(
        text(
            """
            SELECT price_list_id
            FROM auditcore.price_list_versions
            WHERE tenant_id=:tenant_id AND price_list_version_id=:version_id
            """
        ),
        {"tenant_id": tenant_id, "version_id": current_price_version_id},
    ).scalar_one_or_none()
    if price_list_id is None:
        return AuditIssue(
            rule_key="BK_PRICE_MASTER_UNAVAILABLE",
            title="Price Master unavailable",
            description="The Booking Price List reference does not resolve to a valid Price Master.",
        )
    versions = connection.execute(
        text(
            """
            SELECT price_list_version_id, effective_from
            FROM auditcore.price_list_versions
            WHERE tenant_id=:tenant_id AND price_list_id=:price_list_id
              AND lifecycle_status='PUBLISHED'
              AND effective_from <= :effective_on
              AND (effective_to IS NULL OR effective_to >= :effective_on)
            ORDER BY effective_from DESC, version_no DESC
            """
        ),
        {
            "tenant_id": tenant_id,
            "price_list_id": price_list_id,
            "effective_on": confirmation_date,
        },
    ).mappings().all()
    if not versions:
        return AuditIssue(
            rule_key="BK_PRICE_MASTER_UNAVAILABLE",
            title="Price Master unavailable",
            description="No published Price Master is effective on the confirmed Booking date.",
        )
    best_date = versions[0]["effective_from"]
    best_versions = [row for row in versions if row["effective_from"] == best_date]
    if len(best_versions) != 1:
        return AuditIssue(
            rule_key="BK_PRICE_MASTER_UNAVAILABLE",
            title="Price Master is ambiguous",
            description="Multiple published Price Master versions share the applicable WEF.",
        )
    effective_version_id = best_versions[0]["price_list_version_id"]
    rows = connection.execute(
        text(
            """
            SELECT cl.component_key, cl.actual_amount, pli.standard_amount
            FROM auditcore.commercial_lines cl
            LEFT JOIN auditcore.price_list_items pli
              ON pli.tenant_id=cl.tenant_id
             AND pli.price_list_version_id=:price_version_id
             AND pli.product_sku_id=:sku_id
             AND pli.component_key=cl.component_key
            WHERE cl.tenant_id=:tenant_id AND cl.journey_id=:journey_id
              AND cl.actual_amount IS NOT NULL
            ORDER BY cl.component_key
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "price_version_id": effective_version_id,
            "sku_id": sku_id,
        },
    ).mappings().all()
    mismatches: list[str] = []
    for row in rows:
        if row["standard_amount"] is None:
            mismatches.append(f"{row['component_key']}: no matching master price")
        elif Decimal(row["actual_amount"]) != Decimal(row["standard_amount"]):
            mismatches.append(
                f"{row['component_key']}: booking={row['actual_amount']}, "
                f"master={row['standard_amount']}"
            )
    if not mismatches:
        return None
    return AuditIssue(
        rule_key="BK_PRICE_MASTER_MISMATCH",
        title="Booking cost differs from Price Master",
        description=(
            "One or more reviewed Booking cost components differ from the applicable SKU "
            "Price Master."
        ),
        observed_summary="; ".join(mismatches)[:4000],
    )


def _name_issue(
    connection: Connection,
    tenant_id: str,
    journey_id: UUID,
) -> AuditIssue | None:
    rows = connection.execute(
        text(
            """
            SELECT source_document_type_key, field_key, proposed_value, accepted_value,
                   accepted_at_utc, capture_proposal_id
            FROM auditcore.journey_capture_proposals
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING'
              AND proposal_status IN ('ACCEPTED','CORRECTED')
              AND field_key IN ('customer_name','pan_name','aadhaar_name')
            ORDER BY accepted_at_utc DESC NULLS LAST, capture_proposal_id DESC
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    latest_by_source: dict[str, str] = {}
    for row in rows:
        source = str(row["source_document_type_key"] or row["field_key"])
        if source in latest_by_source:
            continue
        raw = (
            row["accepted_value"]
            if row["accepted_value"] is not None
            else row["proposed_value"]
        )
        value = _json_value(raw)
        if isinstance(value, str) and value.strip():
            latest_by_source[source] = value.strip()
    names = list(latest_by_source.items())
    if len(names) < 2:
        return None
    baseline_source, baseline = names[0]
    mismatched = [
        source
        for source, value in names[1:]
        if not names_logically_equal(baseline, value)
    ]
    if not mismatched:
        return None
    sources = ", ".join([baseline_source, *mismatched])
    return AuditIssue(
        rule_key="BK_CUSTOMER_NAME_CROSS_DOCUMENT",
        title="Customer name is inconsistent across Booking documents",
        description=(
            "Reviewed customer names are not logically consistent across the available "
            "Booking identity documents."
        ),
        observed_summary=f"Mismatch across sources: {sources}",
    )


def _duplicate_issue_and_link(
    connection: Connection,
    *,
    tenant_id: str,
    context: dict[str, Any],
) -> tuple[AuditIssue | None, bool]:
    rows = connection.execute(
        text(
            """
            WITH current_keys AS (
                SELECT identity_type, match_hash
                FROM auditcore.customer_identity_index
                WHERE tenant_id=:tenant_id AND customer_id=:customer_id
            ), raw_matches AS (
                SELECT b.booking_id, b.created_at_utc, i.identity_type AS reason,
                       d.actual_delivery_status_code, d.actual_delivered_at
                FROM current_keys ck
                JOIN auditcore.customer_identity_index i
                  ON i.tenant_id=:tenant_id
                 AND i.identity_type=ck.identity_type
                 AND i.match_hash=ck.match_hash
                JOIN auditcore.journeys j
                  ON j.tenant_id=i.tenant_id AND j.customer_id=i.customer_id
                JOIN auditcore.bookings b
                  ON b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id
                LEFT JOIN auditcore.deliveries d
                  ON d.tenant_id=j.tenant_id AND d.journey_id=j.journey_id
                WHERE b.booking_id<>:current_booking_id
                UNION ALL
                SELECT b.booking_id, b.created_at_utc, 'CUSTOMER_NAME' AS reason,
                       d.actual_delivery_status_code, d.actual_delivered_at
                FROM auditcore.customers c
                JOIN auditcore.journeys j
                  ON j.tenant_id=c.tenant_id AND j.customer_id=c.customer_id
                JOIN auditcore.bookings b
                  ON b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id
                LEFT JOIN auditcore.deliveries d
                  ON d.tenant_id=j.tenant_id AND d.journey_id=j.journey_id
                WHERE c.tenant_id=:tenant_id
                  AND b.booking_id<>:current_booking_id
                  AND lower(btrim(c.display_name))=lower(btrim(:customer_name))
            )
            SELECT booking_id, min(created_at_utc) AS created_at_utc,
                   array_agg(DISTINCT reason ORDER BY reason) AS reasons,
                   max(actual_delivery_status_code) AS delivery_status,
                   max(actual_delivered_at) AS delivered_at
            FROM raw_matches
            GROUP BY booking_id
            ORDER BY min(created_at_utc), booking_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "customer_id": context["customer_id"],
            "current_booking_id": context["booking_id"],
            "customer_name": context["customer_name"],
        },
    ).mappings().all()
    if not rows:
        return None, False

    current = (context["booking_created_at_utc"], str(context["booking_id"]))
    candidates = [
        row
        for row in rows
        if (row["created_at_utc"], str(row["booking_id"])) < current
    ]
    if not candidates:
        return None, False
    original = min(
        candidates,
        key=lambda row: (row["created_at_utc"], str(row["booking_id"])),
    )
    reasons = list(original["reasons"] or [])
    connection.execute(
        text(
            """
            INSERT INTO auditcore.booking_duplicate_links (
                tenant_id, original_booking_id, duplicate_booking_id, match_reasons
            ) VALUES (
                :tenant_id, :original_booking_id, :duplicate_booking_id,
                CAST(:match_reasons AS jsonb)
            )
            ON CONFLICT (tenant_id, original_booking_id, duplicate_booking_id)
            DO UPDATE SET match_reasons=EXCLUDED.match_reasons,
                          last_confirmed_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "original_booking_id": original["booking_id"],
            "duplicate_booking_id": context["booking_id"],
            "match_reasons": json.dumps(reasons),
        },
    )
    delivered = original["delivered_at"] is not None
    delivery_text = (
        f"DELIVERED ({original['delivered_at'].date().isoformat()})"
        if delivered
        else str(original["delivery_status"] or "NOT DELIVERED")
    )
    return AuditIssue(
        rule_key="BK_DUPLICATE_BOOKING",
        title="Duplicate Booking detected",
        description=(
            "The current Booking is later than an existing matching Booking and is therefore "
            "treated as the duplicate by default."
        ),
        observed_summary=(
            f"Original booking={original['booking_id']}; match reasons={','.join(reasons)}; "
            f"original delivery={delivery_text}"
        ),
    ), True


def _persist_issues(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    issues: list[AuditIssue],
    correlation_id: str | None,
) -> int:
    if not issues:
        return 0
    existing = {
        row["rule_key"]: row["audit_finding_id"]
        for row in connection.execute(
            text(
                """
                SELECT audit_finding_id, rule_key
                FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING' AND origin_kind='MACHINE'
                  AND finding_status IN ('OPEN','ACKNOWLEDGED')
                  AND rule_key = ANY(:rule_keys)
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "rule_keys": [issue.rule_key for issue in issues],
            },
        ).mappings().all()
    }
    updates = [issue for issue in issues if issue.rule_key in existing]
    if updates:
        connection.execute(
            text(
                """
                UPDATE auditcore.audit_findings
                SET severity=:severity, title=:title, description=:description,
                    expected_summary=:expected_summary, observed_summary=:observed_summary,
                    updated_at_utc=now(), version_no=version_no+1
                WHERE tenant_id=:tenant_id AND audit_finding_id=:finding_id
                  AND (
                        severity IS DISTINCT FROM :severity
                        OR title IS DISTINCT FROM :title
                        OR description IS DISTINCT FROM :description
                        OR expected_summary IS DISTINCT FROM :expected_summary
                        OR observed_summary IS DISTINCT FROM :observed_summary
                      )
                """
            ),
            [
                {
                    "tenant_id": tenant_id,
                    "finding_id": existing[issue.rule_key],
                    "severity": issue.severity,
                    "title": issue.title,
                    "description": issue.description,
                    "expected_summary": issue.expected_summary,
                    "observed_summary": issue.observed_summary,
                }
                for issue in updates
            ],
        )

    new_issues = [issue for issue in issues if issue.rule_key not in existing]
    if not new_issues:
        return 0
    rows = [(uuid4(), issue) for issue in new_issues]
    connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_findings (
                tenant_id, audit_finding_id, journey_id, finding_type_code,
                severity, finding_status, title, description,
                expected_summary, observed_summary, created_by_actor_id,
                correlation_id, stage_code, origin_kind, origin_actor_id,
                origin_role_snapshot, rule_key, blocking_completion
            ) VALUES (
                :tenant_id, :finding_id, :journey_id, :finding_type_code,
                :severity, 'OPEN', :title, :description,
                :expected_summary, :observed_summary, 'SYSTEM',
                :correlation_id, 'BOOKING', 'MACHINE', NULL,
                'SYSTEM', :rule_key, false
            )
            """
        ),
        [
            {
                "tenant_id": tenant_id,
                "finding_id": finding_id,
                "journey_id": journey_id,
                "finding_type_code": issue.rule_key[:100],
                "severity": issue.severity,
                "title": issue.title,
                "description": issue.description,
                "expected_summary": issue.expected_summary,
                "observed_summary": issue.observed_summary,
                "correlation_id": correlation_id,
                "rule_key": issue.rule_key,
            }
            for finding_id, issue in rows
        ],
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_finding_events (
                tenant_id, audit_finding_id, journey_id, stage_code,
                event_type, actor_id, actor_role_snapshot, safe_payload,
                correlation_id
            ) VALUES (
                :tenant_id, :finding_id, :journey_id, 'BOOKING',
                'RAISED', NULL, 'SYSTEM', CAST(:safe_payload AS jsonb),
                :correlation_id
            )
            """
        ),
        [
            {
                "tenant_id": tenant_id,
                "finding_id": finding_id,
                "journey_id": journey_id,
                "safe_payload": json.dumps(
                    {"originKind": "MACHINE", "ruleKey": issue.rule_key}
                ),
                "correlation_id": correlation_id,
            }
            for finding_id, issue in rows
        ],
    )
    return len(rows)


def _claim_outbox_event(
    connection: Connection,
    tenant_id: str,
    event_id: UUID,
) -> dict[str, Any] | None:
    row = connection.execute(
        text(
            """
            UPDATE auditcore.outbox_events
            SET event_status='PUBLISHING', attempt_count=attempt_count+1
            WHERE tenant_id=:tenant_id AND outbox_event_id=:event_id
              AND event_type=:event_type AND event_status='PENDING'
            RETURNING journey_id, event_payload, correlation_id, attempt_count
            """
        ),
        {
            "tenant_id": tenant_id,
            "event_id": event_id,
            "event_type": _AUDIT_EVENT_TYPE,
        },
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def process_booking_audit_event(
    engine: Engine,
    tenant_id: str,
    outbox_event_id: UUID,
) -> None:
    """FastAPI background task: one claimed outbox event, one compact rule run, no polling loop."""
    started = time.monotonic()
    journey_id: UUID | None = None
    correlation_id: str | None = None
    try:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            event = _claim_outbox_event(connection, tenant_id, outbox_event_id)
            if event is None:
                return
            journey_id = event["journey_id"]
            correlation_id = event["correlation_id"]
            logger.info(
                "uc03_booking_audit_started",
                tenant_id=tenant_id,
                journey_id=str(journey_id),
                outbox_event_id=str(outbox_event_id),
            )

            context = _load_context(connection, tenant_id, journey_id)
            if context is None:
                raise RuntimeError("Booking audit context not found")

            identity, identity_issue = _load_product_identity(connection, context=context)
            receipts = _load_verified_receipts(connection, tenant_id, journey_id)
            policy_rows = _load_minimum_booking_policy(connection, tenant_id)
            confirmation_date, _, _, confirmation_issue = derive_booking_confirmation_date(
                receipts,
                policy_rows,
                identity=identity,
            )
            if identity is None:
                sku = _resolution_from_identity_issue(
                    identity_issue
                    or AuditIssue(
                        rule_key="BK_SKU_NOT_FOUND",
                        title="Model/SKU not identified from Booking documents",
                        description=_SKU_NOT_FOUND_REMARK,
                    )
                )
            else:
                sku = _resolve_sku(
                    connection,
                    tenant_id=tenant_id,
                    confirmation_date=confirmation_date,
                    identity=identity,
                    colour_code=context.get("colour_code_snapshot"),
                    colour_name=context.get("colour_name_snapshot"),
                )
            confirmation_is_determinate = (
                confirmation_issue is None
                or confirmation_issue.rule_key == "BK_MINIMUM_BOOKING_AMOUNT_NOT_MET"
            )
            _persist_derived_fields(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                confirmation_date=confirmation_date,
                confirmation_is_determinate=confirmation_is_determinate,
                sku=sku,
            )

            issues: list[AuditIssue] = []
            if confirmation_issue is not None:
                issues.append(confirmation_issue)
            if sku.issue is not None:
                issues.append(sku.issue)

            price_issue = _price_issue(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                current_price_version_id=context.get("price_list_version_id"),
                confirmation_date=confirmation_date,
                sku_id=sku.product_sku_id,
            )
            if price_issue is not None:
                issues.append(price_issue)
            name_issue = _name_issue(connection, tenant_id, journey_id)
            if name_issue is not None:
                issues.append(name_issue)
            duplicate_issue, duplicate_linked = _duplicate_issue_and_link(
                connection,
                tenant_id=tenant_id,
                context=context,
            )
            if duplicate_issue is not None:
                issues.append(duplicate_issue)

            # Defensive de-duplication by business rule key before persistence.
            issues_by_rule = {issue.rule_key: issue for issue in issues}
            created = _persist_issues(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                issues=list(issues_by_rule.values()),
                correlation_id=correlation_id,
            )
            connection.execute(
                text(
                    """
                    UPDATE auditcore.outbox_events
                    SET event_status='PUBLISHED', published_at_utc=now(),
                        last_error_code=NULL, last_error_summary=NULL
                    WHERE tenant_id=:tenant_id AND outbox_event_id=:event_id
                    """
                ),
                {"tenant_id": tenant_id, "event_id": outbox_event_id},
            )
            logger.info(
                "uc03_booking_audit_completed",
                tenant_id=tenant_id,
                journey_id=str(journey_id),
                outbox_event_id=str(outbox_event_id),
                booking_confirmation_date=(
                    confirmation_date.isoformat() if confirmation_date else None
                ),
                sku_resolution_status=sku.status,
                issue_count=len(issues_by_rule),
                flags_created=created,
                duplicate_linked=duplicate_linked,
                elapsed_ms=round((time.monotonic() - started) * 1000, 2),
            )
    except Exception as exc:
        logger.exception(
            "uc03_booking_audit_failed",
            tenant_id=tenant_id,
            journey_id=str(journey_id) if journey_id else None,
            outbox_event_id=str(outbox_event_id),
            error_type=type(exc).__name__,
        )
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                connection.execute(
                    text(
                        """
                        UPDATE auditcore.outbox_events
                        SET event_status='FAILED',
                            attempt_count=CASE
                                WHEN event_status='PENDING' THEN attempt_count+1
                                ELSE attempt_count
                            END,
                            last_error_code='BOOKING_AUDIT_FAILED',
                            last_error_summary=:summary
                        WHERE tenant_id=:tenant_id AND outbox_event_id=:event_id
                          AND event_status IN ('PENDING','PUBLISHING')
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "event_id": outbox_event_id,
                        "summary": type(exc).__name__[:200],
                    },
                )
        except Exception:
            logger.exception(
                "uc03_booking_audit_failure_state_update_failed",
                tenant_id=tenant_id,
                outbox_event_id=str(outbox_event_id),
            )
