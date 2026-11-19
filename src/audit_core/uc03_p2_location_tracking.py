"""The phone's location in the background, from check-in to check-out
(product owner, 2026-11-16).

"can you pls do offline check for the location on the mobile device ... though
app may be in background or may be killed.. some way to collect the data".
Decided the same day: everyone who marks attendance on the phone is tracked,
from their myHR check-in to their check-out, within 10:00-19:30 IST. Changed on
2026-11-19 ("what is problem to continue reading till the time check out doesnt
happen... pls change"): it runs until check-out, however late; a check-out that
never comes stops with the day.

The Android app starts a tracking service when the person checks in. The service
keeps running when the app is put in the background or swiped away, reads the
location every TRACKING_INTERVAL_MINUTES, keeps each reading on the phone when
there is no network, and sends what it holds whenever it can. It stops at
check-out, at the end of the day, or when the server says the key is no longer active.

The service cannot use the person's sign-in token: it runs while the app is
closed, when the token cannot be refreshed. So at check-in the app asks for a
tracking key:

    POST /p2/v1/location-tracking:start              (signed in)  a key for today
    POST /p2/v1/location-tracking:stop               (signed in)  check-out: stop
    POST /p2/v1/tenants/{tenant_id}/location-tracking/checks
                                                     (X-Location-Key) the readings

A key adds location readings for one person in one Project and nothing else. A
reading counts only when it was read before the key was stopped and within the
day; the key itself is accepted for QUEUED_MAX_AGE after the day ends, so
readings kept on an offline phone can still be sent. Only the key's SHA-256 is
stored. Readings land in ``p2_location_checks`` as BACKGROUND checks, judged
against the person's outlets in any role (uc03_p2_location_checks).
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.db import set_security_actor_context, set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.uc03_p2_location_checks import (
    QUEUED_MAX_AGE,
    TRACKING_DAY_END,
    TRACKING_INTERVAL_MINUTES,
    WINDOW_START,
    LocationCheckCommand,
    LocationCheckResult,
    check_moment,
    in_tracking_hours,
    record_location_check,
)
from audit_core.uc03_p2_object_keys import IST

router = APIRouter(prefix="/p2/v1", tags=["uc03-phase2-location-tracking"])

KEY_HEADER = "X-Location-Key"
MAX_READINGS_PER_SEND = 50
_ACTIVE = """
    ba.assignment_status = 'ACTIVE' AND ba.effective_from <= now()
    AND (ba.effective_to IS NULL OR ba.effective_to >= now())
"""


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def tracking_until(created: datetime) -> datetime:
    """When a key made at ``created`` stops by itself: the end of that day (IST). Check-out stops
    it before that; this is only for a check-out that never comes."""
    local = created.astimezone(IST)
    return (datetime.combine(local.date(), time(0, 0), IST) + timedelta(days=1)).astimezone(UTC)


def my_projects(connection: Connection, *, actor_id: str) -> list[dict[str, Any]]:
    """The active Projects the person holds an active assignment in, by name."""
    set_security_actor_context(connection, actor_id)
    rows = connection.execute(
        text(
            f"""
            SELECT DISTINCT p.tenant_id, p.project_name
            FROM auditcore.business_assignments ba
            JOIN auditcore.projects p ON p.tenant_id = ba.tenant_id
            WHERE ba.security_actor_id = :a AND p.project_status = 'ACTIVE' AND {_ACTIVE}
            ORDER BY p.project_name, p.tenant_id
            """
        ),
        {"a": actor_id},
    ).mappings().all()
    return [dict(row) for row in rows]


class TrackingStartCommand(BaseModel):
    # The Project the app has selected, if any; else the person's first Project.
    tenantId: str | None = Field(default=None, max_length=128)
    deviceType: Literal["ANDROID", "IOS"] = "ANDROID"


class TrackingStart(BaseModel):
    tracking: bool
    # Why not: NO_PROJECT (no Project to log against) or WINDOW_OVER (the day is over).
    reason: str | None = None
    tenantId: str | None = None
    projectName: str | None = None
    key: str | None = None
    trackingUntilUtc: datetime | None = None
    sendUntilUtc: datetime | None = None
    intervalMinutes: int = TRACKING_INTERVAL_MINUTES
    # The hours the phone reads in: from the window's start to the end of the day (check-out
    # stops it). The phone takes them from here, so released apps follow without an update.
    window: dict[str, str] = {"from": WINDOW_START.strftime("%H:%M"), "to": TRACKING_DAY_END}
    path: str | None = None


def _revoke(connection: Connection, *, tenant_id: str, actor_id: str, now: datetime) -> int:
    return connection.execute(
        text(
            "UPDATE auditcore.p2_location_tracking_keys SET revoked_at_utc = :now "
            "WHERE tenant_id = :t AND actor_id = :a AND revoked_at_utc IS NULL AND expires_at_utc > :now"
        ),
        {"t": tenant_id, "a": actor_id, "now": now},
    ).rowcount


def start_tracking(
    connection: Connection, *, actor_id: str, command: TrackingStartCommand, now: datetime | None = None,
) -> TrackingStart:
    now = now or connection.execute(text("SELECT now()")).scalar_one()
    projects = my_projects(connection, actor_id=actor_id)
    if not projects:
        return TrackingStart(tracking=False, reason="NO_PROJECT")
    chosen = next((p for p in projects if p["tenant_id"] == command.tenantId), projects[0])
    until = tracking_until(now)
    if now >= until:
        return TrackingStart(tracking=False, reason="WINDOW_OVER", tenantId=chosen["tenant_id"],
                             projectName=chosen["project_name"])
    tenant_id = str(chosen["tenant_id"])
    set_tenant_context(connection, tenant_id)
    # One phone at a time: a new key (a check-in on another phone) stops the old one.
    _revoke(connection, tenant_id=tenant_id, actor_id=actor_id, now=now)
    key = secrets.token_urlsafe(32)
    send_until = until + QUEUED_MAX_AGE
    connection.execute(
        text(
            "INSERT INTO auditcore.p2_location_tracking_keys "
            "(tenant_id, actor_id, key_hash, device_type, created_at_utc, expires_at_utc) "
            "VALUES (:t, :a, :h, :d, :now, :exp)"
        ),
        {"t": tenant_id, "a": actor_id, "h": key_hash(key), "d": command.deviceType, "now": now,
         "exp": send_until},
    )
    return TrackingStart(
        tracking=True, tenantId=tenant_id, projectName=chosen["project_name"], key=key,
        trackingUntilUtc=until, sendUntilUtc=send_until,
        path=f"/p2/v1/tenants/{tenant_id}/location-tracking/checks",
    )


@router.post("/location-tracking:start", response_model=TrackingStart)
def post_tracking_start(
    command: TrackingStartCommand,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> TrackingStart:
    """Check-in on the phone: a tracking key for today (everyone who marks attendance)."""
    return start_tracking(connection, actor_id=human_principal.subject, command=command)


class TrackingStopCommand(BaseModel):
    tenantId: str | None = Field(default=None, max_length=128)


@router.post("/location-tracking:stop")
def post_tracking_stop(
    command: TrackingStopCommand,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Check-out: every key of the person stops (readings made before now still count)."""
    now = connection.execute(text("SELECT now()")).scalar_one()
    stopped = 0
    for project in my_projects(connection, actor_id=human_principal.subject):
        tenant_id = str(project["tenant_id"])
        if command.tenantId and tenant_id != command.tenantId:
            continue
        set_tenant_context(connection, tenant_id)
        stopped += _revoke(connection, tenant_id=tenant_id, actor_id=human_principal.subject, now=now)
    return {"stopped": stopped}


