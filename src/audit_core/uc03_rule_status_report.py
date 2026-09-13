"""uc03_rule_status_report.py — the Compliance Report's "Rule Status" tab.

  GET /v1/tenants/{tenant_id}/journeys/{journey_id}/uc03/rule-status

For one journey, every enabled rule (both executors) classified into
exactly one of three buckets:

  - EXECUTED       -- has a rule_executions row with outcome PASS/FAIL/ERROR
  - NOT_APPLICABLE -- has a rule_executions row with outcome SKIPPED
  - PENDING        -- no rule_executions row at all (its trigger event
                       hasn't happened for this journey yet)

A separate, lazily-fetched endpoint from the main compliance-report call
(not folded into ComplianceReportResponse) so opening the report never
pays this query's cost -- it only runs when the Rule Status tab is
actually opened, matching this module's own "add a dedicated, lightweight
endpoint" precedent (see uc03_compliance_report.py's module docstring).

Performance: three single-journey, index-backed reads, no live rule-engine
call beyond what the rule-catalog's own TTL cache already pays for --

  - rule_definitions: a full scan of a ~24-row global reference table
    (identical cost to what /rule-catalog already does every time).
  - rule_executions: DISTINCT ON (rule_code) ... WHERE tenant_id=:t AND
    journey_id=:j, served by ix_rule_executions_journey_rule
    (tenant_id, journey_id, rule_code, evaluated_at_utc DESC) -- built
    for exactly this "latest execution per rule for one journey" access
    pattern (see migration 0085's own comment).
  - audit_findings: WHERE tenant_id=:t AND journey_id=:j, served by the
    existing ix_audit_findings_journey (migration 0034) -- the same query
    shape uc03_compliance_report.py's own _findings() already runs.

No N+1: rule count and finding count are both bounded per journey, and
every list is built once, then joined in Python.

Known data-coverage gap, surfaced rather than hidden: only 7 of ~24
AUDIT_CORE rules write to rule_executions at all today (Phase 4 of the
platform is partial -- see rule_definitions.execution_log_instrumented,
migration 0088). For the other ~17, a genuine PASS or SKIP leaves no
trace here -- only a FAIL does, indirectly, as an audit_findings row.
Rather than call every one of those "Pending" indistinguishably from a
rule that's truly awaiting its trigger event, this endpoint cross-checks
audit_findings for a matching rule_key and reports EXECUTED (inferred)
with a note when one exists, and otherwise reports PENDING with a note
explaining the gap. RULE_ENGINE rows need no such handling -- Phase 2
already writes a row for every RULE_ENGINE rule on every phase call.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_compliance_report import (
    _authorize,
    _journey_scope,
    _require_business_scope,
)
from audit_core.uc03_rule_registry import _rule_engine_rows

router = APIRouter(prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}/uc03", tags=["uc03-rule-status"])

_RULE_ENGINE_KEY_PREFIX = "RE_"

_NOT_INSTRUMENTED_PENDING_NOTE = (
    "Not yet wired to the Execution Log -- this rule only records a trace "
    "when it raises a finding. Check Audit Flags for its actual outcome."
)
_NOT_INSTRUMENTED_INFERRED_NOTE = (
    "Inferred from an existing Audit Flag -- this rule doesn't write to the "
    "Execution Log directly yet."
)


class RuleStatusEntry(BaseModel):
    ruleCode: str
    category: str
    title: str
    executor: str
    severity: str | None
    status: str  # EXECUTED | PENDING | NOT_APPLICABLE
    outcome: str | None  # PASS | FAIL | ERROR -- only when status == EXECUTED
    reason: str | None  # SKIPPED reason -- only when status == NOT_APPLICABLE
    evaluatedAtUtc: datetime | None
    auditFindingId: UUID | None
    note: str | None  # set only for the AUDIT_CORE data-coverage gap above


class RuleStatusSummary(BaseModel):
    executed: int
    pending: int
    notApplicable: int


class RuleStatusResponse(BaseModel):
    generatedAtUtc: datetime
    summary: RuleStatusSummary
    rules: list[RuleStatusEntry]


def _audit_core_definitions(connection: Connection) -> list[dict]:
    rows = connection.execute(
        text(
            """
            SELECT rule_code, category, title, default_severity, execution_log_instrumented
            FROM auditcore.rule_definitions
            WHERE executor = 'AUDIT_CORE' AND enabled = true
            ORDER BY category, rule_code
            """
        )
    ).mappings().all()
    return [dict(row) for row in rows]


def _latest_executions(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> dict[str, dict]:
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (rule_code)
                   rule_code, outcome, reason, evaluated_at_utc, audit_finding_id
            FROM auditcore.rule_executions
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY rule_code, evaluated_at_utc DESC
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return {row["rule_code"]: dict(row) for row in rows}


def _finding_rule_keys(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[str]:
    rows = connection.execute(
        text(
            """
            SELECT rule_key
            FROM auditcore.audit_findings
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND subject_kind = 'JOURNEY' AND finding_status != 'VOIDED'
              AND rule_key IS NOT NULL
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalars().all()
    return list(rows)


