"""uc03_unified_document_capture.py — one upload screen, Booking and
Delivery alike (2026-09-13).

Deliberately additive: neither of the existing Booking/Delivery capture
screens, their own upload-intent endpoints, nor their own reconciliation
functions (``_reconcile_documents`` / ``_reconcile_delivery_documents``)
are touched. This module is a new, parallel path the unified Journey
Documents page uses instead -- "keep existing functionality intact" per
explicit instruction.

Why a new path rather than teaching the old ones to merge:

  - DI's own upload-intent contract is phase-scoped
    (``DiCaptureV2Client.create_upload_intents``/``list_documents`` both
    take a ``phase``, and ``list_documents(phase=X)`` genuinely only
    returns documents DI filed under that phase). A document uploaded
    without an upfront stage choice has to be filed under SOME phase at
    DI -- this module always uses ``"BOOKING"`` (arbitrary but harmless;
    the real classification scope is ``candidate_document_type_keys``,
    not the phase label) and never asks DI to partition by stage at all.
  - Its own reconciliation (``reconcile_unified_documents`` below) is what
    actually decides Booking vs. Delivery, from the classified type, and
    corrects ``document_capture_v2_documents.stage_code`` accordingly --
    that column is local bookkeeping only (which of Booking's/Delivery's
    own existing GET endpoints will list the document), NOT what drives
    sync/extraction correctness. That correctness comes entirely from
    ``requirement_refs_by_document_type_key`` -- DI's document-link
    webhook (``acknowledge_booking_document_link_with_auto_sync``)
    resolves BOOKING/DELIVERY from the *requirement* a classified document
    got bound to at upload time (see uc03_pc_booking_documents.py
    ``_discover_requirement_for_callback``), not from anything this
    module writes -- so a correctly-populated requirement map at upload
    time is sufficient for the sync pipeline regardless of when (or
    whether) this module's own reconciliation has run yet.
  - Delivery's own requirement rows (``journey_document_requirements``)
    only exist today once Delivery has actually started (a DB trigger
    fires on that INSERT) -- but the DI webhook hard-requires a resolvable
    requirement for EVERY classified document, always. So this module
    seeds those rows eagerly, via ``auditcore.seed_delivery_document_
    requirements`` (migration 0092 -- the same body the start-Delivery
    trigger itself calls), *before* Delivery has "started" in the
    business sense (no journey_stage_states row, no flags) -- purely so
    Delivery's document types are legitimate, bindable classification
    candidates from the very first upload on a journey, whether or not
    Delivery is otherwise underway. Actually STARTING Delivery (the
    business-meaningful transition -- journey_stage_states row, the
    "Booking incomplete" flag if warranted) happens lazily, only once
    ``reconcile_unified_documents`` sees a document classify as something
    only Delivery's requirement set recognizes.
"""
from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import Connection, Engine, text

from audit_core.dependencies import get_connection, get_engine, get_human_principal
from audit_core.di_capture_v2_client import DiCaptureV2Client, DiCaptureV2Error
from audit_core.di_client import DiClient
from audit_core.errors import DependencyUnavailableError
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient
from audit_core.uc03_booking_capture import _scope
from audit_core.uc03_delivery_capture_v2 import _delivery_requirements
from audit_core.uc03_delivery_commands import _delivery_state, ensure_delivery_started
from audit_core.uc03_document_capture_v2 import (
    UploadIntentCommand,
    UploadIntentResponse,
    UploadIntentResult,
    _base_requirements,
    _candidate_type_keys,
    _canonical_document_type,
    _ensure_di_context,
    _human_actor_id,
    _log_di_capture_v2_failure,
    _requirement_refs_by_document_type_key,
    get_di_capture_v2_client,
    get_di_client,
    get_security_oauth_client,
)

router = APIRouter(
    prefix="/v2/tenants/{tenant_id}/journeys/{journey_id}/uc03/documents",
    tags=["uc03-unified-document-capture"],
)


def _seed_delivery_requirements(connection: Connection, *, tenant_id: str, journey_id: UUID) -> None:
    """Materialize Delivery's requirement catalog rows for this journey
    WITHOUT starting Delivery -- see module docstring. Idempotent (the
    underlying function is all ON CONFLICT DO NOTHING)."""
    connection.execute(
        text("SELECT auditcore.seed_delivery_document_requirements(:tenant_id, :journey_id)"),
        {"tenant_id": tenant_id, "journey_id": str(journey_id)},
    )


