"""Daily Duties: the PC's checklist for the day (product owner, 2026-11-18;
docs/DAILY_DUTIES_DESIGN_v1.0.md).

"each PC is supposed to do 5-8 tasks in a day, it may change ... let PC know which
tasks are pending, give him flexibility to perform the tasks, but at day end they have
to complete all tasks, or if not completed need to give the reason. If something is not
completed, it has to be clubbed next day ... TL should be able to see the progress".

Version 1 is a checkbox list ("a simple checkbox type ... to start off with"):

- an Executive or SuperAdmin keeps the duty list of the Project (name, mandatory or
  not, the days, the outlets, the numbers a tick asks for, "not received today");
- every outlet with a PC gets one item per duty and day; a PC of the outlet ticks it;
- at day close (check-out) every mandatory duty not done needs a reason;
- an item not done stays on the list for its carry days, then is incomplete;
- a Team Lead, PM or Executive sees every outlet and PC day by day.

Two rules of the product owner shape the module:

**Loosely coupled.** It has its own tables (p2_duty_*) and only reads the rest of
Verigence, through a checker: a function that says whether the work of one duty is
done at an outlet for a date. The one checker so far reads the gate register. No
other module calls this one, and nothing here is kept in p2_tasks.

**Switchable at any time.** One switch per Project, off by default. Off: every read
answers ``enabled: false`` and nothing is created. On again: it starts from that day.

Nothing runs in the worker: items are created when someone opens the list, and
"incomplete" and "not closed" are read from the dates.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, text

from audit_core.daily_duty_checkers import CHECKERS
from audit_core.task_manager import EXECUTIVE, SUPER_ADMIN, Caller, known_names
from audit_core.uc03_p2_object_keys import IST

logger = structlog.get_logger(__name__)

MANAGERS = frozenset({EXECUTIVE, SUPER_ADMIN})
TEAM_VIEWERS = frozenset({"TL", "PM", EXECUTIVE, SUPER_ADMIN})
MAX_RANGE_DAYS = 31
MAX_INPUTS = 6
INPUT_TYPES = ("NUMBER", "AMOUNT", "TEXT")
NOT_RECEIVED = "NOT_RECEIVED"

# Why a duty was not done (the PC picks one at day close). ``side`` is who it sits
# with, the split the gate register view already uses (not notified to the PC /
# pending due to the PC).
REASONS: tuple[dict[str, Any], ...] = (
    {"code": "NOT_GIVEN_BY_DEALERSHIP", "label": "Dealership did not give it", "side": "DEALERSHIP"},
    {"code": "NOT_IN_VERIGENCE", "label": "Booking is not in Verigence", "side": "DEALERSHIP"},
    {"code": "HANDLED_BY_OTHER_PC", "label": "Another PC is doing it", "side": "PC", "needsPerson": True},
    {"code": "SYSTEM_ISSUE", "label": "System issue", "side": "SYSTEM"},
    {"code": "ON_LEAVE", "label": "On leave or holiday", "side": "NONE"},
    {"code": "OTHER", "label": "Other", "side": "PC", "needsRemark": True},
)
_REASON = {r["code"]: r for r in REASONS}
_NOT_RECEIVED_REASON = {"code": NOT_RECEIVED, "label": "Not received today", "side": "DEALERSHIP"}


class DutyError(ValueError):
    """A request the rules do not allow (422)."""


class DutyForbidden(ValueError):
    """The caller may not do this (403)."""


class DutyConflict(ValueError):
    """The state does not allow this (409)."""


class DutyNotFound(LookupError):
    """Not found in the caller's view of the Project (404)."""


def ist_today(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(IST).date()


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


# The duties the product owner named on 18 Nov 2026: one click adds them, and each
# can then be changed or switched off like any other.
STANDARD_DUTIES: tuple[dict[str, Any], ...] = (
    {"standardCode": "GATE_REGISTER", "title": "Gate out register",
     "description": "Upload the previous day's gate out register and enter its counts.",
     "weekdays": [1, 2, 3, 4, 5, 6, 7], "dayOffset": -1, "closeMode": "AUTO", "checkerCode": "GATE_REGISTER",
     "sortOrder": 10},
    {"standardCode": "INTIMATION", "title": "Booking and delivery files received",
     "description": "Enter how many booking files and delivery files the dealership gave you today. Enter 0 if none.",
     "inputs": [{"key": "bookingFiles", "label": "Booking files received", "type": "NUMBER", "required": True},
                {"key": "deliveryFiles", "label": "Delivery files received", "type": "NUMBER", "required": True}],
     "sortOrder": 20},
    {"standardCode": "UPLOADS", "title": "Upload bookings and deliveries",
     "description": "Upload every booking and delivery file received today.",
     "closeMode": "AUTO", "checkerCode": "UPLOADS", "sortOrder": 30},
    {"standardCode": "PHYSICAL_VERIFICATION", "title": "Physical delivery verification",
     "description": "Upload photographs of every vehicle delivered today, from the booking's Vehicle photos tab.",
     "closeMode": "AUTO", "checkerCode": "VEHICLE_PHOTOS", "sortOrder": 40},
    {"standardCode": "BANK_STATEMENT", "title": "Bank statement",
     "description": "Upload the bank statement when the dealership gives it. One PC of the outlet picks this up.",
     "closeMode": "AUTO", "checkerCode": "BANK_STATEMENT", "allowNotReceived": True, "rolling": True,
     "sortOrder": 50},
    {"standardCode": "CASH_LEDGER", "title": "Cash ledger",
     "description": "Enter the previous day's cash ledger: the total amount and the number of transactions.",
     "dayOffset": -1,
     "inputs": [{"key": "amount", "label": "Amount", "type": "AMOUNT", "required": True},
                {"key": "transactions", "label": "Number of transactions", "type": "NUMBER", "required": True}],
     "allowNotReceived": True, "sortOrder": 60},
    # A day's work on the tasks cannot be done the next day: nothing is carried (carryDays 0).
    {"standardCode": "OPEN_TASKS", "title": "Work on open tasks",
     "description": "Work on the tasks raised on your outlet's files: the pending ones and those raised by "
                    "today's uploads. Open the Tasks screen.",
     "closeMode": "AUTO", "checkerCode": "OPEN_TASKS", "carryDays": 0, "sortOrder": 70},
)


# --------------------------------------------------------------------------- settings

def _settings(connection: Connection, tenant_id: str) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT enabled, enabled_from, cutoff_time_ist, default_carry_days, working_weekdays, generated_through, "
             "updated_by_name, updated_at_utc FROM auditcore.p2_duty_settings WHERE tenant_id = :t"),
        {"t": tenant_id},
    ).mappings().first()
    if row is None:
        return {"enabled": False, "enabled_from": None, "cutoff_time_ist": time(21, 0), "default_carry_days": 5,
                "working_weekdays": [1, 2, 3, 4, 5, 6], "generated_through": None, "updated_by_name": None,
                "updated_at_utc": None, "exists": False}
    out = dict(row)
    out["working_weekdays"] = [int(d) for d in (row["working_weekdays"] or [])] or [1, 2, 3, 4, 5, 6]
    out["exists"] = True
    return out


def _public_settings(s: dict[str, Any], caller: Caller) -> dict[str, Any]:
    return {
        "enabled": bool(s["enabled"]), "enabledFrom": _iso(s["enabled_from"]),
        "cutoffTimeIst": s["cutoff_time_ist"].strftime("%H:%M"), "defaultCarryDays": int(s["default_carry_days"]),
        "workingWeekdays": list(s["working_weekdays"]), "updatedByName": s["updated_by_name"],
        "updatedAt": _iso(s["updated_at_utc"]), "canManage": caller.role in MANAGERS,
    }


def duties_enabled(connection: Connection, tenant_id: str) -> bool:
    """Whether the Project has daily duties switched on: one small read, for the callers
    that ask on every screen (Attendance, PC Home) in Projects that have it off."""
    return bool(connection.execute(
        text("SELECT enabled FROM auditcore.p2_duty_settings WHERE tenant_id = :t"), {"t": tenant_id},
    ).scalar_one_or_none())


def get_settings(connection: Connection, *, tenant_id: str, caller: Caller) -> dict[str, Any]:
    return _public_settings(_settings(connection, tenant_id), caller)


def _weekdays(value: Any, *, what: str) -> list[int]:
    try:
        days = sorted({int(d) for d in value})
    except (TypeError, ValueError) as exc:
        raise DutyError(f"{what}: choose the days of the week.") from exc
    if not days or any(d < 1 or d > 7 for d in days):
        raise DutyError(f"{what}: choose at least one day of the week.")
    return days


