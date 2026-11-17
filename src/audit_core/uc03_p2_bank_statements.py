"""uc03_p2_bank_statements.py -- the dealership's own bank account statements, uploaded
on the Data uploads tab and reconciled against the payment receipts on file (product
owner 2026-11-14).

"bank statement payment reconcile have to done.. this could be per outlet or per
dealership, file could be .pdf, or excel.. different bank will have different format..
add a section Upload in the Verigence, and then the above files needs to be read and
checked against the payment receipts for that particular dealership or outlet and verify
the payment is reconciled.. i would like to know how many payments doesnt got identified
from the bank statement and which all journey got fully verified and which partially and
how much amount and how much left."
"when we do cross check it should be from +- 5 days for which the account statement is..
and only those bookings and journey need to be verified."

Reading: bank_statement_parsers (any bank, PDF with text or Excel / CSV). Matching:
bank_reconciliation (pure). This module stores the statement and its transactions, runs
the dealership's reconciliation over every current statement of the dealership (each
receipt may have gone into any of its accounts), and reports per statement: the receipts
in its period +/- 5 days and their verdicts, the Journeys fully / partly / not verified
with the amount verified and left, and the credits no receipt stands for -- marking those
that name a customer on file (money received, no receipt in Verigence).

Who: anyone on the Project (product owner 2026-11-14: "bankstatements can be uploaded by
anyone"), for the dealerships they are mapped to.

Status (product owner 2026-11-14: "if payment receipt is verified, its status to be updated,
and if all got verified, status of that journey should be updated to bank verified"): a
receipt found in the bank gets a VERIFIED payment verification (and its open "payment not
evidenced in a bank statement" finding is resolved); a Journey whose every bank receipt (any
date) is found is BANK_VERIFIED. Cash receipts, deposited in batches, do not hold it back.
"""
from __future__ import annotations

import io
import json
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import Connection, text

