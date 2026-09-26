"""uc03_unified_document_capture.py — one upload screen, Booking and
Delivery alike (2026-09-13).

Deliberately additive: neither of the existing Booking/Delivery capture
screens, their own upload-intent endpoints, nor their own document tables
are touched. This module is a new, parallel path the unified Journey
Documents page uses instead -- "keep existing functionality intact" per
explicit instruction.

Direct user directive (2026-09-23): "we should allow PC to upload
documents without giving him pain to select the stage." One exception to
"deliberately additive" above, made for exactly this reason: Booking's and
Delivery's own reconciliation functions (``_reconcile_documents`` /
``_reconcile_delivery_documents``, uc03_document_capture_v2.py /
uc03_delivery_capture_v2.py) now delegate to ``apply_di_classification``
below instead of each only managing their own stage's rows -- see that
function's own docstring for why. Their upload-intent endpoints, requirement
catalogs, and UI stay untouched; only how a misclassified document gets
noticed and relocated changed.

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

from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel
from sqlalchemy import Connection, Engine, text

from audit_core.dependencies import get_connection, get_engine, get_human_principal
from audit_core.di_capture_v2_client import DiCaptureV2Client, DiCaptureV2Error
from audit_core.di_client import DiClient
from audit_core.errors import ConflictError, DependencyUnavailableError
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
    CaptureV2Document,
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
    _requirements_with_open_slot,
    _upload_intent_failures,
    get_di_capture_v2_client,
    get_di_client,
    get_security_oauth_client,
)
from audit_core.uc03_requirement_satisfaction import linked_documents_for_journey

router = APIRouter(
    prefix="/v2/tenants/{tenant_id}/journeys/{journey_id}/uc03/documents",
    tags=["uc03-unified-document-capture"],
)
logger = structlog.get_logger(__name__)


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
    # Full (unfiltered) union here -- classification must still recognize a
    # duplicate copy for what it actually is, and a type is a legitimate
    # candidate at all if EITHER stage's catalog registers it, even if the
    # requirement_ref binding below (correctly) won't come from that row.
    full_type_universe = booking_requirements + delivery_requirements
    # requirement_ref is what DI reports back on classification, and the
    # document-link webhook (uc03_pc_booking_documents._discover_requirement_
    # for_callback) resolves BOOKING/DELIVERY from that row's own process_area
    # -- straight off the DB, at upload time, before this module's own
    # reconciliation ever runs. So which row wins here IS the live,
    # automatic-sync stage decision, not a cosmetic ordering choice: a
    # requirement row can only supply a ref for the stage _stage_for_type
    # actually assigns its canonical type to (see that function's own
    # comment) -- never "whichever stage's catalog happened to register it
    # first", which is the exact per-catalog dependency this module's
    # reconciliation was fixed to not use.
    receipt_defaults_to_delivery = _receipt_defaults_to_delivery(
        connection, tenant_id=tenant_id, journey_id=journey_id,
    )
    stage_owned_requirements = _requirements_owned_by_stage(
        booking_requirements, "BOOKING", receipt_defaults_to_delivery=receipt_defaults_to_delivery,
    ) + _requirements_owned_by_stage(
        delivery_requirements, "DELIVERY", receipt_defaults_to_delivery=receipt_defaults_to_delivery,
    )
    open_requirements = _requirements_with_open_slot(
        connection, tenant_id=tenant_id, journey_id=journey_id, requirements=stage_owned_requirements,
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
        payload = v2_client.create_upload_intents(
            token=token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            phase="BOOKING",
            candidate_document_type_keys=_candidate_type_keys(full_type_universe),
            # Filtered to each type's static-list-correct stage AND to an
            # open requirement slot -- only that ref gates DI's own
            # extraction (see _requirements_with_open_slot's own docstring).
            requirement_refs_by_document_type_key=(
                _requirement_refs_by_document_type_key(open_requirements)
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
    return UploadIntentResponse(
        externalContextRef=context_ref, uploads=results, failures=_upload_intent_failures(payload)
    )


class FinalizeResponse(BaseModel):
    documentId: UUID
    state: str


@router.post("/{document_id}/finalize", response_model=FinalizeResponse)
def finalize_unified_document(
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
    _scope(
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
        raise DependencyUnavailableError(detail="Uploaded document could not be finalized.") from exc
    return FinalizeResponse(documentId=document_id, state=str(payload["state"]))


# Direct user correction (2026-09-23): "tagging document to stage won't
# work for us" -- matching a classified type against whichever journey_
# document_requirements rows happen to exist made stage depend on
# requirement-catalog setup and DI's own classification timing (the actual
# cause of every stage-misrouting bug found this session: gate_pass,
# accessory_invoice_dms, dealer_receipt/payment_receipt). Booking's own
# document set is small and fixed; everything else defaults to Delivery.
# No per-journey matching needed to decide this any more.
_BOOKING_ONLY_DOCUMENT_TYPES = frozenset({
    "aadhaar",
    "pan_card",
    "booking_form",  # booking_docket canonicalizes to this
    "minimum_booking_payment_proof",
    "customer_kyc",  # one per journey, Booking-side -- never duplicated to Delivery
})
# The one genuine exception: a receipt legitimately happens at both stages
# (installments toward the minimum booking amount, then further payments at
# Delivery) -- resolved by running total, not type membership, in
# _receipt_defaults_to_delivery below.
_RECEIPT_CANONICAL_TYPE = "dealer_receipt"


def _stage_for_type(canonical: str, *, receipt_defaults_to_delivery: bool) -> str:
    """Pure type->stage decision, shared by resolve_document_stage (below,
    at classification/reconciliation time) and create_unified_upload_intents
    (at upload time, before classification exists) -- one definition, so a
    document's upload-time requirement_ref binding and its later
    reconciliation never disagree about which stage it belongs to."""
    if canonical == _RECEIPT_CANONICAL_TYPE:
        return "DELIVERY" if receipt_defaults_to_delivery else "BOOKING"
    if canonical in _BOOKING_ONLY_DOCUMENT_TYPES:
        return "BOOKING"
    return "DELIVERY"


def _requirements_owned_by_stage(
    requirements: list[dict[str, Any]], stage: str, *, receipt_defaults_to_delivery: bool,
) -> list[dict[str, Any]]:
    """Keep only the rows whose canonical type _stage_for_type actually
    assigns to ``stage`` -- never "this row exists in the catalog under
    this process_area", which is the exact per-catalog dependency
    resolve_document_stage was fixed to not use (see _BOOKING_ONLY_
    DOCUMENT_TYPES' own comment). Used at upload time
    (create_unified_upload_intents) to decide which row supplies a
    requirement_ref -- the live, automatic-sync stage decision (see that
    function's own comment) -- so a row a tenant's requirement profile
    mistakenly registers under the wrong process_area is never used to
    bind one."""
    return [
        requirement
        for requirement in requirements
        if requirement.get("document_type_key")
        and _stage_for_type(
            _canonical_document_type(str(requirement["document_type_key"])),
            receipt_defaults_to_delivery=receipt_defaults_to_delivery,
        ) == stage
    ]


def _receipt_defaults_to_delivery(
    connection: Connection, *, tenant_id: str, journey_id: UUID, document_id: UUID | None = None,
) -> bool:
    """True once this journey's already-extracted receipts (any stage,
    canonicalized -- see uc03_duplicate_receipt_detection._receipt_documents)
    sum to at least the tenant's minimum booking amount: a receipt beyond
    that point is no longer proving the booking payment, so it defaults to
    Delivery.

    Reported live (2026-09-24): a single receipt whose OWN amount alone
    met the minimum showed as "Missing" on Booking's own checklist forever
    after. A receipt's own amount is only known after extraction, so at
    the moment it is FIRST classified it can never appear in "already-
    extracted receipts" -- but apply_di_classification re-resolves every
    already-classified document's stage on every subsequent poll too
    (self-correcting a receipt that arrives out of order), and by then
    that receipt's own extracted amount IS in the total, so it could tip
    itself over the threshold and relocate itself to Delivery, orphaning
    the Booking requirement it had already correctly fulfilled.

    document_id (when given -- re-resolving an ALREADY-extracted receipt,
    not a brand-new upload) excludes that receipt from its own check by
    only summing the receipts that come BEFORE it in receipt-date order
    (ties broken by document_id for a stable order) -- mirroring
    evaluate_minimum_booking_payment's own date-ordering, for the same
    "a backdated receipt can shift the boundary" reason. The receipt that
    itself completes the minimum still belongs to Booking; only ones whose
    position falls after that point are further payments. Without
    document_id (upload time, before this document has any row at all),
    every already-extracted receipt unconditionally precedes it, so this
    collapses to a plain unconditional sum.
    """
    from audit_core.uc03_booking_confirmation_rules import _minimum_booking_amount
    from audit_core.uc03_duplicate_receipt_detection import _receipt_documents

    minimum = _minimum_booking_amount(connection, tenant_id=tenant_id)
    records = [
        r for r in _receipt_documents(connection, tenant_id=tenant_id, journey_id=journey_id)
        if r.amount is not None
    ]
    if document_id is None:
        return sum((r.amount for r in records), Decimal(0)) >= minimum

    records.sort(key=lambda r: (r.receipt_date or "9999-99-99", str(r.document_id)))
    running = Decimal(0)
    for record in records:
        if record.document_id == document_id:
            return running >= minimum
        running += record.amount
    # This document's own extracted amount isn't in the durable set at all
    # (shouldn't happen once document_id is passed, but stay conservative
    # rather than raise) -- treat it like a brand-new, not-yet-extracted one.
    return sum((r.amount for r in records), Decimal(0)) >= minimum


def resolve_document_stage(
    connection: Connection,
    classified_type: str | None,
    *,
    tenant_id: str,
    journey_id: UUID,
    booking_requirements: list[dict[str, Any]],
    delivery_requirements: list[dict[str, Any]],
    document_id: UUID | None = None,
) -> tuple[str, str | None]:
    """(stage_code, requirement_key) for a classified document type.

    Stage is a static property of the type itself (_BOOKING_ONLY_DOCUMENT_
    TYPES, else Delivery), not resolved by matching against whichever
    journey_document_requirements rows happen to exist -- see that
    constant's own comment. The one exception (a canonicalized receipt) is
    resolved by running total instead, see _receipt_defaults_to_delivery.

    Once stage is decided, the matching row in that stage's own requirement
    list (if any -- a tenant need not require every type) supplies the
    specific requirement_key.

    An unrecognized type (no classification at all) defaults to BOOKING,
    the stage that always exists.

    document_id identifies the specific document being (re-)resolved --
    passed through to _receipt_defaults_to_delivery so an already-extracted
    receipt is never checked against a running total that includes its own
    amount (see that function's own docstring for the live bug this fixes).
    """
    if not classified_type:
        return "BOOKING", None
    canonical = _canonical_document_type(str(classified_type))
    receipt_defaults_to_delivery = (
        _receipt_defaults_to_delivery(
            connection, tenant_id=tenant_id, journey_id=journey_id, document_id=document_id,
        )
        if canonical == _RECEIPT_CANONICAL_TYPE
        else False
    )
    stage = _stage_for_type(canonical, receipt_defaults_to_delivery=receipt_defaults_to_delivery)

    requirements = booking_requirements if stage == "BOOKING" else delivery_requirements
    for requirement in requirements:
        document_type_key = requirement.get("document_type_key")
        if document_type_key and _canonical_document_type(str(document_type_key)) == canonical:
            return stage, str(requirement["requirement_key"])
    return stage, None


def _correct_durable_store_stage(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    stage_code: str,
) -> bool:
    """Re-tag this document's durably-stored extracted fields to the
    now-known-correct stage, if they were ever written under a different
    one. Returns True when a correction was actually made.

    journey_document_extracted_fields.stage_code is written once, at
    whatever moment a document's facts were first durably copied in (the DI
    document-link webhook, uc03_confidence_review_policy._sync_booking_
    document) -- unlike document_capture_v2_documents.stage_code (this
    module's own checklist column, corrected above on every reconcile),
    nothing ever revisited this one. The Delivery/Booking materializers
    that read canonical insurance/registration/finance/commercial-lines
    facts key their durable-store read entirely on this column
    (_documents_from_durable_store, WHERE stage_code=:stage_code) -- so a
    document whose fact-write ever landed under the wrong stage stayed
    permanently invisible to materialization, with its checklist entry
    showing the correct stage the whole time, and Resync never fixing it
    since Resync's own re-sync reads this exact same never-corrected
    column.

    The guard against re-tagging into a row that already exists under the
    target stage (rather than blindly UPDATEing) matters because stage_code
    is part of journey_document_extracted_fields' own partial unique index
    (tenant_id, journey_id, stage_code, di_document_id,
    source_canonical_field_id, source_fact_version WHERE
    source_canonical_field_id IS NOT NULL, uc03_di_core_persistence.py's
    _V2_UPSERT) -- a later, correctly-tagged webhook redelivery could have
    already written the right row independently, and blindly retagging the
    stale one into the same identity would violate that index.
    """
    result = connection.execute(
        text(
            """
            UPDATE auditcore.journey_document_extracted_fields AS target
            SET stage_code = :stage_code, updated_at_utc = now()
            WHERE target.tenant_id = :tenant_id
              AND target.journey_id = :journey_id
              AND target.di_document_id = :document_id
              AND target.stage_code <> :stage_code
              AND NOT EXISTS (
                  SELECT 1 FROM auditcore.journey_document_extracted_fields other
                  WHERE other.tenant_id = target.tenant_id
                    AND other.journey_id = target.journey_id
                    AND other.di_document_id = target.di_document_id
                    AND other.stage_code = :stage_code
                    AND other.source_canonical_field_id
                        IS NOT DISTINCT FROM target.source_canonical_field_id
                    AND other.source_fact_version = target.source_fact_version
              )
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": document_id,
            "stage_code": stage_code,
        },
    )
    return result.rowcount > 0


