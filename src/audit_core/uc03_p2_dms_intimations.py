"""uc03_p2_dms_intimations.py -- the PCs say which DMS bookings and deliveries were
intimated to them (product owner 2026-11-18).

"we ask the PC during gate pass register upload, how many booking are intimated to them..
so we should discontinue asking those queries, and build the entire process full
deterministic.. Reviewer or any will upload the booking dump .. Once the booking dump is
uploaded, message should pop on every PC laptop/mobile that Booking dump has been uploaded,
pls update your booking intimations. Lets create another tab which says Booking
Intimation ... search DMS booking dump on the basis of the customer name, mobile, OTF ...
show by default the entries of the all outlets which are grouped together in DMS and PC
belongs to one of them. PC can search and see the booking details and can claim that
booking is intimated ... same process has to be followed for the delivery dump."

And: "lets show the data to PC between the two dump dates which has been uploaded, and by
default retain or carry for previous days which are in dms dump".

The rows      the dealership's latest dump of the kind (booking or delivery) in the month,
              rows outside its days and cancelled bookings left out. A PC sees the rows of
              the DMS locations their outlets stand under (Aditya's CUTTACK carries every
              Aditya outlet but JAJPUR), and rows naming no location.
Default view  rows dated after the day of the dealership's previous dump of the kind (the
              days between the two dumps), and every earlier row of the month still not in
              Verigence and not marked intimated -- carried into each new dump until a PC
              marks it or it reaches Verigence (2026-11-19: "the bookings which were not
              intimated in past may have been intimated ... let the PC have the view for all
              the bookings which hasnt been confirmed by him as intimated whenever the new
              dumps get uploaded"). "open" is the carried part alone; "month" all. A search
              (name, mobile, OTF, booking number, chassis, invoice) runs over the whole month.
The mark      a row in Verigence needs none (the PC has its papers). Any other row the PC
              marks "intimated to me". The mark is kept by the row's identity (OTF /
              booking number for a booking; chassis / invoice for a delivery), so it holds
              for the same row in every later dump; a PC withdraws only their own mark.
The alert     an upload alerts the PCs of the dealership's outlets on every screen.
The counter   on demand, for whoever generates the report: per PC of the dealership, how
              many rows they have marked, and the rows still open.

``GET  /p2/v1/tenants/{t}/dms-intimations/setup``
``GET  /p2/v1/tenants/{t}/dms-intimations?kind=BOOKING&dealerId=&view=new|open|month&q=``
``POST /p2/v1/tenants/{t}/dms-intimations/claim``    {dumpId, rowNo}
``POST /p2/v1/tenants/{t}/dms-intimations/withdraw`` {dumpId, rowNo}
``GET  /p2/v1/tenants/{t}/dms-intimations/counter?kind=BOOKING&dealerId=``
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_dms_dumps import (
    _READ,
    BOOKING,
    CANCELLED,
    DELIVERY,
    MATCHED,
    NOT_IN_TOOL,
    OUT_OF_PERIOD,
    _access,
    _dealers,
    _name_of,
    _today,
)

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-dms-intimations"])

# Who marks: the PC. Who reads the counter and generates the report: the Reviewer and above.
PC_ROLES = ("PC",)
REPORT_ROLES = ("REVIEWER", "TL", "PM", "EXECUTIVE")
VIEWS = ("new", "open", "month")
ALERT_KIND = "DMS_DUMP_UPLOADED"
ALERT_HOURS = 24
# The row states a PC sees.
IN_VERIGENCE, INTIMATED, OPEN = "IN_VERIGENCE", "INTIMATED", "OPEN"


# --------------------------------------------------------------------------- identity

def _alnum(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))[-10:]


def row_key(kind: str, row: dict[str, Any]) -> str:
    """The row's identity across dumps: the OTF / booking number for a booking; the chassis /
    invoice for a delivery (then its OTF); else name, mobile and day."""
    if kind == BOOKING:
        order = (("OTF", row.get("dms_booking_number")), ("BKG", row.get("booking_number")))
    else:
        order = (("CH", row.get("chassis")), ("INV", row.get("invoice_number")),
                 ("OTF", row.get("dms_booking_number")), ("BKG", row.get("booking_number")))
    for tag, value in order:
        if len(key := _alnum(value)) >= 4:
            return f"{tag}:{key}"
    day = row.get("event_date")
    return f"NM:{_alnum(row.get('customer_name'))}|{_digits(row.get('mobile'))}|{day.isoformat() if day else ''}"


# --------------------------------------------------------------------------- the dumps and marks

def dump_pair(connection: Connection, tenant_id: str, dealer_id: str, kind: str, upto: date | None = None
              ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The dealership's latest current dump of the kind (up to ``upto``) and the one before it
    (an earlier day, the same month)."""
    rows = connection.execute(
        text("""
            SELECT DISTINCT ON (u.business_date) u.dump_id, u.business_date, u.period_month, u.uploaded_at_utc
            FROM auditcore.p2_dms_dumps u
            WHERE u.tenant_id = :t AND u.dealer_id = CAST(:d AS uuid) AND u.dump_kind = :k AND u.status = 'ACTIVE'
              AND (CAST(:upto AS date) IS NULL OR u.business_date <= CAST(:upto AS date))
            ORDER BY u.business_date DESC, u.uploaded_at_utc DESC
            LIMIT 2
        """),
        {"t": tenant_id, "d": dealer_id, "k": kind, "upto": upto},
    ).mappings().all()
    latest = dict(rows[0]) if rows else None
    previous = (dict(rows[1]) if len(rows) > 1 and rows[1]["period_month"] == rows[0]["period_month"] else None)
    return latest, previous


