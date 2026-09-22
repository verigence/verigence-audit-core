from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy import Connection, Engine, text

from audit_core.dependencies import get_connection, get_engine, get_human_principal
from audit_core.di_capture_v2_client import DiCaptureV2Client, DiCaptureV2Error
from audit_core.di_client import DiClient
from audit_core.errors import ConflictError, DependencyUnavailableError, NotFoundError
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient
from audit_core.uc03_booking_capture import _scope
from audit_core.uc03_delivery_commands import _append_delivery_event, _machine_flag
from audit_core.uc03_document_capture_v2 import (
    CaptureV2Document,
    CaptureV2Requirement,
    FinalizeResponse,
    UploadIntentCommand,
    UploadIntentResponse,
    UploadIntentResult,
    _candidate_type_keys,
    _ensure_di_context,
    _human_actor_id,
    _log_di_capture_v2_failure,
    _requirement_refs_by_document_type_key,
    _upload_intent_failures,
    get_di_capture_v2_client,
    get_di_client,
    get_security_oauth_client,
)
from audit_core.workflow import (
    cancel_workflow_task,
    complete_workflow_task,
    create_workflow_task,
)

router = APIRouter(
    prefix="/v2/tenants/{tenant_id}/journeys/{journey_id}/delivery",
    tags=["uc03-delivery-capture-v2"],
)

_TERMINAL_FAILURE_STATES = {"FAILED", "ERROR", "REJECTED"}

# A document that failed processing (corrupt file, unreadable scan, unsupported
# format) isn't a rule breach -- there's nothing to adjudicate, only a document
# for PC to re-upload. Standalone task (no backing Audit Finding), same shape
# as uc03_document_unrecognized.py's PC_VERIFY_UNRECOGNIZED_DOCUMENT.
_PROCESSING_FAILED_TASK_TYPE = "PC_RESOLVE_DOCUMENT_PROCESSING_FAILURE"
_PROCESSING_FAILED_WORKFLOW_TYPE = "UC03_DOCUMENT_VERIFICATION"

# One workflow_tasks row per journey+stage for each side of the handoff --
# not a new column on journey_stage_states. PC's own task records when PC
# actually finished (created and completed together, right here, the same
# instant Submit succeeds -- Submit itself stays unconditional). TL's task
# (see uc03_delivery_review_readiness_sweep.py) is raised separately, only
# once the system confirms document/data review is genuinely done -- its own
# created_at_utc is TL's real SLA/KPI clock, deliberately decoupled from
# PC's click, which workflow_tasks already tracks correctly without needing
# any new state anywhere else.
PC_DELIVERY_CAPTURE_TASK_TYPE = "PC_DELIVERY_CAPTURE"
_PC_DELIVERY_CAPTURE_WORKFLOW_TYPE = "UC03_DELIVERY_CAPTURE"
TL_DELIVERY_REVIEW_TASK_TYPE = "TL_DELIVERY_REVIEW"
TL_DELIVERY_REVIEW_WORKFLOW_TYPE = "UC03_DELIVERY_REVIEW"


def _pc_delivery_capture_effect_key(tenant_id: str, journey_id: UUID) -> str:
    return f"task:delivery-pc-capture:{tenant_id}:{journey_id}"


def tl_delivery_review_effect_key_prefix(tenant_id: str) -> str:
    # Split out from tl_delivery_review_effect_key so
    # uc03_delivery_review_readiness_sweep.py's own "does a TL task already
    # exist for this journey" SQL can build its WHERE-clause prefix from the
    # exact same source instead of a second, hand-copied literal that could
    # silently drift out of sync with this one.
    return f"task:delivery-tl-review:{tenant_id}:"


def tl_delivery_review_effect_key(tenant_id: str, journey_id: UUID) -> str:
    return f"{tl_delivery_review_effect_key_prefix(tenant_id)}{journey_id}"


def _processing_failed_effect_key(tenant_id: str, journey_id: UUID, document_id: str) -> str:
    return f"task:document-processing-failed:{tenant_id}:{journey_id}:{document_id}"


class DeliveryCaptureV2Response(BaseModel):
    journeyId: UUID
    externalContextRef: str
    phase: Literal["DELIVERY"] = "DELIVERY"
    requirements: list[CaptureV2Requirement]
    uploads: list[CaptureV2Document]
    canSubmit: bool = True
    submitted: bool = False


