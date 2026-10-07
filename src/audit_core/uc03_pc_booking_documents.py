from __future__ import annotations

import os
from functools import lru_cache
from collections.abc import Mapping
from typing import Annotated, Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    get_bearer_token,
    get_connection,
    get_engine,
    get_human_principal,
)
from audit_core.di_client import DiClient, DiClientError
from audit_core.errors import (
    AuditCoreError,
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
)
from audit_core.evidence import (
    _external_context_ref,
    _journey_context as evidence_journey_context,
    _persist_subject_mapping,
    _subject_mapping,
    get_di_client,
    get_security_oauth_client,
)
from audit_core.security import (
    HumanPrincipal,
    SecurityTokenError,
    SecurityTokenValidator,
    ServiceIntegrationPrincipal,
)
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient, SecurityTokenError as OAuthTokenError
from audit_core.uc03_booking_capture import (
    _PROPOSAL_CAPTURE_MAP,
    _SUPPORTED_PROPOSAL_FIELDS,
    _require_active_booking,
    _scope,
)
from audit_core.uc03_booking_commands import (
    _stage_state,
)
from audit_core.uc03_booking_receipt_capture import (
    _RECEIPT_CAPTURE_MAP,
)
from audit_core.uc03_document_assessments import _effective_applicability
from audit_core.uc03_document_registry import is_receipt_document_type

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["uc03-pc-booking-documents"])

_DI_AUDIENCE = "di"
_AUDIT_SERVICE_AUDIENCE = "audit"
# Requirement keys that accept more than one active evidence row (partial
# payments, multiple receipts, multi-page/multi-period bank statements)
# rather than the newest superseding the last. Delivery's payment-receipt
# requirement (0017/0022) needs the same treatment as Booking's now that this
# callback handles both process areas; the bank-statement requirement
# (0070) is repeatable for the same reason on both stages. The scrappage
# certificate requirement (0078) is repeatable too -- a journey can hold both
# the original Certificate of Deposit and a Transfer Certificate of Deposit
# recording its resale as two separate documents.
_REPEATABLE_REQUIREMENT_KEYS = {
    "booking_payment_receipt",
    "payment_receipt",
    "booking_bank_statement",
    "delivery_bank_statement",
    "booking_scrappage_certificate",
    "delivery_scrappage_certificate",
}


class BookingUploadRequirement(BaseModel):
    requirementRef: UUID
    requirementKey: str
    documentTypeKey: str
    requirementLevel: str
    requirementStatus: str
    applicabilityState: Literal["APPLICABLE", "NOT_APPLICABLE", "UNRESOLVED"]
    applicabilityReason: str | None = None
    currentDocumentId: UUID | None = None
    activeDocumentIds: list[UUID] = Field(default_factory=list)
    repeatable: bool = False
    captureEligibleFieldKeys: list[str] = Field(default_factory=list)


class BookingUploadContextResponse(BaseModel):
    journeyId: UUID
    externalContextRef: str
    requirements: list[BookingUploadRequirement]


class BookingDocumentLinkCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirementRef: UUID
    documentId: UUID


class BookingDocumentLinkResponse(BaseModel):
    requirementRef: UUID
    documentId: UUID
    evidenceId: UUID
    status: Literal["ACKNOWLEDGED"] = "ACKNOWLEDGED"


@lru_cache
def _audit_service_token_validator() -> SecurityTokenValidator:
    jwks_url = os.environ.get("SECURITY_JWKS_URL", "").strip()
    issuer = os.environ.get("SECURITY_ISSUER", "").strip()
    if not jwks_url or not issuer:
        raise RuntimeError("Security ServiceIntegration verification is not configured")
    return SecurityTokenValidator(
        jwks_url=jwks_url,
        issuer=issuer,
        audience=_AUDIT_SERVICE_AUDIENCE,
    )


def require_audit_service_principal(
    bearer_token: Annotated[str, Depends(get_bearer_token)],
) -> ServiceIntegrationPrincipal:
    try:
        return _audit_service_token_validator().validate_service_integration(bearer_token)
    except SecurityTokenError as exc:
        logger.warning("audit_service_auth_failed", reason=str(exc))
        raise


