"""Deal Type, Customer Type and the insurance decision of a Journey: one rule each,
one stored copy (product owner 2026-11-11).

Deal Type -- Inscope, Outscope, MR, Lease or Bulk
    * The Team Lead's (or Reviewer's) call wins over the rule below ("TL task will hold
      precedence"): MR, Lease and Bulk are theirs alone, and they can also settle
      Inscope / Outscope -- for one Journey (``set_tl_deal_type``: Journey 360 or the
      DEAL_TYPE_CONFIRM task) or for several found by customer name or mobile number in
      one go (``set_deal_type_for_many``: a bulk order is many Journeys). A Journey the
      Team Lead opted into the Management Referral discount is MR as well.
    * Inscope: the booking date is on or after 1 Sep 2026 AND the vehicle is less than
      90 days old. Outscope: booked before 1 Sep 2026, or the vehicle is 90 days old or
      more.
    * The vehicle's age is read from its VIN. A Mahindra VIN (MA1...) carries the year
      of manufacture in its 10th character (A = 2010 ... T = 2026, no I, O, Q, U, Z)
      and the month in its 12th (A = January ... M = December, no I). Checked on
      Mahindra Orissa 2026-11-11: 496 Journeys with a 17-character VIN decode to
      April - October 2026. A VIN gives the month only, so the age is counted from the
      1st of that month, up to the vehicle's invoice date (the DMS invoice, else the
      Tally one), else its delivery date, else the booking date.
    * The VIN is the one a person entered, else the one most of the sale's own documents
      agree on (``agreed_vin``): never a single misread copy, never the customer's old
      vehicle's RC.
    * No VIN on file, or one that cannot be read (not 17 characters, not a Mahindra
      VIN, a month after today): the Deal Type is NOT filled until the VIN is provided
      (product owner 2026-11-11) -- except a booking before 1 Sep 2026, which is
      Outscope whatever the vehicle. Once delivery has started and there is still no
      readable VIN, the system task DEAL_TYPE_CONFIRM asks the Team Lead to set the
      Deal Type; it closes by itself when the VIN is read.

Customer Type -- Individual, Corporate or Company
    * Corporate: the customer has provided a GSTIN (a GST certificate on file, a GSTIN
      read on a GST certificate / declaration, or printed as the buyer's on a dealer
      invoice).
    * Company: a company ID card is on file AND a corporate discount was given.
    * Otherwise Individual.

Insurance -- Dealership or Self
    ``uc03_insurance_source.resolve`` is the rule; this keeps its answer. Until
    2026-11-11 only a PC's own answer was stored (108 of 795 Journeys in delivery on
    Mahindra Orissa); what the documents decide was worked out again on every screen,
    and every list reading the stored column saw nothing.

``refresh`` writes ``auditcore.journey_classifications`` on every recompute of a
Journey (the worker's rule pass) and after a Team Lead's or PC's own decision.
``run_journey_classification`` (p2_worker_jobs kind JOURNEY_CLASSIFICATION) fills
every Journey already on file, and corrects its vehicle record's VIN
(``correct_vehicle_record``): a DRY RUN unless the tag ends with "-commit".
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

logger = structlog.get_logger(__name__)

SCOPE_START = date(2026, 9, 1)
MAX_VEHICLE_AGE_DAYS = 90

INSCOPE, OUTSCOPE, MR, LEASE, BULK = "INSCOPE", "OUTSCOPE", "MR", "LEASE", "BULK"
TL_DEAL_TYPES = (INSCOPE, OUTSCOPE, MR, LEASE, BULK)
TASK_TYPE = "DEAL_TYPE_CONFIRM"
INDIVIDUAL, CORPORATE, COMPANY = "INDIVIDUAL", "CORPORATE", "COMPANY"

LABELS = {
    INSCOPE: "Inscope", OUTSCOPE: "Outscope", MR: "MR", LEASE: "Lease", BULK: "Bulk",
    INDIVIDUAL: "Individual", CORPORATE: "Corporate", COMPANY: "Company",
    "DEALERSHIP": "Dealership", "SELF": "Self",
}

# ── the VIN ───────────────────────────────────────────────────────────────────

# The model-year letters of a VIN's 10th character (ISO 3779), 2010 - 2030.
_YEAR_CODES = {code: 2010 + i for i, code in enumerate("ABCDEFGHJKLMNPRSTVWXY")}
# The month letters Mahindra prints in the 12th character: A = January ... M = December.
_MONTH_CODES = {code: i + 1 for i, code in enumerate("ABCDEFGHJKLM")}
_VIN = re.compile(r"^[A-Z0-9]{17}$")


def clean_vin(value: Any) -> str | None:
    """A 17-character VIN as printed, without spaces or separators; else None."""
    vin = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    return vin if _VIN.match(vin) else None


def vin_manufacture_month(value: Any, *, today: date | None = None) -> date | None:
    """The 1st of the month a Mahindra vehicle was made in, read from its VIN; None when
    the VIN is not a 17-character Mahindra one (MA1...), its year or month character is
    not a code, or it names a month after today (a misread)."""
    vin = clean_vin(value)
    if vin is None or not vin.startswith("MA1"):
        return None
    year, month = _YEAR_CODES.get(vin[9]), _MONTH_CODES.get(vin[11])
    if year is None or month is None:
        return None
    made = date(year, month, 1)
    return None if today is not None and made > today else made


# ── Deal Type ────────────────────────────────────────────────────────────────

def _day(value: date) -> str:
    return f"{value.day} {value:%b %Y}"


def decide_deal_type(
    *, booking_date: date | None, vin: str | None, manufacture_month: date | None,
    reference_date: date | None, reference_kind: str | None,
    tl_deal_type: str | None = None, tl_reason: str | None = None, mr_opted: bool = False,
) -> dict[str, Any]:
    """The Deal Type and what it was decided on (see the module docstring)."""
    out: dict[str, Any] = {
        "deal_type": INSCOPE, "deal_type_decided_by": "RULE", "deal_type_basis": None, "vin_pending": False,
        "booking_date_used": booking_date, "vin_used": vin, "vin_manufacture_month": manufacture_month,
        "age_reference_date": None, "age_reference_kind": None, "vehicle_age_days": None,
    }
    if manufacture_month is not None and reference_date is not None:
        out["age_reference_date"], out["age_reference_kind"] = reference_date, reference_kind
        out["vehicle_age_days"] = (reference_date - manufacture_month).days
    if tl_deal_type in TL_DEAL_TYPES:
        reason = f": {tl_reason}" if tl_reason else ""
        return {**out, "deal_type": tl_deal_type, "deal_type_decided_by": "TL",
                "deal_type_basis": f"Set by the Team Lead{reason}"}
    if mr_opted:
        return {**out, "deal_type": MR, "deal_type_decided_by": "TL",
                "deal_type_basis": "The Team Lead opted this Journey into the Management Referral discount."}
    if booking_date is not None and booking_date < SCOPE_START:
        return {**out, "deal_type": OUTSCOPE,
                "deal_type_basis": f"Booked on {_day(booking_date)}, before {_day(SCOPE_START)}."}
    booked = f"Booked on {_day(booking_date)}" if booking_date is not None else "No booking date yet"
    age = out["vehicle_age_days"]
    if age is None:
        # Not filled until the VIN is provided, or the Team Lead decides (2026-11-11).
        why = ("no VIN is on file yet" if vin is None
               else f"the VIN {vin} does not give a month of manufacture")
        return {**out, "deal_type": None, "deal_type_decided_by": None, "vin_pending": True,
                "deal_type_basis": f"{booked}; {why}. The Deal Type is filled when the VIN is read, "
                                   "or when the Team Lead sets it."}
    made = f"VIN {vin}: made {manufacture_month:%b %Y}, {age} days old on the {reference_kind} date " \
           f"{_day(reference_date)}"  # type: ignore[arg-type]
    if age >= MAX_VEHICLE_AGE_DAYS:
        return {**out, "deal_type": OUTSCOPE,
                "deal_type_basis": f"{made} ({MAX_VEHICLE_AGE_DAYS} days or more). {booked}."}
    return {**out, "deal_type_basis": f"{booked}. {made} (under {MAX_VEHICLE_AGE_DAYS} days)."}


# Papers that can be about the customer's OLD vehicle (an exchange or scrappage deal).
_OLD_VEHICLE_DOCUMENT_TYPES = ("vehicle_rc", "valuation_report", "transfer_letter",
                               "scrappage_certificate_of_deposit")


def journey_vin(connection: Connection, *, tenant_id: str, journey_id: UUID, today: date | None = None) -> str | None:
    """The Journey's VIN: the one a person entered, else the one most of the sale's own
    documents agree on (``agreed_vin``). None when neither gives a 17-character VIN."""
    params = {"t": tenant_id, "j": journey_id}
    entered = next((v for v in (clean_vin(x) for x in connection.execute(
        text(_ENTERED_VIN_SQL), params).scalars().all()) if v is not None), None)
    return entered or agreed_vin(
        [(r["document_type_key"], r["di_document_id"], r["value"]) for r in connection.execute(
            text(_DOCUMENT_VIN_SQL), {**params, "types": list(_SALE_DOCUMENT_TYPES)}).mappings().all()],
        today=today)


def correct_vehicle_record(connection: Connection, *, tenant_id: str, journey_id: UUID,
                           apply: bool = True) -> dict[str, Any] | None:
    """Bring the Journey's vehicle record (the VIN / chassis number Journey 360 and the VIN
    checks read) in line with ``journey_vin``. The record keeps the first value it was
    given, so a misread copy or the old vehicle's RC stayed on it for good (2026-11-11).

    * A VIN is known: it is the record's VIN; a chassis number that is another 17-character
      number, or a short one that is not the VIN's own ending, becomes the VIN too.
    * No VIN is known: a value the record holds that was read from an old-vehicle paper
      (an RC, valuation report, transfer letter, scrappage certificate) is taken off.

    Returns what changed (``apply=False``: what would), or None when nothing does."""
    params = {"t": tenant_id, "j": journey_id}
    record = connection.execute(
        text("SELECT vin, chassis_number FROM auditcore.vehicle_records WHERE tenant_id=:t AND journey_id=:j "
             + ("FOR UPDATE" if apply else "")), params).mappings().first()
    if record is None:
        return None
    vin = journey_vin(connection, tenant_id=tenant_id, journey_id=journey_id)
    new_vin, new_chassis = record["vin"], record["chassis_number"]
    if vin is not None:
        new_vin = vin
        chassis = re.sub(r"[^A-Z0-9]", "", str(record["chassis_number"] or "").upper())
        if not chassis or (len(chassis) >= 17 and chassis != vin) or (len(chassis) < 17 and not vin.endswith(chassis)):
            new_chassis = vin
    else:
        old_vehicle = {clean_vin(v) for v in connection.execute(
            text(_DOCUMENT_VIN_SQL), {**params, "types": list(_OLD_VEHICLE_DOCUMENT_TYPES)},
        ).mappings().all() for v in [v["value"]]} - {None}
        if clean_vin(record["vin"]) in old_vehicle:
            new_vin = None
        if clean_vin(record["chassis_number"]) in old_vehicle:
            new_chassis = None
    if (new_vin, new_chassis) == (record["vin"], record["chassis_number"]):
        return None
    change = {"vin": {"from": record["vin"], "to": new_vin},
              "chassisNumber": {"from": record["chassis_number"], "to": new_chassis}}
    if apply:
        connection.execute(
            text("UPDATE auditcore.vehicle_records SET vin=:vin, chassis_number=:chassis, updated_at_utc=now(), "
                 "version_no=version_no+1 WHERE tenant_id=:t AND journey_id=:j"),
            {**params, "vin": new_vin, "chassis": new_chassis})
        logger.info("vehicle_record_vin_corrected", tenant_id=tenant_id, journey_id=str(journey_id), **change)
    return change


def _usable(value: Any, *, floor: date | None, today: date) -> date | None:
    from audit_core.uc03_p2_dates import parse_extracted_date

    day = parse_extracted_date(value)
    if day is None or day > today or (floor is not None and day < floor):
        return None
    return day


def _booking_date(connection: Connection, *, tenant_id: str, journey_id: UUID) -> date | None:
    """The Journey's booking date (uc03_booking_date), else the day its booking documents
    were uploaded (product owner 2026-11-07: every Journey has a booking date)."""
    from audit_core.uc03_booking_date import journey_booking_date

    booked = journey_booking_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    if booked is not None:
        return booked
    return connection.execute(
        text("SELECT CAST(created_at_utc AT TIME ZONE 'Asia/Kolkata' AS date) FROM auditcore.journeys "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one_or_none()


# A person's own entry of the VIN (the vehicle-identification task) wins over any reading.
_ENTERED_VIN_SQL = """
SELECT i.vin FROM auditcore.p2_vehicle_identifications i
WHERE i.tenant_id=:t AND i.journey_id=:j AND i.vin IS NOT NULL ORDER BY i.created_at_utc DESC
"""

# The papers of THIS sale that print the vehicle's VIN. An RC, a valuation report, a
# transfer letter or a scrappage certificate is left out: on an exchange or scrappage deal
# it is the customer's OLD vehicle (found 2026-11-11: UTK-SR-261105-002 and
# UTK-PR-261109-002 were Outscope on a 2014 and a 2017 chassis read from the old car's RC).
_SALE_DOCUMENT_TYPES = (
    "customer_invoice_dms", "customer_invoice_dms_v2", "tax_invoice_dms", "tax_invoice_tally", "tax_invoice",
    "insurance_cover", "rto_challan", "form_21", "credit_note", "delivery_receipt", "delivery_order_cover",
    "bank_approval_letter", "wholesale_invoice", "gate_pass", "customer_ledger",
    "accessory_invoice_dms", "accessory_invoice_tally",
)
_INVOICE_TYPES = ("customer_invoice_dms", "customer_invoice_dms_v2", "tax_invoice_dms", "tax_invoice_tally",
                  "tax_invoice")

_DOCUMENT_VIN_SQL = """
SELECT e.document_type_key, f.di_document_id, f.effective_value #>> '{}' AS value
FROM auditcore.journey_document_extracted_fields f
JOIN auditcore.evidence e ON e.tenant_id=f.tenant_id AND e.journey_id=f.journey_id
 AND e.di_document_id=f.di_document_id AND e.association_status='ACTIVE'