_MACHINE_ACTOR = "SYSTEM:DI_AUTO"


def apply_di_classification(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    di_documents: list[dict[str, Any]],
    actor_id: str,
    actor_role: str,
    correlation_id: str,
) -> None:
    """Apply DI's classification result to document_capture_v2_documents for
    every item in di_documents, relocating stage_code/requirement_key to
    wherever resolve_document_stage says the type actually belongs --
    regardless of which single phase's listing di_documents came from. A
    document's classified type alone decides its stage; nothing here needs
    a listing from DI's other phase.

    Direct user directive (2026-09-23): "we should allow PC to upload
    documents without giving him pain to select the stage" -- this is the
    piece that actually makes that seamless. Before this, only
    reconcile_unified_documents itself (a full, both-phase DI listing,
    effectively only run right after upload or via an explicit Resync)
    ever relocated a misplaced document. Booking's own and Delivery's own
    everyday polling (_reconcile_documents / _reconcile_delivery_documents,
    uc03_document_capture_v2.py / uc03_delivery_capture_v2.py) each already
    fetch a live classification result on every read, but used to only
    refresh requirement_key within their OWN stage's rows -- silently
    nulling it forever, never relocating, whenever the type actually
    belonged to the other stage. Both now call this same function with
    their own single-phase di_documents, so a misplaced document self-
    heals within one poll cycle, from whichever screen a PC happens to
    have open, with no dependency on the full two-phase reconcile ever
    running again.
    """
    # Idempotent and cheap -- called from every caller below (not just
    # create_unified_upload_intents) so this function gives correct
    # results regardless of which entry point reaches it first.
    _seed_delivery_requirements(connection, tenant_id=tenant_id, journey_id=journey_id)
    booking_requirements = _base_requirements(connection, tenant_id, journey_id)
    delivery_requirements = _delivery_requirements(connection, tenant_id, journey_id)

    delivery_started = False
    corrected_to_delivery = False
    corrected_to_booking = False
    for item in di_documents:
        document_id = UUID(str(item["documentId"]))
        classified_type = item.get("classifiedDocumentTypeKey")
        stage_code, requirement_key = resolve_document_stage(
            connection,
            classified_type,
            tenant_id=tenant_id,
            journey_id=journey_id,
            booking_requirements=booking_requirements,
            delivery_requirements=delivery_requirements,
            document_id=document_id,
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
        if _correct_durable_store_stage(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document_id,
            stage_code=stage_code,
        ):
            if stage_code == "DELIVERY":
                corrected_to_delivery = True
            else:
                corrected_to_booking = True

    # document_capture_v2_documents.stage_code (just corrected above) is
    # local checklist bookkeeping only -- journey_document_extracted_
    # fields.stage_code is the column the actual materializers key their
    # durable-store reads on (_documents_from_durable_store,
    # materialize_delivery_documents_from_durable_store /
    # materialize_booking_documents_from_durable_store), and correcting one
    # table never used to correct the other. A document whose checklist
    # entry correctly says "Delivery" could still leave insurance/
    # registration/finance/commercial-lines permanently empty -- no amount
    # of clicking Resync would ever fix it, since Resync's own re-sync path
    # reads the exact same never-corrected column. Re-run the affected
    # stage's durable-store materializer now, immediately, rather than
    # waiting for some other document's future sync to incidentally re-hit
    # the fixed rows.
    if corrected_to_delivery:
        from audit_core.uc03_delivery_post_extraction_materialization import (
            materialize_delivery_documents_from_durable_store,
        )

        materialize_delivery_documents_from_durable_store(
            connection, tenant_id=tenant_id, journey_id=journey_id,
        )
    if corrected_to_booking:
        from audit_core.uc03_delivery_post_extraction_materialization import (
            materialize_booking_documents_from_durable_store,
        )

        materialize_booking_documents_from_durable_store(
            connection, tenant_id=tenant_id, journey_id=journey_id,
        )


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
    apply the combined result via apply_di_classification -- so Booking's
    and Delivery's own, completely untouched GET endpoints each show the
    right documents afterward, purely by reading that column as they
    already do.
    """
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

    apply_di_classification(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        di_documents=di_documents,
        actor_id=actor_id,
        actor_role=actor_role,
        correlation_id=correlation_id,
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


# --- Unified GET/DELETE/resync (Phase 4) -----------------------------------
#
# Phase 3 (#251) deliberately left the existing Booking/Delivery GET, DELETE
# and resync endpoints untouched, on the stated assumption that reconcile_
# unified_documents correcting document_capture_v2_documents.stage_code was
# enough for "Booking's and Delivery's own, completely untouched GET
# endpoints [to] show the right documents purely by reading that column."
# That assumption broke: both GET builders also cross-reference DI's own
# phase-scoped list_documents() call for live fields (contentUrl, live
# state), and DI's upload-time phase label is never updated by reconcile
# (see PR #375's own root-cause note) -- a relocated document's DI record is
# never found under its now-correct stage's phase, forcing a local-row
# fallback with contentUrl hardcoded to None. Separately, both DELETE
# endpoints tried DI before audit-core, so a document DI never durably
# received could never be removed at all, and Booking's own canDelete never
# matched the lock its DELETE endpoint actually enforces.
#
# This section finishes Phase 4: one GET (local + live), one DELETE, one
# resync, each covering both stages from a single call -- fixing the phase-
# lookup bug at the root (query DI under BOTH phases, merge by documentId,
# feed the SAME merged list to both stages' existing, unmodified response
# builders) instead of the local-fallback band-aid, and fixing delete's
# ordering and canDelete's lock check in the same pass. The Booking/Delivery
# routers' own capture-response builders (_build_capture_response et al.)
# are reused verbatim, unmodified -- they are also called directly by
# uc03_capture_local_reads.py... [see below], uc03_simplified_booking_flow.py,
# uc03_create_booking.py, uc03_booking_v2.py and uc03_document_review_v2.py,
# so changing their behavior here would ripple into flows this change never
# discussed.


class UnifiedCaptureV2Requirement(BaseModel):
    requirementKey: str
    stageCode: Literal["BOOKING", "DELIVERY"]
    label: str
    documentTypeKey: str
    requirementLevel: str
    conditionKey: str | None = None
    applicabilityState: Literal["APPLICABLE", "NOT_APPLICABLE", "UNRESOLVED"]
    state: str
    document: CaptureV2Document | None = None
    canView: bool = False
    canDelete: bool = False


class UnifiedCaptureV2Upload(CaptureV2Document):
    stageCode: Literal["BOOKING", "DELIVERY"]


class UnifiedCaptureV2Response(BaseModel):
    journeyId: UUID
    externalContextRef: str
    requirements: list[UnifiedCaptureV2Requirement]
    uploads: list[UnifiedCaptureV2Upload]
    bookingSubmitted: bool
    deliverySubmitted: bool


def _stage_completed(
    connection: Connection, *, tenant_id: str, journey_id: UUID, stage_code: str,
) -> bool:
    """True once the given stage has been marked complete (Booking's
    complete_booking_capture_v2 / Delivery's submit_delivery_capture_v2).
    A missing journey_stage_states row (the stage hasn't started/been
    seeded yet -- see module docstring on Delivery's requirements being
    seeded eagerly before Delivery 'starts') is correctly 'not completed',
    not an error -- unlike _capture_phase_state/_delivery_state, which raise
    NotFoundError for exactly that case and would incorrectly 404 a unified
    read for the (very common) journey still entirely in Booking.
    """
    row = connection.execute(
        text(
            """
            SELECT capture_completed_at_utc FROM auditcore.journey_stage_states
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND stage_code=:stage_code
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "stage_code": stage_code},
    ).mappings().one_or_none()
    return bool(row is not None and row["capture_completed_at_utc"] is not None)


