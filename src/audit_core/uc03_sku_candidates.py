from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError
from audit_core.price_lists import (
    resolve_effective_price_list_version,
    resolve_effective_price_plan,
)
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_capture import _scope
from audit_core.uc03_booking_details import _effective_date

router = APIRouter(
    prefix="/v2/tenants/{tenant_id}/journeys/{journey_id}/booking",
    tags=["uc03-sku-candidates"],
)

_SELECTION_METHOD_DIRECT_SKU = "BOOKING_DIRECT_SKU_V1"
_SELECTION_METHOD_UNIQUE_MODEL_PRICE = "BOOKING_MODEL_PRICE_EXACT_V1"
_SELECTION_METHOD_TENTATIVE = "BOOKING_MODEL_PRICE_MULTI_EXACT_V1"
_TENTATIVE_NOTE = "* Tentative — multiple exact Booking matches; confirmation required"
_RESOLVED_NOTE = (
    "Resolved from exact Booking Form evidence; validate against Delivery Invoice later."
)
_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


class SkuCandidateRequest(BaseModel):
    """Machine-observed Booking Form facts used for exact SKU resolution."""

    model_config = ConfigDict(extra="forbid")

    modelName: str = Field(min_length=1, max_length=200)
    variantName: str | None = Field(default=None, min_length=1, max_length=240)
    colourName: str | None = Field(default=None, min_length=1, max_length=160)
    skuCode: str | None = Field(default=None, min_length=1, max_length=160)
    totalCommercialAmount: Decimal = Field(gt=0)
    currencyCode: str | None = Field(default=None, min_length=3, max_length=3)
    maxCandidates: int = Field(default=5, ge=1, le=10)


class SkuCandidate(BaseModel):
    rank: int
    productSkuId: UUID
    skuCode: str
    modelName: str
    variantName: str
    colourName: str | None = None
    displayLabel: str
    masterTotalAmount: Decimal
    observedTotalCommercialAmount: Decimal
    commercialDifferenceAmount: Decimal
    commercialDifferencePercent: Decimal
    score: Decimal
    modelScore: Decimal
    variantScore: Decimal
    commercialScore: Decimal
    candidateStatus: Literal["CONFIRMED", "TENTATIVE"]
    confirmationRequired: bool


class SkuCandidateResponse(BaseModel):
    journeyId: UUID
    effectiveOn: date
    priceListVersionId: UUID
    currencyCode: str
    status: Literal[
        "BOOKING_SKU_RESOLVED",
        "CONFIRMATION_REQUIRED",
        "CONFIRMED_SKU_PRESERVED",
    ]
    selectionNote: str
    bookingRecordUpdated: bool
    mostLikelyProductSkuId: UUID | None = None
    matchingRowCount: int = 0
    resolutionBasis: Literal[
        "DIRECT_SKU",
        "MODEL_PRICE_EXACT_UNIQUE",
        "MULTIPLE_EXACT_BOOKING_MATCHES",
        "EXISTING_CONFIRMED",
    ]
    deliveryValidationRequired: Literal[True] = True
    processingMethod: Literal["MASTER_SQL_PLUS_DETERMINISTIC_PYTHON"] = (
        "MASTER_SQL_PLUS_DETERMINISTIC_PYTHON"
    )
    candidates: list[SkuCandidate]


def _normalize_label(value: str) -> str:
    return " ".join(part for part in _NON_ALNUM.sub(" ", value.upper()).split() if part)


def _normalized_key(value: str) -> str:
    return _normalize_label(value).replace(" ", "")


def _label_similarity(observed: str, master: str) -> Decimal:
    """Return 1 only for formatting-normalized exact equality; never fuzzy-match."""

    left = _normalized_key(observed)
    right = _normalized_key(master)
    if not left or not right:
        return Decimal(0)
    return Decimal(1) if left == right else Decimal(0)


