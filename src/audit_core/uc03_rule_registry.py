"""uc03_rule_registry.py — the unified rule catalog (Definition), read surface.

Phase 1 of the unified rule-engine platform (see the session's design plan
for full context). One endpoint:

  GET /v1/tenants/{tenant_id}/uc03/rule-catalog

returns every rule the platform has, grouped by category, regardless of
which engine executes it:

  - audit-core's own CODE rows come straight from
    ``auditcore.rule_definitions`` (seeded by migration 0083 from this
    session's verified per-rule inventory).
  - the external rule-engine's DECLARATIVE rows are fetched live (through a
    short in-process TTL cache -- the exact pattern
    ``uc03_finding_classification.py::_load_registry`` already uses for
    ``finding_types``) from its own ``GET /audit/rules`` catalog and merged
    in. Never persisted back into ``rule_definitions`` by this read path --
    that write-through is a later phase; this endpoint's job is purely to
    show the unified list, always live for the rule-engine half.

Dormant gracefully: if ``RULE_ENGINE_BASE_URL`` isn't configured, or the
live call fails, the catalog still returns audit-core's own rules --
never blocks the whole catalog on one dependency.
"""
from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import DependencyUnavailableError
from audit_core.rule_engine_client import RuleEngineClient, build_rule_engine_client
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    SecurityAuthorizationError,
    get_security_authorization_client,
)
from audit_core.uc03_rule_engine_findings import (
    _DEFAULT_FINDING_TYPE,
    _DEFAULT_SEVERITY,
    _FINDING_TYPE_BY_CATEGORY,
    _SEVERITY_MAP,
    _build_security_oauth_client,
)

router = APIRouter(prefix="/v1/tenants/{tenant_id}/uc03", tags=["uc03-rule-registry"])

_PERMISSION_KEY = "audit.finding.read"

# finding_type_code -> the display category this catalog groups by. Every
# audit-core rule's category is stored directly on its rule_definitions row
# (migration 0083); the rule-engine's rows carry no such column at all
# (only its own internal "PRICE"/"DISCOUNT"/... short codes), so this maps
# through the SAME finding_type_code bridge uc03_rule_engine_findings.py
# already uses to materialize its anomalies -- not a new, second taxonomy.
_RULE_ENGINE_DISPLAY_CATEGORY: dict[str, str] = {
    "PRICING_ANOMALY": "Pricing & Commercial Anomalies",
    "DISCOUNT_ANOMALY": "Pricing & Commercial Anomalies",
    "ACCESSORY_ANOMALY": "Pricing & Commercial Anomalies",
    "INSURANCE_ANOMALY": "Pricing & Commercial Anomalies",
    "RTO_ANOMALY": "Pricing & Commercial Anomalies",
    "VEHICLE_IDENTITY_ANOMALY": "Vehicle & Model Resolution",
    "CUSTOMER_IDENTITY_CONCERN": "Customer & Dealer Identity",
    "PROCESS_NON_COMPLIANCE": "Manual / Human Observations",
    "CROSS_CASE_DUPLICATE": "Cross-Case / Fraud Detection",
}
_DEFAULT_DISPLAY_CATEGORY = "Cross-Case / Fraud Detection"

_RULE_ENGINE_CACHE_TTL_SECONDS = 300.0
_rule_engine_cache: tuple[float, list[dict[str, Any]]] | None = None

# Every rule-engine row is VIOLATION/ADJUDICATED (see the class comment
# above) -- same bound-action set migration 0084 backfilled for audit-core's
# own ADJUDICATED rows. Kept in sync with that migration and with
# uc03_finding_routing.py::permitted_actions's ADJUDICATED branch.
_ADJUDICATED_ACTIONS: list[str] = [
    "REMARK", "ACKNOWLEDGE", "CONFIRM_BREACH", "MARK_FALSE_POSITIVE", "RESOLVE",
]


def _authorize(
    client: SecurityAuthorizationClient,
    *,
    human_principal: HumanPrincipal,
    tenant_id: str,
) -> None:
    try:
        decision = client.check_user_permission(
            user_id=human_principal.subject,
            tenant_id=tenant_id,
            permission_key=_PERMISSION_KEY,
        )
    except SecurityAuthorizationError as exc:
        raise DependencyUnavailableError(
            detail="The rule catalog is temporarily unavailable. Please try again."
        ) from exc
    if not decision.allowed:
        from audit_core.authorization import AuthorizationError

        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )


class RuleCatalogEntry(BaseModel):
    ruleCode: str
    category: str
    title: str
    description: str | None
    executor: str
    executionKind: str
    triggerEvents: list[str]
    rerunPolicy: str
    findingClass: str | None
    defaultSeverity: str | None
    defaultOwnerRole: str | None
    resolutionMode: str | None
    boundActions: list[str]
    blockingCompletion: bool
    enabled: bool


class RuleCategoryGroup(BaseModel):
    category: str
    rules: list[RuleCatalogEntry]