class DeliveryCaptureV2SubmissionResponse(BaseModel):
    journeyId: UUID
    phase: Literal["DELIVERY"] = "DELIVERY"
    status: Literal["SUBMITTED"] = "SUBMITTED"
    aggregateVersion: int
    raisedFlagIds: list[UUID]


def _delivery_state(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT business_status, capture_completed_at_utc, version_no
            FROM auditcore.journey_stage_states
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='DELIVERY'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Delivery not found",
            detail="Start Delivery before capturing Delivery documents.",
        )
    return dict(row)


def _authorize_delivery(
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
    return _delivery_state(connection, tenant_id=tenant_id, journey_id=journey_id)


def _delivery_requirements(
    connection: Connection,
    tenant_id: str,
    journey_id: UUID,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT jdr.journey_document_requirement_id AS requirement_ref,
                   jdr.requirement_key, jdr.document_type_key,
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
             AND p.process_area='DELIVERY'
             AND p.is_active=true
            WHERE jdr.tenant_id=:tenant_id
              AND jdr.journey_id=:journey_id
              AND upper(jdr.process_area)='DELIVERY'
            ORDER BY COALESCE(p.sort_order, dri.sort_order, 999999), jdr.requirement_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    requirements = [dict(row) for row in rows]

    extensions = connection.execute(
        text(
            """
            SELECT NULL::uuid AS requirement_ref,
                   requirement_key,
                   extension_document_type_key AS document_type_key,
                   extension_requirement_level AS requirement_level,
                   'PENDING' AS requirement_status,
                   display_label, condition_key, sort_order
            FROM auditcore.document_capture_v2_requirement_policy
            WHERE process_area='DELIVERY' AND is_active=true AND is_extension=true
            ORDER BY sort_order, requirement_key
            """
        )
    ).mappings().all()
    existing = {row["requirement_key"] for row in requirements}
    requirements.extend(dict(row) for row in extensions if row["requirement_key"] not in existing)
    requirements.sort(key=lambda row: (int(row.get("sort_order") or 999999), row["requirement_key"]))
    return requirements


def _linked_delivery_documents(
    connection: Connection,
    tenant_id: str,
    journey_id: UUID,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT di_document_id, client_upload_id, requirement_key,
                   classified_document_type_key, capture_status,
                   original_filename, content_type
            FROM auditcore.document_capture_v2_documents
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='DELIVERY' AND capture_status <> 'SUPERSEDED'
            ORDER BY created_at_utc, di_document_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _reconcile_delivery_documents(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    requirements: list[dict[str, Any]],
    di_documents: list[dict[str, Any]],
) -> None:
    type_to_requirement: dict[str, str] = {}
    for requirement in requirements:
        document_type_key = requirement.get("document_type_key")
        if document_type_key:
            type_to_requirement.setdefault(
                str(document_type_key),
                str(requirement["requirement_key"]),
            )

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
                  AND stage_code='DELIVERY' AND di_document_id=:document_id
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


def _build_delivery_capture_response(
    *,
    journey_id: UUID,
    context_ref: str,
    requirements: list[dict[str, Any]],
    audit_documents: list[dict[str, Any]],
    di_documents: list[dict[str, Any]],
    submitted: bool,
) -> DeliveryCaptureV2Response:
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
        if link.get("requirement_key") and str(di["state"]).upper() == "CLASSIFIED":
            active_by_requirement.setdefault(str(link["requirement_key"]), di)

    results: list[CaptureV2Requirement] = []
    for requirement in requirements:
        key = str(requirement["requirement_key"])
        level = str(requirement["requirement_level"])
        requirement_status = str(requirement.get("requirement_status") or "PENDING").upper()
        di = active_by_requirement.get(key)
        not_applicable = requirement_status == "NOT_APPLICABLE"
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
        results.append(
            CaptureV2Requirement(
                requirementKey=key,
                label=str(requirement["display_label"]),
                documentTypeKey=str(requirement["document_type_key"]),
                requirementLevel=level,
                conditionKey=(str(requirement["condition_key"]) if requirement.get("condition_key") else None),
                applicabilityState="NOT_APPLICABLE" if not_applicable else "APPLICABLE",
                state=("NOT_APPLICABLE" if not_applicable else "UPLOADED" if public_doc else "NOT_UPLOADED"),
                document=public_doc,
                canView=public_doc is not None and bool(public_doc.contentUrl),
                canDelete=public_doc is not None and not submitted,
                needsDecision=False,
                blocksContinue=False,
            )
        )

    return DeliveryCaptureV2Response(
        journeyId=journey_id,
        externalContextRef=context_ref,
        requirements=results,
        uploads=uploads,
        canSubmit=True,
        submitted=submitted,
    )


def _build_local_delivery_capture_response(
    *,
    journey_id: UUID,
    requirements: list[dict[str, Any]],
    audit_documents: list[dict[str, Any]],
    submitted: bool,
) -> DeliveryCaptureV2Response:
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
    return _build_delivery_capture_response(
        journey_id=journey_id,
        context_ref="local-v2-delivery-capture",
        requirements=requirements,
        audit_documents=audit_documents,
        di_documents=di_documents,
        submitted=submitted,
    )


def _read_delivery_capture(
    *,
    connection: Connection,
    engine: Engine,
    tenant_id: str,
    journey_id: UUID,
    security_client: SecurityOAuthClient,
    di_client: DiClient,
    v2_client: DiCaptureV2Client,
) -> DeliveryCaptureV2Response:
    state = _delivery_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    requirements = _delivery_requirements(connection, tenant_id, journey_id)
    audit_documents = _linked_delivery_documents(connection, tenant_id, journey_id)
    submitted = state.get("capture_completed_at_utc") is not None
    if not audit_documents:
        return _build_local_delivery_capture_response(
            journey_id=journey_id,
            requirements=requirements,
            audit_documents=audit_documents,
            submitted=submitted,
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
        payload = v2_client.list_documents(
            token=token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            phase="DELIVERY",
        )
    except DiCaptureV2Error as exc:
        _log_di_capture_v2_failure(
            operation="list_documents", exc=exc, tenant_id=tenant_id,
            journey_id=journey_id, context_ref=context_ref,
        )
        raise DependencyUnavailableError(
            detail="Delivery document status is temporarily unavailable."
        ) from exc
    di_documents = list(payload.get("documents") or [])
    _reconcile_delivery_documents(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        requirements=requirements,
        di_documents=di_documents,
    )
    from audit_core.uc03_document_unrecognized import (
        sync_document_unrecognized_findings,
    )

    sync_document_unrecognized_findings(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code="DELIVERY",
        di_documents=di_documents,
        correlation_id="",
    )
    return _build_delivery_capture_response(
        journey_id=journey_id,
        context_ref=context_ref,
        requirements=requirements,
        audit_documents=_linked_delivery_documents(connection, tenant_id, journey_id),
        di_documents=di_documents,
        submitted=submitted,
    )


@router.get("/capture", response_model=DeliveryCaptureV2Response)
def get_delivery_capture_v2(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
    v2_client: Annotated[DiCaptureV2Client, Depends(get_di_capture_v2_client)],
) -> DeliveryCaptureV2Response:
    _authorize_delivery(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    return _read_delivery_capture(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
        v2_client=v2_client,
    )


@router.post("/upload-intents", response_model=UploadIntentResponse)
def create_delivery_upload_intents_v2(
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
    # Documents legitimately keep arriving after the PC has moved on to
    # Delivery Details -- a late invoice, a corrected receipt -- and nothing
    # about accepting one more upload conflicts with a submission that
    # already happened (each document syncs/materializes independently).
    # Only deleting already-submitted evidence stays locked (see
    # delete_delivery_document_v2), not adding to it.
    _authorize_delivery(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    requirements = _delivery_requirements(connection, tenant_id, journey_id)
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
            phase="DELIVERY",
            candidate_document_type_keys=_candidate_type_keys(requirements),
            requirement_refs_by_document_type_key=(
                _requirement_refs_by_document_type_key(requirements)
            ),
            files=[item.model_dump() for item in command.files],
        )
    except DiCaptureV2Error as exc:
        _log_di_capture_v2_failure(
            operation="create_upload_intents", exc=exc, tenant_id=tenant_id,
            journey_id=journey_id, context_ref=context_ref,
        )
        raise DependencyUnavailableError(
            detail="Delivery document upload could not be prepared."
        ) from exc

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
                    :tenant_id, :journey_id, 'DELIVERY', :document_id,
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
    return UploadIntentResponse(
        externalContextRef=context_ref, uploads=results, failures=_upload_intent_failures(payload)
    )


@router.post("/documents/{document_id}/finalize", response_model=FinalizeResponse)
def finalize_delivery_document_v2(
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
    # See create_delivery_upload_intents_v2 -- adding a document after
    # submission is allowed; only deleting one is locked.
    _authorize_delivery(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    exists = connection.execute(
        text(
            """
            SELECT 1 FROM auditcore.document_capture_v2_documents
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='DELIVERY' AND di_document_id=:document_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "document_id": document_id},
    ).scalar_one_or_none()
    if exists is None:
        raise NotFoundError(
            error_code="VAC-NF-006",
            title="Delivery document not found",
            detail="The uploaded document is not linked to this Delivery.",
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
        _log_di_capture_v2_failure(
            operation="finalize_document", exc=exc, tenant_id=tenant_id,
            journey_id=journey_id, context_ref=context_ref,
        )
        raise DependencyUnavailableError(
            detail="Uploaded Delivery document could not be finalized."
        ) from exc
    return FinalizeResponse(documentId=document_id, state=str(payload["state"]))


@router.delete("/documents/{document_id}", status_code=204)
def delete_delivery_document_v2(
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
    state = _authorize_delivery(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    if state.get("capture_completed_at_utc") is not None:
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Delivery document submission is complete",
            detail="Submitted audit evidence cannot be deleted from the Delivery review flow.",
        )
    exists = connection.execute(
        text(
            """
            SELECT 1 FROM auditcore.document_capture_v2_documents
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='DELIVERY' AND di_document_id=:document_id
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
        _log_di_capture_v2_failure(
            operation="delete_document", exc=exc, tenant_id=tenant_id,
            journey_id=journey_id, context_ref=context_ref,
        )
        raise DependencyUnavailableError(
            detail="Delivery document could not be deleted safely."
        ) from exc
    connection.execute(
        text(
            """
            DELETE FROM auditcore.document_capture_v2_documents
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='DELIVERY' AND di_document_id=:document_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "document_id": document_id},
    )


def _raise_delivery_capture_exceptions(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    requirements: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    correlation_id: str,
) -> list[UUID]:
    flags: list[UUID] = []
    active_by_requirement = {
        str(row["requirement_key"])
        for row in documents
        if row.get("requirement_key") and str(row.get("capture_status") or "").upper() == "CLASSIFIED"
    }
    classification_pending = any(
        str(row.get("capture_status") or "").upper() in {"RECEIVING", "STORED", "CLASSIFYING"}
        for row in documents
    )

    if not classification_pending:
        for requirement in requirements:
            if str(requirement.get("requirement_level") or "").upper() != "REQUIRED":
                continue
            if str(requirement.get("requirement_status") or "").upper() == "NOT_APPLICABLE":
                continue
            key = str(requirement["requirement_key"])
            if key in active_by_requirement:
                continue
            flags.append(
                _machine_flag(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    stage_code="DELIVERY",
                    rule_key=f"DL_V2_REQUIRED_DOCUMENT_MISSING:{key}",
                    finding_type="DELIVERY_DOCUMENT_MISSING",
                    severity="HIGH",
                    title=f"Delivery document missing: {requirement['display_label']}",
                    description="The Delivery was submitted without this configured mandatory document. The business process was not blocked.",
                    correlation_id=correlation_id,
                    safe_payload={"requirementKey": key, "capturePath": "V2"},
                    blocking_completion=False,
                )
            )

    for row in documents:
        state = str(row.get("capture_status") or "").upper()
        if state in _TERMINAL_FAILURE_STATES:
            document_id = str(row["di_document_id"])
            effect_key = _processing_failed_effect_key(tenant_id, journey_id, document_id)
            existing = connection.execute(
                text(
                    "SELECT 1 FROM auditcore.workflow_tasks "
                    "WHERE tenant_id = :tenant_id AND effect_key = :effect_key"
                ),
                {"tenant_id": tenant_id, "effect_key": effect_key},
            ).scalar_one_or_none()
            if existing is None:
                create_workflow_task(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    workflow_type=_PROCESSING_FAILED_WORKFLOW_TYPE,
                    process_area="DELIVERY",
                    task_type=_PROCESSING_FAILED_TASK_TYPE,
                    assigned_role_code="PC",
                    task_payload={
                        "diDocumentId": document_id,
                        "capturePath": "V2",
                        "comment": "A submitted Delivery document could not be processed successfully. Re-upload a clearer copy.",
                    },
                    effect_key=effect_key,
                    correlation_id=correlation_id,
                )
    return list(dict.fromkeys(flags))


def _resolve_delivery_capture_exceptions(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    requirements: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    correlation_id: str,
) -> list[UUID]:
    """Self-heal the two document-gap exceptions _raise_delivery_capture_
    exceptions raises: a DL_V2_REQUIRED_DOCUMENT_MISSING finding closes once its
    requirement has an active classified document; the standalone
    PC_RESOLVE_DOCUMENT_PROCESSING_FAILURE task cancels once that document is
    no longer in a terminal failure state (retried successfully, replaced, or
    removed).

    _raise_delivery_capture_exceptions was, until now, only ever called once --
    at Submit. Nothing ever re-evaluated it afterward, so a document uploaded
    later (via "Add more documents", or in direct response to the Audit Flag's
    own "Upload document" action) never cleared the finding it was meant to
    resolve, even though the real gap was gone. Mirrors _run_booking_rules'
    resolve-if-not-flagged pattern (uc03_booking_rule_trigger.py).
    """
    from audit_core.uc03_manual_verification import _resolve_finding

    resolved: list[UUID] = []
    active_by_requirement = {
        str(row["requirement_key"])
        for row in documents
        if row.get("requirement_key") and str(row.get("capture_status") or "").upper() == "CLASSIFIED"
    }
    for requirement in requirements:
        if str(requirement.get("requirement_level") or "").upper() != "REQUIRED":
            continue
        if str(requirement.get("requirement_status") or "").upper() == "NOT_APPLICABLE":
            continue
        key = str(requirement["requirement_key"])
        if key not in active_by_requirement:
            continue
        open_finding_id = connection.execute(
            text(
                """
                SELECT audit_finding_id
                FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND rule_key=:rule_key AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "rule_key": f"DL_V2_REQUIRED_DOCUMENT_MISSING:{key}",
            },
        ).scalar_one_or_none()
        if open_finding_id is None:
            continue
        _resolve_finding(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code="DELIVERY",
            finding_id=open_finding_id,
            actor_id=None,
            correlation_id=correlation_id,
            note="The configured mandatory document has since been uploaded and classified.",
        )
        resolved.append(open_finding_id)

    failing_document_ids = {
        str(row["di_document_id"])
        for row in documents
        if str(row.get("capture_status") or "").upper() in _TERMINAL_FAILURE_STATES
    }
    open_processing_tasks = connection.execute(
        text(
            """
            SELECT workflow_task_id, effect_key
            FROM auditcore.workflow_tasks
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND task_type = :task_type
              AND task_status IN ('PENDING','READY','CLAIMED','IN_PROGRESS','RETRY_WAIT')
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "task_type": _PROCESSING_FAILED_TASK_TYPE},
    ).mappings().all()
    for task in open_processing_tasks:
        document_id = str(task["effect_key"]).rsplit(":", 1)[-1]
        if document_id in failing_document_ids:
            continue
        cancel_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task["workflow_task_id"],
            actor_id="SYSTEM",
            reason="The document no longer requires follow-up.",
        )
    return resolved


def schedule_delivery_document_checkpoint(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    correlation_id: str,
    raise_new: bool = True,
) -> tuple[list[UUID], list[UUID]]:
    """Re-evaluate Delivery's document gaps against current durable state:
    raise the DL_V2_REQUIRED_DOCUMENT_MISSING finding for whatever's still
    missing, raise the standalone PC_RESOLVE_DOCUMENT_PROCESSING_FAILURE
    task for whatever's still failing (no finding -- a document that failed
    processing isn't a rule breach, just a re-upload PC owns), and resolve/
    cancel whichever of those have since cleared. Called once per confirmed
    document, the same trigger
    Booking's schedule_booking_checkpoint_rules uses -- Delivery routinely
    confirms 10-15 documents in a tight burst (real dealership upload
    behaviour, already the cause of one live lock-contention incident this
    session), so this uses pg_try_advisory_xact_lock (non-blocking): several
    documents from the same burst each open their own connection to call
    this (see _run_sync_booking_document_task), and this service's
    connection pool is small (SQLAlchemy defaults, pool_timeout=5s). A
    BLOCKING lock would have each of those connections sit idle-in-wait for
    the whole burst instead of being returned to the pool, which is exactly
    what starves an unrelated request (e.g. opening a Booking) waiting for a
    free connection. _sync_booking_document's own per-journey lock uses the
    identical non-blocking primitive now, for the same reason, but retries
    instead of skipping -- see its docstring for why a document's own sync
    can't just be skipped the way a checkpoint re-evaluation can. Skipping
    when contended costs nothing here -- the document that's already
    running this reads the same current state, and the very
    next confirmed document re-triggers it anyway.

    raise_new=False (the per-document DI-webhook trigger, see
    uc03_confidence_review_policy.py) still resolves findings that have
    cleared, but never raises a new one: Delivery routinely has 5-10
    required documents and a PC uploads them one at a time, so evaluating
    "is anything still missing" after every single confirm would flag every
    not-yet-uploaded document as a gap mid-upload -- a false alarm, not a
    real one, since the PC isn't done yet and nothing has actually been
    skipped. The genuine gap check -- raising for whatever is still missing
    once the PC believes Delivery is complete -- runs at Submit
    (raise_new's default), the same moment schedule_booking_checkpoint_rules
    plays for Booking.
    """
    acquired = connection.execute(
        text("SELECT pg_try_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"uc03-delivery-document-checkpoint:{tenant_id}:{journey_id}"},
    ).scalar_one()
    if not acquired:
        return [], []

    requirements = _delivery_requirements(connection, tenant_id, journey_id)
    documents = _linked_delivery_documents(connection, tenant_id, journey_id)
    raised = (
        _raise_delivery_capture_exceptions(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            requirements=requirements,
            documents=documents,
            correlation_id=correlation_id,
        )
        if raise_new
        else []
    )
    resolved = _resolve_delivery_capture_exceptions(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        requirements=requirements,
        documents=documents,
        correlation_id=correlation_id,
    )
    return raised, resolved


_OPEN_TASK_STATUSES = ("PENDING", "READY", "CLAIMED", "IN_PROGRESS", "RETRY_WAIT")


def delivery_review_readiness_blockers(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[dict[str, Any]],
) -> list[str]:
    """What must be resolved before this Delivery's document/data review is
    genuinely finished -- read by uc03_delivery_review_readiness_sweep.py to
    decide when to raise the TL_DELIVERY_REVIEW task, NOT by Submit itself
    (Submit stays unconditional, exactly as it always has -- PC's own action
    and the system's own confirmation of readiness are two different events
    on two different clocks, not one gated button).

    Scoped to exactly the three self-serviceable-by-PC categories: a
    document genuinely missing, a document that failed processing or was
    rejected as wrong/duplicate, and an unreviewed low-confidence field.
    Deliberately excludes VIOLATION-class findings (those need a TL
    decision, not PC action -- would be circular to gate TL's own task on
    something only TL can resolve).
    """
    blockers: list[str] = []

    if any(
        str(row.get("capture_status") or "").upper() in {"RECEIVING", "STORED", "CLASSIFYING"}
        for row in documents
    ):
        blockers.append("One or more uploaded documents are still being processed.")

    # Distinct from the check above -- a document can be past classification
    # (capture_status='CLASSIFIED') while DI is still extracting/confirming
    # its fields. confirmation_status_cache is auditcore's own local mirror
    # of DI's confirmation_status, kept current by the same per-document
    # webhook sync as everything else here (uc03_confidence_review_policy.py
    # _sync_booking_document) -- 'CONFIRMED' is the exact value that sync
    # gates a durable fact copy on, so anything else means this document's
    # classification finished but its extraction has not.
    unextracted_documents = connection.execute(
        text(
            """
            SELECT count(*) FROM auditcore.evidence
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND process_area='DELIVERY' AND association_status='ACTIVE'
              AND confirmation_status_cache IS DISTINCT FROM 'CONFIRMED'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one()
    if unextracted_documents:
        blockers.append(
            f"{unextracted_documents} classified document(s) have not finished extraction yet."
        )

    missing_documents = connection.execute(
        text(
            """
            SELECT count(*) FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='DELIVERY'
              AND finding_status IN ('OPEN','ACKNOWLEDGED')
              AND rule_key LIKE 'DL_V2_REQUIRED_DOCUMENT_MISSING:%'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one()
    if missing_documents:
        blockers.append(f"{missing_documents} required document(s) are still missing.")

    # Local imports, same reasoning as this pipeline's other cross-module
    # TASK_TYPE references (e.g. uc03_confidence_review_policy.py) -- avoids
    # any chance of a load-order/circular-import issue at app startup.
    from audit_core.uc03_customer_identity_consistency import (
        DEALER_TASK_TYPE as _wrong_document_dealer_task_type,
    )
    from audit_core.uc03_customer_identity_consistency import (
        TASK_TYPE as _wrong_document_task_type,
    )
    from audit_core.uc03_document_field_corrections import (
        TASK_TYPE as _field_correction_task_type,
    )
    from audit_core.uc03_duplicate_receipt_detection import (
        TASK_TYPE as _duplicate_receipt_task_type,
    )
    from audit_core.uc03_manual_verification import (
        TASK_TYPE as _manual_verification_task_type,
    )

    open_tasks = connection.execute(
        text(
            """
            SELECT count(*) FROM auditcore.workflow_tasks
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND process_area='DELIVERY'
              AND task_type = ANY(:task_types)
              AND task_status = ANY(:statuses)
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "task_types": [
                _PROCESSING_FAILED_TASK_TYPE,
                _duplicate_receipt_task_type,
                _wrong_document_task_type,
                _wrong_document_dealer_task_type,
                _manual_verification_task_type,
                _field_correction_task_type,
            ],
            "statuses": list(_OPEN_TASK_STATUSES),
        },
    ).scalar_one()
    if open_tasks:
        blockers.append(
            f"{open_tasks} open Task Queue item(s) (document or data review) still require PC action."
        )

    return blockers


@router.post("/submit", response_model=DeliveryCaptureV2SubmissionResponse)
def submit_delivery_capture_v2(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DeliveryCaptureV2SubmissionResponse:
    state = _authorize_delivery(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        refreshed = _delivery_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        aggregate_version = int(refreshed["version_no"])
        # Safety net, same role Submit plays for document sync itself: the
        # async per-document trigger (schedule_delivery_document_checkpoint,
        # called from the DI webhook) already keeps these findings current,
        # but Submit re-evaluates once more here in case anything hasn't
        # landed yet.
        flags, _resolved = schedule_delivery_document_checkpoint(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            correlation_id=correlation_id,
        )
        documents = _linked_delivery_documents(connection, tenant_id, journey_id)
        first_submission = refreshed.get("capture_completed_at_utc") is None
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET capture_completed_at_utc=COALESCE(capture_completed_at_utc, now()),
                    pc_verification_status=COALESCE(pc_verification_status, 'PENDING'),
                    latest_activity_at_utc=now(), updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='DELIVERY'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        )
        if first_submission:
            pc_task_id = create_workflow_task(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                workflow_type=_PC_DELIVERY_CAPTURE_WORKFLOW_TYPE,
                process_area="DELIVERY",
                task_type=PC_DELIVERY_CAPTURE_TASK_TYPE,
                assigned_role_code="PC",
                assigned_actor_id=human_principal.subject,
                task_payload={"documentCount": len(documents)},
                effect_key=_pc_delivery_capture_effect_key(tenant_id, journey_id),
                correlation_id=correlation_id,
            )
            complete_workflow_task(
                connection, tenant_id=tenant_id, workflow_task_id=pc_task_id,
                actor_id=human_principal.subject,
            )
        _append_delivery_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="PC_DELIVERY_DOCUMENTS_SUBMITTED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=None,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={
                "capturePath": "V2",
                "documentCount": len(documents),
                "raisedFlagIds": [str(flag_id) for flag_id in flags],
                "auditBlocksBusinessProcess": False,
            },
            aggregate_version=aggregate_version,
        )
        return DeliveryCaptureV2SubmissionResponse(
            journeyId=journey_id,
            aggregateVersion=aggregate_version,
            raisedFlagIds=flags,
        ).model_dump(mode="json")

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.delivery.document-capture-v2.submit:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={"businessStatus": state.get("business_status")},
        execute=execute,
    )
    return DeliveryCaptureV2SubmissionResponse.model_validate(body)


def _resyncable_document_ids(documents: list[dict[str, Any]]) -> list[UUID]:
    """Only a document DI has actually classified is worth re-syncing --
    one still mid-classification has nothing durable to copy yet, and
    _sync_booking_document's own DOCUMENT_MISSING/manual-verification logic
    already covers a document that never got this far."""
    return [
        row["di_document_id"] for row in documents
        if str(row.get("capture_status") or "").upper() == "CLASSIFIED"
    ]


class DeliveryCaptureV2ResyncResponse(BaseModel):
    documentsFound: int
    documentsResynced: int
    documentsNotYetExtracted: int
    queuedDocumentCount: int


@router.post("/resync", response_model=DeliveryCaptureV2ResyncResponse)
def resync_delivery_capture_v2(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
    v2_client: Annotated[DiCaptureV2Client, Depends(get_di_capture_v2_client)],
    background_tasks: BackgroundTasks,
) -> DeliveryCaptureV2ResyncResponse:
    """Force every already-classified Delivery document through the full
    per-document sync pipeline again (durable fact copy, MANUAL_VERIFICATION,
    payment reconciliation, canonical materialization, document-gap
    checkpoint) -- confirmed live need: a document whose sync attempt fails
    (e.g. the statement-timeout regression on a 15-document upload burst,
    now fixed) never gets a second try. DI's webhook already received a fast
    200 OK for the link callback before that failure happened (the whole
    point of the fast-ack/background-task split), so from DI's side the
    callback was delivered successfully -- it will not retry on its own, no
    matter how long the document sits with durable state never written.
    Idempotent and cheap to call repeatedly: _sync_booking_document's own
    per-journey advisory lock still serializes these against any concurrent
    webhook-triggered sync for the same journey.

    Refreshes classification status from DI's own live state first (via
    _reconcile_delivery_documents, the same call the capture screen's own
    read makes) before deciding which documents are resyncable -- without
    this, a Journey whose Delivery capture screen has not been reopened
    since classification actually finished would be filtered against a
    stale local cache and silently resync 0 documents.
    """
    _authorize_delivery(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    requirements = _delivery_requirements(connection, tenant_id, journey_id)
    if _linked_delivery_documents(connection, tenant_id, journey_id):
        context_ref, token = _ensure_di_context(
            connection=connection,
            engine=engine,
            tenant_id=tenant_id,
            journey_id=journey_id,
            security_client=security_client,
            di_client=di_client,
        )
        try:
            payload = v2_client.list_documents(
                token=token,
                tenant_id=tenant_id,
                external_context_ref=context_ref,
                phase="DELIVERY",
            )
        except DiCaptureV2Error as exc:
            _log_di_capture_v2_failure(
                operation="list_documents", exc=exc, tenant_id=tenant_id,
                journey_id=journey_id, context_ref=context_ref,
            )
            raise DependencyUnavailableError(
                detail="Document status is temporarily unavailable -- try resync again shortly."
            ) from exc
        _reconcile_delivery_documents(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            requirements=requirements,
            di_documents=list(payload.get("documents") or []),
        )

    documents = _linked_delivery_documents(connection, tenant_id, journey_id)
    document_ids = _resyncable_document_ids(documents)

    # Backfill any missing/inactive evidence link BEFORE queuing the sync
    # task -- see uc03_document_capture_v2._ensure_evidence_link_for_resync's
    # own docstring. Without this, a document whose one-time DI "link"
    # callback never landed would report as resynced while
    # _sync_booking_document silently does nothing.
    from audit_core.uc03_document_capture_v2 import _backfill_evidence_links_for_resync

    _backfill_evidence_links_for_resync(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=documents,
        document_ids=document_ids,
        requirements=requirements,
        service_id=f"manual-resync:{human_principal.subject}",
    )

    from audit_core.uc03_confidence_review_policy import (
        _run_sync_booking_document_task,
        sync_stagger_seconds,
    )

    for index, document_id in enumerate(document_ids):
        background_tasks.add_task(
            _run_sync_booking_document_task,
            engine,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document_id,
            service_id=f"manual-resync:{human_principal.subject}",
            stage_code="DELIVERY",
            initial_delay_seconds=sync_stagger_seconds(index),
        )
    return DeliveryCaptureV2ResyncResponse(
        documentsFound=len(documents),
        documentsResynced=len(document_ids),
        documentsNotYetExtracted=len(documents) - len(document_ids),
        queuedDocumentCount=len(document_ids),
    )
