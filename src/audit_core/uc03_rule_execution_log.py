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

CHANGES, NOT REPEATS (2026-10-29, rule storm). A rule's result is written only
when it differs from the last one written for that rule on that Journey
(outcome, reason, finding). Every control run used to write a row for every
rule -- ~660 rows per Journey per day, 60-65% of them the same SKIPPED again --
and the table had grown to a third of the database. The latest row per rule
still says what the rule last concluded and since when ("evaluated_at" is when
that result was first reached), so the status report and the controls read it
unchanged; the derived-control fingerprint (MAX(evaluated_at_utc)) now moves
only when a result changes.
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
    """Insert one Execution Log row, unless it repeats the rule's last result
    on this Journey (see the module note). Never raises on a caller's behalf to
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
            )
            SELECT CAST(:tenant_id AS varchar), CAST(:journey_id AS uuid), CAST(:rule_code AS varchar),
                   CAST(:triggering_event AS varchar), CAST(:outcome AS varchar), CAST(:reason AS text),
                   CAST(:audit_finding_id AS uuid), CAST(:correlation_id AS varchar)
            WHERE NOT EXISTS (
                SELECT 1 FROM (
                    SELECT outcome, reason, audit_finding_id
                    FROM auditcore.rule_executions
                    WHERE tenant_id = CAST(:tenant_id AS varchar) AND journey_id = CAST(:journey_id AS uuid)
                      AND rule_code = CAST(:rule_code AS varchar)
                    ORDER BY evaluated_at_utc DESC
                    LIMIT 1
                ) last
                WHERE last.outcome = CAST(:outcome AS varchar)
                  AND last.reason IS NOT DISTINCT FROM CAST(:reason AS text)
                  AND last.audit_finding_id IS NOT DISTINCT FROM CAST(:audit_finding_id AS uuid)
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


def record_from_summary(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    rule_code: str,
    triggering_event: str,
    result: dict,
    skipped_reason: str,
    correlation_id: str | None = None,
) -> None:
    """Convenience for the common DOCUMENT_SYNCED producer return shape --
    ``{"raised": int, "examined": int, ...}`` on success, ``{"error": True}``
    on the producer's own caught exception -- shared by every rule
    instrumented this way (Phase 4) so the per-call-site logic is identical
    instead of hand-rolled per rule:

      - ``error`` present                -> ERROR
      - ``examined`` is 0                -> SKIPPED (``skipped_reason``)
      - ``raised`` > 0                   -> FAIL
      - otherwise                        -> PASS

    A rule evaluated per-document (its own rule_key per instance, e.g.
    WRONG_DOCUMENT, DUPLICATE_RECEIPT) writes one summary row per
    invocation this way, not one per document -- the specific finding(s)
    stay reachable from ``audit_findings`` by rule_key prefix.
    """
    if result.get("error"):
        outcome: Outcome = "ERROR"
        reason: str | None = f"{rule_code} producer raised an exception"
    elif result.get("examined", 0) == 0:
        outcome = "SKIPPED"
        reason = skipped_reason
    elif result.get("raised", 0) > 0:
        outcome = "FAIL"
        reason = None
    else:
        outcome = "PASS"
        reason = None
    record_execution(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        rule_code=rule_code,
        triggering_event=triggering_event,
        outcome=outcome,
        reason=reason,
        correlation_id=correlation_id,
    )


def record_from_resolution(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    rule_code: str,
    triggering_event: str,
    result: dict,
    correlation_id: str | None = None,
) -> None:
    """Convenience for the sync_model_resolution*-style producer return
    shape -- ``{"resolved": True, ...}`` | ``{"raised": True, ...}`` |
    ``{"skipped": True, "reason": ...}`` | ``{"error": True}`` -- distinct
    from ``record_from_summary``'s raised/examined-COUNT convention
    (this one uses boolean flags, not counts):

      - ``error`` present   -> ERROR
      - ``raised`` truthy    -> FAIL
      - ``skipped`` truthy   -> SKIPPED (``reason`` if given, else generic)
      - otherwise (``resolved``) -> PASS
    """
    if result.get("error"):
        outcome: Outcome = "ERROR"
        reason: str | None = f"{rule_code} producer raised an exception"
    elif result.get("raised"):
        outcome = "FAIL"
        reason = None
    elif result.get("skipped"):
        outcome = "SKIPPED"
        reason = str(result.get("reason") or "not resolvable yet")
    else:
        outcome = "PASS"
        reason = None
    record_execution(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        rule_code=rule_code,
        triggering_event=triggering_event,
        outcome=outcome,
        reason=reason,
        correlation_id=correlation_id,
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
