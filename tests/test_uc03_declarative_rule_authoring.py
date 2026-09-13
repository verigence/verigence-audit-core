"""test_uc03_declarative_rule_authoring.py — POST /declarative-rules
(Phase 5's third UI piece: authoring a new DECLARATIVE rule).

Calls the route function directly with the FastAPI dependency values as
plain kwargs, matching test_uc03_rule_registry.py's own convention for
this module's sibling endpoint -- no TestClient/auth-mock harness needed.
build_rule_engine_client / _build_security_oauth_client are monkeypatched
the same way test_uc03_rule_engine_findings.py does for the same two
dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from audit_core import uc03_declarative_rule_authoring as authoring
from audit_core.errors import ConflictError, DependencyUnavailableError, ValidationError
from audit_core.rule_engine_client import RuleEngineCreateResult
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import SecurityAuthorizationDecision
from audit_core.uc03_declarative_rule_authoring import (
    DeclarativeRuleCreate,
    create_declarative_rule,
)

TENANT = "tenant-rule-author-1"


@dataclass
class AllowedAuthorization:
    def check_user_permission(self, *, user_id: str, tenant_id: str, permission_key: str):
        return SecurityAuthorizationDecision(
            allowed=True, reason_code="AUTHORIZED", user_id=user_id,
            tenant_id=tenant_id, permission_key=permission_key, role_key=None,
        )


@dataclass
class DeniedAuthorization:
    def check_user_permission(self, *, user_id: str, tenant_id: str, permission_key: str):
        return SecurityAuthorizationDecision(
            allowed=False, reason_code="NOT_PERMITTED", user_id=user_id,
            tenant_id=tenant_id, permission_key=permission_key, role_key=None,
        )


class _FakeSecurityClient:
    def get_service_token(self, *, audience: str) -> str:
        return "svc-token"

    def close(self) -> None:
        pass


def _body(**overrides) -> DeclarativeRuleCreate:
    fields = {
        "ruleCode": "TEST_NEW_DECLARATIVE_RULE",
        "category": "PRICE",
        "comparator": "GT",
        "severity": "WARNING",
        "findingMessage": "msg",
    }
    fields.update(overrides)
    return DeclarativeRuleCreate(**fields)


def test_denied_permission_raises_authorization_error(monkeypatch) -> None:
    from audit_core.authorization import AuthorizationError

    with pytest.raises(AuthorizationError):
        create_declarative_rule(
            TENANT, _body(),
            human_principal=HumanPrincipal(subject="pc-1"),
            authorization_client=DeniedAuthorization(),
        )


def test_not_configured_raises_dependency_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(authoring, "build_rule_engine_client", lambda: None)

    with pytest.raises(DependencyUnavailableError):
        create_declarative_rule(
            TENANT, _body(),
            human_principal=HumanPrincipal(subject="pc-1"),
            authorization_client=AllowedAuthorization(),
        )


class _FakeClient:
    def __init__(self, result: RuleEngineCreateResult) -> None:
        self._result = result
        self.received_payload: dict | None = None

    def create_rule(self, *, token, tenant_id, payload):
        self.received_payload = payload
        return self._result

    def close(self) -> None:
        pass


def test_success_invalidates_cache_and_returns_created(monkeypatch) -> None:
    fake_client = _FakeClient(RuleEngineCreateResult(created=True, error_code=None, error_message=None))
    monkeypatch.setattr(authoring, "build_rule_engine_client", lambda: fake_client)
    monkeypatch.setattr(authoring, "_build_security_oauth_client", lambda: _FakeSecurityClient())

    invalidated = []
    monkeypatch.setattr(authoring, "invalidate_rule_engine_cache", lambda: invalidated.append(True))

    response = create_declarative_rule(
        TENANT, _body(),
        human_principal=HumanPrincipal(subject="pc-1"),
        authorization_client=AllowedAuthorization(),
    )

    assert response.created is True
    assert response.ruleCode == "TEST_NEW_DECLARATIVE_RULE"
    assert invalidated == [True]
    assert fake_client.received_payload["ruleCode"] == "TEST_NEW_DECLARATIVE_RULE"
    assert fake_client.received_payload["comparator"] == "GT"


def test_duplicate_raises_conflict_error(monkeypatch) -> None:
    fake_client = _FakeClient(
        RuleEngineCreateResult(created=False, error_code="DUPLICATE", error_message="already exists")
    )
    monkeypatch.setattr(authoring, "build_rule_engine_client", lambda: fake_client)
    monkeypatch.setattr(authoring, "_build_security_oauth_client", lambda: _FakeSecurityClient())

    with pytest.raises(ConflictError) as exc:
        create_declarative_rule(
            TENANT, _body(),
            human_principal=HumanPrincipal(subject="pc-1"),
            authorization_client=AllowedAuthorization(),
        )
    assert exc.value.status_code == 409


def test_validation_failure_raises_validation_error(monkeypatch) -> None:
    fake_client = _FakeClient(
        RuleEngineCreateResult(created=False, error_code="VALIDATION", error_message="bad comparator")
    )
    monkeypatch.setattr(authoring, "build_rule_engine_client", lambda: fake_client)
    monkeypatch.setattr(authoring, "_build_security_oauth_client", lambda: _FakeSecurityClient())

    with pytest.raises(ValidationError) as exc:
        create_declarative_rule(
            TENANT, _body(),
            human_principal=HumanPrincipal(subject="pc-1"),
            authorization_client=AllowedAuthorization(),
        )
    assert exc.value.status_code == 400


def test_unexpected_client_error_becomes_dependency_unavailable(monkeypatch) -> None:
    class _Boom:
        def create_rule(self, *, token, tenant_id, payload):
            raise RuntimeError("boom")

        def close(self) -> None:
            pass

    monkeypatch.setattr(authoring, "build_rule_engine_client", lambda: _Boom())
    monkeypatch.setattr(authoring, "_build_security_oauth_client", lambda: _FakeSecurityClient())

    with pytest.raises(DependencyUnavailableError):
        create_declarative_rule(
            TENANT, _body(),
            human_principal=HumanPrincipal(subject="pc-1"),
            authorization_client=AllowedAuthorization(),
        )
