"""Daily Gate Pass (product owner, 2026-10-26).

Each day a PC photographs the outlet's hand-written gate pass and says how
many new deliveries, stock transfers and test drives it lists. The new-delivery
count is checked against the Delivery files uploaded that day for the outlet:

    equal                       no divergence, the task is finished
    files > declared            the PC names the files that belong to other days
    declared > files            the PC says whether they hold the files; NO
                                raises a high-priority Daily Task for the TL

    1. POST  /gate-passes            multipart photo, quality-checked, stored in R2
    2. POST  /gate-passes/{id}/counts the three counts (and any other)
    3. POST  /gate-passes/{id}/answer the PC's answer when the counts differ
    4. GET   /gate-passes/calendar    declared / uploaded / missing per day
    5. GET   /gate-passes/{id}/image-url  the photo, one outlet at a time
    6. GET   /daily-tasks, POST /daily-tasks/{id}/resolve   the TL's Daily Tasks

A Delivery file is a Journey of the outlet with at least one Delivery-stage
page uploaded that day (IST); Booking pages are not counted, and a Journey with
several Delivery pages counts once.
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
    previous_gate_pass_day,
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


def gate_pass_days(now: datetime | None = None) -> dict[str, Any]:
    """The days a gate pass can be uploaded for now: today's (none on a
    Sunday, the off day: Sunday goes with Monday) and the previous gate pass
    day's, which stays open until 23:59 of the next gate pass day."""
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


def delivery_file_count(connection: Connection, tenant_id: str, outlet_id: UUID | str, day: date) -> int:
    """Journeys of the outlet with a Delivery-stage page uploaded on the days
    the gate pass of ``day`` covers (IST): Monday's counts Sunday's too."""
    return int(connection.execute(
        text(
            """
            SELECT COUNT(DISTINCT q.journey_id)
            FROM auditcore.p2_document_queue q
            JOIN auditcore.journeys j ON j.tenant_id = q.tenant_id AND j.journey_id = q.journey_id
            WHERE q.tenant_id = :tenant_id AND j.outlet_id = :outlet_id
              AND q.business_stage = 'DELIVERY' AND q.queue_status <> 'CANCELLED'
              AND (q.created_at_utc AT TIME ZONE 'Asia/Kolkata')::date BETWEEN :first AND :day
            """
        ),
        {"tenant_id": tenant_id, "outlet_id": str(outlet_id), "day": day, "first": gate_pass_covers(day)[0]},
    ).scalar_one() or 0)


def _gate_pass_row(connection: Connection, tenant_id: str, gate_pass_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT * FROM auditcore.p2_gate_passes WHERE tenant_id = :t AND gate_pass_id = :i"),
        {"t": tenant_id, "i": str(gate_pass_id)},
    ).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Gate pass not found.")
    return dict(row)


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
        "countsSubmitted": row["counts_submitted_at_utc"] is not None,
        "completed": row["completed_at_utc"] is not None,
        # The photo is always kept; its quality is a flag (2026-10-27).
        "photoQuality": row.get("quality_flag") or "GOOD",
        "photoQualityNote": _QUALITY_NOTES.get(row.get("quality_flag") or "GOOD"),
    }


def _own_gate_pass(connection: Connection, tenant_id: str, gate_pass_id: UUID, actor_id: str) -> dict[str, Any]:
    row = _gate_pass_row(connection, tenant_id, gate_pass_id)
    if row["pc_actor_id"] != actor_id:
        raise HTTPException(status_code=404, detail="Gate pass not found.")
    if row["status"] != "ACTIVE":
        raise HTTPException(status_code=409, detail="A newer gate pass was uploaded for this day. Use that one.")
    return row


