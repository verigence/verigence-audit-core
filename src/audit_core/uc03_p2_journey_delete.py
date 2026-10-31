"""A Team Lead deletes a Phase 2 journey (product owner, 2026-10-23).

Who and when
    Only a Team Lead (the caller's operating role on that journey) and only
    for a journey no reviewer has marked reviewed and that is not closed.
    The reason is mandatory.

What is removed
    1. Document Intelligence: every document the journey sent (pages and
       whole files), through the service token -- the same call the PC's own
       "remove document" uses, which also removes DI's stored file.
    2. R2: the original uploads, the page files and the vehicle photos, by
       the keys the journey recorded and by the journey's two key prefixes.
    3. Audit Core: the journey and everything tied to it, through the same
       definer function the SuperAdmin purge uses.

What stays
    One row in ``p2_journey_deletions``: the journey's basic details as they
    stood, who deleted it, when and why, and how many documents and files went.

Order
    The record is written first and committed, so an attempt that fails half
    way is still on file (status FAILED, with what failed). The external
    deletes are repeatable -- a document or file already gone is not an
    error -- so the Team Lead simply deletes again. The database delete is
    one transaction, last.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import OperationalError

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_engine, get_human_principal
from audit_core.di_capture_v2_client import DiCaptureV2Client, DiCaptureV2Error
from audit_core.errors import (
    BusinessValidationError,
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
)
from audit_core.evidence import _external_context_ref
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient
from audit_core.uc03_document_capture_v2 import (
    get_di_capture_v2_client,
    get_security_oauth_client,
)
from audit_core.uc03_p2_access import P2AccessContext, authorize_p2_any
from audit_core.uc03_p2_object_keys import journey_folder_of
from audit_core.uc03_p2_storage import (
    P2DocumentStorage,
    P2DocumentStorageError,
    get_p2_document_storage,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2"])

_DI_AUDIENCE = "di"
# The Team Lead's own bundle carries audit.work.update (not audit.journey.update);
# either opens the door, the operating-role check below is what limits it to a TL.
_PERMISSION_KEYS = ("audit.work.update", "audit.journey.update")
MIN_REASON_LENGTH = 10


class DeleteJourneyCommand(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class DeleteJourneyResult(BaseModel):
    deletionId: UUID
    journeyId: UUID
    journeyReference: str | None
    diDocumentsDeleted: int
    storageObjectsDeleted: int


def get_delete_storage() -> P2DocumentStorage:
    try:
        return get_p2_document_storage()
    except RuntimeError as exc:
        raise DependencyUnavailableError(
            detail="Document storage is not available, so the journey was not deleted."
        ) from exc


_SNAPSHOT_SQL = """
    SELECT j.customer_id, j.journey_reference, COALESCE(c.legal_name, c.display_name) AS customer_name,
           j.dealer_id, d.dealer_name, j.outlet_id, o.outlet_name, o.outlet_code,
           j.created_by_actor_id AS pc_actor_id, j.created_by_display_name AS pc_name,
           NULLIF(concat_ws(' · ',
             NULLIF(jp.model_name_snapshot, ''),
             NULLIF(jp.variant_name_snapshot, ''),
             NULLIF(jp.colour_name_snapshot, '')), '') AS vehicle,
           pr.current_stage, j.created_at_utc,
           EXISTS (
             SELECT 1 FROM auditcore.journey_stage_states st
             WHERE st.tenant_id=j.tenant_id AND st.journey_id=j.journey_id
               AND ((st.stage_code='DELIVERY' AND st.business_completed_at_utc IS NOT NULL)
                    OR (st.stage_code='BOOKING'
                        AND (st.business_status IN ('BOOKING_CANCELLED','DUPLICATE_BOOKING')
                             OR st.closure_disposition='NO_DELIVERY')))
           ) AS is_closed
    FROM auditcore.journeys j
    JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
    JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
    JOIN auditcore.dealer_outlets o
      ON o.tenant_id=j.tenant_id AND o.dealer_id=j.dealer_id AND o.outlet_id=j.outlet_id
    LEFT JOIN auditcore.journey_products jp ON jp.tenant_id=j.tenant_id AND jp.journey_id=j.journey_id
    LEFT JOIN auditcore.p2_journey_runtime pr ON pr.tenant_id=j.tenant_id AND pr.journey_id=j.journey_id
    WHERE j.tenant_id=:tenant_id AND j.journey_id=:journey_id
    FOR UPDATE OF j
