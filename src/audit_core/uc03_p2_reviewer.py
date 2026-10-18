"""The Reviewer's own surface: a board, an open ledger, and two reviewed marks.

A Reviewer sits at one physical location, is mapped to several Dealer Outlets,
and reviews what a PC captured across all of them at once. They do not upload
and they hold no task queue, so this module is their whole workspace:

- a board of the journeys in their covered Outlets, split by dealership, with
  the journeys still open separated from the ones already closed;
- an advisory claim, so two Reviewers do not work the same journey;
- the Booking and Delivery documents-reviewed marks, each of which may only be
  set once every uploaded document on that stage has actually been opened;
- a daily and weekly count of their own reviews, by Outlet.

Two points of precision.

The open ledger is written where a document's bytes are served, not where a
button is clicked, because that is the only place Audit Core can prove the
document was fetched. One click in the box view is exactly one such fetch: the
card calls `onOpen`, the route gains a document id, and the editor then asks
for the content. Nothing prefetches it, no thumbnail renders it, and no
document is selected on arrival, so an open in this ledger means somebody
asked for that document.

Stage comes from `document_capture_v2_documents.stage_code`, which is NOT NULL,
never from `p2_document_queue.business_stage`, which is nullable and is read
two incompatible ways elsewhere -- defaulted to BOOKING in one place and
treated as unmatched in another (see RVW-AC-01 in the pending-issues register).
A gate keyed on that column would be satisfiable, or unsatisfiable, by
accident.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import P2AccessContext, authorize_p2

router = APIRouter(
    prefix="/p2/v1/tenants/{tenant_id}",
    tags=["uc03-phase2-reviewer"],
)

_READ_PERMISSION = "audit.journey.read"
_UPDATE_PERMISSION = "audit.journey.update"

StageCode = Literal["BOOKING", "DELIVERY"]

# The default a Project is onboarded with; only used if a Project somehow has
# no timezone, because a day boundary has to come from somewhere and the
# counters are read by people working Indian dealership hours.
_FALLBACK_TIMEZONE = "Asia/Kolkata"

# Every Journey the caller's active business assignments cover. One Reviewer
# holds one row per Outlet, so this authorises them across all of them at once
# without a per-session Outlet pin. Identical to the predicate the Bookings
# list and `resolve_p2_scope` use; kept literal rather than shared because the
# surrounding queries differ and a half-applied scope predicate is the one bug
# worth never introducing.
_SCOPE_PREDICATE = """
    EXISTS (
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

# Closed means the stage engine finished the Delivery, or the Booking was
# cancelled or found to be a duplicate. Same definition the Bookings list
# uses, so "open" means the same thing on both boards.
_CLOSED_PREDICATE = """
    EXISTS (
      SELECT 1 FROM auditcore.journey_stage_states st
      WHERE st.tenant_id=j.tenant_id AND st.journey_id=j.journey_id
        AND ((st.stage_code='DELIVERY' AND st.business_completed_at_utc IS NOT NULL)
             OR (st.stage_code='BOOKING'
                 AND (st.business_status IN ('BOOKING_CANCELLED','DUPLICATE_BOOKING')
                      OR st.closure_disposition='NO_DELIVERY')))
    )
"""


def _authorize(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID | None,
    human_principal: HumanPrincipal,
    authorization_client: SecurityAuthorizationClient,
    permission_key: str,
) -> P2AccessContext:
    return authorize_p2(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=permission_key,
    )


def _project_timezone(connection: Connection, *, tenant_id: str) -> str:
    row = connection.execute(
        text("SELECT timezone_name FROM auditcore.projects WHERE tenant_id=:tenant_id"),
        {"tenant_id": tenant_id},
    ).scalar_one_or_none()
    return str(row or _FALLBACK_TIMEZONE)


def record_document_open(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    actor_id: str,
) -> None:
    """Note that this actor has now seen this document.

    Called once the bytes have actually been served, so a failed fetch is not
    recorded as a view. The first open is the one the reviewed-marks gate
    reads; `last_opened_at_utc` moves on later opens but is not a count --
    the client caches the content request, so repeat views do not all reach
    the server and counting them would be misleading.
    """
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_document_views (
                tenant_id, di_document_id, actor_id, journey_id
            ) VALUES (:tenant_id, :document_id, :actor_id, :journey_id)
            ON CONFLICT (tenant_id, di_document_id, actor_id)
            DO UPDATE SET last_opened_at_utc = now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "document_id": document_id,
            "actor_id": actor_id,
            "journey_id": journey_id,
        },
    )


