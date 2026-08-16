from __future__ import annotations

import json
from pathlib import Path

from audit_core.authorization import AuthorizationError, authorize
from audit_core.main import create_app
from audit_core.security import Principal

_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def _public_methods() -> list[list[str]]:
    runtime_spec = create_app().openapi()
    return [
        [
            method.upper()
            for method in path_item
            if method.lower() in _HTTP_METHODS
        ]
        for path, path_item in runtime_spec["paths"].items()
        if path.startswith("/v1/")
    ]


def test_security_catalog_and_public_api_expose_no_destructive_delete_capability() -> None:
    catalogue = json.loads(
        Path("design/AUDIT_CORE_SECURITY_CATALOG_v2.1.json").read_text(encoding="utf-8")
    )
    permission_keys = [permission["key"] for permission in catalogue["permissions"]]
    assert all("delete" not in key.lower() for key in permission_keys)
    assert all("purge" not in key.lower() for key in permission_keys)

    public_methods = _public_methods()
    assert public_methods
    assert all("DELETE" not in methods for methods in public_methods)


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