def _prepare_dependency_error(exc: Exception) -> AuditCoreError:
    if isinstance(exc, DiClientError) and 400 <= exc.status_code < 500:
        return AuditCoreError(
            error_code="VAC-DI-002",
            status_code=422,
            title="Document intelligence rejected Booking context",
            detail="The Booking document upload context could not be prepared in Document Intelligence.",
        )
    return DependencyUnavailableError(
        detail="Booking document preparation is temporarily unavailable. Please try again."
    )


def _applicability(requirement: dict[str, Any]) -> tuple[str, str | None]:
    assessment_state = requirement.get("assessment_applicability_state")
    if assessment_state in {"APPLICABLE", "NOT_APPLICABLE"}:
        reason = requirement.get("assessment_applicability_reason")
        return assessment_state, reason if isinstance(reason, str) else None
    return _effective_applicability(requirement)


def _capture_eligible_field_keys(document_type_key: str) -> list[str]:
    normalized = document_type_key.strip().lower()
    if is_receipt_document_type(normalized):
        return sorted(_RECEIPT_CAPTURE_MAP)
    supported = _SUPPORTED_PROPOSAL_FIELDS.get(normalized, set())
    return sorted(field for field in supported if field in _PROPOSAL_CAPTURE_MAP)


def _is_repeatable_requirement(requirement_key: str | None) -> bool:
    key = str(requirement_key or "").strip().lower()
    # A checklist row created on demand for a type the catalogue does not
    # list (uc03_p2_worker._ensure_extra_requirement_rows, 2026-10-01) holds
    # every copy: nothing single-slot is known about such a type.
    return key in _REPEATABLE_REQUIREMENT_KEYS or key.startswith("p2_extra_")


