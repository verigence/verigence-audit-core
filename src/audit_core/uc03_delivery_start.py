"""When a Journey's delivery has started (product owner, 2026-10-28).

The price variance is shown, delivery documents and PC questions are asked
for, and the "deal below standard" finding is raised only once the delivery has
started. It used to start the moment any page was filed as a delivery
document: a UPI screenshot, an extra receipt, a cost sheet or a GST certificate
uploaded with the booking papers started it, and it never stopped once that
page was taken off or filed under the booking. 119 of 346 "started" bookings
of Mahindra Orissa had no delivery document but those.

Delivery has started when the Journey has an active delivery document that is
not one of ``NOT_A_DELIVERY_START`` -- payment proofs, the cost sheet, GST
certificate or declaration and the purchase order, which come with the booking
as often as with the delivery -- or the car has been delivered. A delivery
record on its own (one an upload opened) is not a start.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

NOT_A_DELIVERY_START: tuple[str, ...] = (
    "dealer_receipt",
    "payment_receipt",
    "upi_screenshot",
    "bank_statement_extract",
    "cost_sheet",
    "gst_certificate",
    "gst_declaration",
    "purchase_order",
)

_TYPES = "ARRAY[" + ", ".join(f"'{t}'" for t in NOT_A_DELIVERY_START) + "]::text[]"

# The papers of the vehicle being sold that print its VIN / chassis -- never the exchange
# vehicle's (its RC, valuation report, scrappage certificate come with the booking).
VEHICLE_PAPERS: tuple[str, ...] = (
    "customer_invoice_dms", "customer_invoice_tally", "tax_invoice_dms", "tax_invoice_tally", "customer_ledger",
    "form_21",
    "gate_pass", "accessory_invoice_dms", "accessory_invoice_tally", "insurance_cover", "rto_challan",
    "ew_invoice", "rsa_invoice", "delivery_order_cover",
)
_PAPERS = "ARRAY[" + ", ".join(f"'{t}'" for t in VEHICLE_PAPERS) + "]::text[]"

# The delivery rule (product owner, 2026-11-21): a Journey is a delivery in Verigence when a
# VIN or chassis number of its own vehicle is on it; with none it is a booking only. The
# Delivery stage row is not one (it opens within minutes of a booking upload), nor is a bank
# approval letter, an authorization letter or an exchange vehicle's RC filed under the
# delivery. A delivery a Reviewer completed by hand stays one.
# {t} / {j}: the tenant and journey expressions of the surrounding statement. No other brace
# in it: it is embedded in statements that are .format()-ed again.
DELIVERY_STARTED_SQL = """(
    EXISTS (
      SELECT 1 FROM auditcore.journey_document_extracted_fields f
      JOIN auditcore.evidence e
        ON e.tenant_id=f.tenant_id AND e.journey_id=f.journey_id AND e.di_document_id=f.di_document_id
       AND e.association_status='ACTIVE'
      WHERE f.tenant_id={t} AND f.journey_id={j}
        AND f.field_key IN ('vin_number', 'chassis_number')
        AND COALESCE(e.document_type_key, f.source_document_type_key) = ANY(""" + _PAPERS + """)
        AND length(regexp_replace(upper(f.effective_value #>> ARRAY[]::text[]), '[^A-Z0-9]', '', 'g')) >= 6
        AND (f.effective_value #>> ARRAY[]::text[]) ~ '[0-9]'
    ) OR EXISTS (
      SELECT 1 FROM auditcore.p2_journey_runtime r
      WHERE r.tenant_id={t} AND r.journey_id={j} AND r.delivery_completion_state='COMPLETE'
    )
)"""


def starts_delivery(canonical_type: str | None) -> bool:
    """Whether a page of this type, filed under the delivery, starts it."""
    return bool(canonical_type) and canonical_type not in NOT_A_DELIVERY_START


def delivery_has_started(connection: Connection, *, tenant_id: str, journey_id: UUID) -> bool:
    return bool(connection.execute(
        text("SELECT " + DELIVERY_STARTED_SQL.format(t=":tenant_id", j=":journey_id")),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one())


def delivery_started_ids(connection: Connection, *, tenant_id: str, journey_ids: list[Any]) -> set[str]:
    """The Journeys among ``journey_ids`` whose delivery has started."""
    if not journey_ids:
        return set()
    return {str(j) for j in connection.execute(
        text("SELECT x.journey_id FROM unnest(CAST(:ids AS uuid[])) AS x(journey_id) WHERE "
             + DELIVERY_STARTED_SQL.format(t=":t", j="x.journey_id")),
        {"t": tenant_id, "ids": [str(j) for j in journey_ids]},
    ).scalars().all()}
