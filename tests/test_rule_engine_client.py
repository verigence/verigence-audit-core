from __future__ import annotations

import httpx
import pytest

from audit_core.rule_engine_client import (
    RuleEngineClient,
    RuleEngineClientError,
    build_rule_engine_client,
    get_rule_engine_client,
)

TENANT = "tenant-1"
SUBJECT = "11111111-1111-1111-1111-111111111111"
TOKEN = "svc-token"


def _envelope(data: dict) -> dict:
    return {"errorCode": "000", "errorMessage": "Success", "data": data}


def _client(handler) -> RuleEngineClient:
    return RuleEngineClient(
        base_url="http://rule-engine.internal:8080",
        transport=httpx.MockTransport(handler),
    )


def test_evaluate_phase_parses_anomalies() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.url.path == f"/v1/tenants/{TENANT}/subjects/{SUBJECT}/audit/booking"
        assert request.method == "POST"
        return httpx.Response(
            200,
            json=_envelope(
                {
                    "phase": "BOOKING",
                    "auditRunId": "run-9",
                    "verdict": "CRITICAL_OPEN",
                    "anomalies": [
                        {
                            "ruleCode": "PRICE_BOOKING_VS_INVOICE",
                            "severity": "CRITICAL",
                            "category": "PRICE",
                            "detail": "Booking 900000 vs invoice 950000",
                            "leftValue": "900000",
                            "rightValue": "950000",
                        },
                        {"severity": "INFO"},  # no ruleCode → dropped
                    ],
                }
            ),
        )

    with _client(handle) as client:
        result = client.evaluate_phase(
            token=TOKEN, tenant_id=TENANT, subject_id=SUBJECT, phase="BOOKING"
        )

    assert result.verdict == "CRITICAL_OPEN"
    assert result.audit_run_id == "run-9"
    assert len(result.anomalies) == 1
    anomaly = result.anomalies[0]
    assert anomaly.rule_code == "PRICE_BOOKING_VS_INVOICE"
    assert anomaly.severity == "CRITICAL"
    assert anomaly.left_value == "900000"


def test_evaluate_phase_handles_no_anomalies() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_envelope({"phase": "DELIVERY", "anomalies": []}))

    with _client(handle) as client:
        result = client.evaluate_phase(
            token=TOKEN, tenant_id=TENANT, subject_id=SUBJECT, phase="DELIVERY"
        )
    assert result.anomalies == ()


def test_evaluate_phase_raises_on_http_error() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"errorCode": "403", "errorMessage": "denied"})

    with _client(handle) as client, pytest.raises(RuleEngineClientError) as exc:
        client.evaluate_phase(
            token=TOKEN, tenant_id=TENANT, subject_id=SUBJECT, phase="BOOKING"
        )
    assert exc.value.status_code == 403


def test_evaluate_phase_raises_on_transport_failure() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with _client(handle) as client, pytest.raises(RuleEngineClientError) as exc:
        client.evaluate_phase(
            token=TOKEN, tenant_id=TENANT, subject_id=SUBJECT, phase="BOOKING"
        )
    assert exc.value.code == "RULE_ENGINE_UNAVAILABLE"


def test_evaluate_phase_rejects_unknown_phase() -> None:
    with _client(lambda r: httpx.Response(200)) as client, pytest.raises(ValueError):
        client.evaluate_phase(
            token=TOKEN, tenant_id=TENANT, subject_id=SUBJECT, phase="NONSENSE"
        )


def test_dependency_and_factory_are_dormant_without_base_url(monkeypatch) -> None:
    monkeypatch.delenv("RULE_ENGINE_BASE_URL", raising=False)
    assert build_rule_engine_client() is None
    assert next(get_rule_engine_client()) is None