"""

_DI_DOCUMENTS_SQL = """
    SELECT DISTINCT doc FROM (
        SELECT CAST(di_document_id AS uuid) AS doc FROM auditcore.document_capture_v2_documents
          WHERE tenant_id=:tenant_id AND journey_id=:journey_id
        UNION
        SELECT CAST(di_document_id AS uuid) FROM auditcore.p2_document_queue
          WHERE tenant_id=:tenant_id AND journey_id=:journey_id
        UNION
        SELECT CAST(di_document_id AS uuid) FROM auditcore.evidence
          WHERE tenant_id=:tenant_id AND journey_id=:journey_id
        UNION
        SELECT CAST(di_document_id AS uuid) FROM auditcore.evidence_ingestion_operations
          WHERE tenant_id=:tenant_id AND journey_id=:journey_id
    ) refs
    WHERE doc IS NOT NULL
    ORDER BY doc
"""

_OBJECT_KEYS_SQL = """
    SELECT original_object_key AS object_key FROM auditcore.p2_upload_batches
      WHERE tenant_id=:tenant_id AND journey_id=:journey_id
    UNION
    SELECT page_object_key FROM auditcore.p2_document_queue
      WHERE tenant_id=:tenant_id AND journey_id=:journey_id
    UNION
    SELECT object_key FROM auditcore.delivery_vehicle_photos
      WHERE tenant_id=:tenant_id AND journey_id=:journey_id
