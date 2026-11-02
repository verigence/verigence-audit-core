"""uc03_finance_disbursement_resolution.py — which receipt is the loan's.

finance_records.financed_amount is the RTO Challan's own hypothecation
charges (materialize_delivery_finance) -- correct, and deliberately left
alone by this module. This module links the dealer receipt that IS the
loan disbursement (finance_records.loan_disbursement_payment_id); that
receipt then counts as the loan everywhere money received is shown
(uc03_p2_journey360.payments / _paid, the Audit Summary), never as a
customer receipt.

How Indian dealer-side auto-loan disbursement actually works (confirmed
2026-09-21): the lender pays the dealer directly via RTGS or NEFT --
never cash, and in practice never UPI/card/QR either, since those are
consumer-facing rails, not institutional bank-to-dealer transfer
mechanisms. RTGS itself has a regulatory minimum of Rs 2,00,000, and
Section 269ST of the Income Tax Act makes an aggregate cash receipt of
Rs 2,00,000+ from one person illegal outright -- a loan-sized amount
cannot legally be paid in cash at all. That gives a hard exclusion
filter: only eligible-mode payments (not Cash/UPI/Card/QR) are candidates.

Product owner rules (2026-11-02, Bhikari Singh: a Rs 2,50,000 receipt in
the customer's name was taken as the loan while the bank's sanction letter
and the NDC state another amount):

- The loan amount is what the paper states: the dealer statement's
  financier row (uc03_account_statement.statement_loan) and the newest
  Bank Approval Letter's sanctioned amount (uc03_p2_journey360.
  bank_approval_sanction). The two are checked against each other; the
  basis written says whether they agree.
- A receipt in the financier's name is the loan receipt. A receipt in the
  customer's name that EQUALS the loan amount (within Rs 1) and was paid
  in an eligible mode is the loan receipt too.
- No receipt is ever picked by guessing: with no name match and no amount
  match the PC is asked (FINANCE_DISBURSEMENT_REVIEW), and a PC's
  confirmation is final.

Candidates are every eligible-mode payment of the booking, whatever its
place against the minimum booking amount, less the second copy of a
receipt uploaded twice and receipts from another customer's file
(FOREIGN evidence). "In the financier's name" is read off the receipt's
own name (received from) and its remarks / reference. Its bank name counts
only when marked as the financier's payment ("... (FINANCER)"): unmarked,
it is on live receipts the dealer's own deposit account ("Axis Bank", "SBI
493A/C") and would make every receipt banked there "name" a financier of
the same bank. A customer's name with the hypothecation noted ("SIBARAM
NAYAK ( HP TO UNION BANK OF INDIA )") is the customer's name.

Resolution order:

  1. No financer named on the finance record -- nothing to resolve, skip.
  2. Already PC_CONFIRMED -- sticky forever; never overwritten here.
  3. Exactly one candidate in the financier's name -- HIGH. Several -- no
     link, PC task.
  4. Else exactly one candidate equal (Rs 1) to a stated loan amount --
     HIGH, the basis naming the document whose amount it matched. Several
     -- no link, PC task.
  5. Else -- no link (UNVERIFIED), PC task. Any earlier automatic link is
     cleared: it was a guess.

The PC task (and the standalone, always-available correction on Journey
Documents -- confirm_loan_disbursement, called from both) reuses the
identical candidate list this module computes, so the picker a PC sees is
always exactly what this resolver itself considered.

plan_finance_disbursement works out the outcome without writing (the
LOAN_RECEIPT_RESOLVE worker job's dry run, uc03_loan_receipt_resolve);
resolve_finance_disbursement applies it.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.errors import ConflictError, NotFoundError
from audit_core.uc03_duplicate_receipt_detection import (
    ReceiptRecord,
    compute_duplicate_groups,
    normalize_receipt_date,
    normalize_receipt_number,
)
from audit_core.uc03_finance_rule import receipt_financier
from audit_core.workflow import cancel_workflow_task, create_workflow_task

TASK_TYPE = "FINANCE_DISBURSEMENT_REVIEW"
TL_NOTICE_TASK_TYPE = "FINANCE_DISBURSEMENT_CONFIRMED_NOTICE"
_WORKFLOW_TYPE = "UC03_FINANCE_DISBURSEMENT"

# A receipt equals the loan amount within a rupee (product owner, 2026-11-02).
AMOUNT_TOLERANCE = Decimal(1)

# Consumer-facing payment rails an institutional loan disbursement never
# uses -- classify_payment_mode (uc03_payment_mode.py) has no dedicated
# code for any of these (they fall through to OTHERS there), so they're
# detected directly off the raw payment_method_code text instead of via
# payment_mode_code.
_DISALLOWED_MODE_TOKENS = ("UPI", "CARD", "QR", "POS", "WALLET")
_DISALLOWED_MODE_CODES = {"CASH"}

# Noise words stripped before comparing a financer name against a
# receipt's own bank-name/remarks text -- these are legal-entity/product
# suffixes, not identifying information, and "UCO Bank" vs "UCO Bank Ltd"
# should still match.
_NAME_NOISE_WORDS = {
    "BANK", "LTD", "LIMITED", "FINANCE", "FINANCIAL", "SERVICES", "SERVICE",
    "PVT", "PRIVATE", "CO", "COMPANY", "NBFC", "CORP", "CORPORATION",
    "INDIA", "OF", "AND", "THE", "AUTO", "MOTOR", "MOTORS",
}

# Common abbreviations for major Indian auto lenders that a dealer
# receipt's free-text bank-name/remarks field routinely uses instead of
# the lender's full legal name. Extend as new ones are seen live -- this
# is universal knowledge about lenders, not tenant data, so it stays a
# plain module-level table rather than a database.
_LENDER_ALIASES: dict[str, str] = {
    "MMFSL": "MAHINDRA FINANCIAL SERVICES",
    "MAHINDRA FINANCE": "MAHINDRA FINANCIAL SERVICES",
    "CHOLA": "CHOLAMANDALAM FINANCE",
    "CHOLAMANDALAM": "CHOLAMANDALAM FINANCE",
    "TVSCS": "TVS CREDIT SERVICES",
    "L&T FINANCE": "L AND T FINANCE",
    "LNT FINANCE": "L AND T FINANCE",
    "IDFC": "IDFC FIRST BANK",
    "BOB": "BANK OF BARODA",
    "PNB": "PUNJAB NATIONAL BANK",
    "SBI": "STATE BANK OF INDIA",
    "HDFC": "HDFC BANK",
    "ICICI": "ICICI BANK",
}

_WORD_RE = re.compile(r"[A-Z0-9]+")


def _normalize_name(raw: Any) -> set[str]:
    """Tokenize into a noise-word-free set, additionally expanding any
    known lender abbreviation found ANYWHERE in the text (not only when
    the whole string is exactly the abbreviation) -- a receipt's own
    remarks/bank-name field embeds the abbreviation in a full sentence
    ("Disbursed by MMFSL RTGS"), it never IS just "MMFSL"."""
    text_value = str(raw or "").upper()
    tokens = set(_WORD_RE.findall(text_value))
    for alias, target in _LENDER_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", text_value):
            tokens |= set(_WORD_RE.findall(target))
    return tokens - _NAME_NOISE_WORDS


def _is_eligible_disbursement_mode(payment_method_code: Any, payment_mode_code: Any) -> bool:
    mode_code = str(payment_mode_code or "").strip().upper()
    if mode_code in _DISALLOWED_MODE_CODES:
        return False
    method_compact = re.sub(r"[^A-Z0-9]", "", str(payment_method_code or "").upper())
    return not any(token in method_compact for token in _DISALLOWED_MODE_TOKENS)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _eligible_payments(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[dict[str, Any]]:
    """Every payment on this journey (any stage, wherever it falls against
    the minimum booking amount -- 2026-11-02, Bhikari Singh: the loan was the
    FIRST receipt by date and the old after-minimum-booking walk never saw
    it), in receipt order, less the later copies of a receipt uploaded twice
    and receipts from another customer's file."""
    rows = connection.execute(
        text(
            """
            SELECT payment_id, amount, payment_at_utc, receipt_date, receipt_number,
                   payment_method_code, payment_mode_code, receipt_bank_name,
                   receipt_customer_name, receipt_remarks, payment_reference, currency_code
            FROM auditcore.payments p
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND amount IS NOT NULL AND amount > 0
              -- another customer's receipt is no disbursement of this loan (2026-10-31)
              AND NOT EXISTS (SELECT 1 FROM auditcore.evidence fe
                              WHERE fe.tenant_id=p.tenant_id AND fe.di_document_id=p.source_di_document_id
                                AND fe.association_status='FOREIGN')
            ORDER BY payment_at_utc NULLS LAST, created_at_utc, payment_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()

    excluded_payment_ids: set[Any] = set()
    duplicate_groups = compute_duplicate_groups([
        ReceiptRecord(
            document_id=row["payment_id"],
            stage_code="BOOKING",
            document_type_key="dealer_receipt",
            receipt_number=normalize_receipt_number(row["receipt_number"]),
            amount=Decimal(str(row["amount"])),
            receipt_date=normalize_receipt_date(row["receipt_date"]),
        )
        for row in rows
        if row["receipt_number"] is not None and row["receipt_date"] is not None
    ])
    for group in duplicate_groups:
        for document in group.documents[1:]:
            excluded_payment_ids.add(document.document_id)
    return [dict(row) for row in rows if row["payment_id"] not in excluded_payment_ids]


def eligible_loan_disbursement_candidates(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[dict[str, Any]]:
    """Every payment of the booking in a mode an institutional loan
    disbursement could actually use. This is the exact candidate list both
    the automatic resolver and the PC picker (Task Queue and the standalone
    Journey Documents correction) work from."""
    return [
        row
        for row in _eligible_payments(connection, tenant_id=tenant_id, journey_id=journey_id)
        if _is_eligible_disbursement_mode(row["payment_method_code"], row["payment_mode_code"])
    ]


# A customer's name with the loan's hypothecation noted after it
# ("SIBARAM NAYAK ( HP TO UNION BANK OF INDIA )") is the customer's name: such a
# receipt is the loan only when it equals the loan amount.
_HP_NOTE = re.compile(r"\(?\s*\bH\s*\.?\s*P\s*\.?\s+TO\b[^)]*\)?", re.IGNORECASE)
# A name marked as the financier's payment ("STATE BANK OF INDIA (FINANCER)",
# "ICICI Finance (D.O)").
_FINANCIER_NOTE = re.compile(r"\(\s*(?:FINANC\w*|D\s*\.?\s*/?\s*O\.?)\s*\)", re.IGNORECASE)
# Words of a legal name that never tell two lenders apart ("Bank of India Ltd").
_LEGAL_WORDS = {"LTD", "LIMITED", "PVT", "PRIVATE", "CO", "COMPANY", "CORP", "CORPORATION", "THE"}


def _lender_part(provider_name: Any) -> str:
    """The lender's name without the branch the RTO challan prints after it
    ("STATE BANK OF INDIA, UTKAL UNIV CAMPUS - BHUBANESWAR")."""
    return str(provider_name or "").split(",")[0]


def _exact_words(value: Any) -> tuple[str, ...]:
    return tuple(w for w in _WORD_RE.findall(str(value or "").upper()) if w not in _LEGAL_WORDS)


def _financier_on_receipt(candidate: dict[str, Any], customer_names: tuple[str, ...] = ()) -> str | None:
    """The financier a receipt says it was received from, read as the finance
    rule reads a receipt's payer ("HDFC Bank (Financer)" -> "HDFC Bank",
    "ICICI Finance (D.O)" -> "ICICI Finance"); None when the payer is the
    customer, the customer with the hypothecation noted, or no one. The bank
    name counts only when the receipt marks it as the financier's payment
    ("STATE BANK OF INDIA (FINANCER)" printed there on live): unmarked, it is
    the dealer's own deposit account."""
    payer = str(candidate.get("receipt_customer_name") or "")
    if payer.strip() and not _HP_NOTE.search(payer):
        named = receipt_financier(payer, customer_names=customer_names)
        if named:
            return named
    bank = str(candidate.get("receipt_bank_name") or "")
    if _FINANCIER_NOTE.search(bank) and not _HP_NOTE.search(bank):
        return receipt_financier(bank, customer_names=customer_names)
    return None


def _name_matches(
    candidate: dict[str, Any], financer_tokens: set[str], customer_names: tuple[str, ...] = (),
) -> bool:
    """Whether the receipt is in the financier's name: its payer is a
    financier whose name holds every word of the deal's financier, or its
    remarks / reference name the financier ("Disbursed by MMFSL RTGS",
    "BY TRANSFER-NEFT*HDFC0000240*...*HDFC Bank Ltd"). An unmarked bank name is
    not read: it is the dealer's own deposit account, not who paid."""
    if not financer_tokens:
        return False
    candidate_text = " ".join((
        _financier_on_receipt(candidate, customer_names) or "",
        _HP_NOTE.sub(" ", str(candidate.get("receipt_remarks") or "")),
        str(candidate.get("payment_reference") or ""),
    ))
    return financer_tokens.issubset(_normalize_name(candidate_text))


def names_financier(
    candidate: dict[str, Any], provider_name: Any, *, customer_names: tuple[str, ...] = (),
) -> bool:
    """Whether a receipt is in the name of this financier. A lender whose
    every word is generic ("Bank of India") has no distinguishing word to
    look for: only the same name, word for word, on the receipt's payer is it
    (never "State Bank of India")."""
    lender = _lender_part(provider_name)
    tokens = _normalize_name(lender)
    if tokens:
        return _name_matches(candidate, tokens, customer_names)
    on_receipt = _financier_on_receipt(candidate, customer_names)
    return bool(on_receipt) and bool(_exact_words(lender)) and _exact_words(on_receipt) == _exact_words(lender)


def journey_customer_names(connection: Connection, *, tenant_id: str, journey_id: UUID) -> tuple[str, ...]:
    """The customer's names on the Journey, as the finance rule reads them (a
    payer who is the customer is never a financier)."""
    from audit_core.uc03_finance_rule import _customer_names

    return _customer_names(connection, tenant_id=tenant_id, journey_id=journey_id)


def stated_loan_amounts(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """The loan amount as the paper states it (product owner, 2026-11-02): the
    dealer statement's financier row and the newest Bank Approval Letter's
    sanctioned amount, each with the document it comes from."""
    from audit_core.uc03_account_statement import statement_loan
    from audit_core.uc03_p2_journey360 import bank_approval_sanction

    out: list[dict[str, Any]] = []
    stated = statement_loan(connection, tenant_id=tenant_id, journey_id=journey_id)
    loan = _to_decimal((stated or {}).get("loan"))
    if stated is not None and loan is not None and loan > 0:
        out.append({"amount": loan, "source": f"{stated['source']} financier row",
                    "documentId": str(stated.get("documentId") or "") or None})
    letter = bank_approval_sanction(connection, tenant_id=tenant_id, journey_id=journey_id)
    if letter is not None:
        out.append({"amount": letter["amount"], "source": "Bank Approval Letter (sanctioned amount)",
                    "documentId": letter["documentId"]})
    return out


def _rupees(amount: Any) -> str:
    value = _to_decimal(amount)
    return "Rs ?" if value is None else f"Rs {value:,.2f}"


def _stated_matched(candidate: dict[str, Any], stated: list[dict[str, Any]]) -> dict[str, Any] | None:
    amount = _to_decimal(candidate.get("amount"))
    if amount is None:
        return None
    return next((s for s in stated if abs(amount - s["amount"]) <= AMOUNT_TOLERANCE), None)


def _stated_cross_check(stated: list[dict[str, Any]]) -> str:
    """The statement and the letter checked against each other."""
    if len(stated) < 2:
        return ""
    first, second = stated[0], stated[1]
    if abs(first["amount"] - second["amount"]) <= AMOUNT_TOLERANCE:
        return f" The {first['source']} and the {second['source']} agree."
    return (f" The {first['source']} states {_rupees(first['amount'])} and the {second['source']} "
            f"{_rupees(second['amount'])}: they disagree.")


def _receipt_label(candidate: dict[str, Any]) -> str:
    number = candidate.get("receipt_number")
    return f"Receipt {number}" if number else "The receipt"


def _latest_finance_record(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> dict[str, Any] | None:
    row = connection.execute(
        text(
            """
            SELECT finance_record_id, provider_name, loan_disbursement_confidence,
                   loan_disbursement_payment_id, loan_disbursement_amount, loan_disbursement_match_basis
            FROM auditcore.finance_records
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            ORDER BY created_at_utc DESC, finance_record_id DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def _write_disbursement(
    connection: Connection,
    *,
    tenant_id: str,
    finance_record_id: UUID,
    payment: dict[str, Any],
    confidence: str,
    match_basis: str,
) -> None:
    connection.execute(
        text(
            """
            UPDATE auditcore.finance_records
            SET loan_disbursement_amount=CAST(:amount AS numeric),
                loan_disbursement_payment_id=CAST(:payment_id AS uuid),
                loan_disbursement_confidence=:confidence,
                loan_disbursement_match_basis=:match_basis,
                updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND finance_record_id=:finance_record_id
              -- written only when it changes: the resolver runs on every
              -- Delivery pass, and the Journey's ETag follows updated_at_utc
              AND (loan_disbursement_amount IS DISTINCT FROM CAST(:amount AS numeric)
                   OR loan_disbursement_payment_id IS DISTINCT FROM CAST(:payment_id AS uuid)
                   OR loan_disbursement_confidence IS DISTINCT FROM :confidence
                   OR loan_disbursement_match_basis IS DISTINCT FROM :match_basis)
            """
        ),
        {
            "tenant_id": tenant_id,
            "finance_record_id": finance_record_id,
            "amount": payment["amount"],
            "payment_id": payment["payment_id"],
            "confidence": confidence,
            "match_basis": match_basis,
        },
    )


def _effect_key(tenant_id: str, journey_id: UUID) -> str:
    return f"task:finance-disbursement-review:{tenant_id}:{journey_id}"


def _open_task_id(connection: Connection, *, tenant_id: str, journey_id: UUID) -> UUID | None:
    return connection.execute(
        text(
            """
            SELECT workflow_task_id FROM auditcore.workflow_tasks
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND task_type=:task_type
              AND task_status IN ('PENDING','READY','CLAIMED','IN_PROGRESS','RETRY_WAIT')
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "task_type": TASK_TYPE},
    ).scalar_one_or_none()


def plan_finance_disbursement(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> dict[str, Any]:
    """What the resolver would do, without writing anything: the receipt to
    link as the loan (HIGH) or none (UNVERIFIED, the PC is asked), with the
    basis, and what the finance record holds now (``before``)."""
    finance_record = _latest_finance_record(connection, tenant_id=tenant_id, journey_id=journey_id)
    if finance_record is None or not (finance_record.get("provider_name") or "").strip():
        return {"skipped": True, "reason": "no_financer", "before": None}
    before = {
        "confidence": finance_record["loan_disbursement_confidence"],
        "paymentId": finance_record["loan_disbursement_payment_id"],
        "amount": finance_record["loan_disbursement_amount"],
        "basis": finance_record["loan_disbursement_match_basis"],
    }
    if finance_record["loan_disbursement_confidence"] == "PC_CONFIRMED":
        return {"skipped": True, "reason": "pc_confirmed", "before": before}

    provider = finance_record["provider_name"]
    plan: dict[str, Any] = {"skipped": False, "before": before, "financeRecordId": finance_record["finance_record_id"],
                            "providerName": provider, "stated": []}
    candidates = eligible_loan_disbursement_candidates(connection, tenant_id=tenant_id, journey_id=journey_id)
    plan["candidates"] = len(candidates)

    def link(payment: dict[str, Any], basis: str) -> dict[str, Any]:
        return {**plan, "resolved": True, "reason": None, "confidence": "HIGH", "payment": payment,
                "basis": basis}

    def ask(reason: str, basis: str) -> dict[str, Any]:
        return {**plan, "resolved": False, "reason": reason, "confidence": "UNVERIFIED",
                "payment": {"amount": None, "payment_id": None}, "basis": basis}

    if not candidates:
        return ask("no_eligible_payment", "No eligible (non-Cash/UPI/Card/QR) payment on the booking.")

    # 1. A receipt in the financier's name is the loan receipt.
    customers = journey_customer_names(connection, tenant_id=tenant_id, journey_id=journey_id)
    named = [c for c in candidates if names_financier(c, provider, customer_names=customers)]
    if len(named) == 1:
        return link(named[0], f"{_receipt_label(named[0])} is in the financier's name "
                              f"('{provider}'); {_rupees(named[0]['amount'])}.")
    if len(named) > 1:
        return ask("ambiguous_candidates",
                   f"{len(named)} receipts are in the financier's name ('{provider}'): the PC is asked which "
                   "one is the loan.")

    # 2. Else a receipt equal to the loan amount the paper states.
    stated = stated_loan_amounts(connection, tenant_id=tenant_id, journey_id=journey_id)
    plan["stated"] = [{"amount": str(s["amount"]), "source": s["source"]} for s in stated]
    cross_check = _stated_cross_check(stated)
    equal = [(c, s) for c in candidates if (s := _stated_matched(c, stated)) is not None]
    if len(equal) == 1:
        payment, source = equal[0]
        return link(payment, f"{_receipt_label(payment)} ({_rupees(payment['amount'])}, "
                             f"{payment.get('payment_method_code') or payment.get('payment_mode_code') or 'mode not read'}) "
                             f"equals the loan amount on the {source['source']} "
                             f"({_rupees(source['amount'])}).{cross_check}")
    if len(equal) > 1:
        return ask("ambiguous_candidates",
                   f"{len(equal)} receipts equal the loan amount on the "
                   f"{' / '.join(dict.fromkeys(s['source'] for _c, s in equal))}: the PC is asked which one is the "
                   f"loan.{cross_check}")

    # 3. No guessing: the PC is asked.
    if stated:
        amounts = "; ".join(f"{s['source']} {_rupees(s['amount'])}" for s in stated)
        basis = f"No receipt is in the financier's name or equals the loan amount ({amounts}): the PC is asked."
    else:
        basis = ("No receipt is in the financier's name, and no NDC / settlement financier row or Bank Approval "
                 "Letter states the loan amount: the PC is asked.")
    return ask("no_match", basis)


def resolve_finance_disbursement(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str
) -> dict[str, Any]:
    """Link the loan's receipt automatically when the rules identify it; else
    clear any automatic link and raise a PC task. Never overwrites a PC's
    confirmation. Safe to call on every Delivery materialization pass
    alongside materialize_delivery_finance."""
    plan = plan_finance_disbursement(connection, tenant_id=tenant_id, journey_id=journey_id)
    if plan["skipped"]:
        return {"skipped": True, "reason": plan["reason"]}
    _write_disbursement(
        connection,
        tenant_id=tenant_id,
        finance_record_id=plan["financeRecordId"],
        payment=plan["payment"],
        confidence=plan["confidence"],
        match_basis=plan["basis"],
    )
    if plan["resolved"]:
        open_task_id = _open_task_id(connection, tenant_id=tenant_id, journey_id=journey_id)
        if open_task_id is not None:
            cancel_workflow_task(
                connection,
                tenant_id=tenant_id,
                workflow_task_id=open_task_id,
                actor_id="SYSTEM",
                reason="Automatically resolved once a matching payment became identifiable.",
            )
        return {"resolved": True, "confidence": plan["confidence"]}

    effect_key = _effect_key(tenant_id, journey_id)
    # One task per financed spell: a task cancelled because the deal stopped
    # being financed (uc03_finance_rule) is raised again, under a fresh key,
    # once the documents prove finance again (2026-10-27 review).
    earlier = connection.execute(
        text(
            """
            SELECT task_status FROM auditcore.workflow_tasks
            WHERE tenant_id=:t AND (effect_key=:k OR effect_key LIKE :prefix)
            ORDER BY created_at_utc DESC
            """
        ),
        {"t": tenant_id, "k": effect_key, "prefix": f"{effect_key}:%"},
    ).scalars().all()
    if earlier and earlier[0] == "CANCELLED" and _open_task_id(connection, tenant_id=tenant_id, journey_id=journey_id) is None:
        effect_key = f"{effect_key}:{len(earlier) + 1}"
        earlier = []
    if not earlier:
        create_workflow_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            workflow_type=_WORKFLOW_TYPE,
            process_area="DELIVERY",
            task_type=TASK_TYPE,
            assigned_role_code="PC",
            task_payload={"provider_name": plan["providerName"]},
            effect_key=effect_key,
            correlation_id=correlation_id,
        )
    return {"resolved": False, "reason": plan["reason"]}


def confirm_loan_disbursement(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    payment_id: UUID,
    actor_id: str,
    correlation_id: str,
) -> dict[str, Any]:
    """A PC's explicit pick, from either the Task Queue's inline picker or
    the standalone Journey Documents correction -- same candidate list,
    same write, same TL notice either way. Re-validates the chosen payment
    against the live eligible-candidate list rather than trusting the
    caller: candidates can move between when a picker was rendered and
    when Confirm was actually pressed."""
    finance_record = _latest_finance_record(connection, tenant_id=tenant_id, journey_id=journey_id)
    if finance_record is None:
        raise NotFoundError(
            error_code="VAC-NF-012",
            title="Finance record not found",
            detail="No finance record exists for this Journey.",
        )
    candidates = eligible_loan_disbursement_candidates(
        connection, tenant_id=tenant_id, journey_id=journey_id
    )
    payment = next((c for c in candidates if c["payment_id"] == payment_id), None)
    if payment is None:
        raise ConflictError(
            error_code="VAC-CONFLICT-020",
            title="Payment is not an eligible loan disbursement candidate",
            detail=(
                "The selected payment is not (or is no longer) an eligible "
                "non-Cash/UPI/Card/QR payment on this Journey."
            ),
        )
    _write_disbursement(
        connection,
        tenant_id=tenant_id,
        finance_record_id=finance_record["finance_record_id"],
        payment=payment,
        confidence="PC_CONFIRMED",
        match_basis=f"Confirmed by {actor_id} from the eligible payment list.",
    )
    open_task_id = _open_task_id(connection, tenant_id=tenant_id, journey_id=journey_id)
    if open_task_id is not None:
        cancel_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=open_task_id,
            actor_id=actor_id,
            reason="Confirmed by PC.",
        )
    create_workflow_task(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        workflow_type=_WORKFLOW_TYPE,
        process_area="DELIVERY",
        task_type=TL_NOTICE_TASK_TYPE,
        assigned_role_code="TL",
        task_payload={
            "provider_name": finance_record["provider_name"],
            "amount": str(payment["amount"]),
            "confirmedBy": actor_id,
        },
        correlation_id=correlation_id,
    )
    return {"amount": payment["amount"], "paymentId": payment["payment_id"]}
