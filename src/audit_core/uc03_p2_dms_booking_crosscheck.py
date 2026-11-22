"""uc03_p2_dms_booking_crosscheck.py -- a booking in Verigence that no DMS row found, and why.

Product owner 2026-11-22: "whatever is in DMS has to go in Verigence.. Verigence can't have
additional; if it is additional, we need to cross check where is the issue in match and then
find out". The matcher goes by the OTF / booking number first, then the mobile, then the
customer's name with the model or the booking date (uc03_p2_dms_dumps.match_row). Every
Verigence booking of a dump's days that no row of the dump found is checked here against the
dump's rows the same way, loosened, and given the reason it was not matched and the nearest
DMS row:

  OTF_ON_ANOTHER_JOURNEY   a DMS row has this OTF, but it matched another Journey: two
                           Journeys carry one OTF (a duplicate, or an OTF read wrong)
  OTF_CANCELLED            the DMS has this OTF as cancelled
  OTF_MISREAD              a DMS row's OTF is one or two digits off this Journey's
  MOBILE_ON_ANOTHER_JOURNEY a DMS row has this mobile, matched to another Journey
  MOBILE_MODEL_DIFFERS     a DMS row has this mobile, not matched: the model differs or
                           several Journeys share the mobile
  NAME_ON_ANOTHER_JOURNEY  a DMS row has this customer's name, matched to another Journey
  NAME_MODEL_DIFFERS       a DMS row has this customer's name, not matched: model and date differ
  MOBILE_MISREAD           a DMS row's mobile is one or two digits off this Journey's
  OTF_NOT_READ             no OTF / booking number read on the Journey, and nothing else agrees
  NOT_IN_DMS               no DMS row has its OTF, mobile or name: not entered in the DMS for
                           these days, or entered under another name and number
  DUPLICATE_JOURNEY        another Verigence Journey carries the same OTF, or the same customer
                           with the same mobile or model (2026-11-22): one sale on two Journeys,
                           or one Journey holding another's OTF -- the DMS row can go to only one

Not looked for (2026-11-22): a booking dated after the last day the dump's rows reach (the DMS
could not have it yet), and a Journey the DMS delivery dump found (retailed: it is in the DMS).
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Connection, text

from audit_core.uc03_p2_dms_dumps import (
    BOOKING,
    booking_keys,
    build_index,
    compared_until,
    dms_dates,
    dms_delivery_bookings,
    dump_window,
    mobile_key,
    near_booking,
    near_mobile,
    out_of_scope_model,
    same_model,
)
from audit_core.uc03_p2_names import same_person

REASONS: dict[str, str] = {
    "OTF_ON_ANOTHER_JOURNEY": "OTF matched to another journey",
    "OTF_CANCELLED": "OTF cancelled in the DMS",
    "OTF_MISREAD": "OTF read wrong (1-2 digits off)",
    "MOBILE_ON_ANOTHER_JOURNEY": "Mobile matched to another journey",
    "MOBILE_MODEL_DIFFERS": "Mobile in the DMS, model differs",
    "NAME_ON_ANOTHER_JOURNEY": "Name matched to another journey",
    "NAME_MODEL_DIFFERS": "Name in the DMS, model and date differ",
    "MOBILE_MISREAD": "Mobile read wrong (1-2 digits off)",
    "OTF_NOT_READ": "No OTF read, nothing else agrees",
    "NOT_IN_DMS": "Not in the DMS booking dump",
    "DUPLICATE_JOURNEY": "Duplicate journey in Verigence",
}
# Reasons a duplicate Journey replaces: the DMS row's own verdict (cancelled, OTF one digit off)
# stands.
_DUPLICATE_KEEPS = frozenset({"OTF_CANCELLED", "OTF_MISREAD"})
# The same customer's two Journeys count as one sale only within these days of each other.
DUPLICATE_DAYS = 31

_ROWS_SQL = """
    SELECT r.row_no, r.outcome, CAST(r.journey_id AS text) AS journey_id, r.customer_name, r.mobile,
           r.booking_number, r.dms_booking_number, r.model, r.event_date, j.journey_reference AS matched_to
    FROM auditcore.p2_dms_dump_rows r
    LEFT JOIN auditcore.journeys j ON j.tenant_id = r.tenant_id AND j.journey_id = r.journey_id
    WHERE r.tenant_id = :t AND r.dump_id = :id
    ORDER BY r.row_no