def _merge_requirement(
    requirement: Any, *, stage_code: str, completed: bool,
) -> UnifiedCaptureV2Requirement:
    document = requirement.document
    return UnifiedCaptureV2Requirement(
        requirementKey=requirement.requirementKey,
        stageCode=stage_code,  # type: ignore[arg-type]
        label=requirement.label,
        documentTypeKey=requirement.documentTypeKey,
        requirementLevel=requirement.requirementLevel,
        conditionKey=requirement.conditionKey,
        applicabilityState=requirement.applicabilityState,
        state=requirement.state,
        document=document,
        canView=requirement.canView,
        # The one real bug fix here: Booking's own _build_capture_response
        # never checked the completion lock its DELETE endpoint enforces
        # (canDelete=public_doc is not None, unconditionally); Delivery's
        # own builder already did (and not submitted). Recomputed once,
        # identically, for both stages here instead of trusting either
        # builder's own (one correct, one wrong) value.
        canDelete=document is not None and not completed,
    )


def _build_unified_response(
    *,
    journey_id: UUID,
    context_ref: str,
    booking_response: Any,
    delivery_response: Any,
    booking_completed: bool,
    delivery_completed: bool,
) -> UnifiedCaptureV2Response:
    requirements = [
        _merge_requirement(item, stage_code="BOOKING", completed=booking_completed)
        for item in booking_response.requirements
    ] + [
        _merge_requirement(item, stage_code="DELIVERY", completed=delivery_completed)
        for item in delivery_response.requirements
    ]
    uploads = [
        UnifiedCaptureV2Upload(stageCode="BOOKING", **item.model_dump())
        for item in booking_response.uploads
    ] + [
        UnifiedCaptureV2Upload(stageCode="DELIVERY", **item.model_dump())
        for item in delivery_response.uploads
    ]
    return UnifiedCaptureV2Response(
        journeyId=journey_id,
        externalContextRef=context_ref,
        requirements=requirements,
        uploads=uploads,
        bookingSubmitted=booking_completed,
        deliverySubmitted=delivery_completed,
    )