def _merged_candidate_requirements(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    _seed_delivery_requirements(connection, tenant_id=tenant_id, journey_id=journey_id)
    booking_requirements = _base_requirements(connection, tenant_id, journey_id)
    delivery_requirements = _delivery_requirements(connection, tenant_id, journey_id)
    return booking_requirements, delivery_requirements


@router.post("/upload-intents", response_model=UploadIntentResponse)
def create_unified_upload_intents(
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
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    # Deliberately no "capture phase open" gate (unlike Booking's own
    # upload-intents endpoint): this screen is also how a PC/TL adds a
    # document the process still needs at any later point, including after
    # Booking has been submitted -- matching Delivery's own upload-intents
    # endpoint, which already accepts uploads after submission.
    booking_requirements, delivery_requirements = _merged_candidate_requirements(
        connection, tenant_id=tenant_id, journey_id=journey_id
    )
    merged_requirements = booking_requirements + delivery_requirements
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
            candidate_document_type_keys=_candidate_type_keys(merged_requirements),
            requirement_refs_by_document_type_key=(
                _requirement_refs_by_document_type_key(merged_requirements)
            ),
            files=[item.model_dump() for item in command.files],
        )
    except DiCaptureV2Error as exc:
        _log_di_capture_v2_failure(
            operation="create_upload_intents", exc=exc, tenant_id=tenant_id,
            journey_id=journey_id, context_ref=context_ref,
        )
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
                uploadHeaders=item.get("uploadHeaders") or {},
                expiresAtUtc=item["expiresAtUtc"],
            )
        )
    return UploadIntentResponse(externalContextRef=context_ref, uploads=results)


def resolve_document_stage(
    classified_type: str | None,
    *,
    booking_requirements: list[dict[str, Any]],
    delivery_requirements: list[dict[str, Any]],
) -> tuple[str, str | None]:
    """(stage_code, requirement_key) for a classified document type.

    Exact match against each stage's own requirement set decides it --
    since migration 0091 retired the one type (bank_statement_extract)
    that used to be a candidate under both, this always resolves to
    exactly one stage today. An unrecognized type (matches neither --
    should not happen for anything DI was actually offered as a
    candidate) defaults to BOOKING, the stage that always exists.
    """
    if classified_type:
        canonical = _canonical_document_type(str(classified_type))
        for requirement in delivery_requirements:
            document_type_key = requirement.get("document_type_key")
            if document_type_key and _canonical_document_type(str(document_type_key)) == canonical:
                return "DELIVERY", str(requirement["requirement_key"])
        for requirement in booking_requirements:
            document_type_key = requirement.get("document_type_key")
            if document_type_key and _canonical_document_type(str(document_type_key)) == canonical:
                return "BOOKING", str(requirement["requirement_key"])
    return "BOOKING", None


def reconcile_unified_documents(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    actor_id: str,
    actor_role: str,
    correlation_id: str,
    v2_client: DiCaptureV2Client,
    context_ref: str,
    token: str,
) -> None:
    """Poll DI for every document on this journey, in EITHER phase, and
    write the correctly-dispatched stage_code/requirement_key back to
    document_capture_v2_documents -- so Booking's and Delivery's own,
    completely untouched GET endpoints each show the right documents
    afterward, purely by reading that column as they already do.
    """
    # Idempotent and cheap -- called here too (not just from
    # create_unified_upload_intents) so this function gives correct
    # results even if it's ever invoked on its own (e.g. a future resync).
    _seed_delivery_requirements(connection, tenant_id=tenant_id, journey_id=journey_id)
    booking_requirements = _base_requirements(connection, tenant_id, journey_id)
    delivery_requirements = _delivery_requirements(connection, tenant_id, journey_id)

    di_documents: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for phase in ("BOOKING", "DELIVERY"):
        try:
            payload = v2_client.list_documents(
                token=token, tenant_id=tenant_id, external_context_ref=context_ref, phase=phase,
            )
        except DiCaptureV2Error as exc:
            _log_di_capture_v2_failure(
                operation="list_documents", exc=exc, tenant_id=tenant_id,
                journey_id=journey_id, context_ref=context_ref,
            )
            continue
        for item in payload.get("documents") or []:
            document_id = str(item.get("documentId"))
            if document_id in seen_ids:
                continue
            seen_ids.add(document_id)
            di_documents.append(item)

    delivery_started = False
    for item in di_documents:
        document_id = UUID(str(item["documentId"]))
        classified_type = item.get("classifiedDocumentTypeKey")
        stage_code, requirement_key = resolve_document_stage(
            classified_type,
            booking_requirements=booking_requirements,
            delivery_requirements=delivery_requirements,
        )
        if stage_code == "DELIVERY" and not delivery_started:
            if _delivery_state(connection, tenant_id=tenant_id, journey_id=journey_id) is None:
                ensure_delivery_started(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    correlation_id=correlation_id,
                )
            delivery_started = True
        connection.execute(
            text(
                """
                UPDATE auditcore.document_capture_v2_documents
                SET capture_status=:capture_status,
                    classified_document_type_key=:classified_type,
                    requirement_key=:requirement_key,
                    stage_code=:stage_code,
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
                "stage_code": stage_code,
            },
        )


class ReconcileResponse(BaseModel):
    journeyId: UUID
    reconciled: bool = True


@router.post("/reconcile", response_model=ReconcileResponse)
def reconcile_unified_documents_endpoint(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
    v2_client: Annotated[DiCaptureV2Client, Depends(get_di_capture_v2_client)],
) -> ReconcileResponse:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    context_ref, token = _ensure_di_context(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
    )
    reconcile_unified_documents(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        actor_id=human_principal.subject,
        actor_role=context["operating_role"],
        correlation_id=get_correlation_id(request),
        v2_client=v2_client,
        context_ref=context_ref,
        token=token,
    )
    return ReconcileResponse(journeyId=journey_id)