WHERE f.tenant_id=:t AND f.journey_id=:j AND f.field_key IN ('vin_number', 'chassis_number')
  AND e.document_type_key = ANY(CAST(:types AS text[]))
"""


def agreed_vin(readings: list[tuple[str, str, Any]], *, today: date | None = None) -> str | None:
    """The VIN the sale's documents agree on. ``readings`` are (document type, document id,
    value as read).

    * A document supports a VIN when it prints it in full, or prints a short chassis number
      (6 characters or more: "T2K26045") that is the VIN's own ending.
    * One misread copy does not decide it (2026-11-11: ADI-SJ-261027-006's invoice read
      ...XZTK6K... -- "2019" -- where the policy and the RTO challan both print ...XZXT6K...).
    * Documents that print different VINs with none supported by a second document do NOT
      agree: there is no VIN (SHI-SW-261110-001: the invoice read a "2022" VIN, the RTO
      challan another; UTK-JI-261106-006: three papers, three readings). The Deal Type then
      waits for the VIN or the Team Lead.
    * Equal support: a VIN that names its month of manufacture first, then the invoice's."""
    full: dict[str, set[str]] = {}
    short: list[tuple[str, str]] = []
    on_invoice: set[str] = set()
    for document_type, document_id, value in readings:
        vin = clean_vin(value)
        if vin is not None:
            full.setdefault(vin, set()).add(str(document_id))
            if document_type in _INVOICE_TYPES:
                on_invoice.add(vin)
            continue
        # "T5J31078 (MA1RE2TTKT5J31078)", "CHNO - 52F79014": each run of letters and digits.
        for part in re.findall(r"[A-Z0-9]{6,17}", str(value or "").upper()):
            whole = clean_vin(part)
            if whole is not None:
                full.setdefault(whole, set()).add(str(document_id))
            else:
                short.append((part, str(document_id)))
    if not full:
        return None
    support = {vin: documents | {doc for part, doc in short if vin.endswith(part)} for vin, documents in full.items()}
    best = max(sorted(support), key=lambda vin: (
        len(support[vin]), vin_manufacture_month(vin, today=today) is not None, vin in on_invoice))
    if len(support) > 1 and len(support[best]) < 2:
        return None
    return best


