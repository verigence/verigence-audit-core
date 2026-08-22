import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from audit_core.errors import (
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
    install_error_handlers,
)
from audit_core.security import SecurityTokenError


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/validation/{item_id}")
    def validation(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    @app.get("/auth")
    def auth() -> None:
        raise SecurityTokenError("raw token failure")

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

    return app


@pytest.mark.parametrize(
    ("path", "status", "error_code"),
    [
        ("/validation/not-an-int", 400, "VAC-VAL-001"),
        ("/auth", 401, "VAC-AUTH-001"),
        ("/not-found", 404, "VAC-NF-001"),
        ("/conflict", 409, "VAC-CONFLICT-001"),
        ("/dependency", 503, "VAC-SYS-002"),
        ("/system", 500, "VAC-SYS-001"),
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
