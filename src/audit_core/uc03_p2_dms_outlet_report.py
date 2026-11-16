"""uc03_p2_dms_outlet_report.py -- the DMS dumps outlet wise, day by day, against
Verigence and the gate register (product owner 2026-11-16).

"We need to give the report outlet wise, but there is small catch ... for shivnath,
no issues, DMS will give you mapping for two outlets it has, for utkal there is only
one deviation that anandpur is mapped under keonjhar ... for Aditya its quite complex,
Jajpur is one and rest all are in one, so DMS computation should be as per that, but
you can show verigence count and total count across ... on daily basis ... you have
the data from the gatepass register as well."

The report is as at a day (today by default), month to date -- the DMS dumps are month
to date. Per dealership:

DMS location   a dump row's location (Dealer Location / Branch / Location) names a
               Verigence outlet (p2_dms_outlet_map's location names, else the outlet's
               own name); the outlet stands under its DMS location (dms_outlet_id: Utkal
               ANANDAPUR under KEONJHAR, every Aditya outlet but JAJPUR under CUTTACK). A
               row naming no outlet falls to the dealership's catch-all location, else
               shows as "DMS location not mapped" -- never dropped, so the totals agree
               with the dump.
DMS figures    from the dealership's latest current dump of each kind dated in the month
               up to the day: bookings (cancelled in the DMS left out), synced (a
               Journey found), not in Verigence; deliveries, synced, delivery not
               uploaded (Journey found, no delivery file), not in Verigence. Shown once
               per DMS location: where several outlets stand under one location, each
               outlet's row carries its own Verigence figures and the location's row the
               DMS figures against their total.
Verigence      per outlet, from the 1st up to the last day the dealership's dump of that
               kind covers (a dump of the 5th carries the 1st to the 4th: like with like),
               else up to the report's day when no dump is on file:
               bookings       Journeys whose booking date (the booking form's, else the
                              day the PC uploaded the booking documents -- the booking
                              date rule of 2026-11-07) falls in the period; of those,
                              the ones dated by the upload day are shown apart
               deliveries     delivery Journeys carrying the vehicle's own gate pass dated
                              in the period (vehicle_gate_pass_deliveries: the gate
                              register's "uploaded in tool"), and of those the Deal Type:
                              Inscope / Outscope / MR, Lease, Bulk / not yet typed
Gate register  per outlet with a PC mapped, the gate register days of the delivery
               period (a Sunday before 15 Nov 2026 on Monday's):
               on register (the gate out register's deliveries), not intimated (the
               dealership has not handed the file: pending with the client), pending with
               the PC (intimated, not in the tool), registers missed / counts pending.

``GET  /p2/v1/tenants/{t}/dms-dumps/outlet-report?date=YYYY-MM-DD``
``GET  /p2/v1/tenants/{t}/dms-dumps/outlet-report/file?date=YYYY-MM-DD``  (Excel)
``GET  /p2/v1/tenants/{t}/dms-dumps/outlet-map``
``PUT  /p2/v1/tenants/{t}/dms-dumps/outlet-map/{outlet_id}``  (Team Lead / administrator)
"""
from __future__ import annotations

import io
import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_dms_dumps import (
    _READ,
    _UPDATE,
    BOOKING,
    CANCELLED,
    DELIVERY,
    MATCHED,
    MISSING,
    NOT_IN_TOOL,
    READ_ROLES,
    _access,
    _dealers,
    _today,
    outlet_for,
)

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-dms-outlet-report"])

# Who changes the DMS location map: the Team Lead (and an administrator).
MAP_ROLES = ("TL",)

DMS_KEYS = ("bookingDms", "bookingSynced", "bookingNotInVerigence", "bookingCancelled",
            "deliveryDump", "deliverySynced", "deliveryNotUploaded", "deliveryNotInVerigence")
VERIGENCE_KEYS = ("bookingInVerigence", "bookingDatedByUpload", "deliveryInVerigence", "inscope", "outscope", "otherDealType",
                  "dealTypePending", "gateRegister", "notIntimated", "pendingWithPc", "registersMissed",
                  "countsPending")


# --------------------------------------------------------------------------- the map