@router.post(
    "/v1/tenants/{tenant_id}/journeys/{journey_id}/booking/document-upload-context",
    response_model=BookingUploadContextResponse,
)
def prepare_booking_document_upload_context(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
) -> BookingUploadContextResponse:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    _require_active_booking(state)
    journey = evidence_journey_context(connection, tenant_id, journey_id)
    customer_id: UUID = journey["customer_id"]
    context_ref = _external_context_ref(journey_id=journey_id, customer_id=customer_id)

    subject_id = _subject_mapping(
        connection,
        tenant_id=tenant_id,
        customer_id=customer_id,
    )
    try:
        service_token = security_client.get_service_token(audience=_DI_AUDIENCE)
        if subject_id is None:
            subject = di_client.create_subject(
                token=service_token,
                tenant_id=tenant_id,
                subject_type="OTHER",
                display_name=journey["customer_name"],
            )
            subject_id = UUID(subject.subject_id)
            # Persist immediately so a later DI-context failure cannot create a second
            # DI Subject on retry.
            _persist_subject_mapping(
                engine,
                tenant_id=tenant_id,
                customer_id=customer_id,
                subject_id=subject_id,
            )

        di_client.ensure_audit_storage_context(
            token=service_token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            subject_id=str(subject_id),
            dealer_id=str(journey["dealer_id"]),
            outlet_id=str(journey["outlet_id"]),
            customer_id=str(customer_id),
            project_name=journey["project_name"],
            dealer_name=journey["dealer_name"],
            outlet_name=journey["outlet_name"],
            customer_name=journey["customer_name"],
            idempotency_key=f"uc03-pc-booking-context:{journey_id}",
        )
    except (DiClientError, OAuthTokenError, ValueError) as exc:
        raise _prepare_dependency_error(exc) from exc

    rows = connection.execute(
        text(
            """
            SELECT jdr.journey_document_requirement_id,
                   jdr.requirement_key,
                   jdr.document_type_key,
                   jdr.requirement_level,
                   jdr.requirement_status,
                   jdr.condition_snapshot,
                   jda.applicability_state AS assessment_applicability_state,
                   jda.applicability_reason AS assessment_applicability_reason,
                   e.di_document_id AS current_di_document_id,
                   ARRAY(
                       SELECT e2.di_document_id
                       FROM auditcore.evidence e2
                       WHERE e2.tenant_id=jdr.tenant_id
                         AND e2.journey_id=jdr.journey_id
                         AND e2.journey_document_requirement_id=jdr.journey_document_requirement_id
                         AND e2.association_status='ACTIVE'
                       ORDER BY e2.linked_at_utc, e2.evidence_id
                   ) AS active_di_document_ids,
                   COALESCE(dri.sort_order, 999999) AS sort_order
            FROM auditcore.journey_document_requirements jdr
            LEFT JOIN auditcore.journey_document_assessments jda
              ON jda.tenant_id=jdr.tenant_id
             AND jda.journey_id=jdr.journey_id
             AND jda.stage_code='BOOKING'
             AND jda.requirement_key=jdr.requirement_key
            LEFT JOIN auditcore.evidence e
              ON e.tenant_id=jda.tenant_id
             AND e.evidence_id=jda.evidence_id
             AND e.association_status='ACTIVE'
            LEFT JOIN auditcore.document_requirement_items dri
              ON dri.tenant_id=jdr.tenant_id
             AND dri.document_requirement_item_id=jdr.document_requirement_item_id
            WHERE jdr.tenant_id=:tenant_id
              AND jdr.journey_id=:journey_id
              AND upper(jdr.process_area)='BOOKING'
            ORDER BY COALESCE(dri.sort_order, 999999), jdr.requirement_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()

    requirements: list[BookingUploadRequirement] = []
    for row in rows:
        item = dict(row)
        applicability_state, applicability_reason = _applicability(item)
        if applicability_state != "APPLICABLE":
            continue
        requirements.append(
            BookingUploadRequirement(
                requirementRef=item["journey_document_requirement_id"],
                requirementKey=item["requirement_key"],
                documentTypeKey=item["document_type_key"],
                requirementLevel=item["requirement_level"],
                requirementStatus=item["requirement_status"],
                applicabilityState="APPLICABLE",
                applicabilityReason=applicability_reason,
                currentDocumentId=item["current_di_document_id"],
                activeDocumentIds=list(item["active_di_document_ids"] or []),
                repeatable=_is_repeatable_requirement(item["requirement_key"]),
                captureEligibleFieldKeys=_capture_eligible_field_keys(item["document_type_key"]),
            )
        )

    return BookingUploadContextResponse(
        journeyId=journey_id,
        externalContextRef=context_ref,
        requirements=requirements,
    )


def _discover_requirement_for_callback(
    connection: Connection,
    *,
    service_id: str,
    requirement_ref: UUID,
):
    connection.execute(
        text(
            """
            SELECT set_config('app.internal_service_id', :service_id, true),
                   set_config('app.di_requirement_ref', :requirement_ref, true)
            """
        ),
        {"service_id": service_id, "requirement_ref": str(requirement_ref)},
    )
    row = connection.execute(
        text(
            """
            SELECT tenant_id, journey_id, journey_document_requirement_id,
                   requirement_key, document_type_key, requirement_level,
                   requirement_status, condition_snapshot,
                   upper(process_area) AS process_area
            FROM auditcore.journey_document_requirements
            WHERE journey_document_requirement_id=:requirement_ref
              AND upper(process_area) IN ('BOOKING','DELIVERY')
            """
        ),
        {"requirement_ref": requirement_ref},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-006",
            title="Document requirement not found",
            detail="The supplied requirementRef is not an active Booking or Delivery document requirement.",
        )
    return row


def _require_callback_applicable(requirement) -> tuple[str, str | None]:
    state, reason = _effective_applicability(requirement)
    if state != "APPLICABLE":
        # TEMPORARY DIAGNOSTIC (2026-09-18): three specific documents kept
        # hitting this 409 across dozens of DI retries even after the
        # process_area gate fix. Surface exactly which requirement/condition
        # is still stuck and why -- process_area, requirement_key, condition
        # key/state -- directly in the log line (already wired to include
        # `detail` as of the same fix) instead of guessing. Revert once found.
        snapshot = requirement.get("condition_snapshot") or {}
        detail = (
            "The supplied requirementRef is not currently applicable to this Booking. "
            f"process_area={requirement.get('process_area')} "
            f"requirement_key={requirement.get('requirement_key')} "
            f"requirement_level={requirement.get('requirement_level')} "
            f"requirement_status={requirement.get('requirement_status')} "
            f"applicability_state={state!r} "
            f"condition_key={snapshot.get('conditionKey') if isinstance(snapshot, dict) else None!r} "
            f"condition_snapshot={snapshot!r}"
        )
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Booking document requirement is not applicable",
            detail=detail,
        )
    return state, reason


def _force_conditional_applicable_for_arriving_document(
    connection: Connection, *, tenant_id: str, requirement: Mapping[str, Any],
) -> dict[str, Any] | None:
    """A document-link callback arriving at all, for a CONDITIONAL
    requirement, IS the qualifying fact: DI only reaches here because it
    classified a real document and matched it against this exact
    requirement's own candidate document type (see _candidate_type_keys/
    requirement_refs_by_document_type_key upstream) -- direct, first-party
    evidence the requirement applies, stronger than the indirect
    commercial-line/registration lookup
    resolve_requirement_applicability_if_conditional tries beforehand.

    Hit live: an accessory invoice DI had already extracted was rejected
    forever because accessoriesTaken resolved "No" from commercial_lines,
    even though the invoice's own existence proves accessories were taken.
    A document that exists and was correctly classified is never less
    trustworthy than a separate derived signal saying it shouldn't.

    No-op (returns None) when the requirement isn't CONDITIONAL, or already
    resolved APPLICABLE.
    """
    if str(requirement.get("requirement_level") or "").upper() != "CONDITIONAL":
        return None
    if _effective_applicability(requirement)[0] == "APPLICABLE":
        return None

    from audit_core.uc03_delivery_documents import _apply_resolved_applicability

    snapshot = requirement.get("condition_snapshot") or {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    condition_key = str(snapshot.get("conditionKey") or "").strip().lower()
    return _apply_resolved_applicability(
        connection,
        tenant_id=tenant_id,
        requirement_id=requirement["journey_document_requirement_id"],
        condition_key=condition_key,
        snapshot=snapshot,
        resolved=True,
    )


# No @router decorator: uc03_confidence_review_policy.py's
# acknowledge_booking_document_link_with_auto_sync is decorated directly on
# this same router for this same path (POST /v1/internal/di/booking-
# document-links) -- it calls this function at the start of its own body
# (as pc_documents.acknowledge_booking_document_link), so this stays plain
# library code rather than a second, competing registration.
def acknowledge_booking_document_link(
    payload: BookingDocumentLinkCommand,
    service_principal: Annotated[
        ServiceIntegrationPrincipal,
        Depends(require_audit_service_principal),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingDocumentLinkResponse:
    discovered = _discover_requirement_for_callback(
        connection,
        service_id=service_principal.subject,
        requirement_ref=payload.requirementRef,
    )
    tenant_id = str(discovered["tenant_id"])
    journey_id: UUID = discovered["journey_id"]
    set_tenant_context(connection, tenant_id)

    # Stage is data, not routing: this callback is DI telling Audit Core "this
    # document is linked to this requirement" -- DI has no notion of Booking vs
    # Delivery, and neither should this handler. The requirement row itself
    # already says which process area it belongs to; everything downstream
    # (evidence.process_area, the assessment row, which rules/triggers fire)
    # reads that value instead of assuming one.
    requirement = connection.execute(
        text(
            """
            SELECT jdr.journey_document_requirement_id, jdr.requirement_key,
                   jdr.document_type_key, jdr.requirement_level,
                   jdr.requirement_status, jdr.condition_snapshot,
                   upper(jdr.process_area) AS process_area,
                   j.customer_id, j.document_requirement_profile_version_id
            FROM auditcore.journey_document_requirements jdr
            JOIN auditcore.journeys j
              ON j.tenant_id=jdr.tenant_id AND j.journey_id=jdr.journey_id
            WHERE jdr.tenant_id=:tenant_id
              AND jdr.journey_id=:journey_id
              AND jdr.journey_document_requirement_id=:requirement_ref
              AND upper(jdr.process_area) IN ('BOOKING','DELIVERY')
            FOR UPDATE OF jdr
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "requirement_ref": payload.requirementRef,
        },
    ).mappings().one()

    # A conditional requirement's applicability only ever gets recomputed
    # today when a human happens to load the Booking/Delivery documents list
    # first -- if DI's callback for that exact document arrives before
    # anyone has, _require_callback_applicable below sees it stuck UNRESOLVED
    # and rejects with 409 forever, even once the authoritative fact has been
    # there the whole time. Resolve it here too, scoped to only the one row
    # already locked above (see resolve_requirement_applicability_if_
    # conditional's own docstring for why NOT the journey-wide recompute:
    # that one caused a live lock-contention incident when called from every
    # callback). Despite living in uc03_delivery_documents.py, this resolver
    # is process-area-agnostic -- it already explicitly documents resolving
    # Booking's own gst_certificate/corporate_id requirements via the
    # "corporatecustomer" condition key -- it was only ever gated to
    # DELIVERY here, so a conditional BOOKING requirement hit this exact
    # same 409-forever failure the DELIVERY side was already fixed for.
    from audit_core.uc03_delivery_documents import (
        resolve_requirement_applicability_if_conditional,
    )

    updated = resolve_requirement_applicability_if_conditional(
        connection, tenant_id=tenant_id, journey_id=journey_id, requirement=requirement,
    )
    if updated is not None:
        requirement = {**requirement, **updated}

    forced = _force_conditional_applicable_for_arriving_document(
        connection, tenant_id=tenant_id, requirement=requirement,
    )
    if forced is not None:
        requirement = {**requirement, **forced}

    applicability_state, applicability_reason = _require_callback_applicable(requirement)
    customer_id: UUID = requirement["customer_id"]
    process_area: str = requirement["process_area"]
    repeatable = _is_repeatable_requirement(requirement["requirement_key"])

    subject_id = _subject_mapping(
        connection,
        tenant_id=tenant_id,
        customer_id=customer_id,
    )
    if subject_id is None:
        # TEMPORARY DIAGNOSTIC (2026-09-18): see the matching note on
        # _require_callback_applicable above. Revert once found.
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="DI Subject mapping is not ready",
            detail=(
                "Prepare the Booking document upload context before linking a DI document. "
                f"tenant_id={tenant_id} customer_id={customer_id} journey_id={journey_id}"
            ),
        )

    existing_for_document = connection.execute(
        text(
            """
            SELECT evidence_id, journey_id, journey_document_requirement_id,
                   association_status
            FROM auditcore.evidence
            WHERE tenant_id=:tenant_id AND di_document_id=:document_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "document_id": payload.documentId},
    ).mappings().one_or_none()
    if existing_for_document is not None:
        if (
            existing_for_document["journey_id"] != journey_id
            or existing_for_document["journey_document_requirement_id"] != payload.requirementRef
        ):
            raise ConflictError(
                error_code="VAC-CONFLICT-009",
                title="DI document linkage conflict",
                detail="The DI document is already linked to a different Booking requirement.",
            )
        evidence_id: UUID = existing_for_document["evidence_id"]
        if existing_for_document["association_status"] != "ACTIVE":
            if not repeatable:
                # A delayed duplicate callback for an older replaced single-value
                # document must ACK without making it current again.
                return BookingDocumentLinkResponse(
                    requirementRef=payload.requirementRef,
                    documentId=payload.documentId,
                    evidenceId=evidence_id,
                )
            connection.execute(
                text(
                    """
                    UPDATE auditcore.evidence
                    SET association_status='ACTIVE', void_reason=NULL,
                        voided_by_actor_id=NULL, voided_at_utc=NULL,
                        supersedes_evidence_id=NULL
                    WHERE tenant_id=:tenant_id AND evidence_id=:evidence_id
                    """
                ),
                {"tenant_id": tenant_id, "evidence_id": evidence_id},
            )
    else:
        prior_evidence_id = None
        if not repeatable:
            prior_evidence_id = connection.execute(
                text(
                    """
                    SELECT evidence_id
                    FROM auditcore.evidence
                    WHERE tenant_id=:tenant_id
                      AND journey_id=:journey_id
                      AND journey_document_requirement_id=:requirement_ref
                      AND association_status='ACTIVE'
                    ORDER BY linked_at_utc DESC, evidence_id DESC
                    LIMIT 1
                    FOR UPDATE
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "journey_id": journey_id,
                    "requirement_ref": payload.requirementRef,
                },
            ).scalar_one_or_none()
            if prior_evidence_id is not None:
                # Decision 2026-10-01 (replaces the September rule that voided
                # the newer copy and asked the PC to delete the first): a new
                # copy of a single-slot document supersedes the earlier one,
                # the way "Replace with a new scan" does. The earlier copy
                # stays on file as SUPERSEDED (restorable from its card), its
                # facts stop feeding the journey once the stage is
                # re-materialised, and the new document is linked ACTIVE
                # with supersedes_evidence_id pointing back.
                prior_document_id = connection.execute(
                    text(
                        """
                        UPDATE auditcore.evidence
                        SET association_status='SUPERSEDED',
                            void_reason='REPLACED_BY_NEWER_UPLOAD',
                            voided_by_actor_id=:service_id,
                            voided_at_utc=now()
                        WHERE tenant_id=:tenant_id AND evidence_id=:evidence_id
                          AND association_status='ACTIVE'
                        RETURNING di_document_id
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "evidence_id": prior_evidence_id,
                        "service_id": service_principal.subject,
                    },
                ).scalar_one_or_none()
                if prior_document_id is not None:
                    connection.execute(
                        text(
                            """
                            UPDATE auditcore.document_capture_v2_documents
                            SET capture_status='SUPERSEDED', updated_at_utc=now()
                            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                              AND di_document_id=:document_id
                            """
                        ),
                        {
                            "tenant_id": tenant_id,
                            "journey_id": journey_id,
                            "document_id": prior_document_id,
                        },
                    )
                logger.info(
                    "uc03_document_superseded_by_newer_upload",
                    tenant_id=tenant_id,
                    journey_id=str(journey_id),
                    requirement_key=requirement["requirement_key"],
                    document_type_key=requirement["document_type_key"],
                    prior_evidence_id=str(prior_evidence_id),
                    prior_document_id=str(prior_document_id) if prior_document_id else None,
                    new_document_id=str(payload.documentId),
                )

        evidence_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.evidence (
                    tenant_id, journey_id, customer_id,
                    journey_document_requirement_id,
                    di_subject_id, di_document_id,
                    document_type_key, evidence_purpose, process_area,
                    association_status, supersedes_evidence_id,
                    linked_by_actor_id, correlation_id
                ) VALUES (
                    :tenant_id, :journey_id, :customer_id,
                    :requirement_ref,
                    :subject_id, :document_id,
                    :document_type_key, :evidence_purpose, :process_area,
                    'ACTIVE', :supersedes_evidence_id,
                    :service_id, NULL
                )
                RETURNING evidence_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "customer_id": customer_id,
                "requirement_ref": payload.requirementRef,
                "subject_id": subject_id,
                "document_id": payload.documentId,
                "document_type_key": requirement["document_type_key"],
                "evidence_purpose": f"{process_area}_DOCUMENT",
                "process_area": process_area,
                "supersedes_evidence_id": prior_evidence_id,
                "service_id": service_principal.subject,
            },
        ).scalar_one()

    # The assessment pointer remains the most recently acknowledged evidence for
    # generic current-document views. Repeatable requirements retain all evidence
    # rows ACTIVE and are validated by documentId during extraction decisions.
    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_document_assessments (
                tenant_id, journey_id, stage_code,
                journey_document_requirement_id, requirement_key,
                document_requirement_profile_version_id,
                applicability_state, applicability_reason,
                evidence_id
            ) VALUES (
                :tenant_id, :journey_id, :process_area,
                :requirement_ref, :requirement_key,
                :profile_version_id,
                :applicability_state, :applicability_reason,
                :evidence_id
            )
            ON CONFLICT (tenant_id, journey_id, stage_code, requirement_key)
            DO UPDATE SET
                evidence_id=EXCLUDED.evidence_id,
                version_no=auditcore.journey_document_assessments.version_no+1,
                updated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "process_area": process_area,
            "requirement_ref": payload.requirementRef,
            "requirement_key": requirement["requirement_key"],
            "profile_version_id": requirement["document_requirement_profile_version_id"],
            "applicability_state": applicability_state,
            "applicability_reason": applicability_reason,
            "evidence_id": evidence_id,
        },
    )

    return BookingDocumentLinkResponse(
        requirementRef=payload.requirementRef,
        documentId=payload.documentId,
        evidenceId=evidence_id,
    )


