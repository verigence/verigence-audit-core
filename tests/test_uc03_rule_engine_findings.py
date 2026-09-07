from __future__ import annotations

from uuid import uuid4

import audit_core.uc03_rule_engine_findings as findings
from audit_core.rule_engine_client import RuleEngineAnomaly

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

    assert flagged == [
        "RE_PRICE_BOOKING_VS_INVOICE",
        "RE_KYC_NAME_VS_BOOKING",
        "RE_SOME_NEW_RULE",
    ]
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
