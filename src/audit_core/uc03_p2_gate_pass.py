"""Daily Gate Pass (product owner, 2026-10-26).

Each day a PC photographs the outlet's hand-written gate pass and says how
many new deliveries, stock transfers and test drives it lists. The new-delivery
count is checked against the deliveries of the outlet whose own vehicle gate pass,
dated that day, has been uploaded (product owner 2026-10-27):

    equal                       no divergence, the task is finished
    files > declared            the PC names the files that belong to other days
    declared > files            the PC says whether they hold the files; NO
                                raises a high-priority Daily Task for the TL

    1. POST  /gate-passes            multipart photo, quality-checked, stored in R2
    2. POST  /gate-passes/{id}/counts total deliveries on the register and how many were
                                      intimated to the PC (product owner 2026-11-02); older
                                      clients still send the new-delivery count alone
    3. POST  /gate-passes/{id}/answer the PC's answer when the counts differ (older clients)
    4. GET   /gate-passes/calendar    declared / uploaded / missing per day
    5. GET   /gate-passes/{id}/image-url  the photo, one outlet at a time
    6. GET   /daily-tasks, POST /daily-tasks/{id}/resolve   the TL's Daily Tasks
    7. GET   /gate-passes/days?outletId  the dates the PC can choose, and what each allows
    8. POST  /gate-passes/{id}/pages, DELETE /gate-passes/{id}/pages/{pageNo}
                                      add a photo to a register, or remove one

The date of a register (product owner 2026-11-07): the PC chooses it, there is
no default. A new register can be uploaded for any day from today back to 7
days ago (N to N-7, India time); a register already on file can be edited --
photos added, replaced or removed -- while its date is within N to N-3, and is
locked after that. Sunday is the off day and goes with Monday, so Sunday itself
is never chosen. A register finished after its day's Daily Gate Pass task was
marked incomplete closes that task as done late.

The Team Lead's "Delivery not notified to the PC" task is suspended for the time
being (product owner 2026-11-07): the counts are still compared and kept, no TL
task is raised unless AUDIT_CORE_GATE_PASS_TL_TASKS=on (task_manager).

Every outlet a PC is mapped to has a gate register, satellite outlets included
(product owner 2026-11-02; until then onsite outlets only).

With the two counts the day settles at once (2026-11-02):

    uploaded            delivery Journeys of the day (the rule below)
    pending with PC     intimated - uploaded
    not intimated       total - intimated (kept as not_notified_count, so every
                        figure built on it -- calendar, consolidated view -- holds)

A delivery counts only when its Journey carries the vehicle's gate pass document
and the delivery date on it matches the day (see vehicle_gate_pass_deliveries):
the Daily Gate Pass task, the calendar and the consolidated view all count the
same way (product owner 2026-10-27).
"""
from __future__ import annotations

import calendar as _calendar
import json
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.gate_pass_image_quality import MAX_BYTES, assess_gate_pass_image
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.task_manager import _insert_task as insert_task_manager_task
from audit_core.task_manager import (
    close_gate_pass_tasks,
    gate_pass_covers,
    gate_pass_day_label,
    gate_pass_due,
    gate_pass_tl_tasks_enabled,
    previous_gate_pass_day,
    suspend_gate_pass_tl_tasks,
)
from audit_core.uc03_p2_access import authorize_p2
from audit_core.uc03_p2_object_keys import ist_day, safe_filename, safe_part
from audit_core.uc03_p2_storage import P2DocumentStorageError, get_p2_document_storage

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-gate-pass"])

_READ = "audit.journey.read"
_UPDATE = "audit.journey.update"
_SUPERVISOR_ROLES = ("TL", "PM", "EXECUTIVE")
DAILY_TASK_CATEGORY = "DAILY_TASK"
TASK_DELIVERY_NOT_NOTIFIED = "GATE_PASS_DELIVERY_NOT_NOTIFIED"

_ACTIVE_ASSIGNMENT = """
    ba.tenant_id = :tenant_id AND ba.security_actor_id = :actor_id
    AND ba.assignment_status = 'ACTIVE' AND ba.effective_from <= now()
    AND (ba.effective_to IS NULL OR ba.effective_to >= now())
"""


def _authorize(connection: Connection, tenant_id: str, principal: HumanPrincipal,
               client: SecurityAuthorizationClient, permission: str) -> None:
    authorize_p2(connection, tenant_id=tenant_id, journey_id=None, human_principal=principal,
                 authorization_client=client, permission_key=permission)


def scope_outlets(connection: Connection, tenant_id: str, actor_id: str) -> list[dict[str, Any]]:
    """Every outlet the caller is assigned to, with the roles they hold there."""
    rows = connection.execute(
        text(
            f"""
            SELECT o.dealer_id, d.dealer_name, o.outlet_id, o.outlet_name, o.outlet_classification,
                   array_agg(DISTINCT ba.business_role_code) AS roles
            FROM auditcore.business_assignments ba
            JOIN auditcore.dealer_outlets o
              ON o.tenant_id = ba.tenant_id
             AND (ba.dealer_id IS NULL
                  OR (o.dealer_id = ba.dealer_id AND (ba.outlet_id IS NULL OR o.outlet_id = ba.outlet_id)))
            JOIN auditcore.dealers d ON d.tenant_id = o.tenant_id AND d.dealer_id = o.dealer_id
            WHERE {_ACTIVE_ASSIGNMENT}
            GROUP BY o.dealer_id, d.dealer_name, o.outlet_id, o.outlet_name, o.outlet_classification
            ORDER BY lower(d.dealer_name), (o.outlet_classification = 'ONSITE') DESC, lower(o.outlet_name)
            """
        ),
        {"tenant_id": tenant_id, "actor_id": actor_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _outlet_in_scope(outlets: list[dict[str, Any]], outlet_id: UUID | str) -> dict[str, Any]:
    for outlet in outlets:
        if str(outlet["outlet_id"]) == str(outlet_id):
            return outlet
    # Do not disclose outlets outside the caller's scope.
    raise HTTPException(status_code=404, detail="Outlet not found in your current Project scope.")


def _public_outlet(outlet: dict[str, Any], *, default_id: str | None = None) -> dict[str, Any]:
    return {
        "dealerId": str(outlet["dealer_id"]), "dealerName": outlet["dealer_name"],
        "outletId": str(outlet["outlet_id"]), "outletName": outlet["outlet_name"],
        "classification": outlet["outlet_classification"],
        "isDefault": default_id is not None and str(outlet["outlet_id"]) == default_id,
    }


# How far back a PC may go (product owner 2026-11-07): a new register for any day
# from today back to N-7; an existing register edited only from today back to N-3.
UPLOAD_BACK_DAYS = 7
EDIT_BACK_DAYS = 3


def gate_pass_window(now: datetime | None = None) -> dict[str, Any]:
    """The dates a PC can choose for a gate register now: today back to 7 days ago,
    India time, newest first, Sunday left out (Sunday goes with Monday: Monday's
    register covers both). ``editFrom``: the oldest date whose register can still
    be edited (N-3); older registers are locked."""
    today = ist_day(now).date()
    back = [today - timedelta(days=i) for i in range(UPLOAD_BACK_DAYS + 1)]
    return {"today": today, "uploadFrom": back[-1], "editFrom": today - timedelta(days=EDIT_BACK_DAYS),
            "days": [d for d in back if d.isoweekday() != 7]}


def _range_words(window: dict[str, Any]) -> str:
    return (f"{gate_pass_day_label(window['uploadFrom'])} to {gate_pass_day_label(window['today'])} "
            f"(today back to {UPLOAD_BACK_DAYS} days ago)")


def check_register_date(day: date | None, window: dict[str, Any]) -> date:
    """The date the PC chose, refused when it is not one they may choose (2026-11-07)."""
    if day is None:
        raise HTTPException(status_code=422, detail="Choose the date this gate register is for.")
    if day in window["days"]:
        return day
    if day.isoweekday() == 7 and window["uploadFrom"] <= day <= window["today"] + timedelta(days=1):
        raise HTTPException(status_code=422, detail="Sunday is the off day: upload Sunday's gate pass with "
                                                    "Monday's, one register for both days.")
    if day > window["today"]:
        raise HTTPException(status_code=422, detail="A gate register cannot be uploaded for a day that has not "
                                                    "come yet.")
    raise HTTPException(status_code=422, detail="A gate register can be uploaded only for "
                                                + _range_words(window) + ".")


def register_editable(day: date, window: dict[str, Any]) -> bool:
    """An existing register can be edited while its date is within today back to N-3 (2026-11-07)."""
    return window["editFrom"] <= day <= window["today"]


def check_register_editable(day: date, window: dict[str, Any]) -> None:
    if not register_editable(day, window):
        raise HTTPException(status_code=409, detail=(
            f"The gate register for {gate_pass_day_label(day)} is locked: a register already uploaded can be "
            f"changed only up to {EDIT_BACK_DAYS} days after its date."))


def gate_pass_days(now: datetime | None = None) -> dict[str, Any]:
    """Today's gate pass day (none on a Sunday, the off day: Sunday goes with
    Monday) and the previous gate pass day's while its task is open (until 23:59
    of the next gate pass day). Kept for the ``today`` / ``previous`` fields of
    GET /gate-passes/outlets that older clients read; which dates can be chosen
    is gate_pass_window (2026-11-07)."""
    today = ist_day(now).date()
    current = None if today.isoweekday() == 7 else today
    previous = previous_gate_pass_day(today) if current else today - timedelta(days=1)
    if (now or datetime.now(UTC)) > gate_pass_due(previous):
        previous = None
    return {"today": today, "current": current, "previous": previous}


def _day_info(day: date | None) -> dict[str, Any] | None:
    if day is None:
        return None
    return {"date": day.isoformat(), "label": gate_pass_day_label(day),
            "coversDates": [d.isoformat() for d in gate_pass_covers(day)],
            "dueAtUtc": gate_pass_due(day).isoformat()}


def vehicle_gate_pass_deliveries(connection: Connection, tenant_id: str, outlet_ids: list[str],
                                 first: date, last: date) -> dict[tuple[date, str], int]:
    """Delivery Journeys per (Daily Gate Pass day, outlet) that carry the vehicle's own
    gate pass dated on a day that Daily Gate Pass covers (product owner 2026-10-27).

    A Journey counts only when a vehicle gate pass document is on file (live) and the
    delivery date on its newest reading (a reviewer's correction wins) is the day, year
    included; Monday's Daily Gate Pass covers Sunday too. Uploading other delivery
    documents does not count a Journey, and a gate pass whose date was misread (often
    the year, on a handwritten gate pass) counts once the date is corrected."""
    if not outlet_ids:
        return {}
    out: dict[tuple[date, str], int] = {}
    for r in connection.execute(
        text(
            """
            WITH dated AS (
                SELECT DISTINCT ON (f.journey_id, f.di_document_id) f.journey_id, f.effective_value #>> '{}' AS v
                FROM auditcore.journey_document_extracted_fields f
                JOIN auditcore.evidence e
                  ON e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id
                 AND e.di_document_id = f.di_document_id AND e.association_status = 'ACTIVE'
                WHERE f.tenant_id = :t AND f.field_key = 'delivery_date'
                  AND COALESCE(e.document_type_key, f.source_document_type_key) = 'gate_pass'
                ORDER BY f.journey_id, f.di_document_id, f.source_fact_version DESC NULLS LAST,
                         f.updated_at_utc DESC
            )
            SELECT CAST(d.v AS date) AS day, j.outlet_id, COUNT(DISTINCT d.journey_id) AS n
            FROM dated d
            JOIN auditcore.journeys j ON j.tenant_id = :t AND j.journey_id = d.journey_id
            WHERE d.v ~ '^\\d{4}-\\d{2}-\\d{2}$' AND j.outlet_id = ANY(CAST(:ids AS uuid[]))
              AND CAST(d.v AS date) BETWEEN :a AND LEAST(CAST(:b AS date), CAST(:today AS date))
            GROUP BY 1, 2
            """
        ),
        # A delivery dated after today has not happened: the gate pass date is misread
        # (DATE_IN_FUTURE asks the PC to correct it) and counts on no day until it is.
        {"t": tenant_id, "ids": [str(i) for i in outlet_ids], "a": gate_pass_covers(first)[0], "b": last,
         "today": ist_day().date()},
    ).mappings().all():
        # Sunday's deliveries belong to Monday's Daily Gate Pass, which covers both days.
        day = r["day"] + timedelta(days=1) if r["day"].isoweekday() == 7 else r["day"]
        if first <= day <= last:
            key = (day, str(r["outlet_id"]))
            out[key] = out.get(key, 0) + int(r["n"])
    return out


def delivery_file_count(connection: Connection, tenant_id: str, outlet_id: UUID | str, day: date) -> int:
    """Delivery Journeys of the outlet with the vehicle's gate pass for the day (see
    vehicle_gate_pass_deliveries)."""
    return vehicle_gate_pass_deliveries(connection, tenant_id, [str(outlet_id)], day, day).get((day, str(outlet_id)), 0)


# The live photos of a register, in the order they are shown and opened (?page=n is the
# n-th of these): by day (Sunday's before Monday's), then as they were added. A removed
# photo is kept, with who removed it, and left out (2026-11-07).
_LIVE_PAGES_SQL = """
    SELECT page_no, page_date, object_key, content_type, size_bytes, width_px, height_px, sharpness, brightness,
           quality_flag
    FROM auditcore.p2_gate_pass_pages
    WHERE tenant_id = :t AND gate_pass_id = :i AND removed_at_utc IS NULL
    ORDER BY page_date, page_no
"""


def _gate_pass_row(connection: Connection, tenant_id: str, gate_pass_id: UUID | str, *,
                   lock: bool = False) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT * FROM auditcore.p2_gate_passes WHERE tenant_id = :t AND gate_pass_id = :i"
             + (" FOR UPDATE" if lock else "")),
        {"t": tenant_id, "i": str(gate_pass_id)},
    ).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Gate pass not found.")
    out = dict(row)
    out["pages"] = [dict(p) for p in connection.execute(
        text(_LIVE_PAGES_SQL), {"t": tenant_id, "i": str(gate_pass_id)},
    ).mappings().all()]
    return out


