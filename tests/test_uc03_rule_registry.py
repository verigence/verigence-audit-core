"""uc03_rule_registry.py has no pre-existing endpoint-level test suite (no
TestClient/auth-mock harness established for it); this adds focused coverage
for the new GET /rule-catalog path at the same direct-function-call level as
test_uc03_review_queue_daily_ops.py. rule_definitions is global reference
data seeded by migration 0083 itself, so no journey/customer fixtures are
needed -- just a live connection against a migrated DATABASE_URL.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.authorization import AuthorizationError
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import SecurityAuthorizationDecision
from audit_core.uc03_rule_registry import get_rule_catalog


@dataclass
class AllowedAuthorization:
    def check_user_permission(
        self, *, user_id: str, tenant_id: str, permission_key: str
    ) -> SecurityAuthorizationDecision:
        return SecurityAuthorizationDecision(
            allowed=True,
            reason_code="AUTHORIZED",
            user_id=user_id,
            tenant_id=tenant_id,
            permission_key=permission_key,
            role_key=None,
        )


@dataclass
class DeniedAuthorization:
    def check_user_permission(
        self, *, user_id: str, tenant_id: str, permission_key: str
    ) -> SecurityAuthorizationDecision:
        return SecurityAuthorizationDecision(
            allowed=False,
            reason_code="NOT_PERMITTED",
            user_id=user_id,
            tenant_id=tenant_id,
            permission_key=permission_key,
            role_key=None,
        )


@pytest.fixture
def rule_registry_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    tenant_id = f"tenant-rr-{uuid4().hex[:10]}"
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        yield tenant_id, c
    engine.dispose()


def test_rule_catalog_returns_seeded_audit_core_rules_grouped_by_category(
    rule_registry_setup,
) -> None:
    tenant_id, connection = rule_registry_setup

    response = get_rule_catalog(
        tenant_id,
        human_principal=HumanPrincipal(subject="pc-1"),
        authorization_client=AllowedAuthorization(),
        connection=connection,
    )

    assert response.ruleEngineReachable is False  # RULE_ENGINE_BASE_URL unset in tests
    assert len(response.groups) > 1

    identity_group = next(
        g for g in response.groups if g.category == "Customer & Dealer Identity"
    )
    wrong_document = next(r for r in identity_group.rules if r.ruleCode == "WRONG_DOCUMENT")
    assert wrong_document.executor == "AUDIT_CORE"
    assert wrong_document.executionKind == "CODE"
    assert wrong_document.rerunPolicy == "RERUNNABLE"
    assert wrong_document.findingClass == "VIOLATION"
    assert "DOCUMENT_SYNCED" in wrong_document.triggerEvents

    all_rule_codes = {rule.ruleCode for group in response.groups for rule in group.rules}
    assert "DL_VIN_RECONCILIATION" in all_rule_codes
    once_rule = next(
        r
        for group in response.groups
        for r in group.rules
        if r.ruleCode == "DL_VIN_RECONCILIATION"
    )
    assert once_rule.rerunPolicy == "ONCE"


def test_rule_catalog_denies_without_permission(rule_registry_setup) -> None:
    tenant_id, connection = rule_registry_setup

    with pytest.raises(AuthorizationError) as exc:
        get_rule_catalog(
            tenant_id,
            human_principal=HumanPrincipal(subject="pc-1"),
            authorization_client=DeniedAuthorization(),
            connection=connection,
        )

    assert exc.value.status_code == 403
    assert exc.value.error_code == "VAC-AUTH-002"