def page_stage_code(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    di_document_id: UUID,
) -> str | None:
    """The stage a page belongs to, from the column that is NOT NULL.

    Deliberately not `p2_document_queue.business_stage`: that column is
    nullable and two call sites already read it incompatibly -- one treats a
    null as BOOKING, the other as matching no stage at all. A guard keyed on it
    would fire, or fail to fire, depending on which reading you inherited.
    `document_capture_v2_documents.stage_code` is NOT NULL, and a page with no
    capture row returns None so the caller can fail closed rather than guess.
    """
    row = connection.execute(
        text(
            """
            SELECT stage_code
            FROM auditcore.document_capture_v2_documents
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND di_document_id=:di_document_id
              AND capture_status <> 'SUPERSEDED'
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "di_document_id": di_document_id,
        },
    ).scalar_one_or_none()
    return str(row) if row is not None else None


def stage_documents(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
) -> list[dict[str, Any]]:
    """Every openable document on one stage of a journey.

    Superseded copies are excluded: a replaced document cannot be opened, so
    counting it would leave the stage permanently unreviewable. This is the
    same `capture_status <> 'SUPERSEDED'` test the document-content endpoint
    applies when it decides whether a document exists at all.
    """
    return [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT d.di_document_id,
                       d.classified_document_type_key AS document_type,
                       d.original_filename
                FROM auditcore.document_capture_v2_documents d
                WHERE d.tenant_id=:tenant_id
                  AND d.journey_id=:journey_id
                  AND d.stage_code=:stage_code
                  AND d.capture_status <> 'SUPERSEDED'
                ORDER BY d.original_filename NULLS LAST, d.di_document_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "stage_code": stage_code,
            },
        ).mappings()
    ]


def stage_review_gate(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    actor_id: str,
) -> dict[str, Any]:
    """Whether this actor may mark this stage's documents reviewed.

    Opens are counted per actor on purpose. Marking a stage reviewed is a
    personal attestation -- "I have looked at every document on this stage" --
    so one Reviewer cannot discharge it with documents only a colleague
    opened. The opens may be spread over days; that is why they are stored.
    """
    documents = stage_documents(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage_code
    )
    if not documents:
        return {
            "documentCount": 0,
            "openedCount": 0,
            "outstanding": [],
            "satisfied": False,
            "reason": "NO_DOCUMENTS",
        }

    opened = {
        str(row[0])
        for row in connection.execute(
            text(
                """
                SELECT di_document_id
                FROM auditcore.p2_document_views
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND actor_id=:actor_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "actor_id": actor_id},
        ).all()
    }

    outstanding = [
        {
            "documentId": str(item["di_document_id"]),
            "documentType": item["document_type"],
            "fileName": item["original_filename"],
        }
        for item in documents
        if str(item["di_document_id"]) not in opened
    ]
    return {
        "documentCount": len(documents),
        "openedCount": len(documents) - len(outstanding),
        "outstanding": outstanding,
        "satisfied": not outstanding,
        "reason": None if not outstanding else "DOCUMENTS_NOT_OPENED",
    }


def stage_review(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
) -> dict[str, Any] | None:
    """The reviewed mark for one stage, if it has been set."""
    row = connection.execute(
        text(
            """
            SELECT reviewed_by_actor_id, reviewed_by_role_code, reviewed_at_utc,
                   document_count
            FROM auditcore.p2_stage_document_reviews
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code=:stage_code
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "stage_code": stage_code},
    ).mappings().one_or_none()
    if row is None:
        return None
    return {
        "reviewedByActorId": row["reviewed_by_actor_id"],
        "reviewedByRole": row["reviewed_by_role_code"],
        "reviewedAtUtc": row["reviewed_at_utc"],
        "documentCount": int(row["document_count"]),
    }


def stage_is_reviewed(
    connection: Connection, *, tenant_id: str, journey_id: UUID, stage_code: str
) -> bool:
    """Used by the resync guard: a reviewed stage is not re-read from DI."""
    return (
        connection.execute(
            text(
                """
                SELECT 1 FROM auditcore.p2_stage_document_reviews
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code=:stage_code
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "stage_code": stage_code},
        ).scalar_one_or_none()
        is not None
    )