def label_key(value: Any) -> str:
    """A location name as compared: letters and digits only, upper case."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def short_name(outlet_name: Any) -> str:
    """'Utkal Mahindra - KEONJHAR' -> 'KEONJHAR'."""
    return str(outlet_name or "").split(" - ", 1)[-1].strip()


def outlet_map(connection: Connection, tenant_id: str, dealer_id: str) -> list[dict[str, Any]]:
    """The dealership's outlets, each with its DMS location (itself when unmapped), its
    location names and whether unnamed locations fall to it."""
    return [dict(r) for r in connection.execute(
        text("""
            SELECT o.outlet_id, o.outlet_name, o.outlet_code, o.outlet_classification,
                   COALESCE(m.dms_outlet_id, o.outlet_id) AS dms_outlet_id,
                   COALESCE(m.dms_labels, '{}') AS dms_labels, COALESCE(m.catch_all, false) AS catch_all
            FROM auditcore.dealer_outlets o
            LEFT JOIN auditcore.p2_dms_outlet_map m ON m.tenant_id = o.tenant_id AND m.outlet_id = o.outlet_id
            WHERE o.tenant_id = :t AND o.dealer_id = :d
            ORDER BY lower(o.outlet_name)
        """),
        {"t": tenant_id, "d": dealer_id},
    ).mappings()]


def resolve_outlet(label: Any, outlets: list[dict[str, Any]]) -> str | None:
    """The Verigence outlet a dump's location names: one whose location names carry it
    (letters and digits only), else one whose own name does (outlet_for), else none."""
    key = label_key(label)
    if not key:
        return None
    named = [o for o in outlets if key in {label_key(x) for x in (o.get("dms_labels") or [])}]
    if len(named) == 1:
        return str(named[0]["outlet_id"])
    if not named:
        return outlet_for(label, outlets)
    return None


def resolve_location(label: Any, outlets: list[dict[str, Any]]) -> str | None:
    """The DMS location (its anchor outlet) a dump row belongs to; the catch-all's when
    the location names no outlet; None when there is none."""
    by_id = {str(o["outlet_id"]): o for o in outlets}
    found = resolve_outlet(label, outlets)
    if found is not None:
        return str(by_id[found]["dms_outlet_id"])
    catch = [o for o in outlets if o.get("catch_all")]
    return str(catch[0]["dms_outlet_id"]) if len(catch) == 1 else None


# --------------------------------------------------------------------------- figures

def _zero(keys: tuple[str, ...]) -> dict[str, int]:
    return dict.fromkeys(keys, 0)


def _latest_dumps(connection: Connection, tenant_id: str, dealer_ids: list[str], first: date, day: date
                  ) -> dict[tuple[str, str], dict[str, Any]]:
    """(dealer, kind) -> the latest current dump dated in the month up to the day."""
    rows = connection.execute(
        text("""
            SELECT DISTINCT ON (u.dealer_id, u.dump_kind) u.dealer_id, u.dump_kind, u.dump_id, u.business_date,
                   (SELECT max(r.event_date) FROM auditcore.p2_dms_dump_rows r
                    WHERE r.tenant_id = u.tenant_id AND r.dump_id = u.dump_id AND r.event_date <= u.business_date
                   ) AS covers_to
            FROM auditcore.p2_dms_dumps u
            WHERE u.tenant_id = :t AND u.status = 'ACTIVE' AND u.business_date BETWEEN :f AND :d
              AND CAST(u.dealer_id AS text) = ANY(:ds)
            ORDER BY u.dealer_id, u.dump_kind, u.business_date DESC, u.uploaded_at_utc DESC
        """),
        {"t": tenant_id, "f": first, "d": day, "ds": dealer_ids},
    ).mappings().all()
    return {(str(r["dealer_id"]), r["dump_kind"]): dict(r) for r in rows}


def dms_figures(rows: list[dict[str, Any]], outlets: list[dict[str, Any]]
                ) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """Per DMS location (anchor outlet id) and per unmapped location name: the dump
    rows' figures. ``rows``: dump_kind, outlet_label, outcome."""
    located: dict[str, dict[str, int]] = defaultdict(lambda: _zero(DMS_KEYS))
    unmapped: dict[str, dict[str, int]] = defaultdict(lambda: _zero(DMS_KEYS))
    cache: dict[str, str | None] = {}
    for r in rows:
        label = str(r.get("outlet_label") or "").strip()
        if label not in cache:
            cache[label] = resolve_location(label, outlets)
        anchor = cache[label]
        figures = located[anchor] if anchor else unmapped[label or "(no location)"]
        outcome = r["outcome"]
        if r["dump_kind"] == BOOKING:
            if outcome == CANCELLED:
                figures["bookingCancelled"] += 1
                continue
            figures["bookingDms"] += 1
            figures["bookingSynced"] += outcome == MATCHED
            figures["bookingNotInVerigence"] += outcome == MISSING
        else:
            figures["deliveryDump"] += 1
            figures["deliverySynced"] += outcome == MATCHED
            figures["deliveryNotUploaded"] += outcome == NOT_IN_TOOL
            figures["deliveryNotInVerigence"] += outcome == MISSING
    return dict(located), dict(unmapped)