_ROWS_SQL = """
    SELECT r.dump_id, r.row_no, r.dump_kind, r.dealer_id, r.outlet_label, r.event_date, r.booking_number,
           r.dms_booking_number, r.customer_name, r.mobile, r.chassis, r.invoice_number, r.registration_number,
           r.model, r.variant, r.stage, r.consultant, r.outcome, r.match_basis, r.deal_type, r.deal_type_basis,
           CAST(r.journey_id AS text) AS journey_id, j.journey_reference
    FROM auditcore.p2_dms_dump_rows r
    LEFT JOIN auditcore.journeys j ON j.tenant_id = r.tenant_id AND j.journey_id = r.journey_id
    WHERE r.tenant_id = :t AND r.dump_id = CAST(:id AS uuid) AND r.outcome NOT IN (:oop, :cancelled)
    ORDER BY r.event_date DESC NULLS LAST, r.row_no
"""


def dump_rows(connection: Connection, tenant_id: str, dump_id: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(
        text(_ROWS_SQL), {"t": tenant_id, "id": str(dump_id), "oop": OUT_OF_PERIOD, "cancelled": CANCELLED},
    ).mappings()]


def live_marks(connection: Connection, tenant_id: str, dealer_ids: list[str], kind: str | None = None
               ) -> dict[tuple[str, str, str], dict[str, Any]]:
    """(dealer, kind, row key) -> the live mark."""
    rows = connection.execute(
        text("""
            SELECT CAST(dealer_id AS text) AS dealer_id, dump_kind, row_key, claimed_by, claimed_by_name,
                   claimed_at_utc
            FROM auditcore.p2_dms_intimations
            WHERE tenant_id = :t AND withdrawn_at_utc IS NULL AND CAST(dealer_id AS text) = ANY(:ds)
              AND (CAST(:k AS text) IS NULL OR dump_kind = CAST(:k AS text))
        """),
        {"t": tenant_id, "ds": dealer_ids, "k": kind},
    ).mappings().all()
    return {(r["dealer_id"], r["dump_kind"], r["row_key"]): dict(r) for r in rows}


def needs_mark(row: dict[str, Any]) -> bool:
    """A row Verigence holds needs no mark; a delivery whose booking is on file but whose
    delivery is not uploaded does."""
    return row["outcome"] != MATCHED


def row_state(row: dict[str, Any], mark: dict[str, Any] | None) -> str:
    if not needs_mark(row):
        return IN_VERIGENCE
    return INTIMATED if mark is not None else OPEN


# --------------------------------------------------------------------------- who sees what

