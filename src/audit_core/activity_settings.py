"""The reading window of "time on Verigence" (product owner, 9 Nov 2026).

A minute counts as active only when there was a click, tap or key in that minute
or in the reading window before it (the minutes someone spends reading a screen
after a click); scrolling alone is not counted. The window is 3 minutes by
default; SuperAdmin sets it from 1 to 10 minutes for every Project, or for one
Project (migration 0204, auditcore.p2_activity_settings), and every change is
kept with who and when (p2_activity_setting_changes).

    GET /p2/v1/tenants/{t}/activity/settings   the app: the window it classifies with
                                               (task_manager_api, same readers as
                                               POST /activity, which returns it too)
    GET /v1/admin/activity-settings            SuperAdmin: the all-Projects value, each
                                               Project's own value, the change log
    PUT /v1/admin/activity-settings            SuperAdmin: {tenantId|null, readingWindowMinutes}
                                               (null minutes for a Project: follow the
                                               all-Projects value)

The app classifies each minute itself and sends the day minute by minute, so a
change applies from the moment the app reads it; minutes already recorded keep the
window they were counted with.
"""
from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.db import set_platform_super_admin_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_super_admin_request,
)
from audit_core.errors import NotFoundError, ValidationError

logger = structlog.get_logger(__name__)

admin_router = APIRouter(tags=["system-health"])

DEFAULT_READING_WINDOW_MINUTES = 3
MIN_READING_WINDOW_MINUTES = 1
MAX_READING_WINDOW_MINUTES = 10
_CHANGES_SHOWN = 50


def reading_window(connection: Connection, tenant_id: str | None) -> dict[str, Any]:
    """The window that applies to a Project: its own value, else the all-Projects value, else 3.
    ``scope``: PROJECT, ALL or DEFAULT. The caller has set the Project's tenant context (row
    security lets a Project read its own row and the all-Projects row)."""
    rows = connection.execute(
        text("""
            SELECT tenant_id, reading_window_minutes FROM auditcore.p2_activity_settings
            WHERE (tenant_id IS NULL OR tenant_id = :t) AND reading_window_minutes IS NOT NULL
        """),
        {"t": tenant_id},
    ).mappings().all()
    own = next((r for r in rows if r["tenant_id"] is not None), None)
    every = next((r for r in rows if r["tenant_id"] is None), None)
    if own is not None:
        return {"minutes": int(own["reading_window_minutes"]), "scope": "PROJECT"}
    if every is not None:
        return {"minutes": int(every["reading_window_minutes"]), "scope": "ALL"}
    return {"minutes": DEFAULT_READING_WINDOW_MINUTES, "scope": "DEFAULT"}


def reading_window_minutes(connection: Connection, tenant_id: str | None) -> int:
    """The window that applies to a Project, in minutes; 3 if it cannot be read."""
    try:
        with connection.begin_nested():
            return int(reading_window(connection, tenant_id)["minutes"])
    except Exception:
        logger.warning("activity_reading_window_unreadable", tenant_id=tenant_id, exc_info=True)
        return DEFAULT_READING_WINDOW_MINUTES


def activity_rule_note(minutes: int) -> str:
    """The rule in the words every report uses (product owner 2026-11-09)."""
    unit = "minute" if minutes == 1 else "minutes"
    return (f"Time on Verigence is measured in the app and the browser: active = a click, tap or key in the "
            f"minute or the {minutes} {unit} before it (the reading window, set by SuperAdmin); scrolling alone is "
            f"not counted; idle = open and visible with no click, tap or key for more than {minutes} {unit} "
            "(scrolling included); background = tab hidden or app minimised. Time recorded before this rule "
            "reached the app (November 2026) was counted with the earlier one: a click, tap, key or scroll kept "
            "the next 4 minutes active.")