"""


def storage_prefixes(tenant_id: str, journey_id: UUID) -> tuple[str, str]:
    """The two places Audit Core writes a journey's files (uploads and pages;
    vehicle photos). Both end with "/" so one journey's prefix never matches another's."""
    return (
        f"p2-documents/{tenant_id}/{journey_id}/",
        f"{tenant_id}/p2-vehicle-photos/{journey_id}/",
    )


def _check_and_record(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    reason: str,
    correlation_id: str,
    actor_id: str,
    role: str,
    skip_guards: bool = False,
) -> dict[str, Any]:
    """Check the journey may be deleted, collect what to remove and write the
    record, on a connection whose tenant context is set. The same guards for a
    Team Lead and for the system (2026-10-31): never a reviewed or closed journey.
    ``skip_guards`` only to finish a system deletion already under way."""
    row = connection.execute(
        text(_SNAPSHOT_SQL), {"tenant_id": tenant_id, "journey_id": journey_id}
    ).mappings().first()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Journey not found",
            detail="Journey not found in your current Project scope.",
        )
    reviewed = connection.execute(
        text(
            "SELECT stage_code FROM auditcore.p2_stage_document_reviews "
            "WHERE tenant_id=:tenant_id AND journey_id=:journey_id ORDER BY stage_code"
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalars().all()
    if reviewed and not skip_guards:
        stages = " and ".join(str(stage).title() for stage in reviewed)
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Journey already reviewed",
            detail=(
                f"The {stages} documents of this journey are marked reviewed, so it can "
                "no longer be deleted."
            ),
        )
    if row["is_closed"] and not skip_guards:
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Journey is closed",
            detail="A closed journey cannot be deleted.",
        )
    params = {"tenant_id": tenant_id, "journey_id": journey_id}
    document_ids = [
        UUID(str(value)) for value in connection.execute(text(_DI_DOCUMENTS_SQL), params).scalars().all()
    ]
    object_keys = [
        str(value) for value in connection.execute(text(_OBJECT_KEYS_SQL), params).scalars().all() if value
    ]
    deletion_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_journey_deletions (
                tenant_id, journey_id, journey_reference, customer_name,
                dealer_id, dealer_name, outlet_id, outlet_name, outlet_code,
                pc_actor_id, pc_name, vehicle, current_stage, journey_created_at_utc,
                document_count, reason, deleted_by_actor_id, deleted_by_role, correlation_id
            ) VALUES (
                :tenant_id, :journey_id, :journey_reference, :customer_name,
                :dealer_id, :dealer_name, :outlet_id, :outlet_name, :outlet_code,
                :pc_actor_id, :pc_name, :vehicle, :current_stage, :created_at,
                :document_count, :reason, :actor_id, :role, :correlation_id
            ) RETURNING deletion_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "journey_reference": row["journey_reference"],
            "customer_name": row["customer_name"],
            "dealer_id": row["dealer_id"],
            "dealer_name": row["dealer_name"],
            "outlet_id": row["outlet_id"],
            "outlet_name": row["outlet_name"],
            "outlet_code": row["outlet_code"],
            "pc_actor_id": row["pc_actor_id"],
            "pc_name": row["pc_name"],
            "vehicle": row["vehicle"],
            "current_stage": row["current_stage"],
            "created_at": row["created_at_utc"],
            "document_count": len(document_ids),
            "reason": reason,
            "actor_id": actor_id,
            "role": role,
            "correlation_id": correlation_id,
        },
    ).scalar_one()
    return {
        "deletion_id": UUID(str(deletion_id)),
        "customer_id": UUID(str(row["customer_id"])),
        "journey_reference": row["journey_reference"],
        "document_ids": document_ids,
        "object_keys": object_keys,
    }


def _start(
    engine: Engine,
    *,
    tenant_id: str,
    journey_id: UUID,
    reason: str,
    correlation_id: str,
    human_principal: HumanPrincipal,
    authorization_client: SecurityAuthorizationClient,
) -> dict[str, Any]:
    """Authorize, check the journey may be deleted, collect what to remove and
    write the record. Commits before anything external is touched."""
    with engine.begin() as connection:
        access: P2AccessContext = authorize_p2_any(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            human_principal=human_principal,
            authorization_client=authorization_client,
            permission_keys=_PERMISSION_KEYS,
        )
        if (access.operating_role or "").upper() != "TL":
            raise AuthorizationError(
                error_code="VAC-AUTH-002",
                status_code=403,
                title="Only a Team Lead can delete a journey",
            )
        return _check_and_record(
            connection, tenant_id=tenant_id, journey_id=journey_id, reason=reason,
            correlation_id=correlation_id, actor_id=access.actor_id, role="TL",
        )


def _finish(
    engine: Engine,
    *,
    tenant_id: str,
    deletion_id: UUID,
    status: str,
    di_documents: int,
    objects: int,
    detail: str | None,
    connection: Connection | None = None,
) -> None:
    sql = text(
        """
        UPDATE auditcore.p2_journey_deletions
        SET deletion_status=:status, di_documents_deleted=:di, r2_objects_deleted=:objs,
            failure_detail=:detail, completed_at_utc=now()
        WHERE tenant_id=:tenant_id AND deletion_id=:deletion_id
        """
    )
    params = {
        "status": status, "di": di_documents, "objs": objects, "detail": detail,
        "tenant_id": tenant_id, "deletion_id": deletion_id,
    }
    if connection is not None:
        connection.execute(sql, params)
        return
    with engine.begin() as own:
        set_tenant_context(own, tenant_id)
        own.execute(sql, params)


def _fail(
    engine: Engine, *, tenant_id: str, deletion_id: UUID, di_documents: int, objects: int, detail: str
) -> DependencyUnavailableError:
    _finish(
        engine, tenant_id=tenant_id, deletion_id=deletion_id, status="FAILED",
        di_documents=di_documents, objects=objects, detail=detail,
    )
    return DependencyUnavailableError(
        detail=(
            f"{detail} The journey was not deleted; try again. "
            "Anything already removed stays removed."
        )
    )


def _delete_di_documents(
    *,
    tenant_id: str,
    journey_id: UUID,
    customer_id: UUID,
    document_ids: list[UUID],
    security_client: SecurityOAuthClient,
    v2_client: DiCaptureV2Client,
) -> int:
    """Returns how many documents were removed or already gone. Raises
    DiCaptureV2Error on the first real failure."""
    if not document_ids:
        return 0
    context_ref = _external_context_ref(journey_id=journey_id, customer_id=customer_id)
    token = security_client.get_service_token(audience=_DI_AUDIENCE)
    done = 0
    for document_id in document_ids:
        try:
            v2_client.delete_document(
                token=token,
                tenant_id=tenant_id,
                external_context_ref=context_ref,
                document_id=str(document_id),
            )
        except DiCaptureV2Error as exc:
            if exc.status_code != 404:  # 404: DI never had it, or it is already gone
                raise
        done += 1
    return done


@router.post("/journeys/{journey_id}/delete", response_model=DeleteJourneyResult)
def delete_journey(
    tenant_id: str,
    journey_id: UUID,
    command: DeleteJourneyCommand,
    request: Request,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    v2_client: Annotated[DiCaptureV2Client, Depends(get_di_capture_v2_client)],
    storage: Annotated[P2DocumentStorage, Depends(get_delete_storage)],
) -> DeleteJourneyResult:
    reason = command.reason.strip()
    if len(reason) < MIN_REASON_LENGTH:
        raise BusinessValidationError(
            detail=f"Give the reason for deleting this journey (at least {MIN_REASON_LENGTH} characters)."
        )

    started = _start(
        engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        reason=reason,
        correlation_id=get_correlation_id(request),
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    di_done, object_count = _remove(
        engine, tenant_id=tenant_id, journey_id=journey_id, started=started,
        security_client=security_client, v2_client=v2_client, storage=storage,
    )
    return DeleteJourneyResult(
        deletionId=started["deletion_id"],
        journeyId=journey_id,
        journeyReference=started["journey_reference"],
        diDocumentsDeleted=di_done,
        storageObjectsDeleted=object_count,
    )


class JourneyWorkRunning(RuntimeError):
    """The worker is still running one of the journey's items."""