def _pc_outlets(connection: Connection, tenant_id: str, actor_id: str) -> list[dict[str, Any]]:
    from audit_core.uc03_p2_gate_pass import scope_outlets

    return [o for o in scope_outlets(connection, tenant_id, actor_id)
            if {str(r).upper() for r in (o.get("roles") or [])} & set(PC_ROLES)]


def pc_scope(connection: Connection, tenant_id: str, actor_id: str) -> dict[str, dict[str, Any]]:
    """dealer -> {name, anchors: the DMS locations the PC's outlets stand under, outlets}."""
    from audit_core.uc03_p2_dms_outlet_report import outlet_map, short_name

    out: dict[str, dict[str, Any]] = {}
    for o in _pc_outlets(connection, tenant_id, actor_id):
        did = str(o["dealer_id"])
        entry = out.setdefault(did, {"dealerName": o["dealer_name"], "anchors": set(), "outlets": None})
        if entry["outlets"] is None:
            entry["outlets"] = outlet_map(connection, tenant_id, did)
        by_id = {str(x["outlet_id"]): x for x in entry["outlets"]}
        mapped = by_id.get(str(o["outlet_id"]))
        entry["anchors"].add(str(mapped["dms_outlet_id"]) if mapped else str(o["outlet_id"]))
    for entry in out.values():
        by_id = {str(x["outlet_id"]): x for x in entry["outlets"] or []}
        entry["groups"] = sorted({short_name(by_id[a]["outlet_name"]) for a in entry["anchors"] if a in by_id})
        entry["groupOutlets"] = sorted(short_name(x["outlet_name"]) for x in entry["outlets"] or []
                                       if str(x["dms_outlet_id"]) in entry["anchors"])
    return out


def _in_scope(rows: list[dict[str, Any]], outlets: list[dict[str, Any]], anchors: set[str] | None
              ) -> list[dict[str, Any]]:
    """The rows of the PC's DMS locations, and those naming none (never lost)."""
    from audit_core.uc03_p2_dms_outlet_report import resolve_location, short_name

    by_id = {str(o["outlet_id"]): o for o in outlets}
    cache: dict[str, str | None] = {}
    out = []
    for r in rows:
        label = str(r.get("outlet_label") or "").strip()
        if label not in cache:
            cache[label] = resolve_location(label, outlets)
        anchor = cache[label]
        r["location"] = short_name(by_id[anchor]["outlet_name"]) if anchor in by_id else (label or None)
        if anchors is None or anchor is None or anchor in anchors:
            out.append(r)
    return out


def _role(connection: Connection, tenant_id: str, principal: HumanPrincipal,
          client: SecurityAuthorizationClient) -> set[str]:
    return _access(connection, tenant_id, principal, client, _READ, PC_ROLES + REPORT_ROLES)


def _kind(value: str | None) -> str:
    kind = (value or BOOKING).upper()
    if kind not in (BOOKING, DELIVERY):
        raise HTTPException(status_code=422, detail="Kind is BOOKING or DELIVERY.")
    return kind


# --------------------------------------------------------------------------- endpoints

@router.get("/dms-intimations/setup")
def intimation_setup(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The dealerships the caller marks for (a PC: their DMS locations) or reads (Reviewer and
    above), with each kind's latest and previous dump day."""
    held = _role(connection, tenant_id, principal, client)
    is_pc = "ADMIN" not in held and bool(held & set(PC_ROLES))
    reviewer = "ADMIN" in held or bool(held & set(REPORT_ROLES))
    if is_pc:
        scope = pc_scope(connection, tenant_id, principal.subject)
        dealers = [{"dealerId": d, "dealerName": s["dealerName"], "groups": s["groups"],
                    "groupOutlets": s["groupOutlets"]} for d, s in scope.items()]
    else:
        dealers = [{**d, "groups": [], "groupOutlets": []}
                   for d in _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)]
    for d in dealers:
        for kind in (BOOKING, DELIVERY):
            latest, previous = dump_pair(connection, tenant_id, d["dealerId"], kind)
            d[kind.lower()] = {"dumpDate": latest["business_date"].isoformat() if latest else None,
                               "previousDumpDate": previous["business_date"].isoformat() if previous else None}
    return {"dealers": dealers, "canMark": is_pc, "canSeeCounter": reviewer,
            "today": _today(connection, tenant_id).isoformat()}


