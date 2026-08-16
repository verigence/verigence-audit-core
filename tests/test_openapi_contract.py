from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from audit_core.main import create_app

_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
_DELIVERY_PUT_PATH = "/v1/tenants/{}/journeys/{}/delivery"


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


def _fresh_route_metadata() -> list[dict]:
    app = create_app()
    rows = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        dependant = getattr(route, "dependant", None)
        header_params = getattr(dependant, "header_params", []) if dependant is not None else []
        rows.append(
            {
                "path": path,
                "methods": sorted(methods),
                "headers": [
                    {"alias": field.alias, "required": field.required}
                    for field in header_params
                ],
            }
        )
    return rows


def _implemented_routes() -> dict[tuple[str, str], dict]:
    metadata = _fresh_route_metadata()
    return {
        (method, _normalize_path(route["path"])): route
        for route in metadata
        for method in route["methods"]
        if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }


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
    assert all(method != "DELETE" for method, _ in public_routes)
    assert all("/di/" not in path.lower() for _, path in public_routes)
    assert all(not path.lower().endswith("/di") for _, path in public_routes)
    assert all("/delivery/block" not in path.lower() for _, path in public_routes)
    assert all("/delivery/approve" not in path.lower() for _, path in public_routes)
    assert all("/delivery/stop" not in path.lower() for _, path in public_routes)


def test_required_openapi_idempotency_headers_are_enforced() -> None:
    spec = yaml.safe_load(Path("api/openapi-v1.yaml").read_text(encoding="utf-8"))
    implemented = _implemented_routes()

    for method, path, operation in _operations_from_spec(spec):
        parameters = operation.get("parameters", [])
        requires_idempotency = any(
            parameter.get("$ref") == "#/components/parameters/IdempotencyKey"
            or (
                parameter.get("in") == "header"
                and parameter.get("name", "").lower() == "idempotency-key"
                and parameter.get("required") is True
            )
            for parameter in parameters
        )
        if not requires_idempotency or (method == "PUT" and path == _DELIVERY_PUT_PATH):
            continue

        route = implemented[(method, path)]
        idempotency_params = [
            field
            for field in route["headers"]
            if field["alias"].lower() == "idempotency-key"
        ]
        assert len(idempotency_params) == 1, (method, path)
        assert idempotency_params[0]["required"] is True, (method, path)

    client = TestClient(create_app(), raise_server_exceptions=False)
    response = client.put(
        "/v1/tenants/tenant-contract/journeys/00000000-0000-0000-0000-000000000001/delivery",
        json={},
    )
    body = json.loads(response.content)
    assert response.status_code == 400
    assert body["errorCode"] == "VAC-VAL-001"
