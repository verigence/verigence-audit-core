from __future__ import annotations

import time

import pytest

from audit_core.authorization import AuthorizationError
from audit_core.errors import DependencyUnavailableError
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationDecision,
    SecurityAuthorizationError,
)
from audit_core.uc03_journey_search import _authorize_read_and_check_full_contact

_DELAY_SECONDS = 0.2


class _SlowAuthorizationClient:
    """Each permission check pays its own delay, like a real network round trip."""

    def __init__(self, *, allowed: bool = True, raise_on: str | None = None) -> None:
        self.allowed = allowed
        self.raise_on = raise_on
        self.calls: list[str] = []

    def check_user_permission(self, *, user_id: str, tenant_id: str, permission_key: str):
        self.calls.append(permission_key)
        time.sleep(_DELAY_SECONDS)
        if permission_key == self.raise_on:
            raise SecurityAuthorizationError("Security service unavailable")
        return SecurityAuthorizationDecision(
            allowed=self.allowed,
            reason_code="AUTHORIZED" if self.allowed else "DENIED",
            user_id=user_id,
            tenant_id=tenant_id,
            permission_key=permission_key,
            role_key=None,
        )


def _principal() -> HumanPrincipal:
    return HumanPrincipal(subject="pc-user-1")


def test_both_checks_run_concurrently_not_sequentially() -> None:
    # Regression test for a live production timeout: Journey Overview used to
    # run these two independent Security calls one after another, each
    # paying its own full network round trip on a cache miss -- observed
    # live as the whole read exceeding the web client's 10s timeout. Two
    # ~0.2s calls run concurrently should take ~0.2s total, not ~0.4s.
    client = _SlowAuthorizationClient(allowed=True)
    started = time.monotonic()

    result = _authorize_read_and_check_full_contact(
        client, human_principal=_principal(), tenant_id="tenant-1"
    )

    elapsed = time.monotonic() - started
    assert result is True
    assert elapsed < _DELAY_SECONDS * 1.8, "checks ran sequentially, not concurrently"
    assert set(client.calls) == {"audit.journey.read", "audit.customer.contact.full.read"}


def test_permission_denied_propagates_without_waiting_for_the_other_check() -> None:
    client = _SlowAuthorizationClient(allowed=False)

    with pytest.raises(AuthorizationError):
        _authorize_read_and_check_full_contact(
            client, human_principal=_principal(), tenant_id="tenant-1"
        )


def test_security_service_failure_on_the_read_permission_still_raises() -> None:
    client = _SlowAuthorizationClient(raise_on="audit.journey.read")

    with pytest.raises(DependencyUnavailableError):
        _authorize_read_and_check_full_contact(
            client, human_principal=_principal(), tenant_id="tenant-1"
        )


def test_security_service_failure_on_full_contact_check_degrades_to_false() -> None:
    # Matches _can_read_full_contact's own existing behavior: a failure on
    # this specific check is non-fatal, it just means masked contact info.
    client = _SlowAuthorizationClient(raise_on="audit.customer.contact.full.read")

    result = _authorize_read_and_check_full_contact(
        client, human_principal=_principal(), tenant_id="tenant-1"
    )
    assert result is False