def log_field_change(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    document_id: UUID | None,
    field_key: str,
    canonical_field_id: str | None,
    source_fact_version: int | None,
    previous_value: str | None,
    new_value: str | None,
    confidence_score: float | None,
    change_reason: str,
    actor_id: str,
    actor_role_code: str,
    remarks: str | None = None,
    correlation_id: str | None = None,
) -> None:
    """Append one field change to the durable trail.

    `journey_document_extracted_fields` records who modified a value but not in
    what role, and a resync overwrites the modified value and its attribution
    in place -- so without this row there is no evidence a correction was ever
    made. A Reviewer changes high-confidence values with nobody approving
    afterwards, which makes this the control rather than a convenience.

    `previous_value` and `new_value` are JSON text; the column is jsonb.
    """
    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_field_change_log (
                tenant_id, journey_id, stage_code, di_document_id, field_key,
                source_canonical_field_id, source_fact_version,
                previous_value, new_value, confidence_score, change_reason,
                actor_id, actor_role_code, remarks, correlation_id
            ) VALUES (
                :tenant_id, :journey_id, :stage_code, :document_id, :field_key,
                :canonical_field_id, :source_fact_version,
                CAST(:previous_value AS jsonb), CAST(:new_value AS jsonb),
                :confidence_score, :change_reason,
                :actor_id, :actor_role_code, :remarks, :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
            "document_id": document_id,
            "field_key": field_key[:160],
            "canonical_field_id": canonical_field_id,
            "source_fact_version": source_fact_version,
            "previous_value": previous_value,
            "new_value": new_value,
            "confidence_score": confidence_score,
            "change_reason": change_reason,
            "actor_id": actor_id,
            "actor_role_code": actor_role_code,
            "remarks": (remarks or None),
            "correlation_id": correlation_id,
        },
    )


