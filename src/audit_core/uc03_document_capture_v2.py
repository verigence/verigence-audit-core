from __future__ import annotations

import os
import threading
import time
from functools import lru_cache
from types import SimpleNamespace
from typing import Annotated, Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, Engine, text

from audit_core.dependencies import get_connection, get_engine, get_human_principal
from audit_core.di_capture_v2_client import DiCaptureV2Client, DiCaptureV2Error
from audit_core.di_client import DiClient
from audit_core.errors import ConflictError, NotFoundError
from audit_core.evidence import (
    _external_context_ref,
    _journey_context,
    _persist_subject_mapping,
    _subject_mapping,
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
)
from audit_core.uc03_delivery_documents import _resolve_known_applicability
from audit_core.uc03_pc_booking_documents import _is_repeatable_requirement
from audit_core.uc03_requirement_satisfaction import (
    linked_documents_for_journey,
    requirements_for_journey,
    resolve_requirement_satisfaction,
    unresolved_completion_blockers,
)

router = APIRouter(prefix="/v2/tenants/{tenant_id}/journeys/{journey_id}", tags=["uc03-document-capture-v2"])
_DI_AUDIENCE = "di"
logger = structlog.get_logger(__name__)


def _log_di_capture_v2_failure(
    *,
    operation: str,
    exc: DiCaptureV2Error,
    tenant_id: str,
    journey_id: UUID,
    context_ref: str,
) -> None:
    # DI's real status/detail never reach Web (VAC-SYS-002's message is
    # deliberately generic) — log them here so a Railway log search is enough
    # to diagnose a live failure instead of only "dependency unavailable".
    logger.warning(
        "di_capture_v2_request_failed",
        operation=operation,
        di_status_code=exc.status_code,
        di_detail=exc.detail,
        tenant_id=tenant_id,
        journey_id=str(journey_id),
        external_context_ref=context_ref,
    )


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


class UploadIntentFailure(BaseModel):
    clientUploadId: str
    errorCode: str
    detail: str


class UploadIntentResponse(BaseModel):
    externalContextRef: str
    uploads: list[UploadIntentResult]
    # DI isolates each file in a batch on its own SAVEPOINT -- a problem with
    # one file no longer fails every other file in the same request. Always
    # present; empty when every file in the batch succeeded.
    failures: list[UploadIntentFailure] = Field(default_factory=list)


def _upload_intent_failures(payload: dict[str, Any]) -> list[UploadIntentFailure]:
    """Map DI's per-file failures (create_upload_intents) to our own model.

    Shared by all three upload-intents routes (Booking's own, Delivery's own,
    the unified path) so a PC always sees the same shape regardless of which
    screen they uploaded from.
    """
    return [
        UploadIntentFailure(
            clientUploadId=str(failure["clientUploadId"]),
            errorCode=str(failure.get("errorCode") or "UPLOAD_INTENT_FAILED"),
            detail=str(failure.get("detail") or "This file could not be uploaded."),
        )
        for failure in payload.get("failures") or []
    ]


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


@lru_cache
def get_security_oauth_client() -> SecurityOAuthClient:
    base_url = os.environ.get("SECURITY_BASE_URL", "").strip()
    client_id = os.environ.get("SECURITY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SECURITY_CLIENT_SECRET", "")
    if not base_url or not client_id or not client_secret:
        raise RuntimeError("Security ServiceIntegration is not configured")
    return SecurityOAuthClient(
        base_url=base_url,
        client_id=client_id,
        client_secret=client_secret,
    )


@lru_cache
def get_di_client() -> DiClient:
    base_url = os.environ.get("DI_BASE_URL", "").strip()
    if not base_url:
        raise RuntimeError("DI integration is not configured")
    return DiClient(base_url=base_url)