# How long the database delete waits for the journey's running work to finish
# (it gets no new work once the deletion has started: the worker's claim
# skips it). A check or a recompute takes seconds.
_WORK_WAIT_ATTEMPTS = int(os.environ.get("P2_DELETE_WORK_WAIT_ATTEMPTS", "20"))
_WORK_WAIT_SECONDS = float(os.environ.get("P2_DELETE_WORK_WAIT_SECONDS", "1.5"))
# The worker's own item that drives a same-file delete is running by design.
_OWN_WORK_TYPE = "SAME_FILE_DELETE"

_RUNNING_WORK_SQL = """
    SELECT 1 FROM auditcore.p2_work_queue
    WHERE tenant_id=:t AND journey_id=:j AND work_type <> :own
      AND work_status IN ('CLAIMED','PROCESSING') AND lease_expires_at_utc > now()
    LIMIT 1
"""


def _hold_journey_work(connection: Connection, *, tenant_id: str, journey_id: UUID) -> bool:
    """Inside the delete's own transaction: True once none of the journey's
    work is running and its queue rows are locked, so no claim can start one
    until the delete commits (the claim skips locked rows).

    Found live 2026-10-31: the duplicate purge deleted journey 74d751db while
    the worker was still running its NATIVE:BOOKING check. The check had
    written finding events and then waited, at its payment-match upsert, on a
    row the delete had already removed; the delete (hard_delete_journey_
    transactions disables the audit tables' append-only triggers, which takes
    ShareRowExclusiveLock) waited for audit_finding_events, which the check's
    insert held -- a deadlock, and the check was killed. NOWAIT: the delete never
    waits on a row lock here, so it cannot close a cycle of its own."""
    params = {"t": tenant_id, "j": journey_id, "own": _OWN_WORK_TYPE}
    if connection.execute(text(_RUNNING_WORK_SQL), params).first() is not None:
        return False
    try:
        with connection.begin_nested():
            connection.execute(
                text(
                    "SELECT work_id FROM auditcore.p2_work_queue "
                    "WHERE tenant_id=:t AND journey_id=:j AND work_type <> :own FOR UPDATE NOWAIT"
                ),
                params,
            )
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55P03":  # lock_not_available: being claimed or settled
            return False
        raise
    # A claim that committed before the lock: its item is running now.
    return connection.execute(text(_RUNNING_WORK_SQL), params).first() is None


@contextmanager
def _journey_work_held(engine: Engine, *, tenant_id: str, journey_id: UUID) -> Iterator[Connection]:
    """A transaction in which none of the journey's work runs, waiting a
    little for running work to finish. Raises JourneyWorkRunning."""
    for attempt in range(max(1, _WORK_WAIT_ATTEMPTS)):
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            if _hold_journey_work(connection, tenant_id=tenant_id, journey_id=journey_id):
                yield connection
                return
        if attempt + 1 < _WORK_WAIT_ATTEMPTS:
            time.sleep(_WORK_WAIT_SECONDS)
    raise JourneyWorkRunning(f"Work on journey {journey_id} is still running")


