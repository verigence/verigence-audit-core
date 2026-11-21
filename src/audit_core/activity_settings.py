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

Two more numbers live in the same rows (product owner, 16 Nov 2026; migration 0234),
read by the engagement reports, not the app: the output window (an active minute is
productive when output follows within it; 20 minutes by default, 5 to 60) and the
screen changes that make a 20-minute block with no output "clicking around" (more than
10 by default, 3 to 50). Same scopes and change log; reports read them on every request,
so a change applies to every day shown, past days included.
"""
from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import Connection, event, text

from audit_core import activity_at_night
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
# The engagement reports' two numbers (2026-11-16): column, default, low, high, words.
ENGAGEMENT_SETTINGS: dict[str, tuple[str, int, int, int, str]] = {
    "outputWindowMinutes": ("output_window_minutes", 20, 5, 60, "The output window"),
    "hopScreenChanges": ("hop_screen_changes", 10, 3, 50, "Screen changes for clicking around"),
}
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


def engagement_settings(connection: Connection, tenant_id: str | None) -> dict[str, int]:
    """{outputWindowMinutes, hopScreenChanges} for a Project: its own value, else the
    all-Projects value, else the default. Defaults if the rows cannot be read."""
    out = {key: spec[1] for key, spec in ENGAGEMENT_SETTINGS.items()}
    try:
        with connection.begin_nested():
            rows = connection.execute(
                text("""
                    SELECT tenant_id, output_window_minutes, hop_screen_changes
                    FROM auditcore.p2_activity_settings WHERE tenant_id IS NULL OR tenant_id = :t
                    ORDER BY (tenant_id IS NULL)
                """),
                {"t": tenant_id},
            ).mappings().all()
    except Exception:
        logger.warning("activity_engagement_settings_unreadable", tenant_id=tenant_id, exc_info=True)
        return out
    for key, (column, *_rest) in ENGAGEMENT_SETTINGS.items():
        value = next((r[column] for r in rows if r[column] is not None), None)
        if value is not None:
            out[key] = int(value)
    return out


def engagement_rule_note(settings: dict[str, int]) -> str:
    return (f"Productive = active minutes followed by output (a file, a saved value, a task action, a review) within "
            f"{settings['outputWindowMinutes']} minutes; the rest is active with no output. A 20-minute block with no "
            f"output and more than {settings['hopScreenChanges']} screen changes is clicking around. Both numbers are "
            "set by SuperAdmin and apply to every day shown.")


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
    # The engagement reports' numbers (2026-11-16): sent alone, they change only themselves.
    outputWindowMinutes: int | None = None
    hopScreenChanges: int | None = None


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def admin_view(connection: Connection) -> dict[str, Any]:
    set_platform_super_admin_context(connection)
    rows = connection.execute(
        text("""
            SELECT s.tenant_id, pr.project_name, s.reading_window_minutes, s.output_window_minutes,
                   s.hop_screen_changes, s.set_by_actor_id, s.set_at_utc
            FROM auditcore.p2_activity_settings s
            LEFT JOIN auditcore.projects pr ON pr.tenant_id = s.tenant_id
            ORDER BY (s.tenant_id IS NOT NULL), pr.project_name, s.tenant_id
        """)
    ).mappings().all()
    every = next((r for r in rows if r["tenant_id"] is None), None)
    changes = connection.execute(
        text("""
            SELECT c.tenant_id, pr.project_name, c.previous_minutes, c.reading_window_minutes,
                   c.output_window_minutes, c.hop_screen_changes, c.setting, c.changed_by_actor_id,
                   c.changed_at_utc
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
        "engagement": {
            key: {"default": spec[1], "min": spec[2], "max": spec[3],
                  "allProjects": (every[spec[0]] if every and every[spec[0]] is not None else spec[1])}
            for key, spec in ENGAGEMENT_SETTINGS.items()
        },
        "allProjects": {
            "minutes": int(every["reading_window_minutes"]) if every else DEFAULT_READING_WINDOW_MINUTES,
            "outputWindowMinutes": every["output_window_minutes"] if every else None,
            "hopScreenChanges": every["hop_screen_changes"] if every else None,
            "isDefault": every is None,
            "setByActorId": every["set_by_actor_id"] if every else None,
            "setAtUtc": _iso(every["set_at_utc"]) if every else None,
        },
        "projects": [
            {"tenantId": r["tenant_id"], "projectName": r["project_name"], "minutes": r["reading_window_minutes"],
             "outputWindowMinutes": r["output_window_minutes"], "hopScreenChanges": r["hop_screen_changes"],
             "setByActorId": r["set_by_actor_id"], "setAtUtc": _iso(r["set_at_utc"])}
            for r in rows if r["tenant_id"] is not None and (
                r["reading_window_minutes"] is not None or r["output_window_minutes"] is not None
                or r["hop_screen_changes"] is not None)
        ],
        "changes": [
            {"tenantId": c["tenant_id"], "projectName": c["project_name"], "previousMinutes": c["previous_minutes"],
             "minutes": c["reading_window_minutes"], "outputWindowMinutes": c["output_window_minutes"],
             "hopScreenChanges": c["hop_screen_changes"], "setting": c["setting"] or "READING_WINDOW",
             "changedByActorId": c["changed_by_actor_id"],
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
    # Open tabs are told the window from memory at night (audit_core.activity_at_night): not any
    # more. Now, and again once this is committed -- a tab that asks in between is told the old
    # value and would otherwise keep it until morning.
    activity_at_night.forget_windows()
    event.listen(connection, "commit", lambda _connection: activity_at_night.forget_windows(), once=True)


def set_engagement_settings(connection: Connection, *, tenant_id: str | None, values: dict[str, int | None],
                            actor_id: str) -> None:
    """Set the output window and/or the clicking-around screen changes for every Project
    (tenant_id None) or one Project; None for a Project removes its own value. Only the keys
    given change. Logged with who and when."""
    for key, value in values.items():
        _column, _default, low, high, words = ENGAGEMENT_SETTINGS[key]
        if value is None and tenant_id is None:
            raise ValidationError(detail=f"{words} for every Project needs a value ({low} to {high}).")
        if value is not None and not low <= value <= high:
            raise ValidationError(detail=f"{words} must be {low} to {high}.")
    set_platform_super_admin_context(connection)
    if tenant_id is not None and connection.execute(
        text("SELECT 1 FROM auditcore.projects WHERE tenant_id = :t"), {"t": tenant_id}
    ).first() is None:
        raise NotFoundError(error_code="VAC-NF-000", title="Not found", detail=f"Project {tenant_id} was not found.")
    sets = ", ".join(f"{ENGAGEMENT_SETTINGS[k][0]} = :{k}" for k in values)
    params = {"t": tenant_id, "a": actor_id, "rw": DEFAULT_READING_WINDOW_MINUTES if tenant_id is None else None,
              **values}
    cols = ", ".join(ENGAGEMENT_SETTINGS[k][0] for k in values)
    vals = ", ".join(f":{k}" for k in values)
    # The all-Projects row must carry a reading window (0204): a new one starts at the default.
    connection.execute(
        text(f"""
            INSERT INTO auditcore.p2_activity_settings (tenant_id, reading_window_minutes, {cols},
                                                        set_by_actor_id, set_at_utc)
            VALUES (:t, :rw, {vals}, :a, now())
            ON CONFLICT ((COALESCE(tenant_id, '*'))) DO UPDATE
            SET {sets}, set_by_actor_id = EXCLUDED.set_by_actor_id, set_at_utc = now()
        """),
        params,
    )
    connection.execute(
        text(f"""
            INSERT INTO auditcore.p2_activity_setting_changes (tenant_id, {cols}, setting, changed_by_actor_id)
            VALUES (:t, {vals}, 'ENGAGEMENT', :a)
        """),
        params,
    )
    logger.info("activity_engagement_settings_changed", scope=tenant_id or "ALL", actor_id=actor_id,
                **{k: v for k, v in values.items()})


@admin_router.put("/v1/admin/activity-settings")
def put_activity_settings(
    command: ActivitySettingCommand,
    admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    tenant_id = (command.tenantId or "").strip() or None
    given = command.model_fields_set
    engagement = {k: getattr(command, k) for k in ENGAGEMENT_SETTINGS if k in given}
    if engagement:
        set_engagement_settings(connection, tenant_id=tenant_id, values=engagement, actor_id=admin.user_id)
    if "readingWindowMinutes" in given or not engagement:
        set_reading_window(connection, tenant_id=tenant_id, minutes=command.readingWindowMinutes,
                           actor_id=admin.user_id)
    return admin_view(connection)
