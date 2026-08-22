from fastapi.testclient import TestClient

import audit_core.main as audit_core_main
from audit_core.config import DEV_WEB_ORIGIN

REQUESTED_HEADERS = (
    "authorization,content-type,idempotency-key,if-match,"
    "x-correlation-id,x-trace-id"
)


def _client(monkeypatch) -> TestClient:
    # CORS behavior does not depend on production logging. Keeping these global
    # logging hooks out of the test prevents structlog logger-cache state from
    # leaking into the independent observability capture tests.
    monkeypatch.setattr(audit_core_main, "configure_logging", lambda settings: None)
    monkeypatch.setattr(audit_core_main, "install_observability", lambda app: None)
    return TestClient(audit_core_main.create_app())


def _preflight(client: TestClient, origin: str):
    return client.options(
        "/v1/projects",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": REQUESTED_HEADERS,
        },
    )


def test_dev_web_cors_preflight_allows_uc02_headers(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("AUDIT_CORE_CORS_ALLOWED_ORIGINS", raising=False)
    client = _client(monkeypatch)

    response = _preflight(client, DEV_WEB_ORIGIN)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == DEV_WEB_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"

    allowed_methods = {
        method.strip().upper()
        for method in response.headers["access-control-allow-methods"].split(",")
    }
    assert {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"} <= allowed_methods

    allowed_headers = {
        header.strip().lower()
        for header in response.headers["access-control-allow-headers"].split(",")
    }
    assert set(REQUESTED_HEADERS.split(",")) <= allowed_headers


def test_dev_web_cors_preflight_rejects_unapproved_origin(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("AUDIT_CORE_CORS_ALLOWED_ORIGINS", raising=False)
    client = _client(monkeypatch)

    response = _preflight(client, "https://unapproved.example")

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_non_dev_environment_has_no_implicit_dev_origin(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("AUDIT_CORE_CORS_ALLOWED_ORIGINS", raising=False)
    client = _client(monkeypatch)

    response = _preflight(client, DEV_WEB_ORIGIN)

    assert response.status_code == 405
    assert "access-control-allow-origin" not in response.headers


def test_explicit_origin_override_supports_future_environments(monkeypatch) -> None:
    configured_origin = "https://web.example.com"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUDIT_CORE_CORS_ALLOWED_ORIGINS", configured_origin)
    client = _client(monkeypatch)

    response = _preflight(client, configured_origin)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == configured_origin