def update_settings(connection: Connection, *, tenant_id: str, caller: Caller, spec: dict[str, Any],
                    now: datetime | None = None) -> dict[str, Any]:
    """The switch, the cut-off, the carry days and the working days (Executive / SuperAdmin)."""
    if caller.role not in MANAGERS:
        raise DutyForbidden("Only an Executive or SuperAdmin can change the daily duties settings.")
    now = now or datetime.now(UTC)
    today = ist_today(now)
    current = _settings(connection, tenant_id)
    enabled = bool(spec["enabled"]) if spec.get("enabled") is not None else bool(current["enabled"])
    cutoff = current["cutoff_time_ist"]
    if spec.get("cutoffTimeIst") is not None:
        match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", str(spec["cutoffTimeIst"]).strip())
        if not match:
            raise DutyError("Give the cut-off time as HH:MM, for example 21:00.")
        cutoff = time(int(match.group(1)), int(match.group(2)))
    carry = int(current["default_carry_days"])
    if spec.get("defaultCarryDays") is not None:
        carry = int(spec["defaultCarryDays"])
        if carry < 0 or carry > 30:
            raise DutyError("Carry days must be between 0 and 30.")
    working = current["working_weekdays"]
    if spec.get("workingWeekdays") is not None:
        working = _weekdays(spec["workingWeekdays"], what="Working days")
    turning_on = enabled and not current["enabled"]
    turning_off = current["enabled"] and not enabled
    enabled_from = today if turning_on else current["enabled_from"]
    generated_through = (today - timedelta(days=1)) if turning_on else current["generated_through"]
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_duty_settings (tenant_id, enabled, enabled_from, cutoff_time_ist,
              default_carry_days, working_weekdays, generated_through, updated_by, updated_by_name, updated_at_utc)
            VALUES (:t, :enabled, :enabled_from, :cutoff, :carry, CAST(:working AS smallint[]), :through,
                    :by, :name, :now)
            ON CONFLICT (tenant_id) DO UPDATE SET enabled = EXCLUDED.enabled, enabled_from = EXCLUDED.enabled_from,
              cutoff_time_ist = EXCLUDED.cutoff_time_ist, default_carry_days = EXCLUDED.default_carry_days,
              working_weekdays = EXCLUDED.working_weekdays, generated_through = EXCLUDED.generated_through,
              updated_by = EXCLUDED.updated_by, updated_by_name = EXCLUDED.updated_by_name,
              updated_at_utc = EXCLUDED.updated_at_utc
        """),
        {"t": tenant_id, "enabled": enabled, "enabled_from": enabled_from, "cutoff": cutoff, "carry": carry,
         "working": working, "through": generated_through, "by": caller.actor_id, "name": caller.display_name,
         "now": now},
    )
    if turning_off:
        # Off means off: what was open is closed as switched off, never counted against anyone.
        _cancel_open(connection, tenant_id=tenant_id, reason="SWITCHED_OFF", now=now)
    _event(connection, tenant_id=tenant_id, event_type=("SWITCHED_ON" if turning_on else "SWITCHED_OFF"
                                                         if turning_off else "SETTINGS_CHANGED"),
           caller=caller, details={"enabled": enabled, "cutoffTimeIst": cutoff.strftime("%H:%M"),
                                   "defaultCarryDays": carry, "workingWeekdays": working}, now=now)
    return get_settings(connection, tenant_id=tenant_id, caller=caller)


def _cancel_open(connection: Connection, *, tenant_id: str, reason: str, now: datetime,
                 duty_id: str | None = None) -> int:
    return connection.execute(
        text("""
            UPDATE auditcore.p2_duty_items SET status = 'CANCELLED', cancelled_at_utc = :now, cancel_reason = :reason
            WHERE tenant_id = :t AND status = 'PENDING'
              AND (CAST(:duty AS uuid) IS NULL OR duty_id = CAST(:duty AS uuid))
        """),
        {"t": tenant_id, "now": now, "reason": reason, "duty": duty_id},
    ).rowcount


def _event(connection: Connection, *, tenant_id: str, event_type: str, caller: Caller | None,
           details: dict[str, Any] | None = None, item_id: Any = None, duty_id: Any = None,
           now: datetime | None = None) -> None:
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_duty_events (tenant_id, item_id, duty_id, event_type, actor_id, actor_name,
              actor_role, details, occurred_at_utc)
            VALUES (:t, CAST(:item AS uuid), CAST(:duty AS uuid), :type, :actor, :name, :role,
                    CAST(:details AS jsonb), :now)
        """),
        {"t": tenant_id, "item": str(item_id) if item_id else None, "duty": str(duty_id) if duty_id else None,
         "type": event_type, "actor": caller.actor_id if caller else None,
         "name": caller.display_name if caller else None, "role": caller.role if caller else "SYSTEM",
         "details": _json(details or {}), "now": now or datetime.now(UTC)},
    )


# --------------------------------------------------------------------------- outlets and people

_ACTIVE = """
    ba.assignment_status = 'ACTIVE' AND ba.effective_from <= now()
    AND (ba.effective_to IS NULL OR ba.effective_to >= now())
"""


def _duty_outlets(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    """Every active outlet that has a PC, with its PCs. A PC mapped to a dealership
    covers its outlets; one mapped to the whole Project covers every outlet."""
    outlets = connection.execute(
        text("""
            SELECT o.outlet_id::text AS outlet_id, o.dealer_id::text AS dealer_id, o.outlet_name,
                   o.outlet_classification AS classification, d.dealer_name
            FROM auditcore.dealer_outlets o
            JOIN auditcore.dealers d ON d.tenant_id = o.tenant_id AND d.dealer_id = o.dealer_id
            WHERE o.tenant_id = :t AND o.status = 'ACTIVE'
            ORDER BY d.dealer_name, o.outlet_name
        """),
        {"t": tenant_id},
    ).mappings().all()
    assignments = connection.execute(
        text(f"""
            SELECT ba.security_actor_id AS actor, ba.dealer_id::text AS dealer_id, ba.outlet_id::text AS outlet_id
            FROM auditcore.business_assignments ba
            WHERE ba.tenant_id = :t AND {_ACTIVE} AND upper(ba.business_role_code) = 'PC'
        """),
        {"t": tenant_id},
    ).mappings().all()
    if not assignments:
        return []
    names = known_names(connection, tenant_id=tenant_id)
    out: list[dict[str, Any]] = []
    for outlet in outlets:
        actors = sorted({
            str(a["actor"]) for a in assignments
            if a["outlet_id"] == outlet["outlet_id"]
            or (a["outlet_id"] is None and a["dealer_id"] == outlet["dealer_id"])
            or (a["outlet_id"] is None and a["dealer_id"] is None)
        })
        if actors:
            out.append({**dict(outlet),
                        "pcs": [{"actorId": a, "name": names.get(a) or a} for a in actors]})
    return out


def _own_outlet_ids(connection: Connection, tenant_id: str, actor_id: str) -> tuple[bool, set[str]]:
    """(Project-wide, outlet ids) of the caller's own assignments, any role."""
    rows = connection.execute(
        text(f"""
            SELECT ba.dealer_id::text AS dealer_id, ba.outlet_id::text AS outlet_id
            FROM auditcore.business_assignments ba
            WHERE ba.tenant_id = :t AND ba.security_actor_id = :a AND {_ACTIVE}
        """),
        {"t": tenant_id, "a": actor_id},
    ).mappings().all()
    wide = any(r["dealer_id"] is None and r["outlet_id"] is None for r in rows)
    outlets = {r["outlet_id"] for r in rows if r["outlet_id"] is not None}
    dealers = [r["dealer_id"] for r in rows if r["dealer_id"] is not None and r["outlet_id"] is None]
    if dealers:
        outlets |= set(connection.execute(
            text("SELECT outlet_id::text FROM auditcore.dealer_outlets WHERE tenant_id = :t "
                 "AND dealer_id = ANY(CAST(:d AS uuid[]))"),
            {"t": tenant_id, "d": dealers},
        ).scalars().all())
    return wide, outlets


# --------------------------------------------------------------------------- the duty list

_DUTY_FIELDS = """
    duty_id, standard_code, title, description, mandatory, weekdays, outlet_scope, outlet_ids, day_offset,
    close_mode, checker_code, inputs, allow_not_received, rolling, carry_days, sort_order, status,
    effective_from, updated_by_name, updated_at_utc
"""


def _duties(connection: Connection, tenant_id: str, *, active_only: bool = False) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(f"SELECT {_DUTY_FIELDS} FROM auditcore.p2_duties WHERE tenant_id = :t "
             + ("AND status = 'ACTIVE' " if active_only else "")
             + "ORDER BY (status = 'ACTIVE') DESC, sort_order, created_at_utc"),
        {"t": tenant_id},
    ).mappings().all()
    out = []
    for row in rows:
        duty = dict(row)
        duty["weekdays"] = [int(d) for d in (row["weekdays"] or [])]
        duty["outlet_ids"] = [str(o) for o in (row["outlet_ids"] or [])]
        duty["inputs"] = list(row["inputs"] or [])
        out.append(duty)
    return out


