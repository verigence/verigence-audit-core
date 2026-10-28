"""Whose RC is it: the exchange vehicle's or the new car's (2026-10-28).

The checklist's "Exchange Vehicle RC" (vehicle_rc) is the old vehicle's
registration, the proof of an exchange. Dealers also upload the NEW car's RC --
often the Temporary Certificate of Registration issued at delivery
("T1026OR9323A") -- and the reader types both as vehicle_rc. Taken as the
exchange RC, the new car's RC met the exchange scheme's old-vehicle condition and
put an exchange bonus on the Deal (journey ADI-BM-261026-002: a Bolero Maxx
pick-up's own temporary RC made a Rs 35,000 exchange bonus "eligible").

An RC is the new car's when any of these holds:
- its registration number is a temporary one: T + month/year + state + number;
- its chassis or engine number is the one another document of this Journey
  (invoice, insurance, delivery order, challan) gives for the car sold;
- it was registered on or after the booking date (an exchange vehicle is
  registered years before it is traded in).

Such an RC stays on file; it is not the exchange RC: it does not meet the
checklist entry, prove an exchange or scrappage bonus, or describe an old
vehicle.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import Connection, text

RC_TYPE = "vehicle_rc"
# How the card names it, so nobody takes it for the exchange RC.
NEW_VEHICLE_RC_NAME = "RC of the car sold (not the exchange RC)"
# Documents that describe the old vehicle, not the one sold.
_OLD_VEHICLE_TYPES = ("vehicle_rc", "valuation_report", "scrappage_certificate_of_deposit", "transfer_letter",
                      "no_dues_certificate")


def new_vehicle_rc_sql(evidence: str = "e") -> str:
    """SQL: true when the evidence row ``evidence`` is an RC of the car sold.
    Uses the bound parameters of the surrounding statement only through the
    evidence row itself."""
    e = evidence
    norm = "upper(regexp_replace(COALESCE({v} #>> '{{}}', ''), '[^A-Za-z0-9]', '', 'g'))"
    fv = norm.format(v="rcf.effective_value")
    gv = norm.format(v="other.effective_value")
    return f"""(
      {e}.document_type_key = '{RC_TYPE}' AND EXISTS (
        SELECT 1 FROM auditcore.journey_document_extracted_fields rcf
        WHERE rcf.tenant_id = {e}.tenant_id AND rcf.journey_id = {e}.journey_id
          AND rcf.di_document_id = {e}.di_document_id
          AND (
            (rcf.field_key = 'vehicle_registration_number' AND {fv} ~ '^T[0-9]{{4}}[A-Z]{{2}}[0-9]')
            OR (rcf.field_key IN ('chassis_number', 'engine_number') AND length({fv}) >= 6 AND EXISTS (
                  SELECT 1 FROM auditcore.journey_document_extracted_fields other
                  JOIN auditcore.evidence oe
                    ON oe.tenant_id = other.tenant_id AND oe.journey_id = other.journey_id
                   AND oe.di_document_id = other.di_document_id AND oe.association_status = 'ACTIVE'
                  WHERE other.tenant_id = {e}.tenant_id AND other.journey_id = {e}.journey_id
                    AND other.di_document_id <> {e}.di_document_id
                    AND oe.document_type_key <> ALL(ARRAY{list(_OLD_VEHICLE_TYPES)}::text[])
                    AND other.field_key = rcf.field_key
                    AND {gv} = {fv}))
            OR (rcf.field_key = 'registration_date'
                AND (rcf.effective_value #>> '{{}}') ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'
                AND (rcf.effective_value #>> '{{}}')::date >= COALESCE(
                      (SELECT b.pricing_effective_on FROM auditcore.bookings b
                        WHERE b.tenant_id = {e}.tenant_id AND b.journey_id = {e}.journey_id
                        ORDER BY b.pricing_effective_on NULLS LAST LIMIT 1),
                      DATE '2026-07-01'))
          )
      )
    )"""


def new_vehicle_rcs(connection: Connection, *, tenant_id: str, journey_id: UUID) -> set[str]:
    """The di_document_ids of the Journey's active RCs that are the car sold's."""
    return {str(d) for d in connection.execute(
        text(f"""
            SELECT e.di_document_id FROM auditcore.evidence e
            WHERE e.tenant_id = :t AND e.journey_id = :j AND e.association_status = 'ACTIVE'
              AND e.document_type_key = '{RC_TYPE}' AND {new_vehicle_rc_sql('e')}
        """),
        {"t": tenant_id, "j": journey_id},
    ).scalars().all()}


def exchange_rc_on_file(connection: Connection, *, tenant_id: str, journey_id: UUID) -> bool:
    """Whether an RC that is not the car sold's is on file."""
    return bool(connection.execute(
        text(f"""
            SELECT EXISTS (SELECT 1 FROM auditcore.evidence e
            WHERE e.tenant_id = :t AND e.journey_id = :j AND e.association_status = 'ACTIVE'
              AND e.document_type_key = '{RC_TYPE}' AND NOT {new_vehicle_rc_sql('e')})
        """),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one())