# The vehicle's own invoice: the DMS one first, then the Tally one.
_INVOICE_DATE_SQL = """
SELECT f.effective_value #>> '{}' AS value
FROM auditcore.journey_document_extracted_fields f
JOIN auditcore.evidence e ON e.tenant_id=f.tenant_id AND e.journey_id=f.journey_id
 AND e.di_document_id=f.di_document_id AND e.association_status='ACTIVE'
WHERE f.tenant_id=:t AND f.journey_id=:j AND f.field_key='invoice_date'
  AND e.document_type_key IN ('customer_invoice_dms', 'customer_invoice_dms_v2', 'tax_invoice_dms',
                              'tax_invoice_tally')
ORDER BY (e.document_type_key = 'tax_invoice_tally'), f.updated_at_utc DESC
"""

_DELIVERY_DATE_SQL = """
SELECT value FROM (
    SELECT f.effective_value #>> '{}' AS value, 1 AS rank, e.linked_at_utc AS at
    FROM auditcore.journey_document_extracted_fields f
    JOIN auditcore.evidence e ON e.tenant_id=f.tenant_id AND e.journey_id=f.journey_id
     AND e.di_document_id=f.di_document_id AND e.association_status='ACTIVE'
    WHERE f.tenant_id=:t AND f.journey_id=:j AND e.document_type_key='gate_pass' AND f.field_key='delivery_date'
    UNION ALL
    SELECT CAST(CAST(d.actual_delivered_at AT TIME ZONE 'Asia/Kolkata' AS date) AS text), 2, d.updated_at_utc
    FROM auditcore.deliveries d WHERE d.tenant_id=:t AND d.journey_id=:j AND d.actual_delivered_at IS NOT NULL
) c ORDER BY rank, at DESC NULLS LAST
"""