@lru_cache
def get_di_capture_v2_client() -> DiCaptureV2Client:
    base_url = os.environ.get("DI_BASE_URL", "").strip()
    if not base_url:
        raise RuntimeError("DI integration is not configured")
    return DiCaptureV2Client(base_url=base_url)


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
    """Delete-only lock: a document may be deleted until its own stage is
    marked complete, then it's locked -- the one genuinely stage-based rule
    that survives (protecting already-audited evidence from removal after a
    decision was made on it), matching uc03_delivery_capture_v2's identical
    check on delete_delivery_document_v2. Upload/finalize/classify are never
    gated by this -- those actions are accepted at any time regardless of
    completion state (see _authorize_booking's own docstring).
    """
    state = _capture_phase_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    if state["capture_completed_at_utc"] is not None:
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Booking document capture is complete",
            detail="Documents cannot be deleted after Booking has been submitted.",
        )


def _authorize_booking(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    human_principal: HumanPrincipal,
    authorization_client: SecurityAuthorizationClient,
) -> dict[str, Any]:
    """Deliberately does NOT call _require_active_booking. Every upload/
    finalize/classify/read action on Booking documents is accepted at any
    time, regardless of Booking's own business_status -- the same
    unconditional treatment uc03_delivery_capture_v2._authorize_delivery
    already gives Delivery. This used to be a separate carve-out
    (_authorize_booking_for_resync) needed only because a resync on a
    Journey that had progressed to Delivery would otherwise be rejected by
    this same check; once no action is stage-gated, resync needs no
    carve-out of its own and calls this directly. Still requires the
    Booking stage to exist at all (via _capture_phase_state's own
    NotFoundError).
    """
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    return dict(
        _capture_phase_state(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            for_update=False,
        )
    )


def _base_requirements(connection: Connection, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """Thin delegation to the one shared, stage-parametrized query -- was a
    full copy of uc03_delivery_capture_v2._delivery_requirements with only
    the 'BOOKING'/'DELIVERY' literal differing. See
    uc03_requirement_satisfaction.requirements_for_journey.
    """
    return requirements_for_journey(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING",
    )


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


def _extracted_document_ids(connection: Connection, tenant_id: str, journey_id: UUID) -> set[str]:
    """Documents that have actually produced at least one extracted field --
    the read-only local capture responses (_build_local_capture_response /
    _build_local_delivery_capture_response) never call DI live for
    performance, so they can't ask DI's own processing status. This is the
    DB-only equivalent: a row here only ever exists once DI extraction has
    genuinely completed for that document (see uc03_journey_reviewed_details,
    which reads the same table). Used to tell the checklist card apart from
    "Classified" (DI knows what it is, nothing extracted from it yet) --
    direct user correction (2026-09-24): the checklist previously hardcoded
    processingStatus=None for every document, so a fully reviewed document
    could never show anything but "Classified", forever.
    """
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT di_document_id
            FROM auditcore.journey_document_extracted_fields
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    )
    return {str(row[0]) for row in rows}


