"""uc03_p2_audit_rules.py — the deal-audit checks an auditor runs by hand.

Native Phase 2 controls that read the facts Audit Core already holds (the
Deal sheet, the receipts, the finance and trade-in records, the documents)
and answer the questions a manual deal audit asks:

  Deal undercharged        DEAL_UNDERCHARGED           per price component + net
                           TCS_SHORT                   statutory rate above the threshold
  Excess discount          EXCESS_DISCOUNT             per discount + total
  Short payment            DELIVERED_ON_SHORT_PAYMENT, PAYMENT_AFTER_DELIVERY_WITHIN_GRACE,
                           PAYMENT_AFTER_DELIVERY_BEYOND_GRACE
  Financier (DO)           DO_PAYMENT_NOT_RECEIVED, DO_SHORT_PAYMENT
  Trade-in                 TRADE_IN_NOT_RESOLD, TRADE_IN_SOLD_AT_LOSS
  Refund                   POST_DELIVERY_REFUND
  Receipts                 CASH_ABOVE_LIMIT, PAYMENT_BEFORE_BOOKING
  Third-party payer        THIRD_PARTY_PAYMENT_UNCONFIRMED (PC question),
                           THIRD_PARTY_PAYMENT_UNDECLARED (TL violation)
  Cash intimation          CASH_INTIMATION_UNCONFIRMED (PC question), CASH_NOT_INTIMATED
  NDC signature            NDC_SIGNATURE_UNCONFIRMED (PC question), NDC_NOT_SIGNED
  Accessories fitted       ACCESSORIES_FITTED_UNCONFIRMED (PC question), ACCESSORY_FITTED_UNBILLED
  Delivery in time         DELIVERY_NOT_COMPLETED_IN_TIME  complete within N days of the printed date

A "Manual Observations" control cannot be read from a document: its task
asks a question and fails until a PC, TL or PM answers it (the answer and
their remark are the task's completion result); the paired violation
control fails when the answer is No and carries the remark.

Every night (``queue_nightly_review``, P2_NIGHTLY_REVIEW_UTC) the worker
re-runs the Delivery checks for each Journey whose delivery has started and
is not yet reviewed, so the time-based checks fire without a document
event: the settlement window (P2_SETTLEMENT_GRACE_DAYS, 7), the financier
window (P2_FINANCE_DISBURSEMENT_DAYS, 12), the resale window
(P2_TRADE_IN_RESALE_DAYS, 90) and the delivery-completion window
(P2_DELIVERY_COMPLETION_DAYS, 7, from the date printed on the earliest of
the invoice, insurance cover note and gate pass). The completion window
also has its own event: ``schedule_delivery_completion_check`` queues the
checks for the morning the window closes, so the Team Lead's High task,
listing everything still pending, is raised that day.

A failing violation check is an Audit Finding (rule_key = the control code)
with the check's wording; it resolves itself when the check passes, and a
Team Lead may close it from Findings.

Every rule reads only; it records one Execution Log row per run and the
control ledger turns the outcome into a task. Nothing here blocks a real
delivery from being recorded.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import cached_property
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, text

from audit_core.uc03_p2_dates import parse_extracted_date
from audit_core.uc03_p2_names import same_person
from audit_core.uc03_p2_runtime import enqueue_work
from audit_core.uc03_p2_task_producer import format_value
from audit_core.uc03_rule_execution_log import record_execution

logger = structlog.get_logger(__name__)

# A gap below this is rounding, not a finding (the Rule Engine uses the same).
_TOLERANCE = Decimal(1000)
# Per component or discount line.
_LINE_TOLERANCE = Decimal(100)
# The time windows, in days after delivery, each set per deployment.
_SETTLEMENT_DAYS = int(os.environ.get("P2_SETTLEMENT_GRACE_DAYS", "7"))       # balance due after delivery
_FINANCE_DAYS = int(os.environ.get("P2_FINANCE_DISBURSEMENT_DAYS", "12"))    # financier pays the delivery order
_TRADE_IN_RESALE_DAYS = int(os.environ.get("P2_TRADE_IN_RESALE_DAYS", "90"))  # exchange vehicle resold
# The Delivery must be complete within this many days of the date printed on
# the earliest of the invoice, the insurance cover note and the gate pass.
_DELIVERY_COMPLETION_DAYS = int(os.environ.get("P2_DELIVERY_COMPLETION_DAYS", "7"))
# Statutory limits, set per deployment: a single cash receipt above the
# limit (Income-tax Act s.269ST), TCS on a vehicle priced above the threshold.
_CASH_RECEIPT_LIMIT = Decimal(os.environ.get("P2_CASH_RECEIPT_LIMIT", "200000"))
_TCS_THRESHOLD = Decimal(os.environ.get("P2_TCS_THRESHOLD", "1000000"))
_TCS_RATE_PERCENT = Decimal(os.environ.get("P2_TCS_RATE_PERCENT", "1"))

_YES = {"value": "YES", "label": "Yes"}
_NO = {"value": "NO", "label": "No", "requiresComment": True}


@dataclass(frozen=True)
class RuleOutcome:
    code: str
    outcome: str  # PASS | FAIL | SKIPPED | ERROR
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _rupees(value: Any) -> str:
    return format_value("amount", value)


def _when(value: date | datetime | None) -> str:
    if value is None:
        return "an unknown date"
    day = value.date() if isinstance(value, datetime) else value
    return day.strftime("%d %b %Y")


def _dec(value: Any) -> Decimal | None:
    from audit_core.uc03_p2_journey360 import _dec as parse

    return parse(value)


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return parse_extracted_date(value)


# ------------------------------------------------------------------- facts


class _Facts:
    """Everything the rules read, fetched once per evaluation."""

    def __init__(self, connection: Connection, tenant_id: str, journey_id: UUID) -> None:
        self.connection = connection
        self.tenant_id = tenant_id
        self.journey_id = journey_id
        self.today = datetime.now(UTC).date()
        self._params = {"t": tenant_id, "j": journey_id}

    def _rows(self, sql: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.connection.execute(text(sql), self._params).mappings().all()]

    @cached_property
    def sheet(self) -> dict[str, Any]:
        from audit_core.uc03_p2_journey360 import deal

        return deal(self.connection, tenant_id=self.tenant_id, journey_id=self.journey_id)

    @cached_property
    def payments(self) -> list[dict[str, Any]]:
        """Every evidenced payment on an active document, duplicates of the
        same receipt dropped, oldest first."""
        from audit_core.uc03_p2_journey360 import _duplicate_receipts

        rows = self._rows(
            """
            SELECT p.payment_id, p.amount, p.receipt_number, p.receipt_date, p.created_at_utc,
                   p.source_di_document_id, p.receipt_customer_name,
                   COALESCE(p.payment_mode_code, p.payment_method_code) AS mode,
                   (SELECT l.counterparty_name FROM auditcore.payment_bank_matches m
                      JOIN auditcore.bank_statement_lines l
                        ON l.tenant_id=m.tenant_id AND l.bank_statement_line_id=m.bank_statement_line_id
                     WHERE m.tenant_id=p.tenant_id AND m.payment_id=p.payment_id AND m.match_status='MATCHED'
                     LIMIT 1) AS counterparty
            FROM auditcore.payments p
            JOIN auditcore.evidence e
              ON e.tenant_id=p.tenant_id AND e.journey_id=p.journey_id AND e.di_document_id=p.source_di_document_id
            WHERE p.tenant_id=:t AND p.journey_id=:j AND p.status_source='EVIDENCE'
              AND e.association_status='ACTIVE' AND p.amount IS NOT NULL
            ORDER BY p.receipt_date ASC NULLS LAST, p.created_at_utc ASC, p.payment_id ASC
            """
        )
        positive = [r for r in rows if Decimal(r["amount"]) > 0]
        duplicates = _duplicate_receipts(positive)
        out = []
        for r in rows:
            if r["payment_id"] in duplicates:
                continue
            out.append({
                "paymentId": str(r["payment_id"]),
                "documentId": str(r["source_di_document_id"]) if r["source_di_document_id"] else None,
                "amount": Decimal(r["amount"]),
                "receiptNumber": r["receipt_number"],
                "date": _day(r["receipt_date"]),
                "mode": str(r["mode"] or "").upper(),
                "payer": " ".join(str(r["receipt_customer_name"] or "").split()) or None,
                "counterparty": " ".join(str(r["counterparty"] or "").split()) or None,
                "createdAt": r["created_at_utc"],
            })
        return out

    @cached_property
    def receipts(self) -> list[dict[str, Any]]:
        return [p for p in self.payments if p["amount"] > 0 and p["mode"] != "REFUND"]

    @cached_property
    def refunds(self) -> list[dict[str, Any]]:
        return [p for p in self.payments if p["amount"] < 0 or p["mode"] == "REFUND"]

    @cached_property
    def loan_received(self) -> Decimal:
        from audit_core.uc03_p2_journey360 import _loan_received

        return _loan_received(self.connection, tenant_id=self.tenant_id, journey_id=self.journey_id)

    @cached_property
    def delivery_date(self) -> date | None:
        from audit_core.uc03_p2_journey360 import gate_pass_date

        printed = gate_pass_date(self.connection, tenant_id=self.tenant_id, journey_id=self.journey_id)
        if printed:
            return date.fromisoformat(printed[:10])
        row = self.connection.execute(
            text(
                """
                SELECT COALESCE(
                  (SELECT actual_delivered_at FROM auditcore.deliveries
                    WHERE tenant_id=:t AND journey_id=:j ORDER BY updated_at_utc DESC LIMIT 1),
                  (SELECT business_completed_at_utc FROM auditcore.journey_stage_states
                    WHERE tenant_id=:t AND journey_id=:j AND stage_code='DELIVERY'))
                """
            ),
            self._params,
        ).scalar_one_or_none()
        return _day(row)

    @cached_property
    def finance(self) -> dict[str, Any] | None:
        rows = self._rows(
            """
            SELECT provider_name, do_reference, financed_amount, loan_disbursement_amount
            FROM auditcore.finance_records WHERE tenant_id=:t AND journey_id=:j
            ORDER BY updated_at_utc DESC LIMIT 1
            """
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "provider": row["provider_name"] or "the financier",
            "reference": row["do_reference"],
            "financed": Decimal(row["financed_amount"] or 0),
            "disbursed": Decimal(row["loan_disbursement_amount"] or 0),
        }

    @cached_property
    def trade_in(self) -> dict[str, Any] | None:
        rows = self._rows(
            """
            SELECT old_vehicle_registration, quoted_value, actual_value, handover_at_utc, resale_at_utc, details
            FROM auditcore.trade_in_cases WHERE tenant_id=:t AND journey_id=:j
            ORDER BY updated_at_utc DESC LIMIT 1
            """
        )
        if rows:
            row = rows[0]
            details = dict(row["details"] or {})
            resale_value = next(
                (details[k] for k in ("resaleValue", "resale_value", "resalePrice", "resale_price", "saleValue")
                 if details.get(k) not in (None, "")), None,
            )
            return {
                "registration": row["old_vehicle_registration"] or "the exchange vehicle",
                "cost": _dec(row["actual_value"]) or _dec(row["quoted_value"]),
                "handover": _day(row["handover_at_utc"]),
                "resale": _day(row["resale_at_utc"]),
                "resaleValue": _dec(resale_value),
            }
        valuation = self.documents("valuation_report")
        booking = self.documents("booking_form", "booking_docket")
        exchange = str((booking[0]["fields"] if booking else {}).get("exchange_applicable") or "").strip().upper()
        if valuation or exchange in {"YES", "Y", "TRUE", "APPLICABLE"}:
            fields = valuation[0]["fields"] if valuation else {}
            return {
                "registration": fields.get("registration_number") or "the exchange vehicle",
                "cost": _dec(fields.get("final_offer_value")),
                "handover": None, "resale": None, "resaleValue": None,
            }
        return None

    @cached_property
    def delivery_clock(self) -> dict[str, Any] | None:
        """The earliest date printed on an invoice, the insurance cover note or
        the gate pass: {"date", "document"}; None until one is read. A date
        before the programme's floor is a misreading the PC has been asked
        to fix (task #19, 2026-09-30): it never starts the clock."""
        from audit_core.uc03_p2_registry import get_registry

        floor = get_registry().extraction_rules.date_floor
        found: list[tuple[date, str]] = []
        for doc in self.documents("customer_invoice_dms", "tax_invoice_tally", "insurance_cover", "gate_pass"):
            fields = doc["fields"]
            for key in ("invoice_date", "issue_date", "policy_start_date", "delivery_date"):
                printed = _day(fields.get(key))
                if printed and not (floor and printed < floor):
                    found.append((printed, doc["documentType"]))
        if not found:
            return None
        printed, document_type = min(found)
        return {"date": printed, "document": document_type.replace("_", " ")}

    @cached_property
    def delivery_complete(self) -> bool:
        return self.connection.execute(
            text("SELECT delivery_completion_state='COMPLETE' FROM auditcore.p2_journey_runtime "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            self._params,
        ).scalar_one_or_none() or False

    @cached_property
    def delivery_pending(self) -> list[str]:
        """What still stands between this Journey and a completed Delivery,
        in the stage engine's own words: the documents missing, the vehicle
        proof, the PC tasks open."""
        from audit_core.uc03_p2_registry import get_registry
        from audit_core.uc03_p2_stage import condition_reasons, requirement_items

        pending: list[str] = []
        reasons = condition_reasons(self.connection, tenant_id=self.tenant_id, journey_id=self.journey_id)
        missing = [
            i["label"] for i in requirement_items(self.connection, get_registry(), tenant_id=self.tenant_id,
                                                  journey_id=self.journey_id, stage="DELIVERY", reasons=reasons)
            if i["required"] and not i["received"]
        ]
        if missing:
            pending.append("documents missing: " + ", ".join(missing))
        gates = self._rows(
            """
            SELECT gate_key, details FROM auditcore.p2_stage_gate_state
            WHERE tenant_id=:t AND journey_id=:j AND stage_code='DELIVERY' AND gate_status<>'PASS'
            ORDER BY gate_key
            """
        )
        for gate in gates:
            details = dict(gate["details"] or {})
            if gate["gate_key"] == "REQUIRED_DOCUMENTS":
                continue  # named above
            if gate["gate_key"] == "PC_TASKS_CLOSED":
                titles = self._rows(
                    """
                    SELECT title FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j
                      AND assigned_role_code='PC'
                      AND task_status IN ('READY','IN_PROGRESS','RETURNED','ACTION_COMPLETED','VERIFYING')
                    ORDER BY created_at_utc
                    """
                )
                if titles:
                    pending.append(f"{len(titles)} PC task(s) open: " + "; ".join(str(r["title"]) for r in titles))
                continue
            pending.append(str(details.get("action") or details.get("label") or gate["gate_key"]).rstrip("."))
        return pending

    @cached_property
    def booking_date(self) -> date | None:
        booking = self.documents("booking_form", "booking_docket")
        printed = _day((booking[0]["fields"] if booking else {}).get("booking_date"))
        if printed:
            return printed
        return _day(self.connection.execute(
            text("SELECT booking_confirmation_date FROM auditcore.bookings WHERE tenant_id=:t AND journey_id=:j"),
            self._params,
        ).scalar_one_or_none())

    @cached_property
    def customer_name(self) -> str | None:
        row = self.connection.execute(
            text(
                """
                SELECT c.legal_name FROM auditcore.journeys j
                JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
                WHERE j.tenant_id=:t AND j.journey_id=:j AND c.legal_name_status='VERIFIED'
                """
            ),
            self._params,
        ).scalar_one_or_none()
        return " ".join(str(row).split()) if row else None

    def documents(self, *types: str) -> list[dict[str, Any]]:
        from audit_core.uc03_p2_journey360 import _document_facts

        return _document_facts(self.connection, tenant_id=self.tenant_id, journey_id=self.journey_id, di_types=types)

    def answer(self, code: str) -> dict[str, Any] | None:
        """The PC's latest answer to a question control's task, or None."""
        row = self.connection.execute(
            text("SELECT completion_result FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k"),
            {"t": self.tenant_id, "k": f"control:{self.journey_id}:{code}"},
        ).scalar_one_or_none()
        result = dict(row or {})
        value = str((result.get("details") or {}).get("answer") or "").upper()
        submitted = result.get("submittedAt")
        if not value or not submitted:
            return None
        return {"value": value, "at": datetime.fromisoformat(str(submitted)), "comment": result.get("comment")}


# ------------------------------------------------------- bucket 1: the deal


def _standards_known(facts: _Facts) -> bool:
    return any(c["standard"] is not None for g in facts.sheet["categories"] for c in g["components"])


def _charged(row: dict[str, Any] | None) -> Decimal | None:
    """What the customer is charged for a component: the invoice, else the
    booking form, else the reconciled actual."""
    if row is None:
        return None
    return next((_dec(row[c]) for c in ("billed", "booking", "effective") if row[c] is not None), None)


_NO_STANDARD = "No standard price on record yet: the model must be resolved against a published price list first."


def deal_undercharged(facts: _Facts) -> RuleOutcome:
    code = "DEAL_UNDERCHARGED"
    if not _standards_known(facts):
        return RuleOutcome(code, "SKIPPED", _NO_STANDARD)
    short: list[dict[str, Any]] = []
    for group in facts.sheet["categories"]:
        for row in group["components"]:
            standard = _dec(row["standard"])
            charged = _charged(row)
            if standard is None or charged is None:
                continue
            gap = standard - charged
            if gap > _LINE_TOLERANCE:
                short.append({"key": row["key"], "label": row["label"], "standard": str(standard),
                              "charged": str(charged), "short": str(gap),
                              "source": "invoice" if row["billed"] is not None else "booking form"})
    net = facts.sheet["summary"]["net"]
    net_standard, net_current = _dec(net["standard"]), _dec(net["current"])
    net_gap = net_standard - net_current if net_standard is not None and net_current is not None else None
    if not short and (net_gap is None or net_gap <= _TOLERANCE):
        return RuleOutcome(code, "PASS", "Every price component is charged at or above its standard price.")
    parts = [
        f"{s['label']}: {_rupees(s['charged'])} on the {s['source']} against the standard {_rupees(s['standard'])} "
        f"({_rupees(s['short'])} short)"
        for s in short
    ]
    if net_gap is not None and net_gap > _TOLERANCE:
        parts.append(f"Net deal {_rupees(net_current)} against the standard {_rupees(net_standard)} "
                     f"({_rupees(net_gap)} short)")
    return RuleOutcome(code, "FAIL", "; ".join(parts) + ".", {
        "findingTitle": "Deal charged below the standard price",
        "components": short,
        "netStandard": str(net_standard) if net_standard is not None else None,
        "netCurrent": str(net_current) if net_current is not None else None,
        "netShort": str(net_gap) if net_gap is not None else None,
    })


def excess_discount(facts: _Facts) -> RuleOutcome:
    code = "EXCESS_DISCOUNT"
    # A line no document gives anything on (the Management Referral line
    # is always on the sheet, opted out by default) is no discount yet.
    rows = [r for r in facts.sheet["discounts"] if any(r[c] is not None for c in ("billed", "booking", "effective"))]
    if not rows:
        return RuleOutcome(code, "PASS", "No discount on this deal.")
    if not _standards_known(facts):
        return RuleOutcome(code, "SKIPPED", _NO_STANDARD)
    over: list[dict[str, Any]] = []
    total_given = total_entitled = Decimal(0)
    for row in rows:
        given = next((_dec(row[c]) for c in ("billed", "booking", "effective") if row[c] is not None), None)
        if given is None:
            continue
        entitled = _dec(row["entitled"]) or Decimal(0)
        total_given += given
        total_entitled += entitled
        if given - entitled > _LINE_TOLERANCE:
            over.append({"key": row["key"], "label": row["label"], "entitled": str(entitled),
                         "given": str(given), "extra": str(given - entitled)})
    extra_total = total_given - total_entitled
    if not over and extra_total <= _TOLERANCE:
        return RuleOutcome(code, "PASS", f"Discounts of {_rupees(total_given)} given within the entitlement "
                                         f"of {_rupees(total_entitled)}.")
    parts = [
        (f"{o['label']}: {_rupees(o['given'])} given with no entitlement on record"
         if Decimal(o["entitled"]) == 0
         else f"{o['label']}: {_rupees(o['given'])} given against an entitlement of {_rupees(o['entitled'])} "
              f"({_rupees(o['extra'])} extra)")
        for o in over
    ]
    parts.append(f"Total discount {_rupees(total_given)} against {_rupees(total_entitled)} entitled"
                 + (f" ({_rupees(extra_total)} extra)" if extra_total > 0 else ""))
    return RuleOutcome(code, "FAIL", "; ".join(parts) + ".", {
        "findingTitle": "Discount given beyond the entitlement",
        "discounts": over, "totalGiven": str(total_given), "totalEntitled": str(total_entitled),
        "extra": str(extra_total),
    })


def cash_above_limit(facts: _Facts) -> RuleOutcome:
    code = "CASH_ABOVE_LIMIT"
    over = [r for r in _cash(facts) if r["amount"] > _CASH_RECEIPT_LIMIT]
    if not over:
        return RuleOutcome(code, "PASS", f"No cash receipt above {_rupees(_CASH_RECEIPT_LIMIT)}.")
    return RuleOutcome(code, "FAIL", (
        f"{_cash_lines(over)}: above the {_rupees(_CASH_RECEIPT_LIMIT)} limit for a single cash receipt."
    ), {"findingTitle": "Cash receipt above the statutory limit", "paymentIds": [r["paymentId"] for r in over]})


def payment_before_booking(facts: _Facts) -> RuleOutcome:
    code = "PAYMENT_BEFORE_BOOKING"
    if not facts.receipts:
        return RuleOutcome(code, "PASS", "No payment on this deal yet.")
    booked = facts.booking_date
    if booked is None:
        return RuleOutcome(code, "SKIPPED", "The booking date has not been read yet.")
    early = [r for r in facts.receipts if r["date"] and r["date"] < booked]
    if not early:
        return RuleOutcome(code, "PASS", f"Every payment is dated on or after the booking on {_when(booked)}.")
    lines = "; ".join(f"{_rupees(r['amount'])} on {_when(r['date'])}"
                      + (f" (receipt {r['receiptNumber']})" if r["receiptNumber"] else "") for r in early)
    return RuleOutcome(code, "FAIL", f"{lines}: dated before the booking on {_when(booked)}.",
                       {"findingTitle": "Payment dated before the booking", "paymentIds": [r["paymentId"] for r in early]})


def tcs_short(facts: _Facts) -> RuleOutcome:
    code = "TCS_SHORT"
    rows = {c["key"]: c for g in facts.sheet["categories"] for c in g["components"]}
    price = _charged(rows.get("ex_showroom_price"))
    if price is None:
        return RuleOutcome(code, "SKIPPED", "The ex-showroom price has not been read yet.")
    if price <= _TCS_THRESHOLD:
        return RuleOutcome(code, "PASS", f"Ex-showroom price {_rupees(price)} is within the TCS threshold of "
                                         f"{_rupees(_TCS_THRESHOLD)}.")
    expected = (price * _TCS_RATE_PERCENT / 100).quantize(Decimal("0.01"))
    charged = _charged(rows.get("tcs_amount")) or Decimal(0)
    if expected - charged > _LINE_TOLERANCE:
        return RuleOutcome(code, "FAIL", (
            f"TCS {_rupees(charged)} charged against {_rupees(expected)} due ({_TCS_RATE_PERCENT}% of the "
            f"ex-showroom price {_rupees(price)}; {_rupees(expected - charged)} short)."
        ), {"findingTitle": "TCS undercharged", "expected": str(expected), "observed": str(charged)})
    return RuleOutcome(code, "PASS", f"TCS {_rupees(charged)} charged against {_rupees(expected)} due.")


# ----------------------------------------------- bucket 2: after delivery


_NO_DELIVERY_DATE = "The delivery date is not known yet (no gate pass or delivery record)."


def _at_delivery(facts: _Facts) -> dict[str, Any] | RuleOutcome:
    """What was payable and what was covered when the vehicle left."""
    delivered = facts.delivery_date
    if delivered is None:
        return RuleOutcome("", "SKIPPED", _NO_DELIVERY_DATE)
    payable = _dec(facts.sheet["summary"]["payable"])
    if payable is None:
        return RuleOutcome("", "SKIPPED", "The amount payable is not known yet (no booking form or invoice read).")
    received = sum((r["amount"] for r in facts.receipts if r["date"] is None or r["date"] <= delivered), Decimal(0))
    financed = facts.finance["financed"] if facts.finance else Decimal(0)
    committed = max(financed, facts.loan_received)
    covered = received + committed
    return {"delivered": delivered, "payable": payable, "received": received, "committed": committed,
            "covered": covered, "short": payable - covered}


def _received_in_all(facts: _Facts) -> Decimal:
    return sum((r["amount"] for r in facts.receipts), Decimal(0)) + (
        max(facts.finance["financed"], facts.loan_received) if facts.finance else facts.loan_received
    )


def delivered_on_short_payment(facts: _Facts) -> RuleOutcome:
    code = "DELIVERED_ON_SHORT_PAYMENT"
    state = _at_delivery(facts)
    if isinstance(state, RuleOutcome):
        return RuleOutcome(code, state.outcome, state.reason)
    if state["short"] > _TOLERANCE:
        committed = f" plus {_rupees(state['committed'])} committed by the financier" if state["committed"] else ""
        return RuleOutcome(code, "FAIL", (
            f"Delivered on {_when(state['delivered'])} with {_rupees(state['received'])} received{committed} "
            f"against {_rupees(state['payable'])} payable ({_rupees(state['short'])} short)."
        ), {"findingTitle": "Vehicle delivered on short payment",
            "deliveredOn": state["delivered"].isoformat(), "payable": str(state["payable"]),
            "receivedByDelivery": str(state["received"]), "financed": str(state["committed"]),
            "short": str(state["short"])})
    return RuleOutcome(code, "PASS", f"Paid by delivery: {_rupees(state['covered'])} against "
                                     f"{_rupees(state['payable'])} payable.")


def _late_receipts(facts: _Facts, start: date, end: date | None) -> list[dict[str, Any]]:
    return [r for r in facts.receipts if r["date"] and r["date"] > start and (end is None or r["date"] <= end)]


def _receipt_lines(rows: list[dict[str, Any]], delivered: date) -> str:
    return ", ".join(
        f"{_rupees(r['amount'])} on {_when(r['date'])} ({(r['date'] - delivered).days} days after delivery"
        + (f", receipt {r['receiptNumber']}" if r["receiptNumber"] else "") + ")"
        for r in rows
    )


def payment_after_delivery_within_grace(facts: _Facts) -> RuleOutcome:
    code = "PAYMENT_AFTER_DELIVERY_WITHIN_GRACE"
    state = _at_delivery(facts)
    if isinstance(state, RuleOutcome):
        return RuleOutcome(code, state.outcome, state.reason)
    if state["short"] <= _TOLERANCE:
        return RuleOutcome(code, "PASS", "Nothing was outstanding at delivery.")
    late = _late_receipts(facts, state["delivered"], state["delivered"] + timedelta(days=_SETTLEMENT_DAYS))
    if not late:
        return RuleOutcome(code, "PASS", f"No payment received in the {_SETTLEMENT_DAYS} days after delivery.")
    return RuleOutcome(code, "FAIL", (
        f"{_rupees(state['short'])} was outstanding at delivery on {_when(state['delivered'])}; received afterwards: "
        f"{_receipt_lines(late, state['delivered'])}."
    ), {"findingTitle": f"Payment received within {_SETTLEMENT_DAYS} days after delivery",
        "paymentIds": [r["paymentId"] for r in late], "shortAtDelivery": str(state["short"])})


def payment_after_delivery_beyond_grace(facts: _Facts) -> RuleOutcome:
    code = "PAYMENT_AFTER_DELIVERY_BEYOND_GRACE"
    state = _at_delivery(facts)
    if isinstance(state, RuleOutcome):
        return RuleOutcome(code, state.outcome, state.reason)
    if state["short"] <= _TOLERANCE:
        return RuleOutcome(code, "PASS", "Nothing was outstanding at delivery.")
    deadline = state["delivered"] + timedelta(days=_SETTLEMENT_DAYS)
    late = _late_receipts(facts, deadline, None)
    title = f"Payment received more than {_SETTLEMENT_DAYS} days after delivery"
    if late:
        return RuleOutcome(code, "FAIL", (
            f"{_rupees(state['short'])} was outstanding at delivery on {_when(state['delivered'])}; received "
            f"{_receipt_lines(late, state['delivered'])}."
        ), {"findingTitle": title, "paymentIds": [r["paymentId"] for r in late],
            "shortAtDelivery": str(state["short"])})
    outstanding = state["payable"] - _received_in_all(facts)
    if outstanding <= _TOLERANCE:
        return RuleOutcome(code, "PASS", f"Settled within {_SETTLEMENT_DAYS} days of delivery.")
    if facts.today <= deadline:
        return RuleOutcome(code, "SKIPPED", f"{_rupees(outstanding)} is still outstanding; checked again after "
                                            f"{_when(deadline)}.")
    return RuleOutcome(code, "FAIL", (
        f"{_rupees(outstanding)} is still outstanding {(facts.today - state['delivered']).days} days after "
        f"delivery on {_when(state['delivered'])}."
    ), {"findingTitle": f"Balance outstanding more than {_SETTLEMENT_DAYS} days after delivery",
        "outstanding": str(outstanding), "deliveredOn": state["delivered"].isoformat()})


def do_payment_not_received(facts: _Facts) -> RuleOutcome:
    code = "DO_PAYMENT_NOT_RECEIVED"
    finance = facts.finance
    if finance is None or finance["financed"] <= 0:
        return RuleOutcome(code, "PASS", "The deal is not financed.")
    if finance["disbursed"] > 0:
        return RuleOutcome(code, "PASS", f"Loan of {_rupees(finance['disbursed'])} received from {finance['provider']}.")
    delivered = facts.delivery_date
    if delivered is None:
        return RuleOutcome(code, "SKIPPED", _NO_DELIVERY_DATE)
    deadline = delivered + timedelta(days=_FINANCE_DAYS)
    if facts.today <= deadline:
        return RuleOutcome(code, "SKIPPED", f"Waiting for the loan disbursement from {finance['provider']}: "
                                            f"due by {_when(deadline)}.")
    return RuleOutcome(code, "FAIL", (
        f"No loan disbursement from {finance['provider']} recorded {(facts.today - delivered).days} days after "
        f"delivery on {_when(delivered)}; the sanction was {_rupees(finance['financed'])}."
    ), {"findingTitle": f"Financier payment not received within {_FINANCE_DAYS} days of delivery",
        "financed": str(finance["financed"]), "deliveredOn": delivered.isoformat()})


def do_short_payment(facts: _Facts) -> RuleOutcome:
    code = "DO_SHORT_PAYMENT"
    finance = facts.finance
    if finance is None or finance["financed"] <= 0:
        return RuleOutcome(code, "PASS", "The deal is not financed.")
    if finance["disbursed"] <= 0:
        return RuleOutcome(code, "SKIPPED", f"The loan from {finance['provider']} has not been received yet.")
    short = finance["financed"] - finance["disbursed"]
    if short > _TOLERANCE:
        return RuleOutcome(code, "FAIL", (
            f"{finance['provider']} disbursed {_rupees(finance['disbursed'])} against a sanction of "
            f"{_rupees(finance['financed'])} ({_rupees(short)} short)."
        ), {"findingTitle": "Financier paid less than the delivery order",
            "financed": str(finance["financed"]), "disbursed": str(finance["disbursed"]), "short": str(short)})
    return RuleOutcome(code, "PASS", f"{finance['provider']} disbursed {_rupees(finance['disbursed'])} against "
                                     f"a sanction of {_rupees(finance['financed'])}.")


def trade_in_not_resold(facts: _Facts) -> RuleOutcome:
    code = "TRADE_IN_NOT_RESOLD"
    trade_in = facts.trade_in
    if trade_in is None:
        return RuleOutcome(code, "PASS", "No exchange vehicle on this deal.")
    if trade_in["resale"]:
        return RuleOutcome(code, "PASS", f"{trade_in['registration']} was resold on {_when(trade_in['resale'])}.")
    start = trade_in["handover"] or facts.delivery_date
    if start is None:
        return RuleOutcome(code, "SKIPPED", "The exchange vehicle's handover date is not known yet.")
    deadline = start + timedelta(days=_TRADE_IN_RESALE_DAYS)
    if facts.today <= deadline:
        return RuleOutcome(code, "SKIPPED", f"{trade_in['registration']} taken over on {_when(start)}; "
                                            f"resale due by {_when(deadline)}.")
    return RuleOutcome(code, "FAIL", (
        f"{trade_in['registration']} taken over on {_when(start)} has not been resold for "
        f"{(facts.today - start).days} days."
    ), {"findingTitle": f"Exchange vehicle not resold within {_TRADE_IN_RESALE_DAYS} days",
        "takenOverOn": start.isoformat()})


def trade_in_sold_at_loss(facts: _Facts) -> RuleOutcome:
    code = "TRADE_IN_SOLD_AT_LOSS"
    trade_in = facts.trade_in
    if trade_in is None:
        return RuleOutcome(code, "PASS", "No exchange vehicle on this deal.")
    if not trade_in["resale"]:
        return RuleOutcome(code, "SKIPPED", f"{trade_in['registration']} has not been resold yet.")
    if trade_in["resaleValue"] is None:
        return RuleOutcome(code, "SKIPPED", "The resale price is not recorded on the exchange case.")
    if trade_in["cost"] is None:
        return RuleOutcome(code, "SKIPPED", "The value paid for the exchange vehicle is not known.")
    loss = trade_in["cost"] - trade_in["resaleValue"]
    if loss > _TOLERANCE:
        return RuleOutcome(code, "FAIL", (
            f"{trade_in['registration']} was resold for {_rupees(trade_in['resaleValue'])} against "
            f"{_rupees(trade_in['cost'])} allowed to the customer ({_rupees(loss)} loss)."
        ), {"findingTitle": "Exchange vehicle sold at a loss", "cost": str(trade_in["cost"]),
            "resaleValue": str(trade_in["resaleValue"]), "loss": str(loss)})
    return RuleOutcome(code, "PASS", f"{trade_in['registration']} resold for {_rupees(trade_in['resaleValue'])} "
                                     f"against {_rupees(trade_in['cost'])} allowed.")


def post_delivery_refund(facts: _Facts) -> RuleOutcome:
    code = "POST_DELIVERY_REFUND"
    delivered = facts.delivery_date
    if delivered is None:
        return RuleOutcome(code, "SKIPPED", _NO_DELIVERY_DATE)
    items = [
        f"refund of {_rupees(abs(r['amount']))} on {_when(r['date'])}"
        for r in facts.refunds if r["date"] is None or r["date"] > delivered
    ]
    for doc in facts.documents("credit_note"):
        noted = _day(doc["fields"].get("invoice_date"))
        if noted is None or noted > delivered:
            amount = _dec(doc["fields"].get("grand_total_amount"))
            items.append(f"credit note {doc['fields'].get('invoice_number') or ''} of "
                         f"{_rupees(amount) if amount is not None else 'an unread amount'} on {_when(noted)}".replace("  ", " "))
    if not items:
        return RuleOutcome(code, "PASS", "No refund or credit note after delivery.")
    return RuleOutcome(code, "FAIL", f"Money returned after delivery on {_when(delivered)}: {'; '.join(items)}.",
                       {"findingTitle": "Refund after delivery", "deliveredOn": delivered.isoformat()})


# --------------------------------------------- bucket 3: PC confirmations


def _question(facts: _Facts, code: str, *, subjects: list[datetime], none: str, title: str, question: str,
              answers: list[dict[str, Any]], details: dict[str, Any]) -> RuleOutcome:
    """FAIL (a task asking the PC) until the PC has answered since the newest
    subject appeared; PASS once answered."""
    if not subjects:
        return RuleOutcome(code, "PASS", none)
    latest = max(subjects)
    answer = facts.answer(code)
    if answer and answer["at"] >= latest:
        label = next((a["label"] for a in answers if a["value"] == answer["value"]), answer["value"])
        return RuleOutcome(code, "PASS", f"Answered on {_when(answer['at'])}: {label}{_remark(answer)}.")
    return RuleOutcome(code, "FAIL", question, {"findingTitle": title, "question": question, "answers": answers,
                                                 **details})


def _answered(facts: _Facts, code: str, subjects: list[datetime]) -> dict[str, Any] | None:
    if not subjects:
        return None
    answer = facts.answer(code)
    return answer if answer and answer["at"] >= max(subjects) else None


def _remark(answer: dict[str, Any]) -> str:
    """The observer's own words, when they left any."""
    comment = str(answer.get("comment") or "").strip()
    return f': "{comment}"' if comment else ""


def _third_party(facts: _Facts) -> list[dict[str, Any]]:
    name = facts.customer_name
    if not name:
        return []
    out = []
    for r in facts.receipts:
        payer = r["payer"]
        other = r["counterparty"] if r["counterparty"] and not any(ch.isdigit() or ch == "/" for ch in r["counterparty"]) else None
        if (payer and not same_person(name, payer)) or (other and not same_person(name, other)):
            out.append({**r, "by": payer if payer and not same_person(name, payer) else other})
    return out


def _third_party_lines(rows: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{_rupees(r['amount'])} paid by {r['by']}" + (f" (receipt {r['receiptNumber']})" if r["receiptNumber"] else "")
        for r in rows
    )


def third_party_payment_unconfirmed(facts: _Facts) -> RuleOutcome:
    code = "THIRD_PARTY_PAYMENT_UNCONFIRMED"
    if facts.receipts and not facts.customer_name:
        return RuleOutcome(code, "SKIPPED", "The customer is not named from KYC yet.")
    rows = _third_party(facts)
    return _question(
        facts, code, subjects=[r["createdAt"] for r in rows],
        none="Every payment came from the customer.",
        title="Confirm the third-party payer's declaration and KYC",
        question=(f"{_third_party_lines(rows)}, not by the customer {facts.customer_name}. "
                  "Is a third-party payer declaration and the payer's KYC on file?"),
        answers=[{"value": "YES", "label": "Yes, declaration and KYC on file"},
                 {"value": "NO", "label": "No, not on file", "requiresComment": True}],
        details={"paymentIds": [r["paymentId"] for r in rows]},
    )


def third_party_payment_undeclared(facts: _Facts) -> RuleOutcome:
    code = "THIRD_PARTY_PAYMENT_UNDECLARED"
    rows = _third_party(facts)
    answer = _answered(facts, "THIRD_PARTY_PAYMENT_UNCONFIRMED", [r["createdAt"] for r in rows])
    if answer and answer["value"] == "NO":
        return RuleOutcome(code, "FAIL", (
            f"{_third_party_lines(rows)}, not by the customer {facts.customer_name}, with no third-party "
            f"declaration or payer KYC on file (confirmed on {_when(answer['at'])}){_remark(answer)}."
        ), {"findingTitle": "Third-party payment without a declaration or KYC",
            "paymentIds": [r["paymentId"] for r in rows]})
    return RuleOutcome(code, "PASS", "No third-party payment without a declaration."
                       if rows else "Every payment came from the customer.")


def _cash(facts: _Facts) -> list[dict[str, Any]]:
    return [r for r in facts.receipts if r["mode"].startswith("CASH")]


def _cash_lines(rows: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{_rupees(r['amount'])} in cash on {_when(r['date'])}"
        + (f" (receipt {r['receiptNumber']})" if r["receiptNumber"] else "") for r in rows
    )


def cash_intimation_unconfirmed(facts: _Facts) -> RuleOutcome:
    rows = _cash(facts)
    return _question(
        facts, "CASH_INTIMATION_UNCONFIRMED", subjects=[r["createdAt"] for r in rows],
        none="No cash received on this deal.",
        title="Confirm the cash collection was intimated",
        question=f"{_cash_lines(rows)}. Was this cash collection intimated to the auditor before it was received?",
        answers=[{"value": "YES", "label": "Yes, it was intimated"},
                 {"value": "NO", "label": "No, it was not intimated", "requiresComment": True}],
        details={"paymentIds": [r["paymentId"] for r in rows]},
    )


def cash_not_intimated(facts: _Facts) -> RuleOutcome:
    code = "CASH_NOT_INTIMATED"
    rows = _cash(facts)
    answer = _answered(facts, "CASH_INTIMATION_UNCONFIRMED", [r["createdAt"] for r in rows])
    if answer and answer["value"] == "NO":
        return RuleOutcome(code, "FAIL", (
            f"{_cash_lines(rows)}, collected without intimation to the auditor "
            f"(confirmed on {_when(answer['at'])}){_remark(answer)}."
        ), {"findingTitle": "Cash collected without intimation", "paymentIds": [r["paymentId"] for r in rows]})
    return RuleOutcome(code, "PASS", "Cash collections were intimated." if rows else "No cash received on this deal.")


_NDC_ANSWERS = [
    {"value": "YES", "label": "Signed by the customer in my presence"},
    {"value": "NOT_WITNESSED", "label": "Signed, but not in my presence", "requiresComment": True},
    {"value": "NO", "label": "Not signed by the customer", "requiresComment": True},
]


def _ndc_unsigned_by_reading(docs: list[dict[str, Any]]) -> bool:
    """True when the document service read the NDC and found no customer signature."""
    for doc in docs:
        value = doc["fields"].get("customer_signature_present")
        if value is not None and str(value).strip().lower() in {"false", "no", "0", "absent"}:
            return True
    return False


def ndc_signature_unconfirmed(facts: _Facts) -> RuleOutcome:
    code = "NDC_SIGNATURE_UNCONFIRMED"
    docs = facts.documents("no_dues_certificate")
    if not docs:
        return RuleOutcome(code, "SKIPPED", "The No Dues Certificate has not been uploaded yet.")
    return _question(
        facts, code, subjects=[d["linkedAtUtc"] for d in docs], none="",
        title="Confirm the No Dues Certificate was signed in your presence",
        question="Did the customer sign the No Dues Certificate, and was it signed in your presence?",
        answers=_NDC_ANSWERS, details={"documentIds": [d["documentId"] for d in docs]},
    )


def ndc_not_signed(facts: _Facts) -> RuleOutcome:
    code = "NDC_NOT_SIGNED"
    docs = facts.documents("no_dues_certificate")
    if not docs:
        return RuleOutcome(code, "SKIPPED", "The No Dues Certificate has not been uploaded yet.")
    details = {"documentIds": [d["documentId"] for d in docs]}
    if _ndc_unsigned_by_reading(docs):
        return RuleOutcome(code, "FAIL", "The No Dues Certificate carries no customer signature (read from the "
                                         "document).", {"findingTitle": "No Dues Certificate not signed", **details})
    answer = _answered(facts, "NDC_SIGNATURE_UNCONFIRMED", [d["linkedAtUtc"] for d in docs])
    if answer and answer["value"] == "NO":
        return RuleOutcome(code, "FAIL", f"The No Dues Certificate was not signed by the customer (confirmed on "
                                         f"{_when(answer['at'])}){_remark(answer)}.",
                           {"findingTitle": "No Dues Certificate not signed", **details})
    if answer and answer["value"] == "NOT_WITNESSED":
        return RuleOutcome(code, "FAIL", f"The No Dues Certificate was signed, but not in the auditor's presence "
                                         f"(confirmed on {_when(answer['at'])}){_remark(answer)}.",
                           {"findingTitle": "No Dues Certificate not signed in the auditor's presence", **details})
    if answer is None:
        return RuleOutcome(code, "SKIPPED", "Waiting for the PC to confirm how the No Dues Certificate was signed.")
    return RuleOutcome(code, "PASS", f"Signed by the customer in the auditor's presence (confirmed on {_when(answer['at'])}){_remark(answer)}.")


def delivery_not_completed_in_time(facts: _Facts) -> RuleOutcome:
    code = "DELIVERY_NOT_COMPLETED_IN_TIME"
    clock = facts.delivery_clock
    if clock is None:
        return RuleOutcome(code, "PASS", "No invoice, insurance cover note or gate pass read yet.")
    if facts.delivery_complete:
        return RuleOutcome(code, "PASS", f"Delivery completed within {_DELIVERY_COMPLETION_DAYS} days of the "
                                         f"{clock['document']} dated {_when(clock['date'])}.")
    deadline = clock["date"] + timedelta(days=_DELIVERY_COMPLETION_DAYS)
    pending = facts.delivery_pending or ["the delivery is not marked complete"]
    summary = "; ".join(pending)
    if facts.today < deadline:
        return RuleOutcome(code, "SKIPPED", f"Delivery due by {_when(deadline)} ({_DELIVERY_COMPLETION_DAYS} days "
                                            f"from the {clock['document']} dated {_when(clock['date'])}). "
                                            f"Pending: {summary}.")
    return RuleOutcome(code, "FAIL", (
        f"Delivery not completed {(facts.today - clock['date']).days} days after the {clock['document']} dated "
        f"{_when(clock['date'])}. Pending: {summary}."
    ), {"findingTitle": "Delivery not completed in time: " + summary[:200], "pending": pending,
        "clockDocument": clock["document"], "clockDate": clock["date"].isoformat()})


def accessories_fitted_unconfirmed(facts: _Facts) -> RuleOutcome:
    code = "ACCESSORIES_FITTED_UNCONFIRMED"
    docs = facts.documents("gate_pass", "accessory_invoice_dms", "accessory_invoice_tally")
    if not docs:
        return RuleOutcome(code, "SKIPPED", "Waiting for the delivery documents (gate pass or accessory invoice).")
    return _question(
        facts, code, subjects=[d["linkedAtUtc"] for d in docs], none="",
        title="Confirm the accessories fitted on the car are billed",
        question="Is every accessory fitted on the delivered car billed on an accessory invoice (DMS or Tally)?",
        answers=[{"value": "YES", "label": "Yes, every fitted accessory is billed"},
                 {"value": "NO", "label": "No, an accessory is fitted but not billed", "requiresComment": True}],
        details={"documentIds": [d["documentId"] for d in docs]},
    )


def accessory_fitted_unbilled(facts: _Facts) -> RuleOutcome:
    code = "ACCESSORY_FITTED_UNBILLED"
    docs = facts.documents("gate_pass", "accessory_invoice_dms", "accessory_invoice_tally")
    if not docs:
        return RuleOutcome(code, "SKIPPED", "Waiting for the delivery documents (gate pass or accessory invoice).")
    answer = _answered(facts, "ACCESSORIES_FITTED_UNCONFIRMED", [d["linkedAtUtc"] for d in docs])
    if answer and answer["value"] == "NO":
        return RuleOutcome(code, "FAIL", f"An accessory fitted on the car is not billed (confirmed on "
                                         f"{_when(answer['at'])}){_remark(answer)}.",
                           {"findingTitle": "Accessory fitted but not billed",
                            "documentIds": [d["documentId"] for d in docs]})
    if answer is None:
        return RuleOutcome(code, "SKIPPED", "Waiting for the PC to confirm the accessories fitted are billed.")
    return RuleOutcome(code, "PASS", f"Every fitted accessory is billed (confirmed on {_when(answer['at'])}){_remark(answer)}.")


# ------------------------------------------------------------------ runner

# ------------------------------------------------------ insurance source


def _insurance_decided(insurance: dict[str, Any]) -> str:
    if insurance.get("decidedBy") == "PC":
        return f"confirmed by the PC on {_when(insurance.get('decidedAt'))}"
    return "assumed until confirmed"


def insurance_invoice_missing(facts: _Facts) -> RuleOutcome:
    """The vehicle is invoiced but nothing bills the insurance premium: the
    PC uploads the insurance invoice, or admits the customer arranged their
    own insurance (which keeps the premium out of the deal and flags the
    Team Lead). Decision 2026-09-30; the cover-note reading follows."""
    code = "INSURANCE_INVOICE_MISSING"
    insurance = facts.sheet["insurance"]
    if insurance["source"] == "SELF":
        return RuleOutcome(code, "PASS", f"Self insurance ({_insurance_decided(insurance)}): no dealer insurance invoice is due.")
    if insurance["invoiceOnFile"]:
        return RuleOutcome(code, "PASS", "The insurance premium is invoiced.")
    if not insurance["vehicleInvoiced"]:
        return RuleOutcome(code, "PASS", "The vehicle is not invoiced yet; the insurance invoice is due with it.")
    return RuleOutcome(code, "FAIL", (
        "The vehicle is invoiced but no invoice for the insurance premium is on file. Upload the insurance "
        "invoice (debit note), or confirm that the customer arranged their own insurance."
    ), {
        "findingTitle": "Insurance invoice missing",
        "question": "Is the insurance through the dealership? Upload its invoice; if the customer arranged their own, say so.",
        "answers": [{"value": "SELF", "label": "Self insurance: the customer arranged it", "requiresComment": True}],
        "uploadFirst": True, "documentTypes": ["debit_note"],
    })


def self_insurance_declared(facts: _Facts) -> RuleOutcome:
    code = "SELF_INSURANCE_DECLARED"
    insurance = facts.sheet["insurance"]
    if insurance["source"] == "SELF":
        return RuleOutcome(code, "FAIL", (
            f"The customer arranged their own insurance ({_insurance_decided(insurance)}); "
            "the premium is outside the dealer's deal."
        ), {"findingTitle": "Customer arranged own insurance"})
    return RuleOutcome(code, "PASS", "Insurance is through the dealership.")


# Journey-wide checks run with the Booking unit (always evaluated); checks
# that need a delivery run with the Delivery unit.
_BOOKING_RULES = (
    deal_undercharged, excess_discount, tcs_short,
    third_party_payment_unconfirmed, third_party_payment_undeclared,
    cash_intimation_unconfirmed, cash_not_intimated, cash_above_limit, payment_before_booking,
    insurance_invoice_missing, self_insurance_declared,
)
_DELIVERY_RULES = (
    delivered_on_short_payment, payment_after_delivery_within_grace, payment_after_delivery_beyond_grace,
    do_payment_not_received, do_short_payment,
    trade_in_not_resold, trade_in_sold_at_loss, post_delivery_refund,
    ndc_signature_unconfirmed, ndc_not_signed, accessories_fitted_unconfirmed, accessory_fitted_unbilled,
    delivery_not_completed_in_time,
)
RULE_CODES = {
    "BOOKING": tuple(r.__name__.upper() for r in _BOOKING_RULES),
    "DELIVERY": tuple(r.__name__.upper() for r in _DELIVERY_RULES),
}


# The Findings catalogue entry each check raises under.
_FINDING_TYPES = {
    "DEAL_UNDERCHARGED": "PRICING_ANOMALY", "TCS_SHORT": "PRICING_ANOMALY", "EXCESS_DISCOUNT": "DISCOUNT_ANOMALY",
    "TRADE_IN_NOT_RESOLD": "COMMERCIAL_EXCEPTION", "TRADE_IN_SOLD_AT_LOSS": "COMMERCIAL_EXCEPTION",
    "DELIVERY_NOT_COMPLETED_IN_TIME": "DELIVERY_EXCEPTION",
    "NDC_NOT_SIGNED": "PROCESS_NON_COMPLIANCE", "ACCESSORY_FITTED_UNBILLED": "PROCESS_NON_COMPLIANCE",
    "SELF_INSURANCE_DECLARED": "COMMERCIAL_EXCEPTION",
}
_DEFAULT_FINDING_TYPE = "PAYMENT_EXCEPTION"


def _sync_finding(connection: Connection, *, tenant_id: str, journey_id: UUID, stage: str, result: RuleOutcome,
                  correlation_id: str | None) -> UUID | None:
    """A failing violation check is one open Audit Finding (rule_key = the
    control code), refreshed while it fails and resolved once it passes; a
    Team Lead may also close it from Findings. PC questions are tasks, not
    findings."""
    from audit_core.uc03_delivery_commands import _set_stage_flag_status
    from audit_core.uc03_finding_classification import resolve_classification
    from audit_core.uc03_manual_verification import _resolve_finding
    from audit_core.uc03_p2_registry import get_registry

    control = get_registry().controls.get(result.code)
    existing = connection.execute(
        text(
            """
            SELECT audit_finding_id FROM auditcore.audit_findings
            WHERE tenant_id=:t AND journey_id=:j AND rule_key=:k AND finding_status IN ('OPEN','ACKNOWLEDGED')
            ORDER BY created_at_utc DESC LIMIT 1
            """
        ),
        {"t": tenant_id, "j": journey_id, "k": result.code},
    ).scalar_one_or_none()
    failing = result.outcome == "FAIL" and control is not None and control.finding_class == "VIOLATION"
    if not failing:
        if existing is not None and result.outcome in {"PASS", "SKIPPED"}:
            _resolve_finding(connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage,
                             finding_id=UUID(str(existing)), actor_id=None, correlation_id=correlation_id or "",
                             note=result.reason or "The check passes now.")
        return None
    title = str(result.details.get("findingTitle") or result.code.replace("_", " ").capitalize())[:300]
    severity = str(control.severity or "MEDIUM")
    if existing is None:
        verdict = connection.execute(
            text(
                """
                SELECT disposition FROM auditcore.audit_findings
                WHERE tenant_id=:t AND journey_id=:j AND rule_key=:k AND finding_status='RESOLVED'
                ORDER BY resolved_at_utc DESC NULLS LAST, created_at_utc DESC LIMIT 1
                """
            ),
            {"t": tenant_id, "j": journey_id, "k": result.code},
        ).scalar_one_or_none()
        if verdict in {"CONFIRMED_BREACH", "FALSE_POSITIVE"}:
            # A Team Lead already gave the verdict on this check for this
            # Journey; it is not raised again (Findings can reopen it).
            return None
    if existing is not None:
        connection.execute(
            text("UPDATE auditcore.audit_findings SET title=:title, description=:description, severity=:severity "
                 "WHERE tenant_id=:t AND audit_finding_id=:f"),
            {"t": tenant_id, "f": existing, "title": title, "description": result.reason, "severity": severity},
        )
        return UUID(str(existing))
    routing = resolve_classification(connection, tenant_id=tenant_id, journey_id=journey_id, rule_key=result.code,
                                     finding_type_code=_FINDING_TYPES.get(result.code, _DEFAULT_FINDING_TYPE),
                                     severity=severity)
    finding_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_findings (
                tenant_id, journey_id, finding_type_code, severity, finding_status, title, description,
                created_by_actor_id, correlation_id, stage_code, origin_kind, origin_actor_id, origin_role_snapshot,
                rule_key, blocking_completion, finding_class, owner_role_code, sla_due_at_utc
            ) VALUES (
                :t, :j, :finding_type, :severity, 'OPEN', :title, :description,
                NULL, :correlation_id, :stage, 'MACHINE', NULL, 'SYSTEM',
                :rule_key, FALSE, :finding_class, :owner_role_code, :sla_due_at_utc
            ) RETURNING audit_finding_id
            """
        ),
        {"t": tenant_id, "j": journey_id, "finding_type": _FINDING_TYPES.get(result.code, _DEFAULT_FINDING_TYPE),
         "severity": severity, "title": title, "description": result.reason, "correlation_id": correlation_id,
         "stage": stage, "rule_key": result.code, **routing},
    ).scalar_one()
    connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_finding_events (
                tenant_id, audit_finding_id, journey_id, stage_code, event_type, actor_id, actor_role_snapshot,
                safe_payload, correlation_id
            ) VALUES (:t, :f, :j, :stage, 'RAISED', NULL, 'SYSTEM', CAST(:payload AS jsonb), :correlation_id)
            """
        ),
        {"t": tenant_id, "f": finding_id, "j": journey_id, "stage": stage, "correlation_id": correlation_id,
         "payload": json.dumps({"originKind": "MACHINE", "ruleKey": result.code, **result.details}, default=str)},
    )
    _set_stage_flag_status(connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage)
    return UUID(str(finding_id))