def _commercial_similarity(
    observed: Decimal,
    master: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return exact-price diagnostics; no price tolerance is ever applied."""

    difference = abs(master - observed)
    difference_pct = difference / observed
    score = Decimal(1) if difference == 0 else Decimal(0)
    return score, difference, difference_pct


def _display_label(
    model_name: str,
    variant_name: str,
    *,
    tentative: bool,
) -> str:
    label = f"{model_name.strip()} {variant_name.strip()}".strip()
    return f"{label} *" if tentative else label


def _candidate_from_row(
    row: Mapping[str, Any],
    *,
    rank: int,
    model_name: str,
    variant_name: str | None,
    total_commercial_amount: Decimal,
    tentative: bool,
) -> SkuCandidate:
    master_total = Decimal(str(row["master_total_amount"]))
    model_score = _label_similarity(model_name, str(row["model_name"]))
    variant_score = (
        _label_similarity(variant_name, str(row["variant_name"]))
        if variant_name
        else Decimal(1)
    )
    commercial_score, difference, difference_pct = _commercial_similarity(
        total_commercial_amount,
        master_total,
    )
    score = (
        (model_score * Decimal(2)) + variant_score + (commercial_score * Decimal(2))
    ) / Decimal(5)

    return SkuCandidate(
        rank=rank,
        productSkuId=row["product_sku_id"],
        skuCode=str(row["sku_code"]),
        modelName=str(row["model_name"]),
        variantName=str(row["variant_name"]),
        colourName=(str(row["colour_name"]) if row.get("colour_name") else None),
        displayLabel=_display_label(
            str(row["model_name"]),
            str(row["variant_name"]),
            tentative=tentative,
        ),
        masterTotalAmount=master_total,
        observedTotalCommercialAmount=total_commercial_amount,
        commercialDifferenceAmount=difference.quantize(Decimal("0.01")),
        commercialDifferencePercent=(difference_pct * Decimal(100)).quantize(
            Decimal("0.01")
        ),
        score=score.quantize(Decimal("0.0001")),
        modelScore=model_score.quantize(Decimal("0.0001")),
        variantScore=variant_score.quantize(Decimal("0.0001")),
        commercialScore=commercial_score.quantize(Decimal("0.0001")),
        candidateStatus="TENTATIVE" if tentative else "CONFIRMED",
        confirmationRequired=tentative,
    )


def rank_sku_candidates(
    rows: list[Mapping[str, Any]],
    *,
    model_name: str,
    variant_name: str | None,
    total_commercial_amount: Decimal,
    max_candidates: int,
    tentative: bool = True,
) -> list[SkuCandidate]:
    """Deterministically order already-exact Booking matches.

    This function never broadens the match set. Price proximity and fuzzy text
    similarity are deliberately excluded from candidate discovery.
    """

    ordered = sorted(
        rows,
        key=lambda row: (
            -int(
                bool(variant_name)
                and _label_similarity(variant_name or "", str(row["variant_name"]))
                == Decimal(1)
            ),
            str(row.get("colour_name") or ""),
            str(row["sku_code"]),
        ),
    )
    return [
        _candidate_from_row(
            row,
            rank=index,
            model_name=model_name,
            variant_name=variant_name,
            total_commercial_amount=total_commercial_amount,
            tentative=tentative,
        )
        for index, row in enumerate(ordered[:max_candidates], start=1)
    ]


def _exact_direct_sku_matches(
    rows: list[Mapping[str, Any]],
    *,
    sku_code: str | None,
) -> list[Mapping[str, Any]]:
    if not sku_code:
        return []
    observed = _normalized_key(sku_code)
    return [row for row in rows if _normalized_key(str(row["sku_code"])) == observed]


def _exact_booking_matches(
    rows: list[Mapping[str, Any]],
    *,
    model_name: str,
    variant_name: str | None,
    colour_name: str | None,
    total_commercial_amount: Decimal,
) -> list[Mapping[str, Any]]:
    """Return only exact Booking model + exact commercial-total master matches.

    Text equality is formatting-normalized only. There is no fuzzy matching and
    no price tolerance. Variant and colour can narrow an already exact
    model/price set when those facts are available on the Booking Form.
    """

    matches = [
        row
        for row in rows
        if _label_similarity(model_name, str(row["model_name"])) == Decimal(1)
        and Decimal(str(row["master_total_amount"])) == total_commercial_amount
    ]

    if len(matches) > 1 and variant_name:
        variant_matches = [
            row
            for row in matches
            if _label_similarity(variant_name, str(row["variant_name"])) == Decimal(1)
        ]
        if variant_matches:
            matches = variant_matches

    if len(matches) > 1 and colour_name:
        colour_matches = [
            row
            for row in matches
            if row.get("colour_name")
            and _label_similarity(colour_name, str(row["colour_name"])) == Decimal(1)
        ]
        if colour_matches:
            matches = colour_matches

    return matches


def _price_plan_for_journey(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    effective_on: date,
) -> dict[str, Any]:
    selected_price_list_id = connection.execute(
        text(
            """
            SELECT price_list_id
            FROM auditcore.bookings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()

    if selected_price_list_id is None:
        return resolve_effective_price_plan(
            connection,
            tenant_id=tenant_id,
            effective_on=effective_on,
        )

    version_id = resolve_effective_price_list_version(
        connection,
        tenant_id=tenant_id,
        price_list_id=selected_price_list_id,
        effective_on=effective_on,
    )
    row = connection.execute(
        text(
            """
            SELECT pl.price_list_id,
                   pl.price_list_code,
                   pl.price_list_name,
                   plv.price_list_version_id,
                   plv.version_no,
                   plv.effective_from,
                   plv.effective_to,
                   plv.currency_code,
                   plv.lifecycle_status
            FROM auditcore.price_list_versions plv
            JOIN auditcore.price_lists pl
              ON pl.tenant_id=plv.tenant_id
             AND pl.price_list_id=plv.price_list_id
            WHERE plv.tenant_id=:tenant_id
              AND plv.price_list_version_id=:version_id
            """
        ),
        {"tenant_id": tenant_id, "version_id": version_id},
    ).mappings().one()
    return dict(row)


def _effective_sku_rows(
    connection: Connection,
    *,
    tenant_id: str,
    effective_on: date,
    price_list_version_id: UUID,
) -> list[Mapping[str, Any]]:
    rows = connection.execute(
        text(
            """
            WITH eligible_versions AS (
                SELECT ppm.segment_id,
                       ppmv.version_id,
                       ppmv.effective_from,
                       DENSE_RANK() OVER (
                           PARTITION BY ppm.segment_id
                           ORDER BY ppmv.effective_from DESC
                       ) AS effective_rank,
                       COUNT(*) OVER (
                           PARTITION BY ppm.segment_id, ppmv.effective_from
                       ) AS same_date_version_count
                FROM auditcore.project_product_masters ppm
                JOIN auditcore.project_product_master_versions ppmv
                  ON ppmv.tenant_id=ppm.tenant_id
                 AND ppmv.product_master_id=ppm.product_master_id
                WHERE ppm.tenant_id=:tenant_id
                  AND ppm.status='ACTIVE'
                  AND ppmv.lifecycle_status='PUBLISHED'
                  AND ppmv.effective_from <= :effective_on
            ),
            effective_versions AS (
                SELECT segment_id, version_id, same_date_version_count
                FROM eligible_versions
                WHERE effective_rank=1
            )
            SELECT pmi.product_sku_id,
                   s.sku_code,
                   pm.model_name,
                   pv.variant_name,
                   c.colour_name,
                   ev.same_date_version_count,
                   SUM(pli.standard_amount) AS master_total_amount
            FROM effective_versions ev
            JOIN auditcore.project_product_master_items pmi
              ON pmi.tenant_id=:tenant_id
             AND pmi.version_id=ev.version_id
            JOIN auditcore.product_skus s
              ON s.product_sku_id=pmi.product_sku_id
            JOIN auditcore.product_models pm
              ON pm.model_id=s.model_id
            JOIN auditcore.product_variants pv
              ON pv.variant_id=s.variant_id
            LEFT JOIN auditcore.colours c
              ON c.colour_id=s.colour_id
            JOIN auditcore.price_list_items pli
              ON pli.tenant_id=:tenant_id
             AND pli.price_list_version_id=:price_list_version_id
             AND pli.product_sku_id=s.product_sku_id
            WHERE s.is_active=true
              AND pm.is_active=true
              AND pv.is_active=true
              AND (c.colour_id IS NULL OR c.is_active=true)
            GROUP BY pmi.product_sku_id, s.sku_code, pm.model_name,
                     pv.variant_name, c.colour_name, ev.same_date_version_count
            ORDER BY pm.model_name, pv.variant_name, s.sku_code
            LIMIT 1000
            """
        ),
        {
            "tenant_id": tenant_id,
            "effective_on": effective_on,
            "price_list_version_id": price_list_version_id,
        },
    ).mappings().all()

    if any(int(row["same_date_version_count"]) > 1 for row in rows):
        raise AuditCoreError(
            error_code="VAC-MASTER-003",
            status_code=409,
            title="Product Master configuration conflict",
            detail=(
                "Multiple published Product Master versions share the latest applicable "
                "effective date for a Segment; Booking SKU cannot be resolved safely."
            ),
        )
    return rows


def _persist_sku_selection(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    candidate: SkuCandidate,
    selection_status: Literal["CONFIRMED", "TENTATIVE"],
    selection_method: str,
) -> tuple[bool, bool]:
    """Persist a Booking-derived SKU unless a confirmed SKU already exists."""

    existing_status = connection.execute(
        text(
            """
            SELECT selection_status
            FROM auditcore.journey_products
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()
    if existing_status == "CONFIRMED":
        return False, True

    result = connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_products (
                tenant_id,
                journey_id,
                product_sku_id,
                model_code_snapshot,
                model_name_snapshot,
                variant_code_snapshot,
                variant_name_snapshot,
                colour_code_snapshot,
                colour_name_snapshot,
                selection_source,
                selection_status,
                selection_method,
                selection_score
            )
            SELECT
                :tenant_id,
                :journey_id,
                s.product_sku_id,
                pm.model_code,
                pm.model_name,
                pv.variant_code,
                pv.variant_name,
                c.colour_code,
                c.colour_name,
                'EVIDENCE',
                :selection_status,
                :selection_method,
                :selection_score
            FROM auditcore.product_skus s
            JOIN auditcore.product_models pm ON pm.model_id=s.model_id
            JOIN auditcore.product_variants pv ON pv.variant_id=s.variant_id
            LEFT JOIN auditcore.colours c ON c.colour_id=s.colour_id
            WHERE s.product_sku_id=:product_sku_id
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                product_sku_id=EXCLUDED.product_sku_id,
                model_code_snapshot=EXCLUDED.model_code_snapshot,
                model_name_snapshot=EXCLUDED.model_name_snapshot,
                variant_code_snapshot=EXCLUDED.variant_code_snapshot,
                variant_name_snapshot=EXCLUDED.variant_name_snapshot,
                colour_code_snapshot=EXCLUDED.colour_code_snapshot,
                colour_name_snapshot=EXCLUDED.colour_name_snapshot,
                selection_source='EVIDENCE',
                selection_status=EXCLUDED.selection_status,
                selection_method=EXCLUDED.selection_method,
                selection_score=EXCLUDED.selection_score,
                updated_at_utc=now()
            WHERE auditcore.journey_products.selection_status IS DISTINCT FROM 'CONFIRMED'
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "product_sku_id": candidate.productSkuId,
            "selection_status": selection_status,
            "selection_method": selection_method,
            "selection_score": candidate.score,
        },
    )
    return result.rowcount > 0, False


def _resolved_candidate(
    row: Mapping[str, Any],
    *,
    model_name: str,
    variant_name: str | None,
    total_commercial_amount: Decimal,
) -> SkuCandidate:
    return _candidate_from_row(
        row,
        rank=1,
        model_name=model_name,
        variant_name=variant_name,
        total_commercial_amount=total_commercial_amount,
        tentative=False,
    )


def _raise_model_not_found(
    *,
    model_name: str,
    total_commercial_amount: Decimal,
) -> None:
    raise AuditCoreError(
        error_code="VAC-SKU-001",
        status_code=422,
        title="Model not found in masters",
        detail=(
            "No exact effective Product/Price Master row matches Booking Form model "
            f"'{model_name}' and total commercial amount {total_commercial_amount}. "
            "No tolerance or fuzzy fallback is permitted."
        ),
    )


@router.post("/sku-candidates", response_model=SkuCandidateResponse)
def derive_sku_candidates(
    tenant_id: str,
    journey_id: UUID,
    command: SkuCandidateRequest,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> SkuCandidateResponse:
    """Resolve an exact Booking SKU or flag the Booking when no exact match exists."""

    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    effective_on = date.fromisoformat(
        _effective_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    )
    plan = _price_plan_for_journey(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        effective_on=effective_on,
    )
    plan_currency = str(plan["currency_code"]).upper()
    if command.currencyCode and command.currencyCode.upper() != plan_currency:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Commercial currency mismatch",
            detail=(
                "The extracted Booking commercial currency does not match the effective "
                "Price Master currency."
            ),
        )

    price_list_version_id = plan["price_list_version_id"]
    rows = _effective_sku_rows(
        connection,
        tenant_id=tenant_id,
        effective_on=effective_on,
        price_list_version_id=price_list_version_id,
    )

    direct_sku_matches = _exact_direct_sku_matches(rows, sku_code=command.skuCode)
    if len(direct_sku_matches) > 1:
        raise AuditCoreError(
            error_code="VAC-MASTER-004",
            status_code=409,
            title="Product Master SKU conflict",
            detail="The Booking Form SKU code maps to multiple effective Product Master rows.",
        )
    if len(direct_sku_matches) == 1:
        candidate = _resolved_candidate(
            direct_sku_matches[0],
            model_name=command.modelName,
            variant_name=command.variantName,
            total_commercial_amount=command.totalCommercialAmount,
        )
        updated, confirmed_preserved = _persist_sku_selection(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            candidate=candidate,
            selection_status="CONFIRMED",
            selection_method=_SELECTION_METHOD_DIRECT_SKU,
        )
        return SkuCandidateResponse(
            journeyId=journey_id,
            effectiveOn=effective_on,
            priceListVersionId=price_list_version_id,
            currencyCode=plan_currency,
            status=(
                "CONFIRMED_SKU_PRESERVED"
                if confirmed_preserved
                else "BOOKING_SKU_RESOLVED"
            ),
            selectionNote=_RESOLVED_NOTE,
            bookingRecordUpdated=updated,
            mostLikelyProductSkuId=candidate.productSkuId,
            matchingRowCount=1,
            resolutionBasis=(
                "EXISTING_CONFIRMED" if confirmed_preserved else "DIRECT_SKU"
            ),
            candidates=[candidate],
        )

    exact_matches = _exact_booking_matches(
        rows,
        model_name=command.modelName,
        variant_name=command.variantName,
        colour_name=command.colourName,
        total_commercial_amount=command.totalCommercialAmount,
    )
    if not exact_matches:
        _raise_model_not_found(
            model_name=command.modelName,
            total_commercial_amount=command.totalCommercialAmount,
        )

    if len(exact_matches) == 1:
        candidate = _resolved_candidate(
            exact_matches[0],
            model_name=command.modelName,
            variant_name=command.variantName,
            total_commercial_amount=command.totalCommercialAmount,
        )
        updated, confirmed_preserved = _persist_sku_selection(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            candidate=candidate,
            selection_status="CONFIRMED",
            selection_method=_SELECTION_METHOD_UNIQUE_MODEL_PRICE,
        )
        return SkuCandidateResponse(
            journeyId=journey_id,
            effectiveOn=effective_on,
            priceListVersionId=price_list_version_id,
            currencyCode=plan_currency,
            status=(
                "CONFIRMED_SKU_PRESERVED"
                if confirmed_preserved
                else "BOOKING_SKU_RESOLVED"
            ),
            selectionNote=_RESOLVED_NOTE,
            bookingRecordUpdated=updated,
            mostLikelyProductSkuId=candidate.productSkuId,
            matchingRowCount=1,
            resolutionBasis=(
                "EXISTING_CONFIRMED"
                if confirmed_preserved
                else "MODEL_PRICE_EXACT_UNIQUE"
            ),
            candidates=[candidate],
        )

    candidates = rank_sku_candidates(
        exact_matches,
        model_name=command.modelName,
        variant_name=command.variantName,
        total_commercial_amount=command.totalCommercialAmount,
        max_candidates=command.maxCandidates,
        tentative=True,
    )
    most_likely = candidates[0]
    updated, confirmed_preserved = _persist_sku_selection(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        candidate=most_likely,
        selection_status="TENTATIVE",
        selection_method=_SELECTION_METHOD_TENTATIVE,
    )
    return SkuCandidateResponse(
        journeyId=journey_id,
        effectiveOn=effective_on,
        priceListVersionId=price_list_version_id,
        currencyCode=plan_currency,
        status=(
            "CONFIRMED_SKU_PRESERVED" if confirmed_preserved else "CONFIRMATION_REQUIRED"
        ),
        selectionNote=_TENTATIVE_NOTE,
        bookingRecordUpdated=updated,
        mostLikelyProductSkuId=most_likely.productSkuId,
        matchingRowCount=len(exact_matches),
        resolutionBasis=(
            "EXISTING_CONFIRMED"
            if confirmed_preserved
            else "MULTIPLE_EXACT_BOOKING_MATCHES"
        ),
        candidates=candidates,
    )
