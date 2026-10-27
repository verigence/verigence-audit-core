"""In-house or external insurance: one deterministic rule (product owner 2026-10-27).

"If we don't find the Agent code or MISP code of the Dealer on the insurance, or
any reference of the insurance premium paid in the dealer invoice, then raise a
task for the PC to confirm whether it is an in-house or external insurance.
There should be a comment with the Agent Code / MISP Code, filled manually, and
then the task is closed."

So, in order:

1. The PC's confirmation decides (Deal tab or this task).
2. In-house when the policy carries one of the dealer's own Agent / MISP codes,
   or a dealer invoice bills the insurance premium (an INSURANCE line with an
   amount, or a debit note's insurance amount).
3. Otherwise, once a policy has been read, a PC task INSURANCE_SOURCE_CONFIRM
   asks: in-house or external, with the Agent / MISP code in the comment. An
   in-house answer records the code as the dealer's, so later policies with
   that code are recognised without asking.

Until the PC answers, the deal keeps the premium (as before: "Inhouse" with
decidedBy UNCONFIRMED) and the page says it is not confirmed.

Live 2026-10-27 (Sibaram Nahak and 75 more on Mahindra Orissa): every policy
showed as Inhouse by default, including those issued through Policybazaar,
Turtlemint, individual agents or banks; the documents were never consulted.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

TASK_TYPE = "INSURANCE_SOURCE_CONFIRM"
INHOUSE, EXTERNAL = "INHOUSE", "EXTERNAL"
# The dealer's own billing: its tax invoices and debit notes -- not a generic
# invoice (an insurer's own bill is uploaded as one), not a credit note, and
# not a proforma / quotation / estimate (Booking estimates list insurance).
_INVOICE_TYPES = ("customer_invoice_dms", "tax_invoice_tally", "tax_invoice_dms", "tax_invoice",
                  "customer_invoice_dms_v2", "debit_note")
_NOT_A_BILL = re.compile(r"pro\s*-?\s*forma|quotation|estimate|credit\s+note", re.IGNORECASE)
# A code as a person writes it: letters / digits with / & - and at least one digit.
_CODE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9/&\-.]{3,}[A-Za-z0-9]")


def normalize_code(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def codes_in(comment: str) -> list[str]:
    """The Agent / MISP codes a PC wrote in a comment."""
    out: list[str] = []
    for token in _CODE_TOKEN.findall(comment or ""):
        if any(ch.isdigit() for ch in token) and len(normalize_code(token)) >= 5 and token not in out:
            out.append(token)
    return out


def _dec(value: Any) -> float | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    try:
        return float(cleaned) if cleaned not in ("", ".", "-") else None
    except ValueError:
        return None


def _policy(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any] | None:
    row = connection.execute(
        text(
            """
            SELECT r.insurer_name, r.policy_reference, r.agent_intermediary_name, r.agent_intermediary_code,
                   r.misp_code, r.insurance_source, r.insurance_source_set_at_utc,
                   r.insurance_source_set_by_actor_id, j.dealer_id
            FROM auditcore.insurance_records r
            JOIN auditcore.journeys j ON j.tenant_id = r.tenant_id AND j.journey_id = r.journey_id
            WHERE r.tenant_id = :t AND r.journey_id = :j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().first()
    return dict(row) if row is not None else None


def dealer_codes(connection: Connection, *, tenant_id: str, dealer_id: Any) -> set[str]:
    if dealer_id is None:
        return set()
    return set(connection.execute(
        text("SELECT normalized_code FROM auditcore.dealer_insurance_codes WHERE tenant_id = :t AND dealer_id = :d"),
        {"t": tenant_id, "d": dealer_id},
    ).scalars().all())


