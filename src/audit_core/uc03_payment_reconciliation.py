"""uc03_payment_reconciliation.py — match captured Payments to reviewed bank
statement lines, deterministically.

DI extracts ``bank_statement_extract`` (verigence-di ``schemas/bank_statement.py``).
This module:

  1. Persists every reviewed statement line per journey in ``bank_statement_lines``.
  2. Matches each non-cash Payment to a bank credit on
        reference (exact, normalised)  OR  UTR-suffix (statement ref ends with the
            zero-stripped receipt ref, >= 6 chars)
        AND amount equal
        AND transaction date within +/- 3 days of the receipt date
     recording the outcome in ``payment_bank_matches``.
  3. A single match writes a VERIFIED ``payment_verification_events`` row (which
     already satisfies the Delivery-completion gate) and resolves the payment's
     ``PAYMENT_UNVERIFIED`` finding; no match raises that finding (DATA_GAP / PC).

Deterministic, idempotent, never raises.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_document_registry import is_bank_statement_document_type
from audit_core.uc03_v2_review_materialization import _upsert_review_value_row

logger = logging.getLogger(__name__)

_SYSTEM_ACTOR = "system:bank-reconciliation"
_FINDING_TYPE = "PAYMENT_UNVERIFIED"
_RULE_PREFIX = "PAYMENT_BANK_UNMATCHED"
_STAGE = "BOOKING"
_DATE_WINDOW_DAYS = 3
_MIN_UTR_LEN = 6

_BANK_LINE_FIELDS = (
    "bank_name",
    "account_holder_name",
    "account_number",
    "transaction_date",
    "value_date",
    "transaction_description",
    "reference_no",
    "counterparty_name",
    "debit_amount",
    "credit_amount",
    "running_balance",
    "manually_flagged",
)
_BANK_LINE_DATE_FIELDS = {"transaction_date", "value_date"}
_BANK_LINE_DECIMAL_FIELDS = {"debit_amount", "credit_amount", "running_balance"}
_BANK_LINE_BOOL_FIELDS = {"manually_flagged"}

_CASH_TOKENS = frozenset({"CASH", "CASHDEPOSIT", "CASHPAYMENT", "BYCASH"})
_NONCASH_TOKENS = frozenset(
    {"UPI", "NEFT", "RTGS", "IMPS", "CHEQUE", "CHQ", "DD", "DEMANDDRAFT", "CARD",
     "DEBITCARD", "CREDITCARD", "NETBANKING", "ONLINE", "BANKTRANSFER", "TRANSFER",
     "FUNDTRANSFER", "POS"}
)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    text_value = str(value).strip().replace(",", "")
    if not text_value:
        return None
    try:
        return Decimal(text_value)
    except (InvalidOperation, ValueError):
        return None


def _to_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    token = str(value).strip().lower()
    if token in {"true", "yes", "y", "1"}:
        return True
    if token in {"false", "no", "n", "0"}:
        return False
    return None


def _normalize_ref(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _payment_method_class(value: Any) -> str:
    """'CASH', 'NON_CASH', or 'UNKNOWN'."""
    token = _normalize_ref(value)
    if not token:
        return "UNKNOWN"
    if token in _CASH_TOKENS or token.startswith("CASH"):
        return "CASH"
    if token in _NONCASH_TOKENS or any(t in token for t in _NONCASH_TOKENS):
        return "NON_CASH"
    return "UNKNOWN"


def _utr_suffix_match(receipt_ref: str, bank_ref: str) -> bool:
    stripped = receipt_ref.lstrip("0")
    return len(stripped) >= _MIN_UTR_LEN and bank_ref.endswith(stripped)


def _fields_by_key(document: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in getattr(document, "fields", []) or []:
        key = str(getattr(field, "fieldKey", "")).strip().lower()
        if key:
            out[key] = getattr(field, "value", None)
    return out


def _line_values(raw: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key in _BANK_LINE_FIELDS:
        source = raw.get(key)
        if key in _BANK_LINE_DATE_FIELDS:
            values[key] = _to_date(source)
        elif key in _BANK_LINE_DECIMAL_FIELDS:
            values[key] = _to_decimal(source)
        elif key in _BANK_LINE_BOOL_FIELDS:
            values[key] = _to_bool(source)
        else:
            normalized = " ".join(str(source).split()) if source is not None else None
            values[key] = normalized or None
    return values


# ── ingestion ───────────────────────────────────────────────────────────────
def materialize_reviewed_bank_statements(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[Any],
    actor_id: str,
) -> int:
    written = 0
    for document in documents:
        if not is_bank_statement_document_type(getattr(document, "documentTypeKey", None)):
            continue
        if str(getattr(document, "extractionState", "")).upper() != "READY":
            continue
        raw = _fields_by_key(document)
        if not raw:
            continue
        values = _line_values(raw)
        if values.get("credit_amount") is None and values.get("debit_amount") is None:
            continue
        _upsert_review_value_row(
            connection,
            table_name="bank_statement_lines",
            id_column="bank_statement_line_id",
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document.documentId,
            evidence_id=getattr(document, "evidenceId", None),
            actor_id=actor_id,
            columns=_BANK_LINE_FIELDS,
            values=values,
        )
        written += 1
    return written


# ── matching ────────────────────────────────────────────────────────────────
def _payments(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT payment_id, amount, payment_method_code, payment_reference,
                   receipt_date, receipt_number
            FROM auditcore.payments
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY created_at_utc, payment_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _bank_lines(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT bank_statement_line_id, transaction_date, reference_no,
                   credit_amount, counterparty_name
            FROM auditcore.bank_statement_lines
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND credit_amount IS NOT NULL
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _match_one(payment: dict[str, Any], lines: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    amount = _to_decimal(payment["amount"])
    receipt_ref = _normalize_ref(payment["payment_reference"])
    receipt_date = _to_date(payment["receipt_date"])

    exact: list[dict[str, Any]] = []
    utr: list[dict[str, Any]] = []
    for line in lines:
        if amount is None or _to_decimal(line["credit_amount"]) != amount:
            continue
        line_date = _to_date(line["transaction_date"])
        if (
            receipt_date is not None
            and line_date is not None
            and abs((line_date - receipt_date).days) > _DATE_WINDOW_DAYS
        ):
            continue
        bank_ref = _normalize_ref(line["reference_no"])
        if receipt_ref and bank_ref and receipt_ref == bank_ref:
            exact.append(line)
        elif receipt_ref and bank_ref and (
            _utr_suffix_match(receipt_ref, bank_ref) or _utr_suffix_match(bank_ref, receipt_ref)
        ):
            utr.append(line)

    if len(exact) == 1:
        return exact, "REFERENCE_EXACT"
    if not exact and len(utr) == 1:
        return utr, "UTR_SUFFIX"
    if exact:
        return exact, "REFERENCE_EXACT"
    if utr:
        return utr, "UTR_SUFFIX"
    return [], "NONE"


def _upsert_match(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    payment_id: UUID,
    line_id: UUID | None,
    match_status: str,
    match_method: str,
    candidate_line_ids: list[str],
    details: dict[str, Any],
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.payment_bank_matches (
                tenant_id, journey_id, payment_id, bank_statement_line_id,
                match_status, match_method, candidate_line_ids, details, matched_at_utc
            ) VALUES (
                CAST(:tenant_id AS varchar), CAST(:journey_id AS uuid),
                CAST(:payment_id AS uuid), CAST(:line_id AS uuid),
                CAST(:match_status AS varchar), CAST(:match_method AS varchar),
                CAST(:candidates AS jsonb), CAST(:details AS jsonb),
                CASE WHEN CAST(:match_status AS varchar) = 'MATCHED' THEN now() ELSE NULL END
            )
            ON CONFLICT (tenant_id, payment_id) DO UPDATE SET
                bank_statement_line_id = EXCLUDED.bank_statement_line_id,
                match_status = EXCLUDED.match_status,
                match_method = EXCLUDED.match_method,
                candidate_line_ids = EXCLUDED.candidate_line_ids,
                details = EXCLUDED.details,
                matched_at_utc = CASE
                    WHEN EXCLUDED.match_status = 'MATCHED'
                    THEN COALESCE(auditcore.payment_bank_matches.matched_at_utc, now())
                    ELSE NULL END,
                updated_at_utc = now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "payment_id": payment_id,
            "line_id": line_id,
            "match_status": match_status,
            "match_method": match_method,
            "candidates": json.dumps(candidate_line_ids),
            "details": json.dumps(details),
        },
    )


def _record_verification(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    payment_id: UUID,
    line_id: UUID,
    correlation_id: str,
) -> None:
    already = connection.execute(
        text(
            """
            SELECT 1 FROM auditcore.payment_verification_events
            WHERE tenant_id = :tenant_id AND payment_id = :payment_id
              AND verification_result = 'VERIFIED'
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "payment_id": payment_id},
    ).scalar_one_or_none()
    if already is not None:
        return
    connection.execute(
        text(
            """
            INSERT INTO auditcore.payment_verification_events (
                tenant_id, journey_id, payment_id, verification_result,
                verification_notes, verified_by_actor_id, verified_by_role_code,
                correlation_id
            ) VALUES (
                :tenant_id, :journey_id, :payment_id, 'VERIFIED',
                :notes, :actor, 'SYSTEM', :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "payment_id": payment_id,
            "notes": f"Matched to bank statement line {line_id}.",
            "actor": _SYSTEM_ACTOR,
            "correlation_id": correlation_id or "",
        },
    )


def _resolve_flag(
    connection: Connection, *, tenant_id: str, journey_id: UUID, rule_key: str, correlation_id: str
) -> None:
    ids = connection.execute(
        text(
            """
            SELECT audit_finding_id, stage_code
            FROM auditcore.audit_findings
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND rule_key = :rule_key
              AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "rule_key": rule_key},
    ).mappings().all()
    for row in ids:
        connection.execute(
            text(
                """
                UPDATE auditcore.audit_findings
                SET finding_status = 'RESOLVED', disposition = 'FIXED',
                    resolved_at_utc = now(), updated_at_utc = now()
                WHERE tenant_id = :tenant_id AND audit_finding_id = :fid
                  AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
                """
            ),
            {"tenant_id": tenant_id, "fid": row["audit_finding_id"]},
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_finding_events (
                    tenant_id, audit_finding_id, journey_id, stage_code,
                    event_type, actor_id, actor_role_snapshot, safe_payload, correlation_id
                ) VALUES (
                    :tenant_id, :fid, :journey_id, :stage,
                    'RESOLVED', NULL, 'SYSTEM', CAST(:payload AS jsonb), :correlation_id
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "fid": row["audit_finding_id"],
                "journey_id": journey_id,
                "stage": row["stage_code"] or _STAGE,
                "payload": json.dumps({"disposition": "FIXED", "note": "Payment matched to a bank credit."}),
                "correlation_id": correlation_id or "",
            },
        )


