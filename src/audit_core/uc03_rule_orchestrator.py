"""uc03_rule_orchestrator.py — Trigger Registry (Phase 3, query half).

The plan's original Phase 3 pseudocode imagined every rule as a narrow,
independently-callable "producer function" that a generic dispatcher could
invoke by rule_code. Reading every one of audit-core's own producers before
writing this module shows that's only true for a minority of them
(``DOCUMENT_SYNCED``'s per-document sync pipeline genuinely is detachable
this way). Most of the rest -- ``start_delivery``, ``complete_delivery``,
``_delivery_audit_gaps`` -- ARE the business command itself: there is no
way to "fire the DELIVERY_COMPLETED trigger" independently of actually
completing a delivery, and several of them compute more than one rule's
worth of gaps in a single private helper, not one rule per call. Forcing a
generic per-rule callable registry over that shape would mean refactoring
every producer first -- a much larger, riskier change than "re-point the
dispatch", and indistinguishable from Phase 4's own instrumentation work.

So this module ships the honest, safe, genuinely useful slice of Phase 3
now: a Trigger Registry *query* -- "what rules are supposed to run on this
event" is answered from ``rule_definitions.trigger_events`` (already live
since Phase 1), not buried in fifteen files -- plus the ``rerun_policy``
enforcement a manual "run all applicable rules" action and a future
producer-instrumentation pass will both need: given a rule already fired
(a non-ERROR ``rule_executions`` row exists for it on this journey), a
``ONCE`` rule must not run again. Re-pointing actual call sites through a
unified dispatcher, and instrumenting every producer to report PASS/SKIP,
stay Phase 4's scope -- this module doesn't change any existing call site's
behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, text


@dataclass(frozen=True)
class TriggeredRule:
    rule_code: str
    category: str
    executor: str
    rerun_policy: str
    finding_class: str | None
    default_severity: str | None


def rules_for_event(connection: Connection, *, event: str) -> tuple[TriggeredRule, ...]:
    """Every enabled AUDIT_CORE rule whose ``trigger_events`` contains
    ``event`` -- the answer to "what runs on this event", as data, not code.
    (RULE_ENGINE rows are intentionally excluded: their own trigger wiring
    is phase-based, not this platform's named-event list, and
    ``uc03_rule_engine_findings.py`` already dispatches them.)
    """
    rows = connection.execute(
        text(
            """
            SELECT rule_code, category, executor, rerun_policy,
                   finding_class, default_severity
            FROM auditcore.rule_definitions
            WHERE executor = 'AUDIT_CORE'
              AND enabled = true
              AND :event = ANY(trigger_events)
            ORDER BY category, rule_code
            """
        ),
        {"event": event},
    ).mappings().all()
    return tuple(
        TriggeredRule(
            rule_code=row["rule_code"],
            category=row["category"],
            executor=row["executor"],
            rerun_policy=row["rerun_policy"],
            finding_class=row["finding_class"],
            default_severity=row["default_severity"],
        )
        for row in rows
    )


def has_already_run(
    connection: Connection, *, tenant_id: str, journey_id: UUID, rule_code: str
) -> bool:
    """True if this rule has a non-ERROR Execution Log row for this journey
    already -- the check a ``ONCE`` rule must pass before running again.
    ``ERROR`` rows don't count as "already run": a transient failure should
    still let the rule be tried again, unlike a real PASS/FAIL/SKIPPED."""
    return (
        connection.execute(
            text(
                """
                SELECT 1
                FROM auditcore.rule_executions
                WHERE tenant_id = :tenant_id
                  AND journey_id = :journey_id
                  AND rule_code = :rule_code
                  AND outcome <> 'ERROR'
                LIMIT 1
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "rule_code": rule_code},
        ).first()
        is not None
    )


def runnable_rules_for_event(
    connection: Connection, *, tenant_id: str, journey_id: UUID, event: str
) -> tuple[TriggeredRule, ...]:
    """``rules_for_event`` filtered by ``rerun_policy`` -- a ``RERUNNABLE``
    rule is always runnable; a ``ONCE`` rule only if it hasn't already run
    for this journey. This is the set a caller should actually (re)evaluate
    -- e.g. the future "Run All Applicable Rules" manual action -- as
    opposed to the full set for display/reporting purposes."""
    return tuple(
        rule
        for rule in rules_for_event(connection, event=event)
        if rule.rerun_policy != "ONCE"
        or not has_already_run(
            connection, tenant_id=tenant_id, journey_id=journey_id, rule_code=rule.rule_code
        )
    )