def premium_on_dealer_invoice(connection: Connection, *, tenant_id: str, journey_id: UUID) -> float | None:
    """The premium a current dealer invoice bills, if any."""
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (f.di_document_id, f.field_key)
                   f.di_document_id, f.field_key, f.effective_value
            FROM auditcore.journey_document_extracted_fields f
            JOIN auditcore.evidence e
              ON e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id
             AND e.di_document_id = f.di_document_id AND e.association_status = 'ACTIVE'
            WHERE f.tenant_id = :t AND f.journey_id = :j
              AND COALESCE(e.document_type_key, f.source_document_type_key) = ANY(CAST(:types AS text[]))
              AND f.field_key IN ('line_items', 'insurance_amount', 'invoice_nature', 'invoice_purpose',
                                  'document_title')
              AND NOT EXISTS (
                  SELECT 1 FROM auditcore.p2_document_queue q
                  WHERE q.tenant_id = f.tenant_id AND q.di_document_id = f.di_document_id
                  HAVING bool_and(q.retired_at_utc IS NOT NULL OR q.queue_status IN ('MERGED', 'CANCELLED'))
              )
            ORDER BY f.di_document_id, f.field_key, f.source_fact_version DESC NULLS LAST, f.updated_at_utc DESC
            """
        ),
        {"t": tenant_id, "j": journey_id, "types": list(_INVOICE_TYPES)},
    ).mappings().all()
    not_a_bill = {
        str(r["di_document_id"]) for r in rows
        if r["field_key"] in ("invoice_nature", "invoice_purpose", "document_title")
        and _NOT_A_BILL.search(str(r["effective_value"] or ""))
    }
    best: float | None = None
    for row in rows:
        if row["field_key"] not in ("line_items", "insurance_amount") or str(row["di_document_id"]) in not_a_bill:
            continue
        value = row["effective_value"]
        if row["field_key"] == "insurance_amount":
            amounts = [_dec(value)]
        else:
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    continue
            amounts = [
                next((a for a in (_dec(item.get(k)) for k in ("net_amount", "total", "amount", "taxable_amount"))
                      if a is not None), None)
                for item in (value if isinstance(value, list) else [])
                if isinstance(item, dict) and str(item.get("line_category") or "").upper() == "INSURANCE"
            ]
        for amount in amounts:
            if amount is not None and amount > 0:
                best = max(best or 0.0, amount)
    return best


def document_decision(connection: Connection, *, tenant_id: str, journey_id: UUID,
                      policy: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """In-house from the documents, with the reason, or None when they do not show it."""
    policy = policy if policy is not None else _policy(connection, tenant_id=tenant_id, journey_id=journey_id)
    if policy is not None:
        known = dealer_codes(connection, tenant_id=tenant_id, dealer_id=policy["dealer_id"])
        for label, key in (("MISP code", "misp_code"), ("Agent code", "agent_intermediary_code")):
            if policy.get(key) and normalize_code(policy[key]) in known:
                return {"source": INHOUSE, "basis": f"The policy carries the dealer's {label} {policy[key]}."}
    premium = premium_on_dealer_invoice(connection, tenant_id=tenant_id, journey_id=journey_id)
    if premium is not None:
        return {"source": INHOUSE, "basis": f"The dealer invoice bills the insurance premium (Rs {premium:,.0f})."}
    return None


def resolve(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """The insurance source and who decided it: PC, DOCUMENT, UNCONFIRMED (a
    policy is on file and the PC is asked) or DEFAULT (no policy yet). Only
    SELF takes the premium out of the deal, as before."""
    policy = _policy(connection, tenant_id=tenant_id, journey_id=journey_id)
    if policy and policy["insurance_source"]:
        return {"source": str(policy["insurance_source"]), "decidedBy": "PC",
                "decidedAt": policy["insurance_source_set_at_utc"],
                "actorId": policy["insurance_source_set_by_actor_id"], "basis": None}
    decided = document_decision(connection, tenant_id=tenant_id, journey_id=journey_id, policy=policy)
    if decided is not None:
        return {"source": INHOUSE, "decidedBy": "DOCUMENT", "decidedAt": None, "actorId": None,
                "basis": decided["basis"]}
    if policy and (policy["insurer_name"] or policy["policy_reference"]):
        return {"source": INHOUSE, "decidedBy": "UNCONFIRMED", "decidedAt": None, "actorId": None,
                "basis": "Neither the dealer's Agent / MISP code nor a premium on the dealer invoice was found; "
                         "the PC is asked to confirm."}
    return {"source": INHOUSE, "decidedBy": "DEFAULT", "decidedAt": None, "actorId": None, "basis": None}


def _dedupe_key(journey_id: UUID) -> str:
    return f"insurance-source:{journey_id}"


def sync_insurance_source_task(
    connection: Connection, *, tenant_id: str, journey_id: UUID, evaluation_started_at: datetime | None = None,
) -> dict[str, int]:
    """Raise the PC's task while the rule cannot decide; close it once it can."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh, resolve_if_open

    decision = resolve(connection, tenant_id=tenant_id, journey_id=journey_id)
    key = _dedupe_key(journey_id)
    counts: dict[str, int] = {}
    if decision["decidedBy"] == "UNCONFIRMED":
        policy = _policy(connection, tenant_id=tenant_id, journey_id=journey_id) or {}
        shows = "; ".join(f"{label} {policy[k]}" for label, k in (
            ("insurer", "insurer_name"), ("agent", "agent_intermediary_name"),
            ("Agent code", "agent_intermediary_code"), ("MISP code", "misp_code")) if policy.get(k)) or "no agent details"
        _, outcome = raise_or_refresh(
            connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key, task_type=TASK_TYPE,
            source_type="DOCUMENT_FIELD", source_code="INSURANCE_SOURCE_UNCONFIRMED",
            title="Confirm the insurance: in-house or external",
            description=(
                f"The policy shows {shows}. Neither the dealer's Agent / MISP code nor an insurance premium on "
                "the dealer invoice was found, so it is not known whether the dealership arranged this "
                "insurance. Choose in-house or external and write the Agent code / MISP code in the comment. "
                "Until then the premium stays in the deal."),
            reference={
                "generatedBy": "SYSTEM", "sourceType": "DOCUMENT_FIELD", "sourceCode": "INSURANCE_SOURCE_UNCONFIRMED",
                "insurerName": policy.get("insurer_name"), "agentName": policy.get("agent_intermediary_name"),
                "agentCode": policy.get("agent_intermediary_code"), "mispCode": policy.get("misp_code"),
                "question": "Is this insurance in-house (through the dealership) or external?",
                "answers": [
                    {"value": INHOUSE, "label": "In-house: write the dealer's Agent / MISP code",
                     "requiresComment": True},
                    {"value": EXTERNAL, "label": "External: write the Agent / MISP code on the policy",
                     "requiresComment": True},
                ],
            },
            severity="MEDIUM", registry=get_registry(), evaluation_started_at=evaluation_started_at,
        )
        counts[outcome] = 1
    elif resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                         evidence={"decidedBy": decision["decidedBy"], "source": decision["source"],
                                   "basis": decision["basis"], "verifiedAt": datetime.now(UTC).isoformat()}):
        counts["VERIFIED"] = 1
    return counts


