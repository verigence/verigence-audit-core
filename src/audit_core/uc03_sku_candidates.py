from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
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

_MODEL_WEIGHT = Decimal("0.40")
_VARIANT_WEIGHT = Decimal("0.35")
_COMMERCIAL_WEIGHT = Decimal("0.25")
_MIN_CANDIDATE_SCORE = Decimal("0.45")
_COMMERCIAL_ZERO_SCORE_DELTA = Decimal("0.30")
_SELECTION_METHOD = "BOOKING_COMMERCIAL_MATCH_V1"
_TENTATIVE_NOTE = "* Tentative — confirmation required"
_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


class SkuCandidateRequest(BaseModel):
    """Machine-observed Booking Form facts used for tentative SKU inference."""

    model_config = ConfigDict(extra="forbid")

    modelName: str = Field(min_length=1, max_length=200)
    variantName: str = Field(min_length=1, max_length=240)
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
    candidateStatus: Literal["TENTATIVE"] = "TENTATIVE"
    confirmationRequired: Literal[True] = True


class SkuCandidateResponse(BaseModel):
    journeyId: UUID
    effectiveOn: date
    priceListVersionId: UUID
    currencyCode: str
    status: Literal[
        "CONFIRMATION_REQUIRED",
        "NO_RELIABLE_CANDIDATE",
        "CONFIRMED_SKU_PRESERVED",
    ]
    selectionNote: str = _TENTATIVE_NOTE
    bookingRecordUpdated: bool
    mostLikelyProductSkuId: UUID | None = None
    processingMethod: Literal["MASTER_SQL_PLUS_DETERMINISTIC_PYTHON"] = (
        "MASTER_SQL_PLUS_DETERMINISTIC_PYTHON"
    )
    candidates: list[SkuCandidate]


def _normalize_label(value: str) -> str:
    return " ".join(part for part in _NON_ALNUM.sub(" ", value.upper()).split() if part)


def _label_similarity(observed: str, master: str) -> Decimal:
    left = _normalize_label(observed)
    right = _normalize_label(master)
    if not left or not right:
        return Decimal(0)
    if left == right or left.replace(" ", "") == right.replace(" ", ""):
        return Decimal(1)

    sequence = Decimal(str(SequenceMatcher(None, left, right).ratio()))
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    union = left_tokens | right_tokens
    token_jaccard = (
        Decimal(len(left_tokens & right_tokens)) / Decimal(len(union))
        if union
        else Decimal(0)
    )
    containment = (
        Decimal(1)
        if left.replace(" ", "") in right.replace(" ", "")
        or right.replace(" ", "") in left.replace(" ", "")
        else Decimal(0)
    )
    token_score = (token_jaccard * Decimal("0.85")) + (containment * Decimal("0.15"))
    return min(Decimal(1), max(sequence, token_score))


def _commercial_similarity(observed: Decimal, master: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    difference = abs(master - observed)
    difference_pct = difference / observed
    score = max(
        Decimal(0),
        Decimal(1) - (difference_pct / _COMMERCIAL_ZERO_SCORE_DELTA),
    )
    return score, difference, difference_pct


def _display_label(model_name: str, variant_name: str) -> str:
    return f"{model_name.strip()} {variant_name.strip()} *"


def rank_sku_candidates(
    rows: list[Mapping[str, Any]],
    *,
    model_name: str,
    variant_name: str,
    total_commercial_amount: Decimal,
    max_candidates: int,
) -> list[SkuCandidate]:
    """Rank effective-master rows using explainable text and commercial proximity."""

    scored: list[dict[str, Any]] = []
    for row in rows:
        master_total = Decimal(str(row["master_total_amount"]))
        model_score = _label_similarity(model_name, str(row["model_name"]))
        variant_score = _label_similarity(variant_name, str(row["variant_name"]))
        commercial_score, difference, difference_pct = _commercial_similarity(
            total_commercial_amount,
            master_total,
        )
        score = (
            model_score * _MODEL_WEIGHT
            + variant_score * _VARIANT_WEIGHT
            + commercial_score * _COMMERCIAL_WEIGHT
        )
        if score < _MIN_CANDIDATE_SCORE:
            continue
        scored.append(
            {
                "row": row,
                "score": score,
                "model_score": model_score,
                "variant_score": variant_score,
                "commercial_score": commercial_score,
                "difference": difference,
                "difference_pct": difference_pct,
            }
        )

    scored.sort(
        key=lambda item: (
            -item["score"],
            item["difference_pct"],
            str(item["row"]["sku_code"]),
        )
    )

    result: list[SkuCandidate] = []
    for index, item in enumerate(scored[:max_candidates], start=1):
        row = item["row"]
        result.append(
            SkuCandidate(
                rank=index,
                productSkuId=row["product_sku_id"],
                skuCode=str(row["sku_code"]),
                modelName=str(row["model_name"]),
                variantName=str(row["variant_name"]),
                colourName=(str(row["colour_name"]) if row.get("colour_name") else None),
                displayLabel=_display_label(str(row["model_name"]), str(row["variant_name"])),
                masterTotalAmount=Decimal(str(row["master_total_amount"])),
                observedTotalCommercialAmount=total_commercial_amount,
                commercialDifferenceAmount=item["difference"].quantize(Decimal("0.01")),
                commercialDifferencePercent=(item["difference_pct"] * Decimal(100)).quantize(
                    Decimal("0.01")
                ),
                score=item["score"].quantize(Decimal("0.0001")),
                modelScore=item["model_score"].quantize(Decimal("0.0001")),
                variantScore=item["variant_score"].quantize(Decimal("0.0001")),
                commercialScore=item["commercial_score"].quantize(Decimal("0.0001")),
            )
        )
    return result


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
                "effective date for a Segment; SKU candidates cannot be ranked safely."
            ),
        )
    return rows


def _persist_most_likely_sku(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    candidate: SkuCandidate,
) -> tuple[bool, bool]:
    """Persist the top candidate unless a confirmed SKU already exists.

    Returns (record_updated, confirmed_selection_preserved).
    """

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
                'TENTATIVE',
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
                selection_status='TENTATIVE',
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
            "selection_method": _SELECTION_METHOD,
            "selection_score": candidate.score,
        },
    )
    return result.rowcount > 0, False


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
    """Rank SKU candidates and persist the most likely result as tentative."""

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
    candidates = rank_sku_candidates(
        rows,
        model_name=command.modelName,
        variant_name=command.variantName,
        total_commercial_amount=command.totalCommercialAmount,
        max_candidates=command.maxCandidates,
    )

    if not candidates:
        return SkuCandidateResponse(
            journeyId=journey_id,
            effectiveOn=effective_on,
            priceListVersionId=price_list_version_id,
            currencyCode=plan_currency,
            status="NO_RELIABLE_CANDIDATE",
            bookingRecordUpdated=False,
            candidates=[],
        )

    most_likely = candidates[0]
    updated, confirmed_preserved = _persist_most_likely_sku(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        candidate=most_likely,
    )
    return SkuCandidateResponse(
        journeyId=journey_id,
        effectiveOn=effective_on,
        priceListVersionId=price_list_version_id,
        currencyCode=plan_currency,
        status=(
            "CONFIRMED_SKU_PRESERVED" if confirmed_preserved else "CONFIRMATION_REQUIRED"
        ),
        bookingRecordUpdated=updated,
        mostLikelyProductSkuId=most_likely.productSkuId,
        candidates=candidates,
    )