from audit_core import bank_reconciliation as br
from audit_core.bank_statement_parsers import (
    CREDIT,
    ScannedPdf,
    StatementUnreadable,
    read_statement,
)
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import authorize_p2_any
from audit_core.uc03_p2_dms_dumps import (
    _ADMIN_ROLES,
    _READ,
    _dealers,
    _month,
    _name_of,
    _next_month,
    _today,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-bank-statements"])

MAX_BYTES = 20 * 1024 * 1024

JOURNEY_VERIFIED, JOURNEY_PARTIAL, JOURNEY_NOT_VERIFIED, JOURNEY_CASH_ONLY = (
    "VERIFIED", "PARTIAL", "NOT_VERIFIED", "CASH_ONLY")
OUTCOME_LABELS = {
    br.MATCHED: "Found in bank", br.AMBIGUOUS: "Several possible credits", br.NOT_FOUND: "Not found in bank",
    br.OUTSIDE_DATES: "Outside statement dates",
    br.CASH: "Cash (not traceable one by one)", br.CARD: "Card (settled in batches)",
}
BASIS_LABELS = {
    br.REFERENCE: "UTR / reference", br.NAME: "Customer name", br.NAME_PREFIX: "Customer name (shortened)",
    br.AMOUNT_DATE: "Amount and date only", br.CASH_DEPOSIT: "Cash deposit of the same amount",
    br.NAME_SPLIT: "Paid in parts (name / reference)", br.COMBINED: "One credit for several receipts",
    br.LOAN_REFERENCE: "Loan: D.O. / loan reference", br.LOAN_NAME: "Loan: customer name and loan amount",
    br.LOAN_FINANCIER: "Loan: financier and loan amount", br.LOAN_AMOUNT: "Loan: loan amount and date only",
}
KIND_LABELS = {
    br.K_CUSTOMER: "Customer / third party", br.K_CASH: "Cash deposit", br.K_CARD: "Card / UPI settlement",
    br.K_LOAN: "Loan / financier", br.K_INSURANCE: "Insurer", br.K_INTERNAL: "Own transfer",
}
JOURNEY_LABELS = {
    JOURNEY_VERIFIED: "Fully verified", JOURNEY_PARTIAL: "Partly verified", JOURNEY_NOT_VERIFIED: "Not verified",
    JOURNEY_CASH_ONLY: "Cash only",
}


def _access(connection: Connection, tenant_id: str, principal: HumanPrincipal,
            client: SecurityAuthorizationClient) -> set[str]:
    """Anyone on the Project may upload and read; administrators see every dealership."""
    access = authorize_p2_any(connection, tenant_id=tenant_id, journey_id=None, human_principal=principal,
                              authorization_client=client, permission_keys=_READ)
    return {"ADMIN"} if (access.functional_role or "").upper() in _ADMIN_ROLES else {"MEMBER"}


def _dec(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


def _money(value: Any) -> str:
    return f"{_dec(value):.2f}"


# --------------------------------------------------------------------------- loading

# A statement's transactions' first and last dates: the receipts it checks are dated within
# them (product owner 2026-11-17: "lets change the time to statement transaction dates").
_TXN_DATES = """
    LEFT JOIN LATERAL (SELECT min(x.txn_date) AS txn_from, max(x.txn_date) AS txn_to
                       FROM auditcore.p2_bank_statement_txns x
                       WHERE x.tenant_id = s.tenant_id AND x.statement_id = s.statement_id) tx ON true
"""


def _check_dates(r: Any) -> tuple[date, date]:
    return r["txn_from"] or r["period_from"], r["txn_to"] or r["period_to"]


def _active_statements(connection: Connection, tenant_id: str, dealer_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(
        text("""
            SELECT s.*, tx.txn_from, tx.txn_to FROM auditcore.p2_bank_statements s
        """ + _TXN_DATES + """
            WHERE s.tenant_id = :t AND s.dealer_id = :d AND s.status = 'ACTIVE'
            ORDER BY s.period_from, s.uploaded_at_utc
        """),
        {"t": tenant_id, "d": dealer_id},
    ).mappings()]


def _credits(connection: Connection, tenant_id: str, statements: list[dict[str, Any]]
             ) -> tuple[list[br.Credit], dict[str, str]]:
    """Every credit of the current statements; a credit printed again on a second upload
    of the same account (overlapping periods) is counted once."""
    if not statements:
        return [], {}
    by_id = {str(s["statement_id"]): s for s in statements}
    rows = connection.execute(
        text("""
            SELECT * FROM auditcore.p2_bank_statement_txns
            WHERE tenant_id = :t AND CAST(statement_id AS text) = ANY(:ids) AND direction = 'CR'
            ORDER BY txn_date NULLS LAST, statement_id, row_no
        """),
        {"t": tenant_id, "ids": list(by_id)},
    ).mappings().all()
    seen: dict[tuple[Any, ...], str] = {}
    duplicates: dict[str, str] = {}
    out = []
    for r in rows:
        statement = by_id[str(r["statement_id"])]
        key = f"{r['statement_id']}:{r['row_no']}"
        identity = (statement["account_number"] or str(r["statement_id"]), r["txn_date"], _dec(r["amount"]),
                    re.sub(r"\s+", "", str(r["narration"]).upper()), r["balance"])
        if identity in seen:
            duplicates[key] = seen[identity]
            continue
        seen[identity] = key
        out.append(br.Credit(
            key=key, statement_id=str(r["statement_id"]),
            outlet_id=str(statement["outlet_id"]) if statement["outlet_id"] else None,
            bank=statement["bank_name"], txn_date=r["txn_date"] or r["value_date"], amount=_dec(r["amount"]),
            narration=r["narration"] or "", reference=r["reference"], cheque_number=r["cheque_number"],
            kind=br.classify_credit(r["narration"] or "", statement["account_holder"]),
        ))
    return out, duplicates


_RECEIPTS_SQL = """
    SELECT p.payment_id, p.journey_id, j.outlet_id, p.amount, p.receipt_number,
           COALESCE(p.receipt_date, p.payment_reference_date, (p.payment_at_utc AT TIME ZONE 'Asia/Kolkata')::date)
             AS receipt_date,
           COALESCE(p.payment_mode_code, p.payment_method_code) AS mode, p.payment_reference,
           p.receipt_bank_name, p.receipt_customer_name, c.legal_name, c.display_name,
           EXISTS (SELECT 1 FROM auditcore.finance_records f
                   WHERE f.tenant_id = p.tenant_id AND f.journey_id = p.journey_id
                     AND f.loan_disbursement_payment_id = p.payment_id) AS is_loan
    FROM auditcore.payments p
    JOIN auditcore.journeys j ON j.tenant_id = p.tenant_id AND j.journey_id = p.journey_id
    JOIN auditcore.evidence e
      ON e.tenant_id = p.tenant_id AND e.journey_id = p.journey_id AND e.di_document_id = p.source_di_document_id
    LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
    WHERE p.tenant_id = :t AND j.dealer_id = :d AND p.amount > 0
      AND p.status_source = 'EVIDENCE' AND e.association_status = 'ACTIVE'
      AND COALESCE(p.receipt_date, p.payment_reference_date, (p.payment_at_utc AT TIME ZONE 'Asia/Kolkata')::date)
          BETWEEN :f AND :l
    ORDER BY p.journey_id, receipt_date, p.created_at_utc, p.payment_id
"""


def _names(*values: Any) -> list[str]:
    out = []
    for value in values:
        name = re.sub(r"\s+", " ", str(value or "")).strip()
        if name and not re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", name, re.IGNORECASE) and name not in out:
            out.append(name)
    return out


def _receipts(connection: Connection, tenant_id: str, dealer_id: str, first: date, last: date) -> list[br.Receipt]:
    """The receipts the Journey 360 counts (evidenced, document on file, the same receipt
    uploaded twice once), dated in the range."""
    from audit_core.uc03_p2_journey360 import _duplicate_receipts

    rows = connection.execute(text(_RECEIPTS_SQL), {"t": tenant_id, "d": dealer_id, "f": first, "l": last}
                              ).mappings().all()
    by_journey: dict[Any, list[Any]] = {}
    for r in rows:
        by_journey.setdefault(r["journey_id"], []).append(r)
    out = []
    for journey_rows in by_journey.values():
        duplicates = _duplicate_receipts(journey_rows)
        for r in journey_rows:
            if r["payment_id"] in duplicates:
                continue
            out.append(br.Receipt(
                key=str(r["payment_id"]), journey_id=str(r["journey_id"]),
                outlet_id=str(r["outlet_id"]) if r["outlet_id"] else None, amount=_dec(r["amount"]),
                receipt_date=r["receipt_date"], mode=r["mode"], reference=r["payment_reference"],
                bank=r["receipt_bank_name"],
                customer_names=_names(r["receipt_customer_name"], r["legal_name"], r["display_name"]),
                is_loan=bool(r["is_loan"]), source="RECEIPT", number=r["receipt_number"],
            ))
    return out


def _people(connection: Connection, tenant_id: str, dealer_id: str) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """Every Journey of the dealership: its customer's names, and its outlet / reference."""
    rows = connection.execute(
        text("""
            SELECT j.journey_id, j.outlet_id, j.journey_reference, c.legal_name, c.display_name
            FROM auditcore.journeys j
            LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
            WHERE j.tenant_id = :t AND j.dealer_id = :d
        """),
        {"t": tenant_id, "d": dealer_id},
    ).mappings().all()
    people = {str(r["journey_id"]): _names(r["legal_name"], r["display_name"]) for r in rows}
    meta = {str(r["journey_id"]): {"outletId": str(r["outlet_id"]) if r["outlet_id"] else None,
                                   "reference": r["journey_reference"]} for r in rows}
    return people, meta


# The loan papers of a Journey: (document type, field) -> what it gives the Loan.
_LOAN_FIELDS = {
    ("delivery_order_cover", "loan_amount"): "amount",
    ("delivery_order_cover", "disbursement_reference"): "reference",
    ("delivery_order_cover", "delivery_order_number"): "reference",
    ("delivery_order_cover", "disbursement_date"): "disbursed",
    ("delivery_order_cover", "financier_name"): "financier",
    ("bank_approval_letter", "finance_dealer_payout_amount"): "amount",
    ("bank_approval_letter", "finance_sanctioned_amount"): "amount",
    ("bank_approval_letter", "finance_application_number"): "reference",
    ("bank_approval_letter", "finance_sanction_letter_number"): "reference",
    ("bank_approval_letter", "finance_disbursement_date"): "disbursed",
    ("bank_approval_letter", "finance_sanction_date"): "sanctioned",
    ("bank_approval_letter", "financier_name"): "financier",
}
_LOANS_SQL = """
    SELECT f.journey_id, COALESCE(e.document_type_key, f.source_document_type_key) AS doc, f.field_key,
           f.effective_value #>> '{}' AS value
    FROM auditcore.journey_document_extracted_fields f
    JOIN auditcore.journeys j ON j.tenant_id = f.tenant_id AND j.journey_id = f.journey_id
    JOIN auditcore.evidence e
      ON e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id AND e.di_document_id = f.di_document_id
     AND e.association_status = 'ACTIVE'
    WHERE f.tenant_id = :t AND j.dealer_id = :d
      AND COALESCE(e.document_type_key, f.source_document_type_key) IN ('delivery_order_cover', 'bank_approval_letter')
      AND f.field_key = ANY(:keys) AND jsonb_typeof(f.effective_value) IN ('string', 'number')
    UNION ALL
    SELECT r.journey_id, 'finance_record', k.key, k.value
    FROM auditcore.finance_records r
    JOIN auditcore.journeys j ON j.tenant_id = r.tenant_id AND j.journey_id = r.journey_id
    CROSS JOIN LATERAL (VALUES ('amount', CAST(r.financed_amount AS text)),
                               ('amount', CAST(r.loan_disbursement_amount AS text)),
                               ('financier', r.provider_name), ('reference', r.do_reference)) AS k(key, value)
    WHERE r.tenant_id = :t AND j.dealer_id = :d AND k.value IS NOT NULL
"""


def _loans(connection: Connection, tenant_id: str, dealer_id: str, people: dict[str, list[str]],
           meta: dict[str, Any]) -> list[br.Loan]:
    """Every loan of the dealership's Journeys, from its D.O. cover, bank approval letter and
    finance record."""
    loans: dict[str, br.Loan] = {}
    for r in connection.execute(text(_LOANS_SQL), {"t": tenant_id, "d": dealer_id,
                                                   "keys": sorted({k for _, k in _LOAN_FIELDS})}).mappings():
        jid = str(r["journey_id"])
        what = "amount" if r["doc"] == "finance_record" and r["field_key"] == "amount" else (
            r["field_key"] if r["doc"] == "finance_record" else _LOAN_FIELDS.get((r["doc"], r["field_key"])))
        value = str(r["value"] or "").strip()
        if not what or not value:
            continue
        loan = loans.get(jid)
        if loan is None:
            loan = loans[jid] = br.Loan(journey_id=jid, outlet_id=(meta.get(jid) or {}).get("outletId"),
                                        customer_names=people.get(jid, []))
        if what == "amount":
            amount = _loan_money(value)
            if amount is not None and amount > 0:
                loan.amounts.add(amount)
        elif what == "reference":
            loan.references.append(value)
        elif what == "financier":
            loan.financiers.append(value)
        else:
            day = _loan_day(value)
            if day is not None:
                (loan.disbursed if what == "disbursed" else loan.sanctioned).append(day)
    return [loan for loan in loans.values() if loan.amounts or loan.references]


def _loan_money(value: str) -> Decimal | None:
    digits = re.sub(r"[^0-9.]", "", value)
    try:
        return Decimal(digits).quantize(Decimal("0.01")) if digits and digits.count(".") <= 1 else None
    except ArithmeticError:
        return None


def _loan_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------- the run

def reconcile_dealer(connection: Connection, *, tenant_id: str, dealer_id: str) -> dict[str, Any]:
    """Re-decide every receipt of the dealership that a current statement covers."""
    statements = _active_statements(connection, tenant_id, dealer_id)
    connection.execute(
        text("UPDATE auditcore.p2_bank_receipt_matches SET is_current = false "
             "WHERE tenant_id = :t AND dealer_id = :d AND is_current"),
        {"t": tenant_id, "d": dealer_id},
    )
    if not statements:
        return {"statements": 0}
    windows = [br.Window(statement_id=str(s["statement_id"]),
                         outlet_id=str(s["outlet_id"]) if s["outlet_id"] else None,
                         period_from=_check_dates(s)[0], period_to=_check_dates(s)[1]) for s in statements]
    first = min(w.period_from for w in windows)
    last = max(w.period_to for w in windows)
    credits, duplicates = _credits(connection, tenant_id, statements)
    receipts = _receipts(connection, tenant_id, dealer_id, first, last)
    people, journey_meta = _people(connection, tenant_id, dealer_id)
    named = {c.key: br.name_journeys(c, people) for c in credits}
    # The payment receipts on file and nothing else (product owner 2026-11-17): a line of the
    # NDC or a posting of the customer ledger with no receipt on file is not a receipt to verify.

    results, credit_results = br.reconcile(receipts, credits, windows)
    # A financier's disbursement with no receipt on file: tied to the Journey whose loan it is.
    br.match_loans(_loans(connection, tenant_id, dealer_id, people, journey_meta), results, credit_results, windows)
    for cr in credit_results:
        if not cr.receipt_keys and not cr.loan:
            candidates = named.get(cr.credit.key) or []
            cr.named_journey_id = candidates[0] if len(candidates) == 1 else None

    if results:
        connection.execute(
            text("""
                INSERT INTO auditcore.p2_bank_receipt_matches (tenant_id, journey_id, receipt_key, payment_id, dealer_id,
                  outlet_id, source, receipt_date, amount, mode, receipt_number, reference, outcome, match_basis, credits,
                  note, is_current, reconciled_at_utc)
                VALUES (:t, CAST(:j AS uuid), :k, CAST(:p AS uuid), CAST(:d AS uuid), CAST(:o AS uuid), :s, :rd, :a, :m,
                  :rn, :ref, :out, :b, CAST(:c AS jsonb), :n, true, now())
                ON CONFLICT (tenant_id, journey_id, receipt_key) DO UPDATE SET
                  payment_id = EXCLUDED.payment_id, outlet_id = EXCLUDED.outlet_id, source = EXCLUDED.source,
                  receipt_date = EXCLUDED.receipt_date, amount = EXCLUDED.amount, mode = EXCLUDED.mode,
                  receipt_number = EXCLUDED.receipt_number, reference = EXCLUDED.reference, outcome = EXCLUDED.outcome,
                  match_basis = EXCLUDED.match_basis, credits = EXCLUDED.credits, note = EXCLUDED.note,
                  is_current = true, reconciled_at_utc = now()
            """),
            [{"t": tenant_id, "j": res.receipt.journey_id, "k": res.receipt.key[:160],
              "p": res.receipt.key if res.receipt.source == "RECEIPT" else None, "d": dealer_id,
              "o": res.receipt.outlet_id, "s": res.receipt.source, "rd": res.receipt.receipt_date,
              "a": res.receipt.amount, "m": (res.receipt.mode or None) and str(res.receipt.mode)[:40],
              "rn": _receipt_number(res), "ref": (res.receipt.reference or None) and str(res.receipt.reference)[:200],
              "out": res.outcome, "b": res.basis, "c": json.dumps(res.credit_keys or res.candidates),
              "n": res.note and res.note[:400]} for res in results],
        )
    updates = []
    for cr in credit_results:
        sid, row_no = cr.credit.key.split(":")
        updates.append({"t": tenant_id, "s": sid, "n": int(row_no), "k": cr.credit.kind,
                        "o": "MATCHED" if cr.receipt_keys else "LOAN" if cr.loan else (
                            "OWN_TRANSFER" if cr.credit.kind == br.K_INTERNAL else "UNIDENTIFIED"),
                        "b": cr.basis, "rk": json.dumps(cr.receipt_keys), "j": cr.journey_id,
                        "nj": cr.named_journey_id, "dup": None})
    for key, original in duplicates.items():
        sid, row_no = key.split(":")
        updates.append({"t": tenant_id, "s": sid, "n": int(row_no), "k": None, "o": "DUPLICATE", "b": None,
                        "rk": "[]", "j": None, "nj": None, "dup": original})
    if updates:
        connection.execute(
            text("""
                UPDATE auditcore.p2_bank_statement_txns
                SET credit_kind = :k, outcome = :o, match_basis = :b, receipt_keys = CAST(:rk AS jsonb),
                    journey_id = CAST(:j AS uuid), named_journey_id = CAST(:nj AS uuid), duplicate_of = :dup
                WHERE tenant_id = :t AND statement_id = CAST(:s AS uuid) AND row_no = :n
            """),
            updates,
        )
    for statement, window in zip(statements, windows, strict=True):
        counts = _statement_counts(results, credit_results, window)
        connection.execute(
            text("UPDATE auditcore.p2_bank_statements SET counts = CAST(:c AS jsonb), reconciled_at_utc = now() "
                 "WHERE tenant_id = :t AND statement_id = :s"),
            {"t": tenant_id, "s": str(statement["statement_id"]), "c": json.dumps(counts)},
        )
    _record_verifications(connection, tenant_id, results, credits)
    _journey_status(connection, tenant_id, dealer_id, sorted({r.receipt.journey_id for r in results}))
    logger.info("bank_reconciliation_run", tenant_id=tenant_id, dealer_id=dealer_id, statements=len(statements),
                receipts=len(results), matched=sum(1 for r in results if r.outcome == br.MATCHED),
                credits=len(credits))
    return {"statements": len(statements), "receipts": len(results)}


_SYSTEM_ACTOR = "system:dealer-bank-statement"


def _record_verifications(connection: Connection, tenant_id: str, results: list[br.ReceiptResult],
                          credits: list[br.Credit]) -> None:
    """A receipt found in the bank is verified (its payment's own status), once; its open
    'payment not evidenced in a bank statement' finding is resolved."""
    from audit_core.uc03_payment_reconciliation import _RULE_PREFIX, _resolve_flag

    by_key = {c.key: c for c in credits}
    found = [r for r in results if r.outcome == br.MATCHED and r.receipt.source == "RECEIPT"]
    if not found:
        return
    already = set(connection.execute(
        text("SELECT CAST(payment_id AS text) FROM auditcore.payment_verification_events "
             "WHERE tenant_id = :t AND verification_result = 'VERIFIED' AND CAST(payment_id AS text) = ANY(:ps)"),
        {"t": tenant_id, "ps": [r.receipt.key for r in found]},
    ).scalars().all())
    rows = []
    for r in found:
        if r.receipt.key in already:
            continue
        credit = by_key.get(r.credit_keys[0]) if r.credit_keys else None
        note = (f"Found in the dealership's bank statement ({credit.bank or 'bank'}, credit of "
                f"{credit.txn_date:%d %b %Y}, matched on {BASIS_LABELS.get(r.basis or '', r.basis)})."
                if credit and credit.txn_date else "Found in the dealership's bank statement.")
        rows.append({"t": tenant_id, "j": r.receipt.journey_id, "p": r.receipt.key, "n": note[:500],
                     "a": _SYSTEM_ACTOR, "c": f"bank-statement:{r.credit_keys[0] if r.credit_keys else ''}"[:120]})
    if rows:
        connection.execute(
            text("""
                INSERT INTO auditcore.payment_verification_events (tenant_id, journey_id, payment_id,
                  verification_result, verification_notes, verified_by_actor_id, verified_by_role_code, correlation_id)
                VALUES (:t, CAST(:j AS uuid), CAST(:p AS uuid), 'VERIFIED', :n, :a, 'SYSTEM', :c)
            """),
            rows,
        )
    for r in found:
        try:
            with connection.begin_nested():
                _resolve_flag(connection, tenant_id=tenant_id, journey_id=UUID(r.receipt.journey_id),
                              rule_key=f"{_RULE_PREFIX}:{r.receipt.key}", correlation_id="dealer-bank-statement")
        except Exception:
            logger.warning("bank_reconciliation_flag_not_resolved", payment_id=r.receipt.key, exc_info=True)


_JOURNEY_RECEIPTS_SQL = """
    SELECT p.payment_id, p.journey_id, p.amount, p.receipt_number, p.receipt_date, p.payment_reference,
           COALESCE(p.payment_mode_code, p.payment_method_code) AS mode
    FROM auditcore.payments p
    JOIN auditcore.evidence e
      ON e.tenant_id = p.tenant_id AND e.journey_id = p.journey_id AND e.di_document_id = p.source_di_document_id
    WHERE p.tenant_id = :t AND CAST(p.journey_id AS text) = ANY(:js) AND p.amount > 0
      AND p.status_source = 'EVIDENCE' AND e.association_status = 'ACTIVE'
    ORDER BY p.journey_id, p.receipt_date NULLS LAST, p.created_at_utc, p.payment_id
"""


def journey_bank_status(receipts: list[dict[str, Any]], outcomes: dict[str, str]) -> dict[str, Any]:
    """One Journey's bank verification over every receipt it counts (any date): BANK_VERIFIED
    once each bank receipt is found; cash not found does not hold it back."""
    bank = verified = cash = Decimal(0)
    for r in receipts:
        amount = _dec(r["amount"])
        outcome = outcomes.get(str(r["payment_id"]))
        if br.mode_class(r["mode"]) == "CASH" and outcome != br.MATCHED:
            cash += amount
            continue
        bank += amount
        if outcome == br.MATCHED:
            verified += amount
    left = bank - verified
    status = ("CASH_ONLY" if bank == 0 else "BANK_VERIFIED" if left <= 0 else
              "PARTIAL" if verified > 0 else "NOT_VERIFIED")
    return {"status": status, "receipts": len(receipts), "bank": bank, "verified": verified, "left": left,
            "cash": cash}


def _journey_status(connection: Connection, tenant_id: str, dealer_id: str, journey_ids: list[str]) -> None:
    if not journey_ids:
        return
    from audit_core.uc03_p2_journey360 import _duplicate_receipts

    rows = connection.execute(text(_JOURNEY_RECEIPTS_SQL), {"t": tenant_id, "js": journey_ids}).mappings().all()
    outcomes = {r["receipt_key"]: r["outcome"] for r in connection.execute(
        text("SELECT receipt_key, outcome FROM auditcore.p2_bank_receipt_matches "
             "WHERE tenant_id = :t AND is_current AND CAST(journey_id AS text) = ANY(:js)"),
        {"t": tenant_id, "js": journey_ids},
    ).mappings()}
    by_journey: dict[str, list[Any]] = {j: [] for j in journey_ids}
    for r in rows:
        by_journey.setdefault(str(r["journey_id"]), []).append(r)
    updates = []
    for jid, journey_rows in by_journey.items():
        duplicates = _duplicate_receipts(journey_rows)
        status = journey_bank_status([r for r in journey_rows if r["payment_id"] not in duplicates], outcomes)
        updates.append({"t": tenant_id, "j": jid, "d": dealer_id, "s": status["status"], "n": status["receipts"],
                        "b": status["bank"], "v": status["verified"], "l": status["left"], "c": status["cash"],
                        "bv": status["status"] == "BANK_VERIFIED"})
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_journey_bank_status (tenant_id, journey_id, dealer_id, status, receipts,
              bank_receipts, verified, left_to_verify, cash, bank_verified_at, updated_at_utc)
            VALUES (:t, CAST(:j AS uuid), CAST(:d AS uuid), :s, :n, :b, :v, :l, :c,
                    CASE WHEN :bv THEN now() END, now())
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
              status = EXCLUDED.status, receipts = EXCLUDED.receipts, bank_receipts = EXCLUDED.bank_receipts,
              verified = EXCLUDED.verified, left_to_verify = EXCLUDED.left_to_verify, cash = EXCLUDED.cash,
              bank_verified_at = CASE WHEN EXCLUDED.status = 'BANK_VERIFIED'
                                      THEN COALESCE(auditcore.p2_journey_bank_status.bank_verified_at, now()) END,
              updated_at_utc = now()
        """),
        updates,
    )


def _receipt_number(result: br.ReceiptResult) -> str | None:
    number = result.receipt.number
    return str(number)[:120] if number else None


def journey_rollup(results: list[br.ReceiptResult]) -> dict[str, dict[str, Any]]:
    """Per Journey: its receipts in range, what was verified and what is left."""
    out: dict[str, dict[str, Any]] = {}
    for res in results:
        j = out.setdefault(res.receipt.journey_id, {
            "receipts": 0, "bankReceipts": Decimal(0), "verified": Decimal(0), "notFound": Decimal(0),
            "ambiguous": Decimal(0), "outsideDates": Decimal(0), "cash": Decimal(0), "card": Decimal(0),
            "notFoundCount": 0})
        j["receipts"] += 1
        amount = res.receipt.amount
        if res.outcome in (br.CASH,):
            j["cash"] += amount
            continue
        if res.outcome == br.CARD:
            j["card"] += amount
            continue
        if res.outcome == br.MATCHED and br.mode_class(res.receipt.mode) == "CASH":
            j["cash"] += amount  # a cash receipt found as a deposit: verified, shown with the cash
            j["verified"] += amount
            j["bankReceipts"] += amount
            continue
        j["bankReceipts"] += amount
        if res.outcome == br.MATCHED:
            j["verified"] += amount
        elif res.outcome == br.AMBIGUOUS:
            j["ambiguous"] += amount
        elif res.outcome == br.OUTSIDE_DATES:
            j["outsideDates"] += amount
        else:
            j["notFound"] += amount
            j["notFoundCount"] += 1
    for j in out.values():
        j["left"] = j["bankReceipts"] - j["verified"]
        j["status"] = (JOURNEY_CASH_ONLY if j["bankReceipts"] == 0 else
                       JOURNEY_VERIFIED if j["left"] <= 0 else
                       JOURNEY_PARTIAL if j["verified"] > 0 else JOURNEY_NOT_VERIFIED)
    return out


def _statement_counts(results: list[br.ReceiptResult], credit_results: list[br.CreditResult],
                      window: br.Window) -> dict[str, Any]:
    mine = [r for r in results if window.covers(r.receipt.receipt_date, r.receipt.outlet_id)]
    journeys = journey_rollup(mine)
    credits = [c for c in credit_results if c.credit.statement_id == window.statement_id]
    unidentified = [c for c in credits if not c.receipt_keys and not c.loan and c.credit.kind != br.K_INTERNAL]
    loans = [c for c in credits if c.loan]

    def total(items: list[br.ReceiptResult]) -> str:
        return _money(sum((r.receipt.amount for r in items), Decimal(0)))

    by = {o: [r for r in mine if r.outcome == o] for o in OUTCOME_LABELS}
    return {
        "receipts": len(mine), "receiptsAmount": total(mine),
        "matched": len(by[br.MATCHED]), "matchedAmount": total(by[br.MATCHED]),
        "notFound": len(by[br.NOT_FOUND]), "notFoundAmount": total(by[br.NOT_FOUND]),
        "ambiguous": len(by[br.AMBIGUOUS]), "ambiguousAmount": total(by[br.AMBIGUOUS]),
        "outsideDates": len(by[br.OUTSIDE_DATES]), "outsideDatesAmount": total(by[br.OUTSIDE_DATES]),
        "cash": len(by[br.CASH]), "cashAmount": total(by[br.CASH]),
        "card": len(by[br.CARD]), "cardAmount": total(by[br.CARD]),
        "journeys": len(journeys),
        "journeysVerified": sum(1 for j in journeys.values() if j["status"] == JOURNEY_VERIFIED),
        "journeysPartial": sum(1 for j in journeys.values() if j["status"] == JOURNEY_PARTIAL),
        "journeysNotVerified": sum(1 for j in journeys.values() if j["status"] == JOURNEY_NOT_VERIFIED),
        "journeysCashOnly": sum(1 for j in journeys.values() if j["status"] == JOURNEY_CASH_ONLY),
        "verifiedAmount": _money(sum((j["verified"] for j in journeys.values()), Decimal(0))),
        "leftAmount": _money(sum((j["left"] for j in journeys.values()), Decimal(0))),
        "credits": len(credits), "creditsMatched": sum(1 for c in credits if c.receipt_keys),
        "creditsLoan": len(loans), "creditsLoanAmount": _money(sum((c.credit.amount for c in loans), Decimal(0))),
        "creditsUnidentified": len(unidentified),
        "creditsUnidentifiedAmount": _money(sum((c.credit.amount for c in unidentified), Decimal(0))),
        "creditsNamingCustomer": sum(1 for c in unidentified if c.named_journey_id),
        "creditsNamingCustomerAmount": _money(sum((c.credit.amount for c in unidentified if c.named_journey_id),
                                                  Decimal(0))),
    }


# --------------------------------------------------------------------------- endpoints

def _outlets(connection: Connection, tenant_id: str, dealer_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    rows = connection.execute(
        text("SELECT dealer_id, outlet_id, outlet_name FROM auditcore.dealer_outlets "
             "WHERE tenant_id = :t AND CAST(dealer_id AS text) = ANY(:ds) ORDER BY lower(outlet_name)"),
        {"t": tenant_id, "ds": dealer_ids},
    ).mappings().all()
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(str(r["dealer_id"]), []).append({"outletId": str(r["outlet_id"]), "outletName": r["outlet_name"]})
    return out


@router.get("/bank-statements/setup")
def bank_statement_setup(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The dealerships (with their outlets) the caller covers, and whether they may upload."""
    held = _access(connection, tenant_id, principal, client)
    admin = "ADMIN" in held
    dealers = _dealers(connection, tenant_id, principal.subject, admin)
    outlets = _outlets(connection, tenant_id, [d["dealerId"] for d in dealers])
    return {"dealers": [{**d, "outlets": outlets.get(d["dealerId"], [])} for d in dealers],
            "canUpload": True, "today": _today(connection, tenant_id).isoformat(),
            "windowDays": br.WINDOW_DAYS, "formats": [".pdf", ".xlsx", ".xls", ".csv"]}


def _statement_json(r: Any) -> dict[str, Any]:
    return {
        "statementId": str(r["statement_id"]), "dealerId": str(r["dealer_id"]), "dealerName": r.get("dealer_name"),
        "outletId": str(r["outlet_id"]) if r["outlet_id"] else None, "outletName": r.get("outlet_name"),
        "bankName": r["bank_name"], "accountNumber": r["account_number"], "accountHolder": r["account_holder"],
        "ifsc": r["ifsc"], "periodFrom": r["period_from"].isoformat(), "periodTo": r["period_to"].isoformat(),
        "checkFrom": _check_dates(r)[0].isoformat(), "checkTo": _check_dates(r)[1].isoformat(),
        "openingBalance": _money(r["opening_balance"]) if r["opening_balance"] is not None else None,
        "closingBalance": _money(r["closing_balance"]) if r["closing_balance"] is not None else None,
        "creditCount": r["credit_count"], "creditTotal": _money(r["credit_total"]),
        "debitCount": r["debit_count"], "debitTotal": _money(r["debit_total"]),
        "printedCreditTotal": _money(r["printed_credit_total"]) if r["printed_credit_total"] is not None else None,
        "printedDebitTotal": _money(r["printed_debit_total"]) if r["printed_debit_total"] is not None else None,
        "readingCheck": r["reading_check"], "fileName": r["file_name"], "format": r["file_format"],
        "status": r["status"], "counts": r["counts"] or {},
        "uploadedBy": r["uploaded_by_name"] or r["uploaded_by"], "uploadedAtUtc": r["uploaded_at_utc"].isoformat(),
        "reconciledAtUtc": r["reconciled_at_utc"].isoformat() if r["reconciled_at_utc"] else None,
    }


_STATEMENT_SQL = """
    SELECT s.*, d.dealer_name, o.outlet_name, tx.txn_from, tx.txn_to FROM auditcore.p2_bank_statements s
    JOIN auditcore.dealers d ON d.tenant_id = s.tenant_id AND d.dealer_id = s.dealer_id
    LEFT JOIN auditcore.dealer_outlets o ON o.tenant_id = s.tenant_id AND o.outlet_id = s.outlet_id
""" + _TXN_DATES + """
    WHERE s.tenant_id = :t
"""


def _read_statement(connection: Connection, tenant_id: str, statement_id: UUID) -> dict[str, Any]:
    row = connection.execute(text(_STATEMENT_SQL + " AND s.statement_id = :id"),
                             {"t": tenant_id, "id": str(statement_id)}).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No such bank statement.")
    return _statement_json(row)


@router.post("/bank-statements", status_code=201)
def upload_bank_statement(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    file: Annotated[UploadFile, File()],
    dealerId: Annotated[UUID, Form()],
    outletId: Annotated[UUID | None, Form()] = None,
) -> dict[str, Any]:
    """One account statement of a dealership (or of one of its outlets): PDF, Excel or CSV,
    any bank. Read, stored, and the dealership's receipts reconciled at once."""
    held = _access(connection, tenant_id, principal, client)
    admin = "ADMIN" in held
    if str(dealerId) not in {d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject, admin)}:
        raise HTTPException(status_code=403, detail="That dealership is not one you cover.")
    if outletId is not None and str(outletId) not in {
            o["outletId"] for o in _outlets(connection, tenant_id, [str(dealerId)]).get(str(dealerId), [])}:
        raise HTTPException(status_code=422, detail="That outlet is not one of the dealership's.")
    data = file.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=422, detail="The file is larger than 20 MB.")
    name = (file.filename or "statement")[:300]
    try:
        statement = read_statement(data, name)
    except ScannedPdf as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StatementUnreadable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not statement.transactions or statement.period_from is None or statement.period_to is None:
        raise HTTPException(status_code=422, detail="No dated transactions were found in this statement.")
    if statement.period_to < statement.period_from:
        statement.period_from, statement.period_to = statement.period_to, statement.period_from
    today = _today(connection, tenant_id)
    if statement.period_from > today + timedelta(days=1):
        raise HTTPException(status_code=422, detail="The statement's period is in the future.")
    statement_id = uuid4()
    # The same account and period again replaces the earlier upload (kept, not deleted).
    connection.execute(
        text("""
            UPDATE auditcore.p2_bank_statements SET status = 'SUPERSEDED'
            WHERE tenant_id = :t AND dealer_id = :d AND status = 'ACTIVE'
              AND COALESCE(account_number, '') = COALESCE(:a, '') AND period_from = :f AND period_to = :l
              AND COALESCE(CAST(outlet_id AS text), '') = COALESCE(:o, '')
        """),
        {"t": tenant_id, "d": str(dealerId), "a": statement.account_number, "f": statement.period_from,
         "l": statement.period_to, "o": str(outletId) if outletId else None},
    )
    credits = [t for t in statement.transactions if t.direction == CREDIT]
    debits = [t for t in statement.transactions if t.direction != CREDIT]
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_bank_statements (tenant_id, statement_id, dealer_id, outlet_id, bank_name,
              account_number, account_holder, ifsc, period_from, period_to, opening_balance, closing_balance,
              credit_count, credit_total, debit_count, debit_total, printed_credit_total, printed_debit_total,
              reading_check, file_name, file_format, uploaded_by, uploaded_by_name)
            VALUES (:t, :id, :d, :o, :bank, :acct, :holder, :ifsc, :f, :l, :ob, :cb, :cc, :ct, :dc, :dt, :pct, :pdt,
              :chk, :fn, :fmt, :by, :byn)
        """),
        {"t": tenant_id, "id": str(statement_id), "d": str(dealerId), "o": str(outletId) if outletId else None,
         "bank": statement.bank_name, "acct": statement.account_number,
         "holder": statement.account_holder and statement.account_holder[:300], "ifsc": statement.ifsc,
         "f": statement.period_from, "l": statement.period_to, "ob": statement.opening_balance,
         "cb": statement.closing_balance, "cc": len(credits), "ct": statement.credit_total, "dc": len(debits),
         "dt": statement.debit_total, "pct": statement.printed_credit_total, "pdt": statement.printed_debit_total,
         "chk": statement.check(), "fn": name, "fmt": statement.file_format, "by": principal.subject,
         "byn": _name_of(connection, principal.subject)},
    )
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_bank_statement_txns (tenant_id, statement_id, row_no, txn_date, value_date,
              narration, reference, cheque_number, bank_txn_id, amount, direction, balance)
            VALUES (:t, :id, :n, :d, :vd, :nar, :ref, :chq, :tid, :a, :dir, :bal)
        """),
        [{"t": tenant_id, "id": str(statement_id), "n": n, "d": t.txn_date, "vd": t.value_date,
          "nar": t.narration[:1000], "ref": t.reference, "chq": t.cheque_number and t.cheque_number[:40],
          "tid": t.bank_txn_id, "a": t.amount, "dir": t.direction, "bal": t.balance}
         for n, t in enumerate(statement.transactions, start=1)],
    )
    reconcile_dealer(connection, tenant_id=tenant_id, dealer_id=str(dealerId))
    logger.info("bank_statement_uploaded", tenant_id=tenant_id, statement_id=str(statement_id),
                bank=statement.bank_name, transactions=len(statement.transactions), check=statement.check())
    return _read_statement(connection, tenant_id, statement_id)