def _vehicle_age_inputs(connection: Connection, *, tenant_id: str, journey_id: UUID, booking_date: date | None,
                        today: date) -> dict[str, Any]:
    """The Journey's VIN, the month it names, and the date its age is counted to."""
    from audit_core.uc03_p2_dates import programme_date_floor

    params = {"t": tenant_id, "j": journey_id}
    vin = journey_vin(connection, tenant_id=tenant_id, journey_id=journey_id, today=today)
    floor = programme_date_floor()
    reference: tuple[date | None, str | None] = (None, None)
    for kind, sql in (("invoice", _INVOICE_DATE_SQL), ("delivery", _DELIVERY_DATE_SQL)):
        day = next((d for d in (_usable(v, floor=floor, today=today)
                                for v in connection.execute(text(sql), params).scalars().all()) if d), None)
        if day is not None:
            reference = (day, kind)
            break
    if reference[0] is None and booking_date is not None:
        reference = (booking_date, "booking")
    return {"vin": vin, "manufacture_month": vin_manufacture_month(vin, today=today),
            "reference_date": reference[0], "reference_kind": reference[1]}


# ── Customer Type ────────────────────────────────────────────────────────────

_GSTIN = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]$")
_DEALER_INVOICE_TYPES = ("customer_invoice_dms", "customer_invoice_dms_v2", "tax_invoice_dms", "tax_invoice_tally",
                         "accessory_invoice_dms", "accessory_invoice_tally")