"""


def _day(journey: dict[str, Any], today: date) -> date | None:
    """The booking date (uc03_booking_day): the booking form's, else the first money receipt's."""
    for day in (journey.get("bookingDate"), journey.get("firstReceipt")):
        if day is not None and day <= today:
            return day
    return None


def duplicate_of(journey: dict[str, Any], journeys: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Another Journey of the dealership holding the same sale: the same OTF / booking number,
    or the same customer (by name) with the same mobile or the same model. A customer buying two
    different models (a fleet buyer) is two sales, not a duplicate."""
    mine, refs, mobiles = journey["journeyId"], journey.get("refs") or set(), journey.get("mobiles") or set()
    names, model = journey.get("names") or set(), journey.get("model")
    for jid, other in sorted(journeys.items(), key=lambda kv: str(kv[1].get("reference") or "")):
        if jid == mine or out_of_scope_model(other.get("model")):
            continue
        if refs & (other.get("refs") or set()):
            return other
        same_customer = any(same_person(a, b) for a in names for b in other.get("names") or set())
        if not same_customer:
            continue
        models_agree = not model or not other.get("model") or same_model(model, other.get("model"))
        # A common name on an unrelated booking months apart is not the same sale.
        days, other_days = journey.get("bookingDays") or set(), other.get("bookingDays") or set()
        near = not days or not other_days or any(abs((a - b).days) <= DUPLICATE_DAYS
                                                 for a in days for b in other_days)
        if models_agree and near and (mobiles & (other.get("mobiles") or set()) or (model and other.get("model"))):
            return other
    return None