def _matches(row: dict[str, Any], q: str) -> bool:
    words = [w for w in re.split(r"\s+", q.strip().upper()) if w]
    hay = " ".join(str(row.get(k) or "") for k in ("customer_name", "dms_booking_number", "booking_number",
                                                     "chassis", "invoice_number", "registration_number")).upper()
    digits = _digits(q)
    if len(digits) >= 4 and digits in re.sub(r"\D", "", str(row.get("mobile") or "")):
        return True
    alnum_hay = _alnum(hay)
    return all(w in hay or (len(_alnum(w)) >= 4 and _alnum(w) in alnum_hay) for w in words)


def _item(row: dict[str, Any], mark: dict[str, Any] | None, me: str) -> dict[str, Any]:
    return {
        "dumpId": str(row["dump_id"]), "rowNo": row["row_no"], "kind": row["dump_kind"],
        "eventDate": row["event_date"].isoformat() if row["event_date"] else None,
        "customerName": row["customer_name"], "mobile": row["mobile"], "otf": row["dms_booking_number"],
        "bookingNumber": row["booking_number"], "chassis": row["chassis"], "invoiceNumber": row["invoice_number"],
        "registrationNumber": row["registration_number"], "model": row["model"], "variant": row["variant"],
        "stage": row["stage"], "consultant": row["consultant"], "location": row.get("location"),
        "outcome": row["outcome"], "state": row_state(row, mark),
        "dealType": row.get("deal_type"), "dealTypeBasis": row.get("deal_type_basis"),
        "bookingOnFile": row["outcome"] == NOT_IN_TOOL,
        "journeyId": row["journey_id"], "journeyReference": row["journey_reference"],
        "mark": ({"byName": mark["claimed_by_name"] or mark["claimed_by"], "atUtc": mark["claimed_at_utc"].isoformat(),
                  "mine": mark["claimed_by"] == me} if mark else None),
    }


@router.get("/dms-intimations")
def list_intimations(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    kind: str | None = None,
    dealerId: UUID | None = None,
    view: str = "new",
    q: str | None = None,
) -> dict[str, Any]:
    from audit_core.uc03_p2_dms_outlet_report import outlet_map

    held = _role(connection, tenant_id, principal, client)
    kind = _kind(kind)
    if view not in VIEWS:
        raise HTTPException(status_code=422, detail="View is new, open or month.")
    is_pc = "ADMIN" not in held and bool(held & set(PC_ROLES))
    if is_pc:
        scope = pc_scope(connection, tenant_id, principal.subject)
        allowed = {d: (s["dealerName"], s["anchors"], s["outlets"]) for d, s in scope.items()}
    else:
        allowed = {d["dealerId"]: (d["dealerName"], None, None)
                   for d in _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)}
    if not allowed:
        return {"kind": kind, "dealerId": None, "items": [], "counts": dict.fromkeys(VIEWS, 0), "dump": None}
    did = str(dealerId) if dealerId else next(iter(allowed))
    if did not in allowed:
        raise HTTPException(status_code=403, detail="That dealership is not one you cover.")
    name, anchors, outlets = allowed[did]
    if outlets is None:
        outlets = outlet_map(connection, tenant_id, did)
    latest, previous = dump_pair(connection, tenant_id, did, kind)
    if latest is None:
        return {"kind": kind, "dealerId": did, "dealerName": name, "items": [], "counts": dict.fromkeys(VIEWS, 0),
                "dump": None}
    rows = _in_scope(dump_rows(connection, tenant_id, latest["dump_id"]), outlets, anchors)
    marks = live_marks(connection, tenant_id, [did], kind)
    since = previous["business_date"] if previous else None

    def mark_of(r: dict[str, Any]) -> dict[str, Any] | None:
        return marks.get((did, kind, row_key(kind, r)))

    def is_new(r: dict[str, Any]) -> bool:
        return since is None or r["event_date"] is None or r["event_date"] > since

    carried = [r for r in rows if not is_new(r) and row_state(r, mark_of(r)) == OPEN]
    carried_ids = {id(r) for r in carried}
    groups = {
        # The days since the previous dump, and the earlier rows nobody has marked yet.
        "new": [r for r in rows if is_new(r) or id(r) in carried_ids],
        "open": carried,
        "month": rows,
    }
    chosen = [r for r in rows if _matches(r, q)] if (q or "").strip() else groups[view]
    states = defaultdict(int)
    for r in groups["new"]:
        states[row_state(r, mark_of(r))] += 1
    return {
        "kind": kind, "dealerId": did, "dealerName": name, "view": view, "q": q or None,
        "dump": {"dumpId": str(latest["dump_id"]), "dumpDate": latest["business_date"].isoformat(),
                 "previousDumpDate": since.isoformat() if since else None},
        "counts": {k: len(v) for k, v in groups.items()},
        "newStates": {s: states.get(s, 0) for s in (IN_VERIGENCE, INTIMATED, OPEN)},
        "items": [{**_item(r, mark_of(r), principal.subject), "carried": not is_new(r)} for r in chosen[:500]],
        "truncated": len(chosen) > 500,
    }