def is_gstin(value: Any) -> bool:
    return bool(_GSTIN.match(re.sub(r"[^A-Z0-9]", "", str(value or "").upper())))


def decide_customer_type(*, gst_certificate_on_file: bool, gstins: list[str], company_id_on_file: bool,
                         corporate_discount: float | None) -> dict[str, Any]:
    """The Customer Type and what it was decided on (see the module docstring)."""
    gstin = next((g for g in gstins if is_gstin(g)), None)
    if gstin is not None or gst_certificate_on_file:
        basis = f"The customer has provided GSTIN {gstin}." if gstin else "A GST certificate is on file."
        return {"customer_type": CORPORATE, "customer_type_basis": basis}
    if company_id_on_file and corporate_discount is not None and corporate_discount > 0:
        return {"customer_type": COMPANY,
                "customer_type_basis": f"A company ID card is on file and a corporate discount of "
                                       f"Rs {corporate_discount:,.0f} was given."}
    return {"customer_type": INDIVIDUAL,
            "customer_type_basis": "No GSTIN provided" + (
                "; a company ID card is on file but no corporate discount was given." if company_id_on_file
                else "; no company ID card on file.")}


def _customer_type_inputs(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    params = {"t": tenant_id, "j": journey_id, "invoices": list(_DEALER_INVOICE_TYPES)}
    documents = set(connection.execute(
        text("SELECT DISTINCT document_type_key FROM auditcore.evidence WHERE tenant_id=:t AND journey_id=:j "
             "AND association_status='ACTIVE' AND document_type_key IN ('gst_certificate', 'corporate_id')"),
        params,
    ).scalars().all())
    gstins = [re.sub(r"[^A-Z0-9]", "", str(v or "").upper()) for v in connection.execute(
        text(
            """
            SELECT f.effective_value #>> '{}'
            FROM auditcore.journey_document_extracted_fields f
            JOIN auditcore.evidence e ON e.tenant_id=f.tenant_id AND e.journey_id=f.journey_id
             AND e.di_document_id=f.di_document_id AND e.association_status='ACTIVE'
            WHERE f.tenant_id=:t AND f.journey_id=:j
              AND ((f.field_key='gstin' AND e.document_type_key IN ('gst_certificate', 'gst_declaration'))
                   OR (f.field_key='buyer_gstin' AND e.document_type_key = ANY(CAST(:invoices AS text[]))))
            ORDER BY (f.field_key='gstin') DESC, f.updated_at_utc DESC
            """
        ),
        params,
    ).scalars().all()]
    discount = connection.execute(
        text(
            """
            SELECT max(amount) FROM (
                SELECT actual_discount_amount AS amount FROM auditcore.discount_applications
                 WHERE tenant_id=:t AND journey_id=:j AND discount_key LIKE 'CORPORATE%'
                UNION ALL
                SELECT CASE WHEN f.effective_value #>> '{}' ~ '^[0-9]+(\\.[0-9]+)?$'
                            THEN CAST(f.effective_value #>> '{}' AS numeric) END
                FROM auditcore.journey_document_extracted_fields f
                JOIN auditcore.evidence e ON e.tenant_id=f.tenant_id AND e.journey_id=f.journey_id
                 AND e.di_document_id=f.di_document_id AND e.association_status='ACTIVE'
                WHERE f.tenant_id=:t AND f.journey_id=:j
                  AND f.field_key IN ('corporate_discount_amount', 'corporate_offer_amount')
            ) d
            """
        ),
        params,
    ).scalar_one_or_none()
    return {"gst_certificate_on_file": "gst_certificate" in documents, "gstins": gstins,
            "company_id_on_file": "corporate_id" in documents,
            "corporate_discount": float(discount) if discount is not None else None}


# ── Insurance ────────────────────────────────────────────────────────────────

def _insurance(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    from audit_core.uc03_insurance_source import resolve

    decision = resolve(connection, tenant_id=tenant_id, journey_id=journey_id)
    return {"insurance_type": "SELF" if decision["source"] == "SELF" else "DEALERSHIP",
            "insurance_decided_by": decision["decidedBy"], "insurance_basis": decision["basis"]}


# ── the stored copy ──────────────────────────────────────────────────────────

_RULE_COLUMNS = (
    "deal_type", "deal_type_decided_by", "deal_type_basis", "vin_pending", "booking_date_used", "vin_used",
    "vin_manufacture_month", "age_reference_date", "age_reference_kind", "vehicle_age_days",
    "customer_type", "customer_type_basis", "insurance_type", "insurance_decided_by", "insurance_basis",
)


def classify(connection: Connection, *, tenant_id: str, journey_id: UUID,
             today: date | None = None) -> dict[str, Any]:
    """The three facts of a Journey as the rules decide them now (nothing written)."""
    from audit_core.uc03_p2_dates import programme_today

    today = today or programme_today()
    params = {"t": tenant_id, "j": journey_id}
    tl = connection.execute(
        text("SELECT tl_deal_type, tl_reason FROM auditcore.journey_classifications "
             "WHERE tenant_id=:t AND journey_id=:j"), params).mappings().first()
    mr_opted = bool(connection.execute(
        text("SELECT opted FROM auditcore.p2_management_referrals WHERE tenant_id=:t AND journey_id=:j"),
        params).scalar_one_or_none())
    booking_date = _booking_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    age = _vehicle_age_inputs(connection, tenant_id=tenant_id, journey_id=journey_id,
                              booking_date=booking_date, today=today)
    return {
        **decide_deal_type(booking_date=booking_date, tl_deal_type=tl["tl_deal_type"] if tl else None,
                           tl_reason=tl["tl_reason"] if tl else None, mr_opted=mr_opted, **age),
        **decide_customer_type(**_customer_type_inputs(connection, tenant_id=tenant_id, journey_id=journey_id)),
        **_insurance(connection, tenant_id=tenant_id, journey_id=journey_id),
    }


def refresh(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """Decide the three facts and keep them. The row is rewritten only when a value
    changed. Returns the facts with ``changed``."""
    facts = classify(connection, tenant_id=tenant_id, journey_id=journey_id)
    columns = ", ".join(_RULE_COLUMNS)
    values = ", ".join(f":{c}" for c in _RULE_COLUMNS)
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in _RULE_COLUMNS)
    differs = " OR ".join(f"c.{c} IS DISTINCT FROM EXCLUDED.{c}" for c in _RULE_COLUMNS)
    changed = connection.execute(
        text(
            f"""
            INSERT INTO auditcore.journey_classifications AS c (tenant_id, journey_id, {columns})
            VALUES (:t, :j, {values})
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET {updates}, computed_at_utc=now()
            WHERE {differs}
            RETURNING 1
            """
        ),
        {"t": tenant_id, "j": journey_id, **{c: facts[c] for c in _RULE_COLUMNS}},
    ).first() is not None
    return {**facts, "changed": changed}


def _dedupe_key(journey_id: UUID) -> str:
    return f"deal-type:{journey_id}"


def sync_classification(connection: Connection, *, tenant_id: str, journey_id: UUID,
                        evaluation_started_at: datetime | None = None) -> dict[str, int]:
    """The worker's rule pass: keep the Journey's stored copy current, and ask the
    Team Lead for the Deal Type of a Journey in delivery that still has no readable
    VIN. The task closes by itself once the Deal Type is filled."""
    from audit_core.uc03_delivery_start import delivery_has_started
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh, resolve_if_open

    facts = refresh(connection, tenant_id=tenant_id, journey_id=journey_id)
    counts: dict[str, int] = {"UPDATED": 1} if facts["changed"] else {}
    key = _dedupe_key(journey_id)
    if facts["deal_type"] is None and delivery_has_started(connection, tenant_id=tenant_id, journey_id=journey_id):
        _, outcome = raise_or_refresh(
            connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key, task_type=TASK_TYPE,
            source_type="RULE", source_code="DEAL_TYPE_VIN_MISSING",
            title="Set the Deal Type: no VIN to read the vehicle's age from",
            description=(
                f"{facts['deal_type_basis']} Delivery has started, so choose the Deal Type (Inscope, Outscope, "
                "MR, Lease or Bulk) with a short reason. If the VIN is read first, the Deal Type is filled by "
                "the rule and this task closes by itself."),
            reference={
                "generatedBy": "SYSTEM", "sourceType": "RULE", "sourceCode": "DEAL_TYPE_VIN_MISSING",
                "vin": facts["vin_used"],
                "bookingDate": facts["booking_date_used"].isoformat() if facts["booking_date_used"] else None,
                "question": "What is the Deal Type of this Journey?",
                "answers": [{"value": v, "label": LABELS[v], "requiresComment": True} for v in TL_DEAL_TYPES],
            },
            severity="MEDIUM", registry=get_registry(), evaluation_started_at=evaluation_started_at,
        )
        counts[outcome] = counts.get(outcome, 0) + 1
    elif resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                         evidence={"dealType": facts["deal_type"], "decidedBy": facts["deal_type_decided_by"],
                                   "basis": facts["deal_type_basis"],
                                   "verifiedAt": datetime.now().astimezone().isoformat()}):
        counts["VERIFIED"] = 1
    return counts


def answer_task(connection: Connection, *, tenant_id: str, task: dict[str, Any], actor_id: str,
                actor_role_code: str, comment: str | None, details: dict[str, Any] | None) -> dict[str, Any]:
    """The Team Lead's answer to DEAL_TYPE_CONFIRM: the Deal Type, with a reason."""
    import json

    from audit_core.task_closure_roles import has_team_lead_authority

    role = str(actor_role_code or "").upper()
    if not has_team_lead_authority(role, task.get("assigned_role_code")):
        raise ValueError("Only a Team Lead can set the Deal Type")
    answer = str((details or {}).get("answer") or (details or {}).get("dealType") or "").upper()
    if answer not in TL_DEAL_TYPES:
        raise ValueError("Choose Inscope, Outscope, MR, Lease or Bulk.")
    reason = str((details or {}).get("reason") or comment or "").strip()
    if len(reason) < 5:
        raise ValueError("Give a short reason for this Deal Type.")
    set_tl_deal_type(connection, tenant_id=tenant_id, journey_id=UUID(str(task["journey_id"])), deal_type=answer,
                     reason=reason, actor_id=actor_id, role=role, correlation_id=None)
    connection.execute(
        text("UPDATE auditcore.p2_tasks SET task_status='VERIFIED_COMPLETE', verified_at_utc=now(), "
             "updated_at_utc=now(), completion_result=CAST(:result AS jsonb) WHERE tenant_id=:t AND task_id=:id"),
        {"t": tenant_id, "id": task["task_id"], "result": json.dumps({
            "outcome": answer, "reason": reason, "answeredBy": actor_id,
            "answeredAt": datetime.now().astimezone().isoformat()})},
    )
    return {"taskId": str(task["task_id"]), "status": "VERIFIED_COMPLETE", "outcome": answer}


def view(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """The stored facts as Journey 360 and the reports show them; worked out on the
    spot (not written) for a Journey whose row is not there yet."""
    row = connection.execute(
        text("SELECT * FROM auditcore.journey_classifications WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().first()
    facts = dict(row) if row is not None else classify(connection, tenant_id=tenant_id, journey_id=journey_id)
    unconfirmed = facts["insurance_decided_by"] in ("UNCONFIRMED", "DEFAULT")

    def iso(value: Any) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "dealType": facts["deal_type"], "dealTypeLabel": LABELS.get(str(facts["deal_type"])),
        "dealTypeDecidedBy": facts["deal_type_decided_by"], "dealTypeBasis": facts["deal_type_basis"],
        "vinPending": bool(facts["vin_pending"]),
        "bookingDate": iso(facts["booking_date_used"]), "vin": facts["vin_used"],
        "vinManufactureMonth": facts["vin_manufacture_month"].strftime("%Y-%m")
        if facts["vin_manufacture_month"] else None,
        "ageReferenceDate": iso(facts["age_reference_date"]), "ageReferenceKind": facts["age_reference_kind"],
        "vehicleAgeDays": facts["vehicle_age_days"],
        "tlDealType": facts.get("tl_deal_type"), "tlReason": facts.get("tl_reason"),
        "tlSetAtUtc": facts.get("tl_set_at_utc"),
        "tlDealTypeOptions": [{"value": v, "label": LABELS[v]} for v in TL_DEAL_TYPES],
        "customerType": facts["customer_type"], "customerTypeLabel": LABELS[facts["customer_type"]],
        "customerTypeBasis": facts["customer_type_basis"],
        "insuranceType": facts["insurance_type"],
        "insuranceTypeLabel": LABELS.get(str(facts["insurance_type"])),
        "insuranceDecidedBy": facts["insurance_decided_by"], "insuranceConfirmed": not unconfirmed,
        "insuranceBasis": facts["insurance_basis"],
    }


def set_tl_deal_type(connection: Connection, *, tenant_id: str, journey_id: UUID, deal_type: str | None,
                     reason: str, actor_id: str, role: str, correlation_id: str | None) -> dict[str, Any]:
    """The Team Lead's own Deal Type (it wins over the rule) -- or, with ``deal_type``
    None, the Journey is handed back to the rule."""
    from audit_core.uc03_p2_runtime import note_facts_changed, record_activity

    if deal_type is not None and deal_type not in TL_DEAL_TYPES:
        raise ValueError("Choose Inscope, Outscope, MR, Lease or Bulk.")
    # The row has to exist before the Team Lead's call is put on it.
    refresh(connection, tenant_id=tenant_id, journey_id=journey_id)
    connection.execute(
        text(
            """
            UPDATE auditcore.journey_classifications
            SET tl_deal_type=:deal_type, tl_reason=:reason, tl_set_by_actor_id=:actor, tl_set_by_role=:role,
                tl_set_at_utc=now()
            WHERE tenant_id=:t AND journey_id=:j
            """
        ),
        {"t": tenant_id, "j": journey_id, "deal_type": deal_type, "reason": reason, "actor": actor_id, "role": role},
    )
    record_activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type="DEAL_TYPE_SET",
        subject_type="JOURNEY", subject_id=str(journey_id),
        details={"dealType": deal_type or "RULE", "reason": reason, "role": role}, correlation_id=correlation_id,
    )
    facts = refresh(connection, tenant_id=tenant_id, journey_id=journey_id)
    if facts["deal_type"] is not None:
        # The system's own question for this Journey is answered with it.
        from audit_core.uc03_p2_task_producer import resolve_if_open

        resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=_dedupe_key(journey_id),
                        evidence={"dealType": facts["deal_type"], "decidedBy": "TL", "setBy": actor_id,
                                  "role": role, "reason": reason,
                                  "verifiedAt": datetime.now().astimezone().isoformat()})
    note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id, reason="DEAL_TYPE_SET",
                       correlation_id=correlation_id)
    return view(connection, tenant_id=tenant_id, journey_id=journey_id)


