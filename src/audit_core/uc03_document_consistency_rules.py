"""Document consistency checks moved from the Rule Engine into Audit Core
(product owner 2026-11-15, rule review decision 1).

"For rule engine thing.. lets keep it in audit core for Mahindra Orissa": of
the Rule Engine's 40 checks, these 12 are kept, the rest retired, and the Rule
Engine is no longer called for a Project that says so
(tenant_rule_config.rule_engine_enabled). Each check keeps its Rule Engine code
so its history reads on.

Kept as they were (booking-to-delivery days dropped):
  INVOICE_DATE_BEFORE_BOOKING, DO_DATE_AFTER_GATE, CORPORATE_GSTIN_CERT_VS_INVOICE,
  DEBIT_NOTE_INSURANCE_VS_COVER_NOTE (only when a debit note gives the insurance),
  CORPORATE_PO_AMOUNT_VS_INVOICE (only when a PO is on file).
Kept and corrected (Aadhaar vs PAN date of birth dropped):
  INSURANCE_START_AFTER_DELIVERY and ACCESSORY_DATE_VS_DELIVERY compared the wrong
  way round; ACCESSORY_VIN_VS_VEHICLE_VIN and CHASSIS_INVOICE_VS_DO compared the
  printed last characters with the full VIN; GATE_DATE_BEFORE_INVOICE now ignores
  a misread year.
Never ran, now run:
  CORPORATE_NAME_CERT_VS_INVOICE (read organisation_legal_name / trade name, not
  "legal_name"); EXCHANGE_VALUE_VS_INVOICE_CREDIT, optional: only when a valuation
  report is on file (field valuation_final_offer_value).

A date that cannot be right (before the programme's floor or in the future),
or a chassis number one to three characters off the Journey's VIN, is not a
fact: the check waits instead of failing on it (uc03_read_plausibility).
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from audit_core.uc03_read_plausibility import clean_chassis, date_problem, vin_relation

if TYPE_CHECKING:
    from audit_core.uc03_p2_audit_rules import RuleOutcome, _Facts

_VEHICLE_INVOICES = ("customer_invoice_dms", "tax_invoice_tally")
_ACCESSORY_INVOICES = ("accessory_invoice_dms", "accessory_invoice_tally")
_GATE_PASS = ("gate_pass", "delivery_receipt")
_AMOUNT_TOLERANCE = Decimal(100)
# The shortest chassis suffix worth comparing (accessory invoices print the last 8).
_MIN_SUFFIX = 6


def _rule():
    from audit_core import uc03_p2_audit_rules as rules

    return rules


def _outcome(code: str, outcome: str, reason: str, details: dict[str, Any] | None = None) -> RuleOutcome:
    return _rule().RuleOutcome(code, outcome, reason, details or {})


def _first(facts: _Facts, types: tuple[str, ...], field: str) -> tuple[Any, dict[str, Any] | None]:
    """The newest document of ``types`` that gives ``field``: (value, document)."""
    for doc in facts.documents(*types):
        value = doc["fields"].get(field)
        if value not in (None, ""):
            return value, doc
    return None, None


def _date(facts: _Facts, types: tuple[str, ...], field: str) -> tuple[date | None, str | None]:
    """(the date, why it is not used); a date that cannot be right is not used."""
    value, _ = _first(facts, types, field)
    problem = date_problem(value, field, today=facts.today)
    day = _rule()._day(value)
    if problem == "DATE_UNREADABLE" or (value is not None and day is None):
        return None, f"read as \"{value}\", which is not a date"
    if problem:
        return None, f"read as {_rule()._when(day)}, which cannot be right (correct the reading)"
    return day, None


def _norm_vin(value: Any) -> str:
    return clean_chassis(value)


def _vehicle_vin(facts: _Facts) -> str | None:
    """The Journey's agreed VIN (the one a person entered, else the one most of
    the sale's documents agree on), else the vehicle invoice's chassis number."""
    from audit_core.uc03_p2_journey_classification import journey_vin

    agreed = journey_vin(facts.connection, tenant_id=facts.tenant_id, journey_id=facts.journey_id)
    if agreed:
        return agreed
    value, _ = _first(facts, _VEHICLE_INVOICES, "chassis_number")
    if not value:
        value, _ = _first(facts, _VEHICLE_INVOICES, "vin_number")
    vin = _norm_vin(value)
    return vin or None


# ── dates ─────────────────────────────────────────────────────────────────────


def _later_than(code: str, facts: _Facts, *, later: tuple[tuple[str, ...], str, str],
                earlier: tuple[tuple[str, ...], str, str], fail_title: str) -> RuleOutcome:
    """FAIL when the ``later`` document's date is before the ``earlier`` one's."""
    (l_types, l_field, l_name), (e_types, e_field, e_name) = later, earlier
    l_day, l_why = _date(facts, l_types, l_field)
    e_day, e_why = _date(facts, e_types, e_field)
    if l_why or e_why:
        return _outcome(code, "SKIPPED", "; ".join(
            f"The {name} date {why}" for name, why in ((l_name, l_why), (e_name, e_why)) if why) + ".")
    if l_day is None or e_day is None:
        missing = l_name if l_day is None else e_name
        return _outcome(code, "SKIPPED", f"Checked once the {missing} date is read.")
    when = _rule()._when
    if l_day < e_day:
        return _outcome(code, "FAIL", f"The {l_name} is dated {when(l_day)}, before the {e_name} on {when(e_day)}.",
                        {"findingTitle": fail_title})
    return _outcome(code, "PASS", f"The {l_name} ({when(l_day)}) is on or after the {e_name} ({when(e_day)}).")


def invoice_date_before_booking(facts: _Facts) -> RuleOutcome:
    code = "INVOICE_DATE_BEFORE_BOOKING"
    invoice, why = _date(facts, _VEHICLE_INVOICES, "invoice_date")
    booked = facts.booking_date
    if why:
        return _outcome(code, "SKIPPED", f"The vehicle invoice date {why}.")
    if invoice is None or booked is None:
        return _outcome(code, "SKIPPED", "Checked once the vehicle invoice and the booking date are read.")
    when = _rule()._when
    if invoice < booked:
        return _outcome(code, "FAIL", f"The vehicle invoice is dated {when(invoice)}, before the booking on "
                                      f"{when(booked)}.", {"findingTitle": "Invoice dated before the booking"})
    return _outcome(code, "PASS", f"The vehicle invoice ({when(invoice)}) is on or after the booking ({when(booked)}).")


def gate_date_before_invoice(facts: _Facts) -> RuleOutcome:
    return _later_than("GATE_DATE_BEFORE_INVOICE", facts,
                       later=(_GATE_PASS, "delivery_date", "gate pass"),
                       earlier=(_VEHICLE_INVOICES, "invoice_date", "vehicle invoice"),
                       fail_title="Vehicle left before it was invoiced")


def do_date_after_gate(facts: _Facts) -> RuleOutcome:
    # The financier's delivery order comes before the vehicle leaves.
    return _later_than("DO_DATE_AFTER_GATE", facts,
                       later=(_GATE_PASS, "delivery_date", "gate pass"),
                       earlier=(("delivery_order_cover",), "delivery_date", "delivery order"),
                       fail_title="Vehicle delivered before the delivery order")


def insurance_start_after_delivery(facts: _Facts) -> RuleOutcome:
    # The cover must start on or before the day the vehicle leaves (the Rule
    # Engine compared it the other way round: every sample started before).
    code = "INSURANCE_START_AFTER_DELIVERY"
    start, s_why = _date(facts, ("insurance_cover",), "policy_start_date")
    gate, g_why = _date(facts, _GATE_PASS, "delivery_date")
    if s_why or g_why:
        return _outcome(code, "SKIPPED", "; ".join(f"The {n} date {w}" for n, w in
                                                   (("insurance start", s_why), ("gate pass", g_why)) if w) + ".")
    if start is None or gate is None:
        return _outcome(code, "SKIPPED", "Checked once the insurance start and the gate pass date are read.")
    when = _rule()._when
    if start > gate:
        return _outcome(code, "FAIL", f"The insurance starts on {when(start)}, after the vehicle left on {when(gate)}.",
                        {"findingTitle": "Vehicle delivered before the insurance started"})
    return _outcome(code, "PASS", f"The insurance starts on {when(start)}, on or before the delivery on {when(gate)}.")


def accessory_date_vs_delivery(facts: _Facts) -> RuleOutcome:
    # An accessory invoice dated after the vehicle left (the Rule Engine
    # flagged invoices dated before the gate pass as "after").
    code = "ACCESSORY_DATE_VS_DELIVERY"
    gate, g_why = _date(facts, _GATE_PASS, "delivery_date")
    if g_why:
        return _outcome(code, "SKIPPED", f"The gate pass date {g_why}.")
    invoices = facts.documents(*_ACCESSORY_INVOICES)
    if not invoices:
        return _outcome(code, "PASS", "No accessory invoice on file.")
    if gate is None:
        return _outcome(code, "SKIPPED", "Checked once the gate pass date is read.")
    rules, late = _rule(), []
    for doc in invoices:
        value = doc["fields"].get("invoice_date")
        day = None if date_problem(value, "invoice_date", today=facts.today) else rules._day(value)
        if day is not None and day > gate:
            late.append(f"{doc['fields'].get('invoice_number') or 'accessory invoice'} dated {rules._when(day)}")
    if late:
        return _outcome(code, "FAIL", f"Accessory invoice after the delivery on {rules._when(gate)}: "
                                      f"{'; '.join(late)}.", {"findingTitle": "Accessories invoiced after delivery"})
    return _outcome(code, "PASS", f"Every accessory invoice is dated on or before the delivery on {rules._when(gate)}.")


# ── chassis ───────────────────────────────────────────────────────────────────


def _chassis_check(code: str, facts: _Facts, *, types: tuple[str, ...], name: str, title: str) -> RuleOutcome:
    vin = _vehicle_vin(facts)
    docs = [d for d in facts.documents(*types)
            if _norm_vin(d["fields"].get("chassis_number") or d["fields"].get("vin_number"))]
    if not docs:
        return _outcome(code, "PASS", f"No {name} with a chassis number on file.")
    if not vin:
        return _outcome(code, "SKIPPED", "Checked once the vehicle invoice's chassis number is read.")
    wrong, misread = [], []
    for doc in docs:
        printed = _norm_vin(doc["fields"].get("chassis_number") or doc["fields"].get("vin_number"))
        relation = vin_relation(printed, vin)
        if relation in ("SAME", "UNKNOWN"):
            continue
        (misread if relation == "MISREAD" else wrong).append(printed)
    if wrong:
        return _outcome(code, "FAIL", f"The {name} names chassis {', '.join(wrong)}, not this vehicle ({vin}).",
                        {"findingTitle": title})
    if misread:
        return _outcome(code, "SKIPPED", f"The {name} chassis {', '.join(misread)} differs from {vin} by a few "
                                         "characters: a misread, correct the reading.")
    return _outcome(code, "PASS", f"The {name} names this vehicle ({vin}).")


def accessory_vin_vs_vehicle_vin(facts: _Facts) -> RuleOutcome:
    return _chassis_check("ACCESSORY_VIN_VS_VEHICLE_VIN", facts, types=_ACCESSORY_INVOICES, name="accessory invoice",
                          title="Accessory invoice for another vehicle")


def chassis_invoice_vs_do(facts: _Facts) -> RuleOutcome:
    return _chassis_check("CHASSIS_INVOICE_VS_DO", facts, types=("delivery_order_cover",), name="delivery order",
                          title="Delivery order for another vehicle")


# ── corporate buyer ───────────────────────────────────────────────────────────


def corporate_gstin_cert_vs_invoice(facts: _Facts) -> RuleOutcome:
    code = "CORPORATE_GSTIN_CERT_VS_INVOICE"
    certificate, _ = _first(facts, ("gst_certificate",), "gstin")
    invoice, _ = _first(facts, _VEHICLE_INVOICES, "buyer_gstin")
    if not certificate:
        return _outcome(code, "PASS", "No GST certificate on file.")
    if not invoice:
        return _outcome(code, "SKIPPED", "The vehicle invoice gives no buyer GSTIN to compare with.")
    a, b = _norm_vin(certificate), _norm_vin(invoice)
    if a != b:
        return _outcome(code, "FAIL", f"The GST certificate's GSTIN {a} is not the invoice's buyer GSTIN {b}.",
                        {"findingTitle": "Invoice billed to another GSTIN"})
    return _outcome(code, "PASS", f"The invoice is billed to the certificate's GSTIN {a}.")


def corporate_name_cert_vs_invoice(facts: _Facts) -> RuleOutcome:
    from audit_core.uc03_p2_names import same_organisation, same_person

    code = "CORPORATE_NAME_CERT_VS_INVOICE"
    docs = facts.documents("gst_certificate")
    names = [n for d in docs for n in (d["fields"].get("organisation_legal_name"),
                                       d["fields"].get("organisation_trade_name")) if n]
    if not names:
        return _outcome(code, "PASS", "No GST certificate with a name on file.")
    buyer, _ = _first(facts, _VEHICLE_INVOICES, "buyer_name")
    if not buyer:
        return _outcome(code, "SKIPPED", "Checked once the vehicle invoice's buyer name is read.")
    if any(same_organisation(n, buyer) or same_person(n, buyer) for n in names):
        return _outcome(code, "PASS", f"The invoice's buyer \"{buyer}\" is the GST certificate's name.")
    return _outcome(code, "FAIL", f"The invoice's buyer \"{buyer}\" is not the GST certificate's name "
                                  f"({' / '.join(dict.fromkeys(str(n) for n in names))}).",
                    {"findingTitle": "Invoice buyer is not the GST certificate holder"})


def corporate_po_amount_vs_invoice(facts: _Facts) -> RuleOutcome:
    code = "CORPORATE_PO_AMOUNT_VS_INVOICE"
    rules = _rule()
    po_amount, po = _first(facts, ("purchase_order",), "po_amount")
    if po is None:
        return _outcome(code, "PASS", "No purchase order with an amount on file (a PO is not mandatory).")
    invoice, _ = _first(facts, _VEHICLE_INVOICES, "grand_total_amount")
    amount, billed = rules._dec(po_amount), rules._dec(invoice)
    quantity = rules._dec(po["fields"].get("quantity"))
    if amount is not None and quantity is not None and quantity > 1:
        amount = (amount / quantity).quantize(Decimal("0.01"))
    if amount is None or billed is None:
        return _outcome(code, "SKIPPED", "Checked once the PO amount and the invoice total are read.")
    if abs(amount - billed) > _AMOUNT_TOLERANCE:
        return _outcome(code, "FAIL", f"The purchase order is for {rules._rupees(amount)}{' a vehicle' if quantity and quantity > 1 else ''}; "
                                      f"the invoice is for {rules._rupees(billed)}.",
                        {"findingTitle": "Invoice differs from the purchase order"})
    return _outcome(code, "PASS", f"The invoice ({rules._rupees(billed)}) matches the purchase order.")


# ── insurance and exchange ────────────────────────────────────────────────────

_INSURANCE_WORDS = re.compile(r"INSURANCE|PREMIUM|POLICY", re.IGNORECASE)


def debit_note_insurance_vs_cover_note(facts: _Facts) -> RuleOutcome:
    # Only when a debit note gives the insurance (product owner 2026-11-15).
    code = "DEBIT_NOTE_INSURANCE_VS_COVER_NOTE"
    rules = _rule()
    debited = None
    for doc in facts.documents("debit_note"):
        fields = doc["fields"]
        debited = rules._dec(fields.get("insurance_amount"))
        if debited is None and _INSURANCE_WORDS.search(f"{fields.get('particulars') or ''} {fields.get('line_items') or ''}"):
            debited = rules._dec(fields.get("total_amount"))
        if debited is not None:
            break
    if debited is None:
        return _outcome(code, "PASS", "No debit note for the insurance on file.")
    premium, _ = _first(facts, ("insurance_cover",), "premium_amount")
    cover = rules._dec(premium)
    if cover is None:
        return _outcome(code, "SKIPPED", "Checked once the cover note's premium is read.")
    if abs(debited - cover) > _AMOUNT_TOLERANCE:
        return _outcome(code, "FAIL", f"The debit note charges {rules._rupees(debited)} for the insurance; the "
                                      f"cover note's premium is {rules._rupees(cover)}.",
                        {"findingTitle": "Insurance debited differs from the premium"})
    return _outcome(code, "PASS", f"The debit note's insurance ({rules._rupees(debited)}) is the cover note's premium.")


def exchange_value_vs_invoice_credit(facts: _Facts) -> RuleOutcome:
    # Optional: the valuation report is rarely given (product owner 2026-11-15).
    code = "EXCHANGE_VALUE_VS_INVOICE_CREDIT"
    rules = _rule()
    offered, _ = _first(facts, ("valuation_report",), "valuation_final_offer_value")
    if offered is None:
        return _outcome(code, "PASS", "No valuation report on file (optional).")
    value = rules._dec(offered)
    credited = rules._dec(((facts.sheet.get("summary") or {}).get("paid") or {}).get("oldVehicle"))
    if value is None:
        return _outcome(code, "SKIPPED", f"The valuation report's offer \"{offered}\" is not an amount.")
    if credited is None:
        return _outcome(code, "SKIPPED", "No old vehicle value is credited on the deal yet.")
    if abs(value - credited) > _AMOUNT_TOLERANCE:
        return _outcome(code, "FAIL", f"The old vehicle was valued at {rules._rupees(value)} and credited at "
                                      f"{rules._rupees(credited)}.",
                        {"findingTitle": "Old vehicle credited at a different value"})
    return _outcome(code, "PASS", f"The old vehicle is credited at its valuation ({rules._rupees(value)}).")


DELIVERY_CHECKS = (
    invoice_date_before_booking, gate_date_before_invoice, do_date_after_gate, insurance_start_after_delivery,
    accessory_date_vs_delivery, accessory_vin_vs_vehicle_vin, chassis_invoice_vs_do,
    corporate_gstin_cert_vs_invoice, corporate_name_cert_vs_invoice, corporate_po_amount_vs_invoice,
    debit_note_insurance_vs_cover_note, exchange_value_vs_invoice_credit,
)
CODES = tuple(check.__name__.upper() for check in DELIVERY_CHECKS)