def _public_duty(duty: dict[str, Any]) -> dict[str, Any]:
    return {
        "dutyId": str(duty["duty_id"]), "standardCode": duty["standard_code"], "title": duty["title"],
        "description": duty["description"], "mandatory": bool(duty["mandatory"]), "weekdays": duty["weekdays"],
        "outletScope": duty["outlet_scope"], "outletIds": duty["outlet_ids"], "dayOffset": int(duty["day_offset"]),
        "closeMode": duty["close_mode"], "checkerCode": duty["checker_code"], "inputs": duty["inputs"],
        "allowNotReceived": bool(duty["allow_not_received"]), "rolling": bool(duty["rolling"]),
        "carryDays": duty["carry_days"], "sortOrder": int(duty["sort_order"]), "status": duty["status"],
        "effectiveFrom": _iso(duty["effective_from"]), "updatedByName": duty["updated_by_name"],
        "updatedAt": _iso(duty["updated_at_utc"]),
    }


def list_duties(connection: Connection, *, tenant_id: str, caller: Caller) -> dict[str, Any]:
    if caller.role not in TEAM_VIEWERS:
        raise DutyForbidden("The duty list is for Team Leads, PMs and Executives.")
    outlets = connection.execute(
        text("""
            SELECT o.outlet_id::text AS "outletId", o.outlet_name AS "outletName",
                   o.outlet_classification AS classification, d.dealer_name AS "dealerName"
            FROM auditcore.dealer_outlets o
            JOIN auditcore.dealers d ON d.tenant_id = o.tenant_id AND d.dealer_id = o.dealer_id
            WHERE o.tenant_id = :t AND o.status = 'ACTIVE' ORDER BY d.dealer_name, o.outlet_name
        """),
        {"t": tenant_id},
    ).mappings().all()
    duties = _duties(connection, tenant_id)
    have = {d["standard_code"] for d in duties if d["standard_code"]}
    return {
        "items": [_public_duty(d) for d in duties],
        "settings": get_settings(connection, tenant_id=tenant_id, caller=caller),
        "canManage": caller.role in MANAGERS,
        "checkers": [{"code": code, "label": c["label"]} for code, c in CHECKERS.items()],
        "inputTypes": list(INPUT_TYPES),
        "outlets": [dict(o) for o in outlets],
        "standardMissing": [{"standardCode": s["standardCode"], "title": s["title"]}
                            for s in STANDARD_DUTIES if s["standardCode"] not in have],
        "today": ist_today().isoformat(),
    }


def _clean_inputs(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_INPUTS:
        raise DutyError(f"A duty can ask for at most {MAX_INPUTS} values.")
    out: list[dict[str, Any]] = []
    keys: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise DutyError("Each value a duty asks for needs a name and a type.")
        label = str(raw.get("label") or "").strip()[:60]
        kind = str(raw.get("type") or "NUMBER").strip().upper()
        if not label:
            raise DutyError("Give each value a name.")
        if kind not in INPUT_TYPES:
            raise DutyError(f"'{label}': the type must be one of {', '.join(INPUT_TYPES)}.")
        key = str(raw.get("key") or "").strip()[:40]
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key):
            key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40] or f"value{index + 1}"
            if not key[0].isalpha():
                key = f"v_{key}"[:40]
        base, n = key, 2
        while key in keys:
            key, n = f"{base[:36]}_{n}", n + 1
        keys.add(key)
        out.append({"key": key, "label": label, "type": kind, "required": bool(raw.get("required", True))})
    return out


def _duty_values(connection: Connection, *, tenant_id: str, spec: dict[str, Any], current: dict[str, Any] | None,
                 today: date) -> dict[str, Any]:
    """The stored values of a duty from a create or change request."""
    def pick(key: str, column: str, default: Any) -> Any:
        if key in spec and spec[key] is not None:
            return spec[key]
        return current[column] if current is not None else default

    title = str(pick("title", "title", "")).strip()[:200]
    if not title:
        raise DutyError("Give the duty a name.")
    close_mode = str(pick("closeMode", "close_mode", "TICK")).upper()
    if close_mode not in {"TICK", "AUTO"}:
        raise DutyError("A duty is ticked by the PC (TICK) or ticked by the system (AUTO).")
    checker = spec["checkerCode"] if "checkerCode" in spec else (current["checker_code"] if current else None)
    inputs = _clean_inputs(pick("inputs", "inputs", []))
    if close_mode == "AUTO":
        if checker not in CHECKERS:
            raise DutyError("Choose what the system checks for a duty it ticks itself.")
        if inputs:
            raise DutyError("A duty the system ticks itself cannot also ask the PC for values.")
    else:
        checker = None
    scope = str(pick("outletScope", "outlet_scope", "ALL")).upper()
    if scope not in {"ALL", "ONSITE", "SATELLITE", "SELECTED"}:
        raise DutyError("Outlets: all, onsite only, satellite only or chosen outlets.")
    outlet_ids = [str(o) for o in pick("outletIds", "outlet_ids", [])] if scope == "SELECTED" else []
    if scope == "SELECTED":
        if not outlet_ids:
            raise DutyError("Choose at least one outlet.")
        known = set(connection.execute(
            text("SELECT outlet_id::text FROM auditcore.dealer_outlets WHERE tenant_id = :t "
                 "AND outlet_id = ANY(CAST(:o AS uuid[]))"),
            {"t": tenant_id, "o": outlet_ids},
        ).scalars().all())
        if known != set(outlet_ids):
            raise DutyError("One of the chosen outlets is not in this Project.")
    offset = int(pick("dayOffset", "day_offset", 0))
    if offset not in (0, -1):
        raise DutyError("A duty is for the same day or for the previous day.")
    carry = spec["carryDays"] if "carryDays" in spec else (current["carry_days"] if current else None)
    if carry is not None:
        carry = int(carry)
        if carry < 0 or carry > 30:
            raise DutyError("Carry days must be between 0 and 30.")
    status = str(pick("status", "status", "ACTIVE")).upper()
    if status not in {"ACTIVE", "INACTIVE"}:
        raise DutyError("A duty is active or inactive.")
    effective = pick("effectiveFrom", "effective_from", today)
    if isinstance(effective, str):
        try:
            effective = date.fromisoformat(effective)
        except ValueError as exc:
            raise DutyError("Give the start date as YYYY-MM-DD.") from exc
    if current is None and effective < today:
        raise DutyError("A new duty cannot start in the past.")
    return {
        "title": title, "description": str(pick("description", "description", "")).strip()[:2000],
        "mandatory": bool(pick("mandatory", "mandatory", True)),
        "weekdays": _weekdays(pick("weekdays", "weekdays", [1, 2, 3, 4, 5, 6]), what="Days"),
        "scope": scope, "outlet_ids": outlet_ids, "offset": offset, "close_mode": close_mode, "checker": checker,
        "inputs": _json(inputs), "allow_not_received": bool(pick("allowNotReceived", "allow_not_received", False)),
        "rolling": bool(pick("rolling", "rolling", False)), "carry": carry,
        "sort": int(pick("sortOrder", "sort_order", 100)), "status": status, "effective": effective,
    }


def create_duty(connection: Connection, *, tenant_id: str, caller: Caller, spec: dict[str, Any],
                now: datetime | None = None, standard_code: str | None = None) -> dict[str, Any]:
    if caller.role not in MANAGERS:
        raise DutyForbidden("Only an Executive or SuperAdmin can change the duty list.")
    now = now or datetime.now(UTC)
    values = _duty_values(connection, tenant_id=tenant_id, spec=spec, current=None, today=ist_today(now))
    duty_id = connection.execute(
        text("""
            INSERT INTO auditcore.p2_duties (tenant_id, standard_code, title, description, mandatory, weekdays,
              outlet_scope, outlet_ids, day_offset, close_mode, checker_code, inputs, allow_not_received, rolling,
              carry_days, sort_order, status, effective_from, created_by, created_by_name, created_at_utc,
              updated_by, updated_by_name, updated_at_utc)
            VALUES (:t, :standard, :title, :description, :mandatory, CAST(:weekdays AS smallint[]), :scope,
              CAST(:outlet_ids AS uuid[]), :offset, :close_mode, :checker, CAST(:inputs AS jsonb),
              :allow_not_received, :rolling, :carry, :sort, :status, :effective, :by, :name, :now, :by, :name, :now)
            RETURNING duty_id
        """),
        {"t": tenant_id, "standard": standard_code, "by": caller.actor_id, "name": caller.display_name, "now": now,
         **values},
    ).scalar_one()
    _event(connection, tenant_id=tenant_id, event_type="DUTY_CREATED", caller=caller, duty_id=duty_id,
           details={"title": values["title"]}, now=now)
    return _public_duty(_duty(connection, tenant_id, duty_id))


