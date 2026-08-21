from __future__ import annotations

import re
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from audit_core.main import create_app

_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
_DELIVERY_PUT_PATH = "/v1/tenants/{}/journeys/{}/delivery"
_UC02_ALLOWED_DELETE_PATHS = {
    # Phase-1 administrative hard-delete exceptions.
    "/v1/tenants/{}/dealers/{}",
    "/v1/tenants/{}/dealers/{}/outlets/{}",
    # Assignment removal only: this never deletes the global Security USER.
    "/v1/tenants/{}/role-mappings/{}",
    # Approved Excel staging cleanup only; confirmed/published master data is preserved.
    "/v1/tenants/{}/project-master-imports/{}",
}


def _normalize_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _server_prefix(spec: dict) -> str:
    servers = spec.get("servers") or []
    if not servers:
        return ""
    url = str(servers[0].get("url", "")).rstrip("/")
    if not url or "://" in url:
        return ""
    return url


def _operations_from_spec(spec: dict) -> list[tuple[str, str, dict]]:
    prefix = _server_prefix(spec)
    operations: list[tuple[str, str, dict]] = []
    for path, path_item in spec["paths"].items():
        resolved_path = f"{prefix}{path}"
        for method, operation in path_item.items():
            if method.lower() in _HTTP_METHODS:
                operations.append(
                    (method.upper(), _normalize_path(resolved_path), operation)
                )
    return operations


def _implemented_routes() -> dict[tuple[str, str], dict]:
    runtime_spec = create_app().openapi()
    return {
        (method.upper(), _normalize_path(path)): operation
        for path, path_item in runtime_spec["paths"].items()
        for method, operation in path_item.items()
        if method.lower() in _HTTP_METHODS
    }


def _requires_idempotency(parameters: list[dict]) -> bool:
    return any(
        parameter.get("$ref") == "#/components/parameters/IdempotencyKey"
        or (
            parameter.get("in") == "header"
            and parameter.get("name", "").lower() == "idempotency-key"
            and parameter.get("required") is True
        )
        for parameter in parameters
    )


def _is_forbidden_di_boundary(path: str) -> bool:
    """Block direct DI exposure while allowing DI as a Project-Master owner value."""
    normalized = path.lower()
    if "/project-masters/di/" in normalized:
        return False
    return "/di/" in normalized or normalized.endswith("/di")


def test_openapi_operations_are_implemented_and_public_boundary_is_safe() -> None:
    spec = yaml.safe_load(Path("api/openapi-v1.yaml").read_text(encoding="utf-8"))
    implemented = _implemented_routes()

    missing = [
        (method, path)
        for method, path, _ in _operations_from_spec(spec)
        if (method, path) not in implemented
    ]
    assert missing == []

    public_routes = [
        (method, path)
        for method, path in implemented
        if path.startswith("/v1/")
    ]
    assert public_routes
    delete_paths = {path for method, path in public_routes if method == "DELETE"}
    assert delete_paths == _UC02_ALLOWED_DELETE_PATHS
    assert all(not _is_forbidden_di_boundary(path) for _, path in public_routes)
    assert all("/delivery/block" not in path.lower() for _, path in public_routes)
    assert all("/delivery/approve" not in path.lower() for _, path in public_routes)
    assert all("/delivery/stop" not in path.lower() for _, path in public_routes)


def test_required_openapi_idempotency_headers_are_enforced() -> None:
    spec = yaml.safe_load(Path("api/openapi-v1.yaml").read_text(encoding="utf-8"))
    implemented = _implemented_routes()

    for method, path, operation in _operations_from_spec(spec):
        if not _requires_idempotency(operation.get("parameters", [])):
            continue
        if method == "PUT" and path == _DELIVERY_PUT_PATH:
            continue

        runtime_operation = implemented[(method, path)]
        assert _requires_idempotency(runtime_operation.get("parameters", [])), (
            method,
            path,
        )

    client = TestClient(create_app(), raise_server_exceptions=False)
    response = client.put(
        "/v1/tenants/tenant-contract/journeys/00000000-0000-0000-0000-000000000001/delivery",
        json={},
    )
    assert response.status_code == 400
    assert response.json()["errorCode"] == "VAC-VAL-001"