def _linked_documents(connection: Connection, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """Thin delegation to the one shared, stage-parametrized query -- was a
    full copy of uc03_delivery_capture_v2._linked_delivery_documents with
    only the 'BOOKING'/'DELIVERY' literal differing. See
    uc03_requirement_satisfaction.linked_documents_for_journey.
    """
    return linked_documents_for_journey(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING",
    )


# _ensure_di_context is called on every Booking/Delivery capture read (once
# documents exist), every upload-intents call, every Review-page DI fetch and
# work-item enrichment pass -- shared by uc03_document_capture_v2.py,
# uc03_delivery_capture_v2.py, uc03_document_review_v2.py, uc03_work_item_
# enrichment.py and (via review_v2._ensure_di_context) uc03_delivery_review_
# confirm.py. Its ensure_audit_storage_context() PUT is idempotent at DI (an
# Idempotency-Key is already passed) but was previously re-issued on every
# single call regardless -- a second full DI round trip (up to a 15s budget)
# stacked in front of whatever the caller actually wanted, repeated roughly
# every 5s for up to 2 minutes after every upload while extraction is
# pending. Reusing the underlying (dealer/outlet/customer) context info for a
# journey doesn't change between calls in that window, so a short in-process
# cache skips the redundant PUT without weakening the guarantee -- worst
# case on a cache miss (process restart, cache eviction) is exactly today's
# behavior, never a correctness issue.
_DI_CONTEXT_ENSURE_REUSE_SECONDS = 300.0
_di_context_ensured_until: dict[tuple[str, str], float] = {}
_di_context_ensured_lock = threading.Lock()


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

    cache_key = (tenant_id, context_ref)
    now = time.monotonic()
    with _di_context_ensured_lock:
        already_ensured = _di_context_ensured_until.get(cache_key, 0.0) > now
    if not already_ensured:
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
        with _di_context_ensured_lock:
            _di_context_ensured_until[cache_key] = now + _DI_CONTEXT_ENSURE_REUSE_SECONDS
    return context_ref, token


# Audit Core's capture-requirement master carries the legacy key ``booking_docket``
# for the Booking Form / OTF, but DI's extraction schema, the DI V2 classifier and
# Core's own reviewed-value materialisation (``_BOOKING_FORM_DOCUMENT_TYPE``) all use
# ``booking_form``.  Send/accept the canonical key so the sales contract is extracted
# against the real Booking Form field set instead of the generic fallback schema.
#
# payment_receipt -> dealer_receipt: the same real-world document (a
# dealership money receipt) registered under two different document_type_
# keys purely because Booking's and Delivery's default requirement
# profiles were authored independently (dealer_receipt.py and payment_
# receipt.py are near-identical DI schemas -- see payment_receipt.py's own
# docstring). Direct user correction (2026-09-23): asking DI to visually
# tell these apart is unreliable by construction -- nothing distinguishes
# an advance receipt from a balance receipt except which stage it's
# for, which is exactly the fact classification can't see. Canonicalizing
# removes the ambiguous choice from DI's classifier entirely; which
# stage's requirement a canonicalized receipt actually binds to is decided
# separately, by journey state (see resolve_document_stage and
# _requirement_ref_for_open_requirements below), not by content.
_DOCUMENT_TYPE_ALIASES: dict[str, str] = {
    "booking_docket": "booking_form",
    "payment_receipt": "dealer_receipt",
}


def _canonical_document_type(key: str) -> str:
    return _DOCUMENT_TYPE_ALIASES.get(key, key)


def _candidate_type_keys(requirements: list[dict[str, Any]]) -> list[str]:
    return list(
        dict.fromkeys(
            _canonical_document_type(str(row["document_type_key"]))
            for row in requirements
            if row.get("document_type_key")
        )
    )


def _requirement_refs_by_document_type_key(
    requirements: list[dict[str, Any]],
) -> dict[str, str]:
    """Map DI classification keys to the exact pinned Audit Core requirement.

    DI does not know the document type until classification.  Persisting this map
    with the upload lets DI bind the accepted type to the correct requirement before
    extraction is queued, so the existing DI -> Audit Core evidence callback can run
    both before and after Booking submission.

    This feeds DI's create_upload_intents call alongside candidate_document_type_keys
    (built by _candidate_type_keys, which canonicalizes every key), and DI rejects the
    whole request with INVALID_REQUEST if this map's keys are not a subset of the
    candidate list. Emit ONLY the canonical key per row -- e.g. requirement rows still
    carrying the legacy document_type_key='booking_docket' (see _DOCUMENT_TYPE_ALIASES)
    must map under 'booking_form', the key DI actually sees as a candidate, or every
    Booking upload attempt fails this validation (found live: 'Requirement-ref mapping
    contains a non-candidate document type.'). _reconcile_documents binds an accepted
    classification of either key back to the requirement separately -- that direction
    is unaffected by this fix.
    """

    result: dict[str, str] = {}
    for row in requirements:
        document_type_key = row.get("document_type_key")
        requirement_ref = row.get("requirement_ref")
        if document_type_key and requirement_ref:
            result.setdefault(_canonical_document_type(str(document_type_key)), str(requirement_ref))
    return result


def _requirements_with_open_slot(
    connection: Connection, *, tenant_id: str, journey_id: UUID, requirements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Requirements DI should still be told to extract a NEW document
    against. A repeatable requirement (multiple payment receipts, bank
    statements, scrappage certificates -- see _is_repeatable_requirement)
    always has an open slot; a single-document requirement that already has
    an ACTIVE evidence link does not -- a further upload of that same type
    is an extra copy, not a new fact, and extracting it wastes DI's own
    processing for data that will never be used.

    Deliberately NOT used to build candidate_document_type_keys (what DI is
    allowed to classify the file as) -- that stays on the full, unfiltered
    requirements list, so a duplicate copy still gets correctly classified
    as whatever it actually is. Only requirement_refs_by_document_type_key
    reads this filtered list, so a duplicate simply gets no requirement_ref
    -- DI's own extraction gate (create_initial_job's requirement_ref
    check) skips queuing extraction for it on that basis alone.
    """
    fulfilled_refs = {
        str(row[0])
        for row in connection.execute(
            text(
                """
                SELECT DISTINCT journey_document_requirement_id
                FROM auditcore.evidence
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND association_status='ACTIVE'
                  AND journey_document_requirement_id IS NOT NULL
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        )
    }
    return [
        row for row in requirements
        if _is_repeatable_requirement(row.get("requirement_key"))
        or str(row.get("requirement_ref")) not in fulfilled_refs
    ]


