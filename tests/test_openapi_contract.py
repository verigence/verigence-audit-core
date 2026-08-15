from __future__ import annotations

import re
from pathlib import Path

import yaml
from fastapi.routing import APIRoute

from audit_core.main import create_app

_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


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


def _api_routes() -> list[APIRoute]:
    application = create_app()
    return [route for route in application.routes if isinstance(route, APIRoute)]


def test_openapi_operations_are_implemented_and_public_boundary_is_safe() -> None:
    spec = yaml.safe_load(Path("api/openapi-v1.yaml").read_text(encoding="utf-8"))
    api_routes = _api_routes()
    implemented = {
        (method, _normalize_path(route.path)): route
        for route in api_routes
        for method in route.methods
        if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }

    missing = [
        (method, path)
        for method, path, _ in _operations_from_spec(spec)
        if (method, path) not in implemented
    ]
    assert missing == []

    public_routes = [route for route in api_routes if route.path.startswith("/v1/")]
    assert public_routes
    assert all("DELETE" not in route.methods for route in public_routes)
    assert all("/di/" not in route.path.lower() for route in public_routes)
    assert all(not route.path.lower().endswith("/di") for route in public_routes)
    assert all("/delivery/block" not in route.path.lower() for route in public_routes)
    assert all("/delivery/approve" not in route.path.lower() for route in public_routes)
    assert all("/delivery/stop" not in route.path.lower() for route in public_routes)


def test_required_openapi_idempotency_headers_are_enforced_by_routes() -> None:
    spec = yaml.safe_load(Path("api/openapi-v1.yaml").read_text(encoding="utf-8"))
    api_routes = _api_routes()
    implemented = {
        (method, _normalize_path(route.path)): route
        for route in api_routes
        for method in route.methods
        if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }

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
        if not requires_idempotency:
            continue

        route = implemented[(method, path)]
        idempotency_params = [
            field
            for field in route.dependant.header_params
            if field.alias.lower() == "idempotency-key"
        ]
        assert len(idempotency_params) == 1, (method, path)
        assert idempotency_params[0].required is True, (method, path)
