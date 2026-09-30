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


def test_every_request_logs_its_response_time_once(monkeypatch) -> None:
    """Decision 2026-09-30: one timing line per request, with the response
    time and where it went; health probes excluded."""
    from audit_core.observability import add_timing

    app = _app()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/tenants/{tenant_id}/things")
    def things(tenant_id: str) -> dict[str, str]:
        add_timing("db_ms", 4.0, count_key="db_queries")
        return {"tenant": tenant_id}

    client = TestClient(app, raise_server_exceptions=False)
    with capture_logs() as logs:
        assert client.get("/health").status_code == 200
        assert client.get("/v1/tenants/t-1/things", headers={CORRELATION_HEADER: "c-success"}).status_code == 200

    lines = [event for event in logs if str(event.get("event", "")).startswith("http_request")]
    assert len(lines) == 1
    [line] = lines
    assert line["event"] == "http_request" and line["route"] == "/v1/tenants/{tenant_id}/things"
    assert line["status_code"] == 200 and line["duration_ms"] >= 0
    assert (line["db_ms"], line["db_queries"], line["tenant_id"]) == (4.0, 1, "t-1")


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


def test_slow_requests_report_where_the_time_went(monkeypatch) -> None:
    from audit_core import observability
    from audit_core.observability import add_timing

    monkeypatch.setattr(observability, "_slow_request_threshold_ms", lambda: 0.0)
    app = FastAPI()
    install_error_handlers(app)
    install_observability(app)

    @app.get("/work")
    def work() -> dict[str, str]:
        add_timing("db_ms", 12.5, count_key="db_queries")
        add_timing("db_ms", 7.5, count_key="db_queries")
        add_timing("outbound_ms", 30.0, count_key="outbound_calls")
        return {"status": "ok"}

    with capture_logs() as logs:
        TestClient(app).get("/work")
    [slow] = [event for event in logs if event["event"] == "http_request_slow"]
    assert (slow["db_ms"], slow["db_queries"]) == (20.0, 2)
    assert (slow["outbound_ms"], slow["outbound_calls"]) == (30.0, 1)
    assert slow["route"] == "/work"


def test_parallel_admin_requests_ask_security_once(monkeypatch) -> None:
    import threading
    import time

    from audit_core import dependencies
    from audit_core.security import HumanPrincipal
    from audit_core.security_integration import SecurityAdminContext

    calls: list[str] = []

    class _Client:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def get_admin_context(self, *, human_bearer_token: str) -> SecurityAdminContext:
            calls.append(human_bearer_token)
            time.sleep(0.2)
            return SecurityAdminContext(user_id="u-parallel", is_super_admin=True, admin_scopes=())

    monkeypatch.setenv("SECURITY_BASE_URL", "https://security.example")
    monkeypatch.setattr(dependencies, "SecurityAdminClient", _Client)
    dependencies._admin_context_cache.pop("u-parallel", None)
    principal = HumanPrincipal(subject="u-parallel")
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(
            dependencies._security_admin_context(bearer_token="t", human_principal=principal)))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 4 and len(calls) == 1