class RuleCatalogResponse(BaseModel):
    groups: list[RuleCategoryGroup]
    ruleEngineReachable: bool


def _audit_core_rows(connection: Connection) -> list[RuleCatalogEntry]:
    rows = connection.execute(
        text(
            """
            SELECT rule_code, category, title, description, executor,
                   execution_kind, trigger_events, rerun_policy,
                   finding_class, default_severity, default_owner_role,
                   resolution_mode, bound_actions, blocking_completion, enabled
            FROM auditcore.rule_definitions
            WHERE executor = 'AUDIT_CORE'
            ORDER BY category, rule_code
            """
        )
    ).mappings().all()
    return [
        RuleCatalogEntry(
            ruleCode=row["rule_code"],
            category=row["category"],
            title=row["title"],
            description=row["description"],
            executor=row["executor"],
            executionKind=row["execution_kind"],
            triggerEvents=list(row["trigger_events"] or []),
            rerunPolicy=row["rerun_policy"],
            findingClass=row["finding_class"],
            defaultSeverity=row["default_severity"],
            defaultOwnerRole=row["default_owner_role"],
            resolutionMode=row["resolution_mode"],
            boundActions=list(row["bound_actions"] or []),
            blockingCompletion=row["blocking_completion"],
            enabled=row["enabled"],
        )
        for row in rows
    ]


def _rule_engine_rows(tenant_id: str) -> tuple[list[RuleCatalogEntry], bool]:
    """Live rule-engine catalog, through a short TTL cache -- matches
    uc03_finding_classification.py::_load_registry's own pattern for
    finding_types. Returns (rows, reachable): a dependency failure or the
    integration being unconfigured returns ([], False) rather than raising
    -- this catalog must never fail just because one of its two sources is
    momentarily unavailable.
    """
    global _rule_engine_cache
    now = time.monotonic()
    if _rule_engine_cache is not None and now - _rule_engine_cache[0] < _RULE_ENGINE_CACHE_TTL_SECONDS:
        cached = _rule_engine_cache[1]
        return [RuleCatalogEntry(**row) for row in cached], True

    client: RuleEngineClient | None = None
    security_client = None
    try:
        client = build_rule_engine_client()
        if client is None:
            return [], False
        security_client = _build_security_oauth_client()
        if security_client is None:
            return [], False
        from audit_core.rule_engine_client import RULE_ENGINE_AUDIENCE

        token = security_client.get_service_token(audience=RULE_ENGINE_AUDIENCE)
        engine_rules = client.list_rules(token=token, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 - never break the whole catalog over one dependency
        return [], False
    finally:
        if client is not None:
            client.close()
        if security_client is not None:
            security_client.close()

    entries: list[RuleCatalogEntry] = []
    for rule in engine_rules:
        finding_type = _FINDING_TYPE_BY_CATEGORY.get(
            (rule.category or "").upper(), _DEFAULT_FINDING_TYPE
        )
        display_category = _RULE_ENGINE_DISPLAY_CATEGORY.get(finding_type, _DEFAULT_DISPLAY_CATEGORY)
        entries.append(
            RuleCatalogEntry(
                ruleCode=rule.rule_code,
                category=display_category,
                title=rule.finding_message or rule.rule_code,
                description=rule.finding_message,
                executor="RULE_ENGINE",
                executionKind="DECLARATIVE",
                triggerEvents=(
                    ["BOOKING_REVIEW_CONFIRMED"] if "BOOKING" in rule.phases
                    else ["DELIVERY_COMPLETED"] if "DELIVERY" in rule.phases
                    else []
                ),
                rerunPolicy="ONCE",
                findingClass="VIOLATION",
                defaultSeverity=_SEVERITY_MAP.get((rule.severity or "").upper(), _DEFAULT_SEVERITY),
                defaultOwnerRole="TL",
                resolutionMode="ADJUDICATED",
                boundActions=_ADJUDICATED_ACTIONS,
                blockingCompletion=False,
                enabled=rule.enabled,
            )
        )
    _rule_engine_cache = (now, [entry.model_dump() for entry in entries])
    return entries, True


@router.get("/rule-catalog", response_model=RuleCatalogResponse)
def get_rule_catalog(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> RuleCatalogResponse:
    _authorize(authorization_client, human_principal=human_principal, tenant_id=tenant_id)

    all_rules = _audit_core_rows(connection)
    rule_engine_rows, reachable = _rule_engine_rows(tenant_id)
    all_rules.extend(rule_engine_rows)

    grouped: dict[str, list[RuleCatalogEntry]] = {}
    for entry in all_rules:
        grouped.setdefault(entry.category, []).append(entry)

    groups = [
        RuleCategoryGroup(category=category, rules=rules)
        for category, rules in sorted(grouped.items())
    ]
    return RuleCatalogResponse(groups=groups, ruleEngineReachable=reachable)
