import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from audit_core.di_capture_v2_client import DiCaptureV2Error
from audit_core.errors import (
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
    install_error_handlers,
)
from audit_core.security import SecurityTokenError
from audit_core.security_integration import SecurityTokenUnavailableError


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/validation/{item_id}")
    def validation(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    @app.get("/auth")
    def auth() -> None:
        raise SecurityTokenError("raw token failure")

    @app.get("/service-token-unavailable")
    def service_token_unavailable() -> None:
        raise SecurityTokenUnavailableError("raw downstream token endpoint failure")

    @app.get("/not-found")
    def not_found() -> None:
        raise NotFoundError(
            error_code="VAC-NF-001",
            title="Project not found",
            detail="Project was not found.",
        )

    @app.get("/conflict")
    def conflict() -> None:
        raise ConflictError(
            error_code="VAC-CONFLICT-001",
            title="Version conflict",
            detail="The resource version changed.",
        )

    @app.get("/dependency")
    def dependency() -> None:
        raise DependencyUnavailableError(
            detail="Project administration is temporarily unavailable. Please try again."
        )

    @app.get("/system")
    def system() -> None:
        raise RuntimeError("sensitive internal failure")

    @app.get("/http-exception")
    def http_exception() -> None:
        raise HTTPException(status_code=422, detail="Unknown photo view FRONT.")

    @app.get("/lookup")
    def lookup() -> None:
        raise LookupError("Escalation not found or already resolved")

    @app.get("/key-error")
    def key_error() -> None:
        raise KeyError("sensitive-key")

    @app.get("/unique")
    def unique() -> None:
        raise IntegrityError("INSERT ...", {"name": "sensitive"}, _PgError("23505"))

    @app.get("/di-down")
    def di_down() -> None:
        raise DiCaptureV2Error(status_code=504, detail="timeout")

    @app.get("/di-rejected")
    def di_rejected() -> None:
        raise DiCaptureV2Error(status_code=422, detail='{"detail": "sensitive value"}')

    return app


class _PgError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__("duplicate key value violates unique constraint")
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    ("path", "status", "error_code"),
    [
        ("/validation/not-an-int", 400, "VAC-VAL-001"),
        ("/auth", 401, "VAC-AUTH-001"),
        ("/service-token-unavailable", 503, "VAC-SYS-002"),
        ("/not-found", 404, "VAC-NF-001"),
        ("/conflict", 409, "VAC-CONFLICT-001"),
        ("/dependency", 503, "VAC-SYS-002"),
        ("/system", 500, "VAC-SYS-001"),
        ("/http-exception", 422, "VAC-VAL-002"),
        ("/lookup", 404, "VAC-NF-000"),
        ("/key-error", 500, "VAC-SYS-001"),
        ("/unique", 409, "VAC-CONFLICT-000"),
        ("/di-down", 503, "VAC-DI-001"),
        ("/di-rejected", 422, "VAC-DI-002"),
        ("/no-such-route", 404, "VAC-NF-000"),
    ],
)
def test_errors_match_catalogue_contract(path: str, status: int, error_code: str) -> None:
    client = TestClient(_app(), raise_server_exceptions=False)
    response = client.get(path, headers={"X-Correlation-ID": "c-test"})

    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["errorCode"] == error_code
    assert body["correlationId"] == "c-test"
    assert body["type"] == f"urn:verigence:audit-core:error:{error_code}"
    assert "sensitive internal failure" not in body["detail"]


@pytest.mark.parametrize(
    ("path", "category", "retryable"),
    [
        ("/validation/not-an-int", "VALIDATION", False),
        ("/auth", "SECURITY", False),
        ("/not-found", "BUSINESS", False),
        ("/conflict", "BUSINESS", False),
        ("/dependency", "DEPENDENCY", True),
        ("/di-down", "DEPENDENCY", True),
        ("/system", "TECHNICAL", False),
    ],
)
def test_every_error_says_whether_it_is_business_or_technical(path: str, category: str, retryable: bool) -> None:
    body = TestClient(_app(), raise_server_exceptions=False).get(path).json()
    assert body["errorCategory"] == category
    assert body["retryable"] is retryable


def test_error_logs_carry_code_category_and_level_by_category() -> None:
    from structlog.testing import capture_logs

    client = TestClient(_app(), raise_server_exceptions=False)
    with capture_logs() as logs:
        client.get("/not-found", headers={"X-Correlation-ID": "c-1"})
        client.get("/system", headers={"X-Correlation-ID": "c-2"})
    business, technical = [e for e in logs if e["event"] == "api_error"]
    assert (business["log_level"], business["error_category"], business["correlation_id"]) == ("info", "BUSINESS", "c-1")
    assert "exc_stack" not in business
    assert (technical["log_level"], technical["error_category"]) == ("error", "TECHNICAL")
    assert technical["exc_type"] == "RuntimeError"
    assert any("test_errors.py" in frame for frame in technical["exc_stack"])
    assert "sensitive" not in repr(logs)


@pytest.mark.parametrize(
    ("http_status", "status", "error_code", "category"),
    [
        (401, 401, "VAC-AUTH-001", "SECURITY"),
        (403, 403, "VAC-AUTH-002", "SECURITY"),
        (404, 404, "VAC-NF-000", "BUSINESS"),
        (409, 409, "VAC-CONFLICT-000", "BUSINESS"),
        (422, 422, "VAC-VAL-002", "BUSINESS"),
        (502, 503, "VAC-SYS-002", "DEPENDENCY"),
        (None, 503, "VAC-SYS-002", "DEPENDENCY"),
    ],
)
def test_security_admin_failures_keep_what_security_answered(http_status, status, error_code, category) -> None:
    from audit_core.security_integration import SecurityAdminError

    app = FastAPI()
    install_error_handlers(app)

    @app.get("/admin")
    def admin() -> None:
        raise SecurityAdminError("Security administrative request failed", http_status=http_status)

    response = TestClient(app, raise_server_exceptions=False).get("/admin")
    assert response.status_code == status
    assert (response.json()["errorCode"], response.json()["errorCategory"]) == (error_code, category)


def test_signing_keys_outage_is_503_not_401() -> None:
    from jwt import PyJWKClientConnectionError

    from audit_core.security import SecurityKeysUnavailableError, SecurityTokenValidator

    class _DownJwks:
        def get_signing_key_from_jwt(self, token: str):
            raise PyJWKClientConnectionError("Fail to fetch data from the url")

    validator = SecurityTokenValidator(jwks_url="https://security/jwks", issuer="i", audience="a",
                                       jwks_client=_DownJwks())
    with pytest.raises(SecurityKeysUnavailableError):
        validator.validate("header.payload.signature")

    app = FastAPI()
    install_error_handlers(app)

    @app.get("/secure")
    def secure() -> None:
        validator.validate("header.payload.signature")

    response = TestClient(app, raise_server_exceptions=False).get("/secure")
    assert response.status_code == 503 and response.json()["errorCategory"] == "DEPENDENCY"