@router.get("/gate-passes/outlets")
def gate_pass_outlets(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The outlets the caller may upload for, the mapped ONSITE outlet first and marked as the default,
    and the days a gate pass can be uploaded for. ``previousOpenOutletIds``: outlets whose gate pass of
    the previous day is not finished yet, so the screen asks which day the photo is for."""
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
        "outlets": [_public_outlet(o, default_id=default_id) for o in outlets],
        "defaultOutletId": default_id,
    }


@router.post("/gate-passes", status_code=201)
def upload_gate_pass(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    outletId: Annotated[UUID, Form()],
    file: Annotated[UploadFile, File()],
    pcName: Annotated[str | None, Form()] = None,
    businessDate: Annotated[date | None, Form()] = None,
) -> dict[str, Any]:
    _authorize(connection, tenant_id, principal, client, _UPDATE)
    outlet = _outlet_in_scope(scope_outlets(connection, tenant_id, principal.subject), outletId)
    days = gate_pass_days()
    allowed = [d for d in (days["current"], days["previous"]) if d is not None]
    business_date = businessDate or (days["current"] or days["previous"])
    if business_date not in allowed:
        if days["current"] is None and businessDate == days["today"]:
            raise HTTPException(status_code=422, detail="Sunday is the off day: upload Sunday's gate pass with "
                                                        "Monday's, one photo for both days.")
        raise HTTPException(status_code=422, detail="A gate pass can be uploaded only for "
                                                    + " or ".join(gate_pass_day_label(d) for d in allowed) + ".")
    data = file.file.read(MAX_BYTES + 1)
    assessment = assess_gate_pass_image(data)
    if not assessment.ok:
        raise HTTPException(status_code=422, detail=assessment.message)

    day = datetime.combine(business_date, datetime.min.time())
    pc_name = (pcName or "").strip() or principal.subject
    gate_pass_id = uuid4()
    label = f"{safe_part(outlet['outlet_name'], fallback='outlet')}__{safe_part(pc_name, fallback='pc')}__{day:%d-%m-%y}"
    key = (f"daily-gate-passes/{tenant_id}/{day:%Y-%m-%d}/{gate_pass_id}/"
           f"{safe_filename(label)}.{assessment.extension}")
    try:
        get_p2_document_storage().put_object(key, data, content_type=str(assessment.content_type))
    except (P2DocumentStorageError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="The photo could not be stored just now. Please try again.") from exc

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
                object_key, content_type, size_bytes, width_px, height_px, sharpness, brightness, quality_flag)
            VALUES (:t, :id, :dealer, :outlet, :outlet_name, :actor, :pc_name, :day,
                    :key, :ct, :size, :w, :h, :sharp, :bright, :quality)
            """
        ),
        {"t": tenant_id, "id": str(gate_pass_id), "dealer": str(outlet["dealer_id"]),
         "outlet": str(outlet["outlet_id"]), "outlet_name": outlet["outlet_name"], "actor": principal.subject,
         "pc_name": pc_name, "day": day.date(), "key": key, "ct": assessment.content_type, "size": len(data),
         "w": assessment.width, "h": assessment.height, "sharp": assessment.sharpness,
         "bright": assessment.brightness, "quality": assessment.quality},
    )
    return _public_gate_pass(_gate_pass_row(connection, tenant_id, gate_pass_id))


