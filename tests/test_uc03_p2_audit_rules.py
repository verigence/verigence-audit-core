"""The deal-audit checks (uc03_p2_audit_rules): price, discounts, settlement
after delivery, financier, trade-in, third-party payer, cash intimation and
the No Dues Certificate, on a real Postgres."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_evidence,
    add_ready_document,
    add_receipt_payment,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import uc03_p2_controls as controls
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_audit_rules import (
    queue_nightly_review,
    run_p2_audit_rules,
    schedule_delivery_completion_check,
)
from audit_core.uc03_p2_task_producer import apply_control_transitions
from audit_core.uc03_p2_tasks import submit_action

TODAY = datetime.now(UTC).date()


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine)
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql: str, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _run(journey, stage: str) -> dict:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return {
            r.code: r for r in run_p2_audit_rules(
                connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, stage=stage,
                correlation_id="test", triggering_event="TEST",
            )
        }


def _line(journey, key: str, standard: str, actual: str | None) -> None:
    _sql(journey, "INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, standard_amount, "
                  "actual_amount) VALUES (:t, :j, :k, :s, :a)", k=key, s=standard, a=actual)


def _source(journey, kind: str, key: str, document_type: str, amount: str) -> None:
    _sql(journey, "INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind, "
                  "component_key, source_document_type, amount, source_document_id) "
                  "VALUES (:t, :j, :k, :c, :d, :a, :doc)", k=kind, c=key, d=document_type, a=amount, doc=uuid4())


def _discount(journey, key: str, entitled: str, given: str) -> None:
    _sql(journey, "INSERT INTO auditcore.discount_applications (tenant_id, journey_id, discount_key, "
                  "standard_eligible_amount, actual_discount_amount, eligibility_result, actual_source_kind) "
                  "VALUES (:t, :j, :k, :e, :g, 'ELIGIBLE', 'EVIDENCE')", k=key, e=entitled, g=given)


def _receipt(journey, amount: str, on: date, number: str, *, mode: str | None = None, payer: str | None = None):
    document_id = add_receipt_payment(journey, amount=amount, receipt_number=number, receipt_date=on.isoformat())
    _sql(journey, "UPDATE auditcore.payments SET payment_mode_code=COALESCE(:m, payment_mode_code), "
                  "receipt_customer_name=:p WHERE tenant_id=:t AND source_di_document_id=:d",
         m=mode, p=payer, d=document_id)
    return document_id


def _name(journey, name: str) -> None:
    _sql(journey, "UPDATE auditcore.customers SET legal_name=:n, legal_name_status='VERIFIED' "
                  "WHERE tenant_id=:t AND customer_id=:c", n=name, c=journey.customer_id)


def _deliver(journey, on: date) -> None:
    add_ready_document(journey, "gate_pass", delivery_date=on.isoformat())


def _states(journey) -> dict:
    rows = _sql(journey, "SELECT control_code, control_status, status_reason FROM auditcore.p2_control_state "
                         "WHERE tenant_id=:t AND journey_id=:j").mappings().all()
    return {r["control_code"]: dict(r) for r in rows}


def _task(journey, code: str) -> dict | None:
    row = _sql(journey, "SELECT task_id, task_type, task_status, assigned_role_code, reference, round_number "
                        "FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k",
               k=f"control:{journey.journey_id}:{code}").mappings().one_or_none()
    return dict(row) if row else None


def _finding(journey, code: str) -> tuple:
    row = _sql(journey, "SELECT finding_status, severity, title FROM auditcore.audit_findings "
                        "WHERE tenant_id=:t AND journey_id=:j AND rule_key=:k ORDER BY created_at_utc DESC LIMIT 1",
               k=code).one_or_none()
    return tuple(row) if row else (None, None, None)


def _recompute(journey) -> None:
    from audit_core.uc03_p2_stage import recompute_journey_stage

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        recompute_journey_stage(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _evaluate(journey, unit: str) -> None:
    started = datetime.now(UTC)
    transitions = controls.evaluate_unit(journey.engine, tenant_id=journey.tenant_id,
                                         journey_id=journey.journey_id, unit=unit, force=True)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        apply_control_transitions(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                  transitions=transitions, evaluation_started_at=started)


def _answer(journey, code: str, answer: str, comment: str = "noted", *, role: str = "PC") -> None:
    task = _task(journey, code)
    assert task is not None
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        submit_action(connection, tenant_id=journey.tenant_id, task_id=task["task_id"], action="COMPLETE_ACTION",
                      actor_id=journey.actor_id if role == "PC" else f"{role.lower()}-1", actor_role_code=role,
                      comment=comment, details={"answer": answer})


# ------------------------------------------------------------ the deal


def test_deal_undercharged_and_excess_discount_name_each_line(journey):
    _line(journey, "ex_showroom_price", "1000000", "990000")
    _source(journey, "COMMERCIAL", "ex_showroom_price", "tax_invoice_tally", "990000")
    _line(journey, "tcs_amount", "10000", "10000")
    _source(journey, "COMMERCIAL", "tcs_amount", "tax_invoice_tally", "10000")
    _discount(journey, "CASH_DISCOUNT", "25000", "40000")
    _source(journey, "DISCOUNT", "CASH_DISCOUNT", "tax_invoice_tally", "40000")

    result = _run(journey, "BOOKING")
    under = result["DEAL_UNDERCHARGED"]
    assert under.outcome == "FAIL"
    assert "Ex-showroom price: ₹9,90,000 on the invoice against the standard ₹10,00,000 (₹10,000 short)" in under.reason
    assert "TCS" not in under.reason
    assert "Net deal ₹9,60,000 against the standard ₹9,85,000 (₹25,000 short)" in under.reason
    assert [c["key"] for c in under.details["components"]] == ["ex_showroom_price"]
    excess = result["EXCESS_DISCOUNT"]
    assert excess.outcome == "FAIL"
    assert "Consumer / cash discount: ₹40,000 given against an entitlement of ₹25,000 (₹15,000 extra)" in excess.reason
    # A failing check is one open Audit Finding in the check's own words.
    assert _finding(journey, "DEAL_UNDERCHARGED") == ("OPEN", "HIGH", "Deal charged below the standard price")
    assert _finding(journey, "EXCESS_DISCOUNT")[0] == "OPEN"

    _sql(journey, "UPDATE auditcore.commercial_line_source_values SET amount=1000000 WHERE tenant_id=:t "
                  "AND component_key='ex_showroom_price'")
    _sql(journey, "UPDATE auditcore.commercial_line_source_values SET amount=25000 WHERE tenant_id=:t "
                  "AND component_key='CASH_DISCOUNT'")
    result = _run(journey, "BOOKING")
    assert result["DEAL_UNDERCHARGED"].outcome == "PASS"
    assert result["EXCESS_DISCOUNT"].outcome == "PASS"
    assert _finding(journey, "DEAL_UNDERCHARGED")[0] == "RESOLVED"


def test_deal_checks_wait_for_the_standard_prices(journey):
    _source(journey, "COMMERCIAL", "ex_showroom_price", "booking_form", "900000")
    result = _run(journey, "BOOKING")
    assert result["DEAL_UNDERCHARGED"].outcome == "SKIPPED"
    assert "price list" in result["DEAL_UNDERCHARGED"].reason


# ---------------------------------------------- cash intimation question


def test_cash_intimation_is_asked_answered_and_asked_again(journey):
    _receipt(journey, "50000", TODAY - timedelta(days=3), "R-1", mode="CASH")
    _evaluate(journey, "NATIVE:BOOKING")
    assert _states(journey)["CASH_INTIMATION_UNCONFIRMED"]["control_status"] == "FAIL"
    question = _task(journey, "CASH_INTIMATION_UNCONFIRMED")
    assert question["task_type"] == "PC_CONFIRMATION" and question["assigned_role_code"] == "PC"
    assert [a["value"] for a in question["reference"]["answers"]] == ["YES", "NO"]
    assert "₹50,000 in cash" in question["reference"]["question"]
    assert _states(journey)["CASH_NOT_INTIMATED"]["control_status"] == "PASS"

    _answer(journey, "CASH_INTIMATION_UNCONFIRMED", "NO", "Collected at the customer's home")
    _evaluate(journey, "NATIVE:BOOKING")
    assert _task(journey, "CASH_INTIMATION_UNCONFIRMED")["task_status"] == "VERIFIED_COMPLETE"
    state = _states(journey)
    assert state["CASH_INTIMATION_UNCONFIRMED"]["control_status"] == "PASS"
    assert state["CASH_NOT_INTIMATED"]["control_status"] == "FAIL"
    assert "without intimation" in state["CASH_NOT_INTIMATED"]["status_reason"]
    assert "Collected at the customer's home" in state["CASH_NOT_INTIMATED"]["status_reason"]
    violation = _task(journey, "CASH_NOT_INTIMATED")
    assert violation["task_type"] == "FINDING_REVIEW" and violation["assigned_role_code"] == "TL"

    # Another cash receipt: the question is asked again.
    _receipt(journey, "20000", TODAY - timedelta(days=1), "R-2", mode="CASH")
    _evaluate(journey, "NATIVE:BOOKING")
    reopened = _task(journey, "CASH_INTIMATION_UNCONFIRMED")
    assert reopened["task_status"] == "READY" and reopened["round_number"] == 2
    assert _states(journey)["CASH_NOT_INTIMATED"]["control_status"] == "PASS"


# ------------------------------------------------------ third-party payer


def test_third_party_payer_is_flagged_only_against_the_verified_name(journey):
    _receipt(journey, "100000", TODAY - timedelta(days=2), "R-1", payer="SURESH KUMAR")
    assert _run(journey, "BOOKING")["THIRD_PARTY_PAYMENT_UNCONFIRMED"].outcome == "SKIPPED"
    _name(journey, "RAVI KUMAR")
    asked = _run(journey, "BOOKING")["THIRD_PARTY_PAYMENT_UNCONFIRMED"]
    assert asked.outcome == "FAIL"
    assert "₹1,00,000 paid by SURESH KUMAR (receipt R-1), not by the customer RAVI KUMAR" in asked.reason
    assert asked.details["answers"][1]["value"] == "NO"


def test_a_payment_by_the_customer_is_not_third_party(journey):
    _name(journey, "RAVI KUMAR")
    _receipt(journey, "100000", TODAY - timedelta(days=2), "R-1", payer="Ravi  Kumar")
    result = _run(journey, "BOOKING")
    assert result["THIRD_PARTY_PAYMENT_UNCONFIRMED"].outcome == "PASS"
    assert result["THIRD_PARTY_PAYMENT_UNDECLARED"].outcome == "PASS"


# ---------------------------------------------------- settlement windows


def test_short_payment_at_delivery_and_the_seven_day_windows(journey):
    delivered = TODAY - timedelta(days=20)
    _line(journey, "ex_showroom_price", "1000000", "1000000")
    _deliver(journey, delivered)
    _receipt(journey, "600000", delivered - timedelta(days=2), "R-1")
    _receipt(journey, "300000", delivered + timedelta(days=3), "R-2")
    _receipt(journey, "50000", delivered + timedelta(days=10), "R-3")
    refund = _receipt(journey, "20000", delivered + timedelta(days=5), "CR-1", mode="REFUND")
    assert refund

    result = _run(journey, "DELIVERY")
    short = result["DELIVERED_ON_SHORT_PAYMENT"]
    assert short.outcome == "FAIL"
    assert "₹6,00,000 received against ₹10,00,000 payable (₹4,00,000 short)" in short.reason
    within = result["PAYMENT_AFTER_DELIVERY_WITHIN_GRACE"]
    assert within.outcome == "FAIL" and "₹3,00,000 on" in within.reason and "3 days after delivery" in within.reason
    beyond = result["PAYMENT_AFTER_DELIVERY_BEYOND_GRACE"]
    assert beyond.outcome == "FAIL" and "₹50,000 on" in beyond.reason and "10 days after delivery" in beyond.reason
    assert result["POST_DELIVERY_REFUND"].outcome == "FAIL"
    assert "refund of ₹20,000" in result["POST_DELIVERY_REFUND"].reason
    assert result["DO_PAYMENT_NOT_RECEIVED"].outcome == "PASS"  # not financed
    assert result["TRADE_IN_NOT_RESOLD"].outcome == "PASS"  # no exchange


def test_balance_still_outstanding_waits_for_the_window_then_fails(journey):
    _line(journey, "ex_showroom_price", "1000000", "1000000")
    _deliver(journey, TODAY - timedelta(days=2))
    _receipt(journey, "900000", TODAY - timedelta(days=3), "R-1")
    result = _run(journey, "DELIVERY")
    assert result["DELIVERED_ON_SHORT_PAYMENT"].outcome == "FAIL"
    assert result["PAYMENT_AFTER_DELIVERY_BEYOND_GRACE"].outcome == "SKIPPED"
    assert "₹1,00,000 is still outstanding" in result["PAYMENT_AFTER_DELIVERY_BEYOND_GRACE"].reason

    _sql(journey, "UPDATE auditcore.journey_document_extracted_fields SET effective_value=CAST(:v AS jsonb) "
                  "WHERE tenant_id=:t AND field_key='delivery_date'",
         v=json.dumps((TODAY - timedelta(days=9)).isoformat()))
    result = _run(journey, "DELIVERY")
    assert result["PAYMENT_AFTER_DELIVERY_BEYOND_GRACE"].outcome == "FAIL"
    assert "9 days after delivery" in result["PAYMENT_AFTER_DELIVERY_BEYOND_GRACE"].reason


def test_financier_windows(journey):
    _line(journey, "ex_showroom_price", "1000000", "1000000")
    _deliver(journey, TODAY - timedelta(days=20))
    _receipt(journey, "500000", TODAY - timedelta(days=22), "R-1")
    _sql(journey, "INSERT INTO auditcore.finance_records (tenant_id, journey_id, provider_name, financed_amount) "
                  "VALUES (:t, :j, 'HDFC Bank', 500000)")
    result = _run(journey, "DELIVERY")
    # The sanction counts as committed at delivery: not a short payment.
    assert result["DELIVERED_ON_SHORT_PAYMENT"].outcome == "PASS"
    assert result["DO_PAYMENT_NOT_RECEIVED"].outcome == "FAIL"
    assert "No loan disbursement from HDFC Bank recorded 20 days after delivery" in result["DO_PAYMENT_NOT_RECEIVED"].reason
    assert result["DO_SHORT_PAYMENT"].outcome == "SKIPPED"

    _sql(journey, "UPDATE auditcore.finance_records SET loan_disbursement_amount=450000 WHERE tenant_id=:t")
    result = _run(journey, "DELIVERY")
    assert result["DO_PAYMENT_NOT_RECEIVED"].outcome == "PASS"
    assert result["DO_SHORT_PAYMENT"].outcome == "FAIL"
    assert "₹4,50,000 against a sanction of ₹5,00,000 (₹50,000 short)" in result["DO_SHORT_PAYMENT"].reason


# ------------------------------------------------------------- trade-in


def test_trade_in_resale_window_and_loss(journey):
    _sql(journey, "INSERT INTO auditcore.trade_in_cases (tenant_id, journey_id, old_vehicle_registration, "
                  "actual_value, handover_at_utc) VALUES (:t, :j, 'KA01AB1234', 300000, now() - interval '100 days')")
    result = _run(journey, "DELIVERY")
    assert result["TRADE_IN_NOT_RESOLD"].outcome == "FAIL"
    assert "KA01AB1234" in result["TRADE_IN_NOT_RESOLD"].reason and "100 days" in result["TRADE_IN_NOT_RESOLD"].reason
    assert result["TRADE_IN_SOLD_AT_LOSS"].outcome == "SKIPPED"

    _sql(journey, "UPDATE auditcore.trade_in_cases SET resale_at_utc=now(), details=CAST(:d AS jsonb) "
                  "WHERE tenant_id=:t", d=json.dumps({"resaleValue": 250000}))
    result = _run(journey, "DELIVERY")
    assert result["TRADE_IN_NOT_RESOLD"].outcome == "PASS"
    assert result["TRADE_IN_SOLD_AT_LOSS"].outcome == "FAIL"
    assert "resold for ₹2,50,000 against ₹3,00,000 allowed to the customer (₹50,000 loss)" in result["TRADE_IN_SOLD_AT_LOSS"].reason


# ------------------------------------------------------------------ NDC


def test_ndc_signature_is_confirmed_by_the_pc_or_read_from_the_document(journey):
    result = _run(journey, "DELIVERY")
    assert result["NDC_SIGNATURE_UNCONFIRMED"].outcome == "SKIPPED"
    assert result["NDC_NOT_SIGNED"].outcome == "SKIPPED"

    add_ready_document(journey, "no_dues_certificate", marker="x")
    _evaluate(journey, "NATIVE:DELIVERY")
    state = _states(journey)
    assert state["NDC_SIGNATURE_UNCONFIRMED"]["control_status"] == "FAIL"
    assert state["NDC_NOT_SIGNED"]["control_status"] == "WAITING_FOR_FACTS"
    question = _task(journey, "NDC_SIGNATURE_UNCONFIRMED")
    assert [a["value"] for a in question["reference"]["answers"]] == ["YES", "NOT_WITNESSED", "NO"]

    # An observation may be keyed in by the Team Lead as well as the PC.
    _answer(journey, "NDC_SIGNATURE_UNCONFIRMED", "NOT_WITNESSED", "Signed at the showroom before I arrived", role="TL")
    _evaluate(journey, "NATIVE:DELIVERY")
    state = _states(journey)
    assert state["NDC_SIGNATURE_UNCONFIRMED"]["control_status"] == "PASS"
    assert state["NDC_NOT_SIGNED"]["control_status"] == "FAIL"
    assert "not in the auditor's presence" in state["NDC_NOT_SIGNED"]["status_reason"]
    assert 'Signed at the showroom before I arrived' in state["NDC_NOT_SIGNED"]["status_reason"]
    assert _task(journey, "NDC_NOT_SIGNED")["assigned_role_code"] == "TL"


def test_ndc_without_a_customer_signature_fails_from_the_reading(journey):
    add_ready_document(journey, "no_dues_certificate", customer_signature_present=False)
    result = _run(journey, "DELIVERY")
    assert result["NDC_NOT_SIGNED"].outcome == "FAIL"
    assert "no customer signature" in result["NDC_NOT_SIGNED"].reason


def test_accessories_fitted_is_asked_once_the_car_is_delivered(journey):
    assert _run(journey, "DELIVERY")["ACCESSORIES_FITTED_UNCONFIRMED"].outcome == "SKIPPED"
    _deliver(journey, TODAY - timedelta(days=1))
    asked = _run(journey, "DELIVERY")["ACCESSORIES_FITTED_UNCONFIRMED"]
    assert asked.outcome == "FAIL" and [a["value"] for a in asked.details["answers"]] == ["YES", "NO"]


def test_cash_limit_tcs_and_payment_before_booking(journey):
    add_ready_document(journey, "booking_form", booking_date="2026-09-10")
    _receipt(journey, "250000", date(2026, 9, 12), "R-1", mode="CASH")
    _receipt(journey, "10000", date(2026, 9, 1), "R-0")
    _line(journey, "ex_showroom_price", "1500000", "1500000")
    _line(journey, "tcs_amount", "15000", "5000")
    _source(journey, "COMMERCIAL", "tcs_amount", "tax_invoice_tally", "5000")
    result = _run(journey, "BOOKING")
    assert result["CASH_ABOVE_LIMIT"].outcome == "FAIL" and "₹2,50,000 in cash" in result["CASH_ABOVE_LIMIT"].reason
    assert result["PAYMENT_BEFORE_BOOKING"].outcome == "FAIL"
    assert "₹10,000 on 01 Sep 2026 (receipt R-0): dated before the booking on 10 Sep 2026" in result["PAYMENT_BEFORE_BOOKING"].reason
    assert result["TCS_SHORT"].outcome == "FAIL"
    assert "TCS ₹5,000 charged against ₹15,000 due (1% of the ex-showroom price ₹15,00,000; ₹10,000 short)" in result["TCS_SHORT"].reason


# ---------------------------------------------------- the TL's verdict


def _verdict(journey, code: str, action: str, comment: str, *, role: str = "TL") -> dict:
    task = _task(journey, code)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return submit_action(connection, tenant_id=journey.tenant_id, task_id=task["task_id"], action=action,
                             actor_id=f"{role.lower()}-1", actor_role_code=role, comment=comment,
                             details={"rejectionCategory": "DATA_ALREADY_CORRECT"})


def test_a_team_lead_verdict_closes_the_finding_and_its_task_for_good(journey):
    _line(journey, "ex_showroom_price", "1000000", "990000")
    _source(journey, "COMMERCIAL", "ex_showroom_price", "tax_invoice_tally", "990000")
    _evaluate(journey, "NATIVE:BOOKING")
    task = _task(journey, "DEAL_UNDERCHARGED")
    assert task["task_type"] == "FINDING_REVIEW" and task["reference"]["findingId"]
    assert "CONFIRM_BREACH" in _sql(journey, "SELECT allowed_actions FROM auditcore.p2_tasks WHERE tenant_id=:t "
                                             "AND task_id=:id", id=task["task_id"]).scalar_one()

    with pytest.raises(ValueError, match="assigned to role TL"):
        _verdict(journey, "DEAL_UNDERCHARGED", "CONFIRM_BREACH", "Dealer sold below list", role="PC")
    result = _verdict(journey, "DEAL_UNDERCHARGED", "CONFIRM_BREACH", "Dealer sold below list to close the month")
    assert result["status"] == "VERIFIED_COMPLETE" and result["outcome"] == "CONFIRMED_BREACH"
    status, _, _ = _finding(journey, "DEAL_UNDERCHARGED")
    assert status == "RESOLVED"
    disposition, reason = _sql(journey, "SELECT disposition, resolution_reason FROM auditcore.audit_findings "
                                        "WHERE tenant_id=:t AND rule_key='DEAL_UNDERCHARGED'").one()
    assert (disposition, reason) == ("CONFIRMED_BREACH", "Dealer sold below list to close the month")
    assert _task(journey, "DEAL_UNDERCHARGED")["task_status"] == "VERIFIED_COMPLETE"

    # The check still fails, but the verdict stands: no new finding, no new task.
    _evaluate(journey, "NATIVE:BOOKING")
    assert _states(journey)["DEAL_UNDERCHARGED"]["control_status"] == "FAIL"
    assert _sql(journey, "SELECT count(*) FROM auditcore.audit_findings WHERE tenant_id=:t "
                         "AND rule_key='DEAL_UNDERCHARGED'").scalar_one() == 1
    assert _task(journey, "DEAL_UNDERCHARGED")["task_status"] == "VERIFIED_COMPLETE"


def test_the_delivery_review_waits_for_every_verdict(journey):
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    _line(journey, "ex_showroom_price", "1000000", "990000")
    _source(journey, "COMMERCIAL", "ex_showroom_price", "tax_invoice_tally", "990000")
    _evaluate(journey, "NATIVE:BOOKING")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        raise_or_refresh(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                         dedupe_key=f"delivery-review:{journey.journey_id}", task_type="DELIVERY_REVIEW",
                         source_type="REVIEW", source_code="DELIVERY_REVIEW", title="Review the completed delivery",
                         description="x", reference={"sourceCode": "DELIVERY_REVIEW"}, severity="MEDIUM",
                         registry=get_registry())
    review = _sql(journey, "SELECT task_id FROM auditcore.p2_tasks WHERE tenant_id=:t AND task_type='DELIVERY_REVIEW'").scalar_one()
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        with pytest.raises(ValueError, match="Give a verdict on 1 open finding.*Deal charged below the standard price"):
            submit_action(connection, tenant_id=journey.tenant_id, task_id=review, action="COMPLETE_ACTION",
                          actor_id="tl-1", actor_role_code="TL", comment=None)
    _verdict(journey, "DEAL_UNDERCHARGED", "MARK_FALSE_POSITIVE", "Price list was superseded")
    assert _finding(journey, "DEAL_UNDERCHARGED")[0] == "RESOLVED"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert submit_action(connection, tenant_id=journey.tenant_id, task_id=review, action="COMPLETE_ACTION",
                             actor_id="tl-1", actor_role_code="TL", comment=None)["status"] == "VERIFYING"


# ------------------------------------ the documents window and the night


def test_a_date_before_the_floor_never_starts_the_delivery_clock(journey):
    """#19 (2026-09-30): a gate pass read as 2019 is a misreading the PC is
    asked to fix; the delivery window is not counted from it."""
    add_ready_document(journey, "gate_pass", delivery_date="12/03/2019")
    _recompute(journey)
    assert _run(journey, "DELIVERY")["DELIVERY_NOT_COMPLETED_IN_TIME"].outcome == "PASS"
    add_ready_document(journey, "customer_invoice_dms", invoice_date=(TODAY - timedelta(days=2)).isoformat())
    _recompute(journey)
    waiting = _run(journey, "DELIVERY")["DELIVERY_NOT_COMPLETED_IN_TIME"]
    assert waiting.outcome == "SKIPPED" and "customer invoice dms dated" in waiting.reason


def test_delivery_not_completed_in_time_lists_everything_pending_for_the_tl(journey):
    assert _run(journey, "DELIVERY")["DELIVERY_NOT_COMPLETED_IN_TIME"].outcome == "PASS"
    add_ready_document(journey, "insurance_cover", policy_start_date=(TODAY - timedelta(days=3)).isoformat())
    add_ready_document(journey, "customer_invoice_dms", invoice_date=(TODAY - timedelta(days=2)).isoformat())
    _recompute(journey)
    waiting = _run(journey, "DELIVERY")["DELIVERY_NOT_COMPLETED_IN_TIME"]
    assert waiting.outcome == "SKIPPED"
    assert f"Delivery due by {(TODAY + timedelta(days=4)).strftime('%d %b %Y')}" in waiting.reason  # from the cover note
    assert "insurance cover dated" in waiting.reason and "Customer Ledger" in waiting.reason

    # An earlier-dated gate pass moves the clock: 8 days ago, so the window has closed.
    add_ready_document(journey, "gate_pass", delivery_date=(TODAY - timedelta(days=8)).isoformat())
    _evaluate(journey, "NATIVE:DELIVERY")
    state = _states(journey)["DELIVERY_NOT_COMPLETED_IN_TIME"]
    assert state["control_status"] == "FAIL"
    assert "8 days after the gate pass dated" in state["status_reason"]
    assert "documents missing: " in state["status_reason"] and "Tax Invoice (Tally)" in state["status_reason"]
    assert "Vehicle photos" in state["status_reason"] or "VIN" in state["status_reason"]
    status, severity, title = _finding(journey, "DELIVERY_NOT_COMPLETED_IN_TIME")
    assert (status, severity) == ("OPEN", "HIGH") and title.startswith("Delivery not completed in time: ")
    task = _task(journey, "DELIVERY_NOT_COMPLETED_IN_TIME")
    assert task["task_type"] == "FINDING_REVIEW" and task["assigned_role_code"] == "TL" and task["reference"]["findingId"]


def test_the_seventh_day_event_follows_the_earliest_printed_date(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert schedule_delivery_completion_check(connection, tenant_id=journey.tenant_id,
                                                  journey_id=journey.journey_id) is None
    add_ready_document(journey, "customer_invoice_dms", invoice_date=TODAY.isoformat())
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        key = schedule_delivery_completion_check(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        assert schedule_delivery_completion_check(connection, tenant_id=journey.tenant_id,
                                                  journey_id=journey.journey_id) is None  # queued once
    fired_at = lambda: _sql(journey, "SELECT next_attempt_at_utc FROM auditcore.p2_work_queue WHERE tenant_id=:t "
                            "AND work_type='CONTROL_EVALUATE' AND work_key=:k", k=key).scalar_one()
    assert fired_at().date() == TODAY + timedelta(days=7)
    # A cover note dated two days earlier pulls the event forward.
    add_ready_document(journey, "insurance_cover", issue_date=(TODAY - timedelta(days=2)).isoformat())
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert schedule_delivery_completion_check(connection, tenant_id=journey.tenant_id,
                                                  journey_id=journey.journey_id) == key
    assert fired_at().date() == TODAY + timedelta(days=5)


def test_nightly_review_queues_each_delivery_in_progress_once(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert queue_nightly_review(connection, tenant_id=journey.tenant_id) == 0  # delivery not started
    add_evidence(journey, di_document_id=uuid4(), document_type_key="gate_pass", process_area="DELIVERY")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert queue_nightly_review(connection, tenant_id=journey.tenant_id) == 1
        assert queue_nightly_review(connection, tenant_id=journey.tenant_id) == 0
    key = f"nightly:{TODAY.isoformat()}:{journey.journey_id}"
    assert _sql(journey, "SELECT payload FROM auditcore.p2_work_queue WHERE tenant_id=:t AND work_key=:k",
                k=key).scalar_one()["unit"] == "NATIVE:DELIVERY"
    # A reviewed delivery is no longer in progress.
    _sql(journey, "UPDATE auditcore.journeys SET review_completed_at_utc=now() WHERE tenant_id=:t AND journey_id=:j")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert queue_nightly_review(connection, tenant_id=journey.tenant_id, today=TODAY + timedelta(days=1)) == 0


def test_a_broken_rule_is_reported_and_the_other_rules_still_run(journey, monkeypatch):
    from audit_core import uc03_p2_audit_rules as rules

    def broken_rule(facts):
        raise RuntimeError("boom")

    monkeypatch.setattr(rules, "_BOOKING_RULES", (broken_rule, *rules._BOOKING_RULES))
    outcomes = _run(journey, "BOOKING")
    assert outcomes["BROKEN_RULE"].outcome == "ERROR"
    assert len(outcomes) == len(rules._BOOKING_RULES)  # every other rule produced its outcome
