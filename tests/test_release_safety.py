from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from audit_core.authorization import AuthorizationError, authorize
from audit_core.security import Principal


def _clean_process_public_methods() -> list[list[str]]:
    script = r'''
import json
from audit_core.main import app

print(json.dumps([
    sorted(route.methods)
    for route in app.routes
    if getattr(route, "path", "").startswith("/v1/")
    and getattr(route, "methods", None)
]))
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


def test_security_catalog_and_public_api_expose_no_destructive_delete_capability() -> None:
    catalogue = json.loads(
        Path("design/AUDIT_CORE_SECURITY_CATALOG_v2.1.json").read_text(encoding="utf-8")
    )
    permission_keys = [permission["key"] for permission in catalogue["permissions"]]
    assert all("delete" not in key.lower() for key in permission_keys)
    assert all("purge" not in key.lower() for key in permission_keys)

    public_methods = _clean_process_public_methods()
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