def _duty(connection: Connection, tenant_id: str, duty_id: UUID | str) -> dict[str, Any]:
    for duty in _duties(connection, tenant_id):
        if str(duty["duty_id"]) == str(duty_id):
            return duty
    raise DutyNotFound("Duty not found.")


def update_duty(connection: Connection, *, tenant_id: str, caller: Caller, duty_id: UUID, spec: dict[str, Any],
                now: datetime | None = None) -> dict[str, Any]:
    """Change a duty. The change shows on the days not yet started: an item already on
    someone's list keeps the duty as it was that day. Making a duty inactive takes its
    open items off every list."""
    if caller.role not in MANAGERS:
        raise DutyForbidden("Only an Executive or SuperAdmin can change the duty list.")
    now = now or datetime.now(UTC)
    current = _duty(connection, tenant_id, duty_id)
    values = _duty_values(connection, tenant_id=tenant_id, spec=spec, current=current, today=ist_today(now))
    connection.execute(
        text("""
            UPDATE auditcore.p2_duties SET title = :title, description = :description, mandatory = :mandatory,
              weekdays = CAST(:weekdays AS smallint[]), outlet_scope = :scope,
              outlet_ids = CAST(:outlet_ids AS uuid[]), day_offset = :offset, close_mode = :close_mode,
              checker_code = :checker, inputs = CAST(:inputs AS jsonb), allow_not_received = :allow_not_received,
              rolling = :rolling, carry_days = :carry, sort_order = :sort, status = :status,
              effective_from = :effective, updated_by = :by, updated_by_name = :name, updated_at_utc = :now
            WHERE tenant_id = :t AND duty_id = :id
        """),
        {"t": tenant_id, "id": duty_id, "by": caller.actor_id, "name": caller.display_name, "now": now, **values},
    )
    if current["status"] == "ACTIVE" and values["status"] == "INACTIVE":
        _cancel_open(connection, tenant_id=tenant_id, reason="DUTY_RETIRED", now=now, duty_id=str(duty_id))
    _event(connection, tenant_id=tenant_id, event_type="DUTY_CHANGED", caller=caller, duty_id=duty_id,
           details={"changed": sorted(spec.keys())}, now=now)
    return _public_duty(_duty(connection, tenant_id, duty_id))


def add_standard_duties(connection: Connection, *, tenant_id: str, caller: Caller,
                        now: datetime | None = None) -> dict[str, Any]:
    """Add the duties the product owner named, those not on the list yet."""
    if caller.role not in MANAGERS:
        raise DutyForbidden("Only an Executive or SuperAdmin can change the duty list.")
    have = {d["standard_code"] for d in _duties(connection, tenant_id) if d["standard_code"]}
    added = 0
    for standard in STANDARD_DUTIES:
        if standard["standardCode"] in have:
            continue
        spec = {k: v for k, v in standard.items() if k != "standardCode"}
        create_duty(connection, tenant_id=tenant_id, caller=caller, spec=spec, now=now,
                    standard_code=standard["standardCode"])
        added += 1
    return {"added": added}


# --------------------------------------------------------------------------- items

def _applies(duty: dict[str, Any], outlet: dict[str, Any]) -> bool:
    scope = duty["outlet_scope"]
    if scope == "ALL":
        return True
    if scope == "SELECTED":
        return outlet["outlet_id"] in duty["outlet_ids"]
    return outlet["classification"] == scope


def _checklist_day(duty: dict[str, Any], for_date: date, working: set[int]) -> date:
    """The day an item shows on the list: the day it is for (or the next day, for a
    previous-day duty), moved on to the PC's next working day."""
    day = for_date + timedelta(days=1 if int(duty["day_offset"]) == -1 else 0)
    for _ in range(7):
        if day.isoweekday() in working:
            return day
        day += timedelta(days=1)
    return day


def _generate(connection: Connection, *, tenant_id: str, settings: dict[str, Any], duties: list[dict[str, Any]],
              outlets: list[dict[str, Any]], first: date, last: date) -> int:
    """Create the items whose list day falls in ``first``..``last`` and are not there
    yet. Idempotent: the unique key is (duty, outlet, the day it is for)."""
    if first > last or not outlets or settings["enabled_from"] is None:
        return 0
    working = set(settings["working_weekdays"])
    default_carry = int(settings["default_carry_days"])
    wanted: list[tuple[date, dict[str, Any], dict[str, Any], date]] = []
    for_date = first - timedelta(days=8)
    while for_date <= last:
        for duty in duties:
            if duty["status"] != "ACTIVE" or for_date.isoweekday() not in duty["weekdays"]:
                continue
            day = _checklist_day(duty, for_date, working)
            if day < first or day > last or day < duty["effective_from"] or day < settings["enabled_from"]:
                continue
            wanted.extend((day, duty, outlet, for_date) for outlet in outlets if _applies(duty, outlet))
        for_date += timedelta(days=1)
    if not wanted:
        return 0
    outlet_ids = sorted({w[2]["outlet_id"] for w in wanted})
    existing = {
        (str(r["duty_id"]), str(r["outlet_id"]), r["for_date"]) for r in connection.execute(
            text("SELECT duty_id, outlet_id, for_date FROM auditcore.p2_duty_items WHERE tenant_id = :t "
                 "AND outlet_id = ANY(CAST(:o AS uuid[])) AND for_date BETWEEN :a AND :b"),
            {"t": tenant_id, "o": outlet_ids, "a": first - timedelta(days=8), "b": last},
        ).mappings().all()
    }
    # A rolling duty (the bank statement) has one open item at a time per outlet.
    # The next one starts the day after the open one is done or has run out.
    rolling_open: dict[tuple[str, str], date] = {}
    if any(w[1]["rolling"] for w in wanted):
        for r in connection.execute(
            text("SELECT duty_id, outlet_id, "
                 "max(CASE WHEN status = 'PENDING' THEN duty_date + carry_days ELSE done_on END) AS live_until "
                 "FROM auditcore.p2_duty_items "
                 "WHERE tenant_id = :t AND status IN ('PENDING', 'DONE') AND rolling "
                 "AND outlet_id = ANY(CAST(:o AS uuid[])) GROUP BY duty_id, outlet_id"),
            {"t": tenant_id, "o": outlet_ids},
        ).mappings().all():
            rolling_open[(str(r["duty_id"]), str(r["outlet_id"]))] = r["live_until"]
    created = 0
    for day, duty, outlet, for_date in sorted(wanted, key=lambda w: (w[0], w[3])):
        key = (str(duty["duty_id"]), outlet["outlet_id"], for_date)
        if key in existing:
            continue
        carry = int(duty["carry_days"]) if duty["carry_days"] is not None else default_carry
        if duty["rolling"]:
            live_until = rolling_open.get(key[:2])
            if live_until is not None and live_until >= day:
                continue
            rolling_open[key[:2]] = day + timedelta(days=carry)
        created += connection.execute(
            text("""
                INSERT INTO auditcore.p2_duty_items (tenant_id, duty_id, dealer_id, outlet_id, duty_date, for_date,
                  title, description, mandatory, close_mode, checker_code, inputs, allow_not_received, rolling,
                  carry_days, sort_order)
                VALUES (:t, :duty, CAST(:dealer AS uuid), CAST(:outlet AS uuid), :day, :for_date, :title,
                  :description, :mandatory, :close_mode, :checker, CAST(:inputs AS jsonb), :allow, :rolling,
                  :carry, :sort)
                ON CONFLICT (tenant_id, duty_id, outlet_id, for_date) DO NOTHING
            """),
            {"t": tenant_id, "duty": duty["duty_id"], "dealer": outlet["dealer_id"], "outlet": outlet["outlet_id"],
             "day": day, "for_date": for_date, "title": duty["title"], "description": duty["description"],
             "mandatory": duty["mandatory"], "close_mode": duty["close_mode"], "checker": duty["checker_code"],
             "inputs": _json(duty["inputs"]), "allow": duty["allow_not_received"], "rolling": duty["rolling"],
             "carry": carry, "sort": duty["sort_order"]},
        ).rowcount
        existing.add(key)
    return created


