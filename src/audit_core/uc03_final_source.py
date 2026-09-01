from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import ConflictError
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_capture import _scope
from audit_core.uc03_booking_commands import _aggregate_lock, _parse_if_match
from audit_core.uc03_final_source_persistence import (
    record_post_delivery_reviewed_resolution,
)
from audit_core.uc03_final_source_policy import (
    PROVEN_REVIEWED_SOURCE_POLICIES,
    UNRESOLVED_TECHNICAL_POLICIES,
    ReviewedSourcePolicy,
)

router = APIRouter(
    prefix="/v2/tenants/{tenant_id}/journeys/{journey_id}",
    tags=["uc03-final-source"],
)


class FinalSourceTechnicalGap(BaseModel):
    reportField: str
    businessSourceLabel: str
    reason: str


class FinalSourceResolution(BaseModel):
    attributeKey: str
    resolvedValue: Any
    sourceReviewedFieldId: UUID
    sourceDocumentId: UUID
    sourceDocumentTypeKey: str | None = None
    sourceFieldKey: str
    sourceFactVersion: int
    resolutionRule: str
    mappingVersion: str
    resolvedAtUtc: datetime


class FinalSourceStatusResponse(BaseModel):
    journeyId: UUID
    status: Literal["NOT_READY", "MAPPING_BLOCKED", "READY", "CONFIRMED"]
    bookingReviewStatus: str
    deliveryReviewStatus: str
    aggregateVersion: int
    resolutionCount: int
    technicalGaps: list[FinalSourceTechnicalGap] = Field(default_factory=list)
    resolutions: list[FinalSourceResolution] = Field(default_factory=list)


class FinalSourceConfirmResponse(BaseModel):
    journeyId: UUID
    status: Literal["CONFIRMED"] = "CONFIRMED"
    aggregateVersion: int
    resolvedAttributeCount: int
    missingAttributes: list[str] = Field(default_factory=list)


def _technical_gaps() -> list[FinalSourceTechnicalGap]:
    return [
        FinalSourceTechnicalGap(
            reportField=gap.report_field,
            businessSourceLabel=gap.business_source_label,
            reason=gap.reason,
        )
        for gap in UNRESOLVED_TECHNICAL_POLICIES
    ]