@router.get("/bank-statements")
def list_bank_statements(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    month: str | None = None,
    dealer_id: UUID | None = None,
) -> dict[str, Any]:
    """The statements whose period touches the month (current by default), newest first;
    replaced ones kept and marked."""
    held = _access(connection, tenant_id, principal, client)
    first = _month(month, _today(connection, tenant_id))
    dealer_ids = [d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)]
    rows = connection.execute(
        text(_STATEMENT_SQL + """
            AND s.period_to >= :f AND s.period_from < :l AND CAST(s.dealer_id AS text) = ANY(:ds)
            AND (CAST(:d AS uuid) IS NULL OR s.dealer_id = CAST(:d AS uuid))
            ORDER BY s.period_to DESC, lower(d.dealer_name), s.uploaded_at_utc DESC
        """),
        {"t": tenant_id, "f": first, "l": _next_month(first), "ds": dealer_ids,
         "d": str(dealer_id) if dealer_id else None},
    ).mappings().all()
    return {"month": first.strftime("%Y-%m"), "items": [_statement_json(r) for r in rows]}


def _window_of(statement: dict[str, Any]) -> tuple[date, date, str | None]:
    return (date.fromisoformat(statement["checkFrom"]), date.fromisoformat(statement["checkTo"]),
            statement["outletId"])


