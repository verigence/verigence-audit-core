from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

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


def _clean_process_route_metadata() -> list[dict]:
    script = r'''
import json
from fastapi.routing import APIRoute
from audit_core.main import app

rows = []
for route in app.routes:
    if not isinstance(route, APIRoute):
        continue
    rows.append({
        "path": route.path,
        "methods": sorted(route.methods),
        "headers": [
            {"alias": field.alias, "required": field.required}
            for field in route.dependant.header_params
        ],
    })
print(json.dumps(rows))
'''
    env = os.environ.copy()
    env.setdefault("APP_ENV", "test")
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _implemented_routes() -> dict[tuple[str, str], dict]:
    metadata = _clean_process_route_metadata()
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

    script = r'''
import json
from fastapi.testclient import TestClient
from audit_core.main import app

client = TestClient(app, raise_server_exceptions=False)
response = client.put(
    "/v1/tenants/tenant-contract/journeys/00000000-0000-0000-0000-000000000001/delivery",
    json={},
)
print(json.dumps({"status": response.status_code, "body": response.json()}))
'''
    env = os.environ.copy()
    env.setdefault("APP_ENV", "test")
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    response = json.loads(result.stdout)
    assert response["status"] == 400
    assert response["body"]["errorCode"] == "VAC-VAL-001"