def test_list_rules_parses_catalog() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.url.path == f"/v1/tenants/{TENANT}/audit/rules"
        assert request.method == "GET"
        return httpx.Response(
            200,
            json=_envelope(
                {
                    "rules": [
                        {
                            "rule_code": "PRICE_BOOKING_VS_INVOICE",
                            "category": "PRICE",
                            "audit_scope": "WITHIN_CASE",
                            "phases": ["BOOKING", "DELIVERY"],
                            "comparator": "ABS_DIFF_GT",
                            "threshold": 500.0,
                            "severity": "CRITICAL",
                            "finding_message": "Booking price differs from invoice",
                            "enabled": True,
                        },
                        {"rule_code": ""},  # dropped -- no usable rule_code
                    ],
                }
            ),
        )

    with _client(handle) as client:
        rules = client.list_rules(token=TOKEN, tenant_id=TENANT)

    assert len(rules) == 1
    rule = rules[0]
    assert rule.rule_code == "PRICE_BOOKING_VS_INVOICE"
    assert rule.category == "PRICE"
    assert rule.phases == ("BOOKING", "DELIVERY")
    assert rule.threshold == 500.0
    assert rule.enabled is True


def test_list_rules_requires_token() -> None:
    with _client(lambda r: httpx.Response(200)) as client, pytest.raises(ValueError):
        client.list_rules(token="", tenant_id=TENANT)


def test_readiness_parses_ready_and_not_ready() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/tenants/{TENANT}/subjects/{SUBJECT}/audit/readiness"
        assert request.method == "GET"
        return httpx.Response(
            200,
            json=_envelope(
                {
                    "ready": ["PRICE_BOOKING_VS_INVOICE", "KYC_NAME_VS_BOOKING"],
                    "notReady": [
                        {"ruleCode": "EXCHANGE_VALUE_BELOW_MARKET", "reason": "no trade_in_valuation on file"},
                        {"ruleCode": ""},  # dropped -- no usable rule_code
                    ],
                }
            ),
        )

    with _client(handle) as client:
        readiness = client.readiness(token=TOKEN, tenant_id=TENANT, subject_id=SUBJECT)

    assert readiness.ready == ("PRICE_BOOKING_VS_INVOICE", "KYC_NAME_VS_BOOKING")
    assert len(readiness.not_ready) == 1
    assert readiness.not_ready[0].rule_code == "EXCHANGE_VALUE_BELOW_MARKET"
    assert readiness.not_ready[0].reason == "no trade_in_valuation on file"


def test_readiness_requires_token() -> None:
    with _client(lambda r: httpx.Response(200)) as client, pytest.raises(ValueError):
        client.readiness(token="", tenant_id=TENANT, subject_id=SUBJECT)


def test_runs_parses_history() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/tenants/{TENANT}/subjects/{SUBJECT}/audit/runs"
        return httpx.Response(
            200,
            json=_envelope(
                {
                    "runs": [
                        {
                            "audit_run_id": "run-9",
                            "audit_scope": "WITHIN_CASE",
                            "trigger_mode": "EVENT_DRIVEN",
                            "triggered_by": "BOOKING_REVIEW_CONFIRMED",
                            "total_rules": 40,
                            "pass_count": 35,
                            "fail_count": 2,
                            "skipped_count": 3,
                            "critical_fail": 1,
                            "warning_fail": 1,
                            "info_fail": 0,
                            "verdict": "CRITICAL_OPEN",
                            "started_at_utc": "2026-09-12T00:00:00Z",
                            "completed_at_utc": "2026-09-12T00:00:01Z",
                        },
                        {"audit_run_id": ""},  # dropped -- no usable id
                    ]
                }
            ),
        )

    with _client(handle) as client:
        runs = client.runs(token=TOKEN, tenant_id=TENANT, subject_id=SUBJECT)

    assert len(runs) == 1
    run = runs[0]
    assert run.audit_run_id == "run-9"
    assert run.total_rules == 40
    assert run.pass_count == 35
    assert run.fail_count == 2
    assert run.skipped_count == 3
    assert run.verdict == "CRITICAL_OPEN"


def test_runs_requires_token() -> None:
    with _client(lambda r: httpx.Response(200)) as client, pytest.raises(ValueError):
        client.runs(token="", tenant_id=TENANT, subject_id=SUBJECT)