_BOOKINGS_SQL = """
    WITH up AS (
        SELECT ub.journey_id, min(CAST(ub.created_at_utc AT TIME ZONE 'Asia/Kolkata' AS date)) AS day
        FROM auditcore.p2_upload_batches ub
        JOIN (SELECT DISTINCT q.batch_id FROM auditcore.p2_document_queue q
              WHERE q.tenant_id = :t AND q.business_stage = 'BOOKING' AND q.queue_status <> 'CANCELLED') qb
          ON qb.batch_id = ub.batch_id
        WHERE ub.tenant_id = :t AND ub.batch_status <> 'AWAITING_UPLOAD'
          AND NOT (ub.batch_status = 'CANCELLED' AND COALESCE(ub.page_count, 0) = 0)
        GROUP BY ub.journey_id
    )
    SELECT j.outlet_id, count(*) AS n,
           count(*) FILTER (WHERE b.booking_date IS NULL OR b.booking_date > CAST(:today AS date)) AS by_upload
    FROM auditcore.journeys j
    LEFT JOIN auditcore.bookings b ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
    LEFT JOIN up ON up.journey_id = j.journey_id
    WHERE j.tenant_id = :t AND CAST(j.outlet_id AS text) = ANY(:ids)
      AND COALESCE(CASE WHEN b.booking_date <= CAST(:today AS date) THEN b.booking_date END, up.day)
          BETWEEN :f AND :bd
    GROUP BY j.outlet_id
"""

# The delivery Journeys of vehicle_gate_pass_deliveries (the gate register's "uploaded in
# tool"), with their Deal Type: the vehicle gate pass on file, its newest delivery date.
_DELIVERIES_SQL = """
    WITH dated AS (
        SELECT DISTINCT ON (f.journey_id, f.di_document_id) f.journey_id, f.effective_value #>> '{}' AS v
        FROM auditcore.journey_document_extracted_fields f
        JOIN auditcore.evidence e
          ON e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id
         AND e.di_document_id = f.di_document_id AND e.association_status = 'ACTIVE'
        WHERE f.tenant_id = :t AND f.field_key = 'delivery_date'
          AND COALESCE(e.document_type_key, f.source_document_type_key) = 'gate_pass'
        ORDER BY f.journey_id, f.di_document_id, f.source_fact_version DESC NULLS LAST, f.updated_at_utc DESC
    )
    SELECT DISTINCT d.journey_id, j.outlet_id, CAST(d.v AS date) AS day, c.deal_type
    FROM dated d
    JOIN auditcore.journeys j ON j.tenant_id = :t AND j.journey_id = d.journey_id
    LEFT JOIN auditcore.journey_classifications c ON c.tenant_id = :t AND c.journey_id = d.journey_id
    WHERE d.v ~ '^\\d{4}-\\d{2}-\\d{2}$' AND CAST(j.outlet_id AS text) = ANY(:ids)
      AND CAST(d.v AS date) BETWEEN :a AND :dd
"""