def _build_local_unified_response(
    *, connection: Connection, tenant_id: str, journey_id: UUID,
) -> UnifiedCaptureV2Response:
    from audit_core.uc03_delivery_capture_v2 import (
        _build_local_delivery_capture_response,
    )
    from audit_core.uc03_document_capture_v2 import (
        _build_local_capture_response,
        _declarations,
    )

    booking_requirements, delivery_requirements = _merged_candidate_requirements(
        connection, tenant_id=tenant_id, journey_id=journey_id,
    )
    booking_documents = linked_documents_for_journey(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING",
    )
    delivery_documents = linked_documents_for_journey(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
    )
    booking_response = _build_local_capture_response(
        connection=connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        requirements=booking_requirements,
        declaration_rows=_declarations(connection, tenant_id, journey_id),
        audit_documents=booking_documents,
    )
    delivery_response = _build_local_delivery_capture_response(
        connection=connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        requirements=delivery_requirements,
        audit_documents=delivery_documents,
        submitted=_stage_completed(
            connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
        ),
    )
    return _build_unified_response(
        journey_id=journey_id,
        context_ref="local-v2-unified-capture",
        booking_response=booking_response,
        delivery_response=delivery_response,
        booking_completed=_stage_completed(
            connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING",
        ),
        delivery_completed=_stage_completed(
            connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
        ),
    )


