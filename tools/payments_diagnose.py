"""Read-only: the Team Lead screen's November figures beside the DMS report's and the Project
Tracker's, per dealership. Counts only; no customer details."""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_dates import programme_date_floor
from audit_core.uc03_p2_dms_outlet_report import build_outlet_report
from audit_core.uc03_p2_journey_views import _DELIVERED_J, COUNTS_WITH_BREAKDOWN_SQL, SCOPED_SQL, scope_params
from audit_core.uc03_p2_project_tracker import build_project_tracker

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
MONTH = "2026-11"
engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=300000"})


def j(value):
    return json.dumps(value, default=str, sort_keys=True)


with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    tz = c.execute(text("SELECT timezone_name FROM auditcore.projects WHERE tenant_id=:t"), {"t": T}).scalar() or "Asia/Kolkata"
    today = datetime.now(UTC).astimezone(ZoneInfo(tz)).date()
    actor = c.execute(text("""
        SELECT ba.security_actor_id FROM auditcore.business_assignments ba
        WHERE ba.tenant_id=:t AND ba.assignment_status='ACTIVE' AND ba.dealer_id IS NULL
          AND ba.effective_from <= now() AND (ba.effective_to IS NULL OR ba.effective_to >= now())
        LIMIT 1"""), {"t": T}).scalar_one()
    params = {"tenant_id": T, "actor_id": actor, **scope_params(search=""),
              "date_floor": programme_date_floor() or date(1900, 1, 1), "month": MONTH,
              "month_basis": "business", "timezone": tz}
    dealers = [dict(r) for r in c.execute(text(
        "SELECT CAST(dealer_id AS text) AS \"dealerId\", dealer_name AS \"dealerName\" FROM auditcore.dealers "
        "WHERE tenant_id=:t ORDER BY dealer_name"), {"t": T}).mappings()]
    print("today", today, "| timezone", tz, "| dealerships", [d["dealerName"] for d in dealers])

    print("\n=== A. Team Lead screen (journeys:counts, calendar month", MONTH, ") ===")
    for r in c.execute(text(COUNTS_WITH_BREAKDOWN_SQL), params).mappings():
        if r["part"] in ("all", "dealer"):
            print(r["part"], r["dealer_name"] if r["part"] == "dealer" else "PROJECT", j({k: r[k] for k in (
                "total", "bookings_in_month", "deliveries_in_month", "booking_open", "delivery_open", "closed")}))

    print("\n=== A2. What the screen's journeys are ===")
    rows = c.execute(text("WITH " + SCOPED_SQL + f"""
        SELECT s.dealer_name, s.booking_in_period, s.delivery_in_period, s.booking_month, s.delivery_month,
               s.in_delivery, s.is_closed,
               (SELECT {_DELIVERED_J} FROM auditcore.journeys j
                 WHERE j.tenant_id = s.tenant_id AND j.journey_id = s.journey_id) AS vin_delivery,
               EXISTS (SELECT 1 FROM auditcore.journey_stage_states bs WHERE bs.tenant_id = s.tenant_id
                       AND bs.journey_id = s.journey_id AND bs.stage_code = 'BOOKING'
                       AND (bs.business_status IN ('BOOKING_CANCELLED', 'DUPLICATE_BOOKING')
                            OR bs.closure_disposition = 'NO_DELIVERY')) AS cancelled
        FROM scoped s"""), params).mappings().all()
    print("journeys on the screen:", len(rows))
    print("booking date in Nov / delivery date in Nov:", dict(Counter(
        ("booking" if r["booking_in_period"] else "-") + "+" + ("delivery" if r["delivery_in_period"] else "-") for r in rows)))
    print("booked in Nov, by delivery month:", dict(Counter(r["delivery_month"] or "no delivery date" for r in rows if r["booking_in_period"])))
    print("delivered in Nov, by booking month:", dict(Counter(r["booking_month"] for r in rows if r["delivery_in_period"])))
    print("stage says delivery x VIN rule says delivery x has delivery date:", dict(Counter(
        ("stage" if r["in_delivery"] else "-") + "/" + ("vin" if r["vin_delivery"] else "-") + "/" + ("date" if r["delivery_month"] else "-")
        for r in rows)))
    print("closed:", dict(Counter(("cancelled or duplicate" if r["cancelled"] else "delivery file closed") for r in rows if r["is_closed"])))
    print("cancelled or duplicate among 'Bookings in Nov':", sum(1 for r in rows if r["booking_in_period"] and r["cancelled"]))
    per = {}
    for r in rows:
        d = per.setdefault(r["dealer_name"], Counter())
        if r["cancelled"]:
            d["cancelled or duplicate"] += 1
            continue
        delivery = bool(r["vin_delivery"] or r["delivery_month"])
        if r["booking_in_period"]:
            d["booked in Nov"] += 1
            d["booked in Nov, now a delivery" if delivery else "booked in Nov, still a booking"] += 1
        if r["delivery_in_period"]:
            d["delivery date in Nov"] += 1
        if delivery and not r["delivery_in_period"]:
            d["a delivery, date not in Nov"] += 1
    for name, d in sorted(per.items()):
        print("  ", name, dict(d))

    print("\n=== B. DMS report (as of today) ===")
    report = build_outlet_report(c, tenant_id=T, dealers=dealers, day=today, today=today)
    keep = ("bookingDms", "bookingSynced", "bookingNotInVerigence", "bookingCancelled", "bookingInVerigence", "bookingNotInDms",
            "bookingInDmsElsewhere", "bookingAfterDump", "bookingNotCompared", "bookingDatedByReceipt", "bookingDateNotRead",
            "deliveryDump", "deliverySynced", "deliveryNotUploaded", "deliveryNotInVerigence", "deliveryInVerigence",
            "deliveryNotInDms", "deliveryNotCompared", "deliveryDatedWithoutGatePass", "gateRegister")
    for d in report["dealers"]:
        print(d["dealerName"], "| booking dump", d["bookingDumpDate"], "bookings to", d["bookingsTo"],
              "| delivery dump", d["deliveryDumpDate"], "deliveries to", d["deliveriesTo"])
        print("   ", j({k: d["figures"].get(k) for k in keep}))
    print("PROJECT", j({k: report["figures"].get(k) for k in keep}))

    print("\n=== C. Project Tracker, month ===")
    tracker = build_project_tracker(c, tenant_id=T, actor_id=actor, period="MONTH", day=today, admin=True)
    print("period", tracker["period"]["first"], "to", tracker["period"]["last"])
    up = tracker["totals"]["uploads"]
    print("PROJECT", j({k: up[k] for k in ("bookings", "deliveries", "updated", "journeys", "newJourneys")}),
          "| gate register", j({k: tracker["totals"]["gatePass"][k] for k in ("onRegister", "uploadedInTool")}),
          "| delivery files closed", tracker["totals"]["closed"]["deliveryFiles"])
    for d in tracker["dealers"]:
        print("   dealer keys:", sorted(k for k in d.keys())[:14])
        figures = d.get("figures") or d.get("totals") or d
        u = figures.get("uploads") or {}
        print("  ", d.get("dealerName") or d.get("name"), j({k: u.get(k) for k in ("bookings", "deliveries", "updated", "newJourneys")}),
              "| gate register", (figures.get("gatePass") or {}).get("onRegister"),
              "| files closed", (figures.get("closed") or {}).get("deliveryFiles"))
