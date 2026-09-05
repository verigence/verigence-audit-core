from __future__ import annotations

import os
from collections import defaultdict
from typing import Annotated, Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.di_client import DiClient, DiClientError
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient, SecurityTokenError
from audit_core.uc03_document_capture_v2 import _ensure_di_context
from audit_core.uc03_document_review_v2 import _build_attributes, _review_document
from audit_core.uc03_fast_work_items import _authorize_workspace

logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/uc03",
    tags=["uc03-work-item-enrichment"],
)

_BOOKING_PRODUCT_DOCUMENT_TYPES = {"booking_form", "booking_docket"}
_PRODUCT_ATTRIBUTE_KEYS = {"model", "variant", "color"}


class WorkItemEnrichmentRequest(BaseModel):
    journeyIds: list[UUID] = Field(min_length=1, max_length=10)


class WorkItemProductEnrichment(BaseModel):
    journeyId: UUID
    model: str | None = None
    variant: str | None = None
    colour: str | None = None
    productLabel: str | None = None
    source: Literal["CORE", "DI"]


class WorkItemEnrichmentResponse(BaseModel):
    items: list[WorkItemProductEnrichment]


def _normalized_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    return normalized or None


def _product_values(attributes: list[Any]) -> dict[str, str | None]:
    resolved: dict[str, str | None] = {"model": None, "variant": None, "color": None}
    for attribute in attributes:
        key = str(attribute.attributeKey).strip().lower()
        if key not in _PRODUCT_ATTRIBUTE_KEYS:
            continue
        if str(attribute.reviewState).upper() != "READY":
            continue
        resolved[key] = _normalized_text(attribute.resolvedValue)
    return resolved


def _product_label(*, model: str | None, variant: str | None, colour: str | None) -> str | None:
    parts = [value for value in (model, variant, colour) if value]
    return " · ".join(parts) if parts else None


def _authorized_journey_ids(
    connection: Connection,
    *,
    tenant_id: str,
    actor_id: str,
    journey_ids: list[UUID],
) -> set[UUID]:
    params: dict[str, Any] = {"tenant_id": tenant_id, "actor_id": actor_id}
    placeholders: list[str] = []
    for index, journey_id in enumerate(journey_ids):
        key = f"journey_{index}"
        params[key] = journey_id
        placeholders.append(f":{key}")
    if not placeholders:
        return set()

    rows = connection.execute(
        text(
            f"""
            SELECT j.journey_id
            FROM auditcore.journeys j
            WHERE j.tenant_id=:tenant_id
              AND j.journey_id IN ({', '.join(placeholders)})
              AND EXISTS (
                    SELECT 1
                    FROM auditcore.business_assignments ba
                    WHERE ba.tenant_id=j.tenant_id
                      AND ba.security_actor_id=:actor_id
                      AND ba.assignment_status='ACTIVE'
                      AND ba.effective_from <= now()
                      AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                      AND (
                            ba.dealer_id IS NULL
                            OR (
                                ba.dealer_id=j.dealer_id
                                AND (ba.outlet_id IS NULL OR ba.outlet_id=j.outlet_id)
                            )
                      )
              )
            """
        ),
        params,
    ).scalars().all()
    return {UUID(str(value)) for value in rows}


def _existing_products(
    connection: Connection,
    *,
    tenant_id: str,
    journey_ids: list[UUID],
) -> dict[UUID, WorkItemProductEnrichment]:
    params: dict[str, Any] = {"tenant_id": tenant_id}
    placeholders: list[str] = []
    for index, journey_id in enumerate(journey_ids):
        key = f"journey_{index}"
        params[key] = journey_id
        placeholders.append(f":{key}")
    if not placeholders:
        return {}

    rows = connection.execute(
        text(
            f"""
            SELECT journey_id, model_name_snapshot, variant_name_snapshot,
                   colour_name_snapshot
            FROM auditcore.journey_products
            WHERE tenant_id=:tenant_id
              AND journey_id IN ({', '.join(placeholders)})
            """
        ),
        params,
    ).mappings().all()
    result: dict[UUID, WorkItemProductEnrichment] = {}
    for row in rows:
        model = _normalized_text(row["model_name_snapshot"])
        variant = _normalized_text(row["variant_name_snapshot"])
        colour = _normalized_text(row["colour_name_snapshot"])
        label = _product_label(model=model, variant=variant, colour=colour)
        if not label:
            continue
        journey_id = UUID(str(row["journey_id"]))
        result[journey_id] = WorkItemProductEnrichment(
            journeyId=journey_id,
            model=model,
            variant=variant,
            colour=colour,
            productLabel=label,
            source="CORE",
        )
    return result