def _reconcile_documents(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    requirements: list[dict[str, Any]],  # kept for signature stability; see below -- unused now
    di_documents: list[dict[str, Any]],
) -> None:
    """Direct user directive (2026-09-23): "we should allow PC to upload
    documents without giving him pain to select the stage" -- a document
    the unified capture screen (uc03_unified_document_capture.py) always
    files with DI under phase="BOOKING", permanently (DI never re-files a
    document into another phase), so this Booking screen's own poll is the
    ONLY place that ever sees a unified-flow document in DI's response at
    all, regardless of which stage it actually belongs to. It used to only
    match a classified type against Booking's own requirements (the
    ``requirements`` param, now unused -- kept in the signature so its two
    callers need no change) and silently null the link forever whenever a
    type actually belonged to Delivery. Delegates to
    apply_di_classification instead, which resolves the correct stage
    itself (booking_requirements AND delivery_requirements, freshly read)
    and relocates the row -- so a document classifies once, from wherever
    a PC happens to have the page open, no stage picker, ever.

    Delivery's own poll (_reconcile_delivery_documents) does NOT need this
    same change: it queries DI with phase="DELIVERY", which never returns
    a unified-flow document in the first place (they're always filed under
    "BOOKING"), so it could never be the one to notice a misplacement here.
    """
    from audit_core.uc03_unified_document_capture import (
        _MACHINE_ACTOR,
        apply_di_classification,
    )

    apply_di_classification(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        di_documents=di_documents,
        actor_id=_MACHINE_ACTOR,
        actor_role="SYSTEM",
        correlation_id="",
    )


