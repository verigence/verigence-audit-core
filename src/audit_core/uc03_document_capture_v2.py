from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, Engine, text

from audit_core.dependencies import get_connection, get_engine, get_human_principal
from audit_core.di_capture_v2_client import DiCaptureV2Client, DiCaptureV2Error
from audit_core.di_client import DiClient
from audit_core.errors import ConflictError, DependencyUnavailableError, NotFoundError
from audit_core.evidence import (
    _external_context_ref,
    _journey_context,
    _persist_subject_mapping,
    _subject_mapping,
    get_di_client,
    get_security_oauth_client,
)
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient
from audit_core.uc03_booking_capture import _require_active_booking, _scope
from audit_core.uc03_booking_commands import (
    _aggregate_lock,
    _append_workflow_event,
    _parse_if_match,
    _stage_state,
)

router = APIRouter(prefix="/v2/tenants/{tenant_id}/journeys/{journey_id}", tags=["uc03-document-capture-v2"])
_DI_AUDIENCE = "di"


class CaptureV2Declaration(BaseModel):
    conditionKey: str
    applicable: bool
    documentAvailable: bool | None = None
    source: Literal["PC", "DOCUMENT"] = "PC"


class CaptureV2Document(BaseModel):
    documentId: UUID
    clientUploadId: str
    state: str
    classifiedDocumentTypeKey: str | None = None
    originalFilename: str
    contentUrl: str | None = None
    processingStatus: str | None = None


class CaptureV2Requirement(BaseModel):
    requirementKey: str
    label: str
    documentTypeKey: str
    requirementLevel: str
    conditionKey: str | None = None
    applicabilityState: Literal["APPLICABLE", "NOT_APPLICABLE", "UNRESOLVED"]
    state: str
    document: CaptureV2Document | None = None
    canView: bool = False
    canDelete: bool = False
    needsDecision: bool = False
    blocksContinue: bool = False


class BookingCaptureV2Response(BaseModel):
    journeyId: UUID
    externalContextRef: str
    phase: Literal["BOOKING"] = "BOOKING"
    requirements: list[CaptureV2Requirement]
    uploads: list[CaptureV2Document]
    declarations: list[CaptureV2Declaration]
    canContinue: bool


class UploadIntentFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clientUploadId: str = Field(min_length=1, max_length=160)
    filename: str = Field(min_length=1, max_length=500)
    contentType: str | None = Field(default=None, max_length=160)


class UploadIntentCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[UploadIntentFile] = Field(min_length=1, max_length=20)


class UploadIntentResult(BaseModel):
    clientUploadId: str
    documentId: UUID
    uploadUrl: str
    uploadHeaders: dict[str, str]
    expiresAtUtc: str


class UploadIntentResponse(BaseModel):
    externalContextRef: str
    uploads: list[UploadIntentResult]


class ConditionalDeclarationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    applicable: bool
    documentAvailable: bool | None = None


class FinalizeResponse(BaseModel):
    documentId: UUID
    state: str


class BookingCaptureV2CompletionResponse(BaseModel):
    journeyId: UUID
    phase: Literal["BOOKING"] = "BOOKING"
    status: Literal["COMPLETED"] = "COMPLETED"
    aggregateVersion: int


def get_di_capture_v2_client() -> Iterator[DiCaptureV2Client]:
    base_url = os.environ.get("DI_BASE_URL", "").strip()
    if not base_url:
        raise RuntimeError("DI integration is not configured")
    with DiCaptureV2Client(base_url=base_url) as client:
        yield client


def _human_actor_id(human_principal: HumanPrincipal) -> str:
    return human_principal.subject


def _capture_phase_state(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    for_update: bool = False,
):
    suffix = " FOR UPDATE" if for_update else ""
    row = connection.execute(
        text(
            """
            SELECT business_status, capture_completed_at_utc, version_no
            FROM auditcore.journey_stage_states
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING'
            """ + suffix
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Booking not found",
            detail="Booking stage not found for the requested Project.",
        )
    return row


def _require_capture_phase_open(connection: Connection, *, tenant_id: str, journey_id: UUID) -> None:
    state = _capture_phase_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    if state["capture_completed_at_utc"] is not None:
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Booking document capture is complete",
            detail="Booking V2 document capture is locked after Booking submission.",
        )