def modified_field_count(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
) -> int:
    """How many of a document's fields currently carry a hand-made value.

    The pre-resync warning uses this. A resync re-copies DI's own values over
    the stored row, so these are the changes that would be lost; warning only
    when there is something to lose keeps the dialogue meaningful instead of
    training people to click through it.
    """
    return int(
        connection.execute(
            text(
                """
                SELECT count(*)
                FROM auditcore.journey_document_extracted_fields
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND di_document_id=:document_id
                  AND is_modified IS TRUE
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "document_id": document_id,
            },
        ).scalar_one()
    )


@router.get("/journeys/{journey_id}/pages/{queue_id}/resync-preview")
def get_resync_preview(
    tenant_id: str,
    journey_id: UUID,
    queue_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """What a resync of this page would cost, asked before it is done.

    A re-copy replaces the stored values with DI's own, so any hand-made value
    on this page is lost and has to be redone. Counting them first means the
    warning only appears when there is something to lose -- a dialogue that
    always appears is one people learn to click through.

    The count is page-scoped, and that is correct rather than convenient: the
    reconcile a resync triggers copies facts only for pages it moves to
    SYNCING_TO_AUDIT_CORE, and leaves other pages' fields alone even when they
    are swept in for classification.
    """
    _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    page = connection.execute(
        text(
            """
            SELECT di_document_id, queue_status
            FROM auditcore.p2_document_queue
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND queue_id=:queue_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "queue_id": queue_id},
    ).mappings().one_or_none()
    if page is None:
        raise HTTPException(status_code=404, detail="That page is not on this journey.")
    if page["di_document_id"] is None:
        return {
            "queueId": str(queue_id),
            "documentId": None,
            "stageCode": None,
            "modifiedFieldCount": 0,
            "stageReviewed": False,
            "canResync": False,
            "blockedReason": "NOT_SYNCED",
        }

    document_id = UUID(str(page["di_document_id"]))
    stage_code = page_stage_code(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        di_document_id=document_id,
    )
    reviewed = bool(
        stage_code
        and stage_is_reviewed(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage_code,
        )
    )
    return {
        "queueId": str(queue_id),
        "documentId": str(document_id),
        "stageCode": stage_code,
        "modifiedFieldCount": modified_field_count(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document_id,
        ),
        "stageReviewed": reviewed,
        "canResync": stage_code is not None and not reviewed,
        "blockedReason": (
            "UNCLASSIFIED" if stage_code is None
            else "STAGE_REVIEWED" if reviewed
            else None
        ),
    }


@router.get("/reviewer/journeys")
def list_reviewer_journeys(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    state: str = "open",
    limit: int = 200,
) -> dict[str, Any]:
    """The Reviewer's board, grouped by dealership and Outlet.

    ``state``: open (the default -- still being worked) or closed. Closed
    journeys stay available but are asked for separately, because a Reviewer's
    day is the open ones.
    """
    access = _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=None,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    wanted = state if state in {"open", "closed"} else "open"
    rows = connection.execute(
        text(
            f"""
            SELECT j.journey_id, j.journey_reference, j.created_at_utc, j.updated_at_utc,
                   j.dealer_id, j.outlet_id, d.dealer_name, o.outlet_name, o.outlet_code,
                   COALESCE(c.legal_name, c.display_name) AS customer_name,
                   j.created_by_display_name AS pc_name,
                   cl.claimed_by_actor_id, cl.claimed_at_utc,
                   bk.reviewed_at_utc AS booking_reviewed_at_utc,
                   bk.reviewed_by_actor_id AS booking_reviewed_by,
                   dl.reviewed_at_utc AS delivery_reviewed_at_utc,
                   dl.reviewed_by_actor_id AS delivery_reviewed_by,
                   (SELECT count(*) FROM auditcore.document_capture_v2_documents dc
                     WHERE dc.tenant_id=j.tenant_id AND dc.journey_id=j.journey_id
                       AND dc.stage_code='BOOKING' AND dc.capture_status <> 'SUPERSEDED'
                   ) AS booking_documents,
                   (SELECT count(*) FROM auditcore.document_capture_v2_documents dc
                     WHERE dc.tenant_id=j.tenant_id AND dc.journey_id=j.journey_id
                       AND dc.stage_code='DELIVERY' AND dc.capture_status <> 'SUPERSEDED'
                   ) AS delivery_documents
            FROM auditcore.journeys j
            JOIN auditcore.customers c
              ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
            JOIN auditcore.dealers d
              ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
            JOIN auditcore.dealer_outlets o
              ON o.tenant_id=j.tenant_id AND o.dealer_id=j.dealer_id AND o.outlet_id=j.outlet_id
            LEFT JOIN auditcore.p2_journey_claims cl
              ON cl.tenant_id=j.tenant_id AND cl.journey_id=j.journey_id
            LEFT JOIN auditcore.p2_stage_document_reviews bk
              ON bk.tenant_id=j.tenant_id AND bk.journey_id=j.journey_id
             AND bk.stage_code='BOOKING'
            LEFT JOIN auditcore.p2_stage_document_reviews dl
              ON dl.tenant_id=j.tenant_id AND dl.journey_id=j.journey_id
             AND dl.stage_code='DELIVERY'
            WHERE j.tenant_id=:tenant_id
              AND {_SCOPE_PREDICATE}
              AND (CAST(:state AS varchar) = 'closed') = {_CLOSED_PREDICATE}
            ORDER BY lower(d.dealer_name), lower(o.outlet_name), j.updated_at_utc DESC
            LIMIT :limit
            """
        ),
        {
            "tenant_id": tenant_id,
            "actor_id": access.actor_id,
            "state": wanted,
            "limit": max(1, min(limit, 500)),
        },
    ).mappings().all()

    groups: list[dict[str, Any]] = []
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["dealer_id"]), str(row["outlet_id"]))
        group = index.get(key)
        if group is None:
            group = {
                "dealerId": str(row["dealer_id"]),
                "dealerName": row["dealer_name"],
                "outletId": str(row["outlet_id"]),
                "outletName": row["outlet_name"],
                "outletCode": row["outlet_code"],
                "journeys": [],
            }
            index[key] = group
            groups.append(group)
        group["journeys"].append(
            {
                "journeyId": str(row["journey_id"]),
                "journeyReference": row["journey_reference"],
                "customerName": row["customer_name"],
                "pcName": row["pc_name"],
                "createdAtUtc": row["created_at_utc"],
                "updatedAtUtc": row["updated_at_utc"],
                "claimedByActorId": row["claimed_by_actor_id"],
                "claimedAtUtc": row["claimed_at_utc"],
                "claimedByMe": row["claimed_by_actor_id"] == access.actor_id,
                "booking": {
                    "documentCount": int(row["booking_documents"]),
                    "reviewedAtUtc": row["booking_reviewed_at_utc"],
                    "reviewedByActorId": row["booking_reviewed_by"],
                },
                "delivery": {
                    "documentCount": int(row["delivery_documents"]),
                    "reviewedAtUtc": row["delivery_reviewed_at_utc"],
                    "reviewedByActorId": row["delivery_reviewed_by"],
                },
            }
        )

    return {
        "state": wanted,
        "journeyCount": len(rows),
        "outlets": groups,
    }


