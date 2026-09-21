"""uc03_finance_disbursement_resolution.py — resolve the actual loan
disbursement amount for a financed deal.

finance_records.financed_amount is the RTO Challan's own hypothecation
charges (materialize_delivery_finance) -- correct, and deliberately left
alone by this module. No document ever states the actual loan amount a
bank/NBFC disbursed to the dealer: the RTO Challan and the insurance cover
note only ever confirm THAT a financer is involved (financer_name), never
how much. The real amount has to be inferred from the journey's own
payment receipts.

How Indian dealer-side auto-loan disbursement actually works (confirmed
2026-09-21): the lender pays the dealer directly via RTGS or NEFT --
never cash, and in practice never UPI/card/QR either, since those are
consumer-facing rails, not institutional bank-to-dealer transfer
mechanisms. RTGS itself has a regulatory minimum of Rs 2,00,000, and
Section 269ST of the Income Tax Act makes an aggregate cash receipt of
Rs 2,00,000+ from one person illegal outright -- a loan-sized amount
cannot legally be paid in cash at all, not just unusually. That gives a
hard, defensible exclusion filter, not just a convention to guess at.

Resolution order, deliberately conservative -- this only ever
auto-resolves the unambiguous cases and defers everything else to a
human instead of guessing:

  1. No financer named on the finance record -- nothing to resolve, skip.
  2. Already PC_CONFIRMED -- a human decision is sticky forever; never
     auto-overwritten by a later automatic pass.
  3. Zero eligible (non-Cash/UPI/Card/QR) payments after the minimum
     booking amount -- UNVERIFIED, raise a PC task. A financed deal with
     no eligible payment at all is a genuine gap, not a rendering blank.
  4. Exactly one eligible payment -- no ambiguity to resolve regardless
     of whether its text mentions the financer by name: auto-accept
     (HIGH if the name matches, MEDIUM otherwise), self-healing any
     previously-open task.
  5. Multiple eligible payments, exactly one of which matches the
     financer's name -- HIGH, the name breaks the tie.
  6. Multiple eligible payments with zero or multiple name matches --
     AMBIGUOUS, raise a PC task showing every eligible candidate; never
     guess between them.

The PC task (and the standalone, always-available correction on Journey
Documents -- uc03_finance_disbursement_resolution.confirm_loan_
disbursement, called from both) reuses the identical candidate list this
module computes, so the picker a PC sees is always exactly what this
resolver itself considered.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.errors import ConflictError, NotFoundError
from audit_core.uc03_booking_confirmation_rules import _minimum_booking_amount
from audit_core.uc03_duplicate_receipt_detection import (
    ReceiptRecord,
    compute_duplicate_groups,
    normalize_receipt_date,
    normalize_receipt_number,
)
from audit_core.workflow import cancel_workflow_task, create_workflow_task

TASK_TYPE = "FINANCE_DISBURSEMENT_REVIEW"
TL_NOTICE_TASK_TYPE = "FINANCE_DISBURSEMENT_CONFIRMED_NOTICE"
_WORKFLOW_TYPE = "UC03_FINANCE_DISBURSEMENT"

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


def _payments_after_minimum_booking(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[dict[str, Any]]:
    """Every payment on this journey (any stage), in receipt order, minus
    whichever leading ones first summed to the tenant's minimum booking
    amount -- mirrors uc03_booking_confirmation_rules.evaluate_minimum_
    booking_payment's own cumulative walk and duplicate exclusion exactly,
    so "post minimum booking amount" means the same thing here as it does
    for Booking Confirm itself."""
    rows = connection.execute(
        text(
            """
            SELECT payment_id, amount, payment_at_utc, receipt_date, receipt_number,
                   payment_method_code, payment_mode_code, receipt_bank_name,
                   receipt_remarks, payment_reference, currency_code
            FROM auditcore.payments
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND amount IS NOT NULL
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

    minimum = _minimum_booking_amount(connection, tenant_id=tenant_id)
    running_total = Decimal(0)
    threshold_reached = False
    after: list[dict[str, Any]] = []
    for row in rows:
        if row["payment_id"] in excluded_payment_ids:
            continue
        if threshold_reached:
            after.append(dict(row))
            continue
        running_total += Decimal(str(row["amount"]))
        if running_total >= minimum:
            threshold_reached = True
    return after


