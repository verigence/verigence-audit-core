"""System remarks: what the system notes on a journey instead of raising a task
(product owner 2026-11-15, rule review decisions 3, 5 and 9).

"For scrappage we already amended the code to handle 25% discount, but it
should come up in the journey observation or system remarks ... if EW, RSA, or
fastag is not taken again it should be in remarks"; the EW / RSA invoice the
dealer did not provide "should be recorded in the remarks"; a temporary
registration is closed with its number; a BH registration is said so.

Every remark is derived from what is already on file -- the Deal, the
undercharge check and the closed tasks -- each time Journey 360 asks. Nothing
is stored twice, so a remark can never disagree with the deal or the task it
comes from.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_registration_closures import (
    DEALER_NOT_PROVIDED,
    TEMPORARY_REGISTRATION,
    confirmed_bh,
    temporary_number,
)

# The optional products whose absence is a remark (decision 5).
_NOT_TAKEN_PRODUCTS = {
    "extended_warranty_amount": "Extended warranty",
    "additional_warranty_amount": "Extended warranty",
    "rsa_amount": "RSA",
    "fastag_amount": "FASTag",
}
_DOCUMENT_LABEL = {"ew_invoice": "Extended warranty invoice", "rsa_invoice": "RSA invoice",
                   "rto_challan": "RTO challan"}
_IN_SENTENCE = {"ew_invoice": "extended warranty invoice", "rsa_invoice": "RSA invoice"}


def _remark(code: str, title: str, text_: str, source: str, *, at: Any = None, by: str | None = None
            ) -> dict[str, Any]:
    return {"code": code, "title": title, "text": text_, "source": source,
            "at": at.isoformat() if hasattr(at, "isoformat") else at, "by": by}


def _deal_remarks(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    from audit_core.uc03_p2_audit_rules import _Facts, _rupees, deal_undercharged
    from audit_core.uc03_scrappage_registration import concession_remarks

    facts = _Facts(connection, tenant_id, journey_id)
    remarks: list[dict[str, Any]] = []
    undercharged = deal_undercharged(facts)
    for line in concession_remarks(list((undercharged.details or {}).get("scrappageConcession") or [])):
        remarks.append(_remark("SCRAPPAGE_CONCESSION", "Scrappage concession on registration", line, "Deal"))
    seen: set[str] = set()
    # FASTag not on the NDC (product owner 2026-11-18: "if it is not in NDC
    # then put a comment in system remarks"; "if NDC doesnt show fast tag and
    # invoice has, then add to deal and audit summary and make remarks"): with
    # an NDC read, a FASTag it does not charge is said so, whether or not the
    # price master prices it.
    from audit_core.uc03_account_statement import ndc_charges

    fastag = fastag_not_on_ndc(ndc_charges(connection, tenant_id=tenant_id, journey_id=journey_id)[0],
                               facts.sheet.get("fastagNotOnNdc"))
    if fastag is not None:
        seen.add("FASTag")
        remarks.append(fastag)
    for row in (facts.sheet.get("summary") or {}).get("notTaken") or []:
        product = _NOT_TAKEN_PRODUCTS.get(str(row.get("key") or ""))
        if not product or product in seen:
            continue
        seen.add(product)
        standard = row.get("standard")
        priced = f" (price master {_rupees(Decimal(str(standard)))})" if standard not in (None, "") else ""
        remarks.append(_remark("NOT_TAKEN", f"{product} not taken",
                               f"No bill on file charges the {product}{priced}: the customer did not take it, "
                               "so it is not counted as an undercharge.", "Deal"))
    return remarks


def fastag_not_on_ndc(ndc: dict[str, Decimal] | None, from_invoice: dict[str, Any] | None) -> dict[str, Any] | None:
    """The remark for a FASTag the NDC does not charge (product owner 2026-11-18),
    None with no NDC read or a FASTag on it. ``from_invoice`` is the Deal's
    ``fastagNotOnNdc``: an invoice charges it, so the Deal took it from there and
    the journey is a HIGH observation (uc03_observation_severity)."""
    from audit_core.uc03_p2_audit_rules import _rupees

    if from_invoice:
        return _remark(
            "FASTAG_NOT_ON_NDC", "FASTag not on the NDC",
            f"The NDC does not charge a FASTag, but the {from_invoice['source']} charges "
            f"{_rupees(Decimal(str(from_invoice['amount'])))} for one: it is taken into the Deal and the Audit "
            "Summary from there, and the journey is marked a HIGH observation.", "NDC")
    if ndc is None or (ndc.get("fastag_amount") or Decimal(0)) > 0:
        return None
    return _remark("FASTAG_NOT_ON_NDC", "FASTag not on the NDC",
                   "The NDC does not charge a FASTag and no invoice does: the customer did not take it from the "
                   "dealership, so it is not counted as an undercharge.", "NDC")


def _bh_remark(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    from audit_core.uc03_p2_journey360 import _bh_series

    if not _bh_series(connection, tenant_id=tenant_id, journey_id=journey_id):
        return []
    how = ("the PC confirmed it" if confirmed_bh(connection, tenant_id=tenant_id, journey_id=journey_id)
           else "read from the registration number or the RTO challan")
    return [_remark("BH_REGISTRATION", "BH-series registration",
                    f"BH-series registration ({how}): the road tax is for two years, so the price master's "
                    "RTO figure is not compared.", "Registration")]


def _closure_remarks(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """A missing document answered "Dealer not provided" or "Temporary registration"
    (p2_document_waivers since 2026-11-15; the answers on the earlier per-document
    tasks were carried over by migration 0229)."""
    rows = connection.execute(
        text(
            """
            SELECT requirement_key AS requirement, choice, reason, claimed_role, claimed_at_utc
            FROM auditcore.p2_document_waivers
            WHERE tenant_id=:t AND journey_id=:j AND status='ACCEPTED' AND choice = ANY(:choices)
            ORDER BY claimed_at_utc, requirement_key
            """
        ),
        {"t": tenant_id, "j": journey_id, "choices": [DEALER_NOT_PROVIDED, TEMPORARY_REGISTRATION]},
    ).mappings().all()
    remarks: list[dict[str, Any]] = []
    for row in rows:
        reason = str(row["reason"] or "")
        requirement = str(row["requirement"] or "")
        at = row["claimed_at_utc"].isoformat() if row["claimed_at_utc"] else None
        role = row["claimed_role"]
        document = _DOCUMENT_LABEL.get(requirement, "Document")
        if row["choice"] == DEALER_NOT_PROVIDED:
            remarks.append(_remark("DEALER_NOT_PROVIDED", f"{document}: dealer not provided",
                                   f"The customer bought it, but the dealership does not issue the "
                                   f"{_IN_SENTENCE.get(requirement, 'document')}; the PC answered \"Dealer not "
                                   f"provided\". Comment: {reason}", "Task", at=at, by=role))
        else:
            number = temporary_number(reason)
            said = f"Delivered on temporary registration {number}." if number else "Delivered on temporary registration."
            closer = {"TL": "Team Lead", "PM": "Project Manager", "REVIEWER": "Reviewer"}.get(
                str(role or "").upper(), "Team Lead")
            remarks.append(_remark("TEMPORARY_REGISTRATION", "Temporary registration",
                                   f"{said} The RTO challan was answered for by the {closer}. Comment: {reason}",
                                   "Task", at=at, by=role))
    return remarks


def _loan_remarks(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """The loan the NDC shows received with no payment receipt on file: no
    task asks for its receipt, the remark says so (product owner 2026-11-16)."""
    from audit_core.uc03_ledger_receipts import loan_without_receipt
    from audit_core.uc03_p2_audit_rules import _rupees, _when

    remarks: list[dict[str, Any]] = []
    for posting in loan_without_receipt(connection, tenant_id=tenant_id, journey_id=journey_id):
        where = "the NDC"
        number = f" ({posting['number']})" if posting.get("number") else ""
        day = f" on {_when(posting['date'])}" if posting.get("date") else ""
        remarks.append(_remark(
            "LOAN_WITHOUT_RECEIPT", "Loan received, no payment receipt",
            f"Loan amount of {_rupees(posting['amount'])} received as per {where}{number}{day}; "
            "no payment receipt available. No task is raised for it.",
            "NDC" if where == "the NDC" else "Customer ledger"))
    return remarks


def _loan_amount_remarks(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """The Delivery Order and the Bank Approval Letter print different loan amounts (product
    owner 2026-11-17: "highlight this as system remarks"). The loan counted follows the fixed
    order (receipt, NDC, DO, sanction letter: uc03_p2_audit_summary.loan_figures); the remark
    names both figures and the difference."""
    from audit_core.uc03_ledger_receipts import is_loan_net_of_charges
    from audit_core.uc03_p2_audit_rules import _rupees
    from audit_core.uc03_p2_journey360 import (
        bank_approval_sanction,
        delivery_order_loan,
    )

    do = delivery_order_loan(connection, tenant_id=tenant_id, journey_id=journey_id)
    letter = bank_approval_sanction(connection, tenant_id=tenant_id, journey_id=journey_id)
    if do is None or letter is None:
        return []
    do_amount, sanctioned = Decimal(do["amount"]), Decimal(letter["amount"])
    difference = abs(do_amount - sanctioned)
    if difference <= _LOAN_TOLERANCE:
        return []
    charges = (" -- within 2%, possibly the lender's charges deducted"
               if is_loan_net_of_charges(min(do_amount, sanctioned), max(do_amount, sanctioned)) else "")
    return [_remark(
        "LOAN_AMOUNT_DIFFERS", "Loan amount differs: DO and sanction letter",
        f"The Delivery Order shows a loan of {_rupees(do_amount)}; the Bank Approval Letter sanctions "
        f"{_rupees(sanctioned)} (a difference of {_rupees(difference)}{charges}). "
        "The Delivery Order's figure is taken ahead of the sanction letter's.",
        "Delivery Order / Bank Approval Letter")]


_LOAN_TOLERANCE = Decimal(1)


def _severity_remarks(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """The journey's observation severity when a Reviewer / Team Lead / Project
    Manager set it over the system's (2026-11-18), with their reason, beside the
    Reviewer's other remarks."""
    from audit_core.uc03_observation_severity import journey_severity

    state = journey_severity(connection, tenant_id=tenant_id, journey_id=journey_id)
    override = state.get("override")
    if not override:
        return []
    system = state.get("system")
    return [_remark("OBSERVATION_SEVERITY_SET", f"Observation severity set to {override['severity'].title()}",
                    f"Set over the system's {system.title() if system else 'none'}. Reason: {override['reason']}",
                    "Observation severity", at=override.get("at"), by=override.get("role"))]


def system_remarks(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    remarks = [
        *_deal_remarks(connection, tenant_id=tenant_id, journey_id=journey_id),
        *_severity_remarks(connection, tenant_id=tenant_id, journey_id=journey_id),
        *_bh_remark(connection, tenant_id=tenant_id, journey_id=journey_id),
        *_closure_remarks(connection, tenant_id=tenant_id, journey_id=journey_id),
        *_loan_remarks(connection, tenant_id=tenant_id, journey_id=journey_id),
        *_loan_amount_remarks(connection, tenant_id=tenant_id, journey_id=journey_id),
    ]
    return {"remarks": remarks, "count": len(remarks)}