def verigence_figures(connection: Connection, tenant_id: str, outlets: list[dict[str, Any]], first: date,
                      booking_to: date, delivery_to: date, today: date) -> dict[str, dict[str, int]]:
    """Per Verigence outlet: bookings from the 1st to ``booking_to``; deliveries in the tool
    (with Deal Type) and the gate register from the 1st to ``delivery_to``."""
    from audit_core.task_manager import gate_pass_covers, sunday_is_off
    from audit_core.uc03_p2_my_work import gate_register

    ids = [str(o["outlet_id"]) for o in outlets]
    out: dict[str, dict[str, int]] = {oid: _zero(VERIGENCE_KEYS) for oid in ids}
    if not ids:
        return out
    for r in connection.execute(text(_BOOKINGS_SQL),
                                {"t": tenant_id, "ids": ids, "f": first, "bd": booking_to, "today": today}).mappings():
        out[str(r["outlet_id"])]["bookingInVerigence"] = int(r["n"])
        # no booking form date read: dated by the day the booking documents were uploaded
        out[str(r["outlet_id"])]["bookingDatedByUpload"] = int(r["by_upload"])
    # A delivery on a Sunday that was off (before 15 Nov 2026) is Monday's register's; one after today has not happened
    # (a misread gate pass date), as vehicle_gate_pass_deliveries counts them.
    seen: set[str] = set()
    for r in connection.execute(
        text(_DELIVERIES_SQL),
        {"t": tenant_id, "ids": ids, "a": gate_pass_covers(first)[0], "dd": min(delivery_to, today)},
    ).mappings():
        shifted = r["day"] + timedelta(days=1) if sunday_is_off(r["day"]) else r["day"]
        jid = str(r["journey_id"])
        if not first <= shifted <= delivery_to or jid in seen:
            continue
        seen.add(jid)
        figures = out[str(r["outlet_id"])]
        figures["deliveryInVerigence"] += 1
        kind = str(r["deal_type"] or "")
        key = ("inscope" if kind == "INSCOPE" else "outscope" if kind == "OUTSCOPE"
               else "otherDealType" if kind in ("MR", "LEASE", "BULK") else "dealTypePending")
        figures[key] += 1
    # The gate register is due only where a PC is mapped (as the Project Tracker counts it).
    with_pc = {str(r[0]) for r in connection.execute(
        text("""
            SELECT DISTINCT outlet_id FROM auditcore.business_assignments
            WHERE tenant_id = :t AND assignment_status = 'ACTIVE' AND outlet_id IS NOT NULL
              AND upper(business_role_code) = 'PC'
              AND effective_from <= now() AND (effective_to IS NULL OR effective_to >= now())
        """),
        {"t": tenant_id},
    )}
    register = gate_register(connection, tenant_id, [o for o in outlets if str(o["outlet_id"]) in with_pc],
                             first, min(delivery_to, today))
    for oid, days in register.items():
        figures = out[oid]
        for d in days:
            if d["gatePassId"] is None:
                figures["registersMissed"] += d["status"] == "MISSED"
                continue
            if d["status"] == "COUNTS_PENDING":
                figures["countsPending"] += 1
                continue
            figures["gateRegister"] += int(d["total"] or 0)
            figures["notIntimated"] += int(d["notIntimated"] or 0)
            figures["pendingWithPc"] += int(d["pendingWithPc"] or 0)
    return out


def _add(into: dict[str, int], more: dict[str, int]) -> None:
    for key, value in more.items():
        into[key] = into.get(key, 0) + value


def _gaps(figures: dict[str, int]) -> dict[str, int]:
    """DMS less Verigence (what Verigence still lacks against the DMS), where both are known."""
    out = dict(figures)
    if "bookingDms" in figures:
        out["bookingGap"] = figures["bookingDms"] - figures.get("bookingInVerigence", 0)
        out["deliveryGap"] = figures["deliveryDump"] - figures.get("deliveryInVerigence", 0)
    return out


