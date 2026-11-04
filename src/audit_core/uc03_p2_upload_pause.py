"""Uploads paused for maintenance (product owner, 2026-10-31, month end).

While bookings that hold other customers' files are corrected, PCs must not
upload documents; they still open bookings, read everything and work their
tasks. One switch, per Project or for all Projects (auditcore.p2_upload_pause,
migration 0181):

    GET /p2/v1/tenants/{tenant_id}/upload-pause   the signed-in user's Project:
                                                  {paused, message, untilUtc}
    GET /v1/admin/upload-pause                    SuperAdmin: every row
    PUT /v1/admin/upload-pause                    SuperAdmin: set or clear one
                                                  {tenantId|null, paused,
                                                   message, untilUtc}

While paused, the endpoints that START a new document upload refuse with 503
VAC-MAINT-001 (refuse_upload_if_paused): uploads:init, documents/{id}/replace,
POST /journeys (a P2 booking is only ever created by its first upload) and the
old booking-stage evidence upload. Everything else is untouched: reads,
vehicle photos, gate pass, tasks, field corrections, removing pages, retries
and re-reads, the old-screen split into pages, and the finalize of an upload
that was prepared before the pause (its file is already in storage), so the
worker finishes every file already received.

No row, or a row with paused = false, changes nothing.

A duplicate booking at two outlets of one dealership (product owner
2026-11-04): the PCs of both bookings may not start an upload until a Reviewer
deletes the duplicate (409 VAC-DUP-001). Nothing to clear by hand: once one of
the two bookings is deleted or closed, their uploads are back. The same GET
tells the signed-in PC (``duplicateBooking``).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.db import set_platform_super_admin_context, set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    get_human_principal,
    require_super_admin_request,
)
from audit_core.errors import (
    AuditCoreError,
    BusinessValidationError,
    NotFoundError,
    ValidationError,
)
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import check_p2_permission

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2"])
admin_router = APIRouter(tags=["system-health"])

UPLOAD_PAUSED_ERROR_CODE = "VAC-MAINT-001"
UPLOAD_PAUSED_TITLE = "Uploads are paused for maintenance"
DEFAULT_MESSAGE = "Uploads are paused for maintenance."
MESSAGE_MAX = 500
_READ_PERMISSION = "audit.journey.read"
_IST = ZoneInfo("Asia/Kolkata")


class UploadsPausedError(AuditCoreError):
    def __init__(self, *, detail: str) -> None:
        super().__init__(UPLOAD_PAUSED_ERROR_CODE, 503, UPLOAD_PAUSED_TITLE, detail)


def upload_pause(connection: Connection, tenant_id: str) -> dict[str, Any] | None:
    """The pause that covers ``tenant_id`` now, or None.

    Paused when the Project's own row or the all-Projects row (tenant_id
    NULL) says paused and its until_utc is unset or still ahead. When both
    do, the one that ends last is returned (an open-ended one first): uploads
    stay refused until every pause covering the Project has ended.
    The caller has set the Project's tenant context (row security)."""
    row = connection.execute(
        text(
            """
            SELECT tenant_id, message, until_utc
            FROM auditcore.p2_upload_pause
            WHERE paused
              AND (tenant_id IS NULL OR tenant_id = :tenant_id)
              AND (until_utc IS NULL OR until_utc > now())
            ORDER BY until_utc DESC NULLS FIRST, (tenant_id IS NULL)
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().first()
    if row is None:
        return None
    return {
        "scope": "ALL" if row["tenant_id"] is None else "PROJECT",
        "message": row["message"],
        "untilUtc": row["until_utc"],
    }


def ist_text(value: datetime) -> str:
    """'31 Oct 2026, 11:30 PM IST'."""
    local = value.astimezone(_IST)
    hour = local.hour % 12 or 12
    return f"{local.day} {local:%b} {local.year}, {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'} IST"


def pause_detail(pause: dict[str, Any]) -> str:
    """The refusal's detail: the SuperAdmin's message, and when uploads are back."""
    message = " ".join(str(pause.get("message") or "").split()) or DEFAULT_MESSAGE
    if message[-1] not in ".!?":
        message += "."
    until = pause.get("untilUtc")
    return f"{message} Uploads are back at {ist_text(until)}." if until else message


DUPLICATE_BLOCK_ERROR_CODE = "VAC-DUP-001"
DUPLICATE_BLOCK_TITLE = "Uploads paused: duplicate booking"


class DuplicateBookingBlockError(AuditCoreError):
    def __init__(self, *, detail: str) -> None:
        super().__init__(DUPLICATE_BLOCK_ERROR_CODE, 409, DUPLICATE_BLOCK_TITLE, detail)


def duplicate_block(connection: Connection, tenant_id: str, actor_id: str | None) -> dict[str, Any] | None:
    """The duplicate booking at two outlets of one dealership that pauses this
    person's uploads (they entered one of the two), while both bookings are
    still open; None otherwise (uc03_p2_same_file.SAME_DEALERSHIP_KIND)."""
    if not actor_id:
        return None
    from audit_core.uc03_p2_same_file import _CLOSED_SQL, SAME_DEALERSHIP_KIND

    row = connection.execute(
        text(
            f"""
            SELECT a.alert_id, a.payload
            FROM auditcore.p2_tenant_alerts a
            WHERE a.tenant_id = :t AND a.alert_kind = :kind
              AND (a.payload #>> '{{newJourney,pcActorId}}' = :a OR a.payload #>> '{{existingJourney,pcActorId}}' = :a)
              AND (SELECT count(*) FROM auditcore.journeys j
                   WHERE j.tenant_id = a.tenant_id AND j.journey_id IN (a.subject_journey_id, a.related_journey_id)
                     AND NOT {_CLOSED_SQL}) = 2
            ORDER BY a.created_at_utc
            LIMIT 1
            """
        ),
        {"t": tenant_id, "kind": SAME_DEALERSHIP_KIND, "a": actor_id},
    ).mappings().first()
    if row is None:
        return None
    payload = row["payload"] or {}
    return {"alertId": str(row["alert_id"]), "newJourney": payload.get("newJourney"),
            "existingJourney": payload.get("existingJourney"), "customerName": payload.get("customerName"),
            "model": payload.get("model")}


def duplicate_block_detail(block: dict[str, Any]) -> str:
    def named(journey: dict[str, Any] | None) -> str:
        journey = journey or {}
        place = journey.get("outletName") or journey.get("dealerName") or "another outlet"
        return f"{journey.get('reference') or 'a booking'} ({place})"

    return (f"{named(block.get('newJourney'))} and {named(block.get('existingJourney'))} are the same customer "
            f"({block.get('customerName') or 'unknown'}, {block.get('model') or 'unknown model'}) at two outlets. "
            "Contact the Reviewer to delete the duplicate booking; your uploads come back once it is deleted.")


def refuse_upload_if_paused(connection: Connection, tenant_id: str, actor_id: str | None = None) -> None:
    """Refuse a NEW document upload (503 VAC-MAINT-001) while uploads are
    paused, and (409 VAC-DUP-001) while a duplicate booking at two outlets
    of one dealership waits for a Reviewer (2026-11-04)."""
    pause = upload_pause(connection, tenant_id)
    if pause is not None:
        raise UploadsPausedError(detail=pause_detail(pause))
    block = duplicate_block(connection, tenant_id, actor_id)
    if block is not None:
        raise DuplicateBookingBlockError(detail=duplicate_block_detail(block))


# ── the signed-in user's Project ──────────────────────────────────────────────
class UploadPauseStatus(BaseModel):
    paused: bool
    message: str | None
    untilUtc: datetime | None
    # 2026-11-04: this person's uploads wait for a Reviewer to delete a duplicate booking
    duplicateBooking: dict[str, Any] | None = None


@router.get("/upload-pause", response_model=UploadPauseStatus)
def get_upload_pause(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> UploadPauseStatus:
    """Whether uploads are paused for this Project now. Any member may read it
    (the journey-read permission every operating role holds); one small read."""
    check_p2_permission(
        tenant_id=tenant_id, human_principal=human_principal,
        authorization_client=authorization_client, permission_key=_READ_PERMISSION,
    )
    set_tenant_context(connection, tenant_id)
    pause = upload_pause(connection, tenant_id)
    if pause is not None:
        return UploadPauseStatus(paused=True, message=pause["message"], untilUtc=pause["untilUtc"])
    block = duplicate_block(connection, tenant_id, human_principal.subject)
    if block is not None:
        return UploadPauseStatus(paused=True, message=duplicate_block_detail(block), untilUtc=None,
                                 duplicateBooking=block)
    return UploadPauseStatus(paused=False, message=None, untilUtc=None)


# ── SuperAdmin ────────────────────────────────────────────────────────────────
class UploadPauseCommand(BaseModel):
    tenantId: str | None = Field(default=None, max_length=128)
    paused: bool
    message: str | None = Field(default=None, max_length=MESSAGE_MAX)
    untilUtc: datetime | None = None


def _rows(connection: Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT p.tenant_id, pr.project_name, p.paused, p.message, p.until_utc,
                   p.set_by_actor_id, p.set_at_utc,
                   (p.paused AND (p.until_utc IS NULL OR p.until_utc > now())) AS active
            FROM auditcore.p2_upload_pause p
            LEFT JOIN auditcore.projects pr ON pr.tenant_id = p.tenant_id
            ORDER BY (p.tenant_id IS NOT NULL), pr.project_name, p.tenant_id
            """
        )
    ).mappings().all()
    return [
        {
            "tenantId": r["tenant_id"],
            "projectName": r["project_name"],
            "paused": bool(r["paused"]),
            "active": bool(r["active"]),
            "message": r["message"],
            "untilUtc": r["until_utc"],
            "setByActorId": r["set_by_actor_id"],
            "setAtUtc": r["set_at_utc"],
        }
        for r in rows
    ]


@admin_router.get("/v1/admin/upload-pause")
def list_upload_pauses(
    _admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    set_platform_super_admin_context(connection)
    now = connection.execute(text("SELECT now()")).scalar_one()
    return {"items": _rows(connection), "serverTimeUtc": now}


@admin_router.put("/v1/admin/upload-pause")
def set_upload_pause(
    command: UploadPauseCommand,
    admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Pause or resume uploads for one Project (tenantId) or all (null)."""
    tenant_id = (command.tenantId or "").strip() or None
    message = " ".join((command.message or "").split()) or None
    until = command.untilUtc if command.paused else None
    if until is not None and until.tzinfo is None:
        raise ValidationError(detail="untilUtc must carry a time zone (for example 2026-10-31T23:30:00+05:30).")
    set_platform_super_admin_context(connection)
    now = connection.execute(text("SELECT now()")).scalar_one()
    if until is not None and until <= now:
        raise BusinessValidationError(detail="The time uploads are back has already passed. Choose a later time.")
    if tenant_id is not None and connection.execute(
        text("SELECT 1 FROM auditcore.projects WHERE tenant_id = :t"), {"t": tenant_id}
    ).first() is None:
        raise NotFoundError(error_code="VAC-NF-000", title="Not found", detail=f"Project {tenant_id} was not found.")
    previous = connection.execute(
        text("SELECT paused, until_utc FROM auditcore.p2_upload_pause WHERE COALESCE(tenant_id, '*') = :scope"),
        {"scope": tenant_id or "*"},
    ).mappings().first()
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_upload_pause (
                tenant_id, paused, message, until_utc, set_by_actor_id, set_at_utc
            ) VALUES (:tenant_id, :paused, :message, :until, :actor, now())
            ON CONFLICT ((COALESCE(tenant_id, '*'))) DO UPDATE
            SET paused = EXCLUDED.paused, message = EXCLUDED.message, until_utc = EXCLUDED.until_utc,
                set_by_actor_id = EXCLUDED.set_by_actor_id, set_at_utc = now()
            """
        ),
        {"tenant_id": tenant_id, "paused": command.paused, "message": message, "until": until,
         "actor": admin.user_id},
    )
    logger.info(
        "upload_pause_changed",
        scope=tenant_id or "ALL",
        paused=command.paused,
        until_utc=until.astimezone(UTC).isoformat() if until else None,
        message=message,
        previous_paused=None if previous is None else bool(previous["paused"]),
        previous_until_utc=(
            previous["until_utc"].isoformat() if previous is not None and previous["until_utc"] else None
        ),
        actor_id=admin.user_id,
    )
    return {"items": _rows(connection), "serverTimeUtc": now}
