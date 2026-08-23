from fastapi import FastAPI
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from audit_core.errors import install_error_handlers
from audit_core.observability import (
    CORRELATION_HEADER,
    install_observability,
    log_dependency,
)


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    install_observability(app)

    @app.get("/ok")
    def ok() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/fail")
    def fail() -> None:
        raise RuntimeError("PAN ABCDE1234F token top-secret")

    return app


def test_correlation_id_is_propagated_and_generated() -> None:
    client = TestClient(_app(), raise_server_exceptions=False)

    provided = client.get("/ok", headers={CORRELATION_HEADER: "c-provided"})
    generated = client.post("/fail")

    assert provided.headers[CORRELATION_HEADER] == "c-provided"
    generated_id = generated.headers[CORRELATION_HEADER]
    assert generated_id
    assert generated.json()["correlationId"] == generated_id


def test_success_request_does_not_create_application_log_noise() -> None:
    client = TestClient(_app(), raise_server_exceptions=False)

    with capture_logs() as logs:
        response = client.get("/ok", headers={CORRELATION_HEADER: "c-success"})

    assert response.status_code == 200
    assert not any(event.get("event") == "http_request" for event in logs)
    assert not any(event.get("event") == "http_request_failed" for event in logs)


def test_request_and_error_logs_exclude_sensitive_payloads() -> None:
    client = TestClient(_app(), raise_server_exceptions=False)

    with capture_logs() as logs:
        client.post(
            "/fail?raw_id=ABCDE1234F",
            headers={"Authorization": "Bearer top-secret"},
            json={"pan": "ABCDE1234F"},
        )

    recorded = repr(logs)
    assert "ABCDE1234F" not in recorded
    assert "top-secret" not in recorded
    assert any(event.get("event") == "api_error" for event in logs)
    assert any(event.get("event") == "http_request_failed" for event in logs)


def test_dependency_success_is_metrics_only_and_failure_is_logged() -> None:
    with capture_logs() as logs:
        log_dependency(
            correlation_id="c-dependency",
            dependency="DI",
            operation="status",
            result="SUCCESS",
        )
        log_dependency(
            correlation_id="c-dependency",
            dependency="DI",
            operation="status",
            result="UNAVAILABLE",
        )

    dependency_logs = [
        record for record in logs if record.get("event") == "dependency_call_failed"
    ]
    assert len(dependency_logs) == 1
    record = dependency_logs[0]
    assert record["correlation_id"] == "c-dependency"
    assert record["dependency"] == "DI"
    assert record["operation"] == "status"
    assert record["result"] == "UNAVAILABLE"