# Who may set a Deal Type: the Team Lead (and Project Manager), and the Reviewer
# (product owner 2026-11-11: "TL or reviewer should be able to do this").
DEAL_TYPE_ROLES = ("TL", "PM", "REVIEWER")
MAX_JOURNEYS_AT_ONCE = 200


def journeys_in_scope(connection: Connection, *, tenant_id: str, actor_id: str, journey_ids: list[UUID]) -> set[str]:
    """Those of ``journey_ids`` inside the actor's Dealerships / Outlets, by the same
    assignment test the journey list uses (uc03_p2_journey_views)."""
    return {str(v) for v in connection.execute(
        text(
            """
            SELECT j.journey_id FROM auditcore.journeys j
            WHERE j.tenant_id=:t AND j.journey_id = ANY(CAST(:ids AS uuid[]))
              AND EXISTS (
                SELECT 1 FROM auditcore.business_assignments ba
                WHERE ba.tenant_id = j.tenant_id AND ba.security_actor_id = :actor
                  AND ba.assignment_status = 'ACTIVE' AND ba.effective_from <= now()
                  AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                  AND (ba.dealer_id IS NULL
                       OR (ba.dealer_id = j.dealer_id AND (ba.outlet_id IS NULL OR ba.outlet_id = j.outlet_id))))
            """
        ),
        {"t": tenant_id, "actor": actor_id, "ids": [str(i) for i in journey_ids]},
    ).scalars().all()}