def _booking_product_documents(
    connection: Connection,
    *,
    tenant_id: str,
    journey_ids: list[UUID],
) -> dict[UUID, list[dict[str, Any]]]:
    params: dict[str, Any] = {"tenant_id": tenant_id}
    placeholders: list[str] = []
    for index, journey_id in enumerate(journey_ids):
        key = f"journey_{index}"
        params[key] = journey_id
        placeholders.append(f":{key}")
    if not placeholders:
        return {}

    rows = connection.execute(
        text(
            f"""
            SELECT journey_id, di_document_id, requirement_key,
                   classified_document_type_key, original_filename, capture_status
            FROM auditcore.document_capture_v2_documents
            WHERE tenant_id=:tenant_id
              AND journey_id IN ({', '.join(placeholders)})
              AND stage_code='BOOKING'
              AND capture_status='CLASSIFIED'
              AND (
                    lower(COALESCE(classified_document_type_key, ''))
                        IN ('booking_form', 'booking_docket')
                    OR lower(COALESCE(requirement_key, ''))
                        IN ('booking_form', 'booking_docket')
              )
            ORDER BY created_at_utc DESC, di_document_id DESC
            """
        ),
        params,
    ).mappings().all()
    grouped: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[UUID(str(row["journey_id"]))].append(dict(row))
    return dict(grouped)


def _persist_evidence_product(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    model: str | None,
    variant: str | None,
    colour: str | None,
) -> WorkItemProductEnrichment | None:
    if not any((model, variant, colour)):
        return None

    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_products (
                tenant_id, journey_id, model_name_snapshot,
                variant_name_snapshot, colour_name_snapshot, selection_source
            ) VALUES (
                :tenant_id, :journey_id, :model, :variant, :colour, 'EVIDENCE'
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                model_name_snapshot=CASE
                    WHEN auditcore.journey_products.product_sku_id IS NULL
                     AND COALESCE(auditcore.journey_products.selection_source, 'EVIDENCE')='EVIDENCE'
                    THEN COALESCE(EXCLUDED.model_name_snapshot, auditcore.journey_products.model_name_snapshot)
                    ELSE auditcore.journey_products.model_name_snapshot
                END,
                variant_name_snapshot=CASE
                    WHEN auditcore.journey_products.product_sku_id IS NULL
                     AND COALESCE(auditcore.journey_products.selection_source, 'EVIDENCE')='EVIDENCE'
                    THEN COALESCE(EXCLUDED.variant_name_snapshot, auditcore.journey_products.variant_name_snapshot)
                    ELSE auditcore.journey_products.variant_name_snapshot
                END,
                colour_name_snapshot=CASE
                    WHEN auditcore.journey_products.product_sku_id IS NULL
                     AND COALESCE(auditcore.journey_products.selection_source, 'EVIDENCE')='EVIDENCE'
                    THEN COALESCE(EXCLUDED.colour_name_snapshot, auditcore.journey_products.colour_name_snapshot)
                    ELSE auditcore.journey_products.colour_name_snapshot
                END,
                selection_source=CASE
                    WHEN auditcore.journey_products.product_sku_id IS NULL
                     AND COALESCE(auditcore.journey_products.selection_source, 'EVIDENCE')='EVIDENCE'
                    THEN 'EVIDENCE'
                    ELSE auditcore.journey_products.selection_source
                END,
                updated_at_utc=now()
            RETURNING model_name_snapshot, variant_name_snapshot,
                      colour_name_snapshot
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "model": model,
            "variant": variant,
            "colour": colour,
        },
    ).mappings().one()
    stored_model = _normalized_text(row["model_name_snapshot"])
    stored_variant = _normalized_text(row["variant_name_snapshot"])
    stored_colour = _normalized_text(row["colour_name_snapshot"])
    label = _product_label(
        model=stored_model,
        variant=stored_variant,
        colour=stored_colour,
    )
    if not label:
        return None
    return WorkItemProductEnrichment(
        journeyId=journey_id,
        model=stored_model,
        variant=stored_variant,
        colour=stored_colour,
        productLabel=label,
        source="DI",
    )


