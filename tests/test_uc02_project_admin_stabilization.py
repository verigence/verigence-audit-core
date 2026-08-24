from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from audit_core import readiness
from audit_core.readiness import ReadinessCheck
from audit_core.uc02_project_admin_stabilization import _dependency_message


def _check(*, key: str, severity: str, status: str) -> ReadinessCheck:
    return ReadinessCheck(
        area="TEST",
        checkKey=key,
        severity=severity,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        message=key,
        targetTask="PROJECT_DETAILS",
    )


def test_blocking_readiness_failure_blocks_activation(monkeypatch) -> None:
    monkeypatch.setattr(readiness, "set_tenant_context", lambda *_: None)
    monkeypatch.setattr(readiness, "_project_state", lambda *_: object())
    monkeypatch.setattr(readiness, "_project_setup_check", lambda *_: _check(key="PROJECT", severity="BLOCKING", status="PASS"))
    monkeypatch.setattr(readiness, "_security_tenant_check", lambda **_: _check(key="SECURITY", severity="BLOCKING", status="PASS"))
    monkeypatch.setattr(readiness, "_dealer_outlet_structure_check", lambda *_: _check(key="STRUCTURE", severity="BLOCKING", status="PASS"))
    monkeypatch.setattr(readiness, "_pc_coverage_check", lambda *_: _check(key="PC", severity="BLOCKING", status="FAIL"))
    monkeypatch.setattr(readiness, "_project_masters_check", lambda *_: _check(key="MASTERS", severity="BLOCKING", status="PASS"))
    monkeypatch.setattr(readiness, "_di_project_check", lambda **_: _check(key="DI", severity="BLOCKING", status="PASS"))
    monkeypatch.setattr(readiness, "_optional_map_metadata_check", lambda *_: _check(key="MAP", severity="WARNING", status="FAIL"))

    result = readiness.evaluate_project_readiness(
        tenant_id="tenant-test",
        admin_request=SimpleNamespace(bearer_token="token"),
        connection=Mock(),
    )

    assert result.readyToActivate is False


def test_warning_does_not_block_activation(monkeypatch) -> None:
    monkeypatch.setattr(readiness, "set_tenant_context", lambda *_: None)
    monkeypatch.setattr(readiness, "_project_state", lambda *_: object())
    monkeypatch.setattr(readiness, "_project_setup_check", lambda *_: _check(key="PROJECT", severity="BLOCKING", status="PASS"))
    monkeypatch.setattr(readiness, "_security_tenant_check", lambda **_: _check(key="SECURITY", severity="BLOCKING", status="PASS"))
    monkeypatch.setattr(readiness, "_dealer_outlet_structure_check", lambda *_: _check(key="STRUCTURE", severity="BLOCKING", status="PASS"))
    monkeypatch.setattr(readiness, "_pc_coverage_check", lambda *_: _check(key="PC", severity="BLOCKING", status="PASS"))
    monkeypatch.setattr(readiness, "_project_masters_check", lambda *_: _check(key="MASTERS", severity="BLOCKING", status="PASS"))
    monkeypatch.setattr(readiness, "_di_project_check", lambda **_: _check(key="DI", severity="BLOCKING", status="PASS"))
    monkeypatch.setattr(readiness, "_optional_map_metadata_check", lambda *_: _check(key="MAP", severity="WARNING", status="FAIL"))

    result = readiness.evaluate_project_readiness(
        tenant_id="tenant-test",
        admin_request=SimpleNamespace(bearer_token="token"),
        connection=Mock(),
    )

    assert result.readyToActivate is True


def test_setup_delete_error_message_is_actionable() -> None:
    message = _dependency_message({"businessAssignments": 2, "customers": 1})
    assert "role mappings: 2" in message
    assert "customers: 1" in message
    assert "Remove the dependent setup first" in message