_QUALITY_NOTES = {
    "BLURRED": "The photo looks blurred.",
    "TOO_DARK": "The photo looks dark.",
    "WASHED_OUT": "The photo looks washed out.",
}


def _public_gate_pass(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "gatePassId": str(row["gate_pass_id"]), "outletId": str(row["outlet_id"]), "outletName": row["outlet_name"],
        "pcName": row["pc_name"], "businessDate": row["business_date"].isoformat(), "status": row["status"],
        "dayLabel": gate_pass_day_label(row["business_date"]),
        "coversDates": [d.isoformat() for d in gate_pass_covers(row["business_date"])],
        "newDeliveries": row["new_deliveries"], "stockTransfers": row["stock_transfers"],
        "testDrives": row["test_drives"], "otherCount": row["other_count"], "otherNote": row["other_note"],
        "deliveryFiles": row["delivery_files"], "divergence": row["divergence"],
        "pastDeliveryNote": row["past_delivery_note"], "filesHeld": row["files_held"],
        "notNotifiedToPc": row.get("not_notified_count"),
        "intimatedToPc": (max(int(row["new_deliveries"] or 0) - int(row.get("not_notified_count") or 0), 0)
                          if row.get("not_notified_count") is not None else None),
        "notUploadedByPc": (max(int(row["new_deliveries"] or 0) - int(row["delivery_files"] or 0)
                                - int(row.get("not_notified_count") or 0), 0)
                            if row.get("not_notified_count") is not None else None),
        "countsSubmitted": row["counts_submitted_at_utc"] is not None,
        "completed": row["completed_at_utc"] is not None,
        # The photo is always kept; its quality is a flag (2026-10-27).
        "photoQuality": row.get("quality_flag") or "GOOD",
        "photoMode": row.get("photo_mode") or "COMBINED",
        "dayCounts": row.get("day_counts"),
        # Correctable by its PC until the end of the day it was uploaded (2026-11-03).
        "countsEditable": row["counts_submitted_at_utc"] is not None and counts_editable(row),
        "countsEditCount": int(row.get("counts_edit_count") or 0),
        "countsEditedAtUtc": row["counts_edited_at_utc"].isoformat() if row.get("counts_edited_at_utc") else None,
        "countsHistory": row.get("counts_history") or [],
        # pageNo is the photo's id within the register (to remove it); position is what
        # GET .../image-url?page= opens (2026-11-07: photos can be added and removed).
        "pages": [{"pageNo": p["page_no"], "position": n, "date": p["page_date"].isoformat(),
                   "photoQuality": p["quality_flag"] or "GOOD"} for n, p in enumerate(row.get("pages", []), start=1)],
        "photoQualityNote": _QUALITY_NOTES.get(row.get("quality_flag") or "GOOD"),
        # Its photos can be added, replaced or removed until N+3 (2026-11-07).
        "photosEditable": row["status"] == "ACTIVE" and register_editable(row["business_date"], gate_pass_window()),
        "editableUntil": (row["business_date"] + timedelta(days=EDIT_BACK_DAYS)).isoformat(),
        "pagesEditCount": int(row.get("pages_edit_count") or 0),
        # Uploaded after its Daily Gate Pass task was due (a register of an earlier day).
        "uploadedLate": bool(row.get("created_at_utc")) and row["created_at_utc"] > gate_pass_due(row["business_date"]),
    }


def counts_editable(row: dict[str, Any], now: datetime | None = None) -> bool:
    """The PC may correct a gate register's counts until the end of the IST day it was
    uploaded (product owner 2026-11-03: PCs make mistakes reporting the numbers)."""
    created = row.get("created_at_utc")
    return bool(created) and ist_day(created).date() == ist_day(now).date()


def _own_gate_pass(connection: Connection, tenant_id: str, gate_pass_id: UUID, actor_id: str) -> dict[str, Any]:
    row = _gate_pass_row(connection, tenant_id, gate_pass_id)
    if row["pc_actor_id"] != actor_id:
        raise HTTPException(status_code=404, detail="Gate pass not found.")
    if row["status"] != "ACTIVE":
        raise HTTPException(status_code=409, detail="A newer gate pass was uploaded for this day. Use that one.")
    return row


def editable_today(connection: Connection, tenant_id: str, actor_id: str,
                   now: datetime | None = None) -> list[dict[str, Any]]:
    """The caller's gate registers whose counts can still be corrected -- submitted, uploaded
    today (IST) -- so the Daily gate pass screen offers the correction right where the PC
    uploads (product owner 2026-11-03)."""
    start = ist_day(now).replace(hour=0, minute=0, second=0, microsecond=0)
    ids = connection.execute(
        text("SELECT gate_pass_id FROM auditcore.p2_gate_passes WHERE tenant_id = :t AND pc_actor_id = :a "
             "AND status = 'ACTIVE' AND counts_submitted_at_utc IS NOT NULL AND created_at_utc >= :s "
             "ORDER BY created_at_utc DESC"),
        {"t": tenant_id, "a": actor_id, "s": start},
    ).scalars().all()
    out = []
    for gate_pass_id in ids:
        row = _gate_pass_row(connection, tenant_id, gate_pass_id)
        if counts_editable(row, now):
            out.append(_public_gate_pass(row))
    return out