class CountsCommand(BaseModel):
    newDeliveries: int = Field(ge=0, le=500)
    stockTransfers: int = Field(ge=0, le=500)
    testDrives: int = Field(ge=0, le=500)
    otherCount: int = Field(default=0, ge=0, le=500)
    otherNote: str | None = Field(default=None, max_length=500)


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
    if row["completed_at_utc"] is not None:
        raise HTTPException(status_code=409, detail="This gate pass is already finished.")
    note = (command.otherNote or "").strip()
    if command.otherCount > 0 and not note:
        raise HTTPException(status_code=422, detail="Say what the other entries are.")
    files = delivery_file_count(connection, tenant_id, row["outlet_id"], row["business_date"])
    declared = command.newDeliveries
    divergence = "NONE" if declared == files else ("EXTRA_FILES" if files > declared else "MISSING_FILES")
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_gate_passes
            SET new_deliveries = :nd, stock_transfers = :st, test_drives = :td, other_count = :oc,
                other_note = :on, delivery_files = :files, divergence = CAST(:div AS varchar),
                counts_submitted_at_utc = now(),
                completed_at_utc = CASE WHEN CAST(:div AS varchar) = 'NONE' THEN now() END
            WHERE tenant_id = :t AND gate_pass_id = :i
            """
        ),
        {"nd": declared, "st": command.stockTransfers, "td": command.testDrives, "oc": command.otherCount,
         "on": note or None, "files": files, "div": divergence, "t": tenant_id, "i": str(gate_pass_id)},
    )
    if divergence == "NONE":
        close_gate_pass_tasks(connection, tenant_id=tenant_id, outlet_id=row["outlet_id"],
                              business_date=row["business_date"])
    return {
        "gatePass": _public_gate_pass(_gate_pass_row(connection, tenant_id, gate_pass_id)),
        "divergence": divergence, "declared": declared, "deliveryFiles": files,
        "difference": abs(declared - files), "nextStep": _next_step(divergence),
    }


class AnswerCommand(BaseModel):
    pastDeliveries: str | None = Field(default=None, max_length=1000)
    filesHeld: bool | None = None


def _raise_delivery_not_notified(connection: Connection, tenant_id: str, row: dict[str, Any], actor_id: str) -> None:
    missing = int(row["new_deliveries"] or 0) - int(row["delivery_files"] or 0)
    day = row["business_date"]
    title = f"Delivery not notified to the PC: {row['outlet_name']}, {gate_pass_day_label(day)}"
    description = (f"The gate pass of {row['outlet_name']} for {gate_pass_day_label(day)} lists {row['new_deliveries']} new "
                   f"deliveries, but only {row['delivery_files']} Delivery files were uploaded. "
                   f"{row['pc_name']} says the {missing} other {'delivery was' if missing == 1 else 'deliveries were'} "
                   f"not notified to them and they hold no files for it.")
    # A Task Manager task of the outlet for the Team Lead, closed by the TL with
    # a note (product owner 2026-10-26: daily tasks live in the Task Manager).
    task_id = insert_task_manager_task(connection, tenant_id=tenant_id, values={
        "scope": "OUTLET", "dealer_id": str(row["dealer_id"]), "outlet_id": str(row["outlet_id"]),
        "task_type": TASK_DELIVERY_NOT_NOTIFIED, "category": DAILY_TASK_CATEGORY, "origin": "SYSTEM",
        "source_type": "GATE_PASS", "source_code": str(row["gate_pass_id"]),
        "dedupe_key": f"gatepass:{row['gate_pass_id']}:{TASK_DELIVERY_NOT_NOTIFIED}",
        "title": title, "description": description, "priority": "HIGH",
        "reference": {"declared": row["new_deliveries"], "deliveryFiles": row["delivery_files"], "missing": missing,
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
                              business_date=row["business_date"])
    elif row["divergence"] == "MISSING_FILES":
        if command.filesHeld is None:
            raise HTTPException(status_code=422, detail="Answer YES or NO: do you hold the files for the other deliveries?")
        connection.execute(
            text("UPDATE auditcore.p2_gate_passes SET files_held = :h, completed_at_utc = now() "
                 "WHERE tenant_id = :t AND gate_pass_id = :i"),
            {"h": command.filesHeld, "t": tenant_id, "i": str(gate_pass_id)},
        )
        close_gate_pass_tasks(connection, tenant_id=tenant_id, outlet_id=row["outlet_id"],
                              business_date=row["business_date"])
        if command.filesHeld is False:
            _raise_delivery_not_notified(connection, tenant_id, row, principal.subject)
    else:
        raise HTTPException(status_code=409, detail="There is no difference to answer.")
    return {"gatePass": _public_gate_pass(_gate_pass_row(connection, tenant_id, gate_pass_id)),
            "taskRaised": row["divergence"] == "MISSING_FILES" and command.filesHeld is False}


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

    The photo (gatePassId) is only given when one outlet is chosen: a dealership
    or all-outlets view has figures only, nothing to open or download."""
    _authorize(connection, tenant_id, principal, client, _READ)
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
            SELECT business_date, outlet_id, gate_pass_id, new_deliveries, divergence, completed_at_utc
            FROM auditcore.p2_gate_passes
            WHERE tenant_id = :t AND status = 'ACTIVE' AND counts_submitted_at_utc IS NOT NULL
              AND outlet_id = ANY(CAST(:ids AS uuid[])) AND business_date BETWEEN :a AND :b
            """
        ),
        {"t": tenant_id, "ids": ids, "a": first, "b": last},
    ).mappings().all()
    uploaded_rows = connection.execute(
        text(
            """
            SELECT (q.created_at_utc AT TIME ZONE 'Asia/Kolkata')::date AS day, j.outlet_id,
                   COUNT(DISTINCT q.journey_id) AS files
            FROM auditcore.p2_document_queue q
            JOIN auditcore.journeys j ON j.tenant_id = q.tenant_id AND j.journey_id = q.journey_id
            WHERE q.tenant_id = :t AND q.business_stage = 'DELIVERY' AND q.queue_status <> 'CANCELLED'
              AND j.outlet_id = ANY(CAST(:ids AS uuid[]))
              AND (q.created_at_utc AT TIME ZONE 'Asia/Kolkata')::date BETWEEN :a AND :b
            GROUP BY 1, 2
            """
        ),
        # The Sunday before the 1st belongs to a Monday the 1st.
        {"t": tenant_id, "ids": ids, "a": gate_pass_covers(first)[0], "b": last},
    ).mappings().all()
    per_day: dict[date, dict[str, Any]] = {}

    def day_entry(day: date) -> dict[str, Any]:
        return per_day.setdefault(day, {"date": day.isoformat(), "declared": 0, "uploaded": 0, "missing": 0,
                                        "gatePasses": 0, "open": 0})

    per_outlet: dict[tuple[date, str], dict[str, int]] = {}
    for r in uploaded_rows:
        # Sunday's delivery files are compared with Monday's gate pass, which covers both days.
        day = r["day"] + timedelta(days=1) if r["day"].isoweekday() == 7 else r["day"]
        if not first <= day <= last:
            continue
        per_outlet.setdefault((day, str(r["outlet_id"])), {"declared": 0, "uploaded": 0})["uploaded"] += int(r["files"])
    gate_pass_ids: dict[date, str] = {}
    for r in declared_rows:
        per_outlet.setdefault((r["business_date"], str(r["outlet_id"])), {"declared": 0, "uploaded": 0})["declared"] += int(r["new_deliveries"] or 0)
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
        entry["missing"] += max(figures["declared"] - figures["uploaded"], 0)
    for day, gate_pass_id in gate_pass_ids.items():
        per_day[day]["gatePassId"] = gate_pass_id
    days = [per_day[d] for d in sorted(per_day)]
    return {
        "month": month, "scope": scope,
        "outlets": [_public_outlet(o) for o in outlets],
        "days": days,
        "totals": {"declared": sum(d["declared"] for d in days), "uploaded": sum(d["uploaded"] for d in days),
                   "missing": sum(d["missing"] for d in days)},
    }


@router.get("/gate-passes/{gate_pass_id}/image-url")
def gate_pass_image_url(
    tenant_id: str,
    gate_pass_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    _authorize(connection, tenant_id, principal, client, _READ)
    row = _gate_pass_row(connection, tenant_id, gate_pass_id)
    _outlet_in_scope(scope_outlets(connection, tenant_id, principal.subject), row["outlet_id"])
    try:
        url = get_p2_document_storage().presign_get(row["object_key"], expires_seconds=600)
    except (P2DocumentStorageError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="The photo could not be opened just now. Please try again.") from exc
    return {"url": url, "contentType": row["content_type"], "filename": row["object_key"].rsplit("/", 1)[-1]}


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