class MarkCommand(BaseModel):
    dumpId: UUID
    rowNo: int


def _row_for_mark(connection: Connection, tenant_id: str, principal: HumanPrincipal, command: MarkCommand
                  ) -> dict[str, Any]:
    row = connection.execute(
        text("""
            SELECT r.*, u.status AS dump_status FROM auditcore.p2_dms_dump_rows r
            JOIN auditcore.p2_dms_dumps u ON u.tenant_id = r.tenant_id AND u.dump_id = r.dump_id
            WHERE r.tenant_id = :t AND r.dump_id = :id AND r.row_no = :n
        """),
        {"t": tenant_id, "id": str(command.dumpId), "n": command.rowNo},
    ).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No such row in the dump.")
    row = dict(row)
    scope = pc_scope(connection, tenant_id, principal.subject).get(str(row["dealer_id"]))
    if scope is None or not _in_scope([dict(row)], scope["outlets"], scope["anchors"]):
        raise HTTPException(status_code=403, detail="That row is not of your outlets.")
    return row


@router.post("/dms-intimations/claim")
def claim_intimation(
    tenant_id: str,
    command: MarkCommand,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The PC marks a row as intimated to them."""
    _access(connection, tenant_id, principal, client, _READ, PC_ROLES)
    row = _row_for_mark(connection, tenant_id, principal, command)
    if row["outcome"] in (OUT_OF_PERIOD, CANCELLED):
        raise HTTPException(status_code=409, detail="This row is not compared in the dump.")
    if not needs_mark(row):
        raise HTTPException(status_code=409, detail="This one is already in Verigence; it needs no mark.")
    kind, key = row["dump_kind"], row_key(row["dump_kind"], row)
    existing = live_marks(connection, tenant_id, [str(row["dealer_id"])], kind).get((str(row["dealer_id"]), kind, key))
    if existing is not None:
        if existing["claimed_by"] == principal.subject:
            return {"state": INTIMATED, "rowNo": row["row_no"]}
        raise HTTPException(status_code=409, detail=(
            f"Already marked intimated by {existing['claimed_by_name'] or 'another PC'}."))
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_dms_intimations (tenant_id, intimation_id, dealer_id, dump_kind, row_key, dump_id,
              row_no, customer_name, event_date, outlet_label, claimed_by, claimed_by_name)
            VALUES (:t, :id, :d, :k, :key, :dump, :n, :c, :e, :l, :by, :byn)
            ON CONFLICT DO NOTHING
        """),
        {"t": tenant_id, "id": str(uuid4()), "d": str(row["dealer_id"]), "k": kind, "key": key,
         "dump": str(row["dump_id"]), "n": row["row_no"], "c": row["customer_name"], "e": row["event_date"],
         "l": row["outlet_label"], "by": principal.subject, "byn": _name_of(connection, principal.subject)},
    )
    return {"state": INTIMATED, "rowNo": row["row_no"]}