def _ensure_generated(connection: Connection, *, tenant_id: str, settings: dict[str, Any], today: date,
                      all_outlets: list[dict[str, Any]], own_outlets: list[dict[str, Any]] | None = None) -> None:
    """Bring the lists up to today: every day since the last one made (for all outlets),
    and today again for the caller's outlets, so a duty or outlet added during the day
    shows without waiting for tomorrow."""
    if not settings["enabled"] or settings["enabled_from"] is None:
        return
    duties = _duties(connection, tenant_id, active_only=True)
    if not duties:
        return
    through = settings["generated_through"]
    start = max(settings["enabled_from"], through + timedelta(days=1) if through else settings["enabled_from"],
                today - timedelta(days=MAX_RANGE_DAYS))
    if start <= today:
        _generate(connection, tenant_id=tenant_id, settings=settings, duties=duties, outlets=all_outlets,
                  first=start, last=today)
        connection.execute(
            text("UPDATE auditcore.p2_duty_settings SET generated_through = :d WHERE tenant_id = :t "
                 "AND (generated_through IS NULL OR generated_through < :d)"),
            {"t": tenant_id, "d": today},
        )
        settings["generated_through"] = today
    else:
        _generate(connection, tenant_id=tenant_id, settings=settings, duties=duties,
                  outlets=own_outlets if own_outlets is not None else all_outlets, first=today, last=today)


_ITEM_FIELDS = """
    i.item_id, i.duty_id, i.dealer_id::text AS dealer_id, i.outlet_id::text AS outlet_id, i.duty_date, i.for_date,
    i.title, i.description, i.mandatory, i.close_mode, i.checker_code, i.inputs, i.allow_not_received, i.rolling,
    i.carry_days, i.sort_order, i.status, i.input_values, i.comment, i.done_at_utc, i.done_on, i.done_by_actor_id,
    i.done_by_name, i.done_source
"""


def _sync_auto(connection: Connection, *, tenant_id: str, items: list[dict[str, Any]],
               now: datetime) -> None:
    """Ask the checkers about the open system-checked items, once per checker for the
    whole list, and tick those whose work is done. Each item gets its ``progress``.

    A checker whose answer can still change during the day (photos on every delivery
    "so far") only ticks an item once its day is over: until then the item shows as
    on track, and a duty the system ticked never comes back. A checker that fails
    leaves its items as they are (``check_failed``) and never fails the request."""
    today = ist_today(now)
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item["status"] == "PENDING" and item["close_mode"] == "AUTO":
            groups.setdefault(item["checker_code"] or "", []).append(item)
    for code, group in groups.items():
        checker = CHECKERS.get(code)
        answers: dict[str, dict[str, Any]] | None = None
        if checker is not None:
            try:
                with connection.begin_nested():
                    answers = checker["check"](connection, tenant_id, group, today)
            except Exception:  # a broken checker must not take the list down
                logger.warning("duty_checker_failed", tenant_id=tenant_id, checker=code, exc_info=True)
        for item in group:
            answer = (answers or {}).get(str(item["item_id"]))
            if answer is None:
                item["check_failed"] = True
                continue
            item["progress"] = answer
            if answer["state"] != "DONE" or not (checker["final"] or item["for_date"] < today):
                continue
            at = answer.get("at") or now
            done_on = max(at.astimezone(IST).date(), item["duty_date"])
            changed = connection.execute(
                text("""
                    UPDATE auditcore.p2_duty_items SET status = 'DONE', done_at_utc = :at, done_on = :on,
                      done_by_actor_id = :actor, done_by_name = :name, done_source = 'SYSTEM', comment = :note
                    WHERE tenant_id = :t AND item_id = :id AND status = 'PENDING'
                """),
                {"t": tenant_id, "id": item["item_id"], "at": at, "on": done_on, "actor": answer.get("actor"),
                 "name": answer.get("name"), "note": answer.get("note")},
            ).rowcount
            if changed:
                _event(connection, tenant_id=tenant_id, event_type="TICKED_BY_SYSTEM", caller=None,
                       item_id=item["item_id"], duty_id=item["duty_id"],
                       details={"checker": code, "note": answer.get("note")}, now=now)
            item.update(status="DONE", done_at_utc=at, done_on=done_on, done_by_actor_id=answer.get("actor"),
                        done_by_name=answer.get("name"), done_source="SYSTEM", comment=answer.get("note"))


def sync_open_items(connection: Connection, *, tenant_id: str, now: datetime | None = None) -> int:
    """Ask the checkers about every open system-checked item still in (or just past) its carry
    days, and tick those whose work is done -- without anyone opening a list (product owner
    2026-11-24: the duties were "not synced well"; an item was only checked when someone read
    it, so the Project Tracker could count a duty done in Verigence as missed). Run by the
    worker every few minutes; returns the items ticked."""
    now = now or datetime.now(UTC)
    settings = _settings(connection, tenant_id)
    if not settings["enabled"]:
        return 0
    today = ist_today(now)
    items = [dict(r) for r in connection.execute(
        text(f"""
            SELECT {_ITEM_FIELDS} FROM auditcore.p2_duty_items i
            WHERE i.tenant_id = :t AND i.status = 'PENDING' AND i.close_mode = 'AUTO'
              AND i.duty_date <= :today AND i.duty_date + i.carry_days >= :since
        """),
        {"t": tenant_id, "today": today, "since": today - timedelta(days=2)},
    ).mappings().all()]
    if not items:
        return 0
    _sync_auto(connection, tenant_id=tenant_id, items=items, now=now)
    return sum(1 for i in items if i["status"] == "DONE")


def _progress(item: dict[str, Any]) -> dict[str, Any] | None:
    answer = item.get("progress")
    if answer is None or item["status"] != "PENDING":
        return None
    return {"state": answer["state"], "note": answer["note"], "done": answer["done"], "total": answer["total"]}


def _on_track(item: dict[str, Any]) -> bool:
    """Open, and the system finds the work done so far (its day is still running)."""
    return item["status"] == "PENDING" and (item.get("progress") or {}).get("state") == "DONE"


def _reasons(connection: Connection, *, tenant_id: str, item_ids: list[Any], first: date,
             last: date) -> dict[tuple[str, date], list[dict[str, Any]]]:
    out: dict[tuple[str, date], list[dict[str, Any]]] = {}
    if not item_ids:
        return out
    rows = connection.execute(
        text("""
            SELECT item_id, on_date, actor_id, actor_name, category, side, remark, handled_by_actor_id,
                   handled_by_name, informed_group, updated_at_utc
            FROM auditcore.p2_duty_reasons
            WHERE tenant_id = :t AND item_id = ANY(CAST(:ids AS uuid[])) AND on_date BETWEEN :a AND :b
            ORDER BY updated_at_utc
        """),
        {"t": tenant_id, "ids": [str(i) for i in item_ids], "a": first, "b": last},
    ).mappings().all()
    for row in rows:
        out.setdefault((str(row["item_id"]), row["on_date"]), []).append(dict(row))
    return out


def _public_reason(row: dict[str, Any]) -> dict[str, Any]:
    known = _NOT_RECEIVED_REASON if row["category"] == NOT_RECEIVED else _REASON.get(row["category"], {})
    return {
        "category": row["category"], "label": known.get("label", row["category"]), "side": row["side"],
        "remark": row["remark"], "byActorId": row["actor_id"], "byName": row["actor_name"] or row["actor_id"],
        "handledByActorId": row["handled_by_actor_id"], "handledByName": row["handled_by_name"],
        "informedGroup": row["informed_group"], "at": _iso(row["updated_at_utc"]),
    }


def _public_item(item: dict[str, Any], *, today: date, reasons: list[dict[str, Any]], caller: Caller) -> dict[str, Any]:
    pending = item["status"] == "PENDING"
    not_received = pending and any(r["category"] == NOT_RECEIVED for r in reasons)
    carried = max((today - item["duty_date"]).days, 0) if pending else 0
    mine = next((r for r in reasons if r["actor_id"] == caller.actor_id), None)
    auto = item["close_mode"] == "AUTO"
    checker = CHECKERS.get(item["checker_code"] or "") if auto else None
    progress = _progress(item)
    # A system-checked duty with nothing in the system to judge by is the PC's to confirm.
    nothing = auto and progress is not None and progress["state"] == "NOTHING"
    if item["status"] == "DONE":
        status = "DONE"
    elif _on_track(item):
        status = "ON_TRACK"
    else:
        status = "NOT_RECEIVED" if not_received else "PENDING"
    return {
        "itemId": str(item["item_id"]), "dutyId": str(item["duty_id"]), "title": item["title"],
        "description": item["description"], "mandatory": bool(item["mandatory"]),
        "dutyDate": item["duty_date"].isoformat(), "forDate": item["for_date"].isoformat(), "status": status,
        "carriedDays": carried, "daysLeft": max(int(item["carry_days"]) - carried, 0) if pending else None,
        "closeMode": item["close_mode"], "checkerCode": item["checker_code"],
        "hint": checker["hint"] if checker else None, "nilLabel": checker["nilLabel"] if checker else None,
        "progress": progress, "checkFailed": bool(item.get("check_failed")),
        "inputs": list(item["inputs"] or []), "values": dict(item["input_values"] or {}), "comment": item["comment"],
        "allowNotReceived": bool(item["allow_not_received"]),
        "doneAt": _iso(item["done_at_utc"]), "doneByName": item["done_by_name"], "doneSource": item["done_source"],
        "canTick": pending and (not auto or nothing),
        "canUndo": (item["status"] == "DONE" and item["done_source"] == "PC"
                    and item["done_by_actor_id"] == caller.actor_id and item["done_on"] == today),
        "myReason": _public_reason(mine) if mine else None,
        "reasons": [_public_reason(r) for r in reasons],
    }