def run_p2_audit_rules(
    connection: Connection, *, tenant_id: str, journey_id: UUID, stage: str, correlation_id: str | None,
    triggering_event: str,
) -> list[RuleOutcome]:
    """Run the stage's rules, log each outcome, return them. A rule that
    breaks reports ERROR (the unit retries) without stopping the others."""
    facts = _Facts(connection, tenant_id, journey_id)
    outcomes: list[RuleOutcome] = []
    for rule in {"BOOKING": _BOOKING_RULES, "DELIVERY": _DELIVERY_RULES}.get(stage, ()):
        code = rule.__name__.upper()
        try:
            result = rule(facts)
        except Exception:
            # A broken rule is our defect, not a finding: log it with its location, keep going.
            logger.exception("p2_audit_rule_failed", rule=code, tenant_id=tenant_id, journey_id=str(journey_id),
                         stage_code=stage, error_category="TECHNICAL")
            result = RuleOutcome(code, "ERROR", "The check could not run; it will be retried.")
        finding_id = _sync_finding(connection, tenant_id=tenant_id, journey_id=journey_id, stage=stage,
                                   result=result, correlation_id=correlation_id)
        record_execution(
            connection, tenant_id=tenant_id, journey_id=journey_id, rule_code=code,
            triggering_event=triggering_event, outcome=result.outcome, reason=result.reason,
            audit_finding_id=finding_id, correlation_id=correlation_id,
        )
        outcomes.append(result)
    return outcomes