# ── SuperAdmin ────────────────────────────────────────────────────────────────
class ActivitySettingCommand(BaseModel):
    tenantId: str | None = Field(default=None, max_length=128)
    readingWindowMinutes: int | None = None


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def admin_view(connection: Connection) -> dict[str, Any]:
    set_platform_super_admin_context(connection)
    rows = connection.execute(
        text("""
            SELECT s.tenant_id, pr.project_name, s.reading_window_minutes, s.set_by_actor_id, s.set_at_utc
            FROM auditcore.p2_activity_settings s
            LEFT JOIN auditcore.projects pr ON pr.tenant_id = s.tenant_id
            ORDER BY (s.tenant_id IS NOT NULL), pr.project_name, s.tenant_id
        """)
    ).mappings().all()
    every = next((r for r in rows if r["tenant_id"] is None), None)
    changes = connection.execute(
        text("""
            SELECT c.tenant_id, pr.project_name, c.previous_minutes, c.reading_window_minutes,
                   c.changed_by_actor_id, c.changed_at_utc
            FROM auditcore.p2_activity_setting_changes c
            LEFT JOIN auditcore.projects pr ON pr.tenant_id = c.tenant_id
            ORDER BY c.changed_at_utc DESC
            LIMIT :n
        """),
        {"n": _CHANGES_SHOWN},
    ).mappings().all()
    projects = connection.execute(
        text("SELECT tenant_id, project_name, project_status FROM auditcore.projects ORDER BY lower(project_name)")
    ).mappings().all()
    return {
        "defaultMinutes": DEFAULT_READING_WINDOW_MINUTES,
        "minMinutes": MIN_READING_WINDOW_MINUTES,
        "maxMinutes": MAX_READING_WINDOW_MINUTES,
        "allProjects": {
            "minutes": int(every["reading_window_minutes"]) if every else DEFAULT_READING_WINDOW_MINUTES,
            "isDefault": every is None,
            "setByActorId": every["set_by_actor_id"] if every else None,
            "setAtUtc": _iso(every["set_at_utc"]) if every else None,
        },
        "projects": [
            {"tenantId": r["tenant_id"], "projectName": r["project_name"], "minutes": r["reading_window_minutes"],
             "setByActorId": r["set_by_actor_id"], "setAtUtc": _iso(r["set_at_utc"])}
            for r in rows if r["tenant_id"] is not None and r["reading_window_minutes"] is not None
        ],
        "changes": [
            {"tenantId": c["tenant_id"], "projectName": c["project_name"], "previousMinutes": c["previous_minutes"],
             "minutes": c["reading_window_minutes"], "changedByActorId": c["changed_by_actor_id"],
             "changedAtUtc": _iso(c["changed_at_utc"])}
            for c in changes
        ],
        "projectChoices": [{"tenantId": p["tenant_id"], "projectName": p["project_name"],
                            "status": p["project_status"]} for p in projects],
    }


@admin_router.get("/v1/admin/activity-settings")
def get_activity_settings(
    _admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    return admin_view(connection)


def set_reading_window(connection: Connection, *, tenant_id: str | None, minutes: int | None,
                       actor_id: str) -> None:
    """Set the window for every Project (tenant_id None) or one Project; minutes None for a
    Project removes its own value (it follows the all-Projects value again). Logged with who
    and when, also when the value does not change."""
    if minutes is None and tenant_id is None:
        raise ValidationError(detail="Choose the reading window for every Project (1 to 10 minutes).")
    if minutes is not None and not MIN_READING_WINDOW_MINUTES <= minutes <= MAX_READING_WINDOW_MINUTES:
        raise ValidationError(detail=f"The reading window must be {MIN_READING_WINDOW_MINUTES} to "
                                     f"{MAX_READING_WINDOW_MINUTES} minutes.")
    set_platform_super_admin_context(connection)
    if tenant_id is not None and connection.execute(
        text("SELECT 1 FROM auditcore.projects WHERE tenant_id = :t"), {"t": tenant_id}
    ).first() is None:
        raise NotFoundError(error_code="VAC-NF-000", title="Not found", detail=f"Project {tenant_id} was not found.")
    previous = connection.execute(
        text("SELECT reading_window_minutes FROM auditcore.p2_activity_settings "
             "WHERE COALESCE(tenant_id, '*') = :scope FOR UPDATE"),
        {"scope": tenant_id or "*"},
    ).scalar_one_or_none()
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_activity_settings (tenant_id, reading_window_minutes, set_by_actor_id, set_at_utc)
            VALUES (:t, :m, :a, now())
            ON CONFLICT ((COALESCE(tenant_id, '*'))) DO UPDATE
            SET reading_window_minutes = EXCLUDED.reading_window_minutes,
                set_by_actor_id = EXCLUDED.set_by_actor_id, set_at_utc = now()
        """),
        {"t": tenant_id, "m": minutes, "a": actor_id},
    )
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_activity_setting_changes
              (tenant_id, previous_minutes, reading_window_minutes, changed_by_actor_id)
            VALUES (:t, :p, :m, :a)
        """),
        {"t": tenant_id, "p": previous, "m": minutes, "a": actor_id},
    )
    logger.info("activity_reading_window_changed", scope=tenant_id or "ALL", minutes=minutes,
                previous_minutes=previous, actor_id=actor_id)


@admin_router.put("/v1/admin/activity-settings")
def put_activity_settings(
    command: ActivitySettingCommand,
    admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    tenant_id = (command.tenantId or "").strip() or None
    set_reading_window(connection, tenant_id=tenant_id, minutes=command.readingWindowMinutes, actor_id=admin.user_id)
    return admin_view(connection)