def _remove(
    engine: Engine,
    *,
    tenant_id: str,
    journey_id: UUID,
    started: dict[str, Any],
    security_client: SecurityOAuthClient,
    v2_client: DiCaptureV2Client,
    storage: P2DocumentStorage,
    on_deleted: Callable[[Connection], None] | None = None,
) -> tuple[int, int]:
    """Document Intelligence, R2, then the database, each outside any other
    open transaction. ``on_deleted`` runs inside the database delete's own
    transaction (the same-file alert is published with it)."""
    deletion_id: UUID = started["deletion_id"]

    di_done = 0
    try:
        di_done = _delete_di_documents(
            tenant_id=tenant_id,
            journey_id=journey_id,
            customer_id=started["customer_id"],
            document_ids=started["document_ids"],
            security_client=security_client,
            v2_client=v2_client,
        )
    except Exception as exc:
        logger.error("p2_journey_delete_di_failed", tenant_id=tenant_id, journey_id=str(journey_id), error=str(exc))
        raise _fail(
            engine, tenant_id=tenant_id, deletion_id=deletion_id, di_documents=di_done, objects=0,
            detail="Document Intelligence could not remove the journey's documents.",
        ) from exc

    object_count = 0
    try:
        keys = set(started["object_keys"])
        for prefix in storage_prefixes(tenant_id, journey_id):
            keys.update(storage.list_keys(prefix))
        # Uploads made since 2026-10-26 sit under a date folder, not directly
        # under the journey: sweep the journey's folder on each upload day too,
        # for anything the database does not list.
        for folder in sorted({f for k in started["object_keys"] if (f := journey_folder_of(k, journey_id))}):
            keys.update(storage.list_keys(folder))
        object_count = storage.delete_objects(sorted(keys))
    except (P2DocumentStorageError, ValueError) as exc:
        logger.error("p2_journey_delete_r2_failed", tenant_id=tenant_id, journey_id=str(journey_id), error=str(exc))
        raise _fail(
            engine, tenant_id=tenant_id, deletion_id=deletion_id, di_documents=di_done, objects=0,
            detail="Document storage could not remove the journey's files.",
        ) from exc

    try:
        with _journey_work_held(engine, tenant_id=tenant_id, journey_id=journey_id) as connection:
            receipt = connection.execute(
                text(
                    "SELECT auditcore.hard_delete_journey_transactions("
                    ":tenant_id, CAST(:journey_ids AS uuid[]))"
                ),
                {"tenant_id": tenant_id, "journey_ids": [journey_id]},
            ).scalar_one()
            if not isinstance(receipt, dict) or int(receipt.get("deletedJourneys", 0)) != 1:
                raise RuntimeError("The journey was not removed from Audit Core")
            _finish(
                engine, tenant_id=tenant_id, deletion_id=deletion_id, status="COMPLETED",
                di_documents=di_done, objects=object_count, detail=None, connection=connection,
            )
            if on_deleted is not None:
                on_deleted(connection)
            # A same-file alert still waiting on this journey's delete goes
            # out now (2026-10-31): its delete item went with the journey.
            from audit_core.uc03_p2_same_file import close_pending_alert

            close_pending_alert(connection, tenant_id=tenant_id, journey_id=journey_id)
    except JourneyWorkRunning as exc:
        logger.warning("p2_journey_delete_work_running", tenant_id=tenant_id, journey_id=str(journey_id))
        raise _fail(
            engine, tenant_id=tenant_id, deletion_id=deletion_id, di_documents=di_done, objects=object_count,
            detail="The journey's checks were still running, so its records were not removed yet.",
        ) from exc
    except Exception as exc:
        logger.error("p2_journey_delete_db_failed", tenant_id=tenant_id, journey_id=str(journey_id), error=str(exc))
        raise _fail(
            engine, tenant_id=tenant_id, deletion_id=deletion_id, di_documents=di_done, objects=object_count,
            detail="Audit Core could not remove the journey's records.",
        ) from exc

    logger.info(
        "p2_journey_deleted",
        tenant_id=tenant_id,
        journey_id=str(journey_id),
        deletion_id=str(deletion_id),
        di_documents=di_done,
        storage_objects=object_count,
    )
    return di_done, object_count


