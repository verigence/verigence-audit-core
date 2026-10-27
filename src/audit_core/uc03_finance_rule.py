"""Finance: one deterministic rule (product owner 2026-10-27).

"Cash can't be treated as Loan or Finance. For Finance it will be on RTO,
Insurance hypothecation or payment receipt by the name of the financier."

A Journey is financed only when one of its current documents shows it:

1. the RTO challan names a financier, or charges hypothecation (the HP charges
   amount, or a line item such as "Hypothecation Addition" with an amount);
2. the insurance policy shows the vehicle hypothecated to a financier;
3. a payment receipt was received from a financier ("SBI (FINANCER)",
   "ICICI Finance (D.O)", "NIRANJAN ROUT ( HP TO CANARA BANK )").

Nothing else makes a deal financed: not an invoice's "Financed by" (DMS
invoices print CASH there for cash sales), not a ledger. The invoice's
"Financed by" is used only for the financier's name when a rule above already
proves finance but names nobody. "Current" documents are the ones still on the
Journey: active evidence whose pages were not merged, cancelled or retired.

The finance record follows the documents both ways: it is created or corrected
when they prove finance, and removed when they no longer do (a voided invoice
no longer leaves a loan behind). A record a person entered is never touched,
nor one whose loan disbursement a PC confirmed.

Live 2026-10-27 (Mahindra Orissa): 12 Journeys showed a loan from "CASH", 5
more a loan that no current document supported; 4 financed deals showed none.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, text

logger = structlog.get_logger(__name__)

_RTO_TYPES = ("rto_challan",)
_INSURANCE_TYPES = ("insurance_cover", "insurance_cover_note", "insurance_policy")
_RECEIPT_TYPES = ("dealer_receipt", "payment_receipt")
_INSURANCE_HYPOTHECATION_KEYS = ("hypothecated_to", "hypothecation", "financier_name", "financer_name")
_RTO_FINANCIER_KEYS = ("financer_name", "financier_name", "hypothecated_to")
_RTO_HP_AMOUNT_KEYS = ("hp_charges_amount",)
_RECEIPT_PAYER_KEYS = ("customer_name", "received_from")
_INVOICE_FINANCIER_KEYS = ("financed_by",)
_RELEVANT_KEYS = frozenset(
    _INSURANCE_HYPOTHECATION_KEYS + _RTO_FINANCIER_KEYS + _RTO_HP_AMOUNT_KEYS + _RECEIPT_PAYER_KEYS
    + _INVOICE_FINANCIER_KEYS + ("line_items",)
)

# Words that, on their own, name no financier: a value made only of these (and
# numbers / punctuation) is "no finance" -- "N.A.", "Hypothecation: NIL",
# "NOT HYPOTHECATED", "0.00". CASH and SELF anywhere at the start mean no
# financier too ("By Cash", "CASH SALES", "Self Finance", "Cash/Self").
_EMPTY_WORDS = frozenset({
    "na", "n", "a", "nil", "nill", "null", "none", "no", "not", "applicable", "hypothecation", "hypothecated",
    "hypothecate", "to", "with", "without", "financer", "financier", "finance", "name", "hpa", "by", "of",
    "the", "is", "nan",
})
_CASH_OR_SELF = re.compile(r"^(by\s+)?(cash|self|own)\b", re.IGNORECASE)
# Receipt payer names that are a financier paying the dealer.
_FINANCIER_MARKER = re.compile(r"\(\s*(financ(er|ier|e)|d\s*\.?\s*o\s*\.?)\s*\)|\bhp\s+to\b", re.IGNORECASE)
_INSTITUTION = re.compile(
    r"\bbank\b|financ|\bfinserv\b|\bfincorp\b|\bmmfsl\b|\bm\.?f\.?s\.?l\b|\bnbfc\b|\bleasing\b|\bhdb\b"
    r"|\bchola\b|\bcholamandalam\b|\bsundaram\b|\bshriram\b|\btata\s+capital\b|\bidfc\b|\bhdfc\b|\bicici\b"
    r"|\baxis\b|\bkotak\b|\bindusind\b|\bhinduja\b|\bmuthoot\b|\bau\s+small\b",
    re.IGNORECASE)
_NOT_A_PAYER = re.compile(r"\bbank\s+charges?\b|\bneft\b|\brtgs\b|\bimps\b|\bthrough\b|\(\s*self\s*\)", re.IGNORECASE)
_HP_TO = re.compile(r"\bhp\s+to\s+(?P<name>[^)]+)", re.IGNORECASE)
_HYPOTHECATION_LINE = re.compile(r"hypoth|\bh\.?\s*p\.?\s*a\b", re.IGNORECASE)


def names_a_financier(value: Any) -> bool:
    """True only for a value that actually names someone: never CASH / SELF,
    never "N.A.", "NIL", "Hypothecation: NIL", "NOT HYPOTHECATED", "0.00"."""
    text_value = _text(value)
    if not text_value or _CASH_OR_SELF.match(text_value.strip(" .:-/")):
        return False
    words = [w for w in re.split(r"[^a-z0-9]+", text_value.lower()) if w]
    return any(w not in _EMPTY_WORDS and not w.isdigit() for w in words)


def _same_name(a: str | None, b: str | None) -> bool:
    def key(value: str | None) -> str:
        return re.sub(r"[^a-z0-9]", "", re.sub(r"\b(m\s*/\s*s|mr|mrs|ms|shri|smt)\b", "", (value or "").lower()))
    return bool(key(a)) and key(a) == key(b)


def receipt_financier(payer: Any, *, customer_names: tuple[str, ...] = ()) -> str | None:
    """The financier a receipt was received from, or None for a customer's own
    payment. A payer who is the Journey's customer is never a financier (a
    customer that is itself a bank buys its own vehicle)."""
    name = _text(payer)
    if not name or _NOT_A_PAYER.search(name):
        return None
    hp_to = _HP_TO.search(name)
    if hp_to:
        return _clean_name(hp_to.group("name"))
    cleaned = _clean_name(_FINANCIER_MARKER.sub("", name))
    if any(_same_name(cleaned, c) for c in customer_names):
        return None
    if _INSTITUTION.search(name):
        return cleaned
    return None


def receipt_from_financier_for_customer(payer: Any, *, customer_names: tuple[str, ...] = ()) -> bool:
    """A receipt in the customer's own name marked as the financier's payment,
    e.g. "R K SAHOO (DO)": finance, with no financier named on it."""
    name = _text(payer)
    if not name or not _FINANCIER_MARKER.search(name) or _HP_TO.search(name):
        return False
    return any(_same_name(_clean_name(_FINANCIER_MARKER.sub("", name)), c) for c in customer_names)


def _clean_name(value: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", value).strip(" ,.-()")
    return cleaned or None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return ""
    result = " ".join(str(value).split()).strip().strip('"')
    return "" if result.lower() in {"null", "none"} else result


def _amount(value: Any) -> Decimal | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    # "Rs. 1,500/-" -> 1500 (the "." of "Rs." is not a decimal point).
    raw = re.sub(r"(?i)\b(rs|inr)\.?|₹|/-", " ", str(value))
    cleaned = re.sub(r"[^0-9.\-]", "", raw).strip(".")
    if cleaned in ("", ".", "-"):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _line_entries(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def hypothecation_line_amount(line_items: Any) -> Decimal | None:
    """The amount of an RTO line item charging hypothecation, if any."""
    total = Decimal(0)
    found = False
    for entry in _line_entries(line_items):
        if not _HYPOTHECATION_LINE.search(_text(entry.get("description_raw") or entry.get("description"))):
            continue
        amount = _amount(entry.get("total"))
        if amount is None:
            amount = _amount(entry.get("amount"))
        if amount is not None and amount > 0:
            total += amount
            found = True
    return total if found else None


@dataclass
class FinanceEvidence:
    financed: bool = False
    provider_name: str | None = None
    financed_amount: Decimal | None = None
    basis: list[str] = field(default_factory=list)
    source_document_id: str | None = None


@dataclass
class _Doc:
    document_id: str
    document_type: str
    fields: dict[str, Any]


def _current_documents(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, _Doc]:
    rows = connection.execute(
        text(
            """
            -- The newest reading of each field of each current document (a
            -- document can hold several fact versions and both stages' rows).
            SELECT DISTINCT ON (f.di_document_id, f.field_key)
                   f.di_document_id, COALESCE(e.document_type_key, f.source_document_type_key) AS document_type,
                   f.field_key, f.effective_value
            FROM auditcore.journey_document_extracted_fields f
            JOIN auditcore.evidence e
              ON e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id
             AND e.di_document_id = f.di_document_id AND e.association_status = 'ACTIVE'
            WHERE f.tenant_id = :t AND f.journey_id = :j
              AND f.field_key = ANY(CAST(:keys AS text[]))
              AND NOT EXISTS (
                  SELECT 1 FROM auditcore.p2_document_queue q
                  WHERE q.tenant_id = f.tenant_id AND q.di_document_id = f.di_document_id
                  HAVING bool_and(q.retired_at_utc IS NOT NULL OR q.queue_status IN ('MERGED', 'CANCELLED'))
              )
            ORDER BY f.di_document_id, f.field_key, f.source_fact_version DESC NULLS LAST,
                     f.updated_at_utc DESC
            """
        ),
        {"t": tenant_id, "j": journey_id, "keys": sorted(_RELEVANT_KEYS)},
    ).mappings().all()
    docs: dict[str, _Doc] = {}
    for row in rows:
        doc_id = str(row["di_document_id"])
        doc = docs.setdefault(doc_id, _Doc(doc_id, str(row["document_type"] or "").lower(), {}))
        doc.fields[str(row["field_key"])] = row["effective_value"]
    return docs


def _overlay(connection: Connection, *, tenant_id: str, docs: dict[str, _Doc], documents: list[Any]) -> None:
    """Values being confirmed right now win over what is stored for the same document."""
    from audit_core.uc03_delivery_review_materialization import (
        live_documents_newest_first,
    )

    for document in live_documents_newest_first(connection, tenant_id=tenant_id, documents=list(documents)):
        doc_id = str(getattr(document, "documentId", "") or "")
        if not doc_id or str(getattr(document, "extractionState", "READY")).upper() != "READY":
            continue
        doc = docs.setdefault(
            doc_id, _Doc(doc_id, str(getattr(document, "documentTypeKey", "") or "").lower(), {}))
        for item in getattr(document, "fields", []) or []:
            key = str(getattr(item, "fieldKey", "") or "")
            if key in _RELEVANT_KEYS:
                doc.fields[key] = getattr(item, "value", None)


def finance_evidence(
    connection: Connection, *, tenant_id: str, journey_id: UUID, documents: list[Any] | None = None,
) -> FinanceEvidence:
    docs = _current_documents(connection, tenant_id=tenant_id, journey_id=journey_id)
    if documents:
        _overlay(connection, tenant_id=tenant_id, docs=docs, documents=documents)
    customers = _customer_names(connection, tenant_id=tenant_id, journey_id=journey_id)
    found = FinanceEvidence()
    names: list[tuple[int, str, str]] = []  # (priority, name, document)
    hp_amounts: list[Decimal] = []
    for doc in sorted(docs.values(), key=lambda d: d.document_id):
        kind, values = doc.document_type, doc.fields
        if kind in _RTO_TYPES:
            for key in _RTO_FINANCIER_KEYS:
                if names_a_financier(values.get(key)):
                    found.basis.append(f"RTO challan names the financier ({_text(values[key])})")
                    names.append((0, _text(values[key]), doc.document_id))
            amount = next((a for a in (_amount(values.get(k)) for k in _RTO_HP_AMOUNT_KEYS) if a), None)
            if amount is None:
                amount = hypothecation_line_amount(values.get("line_items"))
            if amount is not None and amount > 0:
                found.basis.append(f"RTO challan charges hypothecation (Rs {amount:,.0f})")
                hp_amounts.append(amount)
                found.source_document_id = found.source_document_id or doc.document_id
        elif kind in _INSURANCE_TYPES:
            for key in _INSURANCE_HYPOTHECATION_KEYS:
                if names_a_financier(values.get(key)):
                    found.basis.append(f"Insurance policy is hypothecated to {_text(values[key])}")
                    names.append((1, _text(values[key]), doc.document_id))
        elif kind in _RECEIPT_TYPES:
            for key in _RECEIPT_PAYER_KEYS:
                financier = receipt_financier(values.get(key), customer_names=customers)
                if financier:
                    found.basis.append(f"Payment receipt received from {_text(values[key])}")
                    names.append((2, financier, doc.document_id))
                elif receipt_from_financier_for_customer(values.get(key), customer_names=customers):
                    found.basis.append(f"Payment receipt marked as the financier's payment ({_text(values[key])})")
                    found.source_document_id = found.source_document_id or doc.document_id
    found.financed = bool(found.basis)
    if names:
        _priority, name, document_id = min(names, key=lambda n: (n[0], n[2]))
        found.provider_name = name
        found.source_document_id = document_id
    elif found.financed:
        # Proven by hypothecation charges alone: the invoice may still name the financier.
        invoice_names = sorted(
            (_text(d.fields.get(k)), d.document_id) for d in docs.values() for k in _INVOICE_FINANCIER_KEYS
            if names_a_financier(d.fields.get(k))
        )
        if invoice_names:
            found.provider_name = invoice_names[0][0]
    if hp_amounts:
        found.financed_amount = max(hp_amounts)
    return found


def _customer_names(connection: Connection, *, tenant_id: str, journey_id: UUID) -> tuple[str, ...]:
    row = connection.execute(
        text(
            """
            SELECT c.display_name, c.legal_name FROM auditcore.journeys j
            JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
            WHERE j.tenant_id = :t AND j.journey_id = :j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().first()
    buyers = connection.execute(
        text(
            """
            SELECT DISTINCT f.effective_value #>> '{}' FROM auditcore.journey_document_extracted_fields f
            JOIN auditcore.evidence e ON e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id
             AND e.di_document_id = f.di_document_id AND e.association_status = 'ACTIVE'
            WHERE f.tenant_id = :t AND f.journey_id = :j AND f.field_key IN ('buyer_name', 'customer_name')
              AND COALESCE(e.document_type_key, f.source_document_type_key) NOT IN ('dealer_receipt', 'payment_receipt')
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).scalars().all()
    names = [v for v in ((row or {}).get("display_name"), (row or {}).get("legal_name"), *buyers) if v]
    return tuple(dict.fromkeys(str(n) for n in names))


def _evidence_id(connection: Connection, *, tenant_id: str, di_document_id: str | None) -> UUID | None:
    if not di_document_id:
        return None
    return connection.execute(
        text("SELECT evidence_id FROM auditcore.evidence WHERE tenant_id = :t AND di_document_id = CAST(:d AS uuid)"),
        {"t": tenant_id, "d": di_document_id},
    ).scalar_one_or_none()


def apply_finance_rule(
    connection: Connection, *, tenant_id: str, journey_id: UUID, documents: list[Any] | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Make the Journey's finance record match what its documents prove.

    Returns {"action": CREATED | UPDATED | REMOVED | UNCHANGED | KEPT_PERSON_ENTERED |
    KEPT_PC_CONFIRMED_DISBURSEMENT | NONE, ...}. With write=False nothing is changed."""
    if write:
        # One pass per Journey at a time: two passes finding no row could both insert one.
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                           {"k": f"finance-rule:{tenant_id}:{journey_id}"})
    evidence = finance_evidence(connection, tenant_id=tenant_id, journey_id=journey_id, documents=documents)
    records = connection.execute(
        text(
            """
            SELECT finance_record_id, finance_type_code, provider_name, financed_amount, source_kind,
                   loan_disbursement_confidence
            FROM auditcore.finance_records
            WHERE tenant_id = :t AND journey_id = :j
            ORDER BY created_at_utc DESC, finance_record_id DESC
            """ + (" FOR UPDATE" if write else "")
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    latest = records[0] if records else None
    out: dict[str, Any] = {
        "financed": evidence.financed, "provider": evidence.provider_name,
        "financedAmount": str(evidence.financed_amount) if evidence.financed_amount is not None else None,
        "basis": evidence.basis,
        "before": ({"provider": latest["provider_name"], "type": latest["finance_type_code"],
                    "sourceKind": latest["source_kind"]} if latest else None),
    }
    # Only a record Audit Core copied from documents (EVIDENCE) is Audit Core's
    # to change; one a person entered -- OPERATIONAL_INPUT, SOURCE_SYSTEM, or
    # no source given -- is never touched.
    if latest is not None and latest["source_kind"] != "EVIDENCE":
        return {**out, "action": "KEPT_PERSON_ENTERED"}

    if not evidence.financed:
        if latest is None:
            return {**out, "action": "NONE"}
        if any(r["loan_disbursement_confidence"] == "PC_CONFIRMED" for r in records):
            return {**out, "action": "KEPT_PC_CONFIRMED_DISBURSEMENT"}
        if write:
            connection.execute(
                text("DELETE FROM auditcore.finance_records WHERE tenant_id = :t AND journey_id = :j "
                     "AND source_kind = 'EVIDENCE'"),
                {"t": tenant_id, "j": journey_id},
            )
            _after_finance_removed(connection, tenant_id=tenant_id, journey_id=journey_id)
            logger.info("finance_record_removed", tenant_id=tenant_id, journey_id=str(journey_id),
                        provider=latest["provider_name"])
        return {**out, "action": "REMOVED"}

    values = {"finance_type_code": "LOAN", "provider_name": evidence.provider_name,
              "financed_amount": evidence.financed_amount}
    if latest is not None and all(latest[k] == v for k, v in values.items()):
        return {**out, "action": "UNCHANGED"}
    if not write:
        return {**out, "action": "UPDATED" if latest is not None else "CREATED"}
    source_evidence_id = _evidence_id(connection, tenant_id=tenant_id, di_document_id=evidence.source_document_id)
    if latest is None:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.finance_records (
                    tenant_id, journey_id, finance_type_code, provider_name, financed_amount,
                    source_kind, source_evidence_id, details
                ) VALUES (:t, :j, :finance_type_code, :provider_name, :financed_amount, 'EVIDENCE', :ev,
                          CAST(:details AS jsonb))
                """
            ),
            {"t": tenant_id, "j": journey_id, **values, "ev": source_evidence_id,
             "details": _details(evidence)},
        )
        return {**out, "action": "CREATED"}
    provider_changed = (latest["provider_name"] or "") != (evidence.provider_name or "")
    connection.execute(
        text(
            """
            UPDATE auditcore.finance_records
            SET finance_type_code = :finance_type_code, provider_name = :provider_name,
                financed_amount = :financed_amount, source_kind = 'EVIDENCE',
                source_evidence_id = COALESCE(:ev, source_evidence_id),
                details = CAST(:details AS jsonb),
                -- A different financier: the disbursement found for the old one no longer applies.
                loan_disbursement_amount = CASE WHEN :reset THEN NULL ELSE loan_disbursement_amount END,
                loan_disbursement_payment_id = CASE WHEN :reset THEN NULL ELSE loan_disbursement_payment_id END,
                loan_disbursement_confidence = CASE WHEN :reset THEN NULL ELSE loan_disbursement_confidence END,
                loan_disbursement_match_basis = CASE WHEN :reset THEN NULL ELSE loan_disbursement_match_basis END,
                updated_at_utc = now()
            WHERE tenant_id = :t AND finance_record_id = :id
            """
        ),
        {"t": tenant_id, "id": latest["finance_record_id"], **values, "ev": source_evidence_id,
         "details": _details(evidence),
         "reset": provider_changed and latest["loan_disbursement_confidence"] != "PC_CONFIRMED"},
    )
    return {**out, "action": "UPDATED"}


def _details(evidence: FinanceEvidence) -> str:
    import json

    return json.dumps({"rule": "FINANCE_RULE_2026_10_27", "basis": evidence.basis})


def _after_finance_removed(connection: Connection, *, tenant_id: str, journey_id: UUID) -> None:
    """No finance: close what only a financed deal raises."""
    from audit_core.uc03_delivery_review_materialization import (
        sync_finance_hypothecation_findings,
    )
    from audit_core.uc03_finance_disbursement_resolution import TASK_TYPE
    from audit_core.workflow import cancel_workflow_task

    for stage in ("DELIVERY", "BOOKING"):
        sync_finance_hypothecation_findings(connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage)
    open_tasks = connection.execute(
        text(
            """
            SELECT workflow_task_id FROM auditcore.workflow_tasks
            WHERE tenant_id = :t AND journey_id = :j AND task_type = :type
              AND task_status IN ('PENDING','READY','CLAIMED','IN_PROGRESS','RETRY_WAIT')
            """
        ),
        {"t": tenant_id, "j": journey_id, "type": TASK_TYPE},
    ).scalars().all()
    for task_id in open_tasks:
        cancel_workflow_task(connection, tenant_id=tenant_id, workflow_task_id=task_id, actor_id="SYSTEM",
                             reason="Not a financed deal: no RTO, insurance or receipt shows a financier.")
