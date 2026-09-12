from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

import audit_core.uc03_rule_engine_findings as findings
from audit_core.rule_engine_client import (
    NotReadyRule as _NotReadyRule,
)
from audit_core.rule_engine_client import (
    RuleEngineAnomaly,
    RuleEnginePhaseResult,
    RuleEngineReadiness,
    RuleEngineRule,
)

TENANT = "tenant-1"
JOURNEY = uuid4()


def _anomaly(rule_code: str, severity: str, category: str | None, detail: str | None = "d") -> RuleEngineAnomaly:
    return RuleEngineAnomaly(
        rule_code=rule_code,
        severity=severity,
        category=category,
        detail=detail,
        left_value="1",
        right_value="2",
    )


def test_finding_title_collapses_whitespace_and_truncates() -> None:
    long_detail = "word " * 100
    title = findings._finding_title(_anomaly("R", "INFO", "PRICE", long_detail))
    assert len(title) <= findings._TITLE_MAX
    assert "  " not in title


def test_finding_title_falls_back_without_detail() -> None:
    title = findings._finding_title(_anomaly("PRICE_X", "INFO", "PRICE", None))
    assert "PRICE_X" in title


def test_materialize_maps_severity_and_finding_type(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_machine_flag(_connection, **kwargs):
        calls.append(kwargs)
        return uuid4()

    monkeypatch.setattr(findings, "_machine_flag", fake_machine_flag)

    flagged = findings._materialize_anomalies(
        connection=object(),
        tenant_id=TENANT,
        journey_id=JOURNEY,
        stage_code="BOOKING",
        anomalies=(
            _anomaly("PRICE_BOOKING_VS_INVOICE", "CRITICAL", "PRICE"),
            _anomaly("KYC_NAME_VS_BOOKING", "WARNING", "KYC"),
            _anomaly("SOME_NEW_RULE", "INFO", "MYSTERY"),
        ),
        correlation_id="corr-1",
        audit_run_id="run-1",
    )

    assert set(flagged) == {
        "PRICE_BOOKING_VS_INVOICE",
        "KYC_NAME_VS_BOOKING",
        "SOME_NEW_RULE",
    }
    assert all(isinstance(finding_id, type(uuid4())) for finding_id in flagged.values())
    assert [c["severity"] for c in calls] == ["CRITICAL", "HIGH", "MEDIUM"]
    assert [c["finding_type"] for c in calls] == [
        "PRICING_ANOMALY",
        "CUSTOMER_IDENTITY_CONCERN",
        "RULE_ENGINE_ANOMALY",
    ]
    assert all(c["stage_code"] == "BOOKING" for c in calls)
    assert all(c["blocking_completion"] is False for c in calls)
    assert calls[0]["safe_payload"]["ruleCode"] == "PRICE_BOOKING_VS_INVOICE"
    assert calls[0]["safe_payload"]["auditRunId"] == "run-1"


def test_unknown_rule_engine_severity_defaults_to_medium(monkeypatch) -> None:
    captured: list[str] = []
    monkeypatch.setattr(
        findings, "_machine_flag", lambda _c, **kw: captured.append(kw["severity"]) or uuid4()
    )
    findings._materialize_anomalies(
        connection=object(),
        tenant_id=TENANT,
        journey_id=JOURNEY,
        stage_code="DELIVERY",
        anomalies=(_anomaly("R", "SURPRISE", "PRICE"),),
        correlation_id="c",
        audit_run_id=None,
    )
    assert captured == ["MEDIUM"]


def test_run_rule_engine_phase_is_dormant_without_base_url(monkeypatch) -> None:
    monkeypatch.delenv("RULE_ENGINE_BASE_URL", raising=False)
    sentinel = {"called": False}
    monkeypatch.setattr(
        findings, "_build_security_oauth_client", lambda: sentinel.__setitem__("called", True)
    )
    findings.run_rule_engine_phase(
        engine=object(),
        tenant_id=TENANT,
        journey_id=JOURNEY,
        stage_code="BOOKING",
        phase="BOOKING",
        correlation_id="corr",
    )
    assert sentinel["called"] is False


def test_run_rule_engine_phase_swallows_errors(monkeypatch) -> None:
    monkeypatch.setenv("RULE_ENGINE_BASE_URL", "http://rule-engine.internal:8080")

    class _Boom:
        def close(self) -> None:
            pass

    monkeypatch.setattr(findings, "build_rule_engine_client", lambda: _Boom())

    def _raise() -> None:
        raise RuntimeError("security down")

    monkeypatch.setattr(findings, "_build_security_oauth_client", _raise)

    # must not raise
    findings.run_rule_engine_phase(
        engine=object(),
        tenant_id=TENANT,
        journey_id=JOURNEY,
        stage_code="BOOKING",
        phase="BOOKING",
        correlation_id="corr",
    )


def _rule(rule_code: str, *phases: str) -> RuleEngineRule:
    return RuleEngineRule(
        rule_code=rule_code,
        category="PRICE",
        audit_scope="WITHIN_CASE",
        phases=phases,
        comparator="ABS_DIFF_GT",
        threshold=0.0,
        severity="CRITICAL",
        finding_message="msg",
        enabled=True,
    )


def test_rule_codes_for_phase_includes_full_tagged_rules() -> None:
    class _Client:
        def list_rules(self, *, token, tenant_id):
            return (
                _rule("BOOKING_ONLY", "BOOKING"),
                _rule("DELIVERY_ONLY", "DELIVERY"),
                _rule("EVERY_PHASE", "FULL"),
            )

    codes = findings._rule_codes_for_phase(
        _Client(), token="t", tenant_id=TENANT, phase="booking"
    )
    assert codes == {"BOOKING_ONLY", "EVERY_PHASE"}


class _FakeSecurityClient:
    def get_service_token(self, *, audience: str) -> str:
        return "svc-token"

    def close(self) -> None:
        pass


@pytest.fixture
def rule_engine_findings_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-ref-{suffix}"
    with engine.begin() as connection:
        category_id = connection.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"REF-CAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"REF-OEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'REF', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"REF-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = connection.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"REF-D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"REF-O-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text("""INSERT INTO auditcore.customers
                (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        connection.execute(
            text("""INSERT INTO auditcore.di_subject_mappings (
                tenant_id, customer_id, di_subject_id, di_subject_type, mapping_status
            ) VALUES (:t, :cu, :subj, 'OTHER', 'ACTIVE')"""),
            {"t": tenant_id, "cu": customer_id, "subj": uuid4()},
        )
        journey_id = connection.execute(
            text("""INSERT INTO auditcore.journeys
                (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"REF-J-{suffix}"},
        ).scalar_one()
    yield engine, tenant_id, journey_id
    engine.dispose()


def test_run_rule_engine_phase_records_pass_fail_skipped(monkeypatch, rule_engine_findings_setup) -> None:
    engine, tenant_id, journey_id = rule_engine_findings_setup
    monkeypatch.setenv("RULE_ENGINE_BASE_URL", "http://rule-engine.internal:8080")
    monkeypatch.setattr(findings, "_build_security_oauth_client", lambda: _FakeSecurityClient())

    class _FakeClient:
        def evaluate_phase(self, *, token, tenant_id, subject_id, phase):
            return RuleEnginePhaseResult(
                phase=phase,
                audit_run_id="run-1",
                verdict="CRITICAL_OPEN",
                anomalies=(
                    RuleEngineAnomaly(
                        rule_code="PRICE_BOOKING_VS_INVOICE",
                        severity="CRITICAL",
                        category="PRICE",
                        detail="Booking price differs from invoice",
                        left_value="1",
                        right_value="2",
                    ),
                ),
            )

        def list_rules(self, *, token, tenant_id):
            return (
                _rule("PRICE_BOOKING_VS_INVOICE", "BOOKING"),
                _rule("KYC_NAME_VS_BOOKING", "BOOKING"),
                _rule("EXCHANGE_VALUE_BELOW_MARKET", "EXCHANGE"),  # not relevant to BOOKING
            )

        def readiness(self, *, token, tenant_id, subject_id):
            return RuleEngineReadiness(
                ready=("PRICE_BOOKING_VS_INVOICE", "KYC_NAME_VS_BOOKING"),
                not_ready=(_NotReadyRule(rule_code="EXCHANGE_VALUE_BELOW_MARKET", reason="no trade_in_valuation"),),
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(findings, "build_rule_engine_client", lambda: _FakeClient())

    findings.run_rule_engine_phase(
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code="BOOKING",
        phase="BOOKING",
        correlation_id="corr-1",
    )

    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT rule_code, outcome, reason, audit_finding_id, triggering_event "
                "FROM auditcore.rule_executions WHERE tenant_id=:t ORDER BY rule_code"
            ),
            {"t": tenant_id},
        ).mappings().all()

    by_rule = {row["rule_code"]: row for row in rows}
    assert set(by_rule) == {"PRICE_BOOKING_VS_INVOICE", "KYC_NAME_VS_BOOKING"}
    # EXCHANGE_VALUE_BELOW_MARKET's notReady entry is correctly excluded --
    # it isn't relevant to the BOOKING phase that actually ran.

    fail_row = by_rule["PRICE_BOOKING_VS_INVOICE"]
    assert fail_row["outcome"] == "FAIL"
    assert fail_row["audit_finding_id"] is not None
    assert fail_row["triggering_event"] == "BOOKING_REVIEW_CONFIRMED"

    pass_row = by_rule["KYC_NAME_VS_BOOKING"]
    assert pass_row["outcome"] == "PASS"
    assert pass_row["audit_finding_id"] is None
