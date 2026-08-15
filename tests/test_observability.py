import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from audit_core.errors import install_error_handlers
from audit_core.observability import CORRELATION_HEADER, install_observability, log_dependency


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


def test_request_and_error_logs_exclude_sensitive_payloads(caplog) -> None:
    caplog.set_level(logging.INFO, logger="audit_core")
    client = TestClient(_app(), raise_server_exceptions=False)

    client.post(
        "/fail?raw_id=ABCDE1234F",
        headers={"Authorization": "Bearer top-secret"},
        json={"pan": "ABCDE1234F"},
    )

    recorded = " ".join(repr(record.__dict__) for record in caplog.records)
    assert "ABCDE1234F" not in recorded
    assert "top-secret" not in recorded
    assert any(record.getMessage() == "api_error" for record in caplog.records)
    assert any(record.getMessage() == "request_complete" for record in caplog.records)


def test_dependency_log_uses_safe_structured_fields(caplog) -> None:
    caplog.set_level(logging.INFO, logger="audit_core")

    log_dependency(
        correlation_id="c-dependency",
        dependency="DI",
        operation="status",
        result="success",
    )

    record = caplog.records[-1]
    assert record.getMessage() == "dependency_call"
    assert record.correlation_id == "c-dependency"
    assert record.dependency == "DI"
    assert record.operation == "status"
    assert record.result == "success"
