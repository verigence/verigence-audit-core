"""Phase 2 control ledger: every executor writes one authoritative state."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_batch_pages,
    add_ready_document,
    add_receipt_payment,
    create_p2_journey,
    database_engine,
    set_minimum_booking_amount,
)
from sqlalchemy import text

from audit_core import uc03_p2_controls as controls
from audit_core import uc03_rule_engine_findings as re_findings
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_stage import recompute_journey_stage


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2ctl")
    set_minimum_booking_amount(created, "21000")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _states(journey) -> dict[str, dict]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        rows = connection.execute(
            text("SELECT control_code, control_status, status_reason, details, executor_type, control_mode "
                 "FROM auditcore.p2_control_state WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).mappings().all()
    return {r["control_code"]: dict(r) for r in rows}


def _recompute_stage(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        recompute_journey_stage(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _evaluate(journey, unit, **kwargs):
    return controls.evaluate_unit(journey.engine, tenant_id=journey.tenant_id,
                                  journey_id=journey.journey_id, unit=unit, **kwargs)


# ------------------------------------------------------------------- derived

def test_gate_controls_wait_for_evidence_then_pass(journey):
    _recompute_stage(journey)
    _evaluate(journey, "P2:DERIVED")
    state = _states(journey)
    assert state["BK_PAN_PRESENT"]["control_status"] == "WAITING_FOR_FACTS"
    assert "PAN" in state["BK_PAN_PRESENT"]["status_reason"]
    assert state["DL_VIN_RECONCILIATION"]["control_status"] == "WAITING_FOR_FACTS"
    assert "Delivery vehicle observation" in state["DL_VIN_RECONCILIATION"]["status_reason"]
    assert state["DOCUMENT_UNRECOGNIZED"]["control_status"] == "WAITING_FOR_FACTS"

    add_ready_document(journey, "pan_card", pan_number="P")
    add_ready_document(journey, "aadhaar", aadhaar_number="1")
    add_receipt_payment(journey, amount="5000", receipt_number="R1", receipt_date="2026-09-01")
    _recompute_stage(journey)
    _evaluate(journey, "P2:DERIVED")
    state = _states(journey)
    assert state["BK_PAN_PRESENT"]["control_status"] == "PASS"
    assert state["BK_PAN_PRESENT"]["executor_type"] == "P2"
    # receipts exist but fall short: a real failure, not "waiting"
    assert state["BK_MIN_BOOKING_AMOUNT_NOT_MET"]["control_status"] == "FAIL"
    assert state["BK_MIN_BOOKING_AMOUNT_NOT_MET"]["details"]["gates"]["MINIMUM_BOOKING_PAYMENT"]["shortfall"] == "16000.00"


def test_page_controls_follow_page_outcomes(journey):
    add_batch_pages(journey, [("aadhaar", "READY"), (None, "FAILED")], grouping_status="GROUPED")
    _evaluate(journey, "P2:DERIVED")
    state = _states(journey)
    assert state["DL_V2_DOCUMENT_PROCESSING_FAILED"]["control_status"] == "FAIL"
    assert state["DOCUMENT_UNRECOGNIZED"]["control_status"] == "PASS"


def test_sync_controls_mirror_the_existing_document_sync(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.rule_executions (tenant_id, journey_id, rule_code, triggering_event, "
                 "outcome, reason) VALUES (:t, :j, 'FINANCE_HYPOTHECATION_MISSING', 'DOCUMENT_SYNCED', "
                 "'FAIL', 'RTO challan shows no hypothecation')"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
    _evaluate(journey, "P2:DERIVED")
    state = _states(journey)["FINANCE_HYPOTHECATION_MISSING"]
    assert state["control_status"] == "FAIL" and "hypothecation" in state["status_reason"]


def test_unchanged_facts_skip_reevaluation(journey):
    assert _evaluate(journey, "P2:DERIVED")
    assert _evaluate(journey, "P2:DERIVED") == []
    add_ready_document(journey, "pan_card", pan_number="P")
    assert _evaluate(journey, "P2:DERIVED")


# ------------------------------------------------------------------ external

class FakeRuleEngine:
    def __init__(self, *, fail=None):
        self.fail = fail
        self.calls = 0

    def evaluate_phase(self, *, token, tenant_id, subject_id, phase):
        self.calls += 1
        if self.fail:
            raise self.fail
        return SimpleNamespace(
            audit_run_id="run-1", verdict="ANOMALIES",
            anomalies=(SimpleNamespace(rule_code="PRICE_BOOKING_VS_INVOICE", severity="HIGH", category="PRICE",
                                       detail="Invoice total differs from booking total",
                                       left_value="2450000", right_value="2510000"),),
        )

    def list_rules(self, *, token, tenant_id):
        return (
            SimpleNamespace(rule_code="PRICE_BOOKING_VS_INVOICE", phases=("BOOKING",)),
            SimpleNamespace(rule_code="KYC_NAME_VS_BOOKING", phases=("BOOKING",)),
            SimpleNamespace(rule_code="KYC_DOB_AADHAAR_VS_PAN", phases=("BOOKING",)),
        )

    def readiness(self, *, token, tenant_id, subject_id):
        return SimpleNamespace(
            ready=("PRICE_BOOKING_VS_INVOICE", "KYC_NAME_VS_BOOKING"),
            not_ready=(SimpleNamespace(rule_code="KYC_DOB_AADHAAR_VS_PAN", reason="pan_card.date_of_birth missing"),),
        )

    def close(self):
        pass


class FakeSecurity:
    def get_service_token(self, *, audience):
        return "token"

    def close(self):
        pass


def _wire_rule_engine(monkeypatch, engine_double):
    import audit_core.rule_engine_client as rec

    monkeypatch.setattr(rec, "build_rule_engine_client", lambda: engine_double)
    monkeypatch.setattr(re_findings, "_build_security_oauth_client", lambda: FakeSecurity())
    monkeypatch.setattr(re_findings, "_resolve_di_subject_id", lambda connection, **_: uuid4())
    monkeypatch.setattr(
        re_findings, "_materialize_anomalies",
        lambda connection, **kw: {a.rule_code: None for a in kw["anomalies"]},
    )


def test_rule_engine_results_become_ledger_states(journey, monkeypatch):
    _wire_rule_engine(monkeypatch, FakeRuleEngine())
    _evaluate(journey, "RULE_ENGINE:BOOKING")
    state = _states(journey)
    price = state["PRICE_BOOKING_VS_INVOICE"]
    assert price["control_status"] == "FAIL"
    assert (price["details"]["leftValue"], price["details"]["rightValue"]) == ("2450000", "2510000")
    assert price["executor_type"] == "EXTERNAL_RULE_ENGINE"
    assert state["KYC_NAME_VS_BOOKING"]["control_status"] == "PASS"
    assert state["KYC_DOB_AADHAAR_VS_PAN"]["control_status"] == "WAITING_FOR_FACTS"
    assert "date_of_birth" in state["KYC_DOB_AADHAAR_VS_PAN"]["status_reason"]
    # enabled for Booking in the catalog but not evaluated by this Rule Engine
    assert state["BOOKING_AMOUNT_ZERO"]["control_status"] == "NOT_APPLICABLE"


def test_rule_engine_outage_is_retry_pending_never_clean(journey, monkeypatch):
    _wire_rule_engine(monkeypatch, FakeRuleEngine(fail=TimeoutError("rule engine down")))
    with pytest.raises(controls.ControlUnitError):
        _evaluate(journey, "RULE_ENGINE:BOOKING")
    assert _states(journey)["PRICE_BOOKING_VS_INVOICE"]["control_status"] == "RETRY_PENDING"

    # a later recovery replaces retry state; a later outage keeps real results
    _wire_rule_engine(monkeypatch, FakeRuleEngine())
    _evaluate(journey, "RULE_ENGINE:BOOKING")
    _wire_rule_engine(monkeypatch, FakeRuleEngine(fail=TimeoutError("down again")))
    with pytest.raises(controls.ControlUnitError):
        _evaluate(journey, "RULE_ENGINE:BOOKING", force=True)
    state = _states(journey)
    assert state["PRICE_BOOKING_VS_INVOICE"]["control_status"] == "FAIL"
    assert state["KYC_DOB_AADHAAR_VS_PAN"]["control_status"] == "RETRY_PENDING"


def test_unconfigured_rule_engine_is_an_error_not_a_pass(journey, monkeypatch):
    import audit_core.rule_engine_client as rec

    monkeypatch.setattr(rec, "build_rule_engine_client", lambda: None)
    _evaluate(journey, "RULE_ENGINE:BOOKING")
    assert _states(journey)["PRICE_BOOKING_VS_INVOICE"]["control_status"] == "ERROR_TERMINAL"


# -------------------------------------------------------------------- native

def test_native_runner_writes_rerun_controls(journey):
    _evaluate(journey, "NATIVE:BOOKING")
    state = _states(journey)
    for code in ("WRONG_DOCUMENT", "DUPLICATE_RECEIPT", "MANUAL_VERIFICATION", "PAYMENT_BANK_UNMATCHED"):
        assert state[code]["executor_type"] == "NATIVE"
        assert state[code]["control_status"] in {"WAITING_FOR_FACTS", "PASS"}


def test_statistics_are_attributed_to_stages(journey):
    _recompute_stage(journey)
    _evaluate(journey, "P2:DERIVED")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        stats = controls.control_statistics(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert stats["BOOKING"]["total"] > 0 and stats["DELIVERY"]["total"] > 0
    assert sum(v for k, v in stats["BOOKING"].items() if k != "total") == stats["BOOKING"]["total"]


def test_rule_engine_pass_resolves_the_stale_finding(journey, monkeypatch):
    from audit_core.uc03_delivery_commands import _machine_flag

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        stale = _machine_flag(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, stage_code="BOOKING",
            rule_key="RE_KYC_NAME_VS_BOOKING", finding_type="CUSTOMER_IDENTITY_CONCERN", severity="HIGH",
            title="Name differs", description="old anomaly", correlation_id="t", safe_payload={},
            blocking_completion=False,
        )
    _wire_rule_engine(monkeypatch, FakeRuleEngine())
    _evaluate(journey, "RULE_ENGINE:BOOKING")
    assert _states(journey)["KYC_NAME_VS_BOOKING"]["control_status"] == "PASS"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        status = connection.execute(
            text("SELECT finding_status FROM auditcore.audit_findings WHERE tenant_id=:t AND audit_finding_id=:f"),
            {"t": journey.tenant_id, "f": stale},
        ).scalar_one()
    assert status == "RESOLVED"


class RetiredRuleEngine(FakeRuleEngine):
    """Still evaluates a rule the catalogue retired in favour of a native check."""

    def evaluate_phase(self, *, token, tenant_id, subject_id, phase):
        result = super().evaluate_phase(token=token, tenant_id=tenant_id, subject_id=subject_id, phase=phase)
        retired = SimpleNamespace(rule_code="DISCOUNT_BOOKING_EXCEEDS_APPROVAL", severity="CRITICAL",
                                  category="DISCOUNT", detail="Booking promised discount exceeds approved amount",
                                  left_value="40000", right_value="25000")
        return SimpleNamespace(audit_run_id=result.audit_run_id, verdict=result.verdict,
                               anomalies=(*result.anomalies, retired))

    def list_rules(self, *, token, tenant_id):
        return (*super().list_rules(token=token, tenant_id=tenant_id),
                SimpleNamespace(rule_code="DISCOUNT_BOOKING_EXCEEDS_APPROVAL", phases=("BOOKING",)))


def test_a_retired_rule_engine_rule_raises_nothing_and_closes_its_old_finding(journey, monkeypatch):
    from audit_core.uc03_delivery_commands import _machine_flag

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        stale = _machine_flag(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, stage_code="BOOKING",
            rule_key="RE_DISCOUNT_BOOKING_EXCEEDS_APPROVAL", finding_type="DISCOUNT_ANOMALY", severity="HIGH",
            title="Discount exceeds approval", description="old anomaly", correlation_id="t", safe_payload={},
            blocking_completion=False,
        )
    _wire_rule_engine(monkeypatch, RetiredRuleEngine())
    _evaluate(journey, "RULE_ENGINE:BOOKING")
    state = _states(journey)
    assert "DISCOUNT_BOOKING_EXCEEDS_APPROVAL" not in state  # not a control any more
    assert state["PRICE_BOOKING_VS_INVOICE"]["control_status"] == "FAIL"  # the catalogued anomaly still lands
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        status = connection.execute(
            text("SELECT finding_status FROM auditcore.audit_findings WHERE tenant_id=:t AND audit_finding_id=:f"),
            {"t": journey.tenant_id, "f": stale},
        ).scalar_one()
    assert status == "RESOLVED"


def _second_journey(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        customer_id = connection.execute(
            text("INSERT INTO auditcore.customers (tenant_id, dealer_id, outlet_id, customer_type_code, "
                 "display_name) VALUES (:t, :d, :o, 'INDIVIDUAL', 'Second') RETURNING customer_id"),
            {"t": journey.tenant_id, "d": journey.dealer_id, "o": journey.outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, journey_reference, "
                 "created_at_utc) VALUES (:t, :d, :o, :c, :r, now() + interval '1 minute') RETURNING journey_id"),
            {"t": journey.tenant_id, "d": journey.dealer_id, "o": journey.outlet_id, "c": customer_id,
             "r": f"P2-J2-{uuid4().hex[:8]}"},
        ).scalar_one()
    from dataclasses import replace

    return replace(journey, journey_id=journey_id, customer_id=customer_id)


def test_duplicate_booking_fail_links_its_finding_and_match(journey):
    add_ready_document(journey, "pan_card", pan_number="ABCDE1234F", pan_name="RAVI KUMAR")
    second = _second_journey(journey)
    add_ready_document(second, "pan_card", pan_number="ABCDE1234F", pan_name="RAVI KUMAR")
    _evaluate(second, "NATIVE:BOOKING")
    duplicate = _states(second)["DUPLICATE_BOOKING"]
    assert duplicate["control_status"] == "FAIL"
    assert duplicate["details"]["matchBasis"]
    assert duplicate["details"]["believedOriginalJourneyId"] == str(journey.journey_id)
    with second.engine.begin() as connection:
        set_tenant_context(connection, second.tenant_id)
        linked = connection.execute(
            text("SELECT finding_id FROM auditcore.p2_control_state WHERE tenant_id=:t AND journey_id=:j "
                 "AND control_code='DUPLICATE_BOOKING'"), {"t": second.tenant_id, "j": second.journey_id},
        ).scalar_one()
    assert linked is not None


def _apply_tasks(journey, transitions):
    """What the worker does with a unit's transitions: raise or refresh the control's task."""
    from audit_core.uc03_p2_task_producer import apply_control_transitions

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        apply_control_transitions(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                  transitions=transitions, evaluation_started_at=None)


def test_a_stronger_duplicate_match_replaces_the_finding_the_control_and_task_follow(journey):
    """A pair first seen on the mobile number alone, then on the PAN: the control
    state, its details and the one Team Lead task follow the finding raised on
    the current basis, not the voided one."""
    add_ready_document(journey, "booking_form", customer_phone="9123456789")
    second = _second_journey(journey)
    add_ready_document(second, "booking_form", customer_phone="9123456789")
    _apply_tasks(second, _evaluate(second, "NATIVE:BOOKING"))
    assert _states(second)["DUPLICATE_BOOKING"]["details"]["matchBasis"] == "MOBILE"

    add_ready_document(journey, "pan_card", pan_number="ABCDE1234F", pan_name="RAVI KUMAR")
    add_ready_document(second, "pan_card", pan_number="ABCDE1234F", pan_name="RAVI KUMAR")
    _apply_tasks(second, _evaluate(second, "NATIVE:BOOKING"))

    with second.engine.begin() as connection:
        set_tenant_context(connection, second.tenant_id)
        linked, status = connection.execute(
            text("SELECT s.finding_id, f.finding_status FROM auditcore.p2_control_state s "
                 "JOIN auditcore.audit_findings f ON f.tenant_id=s.tenant_id AND f.audit_finding_id=s.finding_id "
                 "WHERE s.tenant_id=:t AND s.journey_id=:j AND s.control_code='DUPLICATE_BOOKING'"),
            {"t": second.tenant_id, "j": second.journey_id},
        ).one()
        tasks = connection.execute(
            text("SELECT reference->>'findingId' AS finding_id FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j "
                 "AND source_code='DUPLICATE_BOOKING' AND task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')"),
            {"t": second.tenant_id, "j": second.journey_id},
        ).scalars().all()
    state = _states(second)["DUPLICATE_BOOKING"]
    assert state["details"]["matchBasis"] == "PAN" and state["details"]["matchConfidencePercent"] == 99
    assert status == "OPEN"  # the control follows the finding on the current basis, not the voided one
    assert tasks == [str(linked)]  # one open Team Lead task, pointing at it