def _ensure_evidence_link_for_resync(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    requirement_ref: Any,
    document_id: UUID,
    service_id: str,
) -> bool:
    """Resync's own filter only re-runs _sync_booking_document for a document
    that ALREADY has an ACTIVE ``evidence`` row -- that row is normally
    created by DI's async "link" callback (acknowledge_booking_document_link),
    a separate mechanism from document_capture_v2_documents.capture_status,
    which _reconcile_documents/_reconcile_delivery_documents refresh above.

    Confirmed live gap: DI's list_documents can report a document CLASSIFIED
    (updating capture_status correctly) while the one-time "link" callback
    for that same document never landed at Audit Core -- dropped during an
    earlier incident, or simply never retried, since DI considers its own
    delivery successful once it gets any 2xx and has no other reason to
    re-send it. In that case _sync_booking_document's very first check
    (``link is None ... return 0``) silently no-ops forever: resync reports
    a document as "resynced" (the background task ran without error) while
    genuinely doing nothing, no matter how many times it's called, because
    nothing else ever creates the missing evidence row. This is the leading,
    well-evidenced explanation for "resync says done, but no payments/
    insurance/etc ever land in the canonical tables."

    Closes that gap by running the exact same idempotent link logic the
    webhook itself uses (uc03_pc_booking_documents.acknowledge_booking_
    document_link) whenever an ACTIVE evidence row is not already present
    for this document -- safe to call redundantly (that function's own
    upsert logic is what the live webhook already relies on for replay
    safety). A ConflictError/NotFoundError here (the requirement is not
    currently applicable, or belongs to a different journey than expected)
    means this specific document genuinely cannot be linked yet -- skip it
    rather than fail the whole resync.
    """
    existing = connection.execute(
        text(
            """
            SELECT association_status
            FROM auditcore.evidence
            WHERE tenant_id=:tenant_id AND di_document_id=:document_id
            """
        ),
        {"tenant_id": tenant_id, "document_id": document_id},
    ).mappings().one_or_none()
    if existing is not None and str(existing["association_status"]) == "ACTIVE":
        return True

    from audit_core.uc03_pc_booking_documents import (
        BookingDocumentLinkCommand,
        acknowledge_booking_document_link,
    )

    try:
        acknowledge_booking_document_link(
            payload=BookingDocumentLinkCommand(
                requirementRef=requirement_ref,
                documentId=document_id,
            ),
            service_principal=SimpleNamespace(subject=service_id),
            connection=connection,
        )
        return True
    except (ConflictError, NotFoundError) as exc:
        logger.warning(
            "uc03_resync_evidence_link_backfill_skipped",
            tenant_id=tenant_id,
            journey_id=str(journey_id),
            document_id=str(document_id),
            requirement_ref=str(requirement_ref),
            error=str(exc),
        )
        return False


def _backfill_evidence_links_for_resync(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[dict[str, Any]],
    document_ids: list[UUID],
    requirements: list[dict[str, Any]],
    service_id: str,
) -> None:
    """Run _ensure_evidence_link_for_resync for every document about to be
    resynced. Kept as one small helper (shared verbatim by both Booking's
    and Delivery's resync endpoints) so the two stages can't drift the way
    the receipt-document-type constant once did.
    """
    requirement_ref_by_key = {
        str(row["requirement_key"]): row["requirement_ref"]
        for row in requirements
        if row.get("requirement_ref") is not None
    }
    by_document_id = {row["di_document_id"]: row for row in documents}
    for document_id in document_ids:
        row = by_document_id.get(document_id)
        requirement_ref = (
            requirement_ref_by_key.get(str(row.get("requirement_key")))
            if row is not None
            else None
        )
        if requirement_ref is None:
            continue
        _ensure_evidence_link_for_resync(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            requirement_ref=requirement_ref,
            document_id=document_id,
            service_id=service_id,
        )