def _read_unified_capture(
    *,
    connection: Connection,
    engine: Engine,
    tenant_id: str,
    journey_id: UUID,
    security_client: SecurityOAuthClient,
    di_client: DiClient,
    v2_client: DiCaptureV2Client,
) -> UnifiedCaptureV2Response:
    from audit_core.uc03_delivery_capture_v2 import _build_delivery_capture_response
    from audit_core.uc03_document_capture_v2 import (
        _build_capture_response,
        _declarations,
        _extracted_document_ids,
    )
    from audit_core.uc03_document_unrecognized import (
        sync_document_unrecognized_findings,
    )

    booking_requirements, delivery_requirements = _merged_candidate_requirements(
        connection, tenant_id=tenant_id, journey_id=journey_id,
    )
    booking_documents = linked_documents_for_journey(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING",
    )
    delivery_documents = linked_documents_for_journey(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
    )

    # Matches _read_delivery_capture's own unconditional call (runs even
    # when delivery_documents is empty) -- fires the vehicle-photos task
    # once Delivery's own requirements are satisfied. Must stay before the
    # local-fallback early return below, not just in the live-DI branch.
    from audit_core.uc03_delivery_commands import _ensure_vehicle_photos_task

    _ensure_vehicle_photos_task(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        requirements=delivery_requirements,
        audit_documents=delivery_documents,
        correlation_id="",
    )

    if not booking_documents and not delivery_documents:
        return _build_local_unified_response(
            connection=connection, tenant_id=tenant_id, journey_id=journey_id,
        )

    context_ref, token = _ensure_di_context(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
    )
    # Merge BOTH of DI's phases into one list -- the actual fix for the
    # "Missing"/lost-contentUrl bug (PR #375's own band-aid): a document
    # relocated to the other stage by reconcile is now found by documentId
    # regardless of which phase DI still has it filed under, so neither
    # builder below needs its own-row fallback for a merely-relocated
    # document. A single phase failing degrades to the other, matching
    # reconcile_unified_documents' own per-phase resilience; only a total
    # DI outage (both phases fail) surfaces as unavailable, same as today.
    di_documents: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    di_failures = 0
    for phase in ("BOOKING", "DELIVERY"):
        try:
            payload = v2_client.list_documents(
                token=token, tenant_id=tenant_id, external_context_ref=context_ref, phase=phase,
            )
        except DiCaptureV2Error as exc:
            di_failures += 1
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
    if di_failures == 2:
        raise DependencyUnavailableError(
            detail="Document capture status is temporarily unavailable."
        )

    # Best-effort, like every other reconciliation call in this codebase
    # (reconcile_unified_documents' own per-phase DI failures, every
    # materializer's own try/except) -- a live READ must never hard-fail
    # because its own self-healing side effect hit a transient DB issue.
    # Root-caused live (2026-09-26): apply_di_classification's UPDATE to
    # document_capture_v2_documents has no lock/retry protection of its own
    # (neither did the pre-existing per-stage _reconcile_documents/
    # _reconcile_delivery_documents this replaces -- same gap, just rarely
    # hit since each only ever touched one stage's documents per call). A
    # concurrent document-sync background task (self-heal sweep or a DI
    # webhook) can hold a lock on the same row for close to its own 45s
    # budget (uc03_confidence_review_policy._sync_booking_document_once)
    # while this request's own connection has only a 10s statement_timeout
    # (dependencies.get_connection) -- long enough to raise psycopg.errors.
    # QueryCanceled here and 500 the whole page. Falling back to whatever
    # document_capture_v2_documents/journey_document_extracted_fields state
    # already exists (skipping just this pass's classification/stage
    # correction) means the PC still sees a working checklist immediately;
    # the next poll or an explicit Recheck documents retries the correction.
    try:
        # A SAVEPOINT (begin_nested), not a bare try/except: a cancelled
        # statement (QueryCanceled, or any other DB error) leaves the whole
        # transaction aborted at the Postgres level -- every later query on
        # this same connection (the requirements/documents reads below,
        # _stage_completed, the final response build) would then fail too
        # with "current transaction is aborted", turning one failed
        # reconciliation into a total request failure regardless of this
        # try/except. Rolling back to the savepoint on failure restores the
        # connection to a clean, usable state for the rest of this request.
        with connection.begin_nested():
            apply_di_classification(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                di_documents=di_documents,
                actor_id=_MACHINE_ACTOR,
                actor_role="SYSTEM",
                correlation_id="",
            )
    except Exception:
        logger.warning(
            "uc03_unified_capture_apply_di_classification_failed",
            tenant_id=tenant_id,
            journey_id=str(journey_id),
            exc_info=True,
        )
    else:
        # Called once, not once per stage: an unrecognized document (DI
        # could not classify it at all) always resolves to stage_code=
        # "BOOKING" by resolve_document_stage's own default, so calling
        # this a second time with stage_code="DELIVERY" against the same
        # merged list would only ever create a second, differently-keyed
        # workflow task for the same document -- never a legitimate
        # Delivery-side unrecognized finding. Skipped (not just best-effort
        # itself) when classification above didn't run -- it reads the same
        # di_documents list to decide what's newly unrecognized, and would
        # otherwise raise tasks against classification state that was never
        # actually applied this pass.
        sync_document_unrecognized_findings(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            stage_code="BOOKING", di_documents=di_documents, correlation_id="",
        )

    booking_documents = linked_documents_for_journey(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING",
    )
    delivery_documents = linked_documents_for_journey(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
    )
    extracted_ids = frozenset(_extracted_document_ids(connection, tenant_id, journey_id))

    booking_response = _build_capture_response(
        journey_id=journey_id,
        context_ref=context_ref,
        requirements=booking_requirements,
        declaration_rows=_declarations(connection, tenant_id, journey_id),
        audit_documents=booking_documents,
        di_documents=di_documents,
        fallback_extracted_ids=extracted_ids,
    )
    delivery_response = _build_delivery_capture_response(
        journey_id=journey_id,
        context_ref=context_ref,
        requirements=delivery_requirements,
        audit_documents=delivery_documents,
        di_documents=di_documents,
        submitted=_stage_completed(
            connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
        ),
        fallback_extracted_ids=extracted_ids,
    )
    return _build_unified_response(
        journey_id=journey_id,
        context_ref=context_ref,
        booking_response=booking_response,
        delivery_response=delivery_response,
        booking_completed=_stage_completed(
            connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING",
        ),
        delivery_completed=_stage_completed(
            connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
        ),
    )