def _di_configuration() -> tuple[str, str, str, str] | None:
    di_base_url = os.environ.get("DI_BASE_URL", "").strip()
    security_base_url = os.environ.get("SECURITY_BASE_URL", "").strip()
    client_id = os.environ.get("SECURITY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SECURITY_CLIENT_SECRET", "")
    if not di_base_url or not security_base_url or not client_id or not client_secret:
        return None
    return di_base_url, security_base_url, client_id, client_secret


@router.post("/work-items/enrich", response_model=WorkItemEnrichmentResponse)
def enrich_work_items(
    tenant_id: str,
    command: WorkItemEnrichmentRequest,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> WorkItemEnrichmentResponse:
    """Best-effort post-paint enrichment for at most one Work Queue page.

    The fast Work Queue request remains unchanged. This endpoint is called only after
    the first page is already visible and only for rows whose product label is absent.
    High-confidence (>= Review threshold) Booking Form/Docket product facts are copied
    into the existing journey_products snapshot owner. Low-confidence values never
    become the PC-facing vehicle summary.
    """

    _authorize_workspace(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    set_tenant_context(connection, tenant_id)

    requested = list(dict.fromkeys(command.journeyIds))
    authorized = _authorized_journey_ids(
        connection,
        tenant_id=tenant_id,
        actor_id=human_principal.subject,
        journey_ids=requested,
    )
    scoped = [journey_id for journey_id in requested if journey_id in authorized]
    existing = _existing_products(connection, tenant_id=tenant_id, journey_ids=scoped)
    missing = [journey_id for journey_id in scoped if journey_id not in existing]
    if not missing:
        return WorkItemEnrichmentResponse(items=[existing[journey_id] for journey_id in scoped if journey_id in existing])

    documents_by_journey = _booking_product_documents(
        connection,
        tenant_id=tenant_id,
        journey_ids=missing,
    )
    configuration = _di_configuration()
    if configuration is None:
        return WorkItemEnrichmentResponse(items=[existing[journey_id] for journey_id in scoped if journey_id in existing])

    di_base_url, security_base_url, client_id, client_secret = configuration
    enriched = dict(existing)
    with SecurityOAuthClient(
        base_url=security_base_url,
        client_id=client_id,
        client_secret=client_secret,
    ) as security_client, DiClient(base_url=di_base_url) as di_client:
        for journey_id in missing:
            linked_documents = documents_by_journey.get(journey_id, [])
            if not linked_documents:
                continue
            try:
                context_ref, token = _ensure_di_context(
                    connection=connection,
                    engine=connection.engine,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    security_client=security_client,
                    di_client=di_client,
                )
                review_documents = []
                for linked in linked_documents:
                    document_type = _normalized_text(linked.get("classified_document_type_key"))
                    requirement_key = _normalized_text(linked.get("requirement_key"))
                    source_key = (document_type or requirement_key or "").lower()
                    if source_key not in _BOOKING_PRODUCT_DOCUMENT_TYPES:
                        continue
                    review_document = _review_document(
                        token=token,
                        tenant_id=tenant_id,
                        context_ref=context_ref,
                        di_client=di_client,
                        document_id=UUID(str(linked["di_document_id"])),
                        label="Booking Form",
                        original_filename=str(linked.get("original_filename") or linked["di_document_id"]),
                        document_type_key=document_type or requirement_key,
                        requirement_key=requirement_key,
                        content_url=None,
                        processing_status_hint=None,
                    )
                    if review_document.extractionState == "READY":
                        review_documents.append(review_document)
                if not review_documents:
                    continue
                attributes, _ = _build_attributes(review_documents, stages=("BOOKING",))
                values = _product_values(attributes)
                with connection.begin_nested():
                    product = _persist_evidence_product(
                        connection,
                        tenant_id=tenant_id,
                        journey_id=journey_id,
                        model=values["model"],
                        variant=values["variant"],
                        colour=values["color"],
                    )
                if product is not None:
                    enriched[journey_id] = product
            except (DiClientError, SecurityTokenError, RuntimeError, SQLAlchemyError) as exc:
                logger.warning(
                    "uc03_work_item_product_enrichment_skipped",
                    tenant_id=tenant_id,
                    journey_id=str(journey_id),
                    reason=type(exc).__name__,
                )

    return WorkItemEnrichmentResponse(
        items=[enriched[journey_id] for journey_id in scoped if journey_id in enriched]
    )