def schedule_delivery_completion_check(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str | None = None,
) -> str | None:
    """The 7th-day event: as soon as an invoice, insurance cover note or gate
    pass is read, queue the Delivery checks for the morning after
    P2_DELIVERY_COMPLETION_DAYS from the earliest printed date, when
    DELIVERY_NOT_COMPLETED_IN_TIME raises its High task to the Team Lead
    with everything still pending. Moves earlier if an earlier-dated
    document arrives; never re-fires once spent."""
    facts = _Facts(connection, tenant_id, journey_id)
    clock = facts.delivery_clock
    if clock is None or facts.delivery_complete:
        return None
    key = f"unit:{journey_id}:NATIVE:DELIVERY:delivery-window"
    fire_at = datetime.combine(clock["date"] + timedelta(days=_DELIVERY_COMPLETION_DAYS), time(0, 30), tzinfo=UTC)
    existing = connection.execute(
        text("SELECT work_status, next_attempt_at_utc FROM auditcore.p2_work_queue "
             "WHERE tenant_id=:t AND work_type='CONTROL_EVALUATE' AND work_key=:k"),
        {"t": tenant_id, "k": key},
    ).mappings().one_or_none()
    if existing is not None and not (
        existing["work_status"] == "PENDING" and existing["next_attempt_at_utc"] is not None
        and existing["next_attempt_at_utc"] > fire_at
    ):
        return None
    enqueue_work(
        connection, tenant_id=tenant_id, journey_id=journey_id, work_type="CONTROL_EVALUATE", work_key=key,
        payload={"unit": "NATIVE:DELIVERY", "force": True,
                 "reason": f"{_DELIVERY_COMPLETION_DAYS} days from the {clock['document']} dated {clock['date']}"},
        correlation_id=correlation_id,
        delay_seconds=max(0, int((fire_at - datetime.now(UTC)).total_seconds())),
    )
    return key