@router.get("/journeys/{journey_id}/review-state")
def get_review_state(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Per-stage: the reviewed mark, and what is still unopened for this actor."""
    access = _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    claim = connection.execute(
        text(
            """
            SELECT claimed_by_actor_id, claimed_by_role_code, claimed_at_utc
            FROM auditcore.p2_journey_claims
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()

    stages: dict[str, Any] = {}
    for stage_code in ("BOOKING", "DELIVERY"):
        gate = stage_review_gate(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage_code,
            actor_id=access.actor_id,
        )
        review = stage_review(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage_code,
        )
        stages[stage_code] = {
            "review": review,
            "gate": gate,
            # Already reviewed stages are never re-marked, so the gate is only
            # worth showing while the mark is still outstanding.
            "canMarkReviewed": review is None and gate["satisfied"],
        }

    return {
        "journeyId": str(journey_id),
        "actorId": access.actor_id,
        "claim": {
            "claimedByActorId": claim["claimed_by_actor_id"],
            "claimedByRole": claim["claimed_by_role_code"],
            "claimedAtUtc": claim["claimed_at_utc"],
            "claimedByMe": claim["claimed_by_actor_id"] == access.actor_id,
        } if claim else None,
        "stages": stages,
    }


@router.post("/journeys/{journey_id}/stages/{stage_code}/documents-reviewed", status_code=201)
def mark_stage_documents_reviewed(
    tenant_id: str,
    journey_id: UUID,
    stage_code: StageCode,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Mark a stage's documents reviewed. Once, and only once every document
    on the stage has been opened by this actor.

    The gate is re-evaluated here, inside the same transaction as the write,
    rather than trusted from the page that offered the button: a replayed or
    hand-made request must not be able to set the mark with documents
    unopened.

    The mark is never revoked. A document uploaded afterwards does not reopen
    it (owner decision 2026-10-18) -- the Reviewer may still review that
    document, there is simply no further mark to set. `document_count` records
    how many documents the mark covered, so a later upload is visible as
    arriving after the fact rather than silently included in it.
    """
    access = _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_UPDATE_PERMISSION,
    )
    existing = stage_review(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage_code
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"The {stage_code.title()} documents are already marked reviewed.",
        )

    gate = stage_review_gate(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=stage_code,
        actor_id=access.actor_id,
    )
    if gate["reason"] == "NO_DOCUMENTS":
        raise HTTPException(
            status_code=409,
            detail=f"There are no {stage_code.lower()} documents to review yet.",
        )
    if not gate["satisfied"]:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(gate['outstanding'])} of {gate['documentCount']} "
                f"{stage_code.lower()} documents have not been opened yet."
            ),
        )

    placement = connection.execute(
        text(
            """
            SELECT dealer_id, outlet_id FROM auditcore.journeys
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()

    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_stage_document_reviews (
                tenant_id, journey_id, stage_code, reviewed_by_actor_id,
                reviewed_by_role_code, document_count, dealer_id, outlet_id
            ) VALUES (
                :tenant_id, :journey_id, :stage_code, :actor_id,
                :role_code, :document_count, :dealer_id, :outlet_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
            "actor_id": access.actor_id,
            "role_code": access.operating_role or access.functional_role or "USER",
            "document_count": gate["documentCount"],
            "dealer_id": placement["dealer_id"],
            "outlet_id": placement["outlet_id"],
        },
    )
    return {
        "journeyId": str(journey_id),
        "stageCode": stage_code,
        "documentCount": gate["documentCount"],
        "reviewedByActorId": access.actor_id,
    }


@router.put("/journeys/{journey_id}/claim")
def claim_journey(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Take a journey, so colleagues can see it is being worked.

    Advisory by design: a claim does not gate the review and anyone may take
    one over. A claim that locked the journey would need releasing,
    reassigning and expiring the first time somebody went on leave holding
    forty of them.
    """
    access = _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_UPDATE_PERMISSION,
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_journey_claims (
                tenant_id, journey_id, claimed_by_actor_id, claimed_by_role_code
            ) VALUES (:tenant_id, :journey_id, :actor_id, :role_code)
            ON CONFLICT (tenant_id, journey_id) DO UPDATE
              SET claimed_by_actor_id=EXCLUDED.claimed_by_actor_id,
                  claimed_by_role_code=EXCLUDED.claimed_by_role_code,
                  claimed_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "actor_id": access.actor_id,
            "role_code": access.operating_role or access.functional_role or "USER",
        },
    )
    return {"journeyId": str(journey_id), "claimedByActorId": access.actor_id}


@router.delete("/journeys/{journey_id}/claim", status_code=204)
def release_journey_claim(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> None:
    """Put a journey back. Only the holder releases their own claim; taking
    one over is a claim, which keeps 'who has it' unambiguous."""
    access = _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_UPDATE_PERMISSION,
    )
    connection.execute(
        text(
            """
            DELETE FROM auditcore.p2_journey_claims
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND claimed_by_actor_id=:actor_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "actor_id": access.actor_id,
        },
    )