def answer_task(connection: Connection, *, tenant_id: str, task: dict[str, Any], actor_id: str,
                comment: str | None, details: dict[str, Any] | None) -> dict[str, Any]:
    """The PC's answer: in-house or external, with the Agent / MISP code."""
    from audit_core.uc03_p2_deal_actions import set_insurance_source

    answer = str((details or {}).get("answer") or "").upper()
    if answer not in (INHOUSE, EXTERNAL):
        raise ValueError("Choose in-house or external.")
    comment = (comment or "").strip()
    codes = codes_in(comment)
    if not codes:
        raise ValueError("Write the Agent code / MISP code in the comment, as printed on the policy.")
    journey_id = UUID(str(task["journey_id"]))
    set_insurance_source(
        connection, tenant_id=tenant_id, journey_id=journey_id, source=INHOUSE if answer == INHOUSE else "SELF",
        actor_id=actor_id, reason=comment, correlation_id=None, via="TASK",
    )
    if answer == INHOUSE:
        dealer_id = connection.execute(
            text("SELECT dealer_id FROM auditcore.journeys WHERE tenant_id = :t AND journey_id = :j"),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one_or_none()
        # Remember as the dealer's only a code this policy carries (never a date,
        # a phone or policy number from the comment). When the policy's codes
        # were not read, the PC's code is all there is.
        policy = _policy(connection, tenant_id=tenant_id, journey_id=journey_id) or {}
        on_policy = {normalize_code(policy.get(k)) for k in ("agent_intermediary_code", "misp_code")} - {""}
        remembered = [c for c in codes if normalize_code(c) in on_policy] if on_policy else codes
        for code in [c for c in remembered if len(normalize_code(c)) <= 120]:
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.dealer_insurance_codes (
                        tenant_id, dealer_id, normalized_code, code_as_entered, confirmed_by_actor_id,
                        journey_id, task_id
                    ) VALUES (:t, :d, :n, :c, :a, :j, :task)
                    ON CONFLICT (tenant_id, dealer_id, normalized_code) DO NOTHING
                    """
                ),
                {"t": tenant_id, "d": dealer_id, "n": normalize_code(code), "c": code[:200], "a": actor_id,
                 "j": journey_id, "task": task["task_id"]},
            )
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks
            SET task_status = 'VERIFIED_COMPLETE', verified_at_utc = now(), updated_at_utc = now(),
                completion_result = CAST(:result AS jsonb)
            WHERE tenant_id = :t AND task_id = :id
            """
        ),
        {"t": tenant_id, "id": task["task_id"], "result": json.dumps({
            "outcome": answer, "codes": codes, "answeredBy": actor_id, "answeredAt": datetime.now(UTC).isoformat(),
            "comment": comment})},
    )
    return {"taskId": str(task["task_id"]), "status": "VERIFIED_COMPLETE", "outcome": answer, "codes": codes}
