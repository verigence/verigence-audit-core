import pytest

from audit_core.authorization import AuthorizationError, authorize
from audit_core.security import Principal


def _principal() -> Principal:
    return Principal(
        subject="user-123",
        tenant_id="tenant-123",
        permissions=("audit.journey.read",),
    )


def test_authorize_allows_matching_tenant_and_permission() -> None:
    authorize(_principal(), tenant_id="tenant-123", permission="audit.journey.read")


def test_authorize_rejects_tenant_mismatch() -> None:
    with pytest.raises(AuthorizationError) as exc_info:
        authorize(_principal(), tenant_id="tenant-999", permission="audit.journey.read")

    assert exc_info.value.error_code == "VAC-AUTH-003"
    assert exc_info.value.status_code == 403


def test_authorize_rejects_missing_permission() -> None:
    with pytest.raises(AuthorizationError) as exc_info:
        authorize(_principal(), tenant_id="tenant-123", permission="audit.journey.write")

    assert exc_info.value.error_code == "VAC-AUTH-002"
    assert exc_info.value.status_code == 403
