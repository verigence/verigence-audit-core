"""What each PC actually did on their daily duties, day by day (product owner 2026-11-24).

"daily tasks is at max worthless tab.. no meaningful representation, its better if you can show
me if any PC updates cash registers, bank statement, and or uploaded pics in the non extracted
folder". One row a PC, for the TL's outlets (PM, Executive, SuperAdmin: all), over up to 31 days:

    Gate register   the outlets' "Gate out register" duty for the day: done (on the day or late)
                    or not done
    Cash register   the outlets' "Cash ledger" duty: the amount and number of transactions the PC
                    entered, "not received" (from the dealership), or nothing
    Bank statement  statements the PC uploaded that day (p2_bank_statements)
    Photos          photos the PC added that day (delivery_vehicle_photos), by where they came from:
                    the "photo" folder of a customer's Drive folder (kept, not read), the app's
                    camera, and pages the system could not read and kept as photos

A duty of today is pending until the day is over; a past day's duty not done is not done. Read-only.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

from audit_core.daily_duties import (
    MAX_RANGE_DAYS,
    NOT_RECEIVED,
    TEAM_VIEWERS,
    DutyError,
    DutyForbidden,
    _duty_outlets,
    _own_outlet_ids,
    ist_today,
)
from audit_core.task_manager import Caller
from audit_core.uc03_p2_object_keys import IST

GATE, CASH = "GATE_REGISTER", "CASH_LEDGER"
# Photos from a customer's Drive folder carry the Drive file in their upload id (uc03_p2_drive_intake);
# pages the system could not read are kept as photos under their own source (uc03_p2_vehicle_photos).
DRIVE_PREFIX, UNREAD_SOURCE = "drive-", "P2_UNRECOGNISED"


def _number(value: Any) -> float | None:
    try:
        return float(Decimal(str(value)))
    except Exception:  # noqa: BLE001 -- a value the PC typed oddly is shown as not a number
        return None


def evidence_view(connection: Connection, *, tenant_id: str, caller: Caller, start: date | None = None,
                  end: date | None = None, now: datetime | None = None) -> dict[str, Any]:
    if caller.role not in TEAM_VIEWERS:
        raise DutyForbidden("The duties of the team are for Team Leads, PMs and Executives.")
    now = now or datetime.now(UTC)
    today = ist_today(now)
    end = min(end or today, today)
    start = min(start or end - timedelta(days=6), end)
    if (end - start).days >= MAX_RANGE_DAYS:
        raise DutyError(f"Choose at most {MAX_RANGE_DAYS} days.")
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]

    outlets = _duty_outlets(connection, tenant_id)
    if caller.role == "TL":
        wide, own = _own_outlet_ids(connection, tenant_id, caller.actor_id)
        outlets = outlets if wide else [o for o in outlets if o["outlet_id"] in own]
    pcs: dict[str, dict[str, Any]] = {}
    for o in outlets:
        for pc in o["pcs"]:
            person = pcs.setdefault(pc["actorId"], {"actorId": pc["actorId"], "name": pc["name"], "outlets": []})
            person["outlets"].append(o)
    base = {"from": start.isoformat(), "to": end.isoformat(), "today": today.isoformat(),
            "days": [d.isoformat() for d in days], "pcs": []}
    if not pcs:
        return base
    outlet_ids = sorted({o["outlet_id"] for o in outlets})
    actors = sorted(pcs)

    # The gate register and cash ledger duties of the outlets, by outlet and day.
    duty_rows = connection.execute(text("""
        SELECT d.standard_code AS code, i.item_id, CAST(i.outlet_id AS text) AS outlet, i.duty_date, i.status,
               i.done_on, i.input_values, i.done_by_name
        FROM auditcore.p2_duty_items i
        JOIN auditcore.p2_duties d ON d.tenant_id = i.tenant_id AND d.duty_id = i.duty_id
        WHERE i.tenant_id = :t AND d.standard_code = ANY(:codes) AND i.status <> 'CANCELLED'
          AND i.outlet_id = ANY(CAST(:o AS uuid[])) AND i.duty_date BETWEEN :a AND :b
    """), {"t": tenant_id, "codes": [GATE, CASH], "o": outlet_ids, "a": start, "b": end}).mappings().all()
    not_received = {(str(r["item_id"]), r["on_date"]) for r in connection.execute(text("""
        SELECT item_id, on_date FROM auditcore.p2_duty_reasons
        WHERE tenant_id = :t AND category = :c AND item_id = ANY(CAST(:ids AS uuid[]))
    """), {"t": tenant_id, "c": NOT_RECEIVED, "ids": [str(r["item_id"]) for r in duty_rows]}).mappings()
    } if duty_rows else set()
    duty: dict[tuple[str, str, date], dict[str, Any]] = {}
    for r in duty_rows:
        if r["status"] == "DONE":
            state = "DONE" if r["done_on"] is not None and r["done_on"] <= r["duty_date"] else "LATE"
        elif any((str(r["item_id"]), d) in not_received for d in days if d >= r["duty_date"]):
            state = "NOT_RECEIVED"
        elif r["duty_date"] >= today:
            state = "PENDING"
        else:
            state = "NOT_DONE"
        values = dict(r["input_values"] or {}) if r["status"] == "DONE" else {}
        duty[(r["code"], r["outlet"], r["duty_date"])] = {
            "state": state, "amount": _number(values.get("amount")),
            "transactions": _number(values.get("transactions")), "by": r["done_by_name"]}

    # What each PC uploaded, by IST day.
    statements: dict[tuple[str, date], int] = defaultdict(int)
    for r in connection.execute(text("""
        SELECT uploaded_by AS actor, (uploaded_at_utc AT TIME ZONE 'Asia/Kolkata')::date AS day, count(*) AS n
        FROM auditcore.p2_bank_statements
        WHERE tenant_id = :t AND uploaded_by = ANY(:a)
          AND uploaded_at_utc >= :from_utc AND uploaded_at_utc < :to_utc
        GROUP BY 1, 2
    """), {"t": tenant_id, "a": actors, **_utc_window(start, end)}).mappings():
        statements[(str(r["actor"]), r["day"])] += int(r["n"])
    photos: dict[tuple[str, date], dict[str, int]] = defaultdict(lambda: {"drive": 0, "camera": 0, "unread": 0})
    for r in connection.execute(text("""
        SELECT uploaded_by_actor_id AS actor, (uploaded_at_utc AT TIME ZONE 'Asia/Kolkata')::date AS day,
               CASE WHEN capture_source = :unread THEN 'unread'
                    WHEN client_upload_id LIKE :drive THEN 'drive' ELSE 'camera' END AS source,
               count(*) AS n
        FROM auditcore.delivery_vehicle_photos
        WHERE tenant_id = :t AND deleted_at_utc IS NULL AND uploaded_by_actor_id = ANY(:a)
          AND uploaded_at_utc >= :from_utc AND uploaded_at_utc < :to_utc
        GROUP BY 1, 2, 3
    """), {"t": tenant_id, "a": actors, "unread": UNREAD_SOURCE, "drive": f"{DRIVE_PREFIX}%",
           **_utc_window(start, end)}).mappings():
        photos[(str(r["actor"]), r["day"])][r["source"]] += int(r["n"])

    rows = []
    for person in sorted(pcs.values(), key=lambda p: str(p["name"]).lower()):
        mine = person["outlets"]
        summary: dict[str, Any] = {"gate": _blank_duty(), "cash": {**_blank_duty(), "amount": 0.0, "transactions": 0.0},
                   "statements": 0, "photos": {"drive": 0, "camera": 0, "unread": 0}}
        day_rows = []
        for d in days:
            cells: dict[str, Any] = {}
            for key, code in (("gate", GATE), ("cash", CASH)):
                per_outlet = [{"outletName": o["outlet_name"], **duty[(code, o["outlet_id"], d)]}
                              for o in mine if (code, o["outlet_id"], d) in duty]
                counts = _blank_duty()
                for c in per_outlet:
                    counts[_KEY[c["state"]]] += 1
                    if key == "cash" and c["state"] in ("DONE", "LATE"):
                        summary["cash"]["amount"] += c["amount"] or 0
                        summary["cash"]["transactions"] += c["transactions"] or 0
                for k, v in counts.items():
                    summary[key][k] += v
                cells[key] = {**counts, "outlets": per_outlet}
            n_statements = statements.get((person["actorId"], d), 0)
            day_photos = dict(photos.get((person["actorId"], d), {"drive": 0, "camera": 0, "unread": 0}))
            summary["statements"] += n_statements
            for k, v in day_photos.items():
                summary["photos"][k] += v
            day_rows.append({"date": d.isoformat(), **cells, "statements": n_statements, "photos": day_photos})
        rows.append({"actorId": person["actorId"], "name": person["name"],
                     "outlets": [f"{o['dealer_name']} - {o['outlet_name']}" for o in mine],
                     "summary": summary, "days": day_rows})
    return {**base, "pcs": rows}


_KEY = {"DONE": "done", "LATE": "late", "NOT_RECEIVED": "notReceived", "PENDING": "pending", "NOT_DONE": "notDone"}


def _blank_duty() -> dict[str, Any]:
    return {"done": 0, "late": 0, "notReceived": 0, "pending": 0, "notDone": 0}


def _utc_window(start: date, end: date) -> dict[str, datetime]:
    """The IST days start..end as a UTC range, so the uploaded-at indexes are used."""
    from datetime import time

    return {"from_utc": datetime.combine(start, time.min, IST).astimezone(UTC),
            "to_utc": datetime.combine(end + timedelta(days=1), time.min, IST).astimezone(UTC)}


__all__ = ["evidence_view"]