# ── the system deletes a journey (2026-10-31) ─────────────────────────────────
# Product owner: the same file uploaded again (same PAN, mobile, customer name
# and model as an earlier journey) is deleted completely, and the journeys he
# listed as duplicates are removed. No person presses the button: the record
# names SYSTEM as who deleted it and why. The guards stay -- a journey whose
# documents a reviewer marked reviewed, or a closed one, is never deleted
# (ConflictError, for the caller to report).
SYSTEM_ACTOR = "SYSTEM"
SYSTEM_ROLE = "SYSTEM"


def system_delete_journey(
    engine: Engine,
    *,
    tenant_id: str,
    journey_id: UUID,
    reason: str,
    correlation_id: str,
    security_client: SecurityOAuthClient,
    v2_client: DiCaptureV2Client,
    storage: P2DocumentStorage,
    on_deleted: Callable[[Connection], None] | None = None,
    before_record: Callable[[Connection, bool], None] | None = None,
) -> DeleteJourneyResult:
    """Delete a journey with no human principal. Must not be called inside an
    open transaction: Document Intelligence and R2 are called between the
    record (committed first) and the database delete (its own transaction).
    ``before_record(connection, continuing)`` runs in the record's transaction
    first (the worker checks it still holds its work item, and re-checks the
    match, there); it may raise to stop. Raises NotFoundError, ConflictError
    (reviewed / closed) or DependencyUnavailableError.

    An earlier system attempt on this journey that did not finish (STARTED,
    or FAILED part way -- some of its Document Intelligence documents or files
    may already be gone) is continued without the reviewed / closed guards
    (2026-10-31 review): a journey is never left half deleted."""
    reason = reason.strip()
    if len(reason) < MIN_REASON_LENGTH:
        raise ValueError("A system deletion needs its reason.")
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        unfinished = connection.execute(
            text(
                "SELECT deletion_id, deletion_status FROM auditcore.p2_journey_deletions "
                "WHERE tenant_id=:t AND journey_id=:j AND deleted_by_role=:role "
                "AND deletion_status IN ('STARTED','FAILED') ORDER BY requested_at_utc DESC LIMIT 1"
            ),
            {"t": tenant_id, "j": journey_id, "role": SYSTEM_ROLE},
        ).mappings().first()
        continuing = unfinished is not None
        if before_record is not None:
            before_record(connection, continuing)
        if continuing:
            logger.warning(
                "p2_journey_delete_continued", tenant_id=tenant_id, journey_id=str(journey_id),
                earlier_deletion_id=str(unfinished["deletion_id"]), earlier_status=unfinished["deletion_status"],
            )
        started = _check_and_record(
            connection, tenant_id=tenant_id, journey_id=journey_id, reason=reason[:2000],
            correlation_id=correlation_id, actor_id=SYSTEM_ACTOR, role=SYSTEM_ROLE, skip_guards=continuing,
        )
    di_done, object_count = _remove(
        engine, tenant_id=tenant_id, journey_id=journey_id, started=started,
        security_client=security_client, v2_client=v2_client, storage=storage, on_deleted=on_deleted,
    )
    # A page written to R2 while the deletion ran (an upload still finishing)
    # is not left behind: the journey's folders are swept once more. Best
    # effort -- the journey itself is already gone.
    try:
        keys: set[str] = set()
        for prefix in storage_prefixes(tenant_id, journey_id):
            keys.update(storage.list_keys(prefix))
        for folder in sorted({f for k in started["object_keys"] if (f := journey_folder_of(k, journey_id))}):
            keys.update(storage.list_keys(folder))
        if keys:
            swept = storage.delete_objects(sorted(keys))
            object_count += swept
            logger.info("p2_journey_delete_late_files_removed", tenant_id=tenant_id,
                        journey_id=str(journey_id), files=swept)
    except Exception as exc:  # noqa: BLE001 - the journey is deleted; a stray file is only logged
        logger.warning("p2_journey_delete_late_sweep_failed", tenant_id=tenant_id, journey_id=str(journey_id),
                       error=str(exc))
    return DeleteJourneyResult(
        deletionId=started["deletion_id"],
        journeyId=journey_id,
        journeyReference=started["journey_reference"],
        diDocumentsDeleted=di_done,
        storageObjectsDeleted=object_count,
    )