_MATCH_SQL = """
    SELECT m.*, j.journey_reference, o.outlet_name, c.legal_name, c.display_name
    FROM auditcore.p2_bank_receipt_matches m
    JOIN auditcore.journeys j ON j.tenant_id = m.tenant_id AND j.journey_id = m.journey_id
    LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
    LEFT JOIN auditcore.dealer_outlets o ON o.tenant_id = m.tenant_id AND o.outlet_id = m.outlet_id
    WHERE m.tenant_id = :t AND m.dealer_id = :d AND m.is_current AND m.receipt_date BETWEEN :f AND :l
      AND (CAST(:o AS uuid) IS NULL OR m.outlet_id = CAST(:o AS uuid))
    ORDER BY m.receipt_date, j.journey_reference, m.amount DESC
"""


def statement_detail(connection: Connection, tenant_id: str, statement: dict[str, Any]) -> dict[str, Any]:
    """The statement's receipts (dated within its transactions' dates), Journeys and credits."""
    check_from, check_to, outlet_id = _window_of(statement)
    matches = connection.execute(
        text(_MATCH_SQL), {"t": tenant_id, "d": statement["dealerId"], "f": check_from, "l": check_to, "o": outlet_id},
    ).mappings().all()
    txns = connection.execute(
        text("""
            SELECT x.*, j.journey_reference AS matched_reference, nj.journey_reference AS named_reference,
                   (SELECT auditcore.shown_name(mc.legal_name, mc.display_name) FROM auditcore.customers mc
                    WHERE mc.tenant_id = j.tenant_id AND mc.customer_id = j.customer_id) AS matched_customer,
                   auditcore.shown_name(nc.legal_name, nc.display_name) AS named_customer
            FROM auditcore.p2_bank_statement_txns x
            LEFT JOIN auditcore.journeys j ON j.tenant_id = x.tenant_id AND j.journey_id = x.journey_id
            LEFT JOIN auditcore.journeys nj ON nj.tenant_id = x.tenant_id AND nj.journey_id = x.named_journey_id
            LEFT JOIN auditcore.customers nc ON nc.tenant_id = nj.tenant_id AND nc.customer_id = nj.customer_id
            WHERE x.tenant_id = :t AND x.statement_id = :s ORDER BY x.row_no
        """),
        {"t": tenant_id, "s": statement["statementId"]},
    ).mappings().all()
    credit_rows = {f"{t['statement_id']}:{t['row_no']}": t for t in txns}
    other_credits = _credit_lookup(connection, tenant_id, matches, set(credit_rows))
    receipts = []
    results = []
    for m in matches:
        credit_keys = list(m["credits"] or [])
        tied = [credit_rows.get(k) or other_credits.get(k) for k in credit_keys]
        receipts.append({
            "journeyId": str(m["journey_id"]), "journeyReference": m["journey_reference"],
            "customer": m["legal_name"] or m["display_name"], "outletName": m["outlet_name"],
            "source": m["source"], "receiptKey": m["receipt_key"], "receiptNumber": m["receipt_number"],
            "receiptDate": m["receipt_date"].isoformat() if m["receipt_date"] else None,
            "amount": _money(m["amount"]), "mode": m["mode"], "reference": m["reference"],
            "outcome": m["outcome"], "outcomeLabel": OUTCOME_LABELS.get(m["outcome"], m["outcome"]),
            "basis": m["match_basis"], "basisLabel": BASIS_LABELS.get(m["match_basis"] or "", None),
            "note": m["note"],
            "credits": [{"date": t["txn_date"].isoformat() if t and t["txn_date"] else None,
                         "amount": _money(t["amount"]) if t else None, "narration": t["narration"] if t else None,
                         "onThisStatement": k in credit_rows} for k, t in zip(credit_keys, tied, strict=True) if t]
            if m["outcome"] == br.MATCHED else [],
            "candidates": len(credit_keys) if m["outcome"] == br.AMBIGUOUS else 0,
        })
        results.append(br.ReceiptResult(
            receipt=br.Receipt(key=m["receipt_key"], journey_id=str(m["journey_id"]), outlet_id=None,
                               amount=_dec(m["amount"]), receipt_date=m["receipt_date"], mode=m["mode"],
                               reference=None, bank=None),
            outcome=m["outcome"], basis=m["match_basis"]))
    rollup = journey_rollup(results)
    meta = {r["journeyId"]: r for r in receipts}
    overall = {str(r["journey_id"]): r for r in connection.execute(
        text("SELECT journey_id, status, verified, left_to_verify, bank_verified_at FROM auditcore.p2_journey_bank_status "
             "WHERE tenant_id = :t AND CAST(journey_id AS text) = ANY(:js)"),
        {"t": tenant_id, "js": list(rollup)},
    ).mappings()} if rollup else {}
    journeys = sorted(({
        "journeyId": jid, "journeyReference": meta[jid]["journeyReference"], "customer": meta[jid]["customer"],
        "outletName": meta[jid]["outletName"], "receipts": j["receipts"],
        "bankReceipts": _money(j["bankReceipts"]), "verified": _money(j["verified"]), "left": _money(j["left"]),
        "notFound": _money(j["notFound"]), "ambiguous": _money(j["ambiguous"]),
        "outsideDates": _money(j["outsideDates"]), "cash": _money(j["cash"]),
        "card": _money(j["card"]), "status": j["status"], "statusLabel": JOURNEY_LABELS[j["status"]],
        "bankVerified": (overall.get(jid) or {}).get("status") == "BANK_VERIFIED",
        "journeyBankStatus": (overall.get(jid) or {}).get("status"),
        "journeyLeft": _money((overall.get(jid) or {}).get("left_to_verify")) if overall.get(jid) else None,
    } for jid, j in rollup.items()), key=lambda x: ({JOURNEY_NOT_VERIFIED: 0, JOURNEY_PARTIAL: 1,
                                                    JOURNEY_VERIFIED: 2, JOURNEY_CASH_ONLY: 3}[x["status"]],
                                                   -Decimal(x["left"]), x["journeyReference"] or ""))
    credits = [{
        "rowNo": t["row_no"], "date": t["txn_date"].isoformat() if t["txn_date"] else None,
        "narration": t["narration"], "reference": t["reference"] or t["cheque_number"], "amount": _money(t["amount"]),
        "direction": t["direction"], "balance": _money(t["balance"]) if t["balance"] is not None else None,
        "kind": t["credit_kind"], "kindLabel": KIND_LABELS.get(t["credit_kind"] or "", None),
        "outcome": t["outcome"], "basis": t["match_basis"], "basisLabel": BASIS_LABELS.get(t["match_basis"] or "", None),
        "journeyId": str(t["journey_id"]) if t["journey_id"] else None, "journeyReference": t["matched_reference"],
        "matchedCustomer": t["matched_customer"],
        "namedJourneyId": str(t["named_journey_id"]) if t["named_journey_id"] else None,
        "namedJourneyReference": t["named_reference"], "namedCustomer": t["named_customer"],
        "receipts": len(t["receipt_keys"] or []),
    } for t in txns]
    return {**statement, "receipts": receipts, "journeys": journeys, "transactions": credits}