def _raise_flag(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    payment: dict[str, Any],
    correlation_id: str,
) -> None:
    from audit_core.uc03_delivery_commands import _machine_flag

    _machine_flag(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=_STAGE,
        rule_key=f"{_RULE_PREFIX}:{payment['payment_id']}",
        finding_type=_FINDING_TYPE,
        severity="HIGH",
        title="Payment not evidenced in a bank statement",
        description=(
            "A non-cash payment could not be matched to a bank credit on reference, "
            "amount and date. Attach the bank statement showing this credit."
        ),
        correlation_id=correlation_id or "",
        safe_payload={
            "paymentId": str(payment["payment_id"]),
            "receiptNumber": payment.get("receipt_number"),
            "amount": str(payment.get("amount")) if payment.get("amount") is not None else None,
            "paymentMethodCode": payment.get("payment_method_code"),
            "paymentReference": payment.get("payment_reference"),
        },
    )


# ── producer ────────────────────────────────────────────────────────────────
def reconcile_payments(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str
) -> dict[str, Any]:
    """Match every Payment against the reviewed bank statement lines. Never raises,
    and never leaves the caller's transaction poisoned (own SAVEPOINT)."""
    try:
        with connection.begin_nested():
            return _reconcile_payments(
                connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
            )
    except Exception:
        logger.warning("reconcile_payments failed", exc_info=True)
        return {"error": True}


