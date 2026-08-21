from __future__ import annotations

import json
import re
from pathlib import Path

from audit_core.authorization import AuthorizationError, authorize
from audit_core.main import create_app
from audit_core.security import Principal

_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
_UC02_ALLOWED_DELETE_PATHS = {
    # Phase-1 administrative hard-delete exceptions.
    "/v1/tenants/{}/dealers/{}",
    "/v1/tenants/{}/dealers/{}/outlets/{}",
    # Project assignment removal only; the global Security USER is preserved.
    "/v1/tenants/{}/role-mappings/{}",
}


def _normalize_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _public_delete_routes() -> set[str]:
    runtime_spec = create_app().openapi()
    return {
        _normalize_path(path)
        for path, path_item in runtime_spec["paths"].items()
        if path.startswith("/v1/")
        and any(
            method.lower() in _HTTP_METHODS and method.upper() == "DELETE"
            for method in path_item
        )
    }


def test_security_catalog_and_public_api_expose_only_approved_uc02_delete_routes() -> None:
    catalogue = json.loads(
        Path("design/AUDIT_CORE_SECURITY_CATALOG_v2.1.json").read_text(encoding="utf-8")
    )
    permission_keys = [permission["key"] for permission in catalogue["permissions"]]
    assert all("delete" not in key.lower() for key in permission_keys)
    assert all("purge" not in key.lower() for key in permission_keys)

    assert _public_delete_routes() == _UC02_ALLOWED_DELETE_PATHS


def test_permission_and_tenant_denials_remain_fail_closed() -> None:
    principal = Principal(
        subject="actor-release-test",
        tenant_id="tenant-a",
        permissions=("audit.journey.read",),
    )

    try:
        authorize(
            principal,
            tenant_id="tenant-b",
            permission="audit.journey.read",
        )
    except AuthorizationError as exc:
        assert exc.error_code == "VAC-AUTH-003"
    else:
        raise AssertionError("Cross-tenant authorization unexpectedly succeeded")

    try:
        authorize(
            principal,
            tenant_id="tenant-a",
            permission="audit.journey.update",
        )
    except AuthorizationError as exc:
        assert exc.error_code == "VAC-AUTH-002"
    else:
        raise AssertionError("Missing permission unexpectedly succeeded")