def _credit_lookup(connection: Connection, tenant_id: str, matches: list[Any], known: set[str]) -> dict[str, Any]:
    """Credits on the dealership's other statements that receipts here were tied to."""
    wanted = {k for m in matches if m["outcome"] == br.MATCHED for k in (m["credits"] or []) if k not in known}
    if not wanted:
        return {}
    pairs = [k.split(":") for k in wanted]
    rows = connection.execute(
        text("""
            SELECT x.* FROM auditcore.p2_bank_statement_txns x
            JOIN unnest(CAST(:s AS text[]), CAST(:n AS int[])) AS w(sid, rn)
              ON CAST(x.statement_id AS text) = w.sid AND x.row_no = w.rn
            WHERE x.tenant_id = :t
        """),
        {"t": tenant_id, "s": [p[0] for p in pairs], "n": [int(p[1]) for p in pairs]},
    ).mappings().all()
    return {f"{r['statement_id']}:{r['row_no']}": r for r in rows}


_MATCHED_SQL = """
    SELECT m.*, j.journey_reference, o.outlet_name, d.dealer_name,
           auditcore.shown_name(c.legal_name, c.display_name) AS customer
    FROM auditcore.p2_bank_receipt_matches m
    JOIN auditcore.journeys j ON j.tenant_id = m.tenant_id AND j.journey_id = m.journey_id
    JOIN auditcore.dealers d ON d.tenant_id = m.tenant_id AND d.dealer_id = m.dealer_id
    LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
    LEFT JOIN auditcore.dealer_outlets o ON o.tenant_id = m.tenant_id AND o.outlet_id = m.outlet_id
    WHERE m.tenant_id = :t AND m.is_current AND m.outcome = 'MATCHED' AND CAST(m.dealer_id AS text) = ANY(:ds)
    ORDER BY lower(d.dealer_name), m.receipt_date, j.journey_reference, m.amount DESC
"""