def eligible_loan_disbursement_candidates(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[dict[str, Any]]:
    """Payments after the minimum booking amount, restricted to modes an
    institutional loan disbursement could actually use. This is the exact
    candidate list both the automatic resolver and the PC picker (Task
    Queue and the standalone Journey Documents correction) work from."""
    return [
        row
        for row in _payments_after_minimum_booking(connection, tenant_id=tenant_id, journey_id=journey_id)
        if _is_eligible_disbursement_mode(row["payment_method_code"], row["payment_mode_code"])
    ]


def _name_matches(candidate: dict[str, Any], financer_tokens: set[str]) -> bool:
    if not financer_tokens:
        return False
    candidate_text = " ".join(
        str(candidate.get(key) or "")
        for key in ("receipt_bank_name", "receipt_remarks", "payment_reference")
    )
    candidate_tokens = _normalize_name(candidate_text)
    return bool(financer_tokens) and financer_tokens.issubset(candidate_tokens)


def _latest_finance_record(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> dict[str, Any] | None:
    row = connection.execute(
        text(
            """
            SELECT finance_record_id, provider_name, loan_disbursement_confidence
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
            SET loan_disbursement_amount=:amount,
                loan_disbursement_payment_id=:payment_id,
                loan_disbursement_confidence=:confidence,
                loan_disbursement_match_basis=:match_basis,
                updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND finance_record_id=:finance_record_id
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


def resolve_finance_disbursement(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str
) -> dict[str, Any]:
    """Try to resolve the loan disbursement amount automatically; raise a
    PC task when it can't. Never raises, safe to call on every Delivery
    materialization pass alongside materialize_delivery_finance."""
    finance_record = _latest_finance_record(connection, tenant_id=tenant_id, journey_id=journey_id)
    if finance_record is None or not (finance_record.get("provider_name") or "").strip():
        return {"skipped": True, "reason": "no_financer"}
    if finance_record["loan_disbursement_confidence"] == "PC_CONFIRMED":
        return {"skipped": True, "reason": "pc_confirmed"}

    candidates = eligible_loan_disbursement_candidates(
        connection, tenant_id=tenant_id, journey_id=journey_id
    )
    financer_tokens = _normalize_name(finance_record["provider_name"])
    matches = [c for c in candidates if _name_matches(c, financer_tokens)]

    resolved: tuple[dict[str, Any], str, str] | None = None
    if len(candidates) == 1:
        candidate = candidates[0]
        if _name_matches(candidate, financer_tokens):
            resolved = (candidate, "HIGH", f"Only eligible post-booking payment; matches financer '{finance_record['provider_name']}'.")
        else:
            resolved = (candidate, "MEDIUM", "Only eligible post-booking payment; financer name not found on the receipt.")
    elif len(matches) == 1:
        resolved = (matches[0], "HIGH", f"One of {len(candidates)} eligible payments matches financer '{finance_record['provider_name']}'.")

    if resolved is not None:
        payment, confidence, basis = resolved
        _write_disbursement(
            connection,
            tenant_id=tenant_id,
            finance_record_id=finance_record["finance_record_id"],
            payment=payment,
            confidence=confidence,
            match_basis=basis,
        )
        open_task_id = _open_task_id(connection, tenant_id=tenant_id, journey_id=journey_id)
        if open_task_id is not None:
            cancel_workflow_task(
                connection,
                tenant_id=tenant_id,
                workflow_task_id=open_task_id,
                actor_id="SYSTEM",
                reason="Automatically resolved once a matching payment became identifiable.",
            )
        return {"resolved": True, "confidence": confidence}

    reason = "no_eligible_payment" if not candidates else "ambiguous_candidates"
    if not candidates:
        _write_disbursement(
            connection,
            tenant_id=tenant_id,
            finance_record_id=finance_record["finance_record_id"],
            payment={"amount": None, "payment_id": None},
            confidence="UNVERIFIED",
            match_basis="No eligible (non-Cash/UPI/Card/QR) payment found after the minimum booking amount.",
        )
    effect_key = _effect_key(tenant_id, journey_id)
    existing = connection.execute(
        text("SELECT 1 FROM auditcore.workflow_tasks WHERE tenant_id=:t AND effect_key=:k"),
        {"t": tenant_id, "k": effect_key},
    ).scalar_one_or_none()
    if existing is None:
        create_workflow_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            workflow_type=_WORKFLOW_TYPE,
            process_area="DELIVERY",
            task_type=TASK_TYPE,
            assigned_role_code="PC",
            task_payload={"provider_name": finance_record["provider_name"]},
            effect_key=effect_key,
            correlation_id=correlation_id,
        )
    return {"resolved": False, "reason": reason}


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
                "post-booking, non-Cash/UPI/Card/QR payment on this Journey."
            ),
        )
    _write_disbursement(
        connection,
        tenant_id=tenant_id,
        finance_record_id=finance_record["finance_record_id"],
        payment=payment,
        confidence="PC_CONFIRMED",
        match_basis=f"Confirmed by {actor_id} from the post-minimum-booking payment list.",
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
