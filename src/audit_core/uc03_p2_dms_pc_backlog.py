"""Each PC against the DMS, month wise (product owner 2026-11-23).

"For each PC view, I need to see the backlog as well as per DMS month wise ... both for booking
and deliveries" -- "for locations which have multiple PC, show the count as same for all PC
tagged to that location" -- "see if they have updated the deliveries/booking file they have
received (this will come from DMS upload)".

For a DMS month, each dealership's latest current dump of each kind in that month (the dumps are
month to date, so the latest holds the whole month so far). Its rows are counted per DMS location
exactly as the DMS report counts them (uc03_p2_dms_outlet_report.dms_figures: same matching,
same intimation marks, same out-of-scope models left out), and every PC mapped to an outlet
standing under a location carries that location's figures in full; a PC on two locations
carries both.

    bookings     in the DMS; in Verigence (synced); pending: not in Verigence, of which
                 intimated to the PC (with the PC) and not intimated (with the dealership)
    deliveries   in the DMS; in Verigence (synced: the delivery file is there); pending: the
                 booking is in Verigence but the delivery file is not uploaded, or the sale is
                 not in Verigence at all; of the pending, intimated and not intimated

Rows naming no DMS location are not put on anybody: they are returned per dealership, so the
totals still agree with the dumps.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from sqlalchemy import Connection, text

FIGURES = ("bookingDms", "bookingSynced", "bookingPending", "bookingIntimated", "bookingNotIntimated",
           "deliveryDms", "deliverySynced", "deliveryNotUploaded", "deliveryNotInVerigence", "deliveryPending",
           "deliveryIntimated", "deliveryNotIntimated")


def _from_dms(dms: dict[str, int]) -> dict[str, int]:
    return {
        "bookingDms": dms.get("bookingDms", 0), "bookingSynced": dms.get("bookingSynced", 0),
        "bookingPending": dms.get("bookingNotInVerigence", 0),
        "bookingIntimated": dms.get("bookingIntimated", 0), "bookingNotIntimated": dms.get("bookingNotIntimated", 0),
        "deliveryDms": dms.get("deliveryDump", 0), "deliverySynced": dms.get("deliverySynced", 0),
        "deliveryNotUploaded": dms.get("deliveryNotUploaded", 0),
        "deliveryNotInVerigence": dms.get("deliveryNotInVerigence", 0),
        "deliveryPending": dms.get("deliveryNotUploaded", 0) + dms.get("deliveryNotInVerigence", 0),
        "deliveryIntimated": dms.get("deliveryIntimated", 0),
        "deliveryNotIntimated": dms.get("deliveryNotIntimated", 0),
    }


def _add(into: dict[str, int], more: dict[str, int]) -> None:
    for k in FIGURES:
        into[k] = into.get(k, 0) + more.get(k, 0)


def months(connection: Connection, tenant_id: str, dealer_ids: list[str]) -> list[str]:
    """The DMS months with a current dump for any of the dealerships, newest first."""
    if not dealer_ids:
        return []
    return [r[0].strftime("%Y-%m") for r in connection.execute(
        text("""
            SELECT DISTINCT period_month FROM auditcore.p2_dms_dumps
            WHERE tenant_id = :t AND status = 'ACTIVE' AND CAST(dealer_id AS text) = ANY(:ds)
            ORDER BY period_month DESC
        """),
        {"t": tenant_id, "ds": dealer_ids},
    )]


def _latest_of_month(connection: Connection, tenant_id: str, dealer_ids: list[str], month: date
                     ) -> dict[tuple[str, str], dict[str, Any]]:
    rows = connection.execute(
        text("""
            SELECT DISTINCT ON (dealer_id, dump_kind) CAST(dealer_id AS text) AS dealer_id, dump_kind, dump_id,
                   business_date
            FROM auditcore.p2_dms_dumps
            WHERE tenant_id = :t AND status = 'ACTIVE' AND period_month = :m AND CAST(dealer_id AS text) = ANY(:ds)
            ORDER BY dealer_id, dump_kind, business_date DESC, uploaded_at_utc DESC
        """),
        {"t": tenant_id, "m": month, "ds": dealer_ids},
    ).mappings().all()
    return {(r["dealer_id"], r["dump_kind"]): dict(r) for r in rows}


def pc_backlog(connection: Connection, *, tenant_id: str, dealer_ids: list[str], month: date
               ) -> dict[str, Any]:
    """``byPerson``: PC -> figures (each location they are tagged to, in full); ``dealers``: per
    dealership the dumps used and the rows naming no DMS location."""
    from audit_core.uc03_p2_dms_dumps import BOOKING, DELIVERY
    from audit_core.uc03_p2_dms_intimations import live_marks, row_key
    from audit_core.uc03_p2_dms_outlet_report import (
        _pcs_by_outlet,
        dms_figures,
        outlet_map,
        short_name,
    )

    dumps = _latest_of_month(connection, tenant_id, dealer_ids, month)
    dump_ids = [str(d["dump_id"]) for d in dumps.values()]
    rows_by_dealer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if dump_ids:
        for r in connection.execute(
            text("""
                SELECT CAST(r.dealer_id AS text) AS dealer_id, r.dump_kind, r.outlet_label, r.outcome,
                       CAST(r.journey_id AS text) AS journey_id, r.deal_type, r.dms_booking_number,
                       r.booking_number, r.chassis, r.invoice_number, r.customer_name, r.mobile, r.event_date
                FROM auditcore.p2_dms_dump_rows r
                WHERE r.tenant_id = :t AND CAST(r.dump_id AS text) = ANY(:ids)
                  AND r.outcome NOT IN ('OUT_OF_PERIOD', 'EXCLUDED', 'DUPLICATE')
            """),
            {"t": tenant_id, "ids": dump_ids},
        ).mappings():
            rows_by_dealer[r["dealer_id"]].append(dict(r))
    marks = live_marks(connection, tenant_id, dealer_ids) if dump_ids else {}
    # A delivery whose file came after the dump was matched is synced (as in the DMS report).
    delivery_journeys = [r["journey_id"] for rows in rows_by_dealer.values() for r in rows
                         if r["dump_kind"] == DELIVERY and r["journey_id"]]
    delivered: set[str] = set()
    if delivery_journeys:
        delivered = {str(x[0]) for x in connection.execute(
            text("""
                SELECT DISTINCT q.journey_id FROM auditcore.p2_document_queue q
                WHERE q.tenant_id = :t AND CAST(q.journey_id AS text) = ANY(:ids) AND q.business_stage = 'DELIVERY'
                  AND q.queue_status <> 'CANCELLED'
            """),
            {"t": tenant_id, "ids": delivery_journeys},
        )}

    dealer_names = {str(r[0]): r[1] for r in connection.execute(
        text("SELECT dealer_id, dealer_name FROM auditcore.dealers WHERE tenant_id = :t AND CAST(dealer_id AS text) = ANY(:ds)"),
        {"t": tenant_id, "ds": dealer_ids})}
    by_person: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(FIGURES, 0))
    locations_of: dict[str, list[str]] = defaultdict(list)
    dealers_out = []
    for did in dealer_ids:
        rows = rows_by_dealer.get(did, [])
        for r in rows:
            r["intimated"] = (did, r["dump_kind"], row_key(r["dump_kind"], r)) in marks
        outlets = outlet_map(connection, tenant_id, did)
        by_id = {str(o["outlet_id"]): o for o in outlets}
        located, unmapped = dms_figures(rows, outlets, delivered)
        members: dict[str, list[str]] = defaultdict(list)
        for o in outlets:
            anchor = str(o["dms_outlet_id"])
            members[anchor if anchor in by_id else str(o["outlet_id"])].append(str(o["outlet_id"]))
        pcs = _pcs_by_outlet(connection, tenant_id, [str(o["outlet_id"]) for o in outlets])
        for anchor, ids in members.items():
            figures = _from_dms(located.get(anchor, {}))
            tagged = set().union(*(pcs.get(i, set()) for i in ids)) if ids else set()
            for pc in tagged:
                _add(by_person[pc], figures)
                locations_of[pc].append(short_name(by_id[anchor]["outlet_name"]))
        booking, delivery = dumps.get((did, BOOKING)), dumps.get((did, DELIVERY))
        dealers_out.append({
            "dealerId": did, "dealerName": dealer_names.get(did),
            "bookingDumpDate": booking["business_date"].isoformat() if booking else None,
            "deliveryDumpDate": delivery["business_date"].isoformat() if delivery else None,
            "unmapped": [{"label": label, **_from_dms(f)} for label, f in sorted(unmapped.items())],
        })
    return {"month": month.strftime("%Y-%m"), "dealers": dealers_out,
            "byPerson": {pc: {**f, "locations": sorted(set(locations_of[pc]))} for pc, f in by_person.items()}}
