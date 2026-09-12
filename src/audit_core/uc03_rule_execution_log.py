"""uc03_rule_execution_log.py — the unified rule Execution Log (Phase 2).

One row per rule per trigger invocation, whatever the outcome. This is the
piece neither rule system had recorded centrally before this platform: a
rule that PASSED, or was SKIPPED as not applicable, wrote nothing anywhere;
only a FAIL left a trace (an ``audit_findings`` row). ``rule_executions``
(migration 0085) is append-only (INSERT-only grant) -- an execution record
is never edited or deleted once written.

Today's one caller is ``uc03_rule_engine_findings.py::run_rule_engine_phase``,
recording outcomes for RULE_ENGINE-executor rules from the rule-engine's own
live ``readiness()`` response (that service remains the source of truth for
pass/fail/skip on its own rules; this module never re-derives applicability
itself). Phase 4 will add the analogous call for every AUDIT_CORE producer.
"""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from sqlalchemy import Connection, text

Outcome = Literal["PASS", "FAIL", "SKIPPED", "ERROR"]


def record_execution(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    rule_code: str,
    triggering_event: str,
    outcome: Outcome,
    reason: str | None = None,
    audit_finding_id: UUID | None = None,
    correlation_id: str | None = None,
) -> None:
    """Insert one Execution Log row. Never raises on a caller's behalf to
    swallow -- callers writing from a best-effort background path (like
    ``run_rule_engine_phase``) already wrap the whole flow in their own
    try/except; this stays a plain insert so a genuine DB failure surfaces
    there rather than being silently double-swallowed here."""
    connection.execute(
        text(
            """
            INSERT INTO auditcore.rule_executions (
                tenant_id, journey_id, rule_code, triggering_event,
                outcome, reason, audit_finding_id, correlation_id
            ) VALUES (
                :tenant_id, :journey_id, :rule_code, :triggering_event,
                :outcome, :reason, :audit_finding_id, :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "rule_code": rule_code,
            "triggering_event": triggering_event,
            "outcome": outcome,
            "reason": reason,
            "audit_finding_id": audit_finding_id,
            "correlation_id": correlation_id,
        },
    )


def record_executions_bulk(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    triggering_event: str,
    correlation_id: str | None,
    pass_rule_codes: tuple[str, ...] = (),
    fail_rule_codes: dict[str, UUID] | None = None,
    skipped_rule_codes: dict[str, str | None] | None = None,
) -> None:
    """Convenience wrapper for a whole phase's worth of rules at once --
    every rule the rule-engine considered ready (minus the ones that
    actually failed) is a PASS, every anomaly is a FAIL linked to the
    finding it raised, every notReady entry is a SKIPPED with the
    rule-engine's own reason."""
    for rule_code in pass_rule_codes:
        record_execution(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            rule_code=rule_code,
            triggering_event=triggering_event,
            outcome="PASS",
            correlation_id=correlation_id,
        )
    for rule_code, finding_id in (fail_rule_codes or {}).items():
        record_execution(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            rule_code=rule_code,
            triggering_event=triggering_event,
            outcome="FAIL",
            audit_finding_id=finding_id,
            correlation_id=correlation_id,
        )
    for rule_code, reason in (skipped_rule_codes or {}).items():
        record_execution(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            rule_code=rule_code,
            triggering_event=triggering_event,
            outcome="SKIPPED",
            reason=reason,
            correlation_id=correlation_id,
        )