def _has_matching_finding(rule_code: str, executor: str, finding_rule_keys: list[str]) -> bool:
    if executor == "RULE_ENGINE":
        target = f"{_RULE_ENGINE_KEY_PREFIX}{rule_code}"
        return any(key == target for key in finding_rule_keys)
    # AUDIT_CORE: exact match, or a per-instance suffix like
    # "PAYMENT_BANK_UNMATCHED:<payment_id>" (uc03_payment_reconciliation.py's
    # own rule_key format).
    return any(key == rule_code or key.startswith(f"{rule_code}:") for key in finding_rule_keys)


@router.get("/rule-status", response_model=RuleStatusResponse)
def get_rule_status(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> RuleStatusResponse:
    _authorize(authorization_client, human_principal=human_principal, tenant_id=tenant_id)
    set_tenant_context(connection, tenant_id)
    journey = _journey_scope(connection, tenant_id, journey_id)
    _require_business_scope(
        connection,
        tenant_id=tenant_id,
        actor_id=human_principal.subject,
        dealer_id=journey["dealer_id"],
        outlet_id=journey["outlet_id"],
    )

    audit_core_defs = _audit_core_definitions(connection)
    rule_engine_defs, _reachable = _rule_engine_rows(tenant_id)
    latest = _latest_executions(connection, tenant_id=tenant_id, journey_id=journey_id)
    finding_rule_keys = _finding_rule_keys(connection, tenant_id=tenant_id, journey_id=journey_id)

    entries: list[RuleStatusEntry] = []

    for row in audit_core_defs:
        rule_code = row["rule_code"]
        execution = latest.get(rule_code)
        if execution is not None:
            if execution["outcome"] == "SKIPPED":
                entries.append(RuleStatusEntry(
                    ruleCode=rule_code, category=row["category"], title=row["title"],
                    executor="AUDIT_CORE", severity=row["default_severity"],
                    status="NOT_APPLICABLE", outcome=None, reason=execution["reason"],
                    evaluatedAtUtc=execution["evaluated_at_utc"], auditFindingId=None, note=None,
                ))
            else:
                entries.append(RuleStatusEntry(
                    ruleCode=rule_code, category=row["category"], title=row["title"],
                    executor="AUDIT_CORE", severity=row["default_severity"],
                    status="EXECUTED", outcome=execution["outcome"], reason=None,
                    evaluatedAtUtc=execution["evaluated_at_utc"],
                    auditFindingId=execution["audit_finding_id"], note=None,
                ))
        elif not row["execution_log_instrumented"] and _has_matching_finding(
            rule_code, "AUDIT_CORE", finding_rule_keys
        ):
            entries.append(RuleStatusEntry(
                ruleCode=rule_code, category=row["category"], title=row["title"],
                executor="AUDIT_CORE", severity=row["default_severity"],
                status="EXECUTED", outcome="FAIL", reason=None,
                evaluatedAtUtc=None, auditFindingId=None,
                note=_NOT_INSTRUMENTED_INFERRED_NOTE,
            ))
        else:
            entries.append(RuleStatusEntry(
                ruleCode=rule_code, category=row["category"], title=row["title"],
                executor="AUDIT_CORE", severity=row["default_severity"],
                status="PENDING", outcome=None, reason=None,
                evaluatedAtUtc=None, auditFindingId=None,
                note=None if row["execution_log_instrumented"] else _NOT_INSTRUMENTED_PENDING_NOTE,
            ))

    for rule in rule_engine_defs:
        if not rule.enabled:
            continue
        execution = latest.get(rule.ruleCode)
        if execution is not None:
            if execution["outcome"] == "SKIPPED":
                entries.append(RuleStatusEntry(
                    ruleCode=rule.ruleCode, category=rule.category, title=rule.title,
                    executor="RULE_ENGINE", severity=rule.defaultSeverity,
                    status="NOT_APPLICABLE", outcome=None, reason=execution["reason"],
                    evaluatedAtUtc=execution["evaluated_at_utc"], auditFindingId=None, note=None,
                ))
            else:
                entries.append(RuleStatusEntry(
                    ruleCode=rule.ruleCode, category=rule.category, title=rule.title,
                    executor="RULE_ENGINE", severity=rule.defaultSeverity,
                    status="EXECUTED", outcome=execution["outcome"], reason=None,
                    evaluatedAtUtc=execution["evaluated_at_utc"],
                    auditFindingId=execution["audit_finding_id"], note=None,
                ))
        else:
            entries.append(RuleStatusEntry(
                ruleCode=rule.ruleCode, category=rule.category, title=rule.title,
                executor="RULE_ENGINE", severity=rule.defaultSeverity,
                status="PENDING", outcome=None, reason=None,
                evaluatedAtUtc=None, auditFindingId=None, note=None,
            ))

    summary = RuleStatusSummary(
        executed=sum(1 for e in entries if e.status == "EXECUTED"),
        pending=sum(1 for e in entries if e.status == "PENDING"),
        notApplicable=sum(1 for e in entries if e.status == "NOT_APPLICABLE"),
    )
    entries.sort(key=lambda e: (e.category, e.ruleCode))

    return RuleStatusResponse(
        generatedAtUtc=datetime.now(UTC), summary=summary, rules=entries,
    )