def matched_transactions(connection: Connection, tenant_id: str, statements: list[dict[str, Any]]
                         ) -> list[dict[str, Any]]:
    """Every receipt tied to a bank credit on one of these statements, across them all, each
    with its Journey and the credit(s) it was tied to (product owner 2026-11-17: "list which
    transactions got matched and give journey 360 link for those ... add this view")."""
    by_id = {s["statementId"]: s for s in statements}
    if not by_id:
        return []
    matches = connection.execute(
        text(_MATCHED_SQL), {"t": tenant_id, "ds": sorted({s["dealerId"] for s in statements})},
    ).mappings().all()
    credits = _credit_lookup(connection, tenant_id, matches, set())
    out = []
    for m in matches:
        tied = []
        for key in m["credits"] or []:
            t = credits.get(key)
            statement = by_id.get(str(t["statement_id"])) if t else None
            if statement is None:
                continue  # a credit on a statement outside this view (another month, or replaced)
            tied.append({
                "statementId": statement["statementId"], "bankName": statement["bankName"],
                "accountNumber": statement["accountNumber"],
                "date": t["txn_date"].isoformat() if t["txn_date"] else None, "amount": _money(t["amount"]),
                "narration": t["narration"], "reference": t["reference"] or t["cheque_number"],
                "kind": t["credit_kind"], "kindLabel": KIND_LABELS.get(t["credit_kind"] or "", None),
            })
        if not tied:
            continue
        out.append({
            "dealerId": str(m["dealer_id"]), "dealerName": m["dealer_name"], "outletName": m["outlet_name"],
            "journeyId": str(m["journey_id"]), "journeyReference": m["journey_reference"], "customer": m["customer"],
            "source": m["source"], "receiptKey": m["receipt_key"], "receiptNumber": m["receipt_number"],
            "receiptDate": m["receipt_date"].isoformat() if m["receipt_date"] else None,
            "amount": _money(m["amount"]), "mode": m["mode"], "reference": m["reference"],
            "basis": m["match_basis"], "basisLabel": BASIS_LABELS.get(m["match_basis"] or "", None),
            "credits": tied,
        })
    return out


