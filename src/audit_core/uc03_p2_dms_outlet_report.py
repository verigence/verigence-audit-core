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
               else up to the report's day when no dump is on file. The DMS is the source
               of truth for dates (2026-11-19): a Journey any of the dealership's dumps
               found is counted on the DMS's date; only a Journey no dump found is
               counted on the date below:
               bookings       Journeys whose booking date (the booking form's, else the
                              day the PC uploaded the booking documents -- the booking
                              date rule of 2026-11-07) falls in the period; of those,
                              the ones dated by the upload day are shown apart
               deliveries     delivery Journeys whose delivery day falls in the period
                              (uc03_p2_delivery_dates, 2026-11-17): the gate pass's date,
                              else the NDC's printed delivery date, else the latest payment
                              receipt -- those dated without a gate pass shown apart; of
                              those not found by the delivery dump ("in Verigence, not in
                              dump"), and the Deal Type: Inscope / Outscope / MR, Lease,
                              Bulk / not yet typed
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
from types import SimpleNamespace
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

# The DMS is the source of truth for dates (product owner 2026-11-19: "you have to stick to the
# DMS date.. check DMS Dump date and compare the data accordingly.. DMS will always be source of
# truth.. no matter"). A Journey a dump found is dated by the DMS, whatever date Verigence reads
# from the papers; only a Journey no dump found is dated by Verigence. So the figures add up:
#   As per DMS     = synced + (delivery not uploaded) + not in Verigence
#   In Verigence   = synced + in Verigence, not in the DMS + not compared (no dump that day)
DMS_KEYS = ("bookingDms", "bookingSynced", "bookingNotInVerigence", "bookingCancelled",
            "bookingIntimated", "bookingNotIntimated", "deliveryDump", "deliverySynced",
            "deliveryNotUploaded", "deliveryNotInVerigence",
            "dumpInscope", "dumpOutscope", "dumpOtherDealType", "dumpDealTypePending",
            "deliveryIntimated", "deliveryNotIntimated")
VERIGENCE_KEYS = ("bookingInVerigence", "bookingDatedByUpload", "bookingNotInDms", "bookingNotCompared",
                  "deliveryInVerigence", "deliveryDatedWithoutGatePass", "deliveryNotInDms", "deliveryNotCompared", "inscope", "outscope", "otherDealType",
                  "dealTypePending", "gateRegister", "registersMissed", "countsPending")


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
    """(dealer, kind) -> the current dump uploaded on the day. An earlier upload is not taken
    in its place (product owner 2026-11-18: "it shouldnt take into consideration the previous
    DMS upload like of 5th Nov"); a dealership with none that day has no DMS figures."""
    rows = connection.execute(
        text("""
            SELECT DISTINCT ON (u.dealer_id, u.dump_kind) u.dealer_id, u.dump_kind, u.dump_id, u.business_date,
                   u.period_month
            FROM auditcore.p2_dms_dumps u
            WHERE u.tenant_id = :t AND u.status = 'ACTIVE' AND u.business_date = :d
              AND CAST(u.dealer_id AS text) = ANY(:ds)
            ORDER BY u.dealer_id, u.dump_kind, u.business_date DESC, u.uploaded_at_utc DESC
        """),
        {"t": tenant_id, "f": first, "d": day, "ds": dealer_ids},
    ).mappings().all()
    return {(str(r["dealer_id"]), r["dump_kind"]): dict(r) for r in rows}


def dms_figures(rows: list[dict[str, Any]], outlets: list[dict[str, Any]],
                delivered: set[str] | None = None
                ) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """Per DMS location (anchor outlet id) and per unmapped location name: the dump
    rows' figures. ``rows``: dump_kind, outlet_label, outcome, journey_id, intimated (a PC's mark
    on a row Verigence does not hold: uc03_p2_dms_intimations, 2026-11-18). A row found in
    Verigence is synced: the DMS dates it (2026-11-19). ``delivered``: the delivery Journeys
    Verigence counts -- a row whose delivery file came after the dump was matched is synced too."""
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
            figures["bookingSynced" if outcome == MATCHED else "bookingNotInVerigence"] += 1
            if outcome != MATCHED:
                figures["bookingIntimated" if r.get("intimated") else "bookingNotIntimated"] += 1
        else:
            figures["deliveryDump"] += 1
            if outcome == MATCHED or (delivered is not None and r.get("journey_id") in delivered):
                figures["deliverySynced"] += 1
            elif outcome == NOT_IN_TOOL:
                figures["deliveryNotUploaded"] += 1
            else:
                figures["deliveryNotInVerigence"] += 1
            # The Deal Type rule on the dump's own rows (2026-11-19): uc03_p2_dms_dumps.DeliveryScope.
            scope = str(r.get("deal_type") or "")
            figures["dumpInscope" if scope == "INSCOPE" else "dumpOutscope" if scope == "OUTSCOPE"
                    else "dumpOtherDealType" if scope in ("MR", "LEASE", "BULK") else "dumpDealTypePending"] += 1
            if outcome != MATCHED:
                figures["deliveryIntimated" if r.get("intimated") else "deliveryNotIntimated"] += 1
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
    SELECT CAST(j.journey_id AS text) AS journey_id, CAST(j.outlet_id AS text) AS outlet_id,
           COALESCE(CASE WHEN b.booking_date <= CAST(:today AS date) THEN b.booking_date END, up.day) AS day,
           (b.booking_date IS NULL OR b.booking_date > CAST(:today AS date)) AS by_upload
    FROM auditcore.journeys j
    LEFT JOIN auditcore.bookings b ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
    LEFT JOIN up ON up.journey_id = j.journey_id
    WHERE j.tenant_id = :t AND CAST(j.outlet_id AS text) = ANY(:ids)
"""

def verigence_figures(connection: Connection, tenant_id: str, outlets: list[dict[str, Any]], first: date,
                      booking_to: date, delivery_to: date, today: date,
                      dump_journeys: set[str] | None = None,
                      missed: dict[str, list[date]] | None = None,
                      booking_dump_journeys: set[str] | None = None,
                      seen: dict[str, Any] | None = None,
                      dms_days: dict[str, dict[str, date]] | None = None,
                      matched_deliveries: set[str] | None = None) -> dict[str, dict[str, int]]:
    """Per Verigence outlet: bookings from the 1st to ``booking_to``; deliveries in the tool
    (with Deal Type) and the gate register from the 1st to ``delivery_to``. ``dump_journeys``:
    the Journeys the dealership's delivery dump found (None: no dump on file). ``missed``, when
    given, gets each outlet's days with no gate register. ``booking_dump_journeys``: the Journeys
    the booking dump found (None: no dump). ``seen``, when given, gets the Journeys counted
    ("booked", "delivered").

    ``dms_days`` (uc03_p2_dms_dumps.dms_dates): the date the DMS gives a Journey, by kind. It
    decides over Verigence's own date (product owner 2026-11-19: the DMS is the source of truth);
    a Journey no dump found keeps Verigence's date. ``matched_deliveries``: the delivery Journeys
    the day's dump found with their delivery file on -- counted on the DMS's date even when no
    delivery date could be read from the papers.

    What was intimated to the PC is no longer asked at the gate register (product owner
    2026-11-18): it comes from the PCs' marks on the dumps (dms_figures)."""
    from audit_core.uc03_p2_delivery_dates import GATE_PASS, deliveries, in_period
    from audit_core.uc03_p2_my_work import gate_register

    ids = [str(o["outlet_id"]) for o in outlets]
    out: dict[str, dict[str, int]] = {oid: _zero(VERIGENCE_KEYS) for oid in ids}
    if not ids:
        return out
    booked: set[str] = set()
    dms_booking = (dms_days or {}).get(BOOKING, {})
    for r in connection.execute(text(_BOOKINGS_SQL), {"t": tenant_id, "ids": ids, "today": today}).mappings():
        dms_day = dms_booking.get(r["journey_id"])
        day = dms_day or r["day"]  # the DMS's date first (2026-11-19)
        if day is None or not first <= day <= booking_to:
            continue
        booked.add(r["journey_id"])
        figures = out[r["outlet_id"]]
        figures["bookingInVerigence"] += 1
        # Not in a dump and no booking form date read: dated by the day the booking documents
        # were uploaded.
        figures["bookingDatedByUpload"] += dms_day is None and bool(r["by_upload"])
        if booking_dump_journeys is not None and r["journey_id"] not in booking_dump_journeys:
            figures["bookingNotInDms"] += 1
    # The delivery day (product owner 2026-11-17): the gate pass's, else the NDC's printed
    # delivery date, else the latest payment receipt -- delivery files on file only.
    every = deliveries(connection, tenant_id=tenant_id, today=today, outlet_ids=ids)
    counted: dict[str, tuple[Any, Any]] = dict(in_period(every, first, min(delivery_to, today),
                                                         (dms_days or {}).get(DELIVERY)))
    # Found by the day's dump with the delivery file on, but no delivery date read from the
    # papers: still a delivery in Verigence, on the DMS's date.
    unread = sorted((matched_deliveries or set()) - set(every))
    if unread:
        for r in connection.execute(
            text("""
                SELECT CAST(j.journey_id AS text) AS journey_id, CAST(j.outlet_id AS text) AS outlet_id, cl.deal_type
                FROM auditcore.journeys j
                LEFT JOIN auditcore.journey_classifications cl
                  ON cl.tenant_id = j.tenant_id AND cl.journey_id = j.journey_id
                WHERE j.tenant_id = :t AND CAST(j.journey_id AS text) = ANY(:j) AND CAST(j.outlet_id AS text) = ANY(:ids)
            """),
            {"t": tenant_id, "j": unread, "ids": ids},
        ).mappings():
            counted[r["journey_id"]] = (SimpleNamespace(outlet_id=r["outlet_id"], source=None,
                                                        deal_type=r["deal_type"]), None)
    if seen is not None:
        seen.update(booked=booked, delivered=set(counted))
    for jid, (delivery, _day) in counted.items():
        figures = out.get(delivery.outlet_id or "")
        if figures is None:
            continue
        figures["deliveryInVerigence"] += 1
        figures["deliveryDatedWithoutGatePass"] += delivery.source != GATE_PASS
        if dump_journeys is not None and jid not in dump_journeys:
            figures["deliveryNotInDms"] += 1
        kind = str(delivery.deal_type or "")
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
                if d["status"] == "MISSED":
                    figures["registersMissed"] += 1
                    if missed is not None:
                        missed.setdefault(oid, []).append(date.fromisoformat(d["date"]))
                continue
            if d["status"] == "COUNTS_PENDING":
                figures["countsPending"] += 1
                continue
            figures["gateRegister"] += int(d["total"] or 0)
    return out


def _add(into: dict[str, int], more: dict[str, int]) -> None:
    for key, value in more.items():
        into[key] = into.get(key, 0) + value


def _gaps(figures: dict[str, int], booking: bool = True, delivery: bool = True) -> dict[str, int]:
    """DMS less Verigence (what Verigence still lacks against the DMS), only for a dump on file:
    a dealership with no dump of a kind that day has no gap of that kind (2026-11-19: its
    Verigence figures had been taken from the other dealerships' gap)."""
    out = dict(figures)
    if "bookingDms" in figures:
        out["bookingGap"] = figures["bookingDms"] - figures.get("bookingInVerigence", 0) if booking else None
        out["deliveryGap"] = figures["deliveryDump"] - figures.get("deliveryInVerigence", 0) if delivery else None
    return out


# --------------------------------------------------------------------------- the statement

def _pcs_by_outlet(connection: Connection, tenant_id: str, outlet_ids: list[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for r in connection.execute(
        text("""
            SELECT CAST(outlet_id AS text) AS outlet_id, security_actor_id FROM auditcore.business_assignments
            WHERE tenant_id = :t AND assignment_status = 'ACTIVE' AND CAST(outlet_id AS text) = ANY(:ids)
              AND upper(business_role_code) = 'PC'
              AND effective_from <= now() AND (effective_to IS NULL OR effective_to >= now())
        """),
        {"t": tenant_id, "ids": outlet_ids},
    ).mappings():
        out[r["outlet_id"]].add(str(r["security_actor_id"]))
    return out


def _day_status(connection: Connection, actors: set[str], first: date, last: date) -> dict[tuple[str, date], str]:
    """(person, day) -> PRESENT / LEAVE, from myHR (auditcore.hr_pc_day_status)."""
    if not actors or last < first:
        return {}
    out: dict[tuple[str, date], str] = {}
    for r in connection.execute(
        text("SELECT security_user_id, day, status FROM auditcore.hr_pc_day_status "
             "WHERE security_user_id = ANY(:a) AND day BETWEEN :f AND :l"),
        {"a": sorted(actors), "f": first, "l": last},
    ).mappings():
        key = (str(r["security_user_id"]), r["day"])
        if out.get(key) != "PRESENT":  # a check-in outweighs a leave the same day
            out[key] = r["status"]
    return out


def dealer_statement(connection: Connection, tenant_id: str, outlets: list[dict[str, Any]],
                     missed: dict[str, list[date]], first: date, to: date, today: date) -> dict[str, Any]:
    """The dealership's simple statement (product owner 2026-11-18): per outlet, the days
    its gate register is missing -- onsite and satellite apart -- and the outlets whose PC
    was not available (no PC mapped; or no PC of the outlet checked in on a register day,
    from myHR attendance and approved leave; today is not judged)."""
    from audit_core.uc03_p2_my_work import gate_register_days

    by_id = {str(o["outlet_id"]): o for o in outlets}
    pcs = _pcs_by_outlet(connection, tenant_id, list(by_id))
    days = [d for d in gate_register_days(first, to) if d < today]
    status = _day_status(connection, set().union(*pcs.values()) if pcs else set(),
                         days[0] if days else first, days[-1] if days else first)
    absent: dict[str, list[tuple[date, bool]]] = {}
    for oid, people in pcs.items():
        for d in days:
            states = [status.get((p, d)) for p in people]
            if "PRESENT" not in states:
                absent.setdefault(oid, []).append((d, "LEAVE" in states))
    register: dict[str, list[dict[str, Any]]] = {"ONSITE": [], "SATELLITE": []}
    for oid in sorted(missed, key=lambda i: short_name(by_id[i]["outlet_name"]).lower()):
        dates = sorted(missed[oid])
        if not dates:
            continue
        away = {d for d, _ in absent.get(oid, [])}
        cls = "SATELLITE" if by_id[oid].get("outlet_classification") == "SATELLITE" else "ONSITE"
        register[cls].append({"outletId": oid, "outlet": short_name(by_id[oid]["outlet_name"]), "days": len(dates),
                              "dates": [d.isoformat() for d in dates],
                              "pcAbsentDays": sum(d in away for d in dates)})
    unavailable = []
    for oid in sorted(by_id, key=lambda i: short_name(by_id[i]["outlet_name"]).lower()):
        name = short_name(by_id[oid]["outlet_name"])
        cls = by_id[oid].get("outlet_classification") or "ONSITE"
        if oid not in pcs:
            unavailable.append({"outletId": oid, "outlet": name, "classification": cls, "noPcMapped": True,
                                "days": 0, "dates": [], "leaveDays": 0})
        elif absent.get(oid):
            unavailable.append({"outletId": oid, "outlet": name, "classification": cls, "noPcMapped": False,
                                "days": len(absent[oid]), "dates": [d.isoformat() for d, _ in absent[oid]],
                                "leaveDays": sum(leave for _, leave in absent[oid])})
    return {"from": first.isoformat(), "to": to.isoformat(), "registerMissing": register,
            "pcNotAvailable": unavailable}


def _dates_text(dates: list[str]) -> str:
    """['2026-11-04', '2026-11-09'] -> '4, 9 Nov'."""
    by_month: dict[str, list[str]] = defaultdict(list)
    for value in dates:
        d = date.fromisoformat(value)
        by_month[f"{d:%b}"].append(str(d.day))
    return "; ".join(f"{', '.join(days)} {month}" for month, days in by_month.items())


def statement_lines(dealers: list[dict[str, Any]]) -> list[str]:
    """The statement as lines, an asterisk to each."""
    lines: list[str] = []
    for dealer in dealers:
        st = dealer.get("statement")
        if not st:
            continue
        lines.append(dealer["dealerName"])
        for cls, label in (("ONSITE", "Onsite"), ("SATELLITE", "Satellite")):
            items = st["registerMissing"][cls]
            text_ = "; ".join(f"{i['outlet']} {i['days']} day{'s' if i['days'] != 1 else ''} "
                              f"({_dates_text(i['dates'])})"
                              + (f", PC absent on {i['pcAbsentDays']}" if i["pcAbsentDays"] else "")
                              for i in items) or "none"
            lines.append(f"* {label} - gate register missing: {text_}")
        items = st["pcNotAvailable"]
        text_ = "; ".join(
            f"{i['outlet']} (no PC mapped)" if i["noPcMapped"] else
            f"{i['outlet']} {i['days']} day{'s' if i['days'] != 1 else ''} ({_dates_text(i['dates'])})"
            + (f", {i['leaveDays']} on leave" if i["leaveDays"] else "")
            for i in items) or "none"
        lines.append(f"* PC not available (outlets impacted): {text_}")
        for kind in st.get("noDump") or []:
            lines.append(f"* No {kind.lower()} dump uploaded for the day: its {kind.lower()}s in Verigence are not "
                         "compared")
        f = st.get("figures") or {}
        if "bookingDms" in f and BOOKING not in (st.get("noDump") or []):
            lines.append(
                f"* Bookings: DMS {f['bookingDms']} = synced {f['bookingSynced']} + not in Verigence "
                f"{f['bookingNotInVerigence']}; Verigence {f['bookingInVerigence']} = synced {f['bookingSynced']} + "
                f"not in the DMS {f['bookingNotInDms']}")
        if "deliveryDump" in f and DELIVERY not in (st.get("noDump") or []):
            lines.append(
                f"* Deliveries: dump {f['deliveryDump']} = synced {f['deliverySynced']} + delivery not uploaded "
                f"{f['deliveryNotUploaded']} + not in Verigence {f['deliveryNotInVerigence']}; Verigence "
                f"{f['deliveryInVerigence']} = synced {f['deliverySynced']} + not in the dump {f['deliveryNotInDms']}")
    return lines


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
                SELECT r.dealer_id, r.dump_kind, r.outlet_label, r.outcome, CAST(r.journey_id AS text) AS journey_id,
                       r.deal_type, r.dms_booking_number, r.booking_number, r.chassis, r.invoice_number,
                       r.customer_name, r.mobile, r.event_date, j.journey_reference
                FROM auditcore.p2_dms_dump_rows r
                LEFT JOIN auditcore.journeys j ON j.tenant_id = r.tenant_id AND j.journey_id = r.journey_id
                WHERE r.tenant_id = :t AND CAST(r.dump_id AS text) = ANY(:ids)
                  AND r.outcome NOT IN ('OUT_OF_PERIOD', 'EXCLUDED', 'DUPLICATE')
            """),
            {"t": tenant_id, "ids": dump_ids},
        ).mappings():
            rows_by_dealer[str(r["dealer_id"])].append(dict(r))
    from audit_core.uc03_p2_dms_intimations import live_marks, row_key

    marks = live_marks(connection, tenant_id, dealer_ids) if dump_ids else {}
    for did, rows in rows_by_dealer.items():
        for r in rows:
            r["intimated"] = (did, r["dump_kind"], row_key(r["dump_kind"], r)) in marks
    project = {**_zero(DMS_KEYS), **_zero(VERIGENCE_KEYS)}
    project_gaps = {"bookingGap": 0, "deliveryGap": 0}
    out_dealers = []
    for dealer in dealers:
        did = dealer["dealerId"]
        outlets = outlet_map(connection, tenant_id, did)
        by_id = {str(o["outlet_id"]): o for o in outlets}
        booking, delivery = dumps.get((did, BOOKING)), dumps.get((did, DELIVERY))
        # Like with like: Verigence is counted up to the last day the dealership's dump covers
        # (a dump of the 5th carries the 1st to the 4th), else up to the report's day.
        # The days each dump is compared for: the 1st to its upload day (2026-11-18).
        from audit_core.uc03_p2_dms_dumps import dump_window

        booking_to = max(first, dump_window(booking["period_month"], booking["business_date"])[1]) if booking else day
        delivery_to = (max(first, dump_window(delivery["period_month"], delivery["business_date"])[1])
                       if delivery else day)
        dealer_rows = rows_by_dealer.get(did, [])
        dump_journeys = ({r["journey_id"] for r in dealer_rows
                          if r["dump_kind"] == DELIVERY and r["journey_id"]} if delivery else None)
        booking_journeys = ({r["journey_id"] for r in dealer_rows
                             if r["dump_kind"] == BOOKING and r["journey_id"]} if booking else None)
        missed: dict[str, list[date]] = {}
        seen: dict[str, Any] = {}
        # The DMS's dates decide (2026-11-19): the dealership's dumps up to the day.
        from audit_core.uc03_p2_dms_dumps import dms_dates

        dated = dms_dates(connection, tenant_id, did, day)
        matched = ({r["journey_id"] for r in dealer_rows
                    if r["dump_kind"] == DELIVERY and r["journey_id"] and r["outcome"] == MATCHED}
                   if delivery else None)
        verigence = verigence_figures(connection, tenant_id, outlets, first, booking_to, delivery_to, today,
                                      dump_journeys, missed, booking_journeys, seen, dated, matched)
        for figures in verigence.values():  # no dump that day: Verigence's side is not compared
            if booking is None:
                figures["bookingNotCompared"] = figures["bookingInVerigence"]
            if delivery is None:
                figures["deliveryNotCompared"] = figures["deliveryInVerigence"]
        located, unmapped = dms_figures(dealer_rows, outlets, seen["delivered"])
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
                        "outlets": outlet_rows,
                        "figures": _gaps({**dms, **summed}, booking is not None, delivery is not None)}
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
            "locations": locations, "unmapped": unmapped_rows,
            "figures": (dealer_figures := _gaps(total, booking is not None, delivery is not None)),
            "statement": {**dealer_statement(connection, tenant_id, outlets, missed, first, min(delivery_to, today),
                                             today),
                          "noDump": [k for k, d in ((BOOKING, booking), (DELIVERY, delivery)) if d is None],
                          "figures": dealer_figures},
        })
        _add(project, total)
        for key in project_gaps:
            project_gaps[key] += dealer_figures.get(key) or 0
    return {"date": day.isoformat(), "month": first.strftime("%Y-%m"), "today": today.isoformat(),
            "dealers": out_dealers, "figures": {**project, **project_gaps},
            "statementLines": statement_lines(out_dealers)}


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
    ("of which dated by upload day (not in DMS, no booking form date)", "bookingDatedByUpload", "Booking"),
    ("Synced with DMS", "bookingSynced", "Booking"),
    ("Not in Verigence", "bookingNotInVerigence", "Booking"),
    ("In Verigence, not in DMS", "bookingNotInDms", "Booking"),
    ("Not compared (no dump)", "bookingNotCompared", "Booking"),
    ("Intimated to PC, not in Verigence", "bookingIntimated", "Booking"),
    ("Not intimated to PC", "bookingNotIntimated", "Booking"),
    ("DMS less Verigence", "bookingGap", "Booking"),
    ("As per gate out register", "gateRegister", "Total delivery"),
    ("As per delivery dump", "deliveryDump", "Total delivery"),
    ("In Verigence", "deliveryInVerigence", "Total delivery"),
    ("of which dated without gate pass", "deliveryDatedWithoutGatePass", "Total delivery"),
    ("Synced with dump", "deliverySynced", "Total delivery"),
    ("Intimated to PC, not uploaded", "deliveryIntimated", "Total delivery"),
    ("Not intimated to PC", "deliveryNotIntimated", "Total delivery"),
    ("Delivery not uploaded", "deliveryNotUploaded", "Total delivery"),
    ("Not in Verigence", "deliveryNotInVerigence", "Total delivery"),
    ("In Verigence, not in dump", "deliveryNotInDms", "Total delivery"),
    ("Not compared (no dump)", "deliveryNotCompared", "Total delivery"),
    ("Dump less Verigence", "deliveryGap", "Total delivery"),
    ("Inscope", "dumpInscope", "Delivery bifurcation (dump)"),
    ("Outscope", "dumpOutscope", "Delivery bifurcation (dump)"),
    ("MR / Lease / Bulk", "dumpOtherDealType", "Delivery bifurcation (dump)"),
    ("Type pending", "dumpDealTypePending", "Delivery bifurcation (dump)"),
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
    groups = {"Booking": "0F62FE", "Total delivery": "8A3800", "Delivery bifurcation (dump)": "005D5D",
              "Delivery bifurcation (Verigence)": "198038",
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
    if report.get("statementLines"):
        st = wb.create_sheet("Statement")
        st.append([f"Statement, {day:%d-%b-%y} (month to date)"])
        st["A1"].font = Font(bold=True)
        for line in report["statementLines"]:
            st.append([line])
            if not line.startswith("*"):
                st.cell(row=st.max_row, column=1).font = Font(bold=True)
        st.column_dimensions["A"].width = 160
    notes = wb.create_sheet("How it is counted")
    for line in (
        ("The DMS is the source of truth for dates. A booking or delivery the DMS has is counted on the DMS's date, "
         "whatever date Verigence reads from the papers. Only a booking or delivery the DMS does not have is counted "
         "on Verigence's own date."),
        ("Month to date. DMS figures come from each dealership's dump uploaded on the report's date; Verigence and "
         "gate register figures are counted up to the last day that dump covers, so both sides are the same days."),
        ("'Of which dated by upload day': bookings the DMS does not have and with no booking form date read, so "
         "Verigence dates them by the day the booking documents were uploaded."),
        ("A dump row is placed by its location name: Utkal's ANANDAPUR is reported by the DMS under KEONJHAR; Aditya's outlets "
         "other than JAJPUR are all under CUTTACK. Where several outlets share a DMS location, each outlet shows its Verigence "
         "figures and the location's row compares the DMS with their total."),
        ("In Verigence: bookings and deliveries the DMS dates in the period, and those the DMS does not have that "
         "Verigence dates in the period (booking form date, else the upload day; gate pass, else NDC, else receipt)."),
        ("Intimated to PC: a dump row Verigence does not hold that the PC marked as intimated to them (Booking / Delivery "
         "Intimation tab) -- pending with the PC. Not intimated to PC: no PC marked it -- pending with the dealership. "
         "A row in Verigence needs no mark."),
        "Gate register: the PC's daily register, its count of deliveries; registers missed are listed on the Statement sheet.",
        ("Synced = a DMS row found in Verigence. Not in Verigence = no journey at all. Delivery not uploaded = journey "
         "found, delivery file not uploaded. In Verigence, not in DMS = in Verigence for these days, not in the dump."),
        ("The figures add up: As per DMS = synced + delivery not uploaded + not in Verigence; In Verigence = synced + "
         "not in DMS + not compared. One journey is matched to one DMS row only; a line repeating an earlier line's "
         "chassis / OTF is a duplicate and is left out."),
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


__all__ = ["build_outlet_report", "build_outlet_report_excel", "dealer_statement", "dms_figures", "label_key",
           "resolve_location", "resolve_outlet", "router", "short_name", "statement_lines"]