@router.get("/reviewer/activity")
def get_reviewer_activity(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    period: str = "day",
    days: int = 14,
) -> dict[str, Any]:
    """How much a Reviewer got through, by day or week, split by Outlet.

    Day and week boundaries are the Project's own, not UTC. India runs
    UTC+05:30, so anything marked before 05:30 local belongs to the previous
    UTC day -- counting in UTC would move a third of an evening's work into
    yesterday. `date_trunc('week', ...)` is ISO, so weeks start on Monday.

    Counts come from the reviewed marks, which are immutable, so a day's
    figure cannot change after the fact. Field changes are counted per journey
    from the change log alongside them, since correcting values is the other
    half of the job.
    """
    access = _authorize(
        connection,
        tenant_id=tenant_id,
        journey_id=None,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    timezone_name = _project_timezone(connection, tenant_id=tenant_id)
    grain = "week" if period == "week" else "day"
    window = max(1, min(days, 180))

    reviews = connection.execute(
        text(
            """
            SELECT
                CASE WHEN :grain = 'week'
                     THEN date_trunc('week', r.reviewed_at_utc AT TIME ZONE :tz)::date
                     ELSE (r.reviewed_at_utc AT TIME ZONE :tz)::date
                END AS bucket,
                r.outlet_id, o.outlet_name, d.dealer_name, r.stage_code,
                count(*) AS marks,
                count(DISTINCT r.journey_id) AS journeys
            FROM auditcore.p2_stage_document_reviews r
            JOIN auditcore.dealer_outlets o
              ON o.tenant_id=r.tenant_id AND o.outlet_id=r.outlet_id
            JOIN auditcore.dealers d
              ON d.tenant_id=r.tenant_id AND d.dealer_id=r.dealer_id
            WHERE r.tenant_id=:tenant_id
              AND r.reviewed_by_actor_id=:actor_id
              AND (r.reviewed_at_utc AT TIME ZONE :tz)::date
                  > (now() AT TIME ZONE :tz)::date - CAST(:window AS integer)
            GROUP BY 1, r.outlet_id, o.outlet_name, d.dealer_name, r.stage_code
            ORDER BY 1 DESC, lower(d.dealer_name), lower(o.outlet_name)
            """
        ),
        {
            "tenant_id": tenant_id,
            "actor_id": access.actor_id,
            "tz": timezone_name,
            "grain": grain,
            "window": window,
        },
    ).mappings().all()

    changes = connection.execute(
        text(
            """
            SELECT
                CASE WHEN :grain = 'week'
                     THEN date_trunc('week', l.changed_at_utc AT TIME ZONE :tz)::date
                     ELSE (l.changed_at_utc AT TIME ZONE :tz)::date
                END AS bucket,
                count(*) AS changes,
                count(DISTINCT l.journey_id) AS journeys
            FROM auditcore.journey_field_change_log l
            WHERE l.tenant_id=:tenant_id
              AND l.actor_id=:actor_id
              AND l.change_reason = 'CORRECTION'
              AND (l.changed_at_utc AT TIME ZONE :tz)::date
                  > (now() AT TIME ZONE :tz)::date - CAST(:window AS integer)
            GROUP BY 1
            ORDER BY 1 DESC
            """
        ),
        {
            "tenant_id": tenant_id,
            "actor_id": access.actor_id,
            "tz": timezone_name,
            "grain": grain,
            "window": window,
        },
    ).mappings().all()

    buckets: dict[str, dict[str, Any]] = {}
    for row in reviews:
        key = str(row["bucket"])
        bucket = buckets.setdefault(
            key,
            {
                "bucket": key,
                "bookingReviewed": 0,
                "deliveryReviewed": 0,
                "journeys": 0,
                "fieldChanges": 0,
                "outlets": [],
            },
        )
        marks = int(row["marks"])
        if row["stage_code"] == "BOOKING":
            bucket["bookingReviewed"] += marks
        else:
            bucket["deliveryReviewed"] += marks
        bucket["outlets"].append(
            {
                "outletId": str(row["outlet_id"]),
                "outletName": row["outlet_name"],
                "dealerName": row["dealer_name"],
                "stageCode": row["stage_code"],
                "marks": marks,
                "journeys": int(row["journeys"]),
            }
        )
    for row in changes:
        key = str(row["bucket"])
        bucket = buckets.setdefault(
            key,
            {
                "bucket": key,
                "bookingReviewed": 0,
                "deliveryReviewed": 0,
                "journeys": 0,
                "fieldChanges": 0,
                "outlets": [],
            },
        )
        bucket["fieldChanges"] = int(row["changes"])

    # Distinct journeys has to be its own query: a journey whose Booking and
    # Delivery were both marked appears in two rows above, so summing the
    # per-outlet counts would double it.
    for row in connection.execute(
        text(
            """
            SELECT
                CASE WHEN :grain = 'week'
                     THEN date_trunc('week', r.reviewed_at_utc AT TIME ZONE :tz)::date
                     ELSE (r.reviewed_at_utc AT TIME ZONE :tz)::date
                END AS bucket,
                count(DISTINCT r.journey_id) AS journeys
            FROM auditcore.p2_stage_document_reviews r
            WHERE r.tenant_id=:tenant_id
              AND r.reviewed_by_actor_id=:actor_id
              AND (r.reviewed_at_utc AT TIME ZONE :tz)::date
                  > (now() AT TIME ZONE :tz)::date - CAST(:window AS integer)
            GROUP BY 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "actor_id": access.actor_id,
            "tz": timezone_name,
            "grain": grain,
            "window": window,
        },
    ).mappings():
        bucket = buckets.get(str(row["bucket"]))
        if bucket is not None:
            bucket["journeys"] = int(row["journeys"])

    ordered = sorted(buckets.values(), key=lambda item: item["bucket"], reverse=True)

    return {
        "period": grain,
        "timezone": timezone_name,
        "actorId": access.actor_id,
        "buckets": ordered,
    }
