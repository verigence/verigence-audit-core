"""uc03_declarative_rule_authoring.py — author a new DECLARATIVE rule
(Phase 5 of the rule-engine platform, the third and final UI piece).

  POST /v1/tenants/{tenant_id}/uc03/declarative-rules

The browser never talks to the rule-engine directly (see the platform
design doc) -- this endpoint is the one intermediary, proxying to the
rule-engine's own ``POST /v1/tenants/{t}/audit/rules`` (rule-engine PR #6)
via ``RuleEngineClient.create_rule``. Audit-core does not re-implement the
rule-engine's own enum/DSL validation (that schema is the source of truth
for what's a legal declarative rule) -- it only relays the two expected
business-rule outcomes (409 duplicate, 400 validation) as proper HTTP
errors instead of a generic 503, and invalidates the rule-catalog's
rule-engine cache on success so the new rule shows up immediately on
/rule-catalog instead of waiting out its TTL.

CODE rules (audit-core's own bespoke Python producers) are NOT authorable
here or anywhere else in the UI -- a new CODE rule always requires a
deploy. This endpoint's honest scope is declarative rules only, matching
the platform design's stated non-goal.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from audit_core.dependencies import get_human_principal
from audit_core.errors import ConflictError, DependencyUnavailableError, ValidationError
from audit_core.rule_engine_client import RULE_ENGINE_AUDIENCE, build_rule_engine_client
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    SecurityAuthorizationError,
    get_security_authorization_client,
)
from audit_core.uc03_rule_engine_findings import _build_security_oauth_client
from audit_core.uc03_rule_registry import invalidate_rule_engine_cache

router = APIRouter(prefix="/v1/tenants/{tenant_id}/uc03", tags=["uc03-rule-authoring"])

# Rule authoring has no dedicated Security permission of its own yet --
# reuses the same bar as raising a finding (audit.finding.create), the
# closest already-provisioned write permission in the audit domain. Worth
# a dedicated audit.rule.manage permission once Security provisions one;
# tracked, not blocking this endpoint.
_PERMISSION_KEY = "audit.finding.create"


def _authorize(
    client: SecurityAuthorizationClient, *, human_principal: HumanPrincipal, tenant_id: str
) -> None:
    try:
        decision = client.check_user_permission(
            user_id=human_principal.subject, tenant_id=tenant_id, permission_key=_PERMISSION_KEY
        )
    except SecurityAuthorizationError as exc:
        raise DependencyUnavailableError(
            detail="Rule authoring is temporarily unavailable. Please try again."
        ) from exc
    if not decision.allowed:
        from audit_core.authorization import AuthorizationError

        raise AuthorizationError(error_code="VAC-AUTH-002", status_code=403, title="Permission denied")


class DeclarativeRuleCreate(BaseModel):
    ruleCode: str
    category: str
    auditScope: str = "WITHIN_CASE"
    phases: list[str] = ["FULL"]
    leftDocType: str | None = None
    leftFieldKey: str | None = None
    leftAggregation: str = "SINGLE"
    rightDocType: str | None = None
    rightFieldKey: str | None = None
    rightAggregation: str = "SINGLE"
    rightConfigKey: str | None = None
    comparator: str
    threshold: float = 0
    severity: str
    findingMessage: str
    conditionExpression: str | None = None
    requiresBothDocs: bool = False
    enabled: bool = True


class DeclarativeRuleCreateResponse(BaseModel):
    created: bool
    ruleCode: str


@router.post("/declarative-rules", response_model=DeclarativeRuleCreateResponse, status_code=201)
def create_declarative_rule(
    tenant_id: str,
    body: DeclarativeRuleCreate,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
) -> DeclarativeRuleCreateResponse:
    _authorize(authorization_client, human_principal=human_principal, tenant_id=tenant_id)

    client = build_rule_engine_client()
    if client is None:
        raise DependencyUnavailableError(
            detail="The rule-engine integration is not configured."
        )
    security_client = _build_security_oauth_client()
    if security_client is None:
        client.close()
        raise DependencyUnavailableError(
            detail="The rule-engine integration is not configured."
        )

    try:
        token = security_client.get_service_token(audience=RULE_ENGINE_AUDIENCE)
        payload: dict[str, Any] = body.model_dump(exclude_none=True)
        result = client.create_rule(token=token, tenant_id=tenant_id, payload=payload)
    except DependencyUnavailableError:
        raise
    except Exception as exc:
        raise DependencyUnavailableError(
            detail="Could not reach the rule-engine. Please try again."
        ) from exc
    finally:
        client.close()
        security_client.close()

    if result.error_code == "DUPLICATE":
        raise ConflictError(
            error_code="VAC-RULE-001",
            title="Rule already exists",
            detail=result.error_message or f"Rule {body.ruleCode!r} already exists.",
        )
    if result.error_code == "VALIDATION":
        raise ValidationError(
            detail=result.error_message or "The rule-engine rejected this rule definition."
        )

    invalidate_rule_engine_cache()
    return DeclarativeRuleCreateResponse(created=True, ruleCode=body.ruleCode)
