"""PCs' day: one window of what every PC did on a day (product owner 2026-11-23: "a single
window where i can see what all my PC are doing in a day").

``GET /p2/v1/tenants/{t}/reports/pc-day?date=YYYY-MM-DD`` (Team Lead, PM, Executive, Project
administrator) and ``GET /v1/admin/tenants/{t}/task-manager/pc-day?date=`` (Superadmin).

One row per PC of the caller's outlets, with:
Journeys     new bookings, new deliveries, Journeys updated (uc03_p2_upload_kind: the
             Project Tracker's rule) and the files behind them by channel (direct, bulk, Drive)
Tasks        tasks worked (Journey tasks acted on, Task Manager tasks done: the engagement
             report's figure) and tasks closed (uc03_p2_tasks_closed: the Project Tracker's)
Files closed the PC's delivery files closed that day (the Daily processing report's rule)
Duties       each daily duty due that day: done on time, late, not done, pending
Gate register the register for the day before (the one the PC uploads that day) at each of
             their outlets: uploaded or missed, deliveries on it against those in the tool
Pending now  deliveries not uploaded, delivery files not closed, open and overdue tasks
Time on Verigence first and last use, active time by device, the longest stretch with no
             output -- Executive and Superadmin only (product owner 2026-10-28: activity is
             theirs to see)

Nothing here is counted afresh: every figure is the one the Project Tracker or the engagement
report shows, read through the same functions (and their two-minute cache), so this window
and those screens cannot disagree.
"""
from __future__ import annotations

import threading
import time as _time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Connection, text

from audit_core.uc03_p2_project_tracker import _FALLBACK_TIMEZONE, _scope, _zone

_CACHE_SECONDS = 120.0
_cache: dict[tuple[str, str], tuple[float, Any]] = {}
_cache_lock = threading.Lock()

_PENDING = ("deliveriesNotUploaded", "deliveryFilesOpen", "tasksOpen", "tasksOverdue", "paymentsNotReconciled",
            "pagesToReupload")
_CHANNELS = ("direct", "bulk", "drive")


def _engagement(connection: Connection, tenant_id: str, day: date, now: datetime, use_cache: bool) -> dict[str, Any]:
    """The engagement report for the one day, per person (kept two minutes, whoever asks)."""
    from audit_core.engagement_reports import build_engagement

    key = (tenant_id, day.isoformat())
    if use_cache:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and _time.monotonic() - hit[0] < _CACHE_SECONDS:
                return hit[1]
    report = build_engagement(connection, tenant_id=tenant_id, start_day=day, end_day=day, period="day", now=now)
    people = {p["actorId"]: p for p in report["people"]}
    if use_cache:
        with _cache_lock:
            if len(_cache) > 32:
                _cache.clear()
            _cache[key] = (_time.monotonic(), people)
    return people


def _outlets(connection: Connection, tenant_id: str) -> dict[str, dict[str, Any]]:
    return {str(r["outlet_id"]): dict(r) for r in connection.execute(text("""
        SELECT o.outlet_id, o.outlet_name, o.outlet_classification, o.dealer_id, d.dealer_name
        FROM auditcore.dealer_outlets o
        JOIN auditcore.dealers d ON d.tenant_id = o.tenant_id AND d.dealer_id = o.dealer_id
        WHERE o.tenant_id = :t
    """), {"t": tenant_id}).mappings()}