def _month_statements(connection: Connection, tenant_id: str, dealer_ids: list[str], first: date,
                      dealer_id: UUID | None) -> list[dict[str, Any]]:
    """The current statements whose period touches the month."""
    rows = connection.execute(
        text(_STATEMENT_SQL + """
            AND s.status = 'ACTIVE' AND s.period_to >= :f AND s.period_from < :l
            AND CAST(s.dealer_id AS text) = ANY(:ds)
            AND (CAST(:d AS uuid) IS NULL OR s.dealer_id = CAST(:d AS uuid))
            ORDER BY lower(d.dealer_name), s.period_from
        """),
        {"t": tenant_id, "f": first, "l": _next_month(first), "ds": dealer_ids,
         "d": str(dealer_id) if dealer_id else None},
    ).mappings().all()
    return [_statement_json(r) for r in rows]


@router.get("/bank-statements/matched")
def list_matched_transactions(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    month: str | None = None,
    dealer_id: UUID | None = None,
) -> dict[str, Any]:
    """The month's matched transactions in one list: every receipt found in the bank across
    the month's current statements, with its Journey and the bank credit it was tied to."""
    held = _access(connection, tenant_id, principal, client)
    first = _month(month, _today(connection, tenant_id))
    dealer_ids = [d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)]
    statements = _month_statements(connection, tenant_id, dealer_ids, first, dealer_id)
    items = matched_transactions(connection, tenant_id, statements)
    return {"month": first.strftime("%Y-%m"), "statements": len(statements), "count": len(items),
            "amount": _money(sum((_dec(i["amount"]) for i in items), Decimal(0))), "items": items}


@router.get("/bank-statements/{statement_id}")
def read_bank_statement(
    tenant_id: str,
    statement_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    outcome: Annotated[str | None, Query(pattern="^(MATCHED|AMBIGUOUS|NOT_FOUND|OUTSIDE_DATES|CASH|CARD)$")] = None,
) -> dict[str, Any]:
    """One statement: its reconciliation -- receipts, Journeys, and every transaction read."""
    held = _access(connection, tenant_id, principal, client)
    statement = _read_statement(connection, tenant_id, statement_id)
    if statement["dealerId"] not in {d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject,
                                                                       "ADMIN" in held)}:
        raise HTTPException(status_code=403, detail="That dealership is not one you cover.")
    detail = statement_detail(connection, tenant_id, statement)
    if outcome:
        detail["receipts"] = [r for r in detail["receipts"] if r["outcome"] == outcome]
    return detail