def _pc_scope(caller: Caller, all_outlets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [o for o in all_outlets if any(p["actorId"] == caller.actor_id for p in o["pcs"])]


def my_day(connection: Connection, *, tenant_id: str, caller: Caller, now: datetime | None = None) -> dict[str, Any]:
    """The signed-in PC's list for today, per outlet: today's duties, what is carried
    from earlier days, and what they finished today."""
    now = now or datetime.now(UTC)
    today = ist_today(now)
    settings = _settings(connection, tenant_id)
    base = {"enabled": bool(settings["enabled"]), "date": today.isoformat(), "role": caller.role,
            "applies": False}
    if not settings["enabled"] or caller.role != "PC":
        return base
    all_outlets = _duty_outlets(connection, tenant_id)
    mine = _pc_scope(caller, all_outlets)
    if not mine:
        return base
    _ensure_generated(connection, tenant_id=tenant_id, settings=settings, today=today, all_outlets=all_outlets,
                      own_outlets=mine)
    items = _open_items(connection, tenant_id=tenant_id, outlet_ids=[o["outlet_id"] for o in mine], today=today)
    _sync_auto(connection, tenant_id=tenant_id, items=items, now=now)
    reasons = _reasons(connection, tenant_id=tenant_id, item_ids=[i["item_id"] for i in items], first=today,
                       last=today)
    close = connection.execute(
        text("SELECT closed_at_utc FROM auditcore.p2_duty_day_closes WHERE tenant_id = :t AND actor_id = :a "
             "AND close_date = :d"),
        {"t": tenant_id, "a": caller.actor_id, "d": today},
    ).scalar_one_or_none()
    outlets = []
    counts = {"total": 0, "done": 0, "onTrack": 0, "pending": 0, "mandatoryPending": 0, "needReason": 0}
    for outlet in mine:
        rows = [_public_item(i, today=today, reasons=reasons.get((str(i["item_id"]), today), []), caller=caller)
                for i in items if i["outlet_id"] == outlet["outlet_id"]]
        if not rows:
            continue
        for row in rows:
            counts["total"] += 1
            if row["status"] == "DONE":
                counts["done"] += 1
                continue
            if row["status"] == "ON_TRACK":
                counts["onTrack"] += 1
                continue
            counts["pending"] += 1
            if row["mandatory"]:
                counts["mandatoryPending"] += 1
                if row["status"] != "NOT_RECEIVED" and row["myReason"] is None:
                    counts["needReason"] += 1
        outlets.append({
            "outletId": outlet["outlet_id"], "outletName": outlet["outlet_name"], "dealerName": outlet["dealer_name"],
            "colleagues": [p for p in outlet["pcs"] if p["actorId"] != caller.actor_id], "items": rows,
        })
    return {
        **base, "applies": True, "cutoffTimeIst": settings["cutoff_time_ist"].strftime("%H:%M"),
        "dayClosedAt": _iso(close), "counts": counts, "outlets": outlets,
        "reasonCategories": [dict(r) for r in REASONS],
    }


def _open_items(connection: Connection, *, tenant_id: str, outlet_ids: list[str], today: date) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(f"""
            SELECT {_ITEM_FIELDS} FROM auditcore.p2_duty_items i
            WHERE i.tenant_id = :t AND i.outlet_id = ANY(CAST(:o AS uuid[])) AND i.status <> 'CANCELLED'
              AND i.duty_date <= :today
              AND (i.duty_date = :today
                   OR (i.status = 'PENDING' AND i.duty_date + i.carry_days >= :today)
                   OR (i.status = 'DONE' AND i.done_on = :today))
            ORDER BY i.duty_date, i.sort_order, i.title
        """),
        {"t": tenant_id, "o": outlet_ids, "today": today},
    ).mappings().all()
    return [dict(r) for r in rows]


def _item_for_pc(connection: Connection, *, tenant_id: str, caller: Caller, item_id: UUID) -> dict[str, Any]:
    """The item, locked, for a PC of its outlet with duties on."""
    settings = _settings(connection, tenant_id)
    if not settings["enabled"]:
        raise DutyConflict("Daily duties are switched off for this Project.")
    if caller.role != "PC":
        raise DutyForbidden("Only a PC ticks a daily duty.")
    row = connection.execute(
        text(f"SELECT {_ITEM_FIELDS} FROM auditcore.p2_duty_items i WHERE i.tenant_id = :t AND i.item_id = :id "
             "FOR UPDATE"),
        {"t": tenant_id, "id": item_id},
    ).mappings().first()
    if row is None:
        raise DutyNotFound("Duty not found.")
    item = dict(row)
    # A PC of the outlet: mapped to it, to its dealership, or to the whole Project.
    mapped = connection.execute(
        text(f"""
            SELECT 1 FROM auditcore.business_assignments ba
            WHERE ba.tenant_id = :t AND ba.security_actor_id = :a AND {_ACTIVE}
              AND upper(ba.business_role_code) = 'PC'
              AND (ba.outlet_id = CAST(:o AS uuid)
                   OR (ba.outlet_id IS NULL AND (ba.dealer_id IS NULL OR ba.dealer_id = CAST(:d AS uuid))))
            LIMIT 1
        """),
        {"t": tenant_id, "a": caller.actor_id, "o": item["outlet_id"], "d": item["dealer_id"]},
    ).scalar_one_or_none()
    if mapped is None:
        raise DutyNotFound("Duty not found.")
    if item["status"] == "CANCELLED":
        raise DutyConflict("This duty was taken off the list.")
    return item


def _clean_values(inputs: list[dict[str, Any]], values: dict[str, Any] | None) -> dict[str, Any]:
    values = values or {}
    out: dict[str, Any] = {}
    for field in inputs:
        raw = values.get(field["key"])
        if raw is None or str(raw).strip() == "":
            if field.get("required", True):
                raise DutyError(f"Enter {field['label']}.")
            continue
        if field["type"] == "TEXT":
            out[field["key"]] = str(raw).strip()[:200]
            continue
        try:
            number = Decimal(str(raw).replace(",", "").strip())
        except InvalidOperation as exc:
            raise DutyError(f"{field['label']} must be a number.") from exc
        if not number.is_finite() or number < 0:
            raise DutyError(f"{field['label']} cannot be negative.")
        if field["type"] == "NUMBER":
            if number != number.to_integral_value() or number > 1_000_000:
                raise DutyError(f"{field['label']} must be a whole number.")
            out[field["key"]] = int(number)
        else:
            if number > Decimal(99999999999):
                raise DutyError(f"{field['label']} is too large.")
            out[field["key"]] = float(number.quantize(Decimal("0.01")))
    return out


def tick(connection: Connection, *, tenant_id: str, caller: Caller, item_id: UUID, values: dict[str, Any] | None,
         comment: str | None, now: datetime | None = None) -> dict[str, Any]:
    """A PC of the outlet marks the duty done, with the values it asks for."""
    now = now or datetime.now(UTC)
    today = ist_today(now)
    item = _item_for_pc(connection, tenant_id=tenant_id, caller=caller, item_id=item_id)
    if item["status"] == "DONE":
        raise DutyConflict("This duty is already done.")
    if item["close_mode"] == "AUTO":
        # The system ticks this one; the PC confirms it only when the system has nothing
        # to judge by for the day (no delivery in Verigence, no count of files received).
        _sync_auto(connection, tenant_id=tenant_id, items=[item], now=now)
        if item["status"] == "DONE":
            return my_day(connection, tenant_id=tenant_id, caller=caller, now=now)
        if (item.get("progress") or {}).get("state") != "NOTHING":
            raise DutyConflict(CHECKERS.get(item["checker_code"] or "", {}).get(
                "hint", "This duty is ticked by the system."))
    if item["duty_date"] > today:
        raise DutyConflict("This duty is not due yet.")
    if item["duty_date"] + timedelta(days=int(item["carry_days"])) < today:
        raise DutyConflict("This duty can no longer be ticked: its carry days are over.")
    cleaned = _clean_values(list(item["inputs"] or []), values)
    note = (comment or "").strip()[:1000] or None
    connection.execute(
        text("""
            UPDATE auditcore.p2_duty_items SET status = 'DONE', input_values = CAST(:values AS jsonb),
              comment = :comment, done_at_utc = :now, done_on = :today, done_by_actor_id = :actor,
              done_by_name = :name, done_source = 'PC'
            WHERE tenant_id = :t AND item_id = :id
        """),
        {"t": tenant_id, "id": item_id, "values": _json(cleaned), "comment": note, "now": now, "today": today,
         "actor": caller.actor_id, "name": caller.display_name},
    )
    _event(connection, tenant_id=tenant_id, event_type="TICKED", caller=caller, item_id=item_id,
           duty_id=item["duty_id"], details={"values": cleaned, "comment": note}, now=now)
    return my_day(connection, tenant_id=tenant_id, caller=caller, now=now)