def queue_nightly_review(connection: Connection, *, tenant_id: str, today: date | None = None) -> int:
    """Every night, re-run the Delivery checks for each Journey whose
    delivery has started and is not yet reviewed, so the time-based checks
    (settlement, financier, resale, documents overdue) fire without a
    document event. One work item per Journey per night; safe to call more
    than once. Returns how many were queued."""
    today = today or datetime.now(UTC).date()
    rows = connection.execute(
        text(
            """
            SELECT j.journey_id FROM auditcore.journeys j
            WHERE j.tenant_id=:t AND j.review_completed_at_utc IS NULL
              AND NOT EXISTS (SELECT 1 FROM auditcore.journey_stage_states s
                               WHERE s.tenant_id=j.tenant_id AND s.journey_id=j.journey_id AND s.stage_code='BOOKING'
                                 AND s.business_status IN ('BOOKING_CANCELLED','DUPLICATE_BOOKING'))
              AND (EXISTS (SELECT 1 FROM auditcore.evidence e
                            WHERE e.tenant_id=j.tenant_id AND e.journey_id=j.journey_id
                              AND e.association_status='ACTIVE' AND upper(COALESCE(e.process_area,''))='DELIVERY')
                   OR EXISTS (SELECT 1 FROM auditcore.p2_journey_runtime r
                               WHERE r.tenant_id=j.tenant_id AND r.journey_id=j.journey_id
                                 AND r.current_stage LIKE 'DELIVERY_%'))
              AND NOT EXISTS (SELECT 1 FROM auditcore.p2_work_queue w
                               WHERE w.tenant_id=j.tenant_id AND w.journey_id=j.journey_id
                                 AND w.work_type='CONTROL_EVALUATE' AND w.work_key=:key_prefix || j.journey_id::text)
            """
        ),
        {"t": tenant_id, "key_prefix": f"nightly:{today.isoformat()}:"},
    ).scalars().all()
    for journey_id in rows:
        enqueue_work(
            connection, tenant_id=tenant_id, journey_id=journey_id, work_type="CONTROL_EVALUATE",
            work_key=f"nightly:{today.isoformat()}:{journey_id}",
            payload={"unit": "NATIVE:DELIVERY", "force": True, "reason": f"nightly review {today.isoformat()}"},
            correlation_id=None,
        )
    return len(rows)


__all__ = ["RULE_CODES", "RuleOutcome", "queue_nightly_review", "run_p2_audit_rules",
           "schedule_delivery_completion_check"]
