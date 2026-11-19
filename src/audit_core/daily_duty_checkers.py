"""What the system can read for a daily duty (docs/DAILY_DUTIES_DESIGN_v1.0.md).

Product owner, 18 Nov 2026: "PC needs to be asked first if the task is completed like
cash register and all, for others like physical deliveries it can be read directly
from the system."

A checker answers one question for a batch of duty items, each an (outlet, day): is
the work done? It only reads -- the gate register, the uploads, the vehicle photos,
the bank statements and the tasks know nothing of duties -- and it reads once for the whole
batch, never once per item. When one of those modules changes, its checker here is
the only thing that follows.

Each answer is ``{"state", "done", "total", "note", "at", "actor", "name"}``:

DONE      the work is there (``at``: when it got there, ``actor`` / ``name``: by whom)
PENDING   not yet; ``note`` says what is missing ("Photos on 3 of 5 deliveries")
NOTHING   the system has nothing to judge by for that day (no delivery in Verigence,
          no count of files received): the PC confirms with a tick

``final`` says whether DONE can still change. An uploaded gate register stays
uploaded; "every delivery of today has photos" is only true so far while the day
runs, so that duty is ticked for good once its day is over (daily_duties._sync_auto).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import Connection, text

from audit_core.uc03_p2_object_keys import IST

# What counts as a person working on a task, and closing one: the Project Tracker's own
# lists, so the duty and the tracker never count differently.
from audit_core.uc03_p2_project_tracker import _CLOSING_EVENTS as TASK_CLOSING_EVENTS
from audit_core.uc03_p2_project_tracker import _WORK_EVENTS as TASK_WORK_EVENTS

_TZ = "Asia/Kolkata"


def _answer(state: str, note: str, *, done: int | None = None, total: int | None = None,
            at: datetime | None = None, actor: str | None = None, name: str | None = None) -> dict[str, Any]:
    return {"state": state, "note": note, "done": done, "total": total, "at": at, "actor": actor, "name": name}


def _span(items: list[dict[str, Any]]) -> tuple[list[str], date, date]:
    return (sorted({i["outlet_id"] for i in items}), min(i["for_date"] for i in items),
            max(i["for_date"] for i in items))


def _ist_start(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=IST)


def _label(day: date) -> str:
    return f"{day.day} {day.strftime('%b')}"


# --------------------------------------------------------------------------- gate out register

def check_gate_register(connection: Connection, tenant_id: str, items: list[dict[str, Any]],
                        today: date) -> dict[str, dict[str, Any]]:
    """The outlet's gate out register of the day is uploaded and its counts entered
    (uc03_p2_gate_pass: the ACTIVE register with ``completed_at_utc``)."""
    outlets, first, last = _span(items)
    found = {
        (str(r["outlet_id"]), r["business_date"]): r for r in connection.execute(
            text("""
                SELECT outlet_id, business_date, completed_at_utc, pc_actor_id, pc_name
                FROM auditcore.p2_gate_passes
                WHERE tenant_id = :t AND status = 'ACTIVE' AND outlet_id = ANY(CAST(:o AS uuid[]))
                  AND business_date BETWEEN :a AND :b
            """),
            {"t": tenant_id, "o": outlets, "a": first, "b": last},
        ).mappings().all()
    }
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        row = found.get((item["outlet_id"], item["for_date"]))
        if row is None:
            out[str(item["item_id"])] = _answer("PENDING", f"Register of {_label(item['for_date'])} is not uploaded")
        elif row["completed_at_utc"] is None:
            out[str(item["item_id"])] = _answer("PENDING", "Register uploaded; its counts are not entered yet")
        else:
            out[str(item["item_id"])] = _answer("DONE", "Register uploaded", at=row["completed_at_utc"],
                                                actor=row["pc_actor_id"], name=row["pc_name"])
    return out


# --------------------------------------------------------------------------- bank statement

def check_bank_statement(connection: Connection, tenant_id: str, items: list[dict[str, Any]],
                         today: date) -> dict[str, dict[str, Any]]:
    """A bank statement of the outlet, or of its dealership, was uploaded on or after
    the day the item came on the list (p2_bank_statements). One PC picks it up: the
    first upload answers it for every PC of the outlet."""
    dealers = sorted({i["dealer_id"] for i in items})
    since = min(i["duty_date"] for i in items)
    rows = connection.execute(
        text("""
            SELECT dealer_id::text AS dealer_id, outlet_id::text AS outlet_id, uploaded_at_utc, uploaded_by,
                   uploaded_by_name
            FROM auditcore.p2_bank_statements
            WHERE tenant_id = :t AND dealer_id = ANY(CAST(:d AS uuid[])) AND uploaded_at_utc >= :since
            ORDER BY uploaded_at_utc
        """),
        {"t": tenant_id, "d": dealers, "since": _ist_start(since)},
    ).mappings().all()
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        hit = next((r for r in rows
                    if r["dealer_id"] == item["dealer_id"]
                    and (r["outlet_id"] is None or r["outlet_id"] == item["outlet_id"])
                    and r["uploaded_at_utc"].astimezone(IST).date() >= item["duty_date"]), None)
        if hit is None:
            out[str(item["item_id"])] = _answer(
                "PENDING", f"No bank statement uploaded since {_label(item['duty_date'])}")
        else:
            out[str(item["item_id"])] = _answer("DONE", "Bank statement uploaded", at=hit["uploaded_at_utc"],
                                                actor=hit["uploaded_by"], name=hit["uploaded_by_name"])
    return out


# --------------------------------------------------------------------------- vehicle photos

def check_vehicle_photos(connection: Connection, tenant_id: str, items: list[dict[str, Any]],
                         today: date) -> dict[str, dict[str, Any]]:
    """Physical delivery verification: every delivery of the outlet on the day has a
    photograph of the vehicle (the booking's Vehicle photos, delivery_vehicle_photos).

    The deliveries of a day, as far as Verigence knows them: the Journeys whose vehicle
    gate pass is dated that day (the gate register's own rule, uc03_p2_gate_pass
    .vehicle_gate_pass_deliveries) and the Journeys photographed that day. A photo
    counts by the delivery review's rule (live, not an Extras page)."""
    outlets, first, last = _span(items)
    delivered: dict[tuple[str, date], set[str]] = {}
    for r in connection.execute(
        text("""
            WITH dated AS (
                SELECT DISTINCT ON (f.journey_id, f.di_document_id) f.journey_id, j.outlet_id::text AS outlet_id,
                       f.effective_value #>> '{}' AS v
                FROM auditcore.journey_document_extracted_fields f
                JOIN auditcore.journeys j
                  ON j.tenant_id = f.tenant_id AND j.journey_id = f.journey_id
                 AND j.outlet_id = ANY(CAST(:o AS uuid[]))
                JOIN auditcore.evidence e
                  ON e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id
                 AND e.di_document_id = f.di_document_id AND e.association_status = 'ACTIVE'
                WHERE f.tenant_id = :t AND f.field_key = 'delivery_date'
                  AND COALESCE(e.document_type_key, f.source_document_type_key) = 'gate_pass'
                ORDER BY f.journey_id, f.di_document_id, f.source_fact_version DESC NULLS LAST,
                         f.updated_at_utc DESC
            )
            SELECT DISTINCT journey_id::text AS journey_id, outlet_id, v FROM dated
            WHERE v ~ '^\\d{4}-\\d{2}-\\d{2}$' AND v BETWEEN :a AND :b
        """),
        {"t": tenant_id, "o": outlets, "a": first.isoformat(), "b": last.isoformat()},
    ).mappings().all():
        try:
            day = date.fromisoformat(r["v"])
        except ValueError:
            continue
        delivered.setdefault((r["outlet_id"], day), set()).add(r["journey_id"])
    known = sorted({j for group in delivered.values() for j in group})
    photographed: dict[tuple[str, date], set[str]] = {}
    last_photo: dict[str, datetime] = {}
    for r in connection.execute(
        text("""
            SELECT p.journey_id::text AS journey_id, j.outlet_id::text AS outlet_id,
                   CAST(p.uploaded_at_utc AT TIME ZONE :tz AS date) AS day, max(p.uploaded_at_utc) AS last_at
            FROM auditcore.delivery_vehicle_photos p
            JOIN auditcore.journeys j ON j.tenant_id = p.tenant_id AND j.journey_id = p.journey_id
            WHERE p.tenant_id = :t AND p.deleted_at_utc IS NULL AND COALESCE(p.view_code, '') <> 'EXTRA'
              AND j.outlet_id = ANY(CAST(:o AS uuid[]))
              AND ((p.uploaded_at_utc >= :s AND p.uploaded_at_utc < :e)
                   OR p.journey_id = ANY(CAST(:known AS uuid[])))
            GROUP BY 1, 2, 3
        """),
        {"t": tenant_id, "tz": _TZ, "o": outlets, "s": _ist_start(first), "e": _ist_start(last + timedelta(days=1)),
         "known": known},
    ).mappings().all():
        photographed.setdefault((r["outlet_id"], r["day"]), set()).add(r["journey_id"])
        if r["journey_id"] not in last_photo or r["last_at"] > last_photo[r["journey_id"]]:
            last_photo[r["journey_id"]] = r["last_at"]
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        key = (item["outlet_id"], item["for_date"])
        deliveries = delivered.get(key, set()) | photographed.get(key, set())
        with_photo = {j for j in deliveries if j in last_photo}
        total, done = len(deliveries), len(with_photo)
        if total == 0:
            out[str(item["item_id"])] = _answer(
                "NOTHING", f"No delivery in Verigence for {_label(item['for_date'])} yet", done=0, total=0)
        elif done == total:
            out[str(item["item_id"])] = _answer(
                "DONE", f"Photos on {done} of {total} deliveries", done=done, total=total,
                at=max(last_photo[j] for j in with_photo))
        else:
            out[str(item["item_id"])] = _answer(
                "PENDING", f"Photos on {done} of {total} deliveries", done=done, total=total)
    return out


# --------------------------------------------------------------------------- uploads

def check_uploads(connection: Connection, tenant_id: str, items: list[dict[str, Any]],
                  today: date) -> dict[str, dict[str, Any]]:
    """Every booking and delivery file received on the day is uploaded: the Journeys
    uploaded to at the outlet that day (My Tracker's rule, uc03_p2_my_work._uploads: a
    Journey with a delivery page among the day's uploads is a delivery, else a booking;
    direct, bulk and Drive uploads alike) against the counts the PC entered in the
    "files received" duty of the same outlet and day."""
    outlets, first, last = _span(items)
    received: dict[tuple[str, date], dict[str, Any]] = {}
    for r in connection.execute(
        text("""
            SELECT i.outlet_id::text AS outlet_id, i.for_date, i.status, i.input_values
            FROM auditcore.p2_duty_items i
            JOIN auditcore.p2_duties d ON d.tenant_id = i.tenant_id AND d.duty_id = i.duty_id
            WHERE i.tenant_id = :t AND d.standard_code = 'INTIMATION' AND i.status <> 'CANCELLED'
              AND i.outlet_id = ANY(CAST(:o AS uuid[])) AND i.for_date BETWEEN :a AND :b
        """),
        {"t": tenant_id, "o": outlets, "a": first, "b": last},
    ).mappings().all():
        received[(r["outlet_id"], r["for_date"])] = dict(r)
    uploaded: dict[tuple[str, date], dict[str, Any]] = {}
    for r in connection.execute(
        text("""
            SELECT j.outlet_id::text AS outlet_id, CAST(ub.created_at_utc AT TIME ZONE :tz AS date) AS day,
                   ub.journey_id, COALESCE(bool_or(q.business_stage = 'DELIVERY'), false) AS delivery,
                   max(ub.created_at_utc) AS last_at
            FROM auditcore.p2_upload_batches ub
            JOIN auditcore.journeys j ON j.tenant_id = ub.tenant_id AND j.journey_id = ub.journey_id
            LEFT JOIN auditcore.p2_document_queue q ON q.tenant_id = ub.tenant_id AND q.batch_id = ub.batch_id
            WHERE ub.tenant_id = :t AND j.outlet_id = ANY(CAST(:o AS uuid[]))
              AND ub.created_at_utc >= :s AND ub.created_at_utc < :e
              AND ub.batch_status <> 'AWAITING_UPLOAD'
              AND NOT (ub.batch_status = 'CANCELLED' AND ub.page_count = 0)
            GROUP BY 1, 2, 3
        """),
        {"t": tenant_id, "tz": _TZ, "o": outlets, "s": _ist_start(first), "e": _ist_start(last + timedelta(days=1))},
    ).mappings().all():
        slot = uploaded.setdefault((r["outlet_id"], r["day"]), {"bookings": 0, "deliveries": 0, "last_at": None})
        slot["deliveries" if r["delivery"] else "bookings"] += 1
        if slot["last_at"] is None or r["last_at"] > slot["last_at"]:
            slot["last_at"] = r["last_at"]
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        key = (item["outlet_id"], item["for_date"])
        got = uploaded.get(key, {"bookings": 0, "deliveries": 0, "last_at": None})
        asked = received.get(key)
        counts = dict(asked["input_values"] or {}) if asked else {}
        if asked is None or (asked["status"] == "DONE"
                             and not {"bookingFiles", "deliveryFiles"} <= counts.keys()):
            out[str(item["item_id"])] = _answer(
                "NOTHING", f"Uploaded: {got['bookings']} bookings, {got['deliveries']} deliveries")
            continue
        if asked["status"] != "DONE":
            out[str(item["item_id"])] = _answer("PENDING", "Enter the files received first")
            continue
        want_b, want_d = int(counts["bookingFiles"]), int(counts["deliveryFiles"])
        done = min(got["bookings"], want_b) + min(got["deliveries"], want_d)
        note = (f"Bookings {got['bookings']} of {want_b}, deliveries {got['deliveries']} of {want_d} uploaded")
        if got["bookings"] >= want_b and got["deliveries"] >= want_d:
            out[str(item["item_id"])] = _answer("DONE", note, done=done, total=want_b + want_d, at=got["last_at"])
        else:
            out[str(item["item_id"])] = _answer("PENDING", note, done=done, total=want_b + want_d)
    return out


# A new system-checked duty is one more row here.
# --------------------------------------------------------------------------- open tasks

_TASK_OPEN = "t.task_status NOT IN ('VERIFIED_COMPLETE', 'CANCELLED', 'FAILED', 'DEAD_LETTER')"


def check_open_tasks(connection: Connection, tenant_id: str, items: list[dict[str, Any]],
                     today: date) -> dict[str, dict[str, Any]]:
    """The PCs of the outlet worked on the tasks raised on its files (product owner,
    19 Nov 2026: "one duty of working on the Tasks which has been accumulated or raised
    as we upload the files").

    The tasks are the PC tasks on the outlet's Journeys (the Tasks screen). A day counts
    as worked when a PC acted on at least one of them that day; the note carries the
    numbers -- how many were worked on, how many of those closed, how many are still
    open -- so the Team Lead reads the volume, not only the tick. With nothing open and
    nothing worked on there is nothing to judge by: the PC confirms."""
    outlets, first, last = _span(items)
    worked: dict[tuple[str, date], dict[str, Any]] = {}
    for r in connection.execute(
        text("""
            SELECT j.outlet_id::text AS outlet_id, ((te.created_at_utc AT TIME ZONE :tz)::date) AS day,
                   count(DISTINCT te.task_id) AS worked,
                   count(DISTINCT te.task_id) FILTER (WHERE te.event_type = ANY(:closing)) AS closed,
                   max(te.created_at_utc) AS last_at,
                   (array_agg(te.actor_id ORDER BY te.created_at_utc DESC))[1] AS last_actor
            FROM auditcore.p2_task_events te
            JOIN auditcore.p2_tasks t ON t.tenant_id = te.tenant_id AND t.task_id = te.task_id
            JOIN auditcore.journeys j ON j.tenant_id = t.tenant_id AND j.journey_id = t.journey_id
            WHERE te.tenant_id = :t AND te.created_at_utc >= :s AND te.created_at_utc < :e
              AND te.actor_id IS NOT NULL AND upper(te.actor_id) <> 'SYSTEM'
              AND upper(COALESCE(te.actor_role_code, '')) = 'PC' AND te.event_type = ANY(:work)
              AND t.scope_kind = 'JOURNEY' AND upper(t.assigned_role_code) = 'PC'
              AND j.outlet_id = ANY(CAST(:o AS uuid[]))
            GROUP BY 1, 2
        """),
        {"t": tenant_id, "tz": _TZ, "o": outlets, "s": _ist_start(first), "e": _ist_start(last + timedelta(days=1)),
         "work": list(TASK_WORK_EVENTS), "closing": list(TASK_CLOSING_EVENTS)},
    ).mappings():
        worked[(r["outlet_id"], r["day"])] = dict(r)
    still_open = {
        r["outlet_id"]: int(r["n"]) for r in connection.execute(
            text(f"""
                SELECT j.outlet_id::text AS outlet_id, count(*) AS n
                FROM auditcore.p2_tasks t
                JOIN auditcore.journeys j ON j.tenant_id = t.tenant_id AND j.journey_id = t.journey_id
                WHERE t.tenant_id = :t AND t.scope_kind = 'JOURNEY' AND upper(t.assigned_role_code) = 'PC'
                  AND {_TASK_OPEN} AND j.outlet_id = ANY(CAST(:o AS uuid[]))
                GROUP BY 1
            """),
            {"t": tenant_id, "o": outlets},
        ).mappings()
    }
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        row = worked.get((item["outlet_id"], item["for_date"]))
        open_now = still_open.get(item["outlet_id"], 0)
        # "Still open" is what is open now: it is said of today only, never of a past day.
        tail = f", {open_now} still open" if item["for_date"] >= today else ""
        if row is not None:
            n, closed = int(row["worked"]), int(row["closed"])
            out[str(item["item_id"])] = _answer(
                "DONE", f"Worked on {n} task{'' if n == 1 else 's'}, {closed} closed{tail}",
                done=n, total=None, at=row["last_at"], actor=row["last_actor"])
        elif open_now == 0 and item["for_date"] >= today:
            out[str(item["item_id"])] = _answer("NOTHING", "No open task on this outlet's files", done=0, total=0)
        elif item["for_date"] >= today:
            out[str(item["item_id"])] = _answer(
                "PENDING", f"{open_now} open task{'' if open_now == 1 else 's'}, none worked on today",
                done=0, total=None)
        else:
            out[str(item["item_id"])] = _answer(
                "PENDING", f"No task was worked on {_label(item['for_date'])}", done=0, total=None)
    return out


CHECKERS: dict[str, dict[str, Any]] = {
    "GATE_REGISTER": {
        "label": "Gate out register uploaded, counts entered", "check": check_gate_register, "final": True,
        "hint": "This ticks itself when the gate out register is uploaded and its counts are entered.",
        "nilLabel": None,
    },
    "UPLOADS": {
        "label": "Files received are uploaded", "check": check_uploads, "final": False,
        "hint": "This ticks itself when the bookings and deliveries uploaded match the files received.",
        "nilLabel": "Everything received is uploaded",
    },
    "VEHICLE_PHOTOS": {
        "label": "Every delivery has vehicle photos", "check": check_vehicle_photos, "final": False,
        "hint": "This ticks itself when every delivery of the day has photos on its Vehicle photos tab.",
        "nilLabel": "No deliveries on this day",
    },
    "BANK_STATEMENT": {
        "label": "Bank statement uploaded", "check": check_bank_statement, "final": True,
        "hint": "This ticks itself when the bank statement is uploaded in Bank statements.",
        "nilLabel": None,
    },
    "OPEN_TASKS": {
        "label": "Tasks on the outlet's files worked on", "check": check_open_tasks, "final": False,
        "hint": "This ticks itself when you work on the open tasks of your outlet's files, on the Tasks screen.",
        "nilLabel": "No open tasks",
    },
}