@router.post("/bank-statements/{statement_id}/rematch")
def rematch_bank_statement(
    tenant_id: str,
    statement_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Reconcile the dealership again (receipts uploaded since are checked)."""
    held = _access(connection, tenant_id, principal, client)
    statement = _read_statement(connection, tenant_id, statement_id)
    if statement["dealerId"] not in {d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject,
                                                                       "ADMIN" in held)}:
        raise HTTPException(status_code=403, detail="That dealership is not one you cover.")
    reconcile_dealer(connection, tenant_id=tenant_id, dealer_id=statement["dealerId"])
    return _read_statement(connection, tenant_id, statement_id)


# --------------------------------------------------------------------------- Excel

def build_report(connection: Connection, *, tenant_id: str, statements: list[dict[str, Any]]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    workbook = Workbook()
    bold, head = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="161616")

    def sheet_with(title: str, names: list[str]) -> Any:
        sheet = workbook.create_sheet(title[:31])
        sheet.append(names)
        for cell in sheet[1]:
            cell.font, cell.fill = bold, head
        sheet.freeze_panes = "A2"
        return sheet

    def num(value: Any) -> Any:
        return float(value) if value not in (None, "") else None

    summary = workbook.active
    summary.title = "Summary"
    summary.append(["Dealership", "Outlet", "Bank", "Account", "Period", "Checked from", "Checked to", "Reading",
                    "Receipts checked", "Receipts amount", "Found in bank", "Found amount", "Not found",
                    "Not found amount", "Several possible", "Outside statement dates", "Cash", "Card", "Journeys", "Fully verified",
                    "Partly verified", "Not verified", "Cash only", "Verified amount", "Left to verify",
                    "Credits", "Credits tied", "Credits not identified", "Not identified amount",
                    "Credits naming a customer (no receipt)", "Amount naming a customer", "File", "Uploaded by"])
    for cell in summary[1]:
        cell.font, cell.fill = bold, head
    summary.freeze_panes = "A2"
    journeys_sheet = sheet_with("Journeys", ["Statement", "Journey", "Customer", "Outlet", "Status", "Receipts",
                                             "Bank receipts", "Verified", "Left", "Not found", "Several possible",
                                             "Outside statement dates", "Cash", "Card", "Journey bank status"])
    receipts_sheet = sheet_with("Receipts", ["Statement", "Journey", "Customer", "Outlet", "Receipt date",
                                             "Receipt no.", "Amount", "Mode", "Reference", "Source", "Result",
                                             "Matched on", "Bank credit date", "Bank credit narration", "Note"])
    matched_sheet = sheet_with("Found in bank", ["Dealership", "Outlet", "Journey", "Customer", "Receipt no.",
                                                 "Receipt date", "Mode", "Reference", "Amount", "Matched on", "Bank",
                                                 "Account", "Bank credit date", "Bank credit amount",
                                                 "Bank credit narration"])
    for r in matched_transactions(connection, tenant_id, statements):
        for n, credit in enumerate(r["credits"]):
            matched_sheet.append([r["dealerName"], r["outletName"], r["journeyReference"], r["customer"],
                                  r["receiptNumber"], r["receiptDate"], r["mode"], r["reference"],
                                  num(r["amount"]) if n == 0 else None, r["basisLabel"], credit["bankName"],
                                  credit["accountNumber"], credit["date"], num(credit["amount"]), credit["narration"]])
    missing_sheet = sheet_with("Not found in bank", ["Statement", "Journey", "Customer", "Outlet", "Receipt date",
                                                     "Receipt no.", "Amount", "Mode", "Reference", "Result", "Note"])
    credits_sheet = sheet_with("Bank credits", ["Statement", "Date", "Narration", "Reference", "Amount", "Kind",
                                                "Result", "Matched on", "Journey", "Names customer (no receipt)",
                                                "Customer"])
    loans_sheet = sheet_with("Loans found in bank", ["Statement", "Journey", "Customer", "Bank credit date",
                                                     "Amount", "Bank credit narration", "Matched on",
                                                     "Loan receipt on file"])
    unidentified_sheet = sheet_with("Credits not identified", ["Statement", "Date", "Narration", "Reference",
                                                               "Amount", "Kind", "Names Journey", "Customer"])
    for statement in statements:
        detail = statement_detail(connection, tenant_id, statement)
        c = detail["counts"] or {}
        label = f"{detail['bankName'] or 'Bank'} {detail['accountNumber'] or ''} {detail['periodFrom']}".strip()
        summary.append([detail["dealerName"], detail["outletName"] or "All outlets", detail["bankName"],
                        detail["accountNumber"], f"{detail['periodFrom']} to {detail['periodTo']}", detail["checkFrom"],
                        detail["checkTo"], detail["readingCheck"], c.get("receipts"), num(c.get("receiptsAmount")),
                        c.get("matched"), num(c.get("matchedAmount")), c.get("notFound"), num(c.get("notFoundAmount")),
                        c.get("ambiguous"), c.get("outsideDates"), c.get("cash"), c.get("card"), c.get("journeys"), c.get("journeysVerified"),
                        c.get("journeysPartial"), c.get("journeysNotVerified"), c.get("journeysCashOnly"),
                        num(c.get("verifiedAmount")), num(c.get("leftAmount")), c.get("credits"),
                        c.get("creditsMatched"), c.get("creditsUnidentified"), num(c.get("creditsUnidentifiedAmount")),
                        c.get("creditsNamingCustomer"), num(c.get("creditsNamingCustomerAmount")),
                        detail["fileName"], detail["uploadedBy"]])
        for j in detail["journeys"]:
            journeys_sheet.append([label, j["journeyReference"], j["customer"], j["outletName"], j["statusLabel"],
                                   j["receipts"], num(j["bankReceipts"]), num(j["verified"]), num(j["left"]),
                                   num(j["notFound"]), num(j["ambiguous"]), num(j["outsideDates"]),
                                   num(j["cash"]), num(j["card"]),
                                   "Bank verified" if j.get("bankVerified") else
                                   {"PARTIAL": "Partly verified", "NOT_VERIFIED": "Not verified",
                                    "CASH_ONLY": "Cash only"}.get(j.get("journeyBankStatus") or "", None)])
        for r in detail["receipts"]:
            first = r["credits"][0] if r["credits"] else {}
            receipts_sheet.append([label, r["journeyReference"], r["customer"], r["outletName"], r["receiptDate"],
                                   r["receiptNumber"], num(r["amount"]), r["mode"], r["reference"],
                                   "Customer ledger / NDC" if r["source"] == "LEDGER" else "Receipt",
                                   r["outcomeLabel"], r["basisLabel"], first.get("date"),
                                   " | ".join(x["narration"] or "" for x in r["credits"]) or None, r["note"]])
            if r["outcome"] in (br.NOT_FOUND, br.AMBIGUOUS):
                missing_sheet.append([label, r["journeyReference"], r["customer"], r["outletName"], r["receiptDate"],
                                      r["receiptNumber"], num(r["amount"]), r["mode"], r["reference"],
                                      r["outcomeLabel"], r["note"]])
        for t in detail["transactions"]:
            if t["direction"] != "CR":
                continue
            result = {"MATCHED": "Tied to receipt", "LOAN": "Loan of the Journey (no receipt)",
                      "UNIDENTIFIED": "No receipt", "OWN_TRANSFER": "Own transfer",
                      "DUPLICATE": "Printed on another upload"}.get(t["outcome"] or "", t["outcome"])
            credits_sheet.append([label, t["date"], t["narration"], t["reference"], num(t["amount"]), t["kindLabel"],
                                  result, t["basisLabel"], t["journeyReference"], t["namedJourneyReference"],
                                  t["namedCustomer"]])
            if t["outcome"] == "LOAN" or (t["outcome"] == "MATCHED" and str(t["basis"] or "").startswith("LOAN")):
                loans_sheet.append([label, t["journeyReference"], t["matchedCustomer"], t["date"], num(t["amount"]),
                                    t["narration"], t["basisLabel"],
                                    "Yes" if t["outcome"] == "MATCHED" else "No receipt on file"])
            if t["outcome"] == "UNIDENTIFIED":
                unidentified_sheet.append([label, t["date"], t["narration"], t["reference"], num(t["amount"]),
                                           t["kindLabel"], t["namedJourneyReference"], t["namedCustomer"]])
    for sheet in workbook.worksheets:
        for column in sheet.columns:
            width = max((len(str(c.value)) for c in column if c.value is not None), default=8)
            sheet.column_dimensions[column[0].column_letter].width = min(max(width + 2, 10), 60)
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


@router.get("/bank-statements/report/file")
def export_bank_statement_report(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    month: str | None = None,
    statement_id: UUID | None = None,
) -> StreamingResponse:
    """Export to Excel: one statement's (or the month's current statements') reconciliation
    -- Summary, Journeys, Receipts, Not found in bank, Bank credits, Credits not identified."""
    held = _access(connection, tenant_id, principal, client)
    dealer_ids = {d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)}
    if statement_id is not None:
        statements = [_read_statement(connection, tenant_id, statement_id)]
        name = f"Bank-Reconciliation_{statements[0]['periodFrom']}_{statements[0]['accountNumber'] or 'account'}.xlsx"
    else:
        first = _month(month, _today(connection, tenant_id))
        rows = connection.execute(
            text(_STATEMENT_SQL + " AND s.status = 'ACTIVE' AND s.period_to >= :f AND s.period_from < :l "
                                  "ORDER BY lower(d.dealer_name), s.period_from"),
            {"t": tenant_id, "f": first, "l": _next_month(first)},
        ).mappings().all()
        statements = [_statement_json(r) for r in rows]
        name = f"Bank-Reconciliation_{first:%Y-%m}.xlsx"
    statements = [s for s in statements if s["dealerId"] in dealer_ids]
    data = build_report(connection, tenant_id=tenant_id, statements=statements)
    return StreamingResponse(io.BytesIO(data), media_type=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


# --------------------------------------------------------------------------- the Journey's own view

def journey_bank_matches(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, dict[str, Any]]:
    """receipt key -> the dealership statement's current verdict, for the Journey 360
    Payments tab and the Audit Summary's AS-08 (payments verified with a bank statement)."""
    try:
        with connection.begin_nested():
            rows = connection.execute(
                text("""
                    SELECT m.receipt_key, m.outcome, m.match_basis, m.credits, s.statement_id, s.bank_name,
                           s.account_number, x.txn_date, x.reference, x.narration
                    FROM auditcore.p2_bank_receipt_matches m
                    LEFT JOIN auditcore.p2_bank_statement_txns x
                      ON m.outcome = 'MATCHED' AND x.tenant_id = m.tenant_id
                     AND x.statement_id = CAST(NULLIF(split_part(m.credits ->> 0, ':', 1), '') AS uuid)
                     AND x.row_no = CAST(NULLIF(split_part(m.credits ->> 0, ':', 2), '') AS integer)
                    LEFT JOIN auditcore.p2_bank_statements s
                      ON s.tenant_id = m.tenant_id AND s.statement_id = x.statement_id
                    WHERE m.tenant_id = :t AND m.journey_id = :j AND m.is_current
                """),
                {"t": tenant_id, "j": str(journey_id)},
            ).mappings().all()
    except Exception:
        logger.warning("journey_bank_matches_unavailable", exc_info=True)
        return {}
    return {r["receipt_key"]: dict(r) for r in rows}


def journey_bank_summary(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any] | None:
    """The Journey's bank verification (BANK_VERIFIED / PARTIAL / NOT_VERIFIED / CASH_ONLY)."""
    try:
        with connection.begin_nested():
            row = connection.execute(
                text("SELECT * FROM auditcore.p2_journey_bank_status WHERE tenant_id = :t AND journey_id = :j"),
                {"t": tenant_id, "j": str(journey_id)},
            ).mappings().one_or_none()
    except Exception:  # noqa: BLE001 -- the table may not exist yet on an old database
        return None
    loans = _journey_loans(connection, tenant_id, journey_id)
    if row is None:
        # no bank receipt to verify, a loan credit of its own on a statement all the same
        return ({"status": None, "bankVerified": False, "bankReceipts": "0.00", "verified": "0.00", "left": "0.00",
                 "cash": "0.00", "bankVerifiedAtUtc": None, "loans": loans} if loans else None)
    return {"status": row["status"], "bankVerified": row["status"] == "BANK_VERIFIED", "loans": loans,
            "bankReceipts": _money(row["bank_receipts"]), "verified": _money(row["verified"]),
            "left": _money(row["left_to_verify"]), "cash": _money(row["cash"]),
            "bankVerifiedAtUtc": row["bank_verified_at"].isoformat() if row["bank_verified_at"] else None}


def _journey_loans(connection: Connection, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """The financier's credits tied to this Journey's loan on the current statements."""
    try:
        with connection.begin_nested():
            rows = connection.execute(
                text("""
                    SELECT x.txn_date, x.amount, x.narration, x.match_basis, x.outcome, s.bank_name, s.account_number
                    FROM auditcore.p2_bank_statement_txns x
                    JOIN auditcore.p2_bank_statements s
                      ON s.tenant_id = x.tenant_id AND s.statement_id = x.statement_id AND s.status = 'ACTIVE'
                    WHERE x.tenant_id = :t AND x.journey_id = :j AND x.match_basis LIKE 'LOAN%'
                    ORDER BY x.txn_date
                """),
                {"t": tenant_id, "j": str(journey_id)},
            ).mappings().all()
    except Exception:  # noqa: BLE001 -- the table may not exist yet on an old database
        return []
    return [{"date": r["txn_date"].isoformat() if r["txn_date"] else None, "amount": _money(r["amount"]),
             "narration": r["narration"], "basis": r["match_basis"],
             "basisLabel": BASIS_LABELS.get(r["match_basis"] or "", None), "bankName": r["bank_name"],
             "accountNumber": r["account_number"], "receiptOnFile": r["outcome"] == "MATCHED"} for r in rows]


__all__ = ["build_report", "journey_bank_matches", "journey_bank_status", "journey_bank_summary", "journey_rollup", "reconcile_dealer", "router",
           "statement_detail"]