def _reconcile_payments(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str
) -> dict[str, Any]:
    payments = _payments(connection, tenant_id=tenant_id, journey_id=journey_id)
    if not payments:
        return {"skipped": True, "reason": "no_payments"}
    lines = _bank_lines(connection, tenant_id=tenant_id, journey_id=journey_id)

    matched = unmatched = not_applicable = ambiguous = 0
    for payment in payments:
        payment_id = payment["payment_id"]
        rule_key = f"{_RULE_PREFIX}:{payment_id}"
        method_class = _payment_method_class(payment["payment_method_code"])
        has_reference = bool(_normalize_ref(payment["payment_reference"]))

        if method_class == "CASH" or (method_class == "UNKNOWN" and not has_reference):
            _upsert_match(
                connection, tenant_id=tenant_id, journey_id=journey_id, payment_id=payment_id,
                line_id=None, match_status="NOT_APPLICABLE", match_method="NONE",
                candidate_line_ids=[], details={"reason": "cash_or_unverifiable"},
            )
            _resolve_flag(
                connection, tenant_id=tenant_id, journey_id=journey_id,
                rule_key=rule_key, correlation_id=correlation_id,
            )
            not_applicable += 1
            continue

        candidates, method = _match_one(payment, lines)
        if len(candidates) == 1:
            line_id = candidates[0]["bank_statement_line_id"]
            _upsert_match(
                connection, tenant_id=tenant_id, journey_id=journey_id, payment_id=payment_id,
                line_id=line_id, match_status="MATCHED", match_method=method,
                candidate_line_ids=[str(line_id)],
                details={"counterpartyName": candidates[0].get("counterparty_name")},
            )
            _record_verification(
                connection, tenant_id=tenant_id, journey_id=journey_id,
                payment_id=payment_id, line_id=line_id, correlation_id=correlation_id,
            )
            _resolve_flag(
                connection, tenant_id=tenant_id, journey_id=journey_id,
                rule_key=rule_key, correlation_id=correlation_id,
            )
            matched += 1
        elif len(candidates) > 1:
            _upsert_match(
                connection, tenant_id=tenant_id, journey_id=journey_id, payment_id=payment_id,
                line_id=None, match_status="AMBIGUOUS", match_method=method,
                candidate_line_ids=[str(c["bank_statement_line_id"]) for c in candidates],
                details={"candidateCount": len(candidates)},
            )
            ambiguous += 1
        else:
            _upsert_match(
                connection, tenant_id=tenant_id, journey_id=journey_id, payment_id=payment_id,
                line_id=None, match_status="UNMATCHED", match_method="NONE",
                candidate_line_ids=[], details={"methodClass": method_class},
            )
            _raise_flag(
                connection, tenant_id=tenant_id, journey_id=journey_id,
                payment=payment, correlation_id=correlation_id,
            )
            unmatched += 1

    return {
        "matched": matched,
        "unmatched": unmatched,
        "notApplicable": not_applicable,
        "ambiguous": ambiguous,
        "bankLines": len(lines),
    }


__all__ = ["materialize_reviewed_bank_statements", "reconcile_payments"]