@router.get("/capture-local", response_model=UnifiedCaptureV2Response)
def get_unified_capture_local(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> UnifiedCaptureV2Response:
    """Replaces uc03_capture_local_reads.get_booking_capture_local_v2 and
    get_delivery_capture_local_v2 -- DB-only, no DI round trip, used for the
    screen's own first paint."""
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    return _build_local_unified_response(
        connection=connection, tenant_id=tenant_id, journey_id=journey_id,
    )


@router.get("/capture", response_model=UnifiedCaptureV2Response)
def get_unified_capture(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
    v2_client: Annotated[DiCaptureV2Client, Depends(get_di_capture_v2_client)],
) -> UnifiedCaptureV2Response:
    """Replaces uc03_document_capture_v2.get_booking_capture_v2 and
    uc03_delivery_capture_v2.get_delivery_capture_v2. Never gates on either
    stage's own business_status (matching _authorize_booking's/_authorize_
    delivery's own unconditional-read docstrings) -- a journey still
    entirely in Booking, or one whose Booking has long since closed, both
    read correctly here.
    """
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    return _read_unified_capture(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
        v2_client=v2_client,
    )


def _document_stage(
    connection: Connection, *, tenant_id: str, journey_id: UUID, document_id: UUID,
) -> str | None:
    row = connection.execute(
        text(
            """
            SELECT stage_code FROM auditcore.document_capture_v2_documents
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND di_document_id=:document_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "document_id": document_id},
    ).scalar_one_or_none()
    return str(row) if row is not None else None


@router.delete("/{document_id}", status_code=204)
def delete_unified_document(
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
    """Replaces uc03_document_capture_v2.delete_booking_document_v2 and
    uc03_delivery_capture_v2.delete_delivery_document_v2 -- both were
    byte-identical except the stage_code literal, and shared the same two
    bugs (see module-section docstring above): canDelete never matching the
    lock actually enforced here, and DI-before-audit-core ordering meaning a
    document DI never durably received could never be removed. Which stage
    owns the document (and so which stage's completion lock applies) is
    resolved from audit-core's own row, not from a caller-supplied stage.
    """
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    stage_code = _document_stage(
        connection, tenant_id=tenant_id, journey_id=journey_id, document_id=document_id,
    )
    if stage_code is None:
        return
    if _stage_completed(connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage_code):
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Document capture is complete",
            detail=f"Documents cannot be deleted after {stage_code.title()} has been submitted.",
        )
    # Audit-core first: this is the row the UI's own "Uploaded" status reads
    # from, and the one the PC actually needs gone. DI's own copy is deleted
    # best-effort, second -- a document DI never durably received (sync
    # never completed) has nothing to delete there anyway, and that must
    # never block removing the audit-core row the PC is looking at.
    connection.execute(
        text(
            """
            DELETE FROM auditcore.document_capture_v2_documents
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND di_document_id=:document_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "document_id": document_id},
    )
    # evidence and journey_document_extracted_fields are permanent audit
    # records -- DELETE is revoked on both at the database level (schema-
    # wide in 0002_runtime_role_rls.py, restated per-table in each table's
    # own migration), with document_capture_v2_documents above as the one
    # deliberate exception. Voiding evidence (an UPDATE, fully permitted) is
    # the correct and only way to make a deleted document's data stop
    # counting, and is the same convention uc03_customer_identity_
    # consistency.py already uses for a wrong-customer document.
    # _sync_booking_document itself already refuses to act on non-ACTIVE
    # evidence (`if link is None or association_status != "ACTIVE": return
    # 0`), and _documents_from_durable_store now excludes it from canonical
    # materialization the same way (uc03_delivery_post_extraction_
    # materialization.py). If this UPDATE fails, the whole delete --
    # including the document row above -- rolls back with it: this
    # connection is one transaction for the entire request
    # (dependencies.get_connection wraps every request in engine.begin()).
    connection.execute(
        text(
            """
            UPDATE auditcore.evidence
            SET association_status='VOIDED',
                void_reason='DOCUMENT_DELETED',
                voided_by_actor_id=:actor_id,
                voided_at_utc=now()
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND di_document_id=:document_id
              AND association_status='ACTIVE'
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": document_id,
            "actor_id": _human_actor_id(human_principal),
        },
    )
    # Re-run this stage's materializer now, immediately -- a canonical fact
    # already written from this document before it was deleted must not sit
    # stale until some unrelated document's future sync happens to
    # recompute it (same reasoning as apply_di_classification's own
    # corrected_to_delivery/corrected_to_booking re-materialization call).
    # Both functions catch their own body in try/except and return
    # {"error": True} rather than raise a bare Python exception -- but a
    # cancelled statement (or any DB error) inside that try/except still
    # leaves the whole transaction aborted at the Postgres level, which
    # their own except-and-return-dict can't undo. SAVEPOINT (begin_nested)
    # so a rare DB-level failure here rolls back cleanly instead of taking
    # down the DI delete call still to come on this same connection.
    #
    # Deliberately not extended to materialize_machine_booking_values
    # (Booking's generic-attribute projection, uc03_post_extraction_
    # materialization.py): it resolves its own "current winner" through a
    # different path (_preferred_rows/_winner_by_id), not _documents_from_
    # durable_store, and has not been audited for the same evidence-status
    # awareness -- a real, separate gap, left alone rather than guessed at.
    try:
        with connection.begin_nested():
            if stage_code == "DELIVERY":
                from audit_core.uc03_delivery_post_extraction_materialization import (
                    materialize_delivery_documents_from_durable_store,
                )

                materialize_delivery_documents_from_durable_store(
                    connection, tenant_id=tenant_id, journey_id=journey_id,
                )
            else:
                from audit_core.uc03_delivery_post_extraction_materialization import (
                    materialize_booking_documents_from_durable_store,
                )

                materialize_booking_documents_from_durable_store(
                    connection, tenant_id=tenant_id, journey_id=journey_id,
                )
    except Exception:
        logger.warning(
            "uc03_unified_delete_rematerialize_failed",
            tenant_id=tenant_id,
            journey_id=str(journey_id),
            stage_code=stage_code,
            exc_info=True,
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


class UnifiedResyncResponse(BaseModel):
    documentsFound: int
    documentsResynced: int
    documentsNotYetExtracted: int
    queuedDocumentCount: int


@router.post("/resync", response_model=UnifiedResyncResponse)
def resync_unified_documents(
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
) -> UnifiedResyncResponse:
    """Replaces uc03_document_capture_v2.resync_booking_capture_v2 and
    uc03_delivery_capture_v2.resync_delivery_capture_v2 -- both already
    delegated reclassification to reconcile_unified_documents and were
    otherwise byte-identical except the stage_code literal and which
    linked-documents getter they called. One call now covers both stages.
    """
    from audit_core.uc03_confidence_review_policy import (
        _run_sync_booking_document_task,
        sync_stagger_seconds,
    )
    from audit_core.uc03_delivery_capture_v2 import (
        _linked_delivery_documents,
        _resyncable_document_ids,
    )
    from audit_core.uc03_document_capture_v2 import (
        _backfill_evidence_links_for_resync,
        _linked_documents,
    )

    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    booking_requirements, delivery_requirements = _merged_candidate_requirements(
        connection, tenant_id=tenant_id, journey_id=journey_id,
    )
    any_documents = bool(_linked_documents(connection, tenant_id, journey_id)) or bool(
        _linked_delivery_documents(connection, tenant_id, journey_id)
    )
    if any_documents:
        context_ref, token = _ensure_di_context(
            connection=connection,
            engine=engine,
            tenant_id=tenant_id,
            journey_id=journey_id,
            security_client=security_client,
            di_client=di_client,
        )
        # SAVEPOINT, not a bare call: reconcile_unified_documents' own
        # apply_di_classification has no lock/retry protection (see
        # _read_unified_capture's matching comment) -- a lock-contention
        # timeout here must not poison this connection's transaction for
        # the rest of resync (the linked-documents reads, evidence
        # backfill, and background-sync dispatch below all still run on
        # this same connection). Degrading to "resync reports pre-existing
        # state" beats a 500 on an explicit PC-triggered Recheck click.
        try:
            with connection.begin_nested():
                reconcile_unified_documents(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    actor_id=f"manual-resync:{human_principal.subject}",
                    actor_role="PC",
                    correlation_id="",
                    v2_client=v2_client,
                    context_ref=context_ref,
                    token=token,
                )
        except Exception:
            logger.warning(
                "uc03_unified_resync_reconcile_failed",
                tenant_id=tenant_id,
                journey_id=str(journey_id),
                exc_info=True,
            )

    booking_documents = _linked_documents(connection, tenant_id, journey_id)
    delivery_documents = _linked_delivery_documents(connection, tenant_id, journey_id)
    booking_ids = [
        row["di_document_id"] for row in booking_documents
        if str(row.get("capture_status") or "").upper() == "CLASSIFIED"
    ]
    delivery_ids = _resyncable_document_ids(delivery_documents)

    _backfill_evidence_links_for_resync(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=booking_documents,
        document_ids=booking_ids,
        requirements=booking_requirements,
        service_id=f"manual-resync:{human_principal.subject}",
    )
    _backfill_evidence_links_for_resync(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=delivery_documents,
        document_ids=delivery_ids,
        requirements=delivery_requirements,
        service_id=f"manual-resync:{human_principal.subject}",
    )

    index = 0
    for document_id in booking_ids:
        background_tasks.add_task(
            _run_sync_booking_document_task,
            engine,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document_id,
            service_id=f"manual-resync:{human_principal.subject}",
            stage_code="BOOKING",
            initial_delay_seconds=sync_stagger_seconds(index),
        )
        index += 1
    for document_id in delivery_ids:
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
        index += 1

    total_documents = len(booking_documents) + len(delivery_documents)
    total_resynced = len(booking_ids) + len(delivery_ids)
    return UnifiedResyncResponse(
        documentsFound=total_documents,
        documentsResynced=total_resynced,
        documentsNotYetExtracted=total_documents - total_resynced,
        queuedDocumentCount=total_resynced,
    )