def _build_capture_response(
    *,
    journey_id: UUID,
    context_ref: str,
    requirements: list[dict[str, Any]],
    declaration_rows: dict[str, dict[str, Any]],
    audit_documents: list[dict[str, Any]],
    di_documents: list[dict[str, Any]],
    fallback_extracted_ids: frozenset[str] = frozenset(),
) -> BookingCaptureV2Response:
    di_by_id = {str(item["documentId"]): item for item in di_documents}
    active_by_requirement: dict[str, dict[str, Any]] = {}
    uploads: list[CaptureV2Document] = []

    for link in audit_documents:
        di = di_by_id.get(str(link["di_document_id"]))
        if di is None:
            # See uc03_delivery_capture_v2._build_delivery_capture_response's
            # matching comment -- the unified capture screen has no stage
            # picker, so a document can be reclassified by audit-core's own
            # reconciliation into a stage_code DI never updates its own
            # upload-time phase for. Fall back to this journey's own row
            # instead of silently dropping it from the checklist.
            di = {
                "documentId": str(link["di_document_id"]),
                "clientUploadId": str(link["client_upload_id"]),
                "state": str(link["capture_status"]),
                "classifiedDocumentTypeKey": link.get("classified_document_type_key"),
                "originalFilename": str(link["original_filename"]),
                "contentUrl": None,
                "processingStatus": (
                    "PROCESSED"
                    if str(link["di_document_id"]) in fallback_extracted_ids
                    else None
                ),
            }
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
    connection: Connection,
    tenant_id: str,
    journey_id: UUID,
    requirements: list[dict[str, Any]],
    declaration_rows: dict[str, dict[str, Any]],
    audit_documents: list[dict[str, Any]],
) -> BookingCaptureV2Response:
    extracted_ids = _extracted_document_ids(connection, tenant_id, journey_id)
    di_documents = [
        {
            "documentId": str(row["di_document_id"]),
            "clientUploadId": str(row["client_upload_id"]),
            "state": str(row["capture_status"]),
            "classifiedDocumentTypeKey": row.get("classified_document_type_key"),
            "originalFilename": str(row["original_filename"]),
            "contentUrl": None,
            "processingStatus": "PROCESSED" if str(row["di_document_id"]) in extracted_ids else None,
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


# GET /booking/capture was removed (Phase 4 unification) -- replaced by
# uc03_unified_document_capture.get_unified_capture, which merges DI's two
# phases into one call instead of this endpoint's single-phase list_documents
# (see that module's own section docstring for why the single-phase version
# was the root cause of a relocated document showing "Missing"/losing its
# contentUrl). _read_capture is gone with it -- nothing else called it.


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

        # Fresh-resolve CONDITIONAL applicability before gating: a
        # requirement whose deciding document never arrives (so the DI
        # webhook's own resolve_requirement_applicability_if_conditional
        # never fires for it) would otherwise sit at requirement_status=
        # PENDING forever even though the answer is already knowable from
        # commercial-line/trade-in facts (_resolve_condition). Booking's
        # checklist read never triggered this recompute the way Delivery's
        # legacy list_delivery_documents does -- doing it here, once, right
        # before the one-shot completion decision, closes that gap without
        # adding a recompute to every checklist read.
        _resolve_known_applicability(
            connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING",
        )
        satisfaction = resolve_requirement_satisfaction(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code="BOOKING",
            requirements=_base_requirements(connection, tenant_id, journey_id),
            documents=_linked_documents(connection, tenant_id, journey_id),
        )
        blockers = unresolved_completion_blockers(satisfaction)
        if blockers:
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Booking document capture is incomplete",
                detail=(
                    "Required documents are still missing or unclassified: "
                    + ", ".join(sorted(b.requirement_key for b in blockers))
                ),
            )
        tentative_sku = connection.execute(
            text(
                """
                SELECT 1 FROM auditcore.journey_products
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND selection_status='TENTATIVE'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one_or_none()
        if tentative_sku is not None:
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Booking document capture is incomplete",
                detail="Vehicle model/SKU selection is still ambiguous and needs Team Lead confirmation.",
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


# POST /booking/upload-intents, POST /booking/documents/{id}/finalize and
# DELETE /booking/documents/{id} were removed (Phase 4 unification). The
# first two had zero live callers already (the frontend's own
# uploadBookingCaptureV2Files/uc03DocumentCaptureV2.ts was dead code -- every
# real upload already went through the unified create_unified_upload_intents/
# finalize_unified_document above them in this same codebase). DELETE is
# replaced by uc03_unified_document_capture.delete_unified_document, which
# fixes the two bugs this copy shared with uc03_delivery_capture_v2's own
# (canDelete never matching this endpoint's own completion lock; DI attempted
# before audit-core, so a document DI never durably received could never be
# removed) -- see that module's own section docstring.


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
    requirements = _base_requirements(connection, tenant_id, journey_id)
    allowed = {
        str(row["condition_key"])
        for row in requirements
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
    return _build_local_capture_response(
        connection=connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        requirements=requirements,
        declaration_rows=_declarations(connection, tenant_id, journey_id),
        audit_documents=_linked_documents(connection, tenant_id, journey_id),
    )


# POST /booking/resync was removed (Phase 4 unification) -- replaced by
# uc03_unified_document_capture.resync_unified_documents, which covers both
# stages in one call. This copy and uc03_delivery_capture_v2.resync_delivery_
# capture_v2 were already both delegating reclassification to
# reconcile_unified_documents and were otherwise byte-identical except the
# stage_code literal and which linked-documents getter they called.