def build_outlet_report(connection: Connection, *, tenant_id: str, dealers: list[dict[str, Any]], day: date,
                        today: date) -> dict[str, Any]:
    first = day.replace(day=1)
    dealer_ids = [d["dealerId"] for d in dealers]
    dumps = _latest_dumps(connection, tenant_id, dealer_ids, first, day)
    dump_ids = [str(d["dump_id"]) for d in dumps.values()]
    rows_by_dealer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if dump_ids:
        for r in connection.execute(
            text("""
                SELECT dealer_id, dump_kind, outlet_label, outcome FROM auditcore.p2_dms_dump_rows
                WHERE tenant_id = :t AND CAST(dump_id AS text) = ANY(:ids)
            """),
            {"t": tenant_id, "ids": dump_ids},
        ).mappings():
            rows_by_dealer[str(r["dealer_id"])].append(dict(r))
    project = {**_zero(DMS_KEYS), **_zero(VERIGENCE_KEYS)}
    out_dealers = []
    for dealer in dealers:
        did = dealer["dealerId"]
        outlets = outlet_map(connection, tenant_id, did)
        by_id = {str(o["outlet_id"]): o for o in outlets}
        located, unmapped = dms_figures(rows_by_dealer.get(did, []), outlets)
        booking, delivery = dumps.get((did, BOOKING)), dumps.get((did, DELIVERY))
        # Like with like: Verigence is counted up to the last day the dealership's dump covers
        # (a dump of the 5th carries the 1st to the 4th), else up to the report's day.
        booking_to = max(first, booking["covers_to"]) if booking and booking["covers_to"] else day
        delivery_to = max(first, delivery["covers_to"]) if delivery and delivery["covers_to"] else day
        verigence = verigence_figures(connection, tenant_id, outlets, first, booking_to, delivery_to, today)
        members: dict[str, list[str]] = defaultdict(list)
        for o in outlets:
            anchor = str(o["dms_outlet_id"])
            members[anchor if anchor in by_id else str(o["outlet_id"])].append(str(o["outlet_id"]))
        locations = []
        total = {**_zero(DMS_KEYS), **_zero(VERIGENCE_KEYS)}
        for anchor in sorted(members, key=lambda a: short_name(by_id[a]["outlet_name"]).lower()):
            ids = sorted(members[anchor], key=lambda i: (i != anchor, short_name(by_id[i]["outlet_name"]).lower()))
            summed = _zero(VERIGENCE_KEYS)
            outlet_rows = []
            for oid in ids:
                _add(summed, verigence[oid])
                outlet_rows.append({"outletId": oid, "outletName": short_name(by_id[oid]["outlet_name"]),
                                    "figures": verigence[oid]})
            dms = located.get(anchor, _zero(DMS_KEYS))
            location = {"dmsOutletId": anchor, "name": short_name(by_id[anchor]["outlet_name"]),
                        "outlets": outlet_rows, "figures": _gaps({**dms, **summed})}
            locations.append(location)
            _add(total, dms)
            _add(total, summed)
        unmapped_rows = []
        for label, dms in sorted(unmapped.items()):
            unmapped_rows.append({"label": label, "figures": dms})
            _add(total, dms)
        out_dealers.append({
            "dealerId": did, "dealerName": dealer["dealerName"],
            "bookingDumpDate": booking["business_date"].isoformat() if booking else None,
            "deliveryDumpDate": delivery["business_date"].isoformat() if delivery else None,
            "bookingsTo": booking_to.isoformat(), "deliveriesTo": delivery_to.isoformat(),
            "locations": locations, "unmapped": unmapped_rows, "figures": _gaps(total),
        })
        _add(project, total)
    return {"date": day.isoformat(), "month": first.strftime("%Y-%m"), "today": today.isoformat(),
            "dealers": out_dealers, "figures": _gaps(project)}


# --------------------------------------------------------------------------- endpoints

def _day(value: str | None, today: date) -> date:
    if not value:
        return today
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Date is YYYY-MM-DD.") from exc
    if day > today:
        raise HTTPException(status_code=422, detail="The report's date cannot be in the future.")
    return day


@router.get("/dms-dumps/outlet-report")
def get_outlet_report(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    date: str | None = None,
) -> dict[str, Any]:
    """Outlet wise, month to date as at the day: DMS against Verigence and the gate register."""
    held = _access(connection, tenant_id, principal, client, _READ, READ_ROLES)
    today = _today(connection, tenant_id)
    dealers = _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)
    return build_outlet_report(connection, tenant_id=tenant_id, dealers=dealers, day=_day(date, today), today=today)


