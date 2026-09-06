"""Materialize confirmed DI facts into canonical UC03 business tables immediately.

This closes the asynchronous Booking case where the PC submits while DI is still
processing. Confidence controls review only; it never controls persistence.

The durable DI copy in journey_document_extracted_fields remains the lossless source
record. This module recomputes preferred Journey values from that Audit Core copy and
projects every currently mapped value into its canonical business owner without
waiting for PC Review Confirm.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, Request, Response
from sqlalchemy import Connection, text

from audit_core import uc03_booking_capture as booking_capture
from audit_core import uc03_journey_reviewed_details as reviewed_details
from audit_core import uc03_v2_review_materialization as materialization
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError, DependencyUnavailableError
from audit_core.evidence import get_di_client, get_security_oauth_client
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_attribute_mapping import spec_for_field
from audit_core.uc03_attribute_resolution import apply_supported_operational_attribute

logger = logging.getLogger(__name__)

_MACHINE_ACTOR = "SYSTEM:DI_AUTO"
_IDENTITY_REVIEW_SENSITIVE = {
    "customer_name",
    "customer_relationship_type",
    "customer_relationship_name",
}
_PRODUCT_SEMANTICS = {
    "model": "model_name_snapshot",
    "variant": "variant_name_snapshot",
    "color": "colour_name_snapshot",
}


def _confidence_percent(row: dict[str, Any]) -> float | None:
    score = row.get("confidenceScore")
    if score is None:
        return None
    value = float(score)
    if str(row.get("confidenceScale") or "").upper() == "UNIT_INTERVAL":
        return value * 100.0
    return value


def _preferred_rows(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Resolve current values from Audit Core only, using the shared precedence rule."""

    rows = reviewed_details.load_reviewed_field_details(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    return reviewed_details.annotate_and_resolve_reviewed_fields(rows)


def _winner_by_id(
    annotated: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        str(row["reviewedFieldId"]): row
        for row in annotated
        if row.get("reviewedFieldId") is not None
    }


def _materialize_product(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    resolved: dict[str, dict[str, Any]],
) -> int:
    values: dict[str, Any] = {}
    for semantic, column in _PRODUCT_SEMANTICS.items():
        item = resolved.get(semantic)
        if item and item.get("value") not in (None, ""):
            values[column] = " ".join(str(item["value"]).split())
    if not values:
        return 0

    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_products (
                tenant_id, journey_id,
                model_name_snapshot, variant_name_snapshot, colour_name_snapshot,
                selection_source
            ) VALUES (
                :tenant_id, :journey_id,
                :model_name_snapshot, :variant_name_snapshot, :colour_name_snapshot,
                'EVIDENCE'
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                model_name_snapshot=COALESCE(EXCLUDED.model_name_snapshot, auditcore.journey_products.model_name_snapshot),
                variant_name_snapshot=COALESCE(EXCLUDED.variant_name_snapshot, auditcore.journey_products.variant_name_snapshot),
                colour_name_snapshot=COALESCE(EXCLUDED.colour_name_snapshot, auditcore.journey_products.colour_name_snapshot),
                selection_source='EVIDENCE',
                updated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "model_name_snapshot": values.get("model_name_snapshot"),
            "variant_name_snapshot": values.get("variant_name_snapshot"),
            "colour_name_snapshot": values.get("colour_name_snapshot"),
        },
    )
    return len(values)