def build_pc_day(connection: Connection, *, tenant_id: str, actor_id: str, day: date | None, admin: bool,
                 show_activity: bool, now: datetime | None = None, use_cache: bool = True) -> dict[str, Any]:
    from audit_core.uc03_p2_my_work import gate_register
    from audit_core.uc03_p2_performance import _cached, _part
    from audit_core.uc03_p2_team_insights import _register_outlets

    now = now or datetime.now(UTC)
    tz = connection.execute(
        text("SELECT timezone_name FROM auditcore.projects WHERE tenant_id = :t"), {"t": tenant_id},
    ).scalar_one_or_none() or _FALLBACK_TIMEZONE
    today = now.astimezone(_zone(tz)).date()
    day = day or today
    if day > today:
        raise HTTPException(status_code=422, detail="That day has not come yet.")
    iso = day.isoformat()

    def part(name: str) -> Any:
        return _part(connection, name, tenant_id=tenant_id, first=day, last=day, tz=tz, now=now, use_cache=use_cache)

    data = _cached(connection, tenant_id=tenant_id, first=day, last=day, tz=tz, now=now, use_cache=use_cache)
    values, people, names = data["values"], data["people"], data["names"]
    flow, channels, backlog = part("flow")["byPerson"], part("channels"), part("backlog")["byPerson"]
    engagement = _engagement(connection, tenant_id, day, now, use_cache)

    # Whose day the caller reads: the PCs of their outlets (all of them for an administrator or a
    # Project-wide mapping), as on the Project Tracker.
    wide, scope = _scope(connection, tenant_id=tenant_id, actor_id=actor_id, admin=admin)
    outlet_ids = {str(o["outlet_id"]) for o in scope}
    dealer_ids = {str(o["dealer_id"]) for o in scope}
    pcs = [a for a, p in people.items() if p["role"] == "PC"
           and (wide or p["outlets"] & outlet_ids or p["dealers"] & dealer_ids)]
    outlets = _outlets(connection, tenant_id)

    def pc_outlets(a: str) -> list[str]:
        mine = set(people[a]["outlets"])
        mine |= {o for o, row in outlets.items() if str(row["dealer_id"]) in people[a]["dealers"]}
        return sorted(mine, key=lambda o: (str(outlets.get(o, {}).get("dealer_name") or ""),
                                           str(outlets.get(o, {}).get("outlet_name") or "")))

    # The gate register the PC uploads on the day: the day before's, at each of their outlets.
    register_day = day - timedelta(days=1)
    owing, starts = _register_outlets(connection, tenant_id)
    wanted = {o for a in pcs for o in pc_outlets(a)}
    registers: dict[str, dict[str, Any]] = {}
    if starts is not None and register_day >= starts:
        rows = [o for o in owing if str(o["outlet_id"]) in wanted]
        for oid, entries in gate_register(connection, tenant_id, rows, register_day, register_day, now=now).items():
            for e in entries:
                registers[oid] = e

    schedules = {s["scheduleId"]: s["title"] for s in data["schedules"]}
    out_rows: list[dict[str, Any]] = []
    for a in pcs:
        v = values.get(a, {})
        f = flow.get(a, {}).get(iso, {})
        files = {c: int(channels["byPerson"].get(a, {}).get(c, {}).get(iso, 0)) for c in _CHANNELS}
        e = engagement.get(a) or {}
        t = e.get("totals") or {}
        d = next((x for x in e.get("days") or [] if x.get("day") == iso), {})
        duties = []
        for schedule_id, by_day in data["duties"].get(a, {}).items():
            cell = by_day.get(iso)
            if cell and cell["due"]:
                duties.append({"title": schedules.get(schedule_id, "Duty"), **cell})
        duties.sort(key=lambda x: x["title"].lower())
        mine = pc_outlets(a)
        register = []
        for o in mine:
            r = registers.get(o)
            if r is None:
                continue
            register.append({"outletId": o, "outletName": outlets.get(o, {}).get("outlet_name"),
                             "date": r["date"], "status": r["status"], "onRegister": r["total"],
                             "inTool": r["uploadedInTool"], "notIntimated": r["notIntimated"],
                             "pendingWithPc": r["pendingWithPc"], "uploadedLate": r["uploadedLate"],
                             "uploadedBy": names.get(r["pcActorId"]) if r["pcActorId"] else None,
                             "byThisPc": r["pcActorId"] == a})
        pending = {k: int(sum(backlog.get(a, {}).get(k, []))) for k in _PENDING}
        row: dict[str, Any] = {
            "actorId": a, "name": names.get(a),
            "outlets": [{"outletId": o, "outletName": outlets.get(o, {}).get("outlet_name"),
                         "dealerName": outlets.get(o, {}).get("dealer_name")} for o in mine],
            "journeys": {"bookings": int(f.get("bookings", 0)), "deliveries": int(f.get("deliveries", 0)),
                         "updated": int(f.get("updated", 0))},
            "files": {**files, "total": sum(files.values())},
            "pagesUploaded": int(v.get("pagesUploaded", {}).get(iso, 0)),
            "pagesRejected": int(v.get("pagesRejected", {}).get(iso, 0)),
            "tasksWorked": int(t.get("journeyTasksWorked", 0)) + int(t.get("tasksCompleted", 0)),
            "tasksClosed": int(v.get("tasksClosed", {}).get(iso, 0)),
            "deliveryFilesClosed": int(v.get("journeysClosed", {}).get(iso, 0)),
            "duties": duties,
            "gateRegister": register,
            "pending": pending,
        }
        row["didWork"] = bool(row["files"]["total"] or row["tasksClosed"] or row["tasksWorked"]
                              or row["deliveryFilesClosed"] or any(x["onTime"] + x["late"] for x in duties)
                              or any(g["byThisPc"] for g in register))
        if show_activity:
            row["activity"] = {
                "activeMinutes": int(t.get("activeMinutes", 0)),
                "bySource": t.get("activeMinutesBySource") or {},
                "firstActiveAtUtc": t.get("firstActiveAtUtc"), "lastActiveAtUtc": t.get("lastActiveAtUtc"),
                "longestNoOutputMinutes": d.get("longestNoOutputMinutes"),
                "longestNoOutputFrom": d.get("longestNoOutputFrom"), "longestNoOutputTo": d.get("longestNoOutputTo"),
                "productiveMinutes": d.get("productiveMinutes"), "activeNoOutputMinutes": d.get("activeNoOutputMinutes"),
            }
            row["seen"] = bool(row["activity"]["activeMinutes"]) or row["didWork"]
        else:
            row["seen"] = row["didWork"]
        out_rows.append(row)

    out_rows.sort(key=lambda r: (not r["seen"], -(r["journeys"]["bookings"] + r["journeys"]["deliveries"]
                                                   + r["journeys"]["updated"]), (r["name"] or "").lower()))

    def total(key: str, sub: str | None = None) -> int:
        return sum(int(r[key][sub] if sub else r[key]) for r in out_rows)

    duty_totals = {k: sum(x[k] for r in out_rows for x in r["duties"]) for k in ("due", "onTime", "late", "notDone",
                                                                               "pending")}
    registers_out = [g for r in out_rows for g in r["gateRegister"]]
    seen_outlets: set[str] = set()
    unique_registers = [g for g in registers_out if not (g["outletId"] in seen_outlets or seen_outlets.add(g["outletId"]))]
    return {
        "date": iso, "today": today.isoformat(), "current": day == today, "asOfUtc": now.isoformat(),
        "timezone": tz, "registerDate": register_day.isoformat(), "showsActivity": show_activity,
        "totals": {
            "pcs": len(out_rows), "seen": sum(1 for r in out_rows if r["seen"]),
            "bookings": total("journeys", "bookings"), "deliveries": total("journeys", "deliveries"),
            "updated": total("journeys", "updated"),
            "files": {c: total("files", c) for c in (*_CHANNELS, "total")},
            "tasksWorked": total("tasksWorked"), "tasksClosed": total("tasksClosed"),
            "deliveryFilesClosed": total("deliveryFilesClosed"), "pagesRejected": total("pagesRejected"),
            "duties": duty_totals,
            "registers": {"outlets": len(unique_registers),
                          "missed": sum(1 for g in unique_registers if g["status"] == "MISSED"),
                          "uploaded": sum(1 for g in unique_registers if g["status"] not in ("MISSED", "NOT_UPLOADED"))},
            "pending": {k: sum(r["pending"][k] for r in out_rows) for k in _PENDING},
        },
        "people": out_rows,
        "notes": [
            ("New bookings, new deliveries and Journeys updated: a Journey counts once a day for the PC who "
             "uploaded, as on the Project Tracker. Files are every file behind them, by how they came in."),
            ("Tasks worked: Journey tasks acted on in any way plus Task Manager tasks done (the engagement report). "
             "Tasks closed: the tasks the PC closed (the Project Tracker)."),
            f"Gate register: the register for {register_day.isoformat()}, which the PC uploads on {iso}.",
            "Pending is as of now, whatever day is picked.",
        ],
    }