@router.post("/dms-intimations/withdraw")
def withdraw_intimation(
    tenant_id: str,
    command: MarkCommand,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The PC withdraws their own mark."""
    _access(connection, tenant_id, principal, client, _READ, PC_ROLES)
    row = _row_for_mark(connection, tenant_id, principal, command)
    done = connection.execute(
        text("""
            UPDATE auditcore.p2_dms_intimations SET withdrawn_at_utc = now(), withdrawn_by = :by
            WHERE tenant_id = :t AND dealer_id = :d AND dump_kind = :k AND row_key = :key
              AND withdrawn_at_utc IS NULL AND claimed_by = :by
        """),
        {"t": tenant_id, "d": str(row["dealer_id"]), "k": row["dump_kind"], "key": row_key(row["dump_kind"], row),
         "by": principal.subject},
    ).rowcount
    if not done:
        raise HTTPException(status_code=409, detail="There is no mark of yours on this row.")
    return {"state": row_state(row, None), "rowNo": row["row_no"]}


def counter(connection: Connection, tenant_id: str, dealer_id: str, kind: str, upto: date | None = None
            ) -> dict[str, Any]:
    """Per PC of the dealership: the rows of the latest dump they marked; and the dump's rows
    in Verigence, marked and still open."""
    from audit_core.uc03_p2_dms_outlet_report import (
        outlet_map,
        resolve_location,
        short_name,
    )

    latest, previous = dump_pair(connection, tenant_id, dealer_id, kind, upto)
    outlets = outlet_map(connection, tenant_id, dealer_id)
    by_id = {str(o["outlet_id"]): o for o in outlets}
    pcs = connection.execute(
        text("""
            SELECT ba.security_actor_id AS actor, COALESCE(pn.display_name, ba.security_actor_id) AS name,
                   array_agg(DISTINCT CAST(ba.outlet_id AS text)) AS outlets
            FROM auditcore.business_assignments ba
            LEFT JOIN auditcore.people_names pn ON pn.actor_id = ba.security_actor_id
            WHERE ba.tenant_id = :t AND ba.dealer_id = CAST(:d AS uuid) AND ba.outlet_id IS NOT NULL
              AND ba.assignment_status = 'ACTIVE' AND upper(ba.business_role_code) = 'PC'
              AND ba.effective_from <= now() AND (ba.effective_to IS NULL OR ba.effective_to >= now())
            GROUP BY ba.security_actor_id, pn.display_name
            ORDER BY lower(COALESCE(pn.display_name, ba.security_actor_id))
        """),
        {"t": tenant_id, "d": dealer_id},
    ).mappings().all()
    rows = dump_rows(connection, tenant_id, latest["dump_id"]) if latest else []
    marks = live_marks(connection, tenant_id, [dealer_id], kind)
    since = previous["business_date"] if previous else None
    totals = {"rows": 0, "inVerigence": 0, "intimated": 0, "open": 0, "newRows": 0, "newOpen": 0}
    by_pc: dict[str, dict[str, Any]] = defaultdict(lambda: {"marked": 0, "markedNew": 0, "lastAtUtc": None})
    open_by_location: dict[str, int] = defaultdict(int)
    for r in rows:
        mark = marks.get((dealer_id, kind, row_key(kind, r)))
        state = row_state(r, mark)
        new = since is None or r["event_date"] is None or r["event_date"] > since
        totals["rows"] += 1
        totals["newRows"] += new
        totals[{"IN_VERIGENCE": "inVerigence", "INTIMATED": "intimated", "OPEN": "open"}[state]] += 1
        if state == OPEN:
            totals["newOpen"] += new
            anchor = resolve_location(r.get("outlet_label"), outlets)
            open_by_location[short_name(by_id[anchor]["outlet_name"]) if anchor in by_id
                             else (r.get("outlet_label") or "(no location)")] += 1
        if mark is not None and state == INTIMATED:
            entry = by_pc[mark["claimed_by"]]
            entry["marked"] += 1
            entry["markedNew"] += new
            at = mark["claimed_at_utc"]
            entry["lastAtUtc"] = max(entry["lastAtUtc"], at) if entry["lastAtUtc"] else at
    people = []
    for pc in pcs:
        entry = by_pc.get(pc["actor"], {"marked": 0, "markedNew": 0, "lastAtUtc": None})
        people.append({"actorId": pc["actor"], "name": pc["name"],
                       "outlets": sorted(short_name(by_id[o]["outlet_name"]) for o in pc["outlets"] if o in by_id),
                       "marked": entry["marked"], "markedNew": entry["markedNew"],
                       "lastAtUtc": entry["lastAtUtc"].isoformat() if entry["lastAtUtc"] else None})
    known = {p["actor"] for p in pcs}
    for actor, entry in by_pc.items():
        if actor not in known:  # a PC no longer mapped who marked rows
            people.append({"actorId": actor, "name": _name_of(connection, actor) or actor, "outlets": [],
                           "marked": entry["marked"], "markedNew": entry["markedNew"],
                           "lastAtUtc": entry["lastAtUtc"].isoformat() if entry["lastAtUtc"] else None})
    return {"kind": kind, "dealerId": dealer_id,
            "dumpDate": latest["business_date"].isoformat() if latest else None,
            "previousDumpDate": since.isoformat() if since else None,
            "totals": totals, "pcs": people,
            "openByLocation": [{"location": k, "open": v} for k, v in sorted(open_by_location.items())]}


@router.get("/dms-intimations/counter")
def get_counter(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    kind: str | None = None,
    dealerId: UUID | None = None,
) -> dict[str, Any]:
    """On demand: which PC has marked how many (Reviewer and above)."""
    held = _access(connection, tenant_id, principal, client, _READ, REPORT_ROLES)
    kind = _kind(kind)
    dealers = _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)
    chosen = [d for d in dealers if dealerId is None or d["dealerId"] == str(dealerId)]
    if dealerId is not None and not chosen:
        raise HTTPException(status_code=403, detail="That dealership is not one you cover.")
    return {"kind": kind, "dealers": [{**counter(connection, tenant_id, d["dealerId"], kind),
                                       "dealerName": d["dealerName"]} for d in chosen]}


# --------------------------------------------------------------------------- the alert

def publish_dump_alert(connection: Connection, *, tenant_id: str, dealer_id: str, dealer_name: str, kind: str,
                       business_date: date) -> None:
    """Every PC of the dealership's outlets, on every screen: the dump is in, mark your
    intimations. One alert per dealership and kind: a later upload replaces the earlier."""
    outlets = [str(r[0]) for r in connection.execute(
        text("SELECT outlet_id FROM auditcore.dealer_outlets WHERE tenant_id = :t AND dealer_id = CAST(:d AS uuid)"),
        {"t": tenant_id, "d": dealer_id})]
    if not outlets:
        return
    word = "Booking" if kind == BOOKING else "Delivery"
    connection.execute(
        text("""
            UPDATE auditcore.p2_tenant_alerts SET expires_at_utc = now(), updated_at_utc = now()
            WHERE tenant_id = :t AND alert_kind = :k AND alert_status = 'PUBLISHED' AND expires_at_utc > now()
              AND payload->>'dealerId' = :d AND payload->>'dumpKind' = :dk
        """),
        {"t": tenant_id, "k": ALERT_KIND, "d": dealer_id, "dk": kind},
    )
    payload = {"audienceOutletIds": outlets, "audienceRoles": list(PC_ROLES), "dealerId": dealer_id,
               "dealerName": dealer_name, "dumpKind": kind, "dumpDate": business_date.isoformat(),
               "link": f"/p2/dms-intimations?kind={kind}&dealerId={dealer_id}"}
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_tenant_alerts (tenant_id, alert_kind, alert_status, title, body, payload,
              published_at_utc, expires_at_utc)
            VALUES (:t, :k, 'PUBLISHED', :title, :body, CAST(:p AS jsonb), now(), now() + make_interval(hours => :h))
        """),
        {"t": tenant_id, "k": ALERT_KIND, "h": ALERT_HOURS,
         "title": f"{word} dump has been uploaded",
         "body": (f"{dealer_name}'s DMS {word.lower()} dump of {business_date:%d %b} is in. "
                  f"Pls update your {word.lower()} intimations."),
         "p": json.dumps(payload)},
    )


__all__ = ["counter", "dump_pair", "dump_rows", "live_marks", "publish_dump_alert", "router", "row_key", "row_state"]