def undo(connection: Connection, *, tenant_id: str, caller: Caller, item_id: UUID,
         now: datetime | None = None) -> dict[str, Any]:
    """The PC takes back their own tick, on the day they made it (PCs make mistakes)."""
    now = now or datetime.now(UTC)
    today = ist_today(now)
    item = _item_for_pc(connection, tenant_id=tenant_id, caller=caller, item_id=item_id)
    if item["status"] != "DONE":
        raise DutyConflict("This duty is not ticked.")
    if item["done_source"] != "PC" or item["done_by_actor_id"] != caller.actor_id or item["done_on"] != today:
        raise DutyConflict("A tick can be taken back only by the person who made it, on the same day.")
    connection.execute(
        text("""
            UPDATE auditcore.p2_duty_items SET status = 'PENDING', input_values = '{}'::jsonb, comment = NULL,
              done_at_utc = NULL, done_on = NULL, done_by_actor_id = NULL, done_by_name = NULL, done_source = NULL
            WHERE tenant_id = :t AND item_id = :id
        """),
        {"t": tenant_id, "id": item_id},
    )
    _event(connection, tenant_id=tenant_id, event_type="UNTICKED", caller=caller, item_id=item_id,
           duty_id=item["duty_id"], details={"values": dict(item["input_values"] or {})}, now=now)
    return my_day(connection, tenant_id=tenant_id, caller=caller, now=now)