def diagnose(journey: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[str, str, dict[str, Any] | None]:
    """(reason code, the reason in words, the nearest DMS row) of a Journey no row matched."""
    refs, mobiles, names = journey.get("refs") or set(), journey.get("mobiles") or set(), journey.get("names") or set()
    mine = journey["journeyId"]

    def other(row: dict[str, Any]) -> bool:
        return bool(row.get("journey_id")) and row["journey_id"] != mine

    def said(row: dict[str, Any]) -> str:
        bits = [f"DMS row {row['row_no']}", str(row.get("customer_name") or "")]
        if row.get("dms_booking_number") or row.get("booking_number"):
            bits.append(f"OTF {row.get('dms_booking_number') or row.get('booking_number')}")
        if row.get("model"):
            bits.append(str(row["model"]))
        return ", ".join(b for b in bits if b)

    keyed = [(row, booking_keys(row.get("booking_number"), row.get("dms_booking_number"))) for row in rows]
    # 1. The OTF / booking number.
    for row, keys in keyed:
        if keys & refs:
            if str(row.get("outcome") or "") == "CANCELLED":
                return "OTF_CANCELLED", f"{said(row)}: cancelled in the DMS.", row
            if other(row):
                return ("OTF_ON_ANOTHER_JOURNEY", (f"{said(row)} has this OTF and matched {row.get('matched_to')}: "
                        "two journeys carry one OTF -- a duplicate, or one OTF read wrong."), row)
    for row, keys in keyed:
        if keys and refs and near_booking(keys, refs) and (not names or any(
                same_person(row.get("customer_name"), n) for n in names)):
            return ("OTF_MISREAD", (f"{said(row)}: the OTF read on the journey ({', '.join(sorted(refs))}) is one or "
                    "two digits off."), row)
    # 2. The mobile.
    for row in rows:
        if (key := mobile_key(row.get("mobile"))) is not None and key in mobiles:
            if other(row):
                return ("MOBILE_ON_ANOTHER_JOURNEY", (f"{said(row)} has this mobile and matched "
                        f"{row.get('matched_to')}: the same customer booked twice, or a duplicate journey."), row)
            return ("MOBILE_MODEL_DIFFERS", (f"{said(row)} has this mobile; the model differs (Verigence "
                    f"{journey.get('model') or 'not read'}) or several journeys share the mobile."), row)
    # 3. The customer's name, with the model or the booking date.
    named = [row for row in rows if any(same_person(row.get("customer_name"), n) for n in names)]
    for row in named:
        if other(row) and same_model(journey.get("model"), row.get("model")):
            return ("NAME_ON_ANOTHER_JOURNEY", (f"{said(row)}: the same name and model, matched "
                    f"{row.get('matched_to')}."), row)
    if named:
        row = named[0]
        return ("NAME_MODEL_DIFFERS", (f"{said(row)}: the same name; the model (Verigence "
                f"{journey.get('model') or 'not read'}) and the booking date do not agree."), row)
    for row in rows:
        if (key := mobile_key(row.get("mobile"))) is not None and near_mobile(key, mobiles):
            return "MOBILE_MISREAD", f"{said(row)}: its mobile is one or two digits off the journey's.", row
    if not refs:
        return ("OTF_NOT_READ", "No OTF / booking number read on the journey, and no DMS row has its mobile or name.",
                None)
    return ("NOT_IN_DMS", ("No DMS row has its OTF, mobile or name: not entered in the DMS for these days, or "
            "entered under another name and number."), None)


def bookings_not_in_dump(connection: Connection, *, tenant_id: str, dump: dict[str, Any], today: date
                         ) -> list[dict[str, Any]]:
    """A booking dump's other side: the dealership's bookings dated (booking form, else first
    receipt) from the 1st of the dump's month to its upload day that no row of the dump found,
    each with the reason and the nearest DMS row. ``dump``: dump_id, dealer_id, period_month,
    business_date."""
    month = dump.get("period_month") or dump["business_date"].replace(day=1)
    first, _ = dump_window(month, dump["business_date"])
    # Up to the last day the dump's rows reach, not its upload day (2026-11-22).
    last = min(compared_until(connection, tenant_id, {**dump, "period_month": month}), today)
    rows = [dict(r) for r in connection.execute(text(_ROWS_SQL), {"t": tenant_id, "id": str(dump["dump_id"])})
            .mappings()]
    found = {r["journey_id"] for r in rows if r.get("journey_id")}
    # A booking the DMS dates outside these days (in another dump) is not looked for in this one.
    dated = dms_dates(connection, tenant_id, str(dump["dealer_id"]), dump["business_date"])[BOOKING]
    # Retailed: the DMS delivery dump found it -- in the DMS (2026-11-22).
    retailed = dms_delivery_bookings(connection, tenant_id, str(dump["dealer_id"]), dump["business_date"])
    outlets = {str(r[0]): r[1] for r in connection.execute(
        text("SELECT outlet_id, outlet_name FROM auditcore.dealer_outlets WHERE tenant_id = :t AND dealer_id = :d"),
        {"t": tenant_id, "d": str(dump["dealer_id"])})}
    index = build_index(connection, tenant_id=tenant_id, dealer_id=dump["dealer_id"])
    out = []
    for jid, journey in index.journeys.items():
        if jid in found or jid in dated or jid in retailed or out_of_scope_model(journey.get("model")):
            continue
        day = _day(journey, today)
        if day is None or not first <= day <= last:
            continue
        code, why, near = diagnose(journey, rows)
        if code not in _DUPLICATE_KEEPS and (twin := duplicate_of(journey, index.journeys)) is not None:
            code, why = "DUPLICATE_JOURNEY", (
                f"Also on {twin.get('reference')} ({twin.get('customer') or 'name not read'}"
                f"{', ' + str(twin['model']) if twin.get('model') else ''}): one sale on two journeys, or one "
                "journey holding the other's OTF -- the DMS row can go to only one. Keep one, remove the other.")
        out.append({
            "journeyId": jid, "journeyReference": journey.get("reference"),
            "outletName": outlets.get(str(journey.get("outletId") or "")), "bookingDate": day.isoformat(),
            "datedBy": "Booking form" if journey.get("bookingDate") == day else "First money receipt",
            "customerName": journey.get("customer"), "mobile": ", ".join(sorted(journey.get("mobiles") or [])),
            "otf": ", ".join(sorted(journey.get("refs") or [])), "model": journey.get("model"),
            "reasonCode": code, "reason": REASONS[code], "detail": why,
            "dmsRow": near.get("row_no") if near else None,
            "dmsCustomer": near.get("customer_name") if near else None,
            "dmsOtf": (near.get("dms_booking_number") or near.get("booking_number")) if near else None,
            "dmsMobile": near.get("mobile") if near else None, "dmsModel": near.get("model") if near else None,
            "dmsDate": near["event_date"].isoformat() if near and near.get("event_date") else None,
            "dmsMatchedTo": near.get("matched_to") if near else None,
            "dumpDate": dump["business_date"].isoformat(),
        })
    return sorted(out, key=lambda r: (r["reasonCode"], r["bookingDate"], r["journeyReference"] or ""))


def store_not_in_dms(connection: Connection, *, tenant_id: str, report_id: str, report_date: date,
                     report: dict[str, Any]) -> int:
    """The report's "In Verigence, not in DMS" bookings onto their Journeys
    (p2_journey_not_in_dms), for the list's "Not in DMS" switch (product owner 2026-11-22).
    Per dealership with a booking dump in the report, and only when no newer report covers it:
    its Journeys listed now are switched on with their reason, the others switched off."""
    stored = 0
    listed: dict[str, list[dict[str, Any]]] = {}
    for row in report.get("notInDmsBookings") or []:
        if row.get("dealerId") and row.get("journeyId"):
            listed.setdefault(str(row["dealerId"]), []).append(row)
    for dealer in report.get("dealers") or []:
        did = str(dealer.get("dealerId") or "")
        if not did or not dealer.get("bookingDumpDate"):
            continue
        newest = connection.execute(text("""
            SELECT max(report_date) FROM auditcore.p2_dms_reports
            WHERE tenant_id = :t AND CAST(:d AS text) = ANY(CAST(dealer_ids AS text[]))
        """), {"t": tenant_id, "d": did}).scalar()
        if newest is not None and newest > report_date:
            continue  # a newer report of the dealership decides
        connection.execute(text("""
            UPDATE auditcore.p2_journey_not_in_dms SET active = false, updated_at_utc = now()
            WHERE tenant_id = :t AND dealer_id = CAST(:d AS uuid) AND active
        """), {"t": tenant_id, "d": did})
        for row in listed.get(did, []):
            connection.execute(text("""
                INSERT INTO auditcore.p2_journey_not_in_dms (tenant_id, journey_id, dealer_id, report_id,
                  report_date, dump_date, booking_date, reason_code, reason, detail, active)
                VALUES (:t, CAST(:j AS uuid), CAST(:d AS uuid), CAST(:r AS uuid), :rd, :dd, :bd, :c, :why, :detail, true)
                ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                  dealer_id = EXCLUDED.dealer_id, report_id = EXCLUDED.report_id,
                  report_date = EXCLUDED.report_date, dump_date = EXCLUDED.dump_date,
                  booking_date = EXCLUDED.booking_date, reason_code = EXCLUDED.reason_code,
                  reason = EXCLUDED.reason, detail = EXCLUDED.detail, active = true, updated_at_utc = now()
            """), {"t": tenant_id, "j": str(row["journeyId"]), "d": did, "r": report_id, "rd": report_date,
                   "dd": row.get("dumpDate"), "bd": row.get("bookingDate"), "c": row["reasonCode"],
                   "why": row.get("reason") or REASONS.get(row["reasonCode"], row["reasonCode"]),
                   "detail": row.get("detail")})
            stored += 1
    return stored


__all__ = ["REASONS", "bookings_not_in_dump", "diagnose", "duplicate_of", "store_not_in_dms"]
