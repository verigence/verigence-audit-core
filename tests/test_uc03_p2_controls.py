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
