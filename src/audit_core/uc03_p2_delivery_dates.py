"""uc03_p2_delivery_dates.py -- a delivery Journey's delivery date, for comparing Verigence
with the dealers' DMS delivery dumps (product owner 2026-11-17).

"when the dump is uploaded daily for the given month, then we need to consider only those
deliveries in verigence which has any one document with delivery date from the start of the
month till that day of the month. if gate pass is not provided for some deliveries, then
payment receipt for delivery, or NDC date can be considered."

A Journey's delivery day, first that applies:

GATE_PASS   the vehicle's gate pass (its delivery date, each gate pass on file: the
            Journey is in a period when any of them is). A Sunday delivery while Sunday
            was off (before 15 Nov 2026) counts on the Monday, as the gate register does.
NDC         no gate pass with a delivery date read (none on file, or its date unread or
            after today), the delivery files are (the delivery stage started, or
            the delivery recorded): the delivery date the NDC / account statement prints
            (customer_ledger). Live 17 Nov 2026: printed on few NDCs; the NDC's invoice
            date is not a delivery date (9 of 452 on the gate pass's day) and is not used.
RECEIPT     neither: the latest payment receipt on file -- the payment taken at
            delivery (in the gate pass's month on 535 of 680 Journeys that have both).

A Journey with no delivery files has no delivery day, whatever its receipts: Shivnath's
offer letters are read as the NDC at booking, so an NDC on file is not a delivery. A day
after today is a misreading and is not used.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Connection, text

GATE_PASS, NDC, RECEIPT = "GATE_PASS", "NDC", "RECEIPT"
SOURCE_LABELS = {GATE_PASS: "Gate pass", NDC: "NDC delivery date", RECEIPT: "Latest payment receipt"}


@dataclass
class Delivery:
    journey_id: str
    outlet_id: str | None
    dealer_id: str | None
    source: str
    days: list[date] = field(default_factory=list)
    reference: str | None = None
    customer: str | None = None
    mobile: str | None = None
    chassis: str | None = None
    model: str | None = None
    deal_type: str | None = None

    def day_in(self, first: date, last: date) -> date | None:
        """The delivery day this Journey counts on within first..last, or None."""
        from audit_core.task_manager import sunday_is_off

        for day in sorted(self.days, reverse=True):
            counted = day + timedelta(days=1) if self.source == GATE_PASS and sunday_is_off(day) else day
            if first <= counted <= last:
                return counted
        return None


_SQL = """
    WITH scope AS (
        SELECT j.journey_id, j.outlet_id, j.dealer_id, j.journey_reference, j.customer_id
        FROM auditcore.journeys j
        WHERE j.tenant_id = :t
          AND (CAST(:ids AS text[]) IS NULL OR CAST(j.outlet_id AS text) = ANY(CAST(:ids AS text[])))
          AND (CAST(:d AS text) IS NULL OR CAST(j.dealer_id AS text) = CAST(:d AS text))
    ),
    dated AS (
        SELECT DISTINCT ON (f.journey_id, f.di_document_id, doc)
               f.journey_id, COALESCE(e.document_type_key, f.source_document_type_key) AS doc,
               f.effective_value #>> '{}' AS v
        FROM auditcore.journey_document_extracted_fields f
        JOIN scope s ON s.journey_id = f.journey_id
        JOIN auditcore.evidence e
          ON e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id
         AND e.di_document_id = f.di_document_id AND e.association_status = 'ACTIVE'
        WHERE f.tenant_id = :t AND f.field_key = 'delivery_date'
          AND COALESCE(e.document_type_key, f.source_document_type_key) IN ('gate_pass', 'customer_ledger')
        ORDER BY f.journey_id, f.di_document_id, doc, f.source_fact_version DESC NULLS LAST, f.updated_at_utc DESC
    ),
    days AS (
        SELECT journey_id,
               array_agg(DISTINCT CAST(v AS date)) FILTER (WHERE doc = 'gate_pass') AS gate_pass,
               max(CAST(v AS date)) FILTER (WHERE doc = 'customer_ledger' AND CAST(v AS date) <= CAST(:today AS date))
                 AS ndc
        FROM dated WHERE v ~ '^\\d{4}-\\d{2}-\\d{2}$'
        GROUP BY journey_id
    ),
    receipts AS (
        SELECT p.journey_id, max(COALESCE(p.receipt_date, p.payment_reference_date)) AS day
        FROM auditcore.payments p
        JOIN scope s ON s.journey_id = p.journey_id
        JOIN auditcore.evidence e
          ON e.tenant_id = p.tenant_id AND e.journey_id = p.journey_id AND e.di_document_id = p.source_di_document_id
         AND e.association_status = 'ACTIVE'
        WHERE p.tenant_id = :t AND p.status_source = 'EVIDENCE' AND p.amount > 0
          AND COALESCE(p.receipt_date, p.payment_reference_date) <= CAST(:today AS date)
        GROUP BY p.journey_id
    )
    SELECT s.journey_id, s.outlet_id, s.dealer_id, s.journey_reference,
           COALESCE(NULLIF(c.legal_name, ''),
                    CASE WHEN c.display_name ~* '^[0-9a-f]{8}-[0-9a-f]{4}-' THEN NULL ELSE c.display_name END)
             AS customer, c.mobile_number,
           (SELECT COALESCE(vr.vin, vr.chassis_number) FROM auditcore.vehicle_records vr
            WHERE vr.tenant_id = :t AND vr.journey_id = s.journey_id LIMIT 1) AS chassis,
           (SELECT jp.model_name_snapshot FROM auditcore.journey_products jp
            WHERE jp.tenant_id = :t AND jp.journey_id = s.journey_id LIMIT 1) AS model,
           cl.deal_type, dy.gate_pass, dy.ndc, r.day AS receipt,
           (EXISTS (SELECT 1 FROM auditcore.deliveries dl
                    WHERE dl.tenant_id = :t AND dl.journey_id = s.journey_id AND dl.actual_delivered_at IS NOT NULL)
            OR EXISTS (SELECT 1 FROM auditcore.journey_stage_states ds
                       WHERE ds.tenant_id = :t AND ds.journey_id = s.journey_id AND ds.stage_code = 'DELIVERY'
                         AND ds.first_started_at_utc IS NOT NULL)) AS has_delivery
    FROM scope s
    LEFT JOIN days dy ON dy.journey_id = s.journey_id
    LEFT JOIN receipts r ON r.journey_id = s.journey_id
    LEFT JOIN auditcore.customers c ON c.tenant_id = :t AND c.customer_id = s.customer_id
    LEFT JOIN auditcore.journey_classifications cl ON cl.tenant_id = :t AND cl.journey_id = s.journey_id