# (header, key, group) -- the Excel's columns, in the layout of the dealers' own daily sheet
EXCEL_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("As per DMS", "bookingDms", "Booking"),
    ("In Verigence", "bookingInVerigence", "Booking"),
    ("of which no booking form date", "bookingDatedByUpload", "Booking"),
    ("Synced with DMS", "bookingSynced", "Booking"),
    ("Not in Verigence", "bookingNotInVerigence", "Booking"),
    ("DMS less Verigence", "bookingGap", "Booking"),
    ("As per gate out register", "gateRegister", "Total delivery"),
    ("As per delivery dump", "deliveryDump", "Total delivery"),
    ("In Verigence", "deliveryInVerigence", "Total delivery"),
    ("Synced with dump", "deliverySynced", "Total delivery"),
    ("Pending with PC", "pendingWithPc", "Total delivery"),
    ("Not intimated (client)", "notIntimated", "Total delivery"),
    ("Delivery not uploaded", "deliveryNotUploaded", "Total delivery"),
    ("Not in Verigence", "deliveryNotInVerigence", "Total delivery"),
    ("Dump less Verigence", "deliveryGap", "Total delivery"),
    ("Inscope", "inscope", "Delivery bifurcation (Verigence)"),
    ("Outscope", "outscope", "Delivery bifurcation (Verigence)"),
    ("MR / Lease / Bulk", "otherDealType", "Delivery bifurcation (Verigence)"),
    ("Type pending", "dealTypePending", "Delivery bifurcation (Verigence)"),
    ("Registers missed", "registersMissed", "Gate register"),
    ("Counts pending", "countsPending", "Gate register"),
)