@router.get("/gate-passes/outlets")
def gate_pass_outlets(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The outlets the caller may upload for -- every mapped outlet, satellites included (2026-11-02) --
    the first ONSITE outlet marked as the default, and the dates a register can be chosen for
    (``window``, 2026-11-07; per outlet, GET /gate-passes/days says what each date allows).
    ``businessDate``, ``today``, ``previous`` and ``previousOpenOutletIds`` are kept for older
    clients; the PC now always chooses the date."""
    _authorize(connection, tenant_id, principal, client, _UPDATE)
    outlets = scope_outlets(connection, tenant_id, principal.subject)
    onsite = [o for o in outlets if o["outlet_classification"] == "ONSITE"]
    default = (onsite or outlets or [None])[0]
    default_id = str(default["outlet_id"]) if default else None
    days = gate_pass_days()
    previous_open: list[str] = []
    if days["previous"] is not None and outlets:
        done = {str(r) for r in connection.execute(
            text("SELECT outlet_id FROM auditcore.p2_gate_passes WHERE tenant_id = :t AND business_date = :d "
                 "AND status = 'ACTIVE' AND completed_at_utc IS NOT NULL AND outlet_id = ANY(CAST(:ids AS uuid[]))"),
            {"t": tenant_id, "d": days["previous"], "ids": [str(o["outlet_id"]) for o in outlets]},
        ).scalars().all()}
        previous_open = [str(o["outlet_id"]) for o in outlets if str(o["outlet_id"]) not in done]
    return {
        "businessDate": (days["current"] or days["previous"] or days["today"]).isoformat(),
        "today": _day_info(days["current"]),
        "previous": _day_info(days["previous"]),
        "previousOpenOutletIds": previous_open,
        "window": _public_window(gate_pass_window()),
        "outlets": [_public_outlet(o, default_id=default_id) for o in outlets],
        "defaultOutletId": default_id,
        "editableToday": editable_today(connection, tenant_id, principal.subject),
        # Kept for older clients: a satellite outlet has a gate register too now.
        "satelliteOnly": False,
    }


def _public_window(window: dict[str, Any]) -> dict[str, Any]:
    return {"today": window["today"].isoformat(), "uploadFrom": window["uploadFrom"].isoformat(),
            "editFrom": window["editFrom"].isoformat(), "uploadBackDays": UPLOAD_BACK_DAYS,
            "editBackDays": EDIT_BACK_DAYS, "maxPages": MAX_PAGES,
            "days": [_day_info(d) for d in window["days"]]}


@router.get("/gate-passes/days")
def gate_pass_choosable_days(
    tenant_id: str,
    outletId: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The dates the PC can choose for the outlet's gate register (product owner 2026-11-07),
    newest first, and what each allows:

        NEW        no register yet: a new one can be uploaded (N to N-7)
        EDITABLE   a register is on file and can be edited: add, replace or remove photos (N to N-3)
        LOCKED     a register is on file and can no longer be changed (N-4 to N-7)
    """
    _authorize(connection, tenant_id, principal, client, _UPDATE)
    outlet = _outlet_in_scope(scope_outlets(connection, tenant_id, principal.subject), outletId)
    window = gate_pass_window()
    ids = connection.execute(
        text("SELECT business_date, gate_pass_id FROM auditcore.p2_gate_passes WHERE tenant_id = :t "
             "AND outlet_id = :o AND status = 'ACTIVE' AND business_date BETWEEN :a AND :b"),
        {"t": tenant_id, "o": str(outlet["outlet_id"]), "a": window["uploadFrom"], "b": window["today"]},
    ).all()
    registers = {r[0]: _public_gate_pass(_gate_pass_row(connection, tenant_id, r[1])) for r in ids}
    days = []
    for d in window["days"]:
        register = registers.get(d)
        state = "NEW" if register is None else ("EDITABLE" if register_editable(d, window) else "LOCKED")
        days.append({
            **(_day_info(d) or {}), "isToday": d == window["today"], "state": state,
            "canUpload": state != "LOCKED", "canEdit": state == "EDITABLE", "register": register,
            "note": {"NEW": "No gate register uploaded yet.",
                     "EDITABLE": (f"Uploaded. You can add, replace or remove photos until "
                                  f"{gate_pass_day_label(d + timedelta(days=EDIT_BACK_DAYS))}."),
                     "LOCKED": (f"Uploaded and locked: a register can be changed only up to {EDIT_BACK_DAYS} "
                                "days after its date.")}[state],
        })
    return {"outlet": _public_outlet(outlet), **_public_window(window), "days": days}


@router.post("/gate-passes", status_code=201)
def upload_gate_pass(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    outletId: Annotated[UUID, Form()],
    file: Annotated[list[UploadFile] | None, File()] = None,
    files: Annotated[list[UploadFile] | None, File()] = None,
    pcName: Annotated[str | None, Form()] = None,
    businessDate: Annotated[date | None, Form()] = None,
    fileDates: Annotated[list[date] | None, Form()] = None,
) -> dict[str, Any]:
    """Up to four photos of the gate-out register of ``businessDate`` (it can run over pages).
    For Monday, which covers Sunday too, ``fileDates`` (one per photo, Sunday or
    Monday) says the PC photographed the two days apart (product owner 2026-10-27).

    ``businessDate`` is required: the PC chooses it, today back to N-7 (2026-11-07). A date
    whose register is on file is a replacement (the old register is kept, SUPERSEDED), allowed
    only while that date is within N to N-3."""
    _authorize(connection, tenant_id, principal, client, _UPDATE)
    outlet = _outlet_in_scope(scope_outlets(connection, tenant_id, principal.subject), outletId)
    window = gate_pass_window()
    business_date = check_register_date(businessDate, window)
    existing = connection.execute(
        text("SELECT 1 FROM auditcore.p2_gate_passes WHERE tenant_id = :t AND outlet_id = :o "
             "AND business_date = :d AND status = 'ACTIVE'"),
        {"t": tenant_id, "o": str(outlet["outlet_id"]), "d": business_date},
    ).first()
    if existing is not None:
        check_register_editable(business_date, window)
    uploads = [f for f in (files or []) + (file or []) if f is not None]
    if not uploads:
        raise HTTPException(status_code=422, detail="Add a photo of the gate pass.")
    if len(uploads) > MAX_PAGES:
        raise HTTPException(status_code=422, detail=f"Upload at most {MAX_PAGES} photos for one gate pass.")
    covers = gate_pass_covers(business_date)
    separate = bool(fileDates) and len(covers) > 1
    if fileDates and len(fileDates) != len(uploads):
        raise HTTPException(status_code=422, detail="Say which day each photo is for.")
    page_dates = list(fileDates) if separate else [business_date] * len(uploads)
    if any(d not in covers for d in page_dates):
        raise HTTPException(status_code=422, detail="A photo is for a day this gate pass does not cover.")
    if separate and set(page_dates) != set(covers):
        raise HTTPException(status_code=422, detail="Add at least one photo for Sunday and one for Monday.")
    pages = []
    for n, upload in enumerate(uploads, start=1):
        data = upload.file.read(MAX_BYTES + 1)
        assessment = assess_gate_pass_image(data)
        if not assessment.ok:
            raise HTTPException(status_code=422, detail=f"Photo {n}: {assessment.message}"
                                if len(uploads) > 1 else assessment.message)
        pages.append((data, assessment))

    day = datetime.combine(business_date, datetime.min.time())
    pc_name = (pcName or "").strip() or principal.subject
    gate_pass_id = uuid4()
    label = f"{safe_part(outlet['outlet_name'], fallback='outlet')}__{safe_part(pc_name, fallback='pc')}__{day:%d-%m-%y}"
    keys = []
    try:
        for n, (data, assessment) in enumerate(pages, start=1):
            suffix = "" if len(pages) == 1 else f"__p{n}"
            key = (f"daily-gate-passes/{tenant_id}/{day:%Y-%m-%d}/{gate_pass_id}/"
                   f"{safe_filename(label + suffix)}.{assessment.extension}")
            get_p2_document_storage().put_object(key, data, content_type=str(assessment.content_type))
            keys.append(key)
    except (P2DocumentStorageError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="The photo could not be stored just now. Please try again.") from exc
    first_data, assessment = pages[0]
    key = keys[0]
    # The gate pass is as good as its worst page.
    worst = next((a.quality for _, a in pages if a.quality != "GOOD"), "GOOD")

    connection.execute(
        text("UPDATE auditcore.p2_gate_passes SET status = 'SUPERSEDED' "
             "WHERE tenant_id = :t AND outlet_id = :o AND business_date = :d AND status = 'ACTIVE'"),
        {"t": tenant_id, "o": str(outlet["outlet_id"]), "d": day.date()},
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_gate_passes (
                tenant_id, gate_pass_id, dealer_id, outlet_id, outlet_name, pc_actor_id, pc_name, business_date,
                object_key, content_type, size_bytes, width_px, height_px, sharpness, brightness, quality_flag,
                photo_mode)
            VALUES (:t, :id, :dealer, :outlet, :outlet_name, :actor, :pc_name, :day,
                    :key, :ct, :size, :w, :h, :sharp, :bright, :quality, :mode)
            """
        ),
        {"t": tenant_id, "id": str(gate_pass_id), "dealer": str(outlet["dealer_id"]),
         "outlet": str(outlet["outlet_id"]), "outlet_name": outlet["outlet_name"], "actor": principal.subject,
         "pc_name": pc_name, "day": day.date(), "key": key, "ct": assessment.content_type,
         "size": len(first_data), "w": assessment.width, "h": assessment.height, "sharp": assessment.sharpness,
         "bright": assessment.brightness, "quality": worst, "mode": "SEPARATE" if separate else "COMBINED"},
    )
    for n, ((data, a), page_key, page_date) in enumerate(zip(pages, keys, page_dates, strict=True), start=1):
        connection.execute(
            text("""
                INSERT INTO auditcore.p2_gate_pass_pages (tenant_id, gate_pass_id, page_no, page_date, object_key,
                  content_type, size_bytes, width_px, height_px, sharpness, brightness, quality_flag)
                VALUES (:t, :id, :n, :d, :key, :ct, :size, :w, :h, :sharp, :bright, :quality)
            """),
            {"t": tenant_id, "id": str(gate_pass_id), "n": n, "d": page_date, "key": page_key,
             "ct": a.content_type, "size": len(data), "w": a.width, "h": a.height, "sharp": a.sharpness,
             "bright": a.brightness, "quality": a.quality},
        )
    return _public_gate_pass(_gate_pass_row(connection, tenant_id, gate_pass_id))


def _editable_register(connection: Connection, tenant_id: str, gate_pass_id: UUID,
                       principal: HumanPrincipal) -> dict[str, Any]:
    """A register on file whose photos the caller may change now (2026-11-07): an outlet of
    theirs, the date's current register, and its date within N to N-3. Locked for the edit."""
    row = _gate_pass_row(connection, tenant_id, gate_pass_id, lock=True)
    _outlet_in_scope(scope_outlets(connection, tenant_id, principal.subject), row["outlet_id"])
    if row["status"] != "ACTIVE":
        raise HTTPException(status_code=409, detail="A newer gate register was uploaded for this day. Use that one.")
    check_register_editable(row["business_date"], gate_pass_window())
    return row


def _refresh_cover(connection: Connection, tenant_id: str, gate_pass_id: UUID | str) -> None:
    """After a photo is added or removed: page 1 on the register row is its first live photo,
    and the register is as good as its worst live photo (as at upload)."""
    pages = connection.execute(text(_LIVE_PAGES_SQL), {"t": tenant_id, "i": str(gate_pass_id)}).mappings().all()
    first = pages[0]
    worst = next((p["quality_flag"] for p in pages if (p["quality_flag"] or "GOOD") != "GOOD"), "GOOD")
    connection.execute(
        text("""
            UPDATE auditcore.p2_gate_passes
            SET object_key = :key, content_type = :ct, size_bytes = :size, width_px = :w, height_px = :h,
                sharpness = :sharp, brightness = :bright, quality_flag = :quality,
                pages_edit_count = pages_edit_count + 1, pages_edited_at_utc = now()
            WHERE tenant_id = :t AND gate_pass_id = :i
        """),
        {"key": first["object_key"], "ct": first["content_type"], "size": first["size_bytes"], "w": first["width_px"],
         "h": first["height_px"], "sharp": first["sharpness"], "bright": first["brightness"], "quality": worst,
         "t": tenant_id, "i": str(gate_pass_id)},
    )


@router.post("/gate-passes/{gate_pass_id}/pages", status_code=201)
def add_gate_pass_pages(
    tenant_id: str,
    gate_pass_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    file: Annotated[list[UploadFile] | None, File()] = None,
    files: Annotated[list[UploadFile] | None, File()] = None,
    pageDate: Annotated[date | None, Form()] = None,
) -> dict[str, Any]:
    """Add photos to a register on file (product owner 2026-11-07), while its date is within
    N to N-3; the register holds at most MAX_PAGES photos. Its counts are kept. A Monday
    register with Sunday and Monday photographed apart needs ``pageDate`` (Sunday or Monday)."""
    _authorize(connection, tenant_id, principal, client, _UPDATE)
    row = _editable_register(connection, tenant_id, gate_pass_id, principal)
    uploads = [f for f in (files or []) + (file or []) if f is not None]
    if not uploads:
        raise HTTPException(status_code=422, detail="Add a photo of the gate pass.")
    have = len(row["pages"])
    if have + len(uploads) > MAX_PAGES:
        raise HTTPException(status_code=422, detail=(
            f"A gate register holds at most {MAX_PAGES} photos and this one has {have}. "
            + (f"Add at most {MAX_PAGES - have}." if have < MAX_PAGES else "Remove a photo first.")))
    business_date = row["business_date"]
    covers = gate_pass_covers(business_date)
    if row.get("photo_mode") == "SEPARATE":
        if pageDate not in covers:
            raise HTTPException(status_code=422, detail="Say which day the photo is for: "
                                + " or ".join(f"{d:%A}" for d in covers) + ".")
        page_date = pageDate
    else:
        if pageDate is not None and pageDate not in covers:
            raise HTTPException(status_code=422, detail="A photo is for a day this gate pass does not cover.")
        page_date = business_date
    pages = []
    for n, upload in enumerate(uploads, start=1):
        data = upload.file.read(MAX_BYTES + 1)
        assessment = assess_gate_pass_image(data)
        if not assessment.ok:
            raise HTTPException(status_code=422, detail=f"Photo {n}: {assessment.message}"
                                if len(uploads) > 1 else assessment.message)
        pages.append((data, assessment))
    next_no = int(connection.execute(
        text("SELECT COALESCE(max(page_no), 0) FROM auditcore.p2_gate_pass_pages WHERE tenant_id = :t "
             "AND gate_pass_id = :i"), {"t": tenant_id, "i": str(gate_pass_id)},
    ).scalar_one()) + 1
    day = datetime.combine(business_date, datetime.min.time())
    label = (f"{safe_part(row['outlet_name'], fallback='outlet')}__{safe_part(row['pc_name'], fallback='pc')}"
             f"__{day:%d-%m-%y}")
    stored = []
    try:
        for offset, (data, assessment) in enumerate(pages):
            page_no = next_no + offset
            key = (f"daily-gate-passes/{tenant_id}/{day:%Y-%m-%d}/{gate_pass_id}/"
                   f"{safe_filename(f'{label}__p{page_no}')}.{assessment.extension}")
            get_p2_document_storage().put_object(key, data, content_type=str(assessment.content_type))
            stored.append((page_no, key, data, assessment))
    except (P2DocumentStorageError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="The photo could not be stored just now. Please try again.") from exc
    for page_no, key, data, a in stored:
        connection.execute(
            text("""
                INSERT INTO auditcore.p2_gate_pass_pages (tenant_id, gate_pass_id, page_no, page_date, object_key,
                  content_type, size_bytes, width_px, height_px, sharpness, brightness, quality_flag, added_by_actor_id)
                VALUES (:t, :id, :n, :d, :key, :ct, :size, :w, :h, :sharp, :bright, :quality, :actor)
            """),
            {"t": tenant_id, "id": str(gate_pass_id), "n": page_no, "d": page_date, "key": key,
             "ct": a.content_type, "size": len(data), "w": a.width, "h": a.height, "sharp": a.sharpness,
             "bright": a.brightness, "quality": a.quality, "actor": principal.subject},
        )
    _refresh_cover(connection, tenant_id, gate_pass_id)
    return _public_gate_pass(_gate_pass_row(connection, tenant_id, gate_pass_id))


@router.delete("/gate-passes/{gate_pass_id}/pages/{page_no}")
def remove_gate_pass_page(
    tenant_id: str,
    gate_pass_id: UUID,
    page_no: int,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Remove one photo from a register on file (product owner 2026-11-07), while its date is
    within N to N-3. The photo is kept, marked removed with who removed it; a register keeps at
    least one photo (one per day when Sunday and Monday were photographed apart)."""
    _authorize(connection, tenant_id, principal, client, _UPDATE)
    row = _editable_register(connection, tenant_id, gate_pass_id, principal)
    page = next((p for p in row["pages"] if p["page_no"] == page_no), None)
    if page is None:
        raise HTTPException(status_code=404, detail="This photo is not on the gate register.")
    rest = [p for p in row["pages"] if p["page_no"] != page_no]
    if not rest:
        raise HTTPException(status_code=409, detail="A gate register needs at least one photo. Add the new photo "
                                                    "first, or replace all the photos.")
    if row.get("photo_mode") == "SEPARATE" and not any(p["page_date"] == page["page_date"] for p in rest):
        raise HTTPException(status_code=409, detail=f"Keep at least one photo for {page['page_date']:%A}: add "
                                                    "the new photo first, or replace all the photos.")
    connection.execute(
        text("UPDATE auditcore.p2_gate_pass_pages SET removed_at_utc = now(), removed_by_actor_id = :a "
             "WHERE tenant_id = :t AND gate_pass_id = :i AND page_no = :n"),
        {"a": principal.subject, "t": tenant_id, "i": str(gate_pass_id), "n": page_no},
    )
    _refresh_cover(connection, tenant_id, gate_pass_id)
    return _public_gate_pass(_gate_pass_row(connection, tenant_id, gate_pass_id))


class DayCounts(BaseModel):
    date: date
    newDeliveries: int = Field(ge=0, le=500)
    # How many of the day's deliveries the dealership intimated to the PC (2026-11-02).
    intimatedToPc: int | None = Field(default=None, ge=0, le=500)
    stockTransfers: int = Field(default=0, ge=0, le=500)
    testDrives: int = Field(default=0, ge=0, le=500)
    otherCount: int = Field(default=0, ge=0, le=500)
    otherNote: str | None = Field(default=None, max_length=500)


class CountsCommand(BaseModel):
    # Total deliveries on the gate register.
    newDeliveries: int = Field(default=0, ge=0, le=1000)
    # How many of them the dealership intimated to the PC (product owner 2026-11-02). Sent by the
    # current screen; an older client leaves it out and answers the difference afterwards.
    intimatedToPc: int | None = Field(default=None, ge=0, le=1000)
    stockTransfers: int = Field(default=0, ge=0, le=1000)
    testDrives: int = Field(default=0, ge=0, le=1000)
    otherCount: int = Field(default=0, ge=0, le=1000)
    otherNote: str | None = Field(default=None, max_length=1000)
    # Sunday and Monday photographed apart: the counts of each day; the totals are their sum.
    days: list[DayCounts] | None = None


def _next_step(divergence: str) -> str:
    return {"NONE": "DONE", "EXTRA_FILES": "EXPLAIN_EXTRA_FILES", "MISSING_FILES": "CONFIRM_FILES_HELD"}[divergence]


@router.post("/gate-passes/{gate_pass_id}/counts")
def submit_counts(
    tenant_id: str,
    gate_pass_id: UUID,
    command: CountsCommand,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    _authorize(connection, tenant_id, principal, client, _UPDATE)
    row = _own_gate_pass(connection, tenant_id, gate_pass_id, principal.subject)
    # Counts already entered are corrected only on the day the register was uploaded (2026-11-03).
    editing = row["counts_submitted_at_utc"] is not None
    if editing and not counts_editable(row):
        raise HTTPException(status_code=409, detail="The counts can be corrected only on the day the gate "
                                                    "register was uploaded.")
    day_counts = None
    if command.days:
        covers = gate_pass_covers(row["business_date"])
        if sorted(d.date for d in command.days) != covers:
            raise HTTPException(status_code=422, detail="Enter the counts of each day: "
                                + " and ".join(gate_pass_day_label(d) for d in covers) + ".")
        for d in command.days:
            if d.otherCount > 0 and not (d.otherNote or "").strip():
                raise HTTPException(status_code=422, detail="Say what the other entries are.")
        for d in command.days:
            if d.intimatedToPc is not None and d.intimatedToPc > d.newDeliveries:
                raise HTTPException(status_code=422, detail=f"{d.date:%a}: more deliveries intimated to you than "
                                                            "the register lists.")
        day_counts = [{**d.model_dump(), "date": d.date.isoformat()} for d in command.days]
        intimated_days = [d.intimatedToPc for d in command.days]
        command = CountsCommand(
            newDeliveries=sum(d.newDeliveries for d in command.days),
            intimatedToPc=(sum(i for i in intimated_days if i is not None)
                           if all(i is not None for i in intimated_days) else None),
            stockTransfers=sum(d.stockTransfers for d in command.days),
            testDrives=sum(d.testDrives for d in command.days),
            otherCount=sum(d.otherCount for d in command.days),
            otherNote="; ".join(f"{d.date:%a}: {d.otherNote.strip()}" for d in command.days
                                if (d.otherNote or "").strip()) or None,
        )
    note = (command.otherNote or "").strip()
    if command.otherCount > 0 and not note:
        raise HTTPException(status_code=422, detail="Say what the other entries are.")
    files = delivery_file_count(connection, tenant_id, row["outlet_id"], row["business_date"])
    declared = command.newDeliveries
    intimated = command.intimatedToPc
    if intimated is not None and intimated > declared:
        raise HTTPException(status_code=422, detail="More deliveries intimated to you than the register lists.")
    divergence = "NONE" if declared == files else ("EXTRA_FILES" if files > declared else "MISSING_FILES")
    # With the intimated count the day settles now (2026-11-02): not intimated = total - intimated, and
    # what the PC still has to upload is intimated - uploaded. Only more uploads than the register lists
    # still asks the PC which belong to other days. An older client (no intimated count) answers the
    # difference in the next step, as before.
    not_notified = declared - intimated if intimated is not None else None
    settled = divergence == "NONE" or (intimated is not None and divergence == "MISSING_FILES")
    pending = max(intimated - files, 0) if intimated is not None else None
    if editing:
        before = {"newDeliveries": row["new_deliveries"], "notNotifiedToPc": row.get("not_notified_count"),
                  "stockTransfers": row["stock_transfers"], "testDrives": row["test_drives"],
                  "otherCount": row["other_count"], "otherNote": row["other_note"], "dayCounts": row.get("day_counts"),
                  "replacedAtUtc": datetime.now(UTC).isoformat(), "replacedBy": principal.subject}
        connection.execute(
            text("UPDATE auditcore.p2_gate_passes SET counts_history = counts_history || CAST(:h AS jsonb), "
                 "counts_edit_count = counts_edit_count + 1, counts_edited_at_utc = now() "
                 "WHERE tenant_id = :t AND gate_pass_id = :i"),
            {"h": json.dumps([before], default=str), "t": tenant_id, "i": str(gate_pass_id)},
        )
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_gate_passes
            SET new_deliveries = :nd, stock_transfers = :st, test_drives = :td, other_count = :oc,
                other_note = :on, delivery_files = :files, divergence = CAST(:div AS varchar),
                day_counts = CAST(:dc AS jsonb), counts_submitted_at_utc = now(),
                not_notified_count = :nn, files_held = :held,
                completed_at_utc = CASE WHEN :settled THEN now() END
            WHERE tenant_id = :t AND gate_pass_id = :i
            """
        ),
        {"nd": declared, "st": command.stockTransfers, "td": command.testDrives, "oc": command.otherCount,
         "on": note or None, "files": files, "div": divergence, "t": tenant_id, "i": str(gate_pass_id),
         "dc": json.dumps(day_counts) if day_counts else None, "nn": not_notified,
         "held": (pending > 0) if pending is not None else None, "settled": settled},
    )
    if settled:
        close_gate_pass_tasks(connection, tenant_id=tenant_id, outlet_id=row["outlet_id"],
                              business_date=row["business_date"], include_incomplete=True)
    task_raised = False
    if editing:
        # The Team Lead's task follows the corrected numbers.
        fresh = _gate_pass_row(connection, tenant_id, gate_pass_id)
        task_raised = _sync_not_notified_task(connection, tenant_id, fresh, principal.subject,
                                              not_notified=not_notified or 0, not_uploaded=pending or 0)
    elif not_notified:
        fresh = _gate_pass_row(connection, tenant_id, gate_pass_id)
        task_raised = _raise_delivery_not_notified(connection, tenant_id, fresh, principal.subject,
                                                   not_notified=not_notified, not_uploaded=pending or 0)
    return {
        "gatePass": _public_gate_pass(_gate_pass_row(connection, tenant_id, gate_pass_id)),
        "divergence": divergence, "declared": declared, "deliveryFiles": files,
        "difference": abs(declared - files),
        "nextStep": "DONE" if settled else _next_step(divergence),
        "intimatedToPc": intimated, "notIntimatedToPc": not_notified, "pendingWithPc": pending,
        "taskRaised": task_raised,
    }


class AnswerCommand(BaseModel):
    pastDeliveries: str | None = Field(default=None, max_length=1000)
    # How many of the missing deliveries the dealership did not notify to the PC
    # (product owner 2026-10-27). filesHeld is the older yes/no answer, still read.
    notNotified: int | None = Field(default=None, ge=0)
    filesHeld: bool | None = None


def _raise_delivery_not_notified(connection: Connection, tenant_id: str, row: dict[str, Any], actor_id: str, *,
                                 not_notified: int, not_uploaded: int = 0) -> bool:
    """Raise the Team Lead's "Delivery not notified to the PC" task of the register. Not
    while those tasks are suspended (product owner 2026-11-07, task_manager.
    gate_pass_tl_tasks_enabled): the comparison is kept on the register, no task is raised.
    True when a task is open for the Team Lead afterwards."""
    if not gate_pass_tl_tasks_enabled():
        return False
    missing = not_notified
    day = row["business_date"]
    title = f"Delivery not notified to the PC: {row['outlet_name']}, {gate_pass_day_label(day)}"
    description = (f"The gate register of {row['outlet_name']} for {gate_pass_day_label(day)} lists {row['new_deliveries']} "
                   f"deliveries; {row['delivery_files']} of that day are uploaded in the tool. "
                   f"{row['pc_name']} says {not_notified} {'delivery was' if not_notified == 1 else 'deliveries were'} "
                   f"not intimated to them by the dealership"
                   + (f", and {not_uploaded} {'is' if not_uploaded == 1 else 'are'} still to be uploaded by them."
                      if not_uploaded else "."))
    # A Task Manager task of the outlet for the Team Lead, closed by the TL -- or the
    # outlet's Reviewer (task_closure_roles, 2026-11-02) -- with a note (product owner
    # 2026-10-26: daily tasks live in the Task Manager).
    task_id = insert_task_manager_task(connection, tenant_id=tenant_id, values={
        "scope": "OUTLET", "dealer_id": str(row["dealer_id"]), "outlet_id": str(row["outlet_id"]),
        "task_type": TASK_DELIVERY_NOT_NOTIFIED, "category": DAILY_TASK_CATEGORY, "origin": "SYSTEM",
        "source_type": "GATE_PASS", "source_code": str(row["gate_pass_id"]),
        "dedupe_key": f"gatepass:{row['gate_pass_id']}:{TASK_DELIVERY_NOT_NOTIFIED}",
        "title": title, "description": description, "priority": "HIGH",
        "reference": {"declared": row["new_deliveries"], "deliveryFiles": row["delivery_files"], "missing": missing,
                      "notUploadedByPc": not_uploaded,
                      "pcName": row["pc_name"], "gatePassId": str(row["gate_pass_id"]),
                      "businessDate": day.isoformat(), "outletName": row["outlet_name"]},
        "assigned_role": "TL", "raised_by": actor_id, "raised_role": "PC", "raised_name": row["pc_name"],
        "protocol": "SELF_CLOSING",
    })
    if task_id is not None:
        connection.execute(
            text("INSERT INTO auditcore.p2_task_events (tenant_id, task_id, journey_id, event_type, actor_id, "
                 "actor_role_code, details) VALUES (:t, :id, NULL, 'RAISED', :a, 'PC', CAST(:d AS jsonb))"),
            {"t": tenant_id, "id": str(task_id), "a": actor_id, "d": json.dumps({"gatePassId": str(row["gate_pass_id"])})},
        )
        return True
    # The register's task exists already (one per register, by its dedupe key). One withdrawn
    # while these tasks were suspended does not block raising it again: it is reopened.
    existing = connection.execute(
        text("SELECT task_id, task_status, completion_result->>'outcome' AS outcome FROM auditcore.p2_tasks "
             "WHERE tenant_id = :t AND dedupe_key = :k"),
        {"t": tenant_id, "k": f"gatepass:{row['gate_pass_id']}:{TASK_DELIVERY_NOT_NOTIFIED}"},
    ).mappings().one_or_none()
    if existing is None:
        return False
    if existing["task_status"] == "CANCELLED" and existing["outcome"] == "SUSPENDED":
        _reopen_not_notified_task(connection, tenant_id, existing["task_id"], description=description,
                                  reference={"declared": row["new_deliveries"], "deliveryFiles": row["delivery_files"],
                                             "missing": missing, "notUploadedByPc": not_uploaded})
        return True
    return existing["task_status"] not in ("VERIFIED_COMPLETE", "CANCELLED", "FAILED", "DEAD_LETTER")


def _reopen_not_notified_task(connection: Connection, tenant_id: str, task_id: Any, *, description: str,
                              reference: dict[str, Any]) -> None:
    """The register's Team Lead task with the latest numbers, open again if it was closed or
    withdrawn (a suspension's mark is cleared)."""
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks
            SET description = :d, updated_at_utc = now(),
                reference = reference || CAST(:ref AS jsonb),
                completion_result = CASE WHEN completion_result->>'outcome' = 'SUSPENDED'
                                         THEN (completion_result - 'outcome' - 'reason' - 'statusBeforeSuspension')
                                              || jsonb_build_object('reraisedAtUtc', now())
                                         ELSE completion_result END,
                verified_at_utc = CASE WHEN task_status IN ('VERIFIED_COMPLETE','CANCELLED') THEN NULL
                                       ELSE verified_at_utc END,
                task_status = CASE WHEN task_status IN ('VERIFIED_COMPLETE','CANCELLED') THEN 'READY'
                                   ELSE task_status END
            WHERE tenant_id = :t AND task_id = :id
            """
        ),
        {"d": description, "t": tenant_id, "id": str(task_id), "ref": json.dumps(reference)},
    )


def _sync_not_notified_task(connection: Connection, tenant_id: str, row: dict[str, Any], actor_id: str, *,
                            not_notified: int, not_uploaded: int) -> bool:
    """After the PC corrects the counts (2026-11-03): the "delivery not notified" task of the
    gate register says the corrected numbers -- raised if there was none, opened again if the
    Team Lead had closed it, cancelled when nothing is left not intimated. True when a task
    is open for the Team Lead afterwards."""
    key = f"gatepass:{row['gate_pass_id']}:{TASK_DELIVERY_NOT_NOTIFIED}"
    if not gate_pass_tl_tasks_enabled():
        # Suspended (2026-11-07): nothing raised or reopened; one still open is withdrawn now.
        suspend_gate_pass_tl_tasks(connection, tenant_id=tenant_id)
        return False
    task = connection.execute(
        text("SELECT task_id, task_status FROM auditcore.p2_tasks WHERE tenant_id = :t AND dedupe_key = :k"),
        {"t": tenant_id, "k": key},
    ).mappings().one_or_none()
    if task is None:
        if not_notified:
            return _raise_delivery_not_notified(connection, tenant_id, row, actor_id, not_notified=not_notified,
                                                not_uploaded=not_uploaded)
        return False
    open_ = task["task_status"] not in ("VERIFIED_COMPLETE", "CANCELLED", "FAILED", "DEAD_LETTER")
    if not not_notified:
        if open_:
            connection.execute(
                text("UPDATE auditcore.p2_tasks SET task_status = 'CANCELLED', updated_at_utc = now(), "
                     "completion_result = CAST(:r AS jsonb) WHERE tenant_id = :t AND task_id = :id"),
                {"r": json.dumps({"note": "The PC corrected the gate register: every delivery was intimated."}),
                 "t": tenant_id, "id": task["task_id"]},
            )
        return False
    day = row["business_date"]
    description = (f"The gate register of {row['outlet_name']} for {gate_pass_day_label(day)} lists "
                   f"{row['new_deliveries']} deliveries; {row['delivery_files']} of that day are uploaded in the tool. "
                   f"{row['pc_name']} says {not_notified} {'delivery was' if not_notified == 1 else 'deliveries were'} "
                   f"not intimated to them by the dealership"
                   + (f", and {not_uploaded} {'is' if not_uploaded == 1 else 'are'} still to be uploaded by them."
                      if not_uploaded else ".")
                   + f" (Counts corrected by the PC, {int(row.get('counts_edit_count') or 0)}x.)")
    _reopen_not_notified_task(connection, tenant_id, task["task_id"], description=description,
                              reference={"declared": row["new_deliveries"], "deliveryFiles": row["delivery_files"],
                                         "missing": not_notified, "notUploadedByPc": not_uploaded,
                                         "countsCorrected": True})
    return True


@router.post("/gate-passes/{gate_pass_id}/answer")
def answer_divergence(
    tenant_id: str,
    gate_pass_id: UUID,
    command: AnswerCommand,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    _authorize(connection, tenant_id, principal, client, _UPDATE)
    row = _own_gate_pass(connection, tenant_id, gate_pass_id, principal.subject)
    if row["counts_submitted_at_utc"] is None:
        raise HTTPException(status_code=409, detail="Enter the counts first.")
    if row["completed_at_utc"] is not None:
        raise HTTPException(status_code=409, detail="This gate pass is already finished.")
    if row["divergence"] == "EXTRA_FILES":
        note = (command.pastDeliveries or "").strip()
        if not note:
            raise HTTPException(status_code=422, detail="Enter the number or name of the customer journeys delivered on an earlier day.")
        connection.execute(
            text("UPDATE auditcore.p2_gate_passes SET past_delivery_note = :n, completed_at_utc = now() "
                 "WHERE tenant_id = :t AND gate_pass_id = :i"),
            {"n": note, "t": tenant_id, "i": str(gate_pass_id)},
        )
        close_gate_pass_tasks(connection, tenant_id=tenant_id, outlet_id=row["outlet_id"],
                              business_date=row["business_date"], include_incomplete=True)
    elif row["divergence"] == "MISSING_FILES":
        missing = max(int(row["new_deliveries"] or 0) - int(row["delivery_files"] or 0), 0)
        not_notified = command.notNotified
        if not_notified is None and command.filesHeld is not None:
            not_notified = 0 if command.filesHeld else missing
        if not_notified is None:
            raise HTTPException(status_code=422, detail="Say how many of these deliveries the dealership did not notify to you.")
        if not_notified > missing:
            raise HTTPException(status_code=422, detail=f"That is more than the {missing} deliveries not found in the tool.")
        not_uploaded = missing - not_notified
        connection.execute(
            text("UPDATE auditcore.p2_gate_passes SET not_notified_count = :n, files_held = :h, completed_at_utc = now() "
                 "WHERE tenant_id = :t AND gate_pass_id = :i"),
            {"n": not_notified, "h": not_uploaded > 0, "t": tenant_id, "i": str(gate_pass_id)},
        )
        close_gate_pass_tasks(connection, tenant_id=tenant_id, outlet_id=row["outlet_id"],
                              business_date=row["business_date"], include_incomplete=True)
        raised = bool(not_notified) and _raise_delivery_not_notified(
            connection, tenant_id, row, principal.subject, not_notified=not_notified, not_uploaded=not_uploaded)
        return {"gatePass": _public_gate_pass(_gate_pass_row(connection, tenant_id, gate_pass_id)),
                "taskRaised": raised, "notNotified": not_notified, "notUploadedByPc": not_uploaded}
    else:
        raise HTTPException(status_code=409, detail="There is no difference to answer.")
    return {"gatePass": _public_gate_pass(_gate_pass_row(connection, tenant_id, gate_pass_id)),
            "taskRaised": False}


@router.get("/gate-passes/calendar")
def gate_pass_calendar(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    outletId: UUID | None = None,
    dealerId: UUID | None = None,
) -> dict[str, Any]:
    """Per day: deliveries declared on the gate pass, Delivery files uploaded, and the files missing.

    The gap is split as the consolidated view splits it (product owner 2026-10-29):
    notNotified -- deliveries the PC said the dealership did not intimate to them;
    toUpload -- the rest, whose files the PC still has to upload (the whole gap
    while the PC has not answered). ``missing`` stays the whole gap.

    The photo (gatePassId) is only given when one outlet is chosen: a dealership
    or all-outlets view has figures only, nothing to open or download."""
    _authorize(connection, tenant_id, principal, client, _READ)
    # Every outlet, satellites included (2026-11-02).
    outlets = scope_outlets(connection, tenant_id, principal.subject)
    chosen = outlets
    scope = "ALL"
    if outletId is not None:
        chosen, scope = [_outlet_in_scope(outlets, outletId)], "OUTLET"
    elif dealerId is not None:
        chosen = [o for o in outlets if str(o["dealer_id"]) == str(dealerId)]
        if not chosen:
            raise HTTPException(status_code=404, detail="Dealership not found in your current Project scope.")
        scope = "DEALER"
    year, mon = int(month[:4]), int(month[5:])
    first, last = date(year, mon, 1), date(year, mon, _calendar.monthrange(year, mon)[1])
    ids = [str(o["outlet_id"]) for o in chosen]
    declared_rows = connection.execute(
        text(
            """
            SELECT business_date, outlet_id, gate_pass_id, new_deliveries, divergence, completed_at_utc,
                   not_notified_count, files_held
            FROM auditcore.p2_gate_passes
            WHERE tenant_id = :t AND status = 'ACTIVE' AND counts_submitted_at_utc IS NOT NULL
              AND outlet_id = ANY(CAST(:ids AS uuid[])) AND business_date BETWEEN :a AND :b
            """
        ),
        {"t": tenant_id, "ids": ids, "a": first, "b": last},
    ).mappings().all()
    uploaded = vehicle_gate_pass_deliveries(connection, tenant_id, ids, first, last)
    per_day: dict[date, dict[str, Any]] = {}

    def day_entry(day: date) -> dict[str, Any]:
        return per_day.setdefault(day, {"date": day.isoformat(), "declared": 0, "uploaded": 0, "missing": 0,
                                        "notNotified": 0, "toUpload": 0, "gatePasses": 0, "open": 0})

    def blank() -> dict[str, Any]:
        return {"declared": 0, "uploaded": 0, "notNotified": None, "filesHeld": None}

    per_outlet: dict[tuple[date, str], dict[str, Any]] = {}
    for key, files in uploaded.items():
        per_outlet.setdefault(key, blank())["uploaded"] += files
    gate_pass_ids: dict[date, str] = {}
    for r in declared_rows:
        figures = per_outlet.setdefault((r["business_date"], str(r["outlet_id"])), blank())
        figures["declared"] += int(r["new_deliveries"] or 0)
        figures["notNotified"], figures["filesHeld"] = r["not_notified_count"], r["files_held"]
        entry = day_entry(r["business_date"])
        entry["gatePasses"] += 1
        if r["completed_at_utc"] is None:
            entry["open"] += 1
        if scope == "OUTLET":
            gate_pass_ids[r["business_date"]] = str(r["gate_pass_id"])
            entry["divergence"] = r["divergence"]
    for (day, _outlet), figures in per_outlet.items():
        entry = day_entry(day)
        entry["declared"] += figures["declared"]
        entry["uploaded"] += figures["uploaded"]
        # Missing is worked out per outlet, so one outlet's surplus never hides another's gap.
        gap = max(figures["declared"] - figures["uploaded"], 0)
        # The PC's answer (2026-10-27); the older yes/no answer "files not held" is the whole gap.
        not_notified = figures["notNotified"]
        if not_notified is None:
            not_notified = gap if figures["filesHeld"] is False else 0
        not_notified = min(int(not_notified), gap)
        entry["missing"] += gap
        entry["notNotified"] += not_notified
        entry["toUpload"] += gap - not_notified
    for day, gate_pass_id in gate_pass_ids.items():
        per_day[day]["gatePassId"] = gate_pass_id
    days = [per_day[d] for d in sorted(per_day)]
    return {
        "month": month, "scope": scope,
        "outlets": [_public_outlet(o) for o in outlets],
        "days": days,
        "totals": {"declared": sum(d["declared"] for d in days), "uploaded": sum(d["uploaded"] for d in days),
                   "missing": sum(d["missing"] for d in days),
                   "notNotified": sum(d["notNotified"] for d in days),
                   "toUpload": sum(d["toUpload"] for d in days)},
    }


MAX_SUMMARY_DAYS = 31
MAX_PAGES = 4


@router.get("/gate-passes/summary")
def gate_pass_summary(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> dict[str, Any]:
    """Consolidated view of the daily gate pass across the caller's outlets, satellites included
    (TL: the outlets under them; PM and Executive: all), per outlet and day
    (product owner 2026-10-27):

    - asPerRegister: new deliveries on the gate-out register (the gate pass counts);
    - updatedInTool: deliveries whose vehicle gate pass, dated that day, is uploaded to the Journey
      (vehicle_gate_pass_deliveries, the same count as the PC's Daily Gate Pass task);
    - notNotifiedToPc: how many of the rest the PC said the dealership did not notify to them
      ("Not notified to PC", 2026-10-27; also returned as pendingDueToClient for older clients);
    - pendingDueToPc: as per register - (updated in tool + not notified to PC): what the PC has
      not uploaded (the whole gap while the PC has not answered);
    - stockTransfers: stock transfer movements on the register (product owner 2026-11-05: big
      outlets move stock between outlets). Reported beside the deliveries, never part of them.
    """
    _authorize(connection, tenant_id, principal, client, _READ)
    today = ist_day().date()
    end = min(end or today, today)
    start = start or end
    if start > end or (end - start).days >= MAX_SUMMARY_DAYS:
        raise HTTPException(status_code=422, detail=f"Choose a range of 1 to {MAX_SUMMARY_DAYS} days.")
    outlets = scope_outlets(connection, tenant_id, principal.subject)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).isoweekday() != 7]
    ids = [str(o["outlet_id"]) for o in outlets]
    empty = {"asPerRegister": 0, "updatedInTool": 0, "notNotifiedToPc": 0, "pendingDueToPc": 0,
             "stockTransfers": 0, "gatePassDays": 0, "gatePassesFinished": 0, "gatePassesMissing": 0}
    if not ids or not days:
        return {"from": start.isoformat(), "to": end.isoformat(), "outlets": [], "totals": dict(empty), "notes": []}
    passes = {(r["business_date"], str(r["outlet_id"])): dict(r) for r in connection.execute(
        text("""
            SELECT g.business_date, g.outlet_id, g.gate_pass_id, g.pc_name, g.new_deliveries, g.files_held,
                   g.not_notified_count, g.stock_transfers,
                   g.divergence, g.counts_submitted_at_utc, g.completed_at_utc, g.quality_flag, g.photo_mode,
                   (SELECT count(*) FROM auditcore.p2_gate_pass_pages pp
                    WHERE pp.tenant_id = g.tenant_id AND pp.gate_pass_id = g.gate_pass_id
                      AND pp.removed_at_utc IS NULL) AS pages
            FROM auditcore.p2_gate_passes g
            WHERE tenant_id = :t AND status = 'ACTIVE' AND outlet_id = ANY(CAST(:ids AS uuid[]))
              AND business_date BETWEEN :a AND :b
        """), {"t": tenant_id, "ids": ids, "a": start, "b": end}).mappings().all()}
    uploaded = vehicle_gate_pass_deliveries(connection, tenant_id, ids, start, end)

    leads = _team_leads(connection, tenant_id, outlets)
    totals = dict(empty)
    rows = []
    for o in outlets:
        oid = str(o["outlet_id"])
        line = dict(empty)
        per_day = []
        uploaders: set[str] = set()
        for d in days:
            gp = passes.get((d, oid))
            files = uploaded.get((d, oid), 0)
            entry = {"date": d.isoformat(), "label": gate_pass_day_label(d), "updatedInTool": files,
                     "asPerRegister": None, "notNotifiedToPc": 0, "pendingDueToPc": 0, "stockTransfers": None,
                     "status": "NOT_UPLOADED", "gatePassId": None, "pcName": None, "photoQuality": None}
            line["gatePassDays"] += 1
            line["updatedInTool"] += files
            if gp is None:
                line["gatePassesMissing"] += 1
            else:
                entry.update(gatePassId=str(gp["gate_pass_id"]), pcName=gp["pc_name"],
                             photoQuality=gp["quality_flag"] or "GOOD", pages=max(1, int(gp["pages"] or 0)),
                             photoMode=gp["photo_mode"])
                if gp["pc_name"]:
                    uploaders.add(gp["pc_name"])
                if gp["counts_submitted_at_utc"] is None:
                    entry["status"] = "COUNTS_PENDING"
                else:
                    register = int(gp["new_deliveries"] or 0)
                    pending = max(register - files, 0)
                    entry["asPerRegister"] = register
                    # The PC's count of deliveries the dealership did not notify (2026-10-27);
                    # the rest of the gap is the PC's to upload.
                    not_notified = gp["not_notified_count"]
                    if not_notified is None:
                        not_notified = pending if gp["files_held"] is False else 0
                    entry["notNotifiedToPc"] = min(int(not_notified), pending)
                    entry["pendingDueToPc"] = pending - entry["notNotifiedToPc"]
                    entry["status"] = "DONE" if gp["completed_at_utc"] is not None else "ANSWER_PENDING"
                    entry["stockTransfers"] = int(gp["stock_transfers"] or 0)
                    line["stockTransfers"] += entry["stockTransfers"]
                    line["asPerRegister"] += register
                    line["notNotifiedToPc"] += entry["notNotifiedToPc"]
                    line["pendingDueToPc"] += entry["pendingDueToPc"]
                if gp["completed_at_utc"] is not None:
                    line["gatePassesFinished"] += 1
            per_day.append(entry)
        for k in totals:
            totals[k] += line[k]
        rows.append({"outletId": oid, "outletName": o["outlet_name"], "dealerId": str(o["dealer_id"]),
                     "dealerName": o["dealer_name"], "teamLeads": leads.get(oid, []),
                     "uploadedBy": sorted(uploaders), **line, "days": per_day})
    for figures in [totals, *rows, *(d for r in rows for d in r["days"])]:
        figures["pendingDueToClient"] = figures["notNotifiedToPc"]
    return {
        "from": start.isoformat(), "to": end.isoformat(), "outlets": rows, "totals": totals,
        "notes": [
            "Every outlet, satellite outlets included (from 2 Nov 2026).",
            ("As per gate-out register: new deliveries on the gate pass. Updated in tool: deliveries whose vehicle "
             "gate pass, dated that day, is uploaded to the Journey (Monday's covers Sunday); a gate pass uploaded "
             "later still counts on the day it is dated."),
            ("Not notified to PC: deliveries the PC said the dealership did not notify to them. Pending due to PC: "
             "as per register - (updated in tool + not notified to PC): deliveries the PC has not uploaded, "
             "or the whole gap while the PC has not answered yet."),
            "Stock transfers: vehicles moved between outlets on the register; not deliveries, not in the gap.",
        ],
    }


def _team_leads(connection: Connection, tenant_id: str, outlets: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Names of the Team Leads over each outlet (assigned to it, its dealer, or the whole Project)."""
    from audit_core.task_manager import known_names

    rows = connection.execute(text("""
        SELECT security_actor_id, dealer_id, outlet_id FROM auditcore.business_assignments ba
        WHERE ba.tenant_id = :t AND upper(ba.business_role_code) = 'TL' AND ba.assignment_status = 'ACTIVE'
          AND ba.effective_from <= now() AND (ba.effective_to IS NULL OR ba.effective_to >= now())
    """), {"t": tenant_id}).mappings().all()
    names = known_names(connection, tenant_id=tenant_id)
    out: dict[str, list[str]] = {}
    for o in outlets:
        oid, did = str(o["outlet_id"]), str(o["dealer_id"])
        found = sorted({names.get(r["security_actor_id"]) or r["security_actor_id"] for r in rows
                        if (r["dealer_id"] is None or str(r["dealer_id"]) == did)
                        and (r["outlet_id"] is None or str(r["outlet_id"]) == oid)})
        out[oid] = found
    return out


@router.get("/gate-passes/{gate_pass_id}")
def get_own_gate_pass(
    tenant_id: str,
    gate_pass_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The PC's own gate register, to correct its counts on the day it was uploaded (2026-11-03)."""
    _authorize(connection, tenant_id, principal, client, _READ)
    return {"gatePass": _public_gate_pass(_own_gate_pass(connection, tenant_id, gate_pass_id, principal.subject))}


@router.get("/gate-passes/{gate_pass_id}/image-url")
def gate_pass_image_url(
    tenant_id: str,
    gate_pass_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    page: Annotated[int, Query(ge=1, le=8)] = 1,
) -> dict[str, Any]:
    _authorize(connection, tenant_id, principal, client, _READ)
    row = _gate_pass_row(connection, tenant_id, gate_pass_id)
    _outlet_in_scope(scope_outlets(connection, tenant_id, principal.subject), row["outlet_id"])
    # ?page=n opens the n-th live photo (pages[].position): a removed photo is not opened (2026-11-07).
    chosen = row["pages"][page - 1] if page <= len(row["pages"]) else None
    if chosen is None and page != 1:
        raise HTTPException(status_code=404, detail="This gate pass has no such page.")
    key = chosen["object_key"] if chosen else row["object_key"]
    content_type = chosen["content_type"] if chosen else row["content_type"]
    try:
        url = get_p2_document_storage().presign_get(key, expires_seconds=600)
    except (P2DocumentStorageError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="The photo could not be opened just now. Please try again.") from exc
    return {"url": url, "contentType": content_type, "filename": key.rsplit("/", 1)[-1],
            "page": page, "pages": max(1, len(row["pages"]))}


# Daily tasks now live in the Task Manager (p2_tasks, OUTLET scope); these two
# endpoints keep their response shape for the Daily Gate Pass screen.
_DAILY_TASK_SQL = """
    SELECT t.task_id, t.category, t.task_type, t.title, t.description, t.priority, t.task_status,
           t.outlet_id, COALESCE(o.outlet_name, t.reference->>'outletName') AS outlet_name,
           t.reference, t.created_at_utc, t.verified_at_utc, t.completion_result
    FROM auditcore.p2_tasks t
    LEFT JOIN auditcore.dealer_outlets o ON o.tenant_id = t.tenant_id AND o.outlet_id = t.outlet_id
"""


def _public_task(row: dict[str, Any]) -> dict[str, Any]:
    reference = row["reference"] or {}
    done = row["task_status"] == "VERIFIED_COMPLETE"
    return {
        "dailyTaskId": str(row["task_id"]), "category": row["category"], "taskType": row["task_type"],
        "title": row["title"], "description": row["description"], "priority": row["priority"],
        "status": "DONE" if done else "OPEN", "outletId": str(row["outlet_id"]), "outletName": row["outlet_name"],
        "businessDate": reference.get("businessDate"), "gatePassId": reference.get("gatePassId"),
        "details": reference, "createdAtUtc": row["created_at_utc"].isoformat(),
        "resolvedAtUtc": row["verified_at_utc"].isoformat() if row["verified_at_utc"] else None,
        "resolutionNote": (row["completion_result"] or {}).get("note"),
    }


@router.get("/daily-tasks")
def list_daily_tasks(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    status: Annotated[str, Query(pattern="^(OPEN|DONE|ALL)$")] = "OPEN",
) -> dict[str, Any]:
    """The Daily Tasks of the outlets the caller supervises (TL, PM, Executive)."""
    _authorize(connection, tenant_id, principal, client, _READ)
    supervised = [str(o["outlet_id"]) for o in scope_outlets(connection, tenant_id, principal.subject)
                  if set(o["roles"]) & set(_SUPERVISOR_ROLES)]
    rows = connection.execute(
        text(
            _DAILY_TASK_SQL + """
            WHERE t.tenant_id = :t AND t.category = :cat AND t.scope_kind = 'OUTLET'
              AND t.outlet_id = ANY(CAST(:ids AS uuid[]))
              AND (:status = 'ALL'
                   OR (:status = 'DONE' AND t.task_status = 'VERIFIED_COMPLETE')
                   OR (:status = 'OPEN' AND t.task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED')))
            ORDER BY (t.task_status <> 'VERIFIED_COMPLETE') DESC, t.created_at_utc DESC
            LIMIT 200
            """
        ),
        {"t": tenant_id, "cat": DAILY_TASK_CATEGORY, "ids": supervised, "status": status},
    ).mappings().all()
    return {"items": [_public_task(dict(r)) for r in rows]}


class ResolveCommand(BaseModel):
    note: str = Field(min_length=1, max_length=1000)


@router.post("/daily-tasks/{daily_task_id}/resolve")
def resolve_daily_task(
    tenant_id: str,
    daily_task_id: UUID,
    command: ResolveCommand,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    _authorize(connection, tenant_id, principal, client, _READ)
    row = connection.execute(
        text(_DAILY_TASK_SQL + " WHERE t.tenant_id = :t AND t.task_id = :i AND t.category = :cat FOR UPDATE OF t"),
        {"t": tenant_id, "i": str(daily_task_id), "cat": DAILY_TASK_CATEGORY},
    ).mappings().one_or_none()
    outlets = scope_outlets(connection, tenant_id, principal.subject)
    if row is None or not any(str(o["outlet_id"]) == str(row["outlet_id"]) and set(o["roles"]) & set(_SUPERVISOR_ROLES)
                              for o in outlets):
        raise HTTPException(status_code=404, detail="Daily task not found.")
    if row["task_status"] == "VERIFIED_COMPLETE":
        raise HTTPException(status_code=409, detail="This task is already done.")
    role = next((str(r).upper() for o in outlets if str(o["outlet_id"]) == str(row["outlet_id"])
                 for r in o["roles"] if str(r).upper() in _SUPERVISOR_ROLES), "TL")
    note = command.note.strip()
    connection.execute(
        text("""
            UPDATE auditcore.p2_tasks
            SET task_status = 'VERIFIED_COMPLETE', verified_at_utc = now(), action_completed_at_utc = now(),
                completed_by_actor_id = :a, updated_at_utc = now(),
                completion_result = completion_result || jsonb_build_object('note', CAST(:n AS text))
            WHERE tenant_id = :t AND task_id = :i
        """),
        {"a": principal.subject, "n": note, "t": tenant_id, "i": str(daily_task_id)},
    )
    connection.execute(
        text("INSERT INTO auditcore.p2_task_events (tenant_id, task_id, journey_id, event_type, actor_id, "
             "actor_role_code, comment) VALUES (:t, :i, NULL, 'COMPLETE_ACTION', :a, :r, :n)"),
        {"t": tenant_id, "i": str(daily_task_id), "a": principal.subject, "r": role, "n": note},
    )
    done = connection.execute(
        text(_DAILY_TASK_SQL + " WHERE t.tenant_id = :t AND t.task_id = :i"),
        {"t": tenant_id, "i": str(daily_task_id)},
    ).mappings().one()
    return _public_task(dict(done))