def _authorize_booking(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    human_principal: HumanPrincipal,
    authorization_client: SecurityAuthorizationClient,
) -> dict[str, Any]:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    _require_active_booking(state)
    return dict(state)


def _base_requirements(connection: Connection, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT jdr.requirement_key, jdr.document_type_key,
                   jdr.requirement_level, jdr.requirement_status,
                   COALESCE(p.display_label, jdr.requirement_key) AS display_label,
                   COALESCE(p.condition_key, jdr.condition_snapshot->>'conditionKey') AS condition_key,
                   COALESCE(p.sort_order, dri.sort_order, 999999) AS sort_order
            FROM auditcore.journey_document_requirements jdr
            LEFT JOIN auditcore.document_requirement_items dri
              ON dri.tenant_id=jdr.tenant_id
             AND dri.document_requirement_item_id=jdr.document_requirement_item_id
            LEFT JOIN auditcore.document_capture_v2_requirement_policy p
              ON p.requirement_key=jdr.requirement_key
             AND p.process_area='BOOKING'
             AND p.is_active=true
            WHERE jdr.tenant_id=:tenant_id
              AND jdr.journey_id=:journey_id
              AND upper(jdr.process_area)='BOOKING'
            ORDER BY COALESCE(p.sort_order, dri.sort_order, 999999), jdr.requirement_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    requirements = [dict(row) for row in rows]

    extensions = connection.execute(
        text(
            """
            SELECT requirement_key,
                   extension_document_type_key AS document_type_key,
                   extension_requirement_level AS requirement_level,
                   'PENDING' AS requirement_status,
                   display_label, condition_key, sort_order
            FROM auditcore.document_capture_v2_requirement_policy
            WHERE process_area='BOOKING' AND is_active=true AND is_extension=true
            ORDER BY sort_order, requirement_key
            """
        )
    ).mappings().all()
    existing_keys = {row["requirement_key"] for row in requirements}
    requirements.extend(dict(row) for row in extensions if row["requirement_key"] not in existing_keys)
    requirements.sort(key=lambda row: (int(row.get("sort_order") or 999999), row["requirement_key"]))
    return requirements


def _declarations(connection: Connection, tenant_id: str, journey_id: UUID) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT condition_key, applicable, document_available
            FROM auditcore.document_capture_v2_declarations
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND stage_code='BOOKING'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return {str(row["condition_key"]): dict(row) for row in rows}


def _linked_documents(connection: Connection, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT di_document_id, client_upload_id, requirement_key,
                   classified_document_type_key, capture_status,
                   original_filename, content_type
            FROM auditcore.document_capture_v2_documents
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING' AND capture_status <> 'SUPERSEDED'
            ORDER BY created_at_utc, di_document_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _ensure_di_context(
    *,
    connection: Connection,
    engine: Engine,
    tenant_id: str,
    journey_id: UUID,
    security_client: SecurityOAuthClient,
    di_client: DiClient,
) -> tuple[str, str]:
    journey = _journey_context(connection, tenant_id, journey_id)
    customer_id: UUID = journey["customer_id"]
    context_ref = _external_context_ref(journey_id=journey_id, customer_id=customer_id)
    token = security_client.get_service_token(audience=_DI_AUDIENCE)
    subject_id = _subject_mapping(connection, tenant_id=tenant_id, customer_id=customer_id)
    if subject_id is None:
        subject = di_client.create_subject(
            token=token,
            tenant_id=tenant_id,
            subject_type="OTHER",
            display_name=journey["customer_name"],
        )
        subject_id = UUID(subject.subject_id)
        _persist_subject_mapping(
            engine,
            tenant_id=tenant_id,
            customer_id=customer_id,
            subject_id=subject_id,
        )
    di_client.ensure_audit_storage_context(
        token=token,
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
        idempotency_key=f"uc03-document-capture-v2-context:{journey_id}",
    )
    return context_ref, token


def _candidate_type_keys(requirements: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(str(row["document_type_key"]) for row in requirements if row.get("document_type_key")))


def _reconcile_documents(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    requirements: list[dict[str, Any]],
    di_documents: list[dict[str, Any]],
) -> None:
    type_to_requirement: dict[str, str] = {}
    for requirement in requirements:
        type_to_requirement.setdefault(str(requirement["document_type_key"]), str(requirement["requirement_key"]))

    for item in di_documents:
        document_id = UUID(str(item["documentId"]))
        classified_type = item.get("classifiedDocumentTypeKey")
        requirement_key = type_to_requirement.get(str(classified_type)) if classified_type else None
        connection.execute(
            text(
                """
                UPDATE auditcore.document_capture_v2_documents
                SET capture_status=:capture_status,
                    classified_document_type_key=:classified_type,
                    requirement_key=:requirement_key,
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND di_document_id=:document_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "document_id": document_id,
                "capture_status": str(item["state"]),
                "classified_type": classified_type,
                "requirement_key": requirement_key,
            },
        )


def _build_capture_response(
    *,
    journey_id: UUID,
    context_ref: str,
    requirements: list[dict[str, Any]],
    declaration_rows: dict[str, dict[str, Any]],
    audit_documents: list[dict[str, Any]],
    di_documents: list[dict[str, Any]],
) -> BookingCaptureV2Response:
    di_by_id = {str(item["documentId"]): item for item in di_documents}
    active_by_requirement: dict[str, dict[str, Any]] = {}
    uploads: list[CaptureV2Document] = []

    for link in audit_documents:
        di = di_by_id.get(str(link["di_document_id"]))
        if di is None:
            continue
        public = CaptureV2Document(
            documentId=UUID(str(di["documentId"])),
            clientUploadId=str(di["clientUploadId"]),
            state=str(di["state"]),
            classifiedDocumentTypeKey=di.get("classifiedDocumentTypeKey"),
            originalFilename=str(di["originalFilename"]),
            contentUrl=di.get("contentUrl"),
            processingStatus=di.get("processingStatus"),
        )
        uploads.append(public)
        if link.get("requirement_key") and di["state"] == "CLASSIFIED":
            active_by_requirement.setdefault(str(link["requirement_key"]), di)

    inferred_conditions: set[str] = set()
    for requirement in requirements:
        if str(requirement["requirement_key"]) in active_by_requirement and requirement.get("condition_key"):
            inferred_conditions.add(str(requirement["condition_key"]))

    response_declarations: list[CaptureV2Declaration] = []
    all_conditions = {str(r["condition_key"]) for r in requirements if r.get("condition_key")}
    for condition_key in sorted(all_conditions):
        if condition_key in inferred_conditions:
            response_declarations.append(
                CaptureV2Declaration(
                    conditionKey=condition_key,
                    applicable=True,
                    documentAvailable=True,
                    source="DOCUMENT",
                )
            )
        elif condition_key in declaration_rows:
            row = declaration_rows[condition_key]
            response_declarations.append(
                CaptureV2Declaration(
                    conditionKey=condition_key,
                    applicable=bool(row["applicable"]),
                    documentAvailable=row["document_available"],
                    source="PC",
                )
            )

    requirement_results: list[CaptureV2Requirement] = []
    can_continue = True
    for requirement in requirements:
        key = str(requirement["requirement_key"])
        level = str(requirement["requirement_level"])
        condition_key = str(requirement["condition_key"]) if requirement.get("condition_key") else None
        di = active_by_requirement.get(key)
        declaration = declaration_rows.get(condition_key) if condition_key else None

        if condition_key is None or di is not None:
            applicability = "APPLICABLE"
            needs_decision = False
        elif declaration is None:
            applicability = "UNRESOLVED"
            needs_decision = True
        elif bool(declaration["applicable"]):
            applicability = "APPLICABLE"
            needs_decision = False
        else:
            applicability = "NOT_APPLICABLE"
            needs_decision = False

        public_doc = None
        if di is not None:
            public_doc = CaptureV2Document(
                documentId=UUID(str(di["documentId"])),
                clientUploadId=str(di["clientUploadId"]),
                state=str(di["state"]),
                classifiedDocumentTypeKey=di.get("classifiedDocumentTypeKey"),
                originalFilename=str(di["originalFilename"]),
                contentUrl=di.get("contentUrl"),
                processingStatus=di.get("processingStatus"),
            )

        if applicability == "NOT_APPLICABLE":
            state = "NOT_APPLICABLE"
        elif di is not None:
            state = "UPLOADED"
        elif condition_key and declaration and bool(declaration["applicable"]) and declaration["document_available"] is False:
            state = "ACKNOWLEDGED_MISSING"
        elif needs_decision:
            state = "NEEDS_DECISION"
        else:
            state = "NOT_UPLOADED"

        blocks = False
        if level == "REQUIRED" and applicability == "APPLICABLE" and di is None or level == "REQUIRED" and applicability == "UNRESOLVED" or level == "CONDITIONAL" and needs_decision or (
            level == "CONDITIONAL"
            and applicability == "APPLICABLE"
            and di is None
            and declaration is not None
            and declaration["document_available"] is True
        ):
            blocks = True
        if blocks:
            can_continue = False

        requirement_results.append(
            CaptureV2Requirement(
                requirementKey=key,
                label=str(requirement["display_label"]),
                documentTypeKey=str(requirement["document_type_key"]),
                requirementLevel=level,
                conditionKey=condition_key,
                applicabilityState=applicability,
                state=state,
                document=public_doc,
                canView=public_doc is not None and bool(public_doc.contentUrl),
                canDelete=public_doc is not None,
                needsDecision=needs_decision,
                blocksContinue=blocks,
            )
        )

    return BookingCaptureV2Response(
        journeyId=journey_id,
        externalContextRef=context_ref,
        requirements=requirement_results,
        uploads=uploads,
        declarations=response_declarations,
        canContinue=can_continue,
    )


def _build_local_capture_response(
    *,
    journey_id: UUID,
    requirements: list[dict[str, Any]],
    declaration_rows: dict[str, dict[str, Any]],
    audit_documents: list[dict[str, Any]],
) -> BookingCaptureV2Response:
    di_documents = [
        {
            "documentId": str(row["di_document_id"]),
            "clientUploadId": str(row["client_upload_id"]),
            "state": str(row["capture_status"]),
            "classifiedDocumentTypeKey": row.get("classified_document_type_key"),
            "originalFilename": str(row["original_filename"]),
            "contentUrl": None,
            "processingStatus": None,
        }
        for row in audit_documents
    ]
    return _build_capture_response(
        journey_id=journey_id,
        context_ref="local-v2-completion-check",
        requirements=requirements,
        declaration_rows=declaration_rows,
        audit_documents=audit_documents,
        di_documents=di_documents,
    )


def _read_capture(
    *,
    connection: Connection,
    engine: Engine,
    tenant_id: str,
    journey_id: UUID,
    security_client: SecurityOAuthClient,
    di_client: DiClient,
    v2_client: DiCaptureV2Client,
) -> BookingCaptureV2Response:
    requirements = _base_requirements(connection, tenant_id, journey_id)
    context_ref, token = _ensure_di_context(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
    )
    try:
        di_payload = v2_client.list_documents(
            token=token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            phase="BOOKING",
        )
    except DiCaptureV2Error as exc:
        raise DependencyUnavailableError(
            detail="Document capture status is temporarily unavailable."
        ) from exc
    di_documents = list(di_payload.get("documents") or [])
    _reconcile_documents(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        requirements=requirements,
        di_documents=di_documents,
    )
    return _build_capture_response(
        journey_id=journey_id,
        context_ref=context_ref,
        requirements=requirements,
        declaration_rows=_declarations(connection, tenant_id, journey_id),
        audit_documents=_linked_documents(connection, tenant_id, journey_id),
        di_documents=di_documents,
    )


@router.get("/booking/capture", response_model=BookingCaptureV2Response)
def get_booking_capture_v2(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
    v2_client: Annotated[DiCaptureV2Client, Depends(get_di_capture_v2_client)],
) -> BookingCaptureV2Response:
    _authorize_booking(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    return _read_capture(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
        v2_client=v2_client,
    )


@router.post("/booking/complete", response_model=BookingCaptureV2CompletionResponse)
def complete_booking_capture_v2(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingCaptureV2CompletionResponse:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_version = _parse_if_match(if_match)
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _capture_phase_state(
            connection, tenant_id=tenant_id, journey_id=journey_id, for_update=True
        )
        _require_active_booking(state)
        if int(state["version_no"]) != expected_version:
            raise ConflictError(
                error_code="VAC-CONFLICT-005",
                title="Booking version conflict",
                detail="Booking changed since it was loaded. Refresh the Booking and retry.",
            )
        if state["capture_completed_at_utc"] is not None:
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Booking document capture is complete",
                detail="Booking V2 document capture has already been submitted.",
            )

        local_capture = _build_local_capture_response(
            journey_id=journey_id,
            requirements=_base_requirements(connection, tenant_id, journey_id),
            declaration_rows=_declarations(connection, tenant_id, journey_id),
            audit_documents=_linked_documents(connection, tenant_id, journey_id),
        )
        if not local_capture.canContinue:
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Booking document capture is incomplete",
                detail="Required classifications or applicability decisions are still pending.",
            )

        next_version = expected_version + 1
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET capture_completed_at_utc=now(),
                    pc_verification_status='PENDING',
                    latest_activity_at_utc=now(),
                    updated_at_utc=now(),
                    version_no=:version
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "version": next_version},
        )
        _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="PC_BOOKING_CAPTURE_SUBMITTED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={
                "capturePath": "V2",
                "pcVerificationStatus": "PENDING",
                "bookingBusinessStatusChanged": False,
            },
            aggregate_version=next_version,
        )
        return BookingCaptureV2CompletionResponse(
            journeyId=journey_id, aggregateVersion=next_version
        ).model_dump(mode="json")

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.document-capture-v2.complete:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={"expectedVersion": expected_version},
        execute=execute,
    )
    response.headers["ETag"] = f'"{body["aggregateVersion"]}"'
    return BookingCaptureV2CompletionResponse.model_validate(body)


@router.post("/booking/upload-intents", response_model=UploadIntentResponse)
def create_booking_upload_intents_v2(
    tenant_id: str,
    journey_id: UUID,
    command: UploadIntentCommand,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
    v2_client: Annotated[DiCaptureV2Client, Depends(get_di_capture_v2_client)],
) -> UploadIntentResponse:
    _authorize_booking(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    _require_capture_phase_open(
        connection, tenant_id=tenant_id, journey_id=journey_id
    )
    requirements = _base_requirements(connection, tenant_id, journey_id)
    context_ref, token = _ensure_di_context(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
    )
    try:
        payload = v2_client.create_upload_intents(
            token=token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            phase="BOOKING",
            candidate_document_type_keys=_candidate_type_keys(requirements),
            files=[item.model_dump() for item in command.files],
        )
    except DiCaptureV2Error as exc:
        raise DependencyUnavailableError(detail="Document upload could not be prepared.") from exc

    results: list[UploadIntentResult] = []
    for item in payload.get("uploads") or []:
        document_id = UUID(str(item["documentId"]))
        input_item = next(file for file in command.files if file.clientUploadId == item["clientUploadId"])
        connection.execute(
            text(
                """
                INSERT INTO auditcore.document_capture_v2_documents (
                    tenant_id, journey_id, stage_code, di_document_id,
                    client_upload_id, capture_status, original_filename,
                    content_type, created_by_actor_id
                ) VALUES (
                    :tenant_id, :journey_id, 'BOOKING', :document_id,
                    :client_upload_id, 'RECEIVING', :filename,
                    :content_type, :actor_id
                )
                ON CONFLICT (tenant_id, journey_id, client_upload_id)
                DO UPDATE SET di_document_id=EXCLUDED.di_document_id,
                              original_filename=EXCLUDED.original_filename,
                              content_type=EXCLUDED.content_type,
                              updated_at_utc=now()
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "document_id": document_id,
                "client_upload_id": item["clientUploadId"],
                "filename": input_item.filename,
                "content_type": input_item.contentType,
                "actor_id": _human_actor_id(human_principal),
            },
        )
        results.append(
            UploadIntentResult(
                clientUploadId=item["clientUploadId"],
                documentId=document_id,
                uploadUrl=item["uploadUrl"],
                uploadHeaders=dict(item.get("uploadHeaders") or {}),
                expiresAtUtc=str(item["expiresAtUtc"]),
            )
        )
    return UploadIntentResponse(externalContextRef=context_ref, uploads=results)


@router.post("/booking/documents/{document_id}/finalize", response_model=FinalizeResponse)
def finalize_booking_document_v2(
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
    v2_client: Annotated[DiCaptureV2Client, Depends(get_di_capture_v2_client)],
) -> FinalizeResponse:
    _authorize_booking(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    _require_capture_phase_open(
        connection, tenant_id=tenant_id, journey_id=journey_id
    )
    context_ref, token = _ensure_di_context(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
    )
    try:
        payload = v2_client.finalize_document(
            token=token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            document_id=str(document_id),
        )
    except DiCaptureV2Error as exc:
        raise DependencyUnavailableError(detail="Uploaded document could not be finalized.") from exc
    return FinalizeResponse(documentId=document_id, state=str(payload["state"]))


@router.delete("/booking/documents/{document_id}", status_code=204)
def delete_booking_document_v2(
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
    v2_client: Annotated[DiCaptureV2Client, Depends(get_di_capture_v2_client)],
) -> None:
    _authorize_booking(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    _require_capture_phase_open(
        connection, tenant_id=tenant_id, journey_id=journey_id
    )
    exists = connection.execute(
        text(
            """
            SELECT 1 FROM auditcore.document_capture_v2_documents
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING' AND di_document_id=:document_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "document_id": document_id},
    ).scalar_one_or_none()
    if exists is None:
        return
    context_ref, token = _ensure_di_context(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
    )
    try:
        v2_client.delete_document(
            token=token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            document_id=str(document_id),
        )
    except DiCaptureV2Error as exc:
        raise DependencyUnavailableError(detail="Document could not be deleted safely.") from exc
    connection.execute(
        text(
            """
            DELETE FROM auditcore.document_capture_v2_documents
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING' AND di_document_id=:document_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "document_id": document_id},
    )


@router.put("/booking/declarations/{condition_key}", response_model=BookingCaptureV2Response)
def set_booking_declaration_v2(
    tenant_id: str,
    journey_id: UUID,
    condition_key: str,
    command: ConditionalDeclarationCommand,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
    v2_client: Annotated[DiCaptureV2Client, Depends(get_di_capture_v2_client)],
) -> BookingCaptureV2Response:
    _authorize_booking(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    _require_capture_phase_open(
        connection, tenant_id=tenant_id, journey_id=journey_id
    )
    allowed = {
        str(row["condition_key"])
        for row in _base_requirements(connection, tenant_id, journey_id)
        if row.get("condition_key")
    }
    if condition_key not in allowed:
        raise NotFoundError(
            error_code="VAC-NF-006",
            title="Document condition not found",
            detail="This Booking does not contain the requested V2 document condition.",
        )
    if command.applicable and command.documentAvailable is None:
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Document availability is required",
            detail="When the condition is applicable, document availability must be answered.",
        )
    document_available = command.documentAvailable if command.applicable else None
    connection.execute(
        text(
            """
            INSERT INTO auditcore.document_capture_v2_declarations (
                tenant_id, journey_id, stage_code, condition_key,
                applicable, document_available, declared_by_actor_id
            ) VALUES (
                :tenant_id, :journey_id, 'BOOKING', :condition_key,
                :applicable, :document_available, :actor_id
            )
            ON CONFLICT (tenant_id, journey_id, stage_code, condition_key)
            DO UPDATE SET applicable=EXCLUDED.applicable,
                          document_available=EXCLUDED.document_available,
                          declared_by_actor_id=EXCLUDED.declared_by_actor_id,
                          declared_at_utc=now(), updated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "condition_key": condition_key,
            "applicable": command.applicable,
            "document_available": document_available,
            "actor_id": _human_actor_id(human_principal),
        },
    )
    return _read_capture(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
        v2_client=v2_client,
    )