class TrackingReadings(BaseModel):
    checks: list[LocationCheckCommand] = Field(min_length=1, max_length=MAX_READINGS_PER_SEND)


class TrackingReadingsResult(BaseModel):
    results: list[LocationCheckResult]
    # False: the phone stops tracking (checked out, the day is over, or a newer key on another phone).
    trackingActive: bool
    trackingUntilUtc: datetime


def _key_row(connection: Connection, *, tenant_id: str, key: str, now: datetime) -> dict[str, Any] | None:
    row = connection.execute(
        text(
            "SELECT key_id, actor_id, created_at_utc, expires_at_utc, revoked_at_utc "
            "FROM auditcore.p2_location_tracking_keys WHERE tenant_id = :t AND key_hash = :h"
        ),
        {"t": tenant_id, "h": key_hash(key)},
    ).mappings().one_or_none()
    if row is None or row["expires_at_utc"] <= now:
        return None
    return dict(row)


def record_readings(
    connection: Connection, *, tenant_id: str, key: str, readings: TrackingReadings, now: datetime | None = None,
) -> TrackingReadingsResult | None:
    """Log the readings a key sends. None: the key is unknown or lapsed."""
    set_tenant_context(connection, tenant_id)
    now = now or connection.execute(text("SELECT now()")).scalar_one()
    row = _key_row(connection, tenant_id=tenant_id, key=key, now=now)
    if row is None:
        return None
    until = tracking_until(row["created_at_utc"])
    stop = min(until, row["revoked_at_utc"]) if row["revoked_at_utc"] else until
    results: list[LocationCheckResult] = []
    for reading in readings.checks:
        moment = check_moment(reading, now)
        if moment is not None and in_tracking_hours(moment) and moment > stop:
            # Read after check-out (or after another phone took over): not this key's.
            results.append(LocationCheckResult(recorded=False, reason="TRACKING_STOPPED"))
            continue
        results.append(record_location_check(
            connection, tenant_id=tenant_id, actor_id=str(row["actor_id"]), command=reading, now=now,
            trigger="BACKGROUND", role=None,
        ))
    connection.execute(
        text("UPDATE auditcore.p2_location_tracking_keys SET last_used_at_utc = :now WHERE key_id = :k"),
        {"now": now, "k": row["key_id"]},
    )
    return TrackingReadingsResult(
        results=results, trackingActive=row["revoked_at_utc"] is None and now < until, trackingUntilUtc=until,
    )


@router.post("/tenants/{tenant_id}/location-tracking/checks", response_model=TrackingReadingsResult)
def post_tracking_readings(
    tenant_id: str,
    readings: TrackingReadings,
    connection: Annotated[Connection, Depends(get_connection)],
    location_key: Annotated[str | None, Header(alias=KEY_HEADER)] = None,
) -> TrackingReadingsResult | JSONResponse:
    """The phone's tracking service sends its readings (no sign-in token: the key)."""
    if not location_key or len(location_key) > 200:
        raise HTTPException(status_code=401, detail="A tracking key is required.")
    if len(tenant_id) > 128:
        raise HTTPException(status_code=404, detail="Unknown Project.")
    result = record_readings(connection, tenant_id=tenant_id, key=location_key, readings=readings)
    if result is None:
        # The phone forgets the key and stops; the next check-in makes a new one.
        return JSONResponse(status_code=401, content={"code": "KEY_INVALID",
                                                      "detail": "This tracking key is not active."})
    return result