def _review_stage_states(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    for_update: bool,
) -> dict[str, dict[str, Any]]:
    suffix = " FOR UPDATE" if for_update else ""
    rows = connection.execute(
        text(
            """
            SELECT stage_code, capture_completed_at_utc, pc_verification_status,
                   version_no
            FROM auditcore.journey_stage_states
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code IN ('BOOKING','DELIVERY')
            ORDER BY stage_code
            """
            + suffix
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return {str(row["stage_code"]): dict(row) for row in rows}


def _review_status(row: dict[str, Any] | None) -> str:
    if row is None or row.get("capture_completed_at_utc") is None:
        return "NOT_SUBMITTED"
    return str(row.get("pc_verification_status") or "PENDING")


def _require_verified_reviews(states: dict[str, dict[str, Any]]) -> tuple[int, int]:
    booking = states.get("BOOKING")
    delivery = states.get("DELIVERY")
    booking_status = _review_status(booking)
    delivery_status = _review_status(delivery)
    if booking_status != "VERIFIED":
        raise ConflictError(
            error_code="VAC-CONFLICT-010",
            title="Booking Review is not verified",
            detail="Complete Booking Review before confirming the post-Delivery final source.",
        )
    if delivery_status != "VERIFIED":
        raise ConflictError(
            error_code="VAC-CONFLICT-010",
            title="Delivery Review is not verified",
            detail="Complete Delivery Review before confirming the post-Delivery final source.",
        )
    assert booking is not None and delivery is not None
    return int(booking["version_no"]), int(delivery["version_no"])


def _mapping_blocked_error() -> ConflictError:
    return ConflictError(
        error_code="VAC-CONFLICT-014",
        title="Final source technical mapping is incomplete",
        detail=(
            f"{len(UNRESOLVED_TECHNICAL_POLICIES)} approved final-report source "
            "mapping(s) still require authoritative technical document/field keys. "
            "No final source was committed."
        ),
    )


def _candidate_conditions(policy: ReviewedSourcePolicy) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for index, (document_type_key, field_key) in enumerate(policy.technical_pairs):
        clauses.append(
            f"(source_document_type_key=:document_type_{index} "
            f"AND field_key=:field_key_{index})"
        )
        params[f"document_type_{index}"] = document_type_key
        params[f"field_key_{index}"] = field_key
    if not clauses:
        raise RuntimeError(f"Final-source policy {policy.attribute_key} has no technical pairs")
    return " OR ".join(clauses), params


def _reviewed_candidates(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    policy: ReviewedSourcePolicy,
) -> list[dict[str, Any]]:
    conditions, params = _candidate_conditions(policy)
    params.update({"tenant_id": tenant_id, "journey_id": journey_id})
    rows = connection.execute(
        text(
            f"""
            SELECT DISTINCT ON (stage_code, di_document_id, field_key)
                   extracted_field_id, stage_code, di_document_id,
                   source_document_type_key, source_canonical_field_id,
                   field_key, source_fact_version, effective_value
            FROM auditcore.journey_document_extracted_fields
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND stage_code IN ('BOOKING','DELIVERY')
              AND effective_value IS NOT NULL
              AND ({conditions})
            ORDER BY stage_code, di_document_id, field_key,
                     source_fact_version DESC, extracted_field_id DESC
            """
        ),
        params,
    ).mappings().all()
    return [dict(row) for row in rows]


def _normalized_value(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _choose_candidate(
    policy: ReviewedSourcePolicy,
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not candidates:
        return None
    distinct_values = {_normalized_value(row["effective_value"]) for row in candidates}
    if len(distinct_values) > 1:
        raise ConflictError(
            error_code="VAC-CONFLICT-015",
            title="Final source values disagree",
            detail=(
                f"Approved final source '{policy.business_source_label}' has "
                f"conflicting reviewed values for '{policy.report_field}'. "
                "No confidence, stage or recency override is configured."
            ),
        )

    # When current legitimate sources agree, deterministic provenance selection is
    # safe because it does not change the business value. This is deliberately not
    # a precedence rule for disagreement.
    return min(
        candidates,
        key=lambda row: (
            str(row["stage_code"]),
            str(row["source_document_type_key"] or ""),
            str(row["di_document_id"]),
            -int(row["source_fact_version"]),
            str(row["extracted_field_id"]),
        ),
    )


def _preflight_final_sources(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> tuple[list[tuple[ReviewedSourcePolicy, dict[str, Any]]], list[str]]:
    selected: list[tuple[ReviewedSourcePolicy, dict[str, Any]]] = []
    missing: list[str] = []
    for policy in PROVEN_REVIEWED_SOURCE_POLICIES:
        candidate = _choose_candidate(
            policy,
            _reviewed_candidates(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                policy=policy,
            ),
        )
        if candidate is None:
            missing.append(policy.attribute_key)
            continue
        selected.append((policy, candidate))
    return selected, sorted(missing)


def _existing_finalization(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> bool:
    return (
        connection.execute(
            text(
                """
                SELECT 1
                FROM auditcore.journey_stage_states
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='POST_DELIVERY'
                LIMIT 1
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one_or_none()
        is not None
    )


def _create_post_delivery_stage(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> int:
    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_stage_states (
                tenant_id, journey_id, stage_code, audit_state, audit_status,
                first_started_at_utc, latest_activity_at_utc, version_no
            ) VALUES (
                :tenant_id, :journey_id, 'POST_DELIVERY', 'IN_PROGRESS',
                'NOT_EVALUATED', now(), now(), 1
            )
            RETURNING version_no
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one()
    return int(row)


def _append_final_source_event(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    actor_id: str,
    actor_role: str | None,
    idempotency_key: str,
    correlation_id: str | None,
    aggregate_version: int,
    resolved_attribute_keys: list[str],
    missing_attribute_keys: list[str],
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_workflow_events (
                tenant_id, journey_id, stage_code, event_type, source_kind,
                actor_id, actor_role_snapshot, idempotency_key, correlation_id,
                safe_payload, occurred_at_utc, aggregate_version
            ) VALUES (
                :tenant_id, :journey_id, 'POST_DELIVERY',
                'FINAL_SOURCE_CONFIRMED', 'HUMAN', :actor_id, :actor_role,
                :idempotency_key, :correlation_id, CAST(:safe_payload AS jsonb),
                now(), :aggregate_version
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "idempotency_key": idempotency_key,
            "correlation_id": correlation_id,
            "safe_payload": json.dumps(
                {
                    "resolvedAttributeKeys": sorted(resolved_attribute_keys),
                    "missingAttributeKeys": sorted(missing_attribute_keys),
                    "rawValuesIncluded": False,
                }
            ),
            "aggregate_version": aggregate_version,
        },
    )


def _resolution_rows(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT attribute_key, resolved_value_snapshot,
                   source_reviewed_field_id, source_di_document_id,
                   source_document_type_key, source_field_key,
                   source_fact_version, resolution_rule, mapping_version,
                   resolved_at_utc
            FROM auditcore.journey_attribute_resolutions
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='POST_DELIVERY'
            ORDER BY attribute_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _resolution_models(rows: list[dict[str, Any]]) -> list[FinalSourceResolution]:
    return [
        FinalSourceResolution(
            attributeKey=str(row["attribute_key"]),
            resolvedValue=row["resolved_value_snapshot"],
            sourceReviewedFieldId=UUID(str(row["source_reviewed_field_id"])),
            sourceDocumentId=UUID(str(row["source_di_document_id"])),
            sourceDocumentTypeKey=(
                str(row["source_document_type_key"])
                if row.get("source_document_type_key")
                else None
            ),
            sourceFieldKey=str(row["source_field_key"]),
            sourceFactVersion=int(row["source_fact_version"]),
            resolutionRule=str(row["resolution_rule"]),
            mappingVersion=str(row["mapping_version"]),
            resolvedAtUtc=row["resolved_at_utc"],
        )
        for row in rows
    ]


@router.get("/audit/final-source", response_model=FinalSourceStatusResponse)
def get_final_source_status(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> FinalSourceStatusResponse:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    states = _review_stage_states(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        for_update=False,
    )
    booking_status = _review_status(states.get("BOOKING"))
    delivery_status = _review_status(states.get("DELIVERY"))
    rows = _resolution_rows(connection, tenant_id=tenant_id, journey_id=journey_id)
    post_state = connection.execute(
        text(
            """
            SELECT version_no
            FROM auditcore.journey_stage_states
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='POST_DELIVERY'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()

    if post_state is not None:
        status: Literal["NOT_READY", "MAPPING_BLOCKED", "READY", "CONFIRMED"] = (
            "CONFIRMED"
        )
        version = int(post_state["version_no"])
    elif booking_status != "VERIFIED" or delivery_status != "VERIFIED":
        status = "NOT_READY"
        version = int((states.get("DELIVERY") or {}).get("version_no") or 0)
    elif UNRESOLVED_TECHNICAL_POLICIES:
        status = "MAPPING_BLOCKED"
        version = int((states.get("DELIVERY") or {}).get("version_no") or 0)
    else:
        status = "READY"
        version = int((states.get("DELIVERY") or {}).get("version_no") or 0)

    return FinalSourceStatusResponse(
        journeyId=journey_id,
        status=status,
        bookingReviewStatus=booking_status,
        deliveryReviewStatus=delivery_status,
        aggregateVersion=version,
        resolutionCount=len(rows),
        technicalGaps=_technical_gaps(),
        resolutions=_resolution_models(rows),
    )


@router.post("/audit/final-source/confirm", response_model=FinalSourceConfirmResponse)
def confirm_final_source(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> FinalSourceConfirmResponse:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_delivery_version = _parse_if_match(if_match)
    correlation_id = get_correlation_id(request)

    # Technical source mappings are static/versioned configuration. Fail before the
    # idempotency command/transaction mutates any final-source state.
    if UNRESOLVED_TECHNICAL_POLICIES:
        raise _mapping_blocked_error()

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        states = _review_stage_states(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            for_update=True,
        )
        _, delivery_version = _require_verified_reviews(states)
        if delivery_version != expected_delivery_version:
            raise ConflictError(
                error_code="VAC-CONFLICT-005",
                title="Delivery version conflict",
                detail=(
                    "Delivery changed since final-source readiness was loaded. "
                    "Refresh and try again."
                ),
            )
        if _existing_finalization(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        ):
            raise ConflictError(
                error_code="VAC-CONFLICT-010",
                title="Final source is already confirmed",
                detail="The post-Delivery final source has already been committed.",
            )

        # Resolve every policy before inserting the first final row so source
        # disagreement can never leave a partial winner set in this transaction.
        selected, missing = _preflight_final_sources(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )

        resolved_keys: list[str] = []
        for policy, candidate in selected:
            record_post_delivery_reviewed_resolution(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                attribute_key=policy.attribute_key,
                source_reviewed_field_id=UUID(str(candidate["extracted_field_id"])),
                actor_id=human_principal.subject,
                resolution_rule=policy.resolution_rule,
            )
            resolved_keys.append(policy.attribute_key)

        aggregate_version = _create_post_delivery_stage(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )
        _append_final_source_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            actor_id=human_principal.subject,
            actor_role=context.get("operating_role"),
            idempotency_key=f"{idempotency_key}:final-source-confirmed",
            correlation_id=correlation_id,
            aggregate_version=aggregate_version,
            resolved_attribute_keys=resolved_keys,
            missing_attribute_keys=missing,
        )
        return {
            "journeyId": str(journey_id),
            "status": "CONFIRMED",
            "aggregateVersion": aggregate_version,
            "resolvedAttributeCount": len(resolved_keys),
            "missingAttributes": missing,
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.final-source.confirm:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={"expectedDeliveryVersion": expected_delivery_version},
        execute=execute,
    )
    response.headers["ETag"] = f'"{body["aggregateVersion"]}"'
    return FinalSourceConfirmResponse.model_validate(body)