def build_outlet_report_excel(report: dict[str, Any]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Outlet report"
    dark, white = PatternFill("solid", fgColor="161616"), Font(bold=True, color="FFFFFF")
    groups = {"Booking": "0F62FE", "Total delivery": "8A3800", "Delivery bifurcation (Verigence)": "198038",
              "Gate register": "525252"}
    sub = PatternFill("solid", fgColor="E8DAFF")
    tot = PatternFill("solid", fgColor="D0E2FF")
    thin = Side(style="thin", color="C6C6C6")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    day = date.fromisoformat(report["date"])
    ws.append([f"As at {day:%d-%b-%y}", "(month to date)"] + [g for _, _, g in EXCEL_COLUMNS])
    ws.append(["Dealer", "Location"] + [h for h, _, _ in EXCEL_COLUMNS])
    for cell in ws[1] + ws[2]:
        cell.font, cell.fill, cell.border = white, dark, border
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    start = 3
    for i, (_, _, group) in enumerate(EXCEL_COLUMNS, start=start):
        ws.cell(row=1, column=i).fill = PatternFill("solid", fgColor=groups[group])
    # merge the group headers
    i = 0
    while i < len(EXCEL_COLUMNS):
        j = i
        while j + 1 < len(EXCEL_COLUMNS) and EXCEL_COLUMNS[j + 1][2] == EXCEL_COLUMNS[i][2]:
            j += 1
        if j > i:
            ws.merge_cells(start_row=1, start_column=start + i, end_row=1, end_column=start + j)
        i = j + 1

    def put(dealer: str, location: str, figures: dict[str, int], *, dms: bool, fill: Any = None,
            bold: bool = False) -> None:
        values = []
        for _, key, group in EXCEL_COLUMNS:
            is_dms = key in DMS_KEYS or key in ("bookingGap", "deliveryGap")
            values.append(figures.get(key) if (dms or not is_dms) and key in figures else None)
        ws.append([dealer, location] + values)
        for cell in ws[ws.max_row]:
            cell.border = border
            if fill is not None:
                cell.fill = fill
            if bold:
                cell.font = Font(bold=True)

    for dealer in report["dealers"]:
        name = dealer["dealerName"]
        for location in dealer["locations"]:
            if len(location["outlets"]) == 1:
                put(name, location["name"], {**location["figures"]}, dms=True)
                continue
            for outlet in location["outlets"]:
                put(name, outlet["outletName"], outlet["figures"], dms=False)
            put(name, f"{location['name']} - DMS location ({' + '.join(o['outletName'] for o in location['outlets'])})",
                location["figures"], dms=True, fill=PatternFill("solid", fgColor="F2F4F8"), bold=True)
        for row in dealer["unmapped"]:
            put(name, f"DMS location not mapped: {row['label']}", row["figures"], dms=True)
        dumps = (f"bookings to {dealer['bookingsTo']} (dump {dealer['bookingDumpDate'] or 'none'}), "
                 f"deliveries to {dealer['deliveriesTo']} (dump {dealer['deliveryDumpDate'] or 'none'})")
        put(f"{name} - Total", dumps, dealer["figures"], dms=True, fill=sub, bold=True)
    put("Project - Total", "", report["figures"], dms=True, fill=tot, bold=True)
    ws.freeze_panes = "C3"
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 34
    for col in range(start, start + len(EXCEL_COLUMNS)):
        ws.column_dimensions[get_column_letter(col)].width = 12
    ws.row_dimensions[2].height = 45
    notes = wb.create_sheet("How it is counted")
    for line in (
        ("Month to date. DMS figures come from each dealership's latest dump dated in the month up to the report's date; the "
         "Verigence and gate register figures are counted up to the last day that dump covers, so both sides are the same days."),
        ("'Of which no booking form date': bookings dated by the day the booking documents were uploaded, as the booking date "
         "rule allows; an old booking first uploaded at delivery can land in the month this way."),
        ("A dump row is placed by its location name: Utkal's ANANDAPUR is reported by the DMS under KEONJHAR; Aditya's outlets "
         "other than JAJPUR are all under CUTTACK. Where several outlets share a DMS location, each outlet shows its Verigence "
         "figures and the location's row compares the DMS with their total."),
        "Bookings in Verigence: journeys whose booking date (booking form, else the day the booking documents were uploaded) is in the period.",
        "Deliveries in Verigence: delivery journeys with the vehicle gate pass dated in the period (the gate register's 'in tool').",
        "Gate register: the PC's daily register. Pending with PC = intimated but not in Verigence; not intimated = file not handed by the dealership.",
        "Synced = a DMS row found in Verigence. Not in Verigence = no journey at all. Delivery not uploaded = journey found, delivery file not uploaded.",
        "Cancelled bookings in the DMS are left out of 'As per DMS'.",
    ):
        notes.append([line])
    notes.column_dimensions["A"].width = 160
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


@router.get("/dms-dumps/outlet-report/file")
def export_outlet_report(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    date: str | None = None,
) -> StreamingResponse:
    held = _access(connection, tenant_id, principal, client, _READ, READ_ROLES)
    today = _today(connection, tenant_id)
    day = _day(date, today)
    dealers = _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)
    report = build_outlet_report(connection, tenant_id=tenant_id, dealers=dealers, day=day, today=today)
    name = f"DMS-Outlet-Report_{day.isoformat()}.xlsx"
    return StreamingResponse(io.BytesIO(build_outlet_report_excel(report)), media_type=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/dms-dumps/outlet-map")
def get_outlet_map(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Each dealership's outlets with their DMS location and location names, and the
    location names this month's dumps used with where each landed."""
    held = _access(connection, tenant_id, principal, client, _READ, READ_ROLES)
    admin = "ADMIN" in held
    today = _today(connection, tenant_id)
    dealers = _dealers(connection, tenant_id, principal.subject, admin)
    seen: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in connection.execute(
        text("""
            SELECT r.dealer_id, COALESCE(r.outlet_label, '') AS label, count(*) AS n
            FROM auditcore.p2_dms_dump_rows r
            JOIN auditcore.p2_dms_dumps u ON u.tenant_id = r.tenant_id AND u.dump_id = r.dump_id
            WHERE r.tenant_id = :t AND u.status = 'ACTIVE' AND u.business_date >= :f
            GROUP BY 1, 2
        """),
        {"t": tenant_id, "f": today.replace(day=1) - timedelta(days=31)},
    ).mappings():
        seen[str(r["dealer_id"])][r["label"]] += int(r["n"])
    out = []
    for dealer in dealers:
        outlets = outlet_map(connection, tenant_id, dealer["dealerId"])
        by_id = {str(o["outlet_id"]): o for o in outlets}
        labels = []
        for label, n in sorted(seen.get(dealer["dealerId"], {}).items()):
            outlet = resolve_outlet(label, outlets)
            location = resolve_location(label, outlets)
            labels.append({"label": label or "(no location)", "rows": n,
                           "outletName": short_name(by_id[outlet]["outlet_name"]) if outlet else None,
                           "dmsLocation": short_name(by_id[location]["outlet_name"]) if location else None})
        out.append({
            "dealerId": dealer["dealerId"], "dealerName": dealer["dealerName"],
            "outlets": [{"outletId": str(o["outlet_id"]), "outletName": short_name(o["outlet_name"]),
                         "dmsOutletId": str(o["dms_outlet_id"]), "labels": list(o["dms_labels"] or []),
                         "catchAll": bool(o["catch_all"])} for o in outlets],
            "labelsSeen": labels,
        })
    return {"dealers": out, "canEdit": admin or bool(held & set(MAP_ROLES))}


@router.put("/dms-dumps/outlet-map/{outlet_id}")
def put_outlet_map(
    tenant_id: str,
    outlet_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Set an outlet's DMS location (an outlet of the same dealership that is its own
    location), its location names and whether unnamed locations fall to it."""
    held = _access(connection, tenant_id, principal, client, _UPDATE, MAP_ROLES)
    admin = "ADMIN" in held
    outlet = connection.execute(
        text("SELECT outlet_id, dealer_id FROM auditcore.dealer_outlets WHERE tenant_id = :t AND outlet_id = :o"),
        {"t": tenant_id, "o": str(outlet_id)},
    ).mappings().one_or_none()
    if outlet is None:
        raise HTTPException(status_code=404, detail="No such outlet.")
    dealer_id = str(outlet["dealer_id"])
    if dealer_id not in {d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject, admin)}:
        raise HTTPException(status_code=403, detail="That dealership is not one you cover.")
    outlets = {str(o["outlet_id"]): o for o in outlet_map(connection, tenant_id, dealer_id)}
    under = str(body.get("dmsOutletId") or outlet_id)
    if under not in outlets:
        raise HTTPException(status_code=422, detail="The DMS location must be an outlet of the same dealership.")
    if under != str(outlet_id):
        if str(outlets[under]["dms_outlet_id"]) != under:
            raise HTTPException(status_code=422, detail="That outlet is itself reported under another DMS location.")
        if any(str(o["dms_outlet_id"]) == str(outlet_id) and oid != str(outlet_id) for oid, o in outlets.items()):
            raise HTTPException(status_code=422, detail=(
                "Other outlets are reported under this one; move them first."))
    raw = body.get("labels") or []
    if not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="Location names are a list.")
    labels = sorted({re.sub(r"\s+", " ", str(x)).strip()[:120] for x in raw if str(x or "").strip()})[:30]
    catch_all = bool(body.get("catchAll"))
    taken = {label_key(x): short_name(o["outlet_name"]) for oid, o in outlets.items() if oid != str(outlet_id)
             for x in (o["dms_labels"] or [])}
    clash = [x for x in labels if label_key(x) in taken]
    if clash:
        raise HTTPException(status_code=422, detail=(
            f"'{clash[0]}' is already a location name of {taken[label_key(clash[0])]}."))
    if catch_all:
        connection.execute(
            text("UPDATE auditcore.p2_dms_outlet_map SET catch_all = false, updated_at_utc = now(), updated_by = :by "
                 "WHERE tenant_id = :t AND dealer_id = :d AND outlet_id <> :o AND catch_all"),
            {"t": tenant_id, "d": dealer_id, "o": str(outlet_id), "by": principal.subject},
        )
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_dms_outlet_map (tenant_id, outlet_id, dealer_id, dms_outlet_id, dms_labels,
                                                     catch_all, updated_by)
            VALUES (:t, :o, :d, :u, CAST(:l AS text[]), :c, :by)
            ON CONFLICT (tenant_id, outlet_id) DO UPDATE
              SET dms_outlet_id = EXCLUDED.dms_outlet_id, dms_labels = EXCLUDED.dms_labels,
                  catch_all = EXCLUDED.catch_all, updated_by = EXCLUDED.updated_by, updated_at_utc = now()
        """),
        {"t": tenant_id, "o": str(outlet_id), "d": dealer_id, "u": under, "l": labels, "c": catch_all,
         "by": principal.subject},
    )
    return {"outletId": str(outlet_id), "dmsOutletId": under, "labels": labels, "catchAll": catch_all}


__all__ = ["build_outlet_report", "build_outlet_report_excel", "dms_figures", "label_key", "resolve_location",
           "resolve_outlet", "router", "short_name"]