def _materialize_commercial(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    winner: dict[str, Any],
) -> bool:
    field_key = str(winner.get("fieldKey") or "").strip().lower()
    if field_key not in materialization._COMMERCIAL_LINE_FIELDS:
        return False
    value = winner.get("effectiveValue")
    if value in (None, ""):
        return False
    amount = booking_capture._as_decimal(value, field_key.upper())
    evidence_id = winner.get("evidenceId")
    document_id = winner.get("documentId")
    document_type = str(winner.get("documentTypeKey") or "di").strip().lower()
    connection.execute(
        text(
            """
            INSERT INTO auditcore.commercial_lines (
                tenant_id, journey_id, component_key, actual_amount,
                actual_source_kind, source_evidence_id, source_reference
            ) VALUES (
                :tenant_id, :journey_id, :component_key, :actual_amount,
                'EVIDENCE', :evidence_id, :source_reference
            )
            ON CONFLICT (tenant_id, journey_id, component_key)
            DO UPDATE SET
                actual_amount=EXCLUDED.actual_amount,
                actual_source_kind='EVIDENCE',
                source_evidence_id=EXCLUDED.source_evidence_id,
                source_reference=EXCLUDED.source_reference,
                updated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "component_key": field_key,
            "actual_amount": amount,
            "evidence_id": evidence_id,
            "source_reference": f"{document_type}:{document_id}",
        },
    )
    return True


def _receipt_documents(annotated: list[dict[str, Any]]) -> list[Any]:
    grouped: dict[UUID, dict[str, Any]] = {}
    for row in annotated:
        if str(row.get("stageCode") or "").upper() != "BOOKING":
            continue
        if str(row.get("documentTypeKey") or "").strip().lower() != "dealer_receipt":
            continue
        if not row.get("hasEffectiveValue"):
            continue
        document_id = UUID(str(row["documentId"]))
        item = grouped.setdefault(
            document_id,
            {
                "documentId": document_id,
                "evidenceId": UUID(str(row["evidenceId"])) if row.get("evidenceId") else None,
                "documentTypeKey": "dealer_receipt",
                "extractionState": "READY",
                "fields": [],
            },
        )
        item["fields"].append(
            SimpleNamespace(
                fieldKey=str(row.get("fieldKey") or ""),
                value=row.get("effectiveValue"),
            )
        )
    return [SimpleNamespace(**item) for item in grouped.values()]


def materialize_machine_booking_values(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> dict[str, int]:
    """Project the latest DI-backed Journey truth into mapped Booking Core owners.

    All DI facts are already durable in journey_document_extracted_fields. This step
    projects the currently preferred values. Low-confidence values are included too;
    the separate confidence policy raises/maintains their PC-review finding.
    """

    annotated, resolved = _preferred_rows(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    winner_rows = _winner_by_id(annotated)
    operational = 0
    commercial = 0
    skipped_invalid = 0

    for semantic, selected in resolved.items():
        reviewed_id = selected.get("reviewedFieldId")
        winner = winner_rows.get(str(reviewed_id)) if reviewed_id is not None else None
        if winner is None or str(winner.get("stageCode") or "").upper() != "BOOKING":
            continue
        field_key = str(winner.get("fieldKey") or "").strip()
        if not field_key or not winner.get("hasEffectiveValue"):
            continue

        try:
            if _materialize_commercial(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                winner=winner,
            ):
                commercial += 1

            spec = spec_for_field(field_key)
            if spec is None or spec.mapping_status != "SUPPORTED":
                continue

            # PAN/Aadhaar remain the selected customer source of truth. If that
            # selected identity value is <90%, keep it visible/durable in Core and
            # flagged for PC review, but do not falsely mark customer identity as
            # VERIFIED through the review-specific typed writer.
            confidence = _confidence_percent(winner)
            if semantic in _IDENTITY_REVIEW_SENSITIVE and (
                confidence is None or confidence < 90.0
            ):
                continue

            result = apply_supported_operational_attribute(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                spec=spec,
                value=winner.get("effectiveValue"),
                actor_id=_MACHINE_ACTOR,
                source_document_type_key=winner.get("documentTypeKey"),
                source_field_key=field_key,
                source_evidence_id=(
                    UUID(str(winner["evidenceId"])) if winner.get("evidenceId") else None
                ),
            )
            if result is not None:
                operational += 1
        except (AuditCoreError, TypeError, ValueError) as exc:
            # Never lose the extracted fact because a stricter typed owner cannot
            # accept its presentation. The lossless Core row and Detail View remain
            # available; the typed projection can be corrected by PC review.
            skipped_invalid += 1
            logger.warning(
                "uc03_machine_materialization_skipped",
                extra={
                    "tenant_id": tenant_id,
                    "journey_id": str(journey_id),
                    "field_key": field_key,
                    "reason": str(exc),
                },
            )

    product = _materialize_product(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        resolved=resolved,
    )

    receipt_result = materialization.materialize_reviewed_booking_receipts(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=_receipt_documents(annotated),
        rejected_review_keys=set(),
        actor_id=None,
    )

    return {
        "operationalOwners": operational,
        "commercialLines": commercial,
        "productFields": product,
        "paymentsCreated": receipt_result["created"],
        "paymentsUpdated": receipt_result["updated"],
        "paymentsUnchanged": receipt_result["unchanged"],
        "typedValuesSkipped": skipped_invalid,
    }


def _v2_booking_document_ids(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> list[UUID]:
    """Return only V2 capture documents eligible for the pre-submit race check."""

    return list(
        connection.execute(
            text(
                """
                SELECT DISTINCT e.di_document_id
                FROM auditcore.evidence e
                JOIN auditcore.document_capture_v2_documents d
                  ON d.tenant_id=e.tenant_id
                 AND d.journey_id=e.journey_id
                 AND d.di_document_id=e.di_document_id
                WHERE e.tenant_id=:tenant_id AND e.journey_id=:journey_id
                  AND e.association_status='ACTIVE'
                  AND e.di_document_id IS NOT NULL
                  AND d.stage_code='BOOKING'
                  AND d.capture_status <> 'SUPERSEDED'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalars().all()
    )


def close_booking_ready_with_lazy_v2_sync(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
):
    """Submit Booking without making DI availability a business-process dependency.

    Only current V2-capture documents participate in the pre-submit race check. DI
    clients are acquired lazily, so legacy evidence and an unavailable DI service do
    not turn an otherwise ready Booking into a 500. The durable callback remains the
    source of eventual post-submit synchronization.
    """

    from audit_core import uc03_confidence_review_policy as confidence_policy

    confidence_policy.booking_review._scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    document_ids = _v2_booking_document_ids(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    if document_ids:
        security_provider = None
        di_provider = None
        try:
            security_provider = get_security_oauth_client()
            security_client = next(security_provider)
            di_provider = get_di_client()
            di_client = next(di_provider)
            for document_id in document_ids:
                try:
                    confidence_policy._sync_booking_document(
                        connection,
                        tenant_id=tenant_id,
                        journey_id=journey_id,
                        document_id=document_id,
                        service_id="audit-core",
                        security_client=security_client,
                        di_client=di_client,
                        bump_version=False,
                    )
                except DependencyUnavailableError:
                    logger.warning(
                        "uc03_booking_pre_submit_di_sync_unavailable",
                        extra={
                            "tenant_id": tenant_id,
                            "journey_id": str(journey_id),
                            "document_id": str(document_id),
                        },
                    )
        except RuntimeError as exc:
            logger.warning(
                "uc03_booking_pre_submit_di_clients_unavailable",
                extra={
                    "tenant_id": tenant_id,
                    "journey_id": str(journey_id),
                    "reason": str(exc),
                },
            )
        finally:
            if di_provider is not None:
                di_provider.close()
            if security_provider is not None:
                security_provider.close()

    return booking_capture.close_booking_ready(
        tenant_id=tenant_id,
        journey_id=journey_id,
        request=request,
        response=response,
        idempotency_key=idempotency_key,
        if_match=if_match,
        human_principal=human_principal,
        authorization_client=authorization_client,
        connection=connection,
    )


def install_uc03_post_extraction_materialization() -> None:
    """Run canonical projection after every successful DI->Core document sync."""

    from audit_core import uc03_confidence_review_policy as confidence_policy

    if getattr(confidence_policy, "_post_extraction_materialization_installed", False):
        return
    original = confidence_policy._sync_booking_document

    def wrapped(*args: Any, **kwargs: Any) -> int:
        fact_count = original(*args, **kwargs)
        if fact_count <= 0:
            return fact_count
        connection: Connection = args[0] if args else kwargs["connection"]
        tenant_id = str(kwargs["tenant_id"])
        journey_id = kwargs["journey_id"]
        result = materialize_machine_booking_values(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )
        logger.info(
            "uc03_post_extraction_materialized",
            extra={
                "tenant_id": tenant_id,
                "journey_id": str(journey_id),
                "fact_count": fact_count,
                **result,
            },
        )
        return fact_count

    confidence_policy._sync_booking_document = wrapped  # type: ignore[assignment]
    confidence_policy._replace_route(
        booking_capture.router,
        suffix="/booking/close-ready",
        method="POST",
        endpoint=close_booking_ready_with_lazy_v2_sync,
        response_model=booking_capture.BookingCommandResponse,
    )
    confidence_policy._post_extraction_materialization_installed = True