"""


def deliveries(connection: Connection, *, tenant_id: str, today: date, outlet_ids: list[str] | None = None,
               dealer_id: str | None = None) -> dict[str, Delivery]:
    """Every delivery Journey of the outlets (or the dealership) with its delivery day(s)."""
    out: dict[str, Delivery] = {}
    for r in connection.execute(text(_SQL), {"t": tenant_id, "ids": outlet_ids, "d": dealer_id,
                                             "today": today}).mappings():
        gate_pass = [d for d in (r["gate_pass"] or []) if d is not None and d <= today]
        if gate_pass:
            source, days = GATE_PASS, gate_pass
        elif not r["has_delivery"]:
            continue
        elif r["ndc"] is not None:
            source, days = NDC, [r["ndc"]]
        elif r["receipt"] is not None:
            source, days = RECEIPT, [r["receipt"]]
        else:
            continue
        jid = str(r["journey_id"])
        out[jid] = Delivery(
            journey_id=jid, outlet_id=str(r["outlet_id"]) if r["outlet_id"] else None,
            dealer_id=str(r["dealer_id"]) if r["dealer_id"] else None, source=source, days=days,
            reference=r["journey_reference"], customer=r["customer"], mobile=r["mobile_number"],
            chassis=r["chassis"], model=r["model"], deal_type=r["deal_type"])
    return out


def in_period(found: dict[str, Delivery], first: date, last: date) -> dict[str, tuple[Delivery, date]]:
    """The deliveries that count in first..last, with the day each counts on."""
    out = {}
    for jid, delivery in found.items():
        day = delivery.day_in(first, last)
        if day is not None:
            out[jid] = (delivery, day)
    return out


def verigence_not_in_dump(connection: Connection, *, tenant_id: str, dump: dict[str, Any], today: date
                          ) -> list[dict[str, Any]]:
    """A delivery dump's other side: the dealership's delivery Journeys dated from the 1st of
    the dump's month to the last day the dump covers that no row of the dump found. ``dump``:
    dump_id, dealer_id, period_month, business_date."""
    first = dump["period_month"]
    covers_to = connection.execute(
        text("SELECT max(event_date) FROM auditcore.p2_dms_dump_rows WHERE tenant_id = :t AND dump_id = :id "
             "AND event_date <= :b"),
        {"t": tenant_id, "id": str(dump["dump_id"]), "b": dump["business_date"]},
    ).scalar()
    last = max(first, covers_to) if covers_to else min(dump["business_date"], today)
    found = set(connection.execute(
        text("SELECT DISTINCT CAST(journey_id AS text) FROM auditcore.p2_dms_dump_rows "
             "WHERE tenant_id = :t AND dump_id = :id AND journey_id IS NOT NULL"),
        {"t": tenant_id, "id": str(dump["dump_id"])},
    ).scalars().all())
    names = {str(r[0]): r[1] for r in connection.execute(
        text("SELECT outlet_id, outlet_name FROM auditcore.dealer_outlets WHERE tenant_id = :t AND dealer_id = :d"),
        {"t": tenant_id, "d": str(dump["dealer_id"])})}
    counted = in_period(deliveries(connection, tenant_id=tenant_id, today=today, dealer_id=str(dump["dealer_id"])),
                        first, last)
    rows = []
    for jid, (d, day) in sorted(counted.items(), key=lambda x: (x[1][1], x[1][0].reference or "")):
        if jid in found:
            continue
        rows.append({
            "rowNo": None, "kind": "DELIVERY", "outcome": "NOT_IN_DMS", "matchBasis": d.source,
            "dealerName": None, "outletName": names.get(d.outlet_id or ""), "outletLabel": None,
            "eventDate": day.isoformat(), "bookingNumber": None, "otf": None, "customerName": d.customer,
            "mobile": d.mobile, "chassis": d.chassis, "invoiceNumber": None, "registrationNumber": None,
            "model": d.model, "variant": None, "stage": None, "consultant": None, "journeyId": jid,
            "journeyReference": d.reference, "dumpDate": dump["business_date"].isoformat(),
        })
    return rows


__all__ = ["GATE_PASS", "NDC", "RECEIPT", "SOURCE_LABELS", "Delivery", "deliveries", "in_period",
           "verigence_not_in_dump"]