def _save_reason(connection: Connection, *, tenant_id: str, caller: Caller, item_id: Any, on_date: date,
                 category: str, side: str, remark: str | None, handled_by: dict[str, Any] | None,
                 now: datetime, informed_group: bool | None = None) -> None:
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_duty_reasons (tenant_id, item_id, on_date, actor_id, actor_name, category, side,
              remark, handled_by_actor_id, handled_by_name, informed_group, created_at_utc, updated_at_utc)
            VALUES (:t, :item, :on_date, :actor, :name, :category, :side, :remark, :hb, :hb_name, :informed,
                    :now, :now)
            ON CONFLICT (tenant_id, item_id, on_date, actor_id) DO UPDATE SET category = EXCLUDED.category,
              side = EXCLUDED.side, remark = EXCLUDED.remark, handled_by_actor_id = EXCLUDED.handled_by_actor_id,
              handled_by_name = EXCLUDED.handled_by_name, actor_name = EXCLUDED.actor_name,
              informed_group = EXCLUDED.informed_group, updated_at_utc = EXCLUDED.updated_at_utc
        """),
        {"t": tenant_id, "item": item_id, "on_date": on_date, "actor": caller.actor_id, "name": caller.display_name,
         "category": category, "side": side, "remark": remark,
         "hb": handled_by["actorId"] if handled_by else None, "hb_name": handled_by["name"] if handled_by else None,
         "informed": informed_group, "now": now},
    )


def not_received(connection: Connection, *, tenant_id: str, caller: Caller, item_id: UUID, comment: str | None,
                 informed_group: bool | None, now: datetime | None = None) -> dict[str, Any]:
    """The dealership did not give it today: the item stays open and is asked again
    the next day (cash ledger, bank statement). The PC also says whether they told the
    relevant WhatsApp group (product owner, 18 Nov 2026: "if the tasks like cash
    register, and bank statement is NO, then pls ask PC if they have intimated the same
    in the relevant whatsapp group")."""
    now = now or datetime.now(UTC)
    today = ist_today(now)
    item = _item_for_pc(connection, tenant_id=tenant_id, caller=caller, item_id=item_id)
    if item["status"] != "PENDING":
        raise DutyConflict("This duty is already done.")
    if not item["allow_not_received"]:
        raise DutyConflict("This duty cannot be marked as not received.")
    if item["duty_date"] + timedelta(days=int(item["carry_days"])) < today:
        raise DutyConflict("This duty's carry days are over.")
    if informed_group is None:
        raise DutyError("Say whether you have informed the relevant WhatsApp group.")
    note = (comment or "").strip()[:500] or None
    _save_reason(connection, tenant_id=tenant_id, caller=caller, item_id=item_id, on_date=today,
                 category=NOT_RECEIVED, side="DEALERSHIP", remark=note, handled_by=None, now=now,
                 informed_group=bool(informed_group))
    _event(connection, tenant_id=tenant_id, event_type="NOT_RECEIVED", caller=caller, item_id=item_id,
           duty_id=item["duty_id"], details={"comment": note, "informedGroup": bool(informed_group)}, now=now)
    return my_day(connection, tenant_id=tenant_id, caller=caller, now=now)


def close_day(connection: Connection, *, tenant_id: str, caller: Caller, reasons: list[dict[str, Any]],
              now: datetime | None = None) -> dict[str, Any]:
    """The PC's day close (at check-out): a reason for every mandatory duty not done.
    A duty marked "not received today" already has its reason. Closing again the same
    day replaces the reasons given before."""
    now = now or datetime.now(UTC)
    today = ist_today(now)
    settings = _settings(connection, tenant_id)
    if not settings["enabled"]:
        raise DutyConflict("Daily duties are switched off for this Project.")
    if caller.role != "PC":
        raise DutyForbidden("Only a PC closes their day.")
    all_outlets = _duty_outlets(connection, tenant_id)
    mine = _pc_scope(caller, all_outlets)
    if not mine:
        raise DutyForbidden("You are not mapped to an outlet of this Project.")
    _ensure_generated(connection, tenant_id=tenant_id, settings=settings, today=today, all_outlets=all_outlets,
                      own_outlets=mine)
    items = _open_items(connection, tenant_id=tenant_id, outlet_ids=[o["outlet_id"] for o in mine], today=today)
    _sync_auto(connection, tenant_id=tenant_id, items=items, now=now)
    # On track (the system finds the work done so far) needs no reason.
    open_items = {str(i["item_id"]): i for i in items if i["status"] == "PENDING" and not _on_track(i)}
    given = _reasons(connection, tenant_id=tenant_id, item_ids=list(open_items), first=today, last=today)
    by_item = {str(r.get("itemId")): r for r in reasons if r.get("itemId")}
    colleagues = {o["outlet_id"]: {p["actorId"]: p for p in o["pcs"] if p["actorId"] != caller.actor_id}
                  for o in mine}
    missing: list[str] = []
    to_save: list[tuple[dict[str, Any], dict[str, Any], str | None, dict[str, Any] | None]] = []
    for item_id, item in open_items.items():
        existing = given.get((item_id, today), [])
        sent = by_item.get(item_id)
        if sent is None:
            covered = any(r["category"] == NOT_RECEIVED or r["actor_id"] == caller.actor_id for r in existing)
            if item["mandatory"] and not covered:
                missing.append(item["title"])
            continue
        known = _REASON.get(str(sent.get("category") or "").upper())
        if known is None:
            raise DutyError(f"'{item['title']}': choose a reason.")
        remark = str(sent.get("remark") or "").strip()[:500] or None
        if known.get("needsRemark") and (remark is None or len(remark) < 3):
            raise DutyError(f"'{item['title']}': say why in a few words.")
        handled_by = None
        if known.get("needsPerson"):
            handled_by = colleagues.get(item["outlet_id"], {}).get(str(sent.get("handledByActorId") or ""))
            if handled_by is None:
                raise DutyError(f"'{item['title']}': choose the PC of this outlet who is doing it.")
        to_save.append((item, known, remark, handled_by))
    if missing:
        raise DutyError("Give a reason for: " + "; ".join(sorted(missing)) + ".")
    for item, known, remark, handled_by in to_save:
        _save_reason(connection, tenant_id=tenant_id, caller=caller, item_id=item["item_id"], on_date=today,
                     category=known["code"], side=known["side"], remark=remark, handled_by=handled_by, now=now)
    open_mandatory = sum(1 for i in open_items.values() if i["mandatory"])
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_duty_day_closes (tenant_id, actor_id, close_date, actor_name, closed_at_utc,
              open_mandatory, open_optional, updated_at_utc)
            VALUES (:t, :a, :d, :name, :now, :m, :o, :now)
            ON CONFLICT (tenant_id, actor_id, close_date) DO UPDATE SET open_mandatory = EXCLUDED.open_mandatory,
              open_optional = EXCLUDED.open_optional, actor_name = EXCLUDED.actor_name,
              updated_at_utc = EXCLUDED.updated_at_utc
        """),
        {"t": tenant_id, "a": caller.actor_id, "d": today, "name": caller.display_name, "now": now,
         "m": open_mandatory, "o": len(open_items) - open_mandatory},
    )
    _event(connection, tenant_id=tenant_id, event_type="DAY_CLOSED", caller=caller,
           details={"date": today.isoformat(), "openMandatory": open_mandatory,
                    "openOptional": len(open_items) - open_mandatory}, now=now)
    return my_day(connection, tenant_id=tenant_id, caller=caller, now=now)


# --------------------------------------------------------------------------- the team's view

def _state_on(item: dict[str, Any], day: date, today: date) -> str | None:
    """What an item was on one day of its life: None when it was not on the list."""
    if day < item["duty_date"]:
        return None
    done_on = item["done_on"] if item["status"] == "DONE" else None
    if done_on is not None and done_on < day:
        return None
    if done_on == day:
        return "DONE" if day == item["duty_date"] else "DONE_LATE"
    last = item["duty_date"] + timedelta(days=int(item["carry_days"]))
    if day > last:
        return None
    if day == last and today > last and done_on is None:
        return "INCOMPLETE"
    return "PENDING"


def team_view(connection: Connection, *, tenant_id: str, caller: Caller, start: date | None = None,
              end: date | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Every outlet and PC, day by day (Team Lead: the outlets under them; PM,
    Executive and SuperAdmin: all). Up to 31 days, never past today."""
    if caller.role not in TEAM_VIEWERS:
        raise DutyForbidden("The duties of the team are for Team Leads, PMs and Executives.")
    now = now or datetime.now(UTC)
    today = ist_today(now)
    settings = _settings(connection, tenant_id)
    end = min(end or today, today)
    start = min(start or end, end)
    if (end - start).days >= MAX_RANGE_DAYS:
        raise DutyError(f"Choose at most {MAX_RANGE_DAYS} days.")
    base = {"enabled": bool(settings["enabled"]), "from": start.isoformat(), "to": end.isoformat(),
            "today": today.isoformat(), "canManage": caller.role in MANAGERS}
    if not settings["enabled"]:
        return base
    all_outlets = _duty_outlets(connection, tenant_id)
    outlets = all_outlets
    if caller.role == "TL":
        wide, own = _own_outlet_ids(connection, tenant_id, caller.actor_id)
        outlets = all_outlets if wide else [o for o in all_outlets if o["outlet_id"] in own]
    _ensure_generated(connection, tenant_id=tenant_id, settings=settings, today=today, all_outlets=all_outlets)
    items = [dict(r) for r in connection.execute(
        text(f"""
            SELECT {_ITEM_FIELDS} FROM auditcore.p2_duty_items i
            WHERE i.tenant_id = :t AND i.outlet_id = ANY(CAST(:o AS uuid[])) AND i.status <> 'CANCELLED'
              AND i.duty_date <= :b AND (i.duty_date + i.carry_days >= :a OR i.done_on >= :a)
            ORDER BY i.sort_order, i.title, i.duty_date
        """),
        {"t": tenant_id, "o": [o["outlet_id"] for o in outlets], "a": start, "b": end},
    ).mappings().all()] if outlets else []
    _sync_auto(connection, tenant_id=tenant_id, items=items, now=now)
    reasons = _reasons(connection, tenant_id=tenant_id, item_ids=[i["item_id"] for i in items], first=start,
                       last=end)
    actors = sorted({p["actorId"] for o in outlets for p in o["pcs"]})
    closes = {
        (r["actor_id"], r["close_date"]): r for r in connection.execute(
            text("SELECT actor_id, close_date, closed_at_utc, open_mandatory FROM auditcore.p2_duty_day_closes "
                 "WHERE tenant_id = :t AND actor_id = ANY(:a) AND close_date BETWEEN :s AND :e"),
            {"t": tenant_id, "a": actors, "s": start, "e": end},
        ).mappings().all()
    } if actors else {}
    working = set(settings["working_weekdays"])
    past_cutoff = now.astimezone(IST).time() >= settings["cutoff_time_ist"]
    totals = {"done": 0, "doneLate": 0, "onTrack": 0, "pending": 0, "notReceived": 0, "incomplete": 0}
    per_duty: dict[str, dict[str, Any]] = {}
    per_pc: dict[str, dict[str, Any]] = {}
    days: list[dict[str, Any]] = []
    day = end
    while day >= start:
        day_outlets = []
        for outlet in outlets:
            cells = []
            for item in items:
                if item["outlet_id"] != outlet["outlet_id"]:
                    continue
                state = _state_on(item, day, today)
                if state is None:
                    continue
                on_day = reasons.get((str(item["item_id"]), day), [])
                if state == "PENDING" and day == today and _on_track(item):
                    state = "ON_TRACK"
                elif state == "PENDING" and any(r["category"] == NOT_RECEIVED for r in on_day):
                    state = "NOT_RECEIVED"
                key = {"DONE": "done", "DONE_LATE": "doneLate", "ON_TRACK": "onTrack", "PENDING": "pending",
                       "NOT_RECEIVED": "notReceived", "INCOMPLETE": "incomplete"}[state]
                duty = per_duty.setdefault(str(item["duty_id"]), {
                    "dutyId": str(item["duty_id"]), "title": item["title"], "sortOrder": int(item["sort_order"]),
                    "done": 0, "doneLate": 0, "onTrack": 0, "pending": 0, "notReceived": 0, "incomplete": 0})
                # Pending is what is open now: one count per item, on today's row.
                if state != "PENDING" or day == today:
                    totals[key] += 1
                    duty[key] += 1
                done = state in {"DONE", "DONE_LATE"}
                cells.append({
                    "itemId": str(item["item_id"]), "dutyId": str(item["duty_id"]), "title": item["title"],
                    "mandatory": bool(item["mandatory"]), "forDate": item["for_date"].isoformat(),
                    "dutyDate": item["duty_date"].isoformat(), "state": state,
                    "carriedDays": (day - item["duty_date"]).days,
                    "doneAt": _iso(item["done_at_utc"]) if done else None,
                    "doneByName": item["done_by_name"] if done else None,
                    "doneSource": item["done_source"] if done else None,
                    "values": dict(item["input_values"] or {}) if done else {},
                    "inputs": list(item["inputs"] or []) if done else [],
                    "comment": item["comment"] if done else None,
                    "progress": _progress(item) if day == min(today, item["duty_date"] + timedelta(
                        days=int(item["carry_days"]))) else None,
                    "reasons": [_public_reason(r) for r in on_day],
                })
            if not cells:
                continue
            pcs = []
            for pc in outlet["pcs"]:
                close = closes.get((pc["actorId"], day))
                if close is not None:
                    state = "CLOSED"
                elif day.isoweekday() not in working:
                    state = "OFF"
                elif day < today or past_cutoff:
                    state = "NOT_CLOSED"
                else:
                    state = "OPEN"
                pcs.append({"actorId": pc["actorId"], "name": pc["name"], "dayClose": state,
                            "closedAt": _iso(close["closed_at_utc"]) if close else None})
                person = per_pc.setdefault(pc["actorId"], {"actorId": pc["actorId"], "name": pc["name"],
                                                           "days": {}})
                person["days"].setdefault(day, state)
            day_outlets.append({
                "outletId": outlet["outlet_id"], "outletName": outlet["outlet_name"],
                "dealerName": outlet["dealer_name"], "pcs": pcs, "items": cells,
            })
        days.append({"date": day.isoformat(), "workingDay": day.isoweekday() in working, "outlets": day_outlets})
        day -= timedelta(days=1)
    people = []
    for person in per_pc.values():
        states = list(person["days"].values())
        people.append({"actorId": person["actorId"], "name": person["name"],
                       "daysClosed": states.count("CLOSED"), "daysNotClosed": states.count("NOT_CLOSED")})
    return {
        **base, "cutoffTimeIst": settings["cutoff_time_ist"].strftime("%H:%M"), "totals": totals,
        "byDuty": sorted(per_duty.values(), key=lambda d: (d["sortOrder"], d["title"])),
        "byPc": sorted(people, key=lambda p: p["name"].lower()), "days": days,
    }
