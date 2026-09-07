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