def set_deal_type_for_many(connection: Connection, *, tenant_id: str, journey_ids: list[UUID], deal_type: str | None,
                           reason: str, actor_id: str, role: str, correlation_id: str | None) -> dict[str, Any]:
    """One Deal Type for several Journeys in one go (a bulk or lease order is many
    Journeys; product owner 2026-11-11). Journeys outside the actor's scope are
    refused, by id, and nothing is changed for them."""
    wanted = list(dict.fromkeys(journey_ids))
    allowed = journeys_in_scope(connection, tenant_id=tenant_id, actor_id=actor_id, journey_ids=wanted)
    updated: list[dict[str, Any]] = []
    for journey_id in wanted:
        if str(journey_id) not in allowed:
            continue
        shown = set_tl_deal_type(connection, tenant_id=tenant_id, journey_id=journey_id, deal_type=deal_type,
                                 reason=reason, actor_id=actor_id, role=role, correlation_id=correlation_id)
        updated.append({"journeyId": str(journey_id), "dealType": shown["dealType"],
                        "dealTypeLabel": shown["dealTypeLabel"]})
    return {"updated": updated, "refused": [str(i) for i in wanted if str(i) not in allowed]}


# ── every Journey already on file ────────────────────────────────────────────

def run_journey_classification(engine: Engine, tag: str, tenant_ids: list[str]) -> dict[str, Any]:
    """Fill (or re-decide) the stored copy of every Journey. A DRY RUN -- nothing
    written -- unless the tag ends with "-commit". Safe to run again. Returns the
    counts per Project, kept on the job row."""
    from audit_core.db import set_tenant_context

    commit = tag.endswith("-commit")
    report: dict[str, Any] = {"commit": commit, "tenants": {}}
    for tenant_id in tenant_ids:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            journeys = connection.execute(
                text("SELECT journey_id, journey_reference FROM auditcore.journeys WHERE tenant_id=:t "
                     "ORDER BY created_at_utc"), {"t": tenant_id}).mappings().all()
        counts: dict[str, int] = {}

        def count(key: str, counts: dict[str, int] = counts) -> None:
            counts[key] = counts.get(key, 0) + 1

        for journey in journeys:
            journey_id = UUID(str(journey["journey_id"]))
            try:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    corrected = correct_vehicle_record(connection, tenant_id=tenant_id, journey_id=journey_id,
                                                       apply=commit)
                    facts = (refresh if commit else classify)(connection, tenant_id=tenant_id, journey_id=journey_id)
            except Exception:
                count("failed")
                logger.warning("journey_classification_failed", tag=tag, tenant_id=tenant_id,
                               journey=journey["journey_reference"], exc_info=True)
                continue
            count(f"deal_{str(facts['deal_type'] or 'not_filled').lower()}")
            count(f"customer_{facts['customer_type'].lower()}")
            count(f"insurance_{str(facts['insurance_type']).lower()}_{str(facts['insurance_decided_by']).lower()}")
            if facts.get("changed"):
                count("written")
            if corrected is not None:
                count("vehicle_record_corrected")
                if len(report.setdefault("vehicleRecords", [])) < 300:
                    report["vehicleRecords"].append({"journey": journey["journey_reference"], **corrected})
        report["tenants"][tenant_id] = {"journeys": len(journeys), **dict(sorted(counts.items()))}
        logger.info("journey_classification_finished", tag=tag, tenant_id=tenant_id, commit=commit,
                    journeys=len(journeys), **dict(sorted(counts.items())))
    return report
