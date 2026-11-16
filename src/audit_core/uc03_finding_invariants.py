"""Findings and tasks invariants (closing list item 7, product owner 2026-11-16).

Three rules hold for every machine finding of a Journey, checked after every
evaluation of its checks and, for what is on file, by the worker job
``FINDING_INVARIANTS``:

1. A finding follows its check's latest result. It closes when
   - the check no longer exists (retired: its code is not a check any more),
   - it was raised by the Rule Engine and the Rule Engine is off for the
     Project, or the check now runs in Audit Core,
   - the check passes or does not apply now,
   - the check cannot decide on the documents on file now (it is raised
     again, with current figures, if it fails then) -- except the checks
     whose waiting is itself the ask (``WAITING_IS_THE_ASK``),
   - a person already decided it on its task (a Team Lead's verdict, or an
     exception accepted): the finding closes with that decision,
   - its task was cancelled (a check retired for tasks, a cleanup): no task
     will come back for it, so the finding closes too,
   - the check raises no task by a product owner decision (cash, the
     booking form): it is informational, recorded and closed.
2. Every finding left open has exactly one open task: the one the check
   raised is linked to it, a missing one is raised, and a second one for the
   same finding is cancelled.
3. An open task whose finding has closed closes too, unless its check's
   waiting is the ask (the booking date to correct).

Human findings (raised by a person from Findings) are not touched. Nothing
here decides a check: it only makes findings and tasks agree with the
results the checks already wrote to the control ledger (p2_control_state).
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_registry import Registry, get_registry

logger = structlog.get_logger(__name__)

# Task statuses that count as the finding's open task.
OPEN_TASK = ("READY", "IN_PROGRESS", "RETURNED", "ACTION_COMPLETED", "VERIFYING", "AWAITING_REQUESTER_REVIEW")
RULE_ENGINE_PREFIX = "RE_"
# The Rule Engine's executor in the registry.
_EXTERNAL = "EXTERNAL_RULE_ENGINE"


@dataclass(frozen=True)
class Decision:
    action: str          # KEEP | CLOSE
    reason: str          # a fixed word, counted in reports
    note: str            # what the finding's event says
    disposition: str | None = None


KEEP = Decision("KEEP", "KEEP", "")


def base_code(rule_key: str | None) -> tuple[str, bool]:
    """(the check's code, raised by the Rule Engine) for a finding's rule key:
    "DOCUMENT_MISSING:<id>" is DOCUMENT_MISSING, "RE_GATE_PASS_MISSING" is the
    Rule Engine's GATE_PASS_MISSING."""
    code = str(rule_key or "").split(":", 1)[0]
    if code.startswith(RULE_ENGINE_PREFIX):
        return code[len(RULE_ENGINE_PREFIX):], True
    return code, False


def taskless_codes() -> frozenset[str]:
    """Checks that raise no task by a product owner decision: their findings
    are informational. CASH_ABOVE_LIMIT keeps its finding (the legal limit,
    shown on the Audit Summary, 2026-10-31)."""
    from audit_core.uc03_p2_task_producer import (
        BOOKING_FORM_TASK_CODES,
        INFORMATIONAL_TASK_CODES,
        SUPPRESSED_TASK_CODES,
    )

    return frozenset((SUPPRESSED_TASK_CODES - {"CASH_ABOVE_LIMIT"}) | BOOKING_FORM_TASK_CODES
                     | INFORMATIONAL_TASK_CODES)


def decide(
    *,
    rule_key: str | None,
    origin_kind: str | None,
    control_status: str | None,
    person_decision: dict[str, Any] | None,
    rule_engine_on: bool,
    task_cancelled: dict[str, Any] | None = None,
    registry: Registry,
) -> Decision:
    """What happens to one open finding (pure)."""
    from audit_core.uc03_p2_task_producer import WAITING_IS_THE_ASK

    if origin_kind != "MACHINE" or not rule_key:
        return KEEP
    code, by_rule_engine = base_code(rule_key)
    control = registry.controls.get(code)
    if control is None:
        return Decision("CLOSE", "CHECK_RETIRED", "This check no longer runs; its finding is closed.")
    if by_rule_engine and not rule_engine_on:
        return Decision("CLOSE", "RULE_ENGINE_OFF", "The Rule Engine is off for this Project; its finding is closed.")
    if by_rule_engine and control.executor != _EXTERNAL:
        return Decision("CLOSE", "MOVED_TO_AUDIT_CORE",
                        "This check now runs in Audit Core; the Rule Engine's finding is closed.")
    if code in taskless_codes():
        return Decision("CLOSE", "INFORMATIONAL",
                        "Informational: this check raises no task (product owner decision); recorded and closed.")
    if control_status in ("PASS", "NOT_APPLICABLE"):
        return Decision("CLOSE", "CHECK_PASSES", "The check passes now.", "FIXED")
    if control_status == "WAITING_FOR_FACTS" and code not in WAITING_IS_THE_ASK:
        return Decision("CLOSE", "CHECK_CANNOT_DECIDE",
                        "The check cannot decide on the documents on file now; it is raised again if it fails.")
    if person_decision:
        outcome = str(person_decision.get("verdict") or person_decision.get("outcome") or "")
        disposition = "CONFIRMED_BREACH" if outcome == "CONFIRMED_BREACH" else "FALSE_POSITIVE"
        who = person_decision.get("verdictBy") or person_decision.get("acceptedBy") or "a Team Lead"
        return Decision("CLOSE", "DECIDED_ON_TASK",
                        f"Decided on its task by {who} ({outcome.replace('_', ' ').lower() or 'accepted'}).",
                        disposition)
    if task_cancelled is not None:
        why = str(task_cancelled.get("outcome") or task_cancelled.get("reason") or "cancelled")
        return Decision("CLOSE", "TASK_CANCELLED", f"Its task was cancelled ({why}); the finding is closed.")
    return KEEP


def finding_may_open(connection: Connection, *, tenant_id: str, journey_id: UUID, code: str) -> bool:
    """Whether a failing check may open a finding: not for a check that raises
    no task, nor while its task is cancelled or a person decided it -- the
    invariants would close such a finding straight away, and every rerun
    would open another."""
    if code in taskless_codes():
        return False
    row = connection.execute(
        text("SELECT task_status, completion_result FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k"),
        {"t": tenant_id, "k": f"control:{journey_id}:{code}"},
    ).mappings().one_or_none()
    if row is None:
        return True
    result = dict(row["completion_result"] or {})
    if row["task_status"] == "CANCELLED":
        return False
    return not (row["task_status"] == "VERIFIED_COMPLETE" and (result.get("verdict") or result.get("acceptedIssueHash")))


def _rule_engine_on(connection: Connection, tenant_id: str) -> bool:
    return connection.execute(
        text("SELECT rule_engine_enabled FROM auditcore.tenant_rule_config WHERE tenant_id=:t"), {"t": tenant_id},
    ).scalar_one_or_none() is not False


def _close_finding(connection: Connection, *, tenant_id: str, finding: dict[str, Any], decision: Decision) -> bool:
    updated = connection.execute(
        text(
            """
            UPDATE auditcore.audit_findings
            SET finding_status='RESOLVED', disposition=:disposition, resolution_reason=:note,
                resolved_at_utc=now(), resolved_by_actor_id=NULL, updated_at_utc=now()
            WHERE tenant_id=:t AND audit_finding_id=:f AND finding_status IN ('OPEN','ACKNOWLEDGED')
            """
        ),
        {"t": tenant_id, "f": finding["audit_finding_id"], "disposition": decision.disposition,
         "note": decision.note[:1000]},
    )
    if updated.rowcount != 1:
        return False
    connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_finding_events (
                tenant_id, audit_finding_id, journey_id, stage_code, event_type, actor_id, actor_role_snapshot,
                safe_payload, correlation_id
            ) VALUES (:t, :f, :j, :stage, 'RESOLVED', NULL, 'SYSTEM', CAST(:payload AS jsonb), NULL)
            """
        ),
        {"t": tenant_id, "f": finding["audit_finding_id"], "j": finding["journey_id"],
         "stage": finding["stage_code"],
         "payload": json.dumps({"reason": decision.reason, "note": decision.note,
                                "disposition": decision.disposition, "invariant": True})},
    )
    return True


def _close_task(connection: Connection, *, tenant_id: str, task: dict[str, Any], outcome: str, reason: str,
                status: str) -> None:
    from audit_core.uc03_p2_tasks import record_task_event

    result = {"outcome": outcome, "reason": reason, "closedAt": datetime.now(UTC).isoformat()}
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks
            SET task_status=:status, completion_result=COALESCE(completion_result,'{}'::jsonb) || CAST(:r AS jsonb),
                verified_at_utc=CASE WHEN :verified THEN now() ELSE verified_at_utc END,
                updated_at_utc=now()
            WHERE tenant_id=:t AND task_id=:id
            """
        ),
        {"t": tenant_id, "id": task["task_id"], "status": status, "r": json.dumps(result),
         "verified": status == "VERIFIED_COMPLETE"},
    )
    record_task_event(connection, tenant_id=tenant_id, journey_id=UUID(str(task["journey_id"])),
                      task_id=UUID(str(task["task_id"])),
                      event_type="CANCELLED" if status == "CANCELLED" else "MACHINE_VERIFICATION_PASS",
                      actor_id="SYSTEM", actor_role_code="SYSTEM",
                      details={**result, "sourceCode": task.get("source_code"),
                               "previousStatus": task.get("task_status")})


def _link_task(connection: Connection, *, tenant_id: str, task_id: Any, finding_id: Any) -> None:
    connection.execute(
        text("UPDATE auditcore.p2_tasks SET reference = COALESCE(reference,'{}'::jsonb) || "
             "jsonb_build_object('findingId', CAST(:f AS text)) WHERE tenant_id=:t AND task_id=:id"),
        {"t": tenant_id, "id": task_id, "f": str(finding_id)},
    )


def _raise_task(connection: Connection, *, tenant_id: str, journey_id: UUID, code: str, finding: dict[str, Any],
                state: dict[str, Any] | None, registry: Registry) -> str | None:
    """Raise the check's task for an open finding that has none, through the
    one path the checks raise tasks by."""
    from audit_core.uc03_p2_controls import Transition
    from audit_core.uc03_p2_task_producer import apply_control_transitions

    control = registry.controls.get(code)
    if control is None or not control.task_type:
        return None
    transition = Transition(
        control_code=code, previous=None, current="FAIL",
        stage=(state or {}).get("stage_code") or finding.get("stage_code"),
        details=dict((state or {}).get("details") or {}),
        finding_id=UUID(str(finding["audit_finding_id"])),
    )
    counts = apply_control_transitions(connection, tenant_id=tenant_id, journey_id=journey_id,
                                       transitions=[transition], evaluation_started_at=None, registry=registry)
    return next(iter(counts), None)


def enforce_journey(
    connection: Connection, *, tenant_id: str, journey_id: UUID, commit: bool = True,
    registry: Registry | None = None, rule_engine_on: bool | None = None,
) -> Counter:
    """Make one Journey's findings and tasks agree with its checks' results.
    Returns what was (or, without ``commit``, would be) done, by reason."""
    registry = registry or get_registry()
    if rule_engine_on is None:
        rule_engine_on = _rule_engine_on(connection, tenant_id)
    counts: Counter = Counter()
    findings = [dict(r) for r in connection.execute(
        text(
            """
            SELECT audit_finding_id, journey_id, rule_key, origin_kind, stage_code, finding_status
            FROM auditcore.audit_findings
            WHERE tenant_id=:t AND journey_id=:j AND finding_status IN ('OPEN','ACKNOWLEDGED')
            ORDER BY created_at_utc
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()]
    states = {str(r["control_code"]): dict(r) for r in connection.execute(
        text("SELECT control_code, control_status, stage_code, details FROM auditcore.p2_control_state "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()}
    tasks = [dict(r) for r in connection.execute(
        text(
            """
            SELECT task_id, journey_id, source_code, dedupe_key, task_status, reference, completion_result,
                   created_at_utc
            FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j AND source_type='RULE'
            ORDER BY created_at_utc
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()]
    open_tasks = [t for t in tasks if t["task_status"] in OPEN_TASK]

    def tasks_for(finding: dict[str, Any], code: str) -> list[dict[str, Any]]:
        fid = str(finding["audit_finding_id"])
        key = f"control:{journey_id}:{code}"
        return [t for t in open_tasks
                if (t["reference"] or {}).get("findingId") == fid
                or ((t["reference"] or {}).get("findingId") in (None, "") and t["dedupe_key"] == key)]

    def control_task(code: str) -> dict[str, Any] | None:
        key = f"control:{journey_id}:{code}"
        return next((t for t in tasks if t["dedupe_key"] == key), None)

    closed: set[str] = set()
    for finding in findings:
        code, _ = base_code(finding["rule_key"])
        state = states.get(code)
        mine = tasks_for(finding, code)
        own = None if mine else control_task(code)
        result = dict((own or {}).get("completion_result") or {})
        decided = (own is not None and own["task_status"] == "VERIFIED_COMPLETE"
                   and bool(result.get("verdict") or result.get("acceptedIssueHash")))
        decision = decide(
            rule_key=finding["rule_key"], origin_kind=finding["origin_kind"],
            control_status=(state or {}).get("control_status"),
            person_decision=result if decided else None,
            rule_engine_on=rule_engine_on, registry=registry,
            task_cancelled=result if own is not None and own["task_status"] == "CANCELLED" else None,
        )
        if decision.action == "CLOSE":
            counts[f"finding:{decision.reason}"] += 1
            closed.add(str(finding["audit_finding_id"]))
            if commit:
                _close_finding(connection, tenant_id=tenant_id, finding=finding, decision=decision)
            for task in mine:
                counts[f"task:closed_with_finding:{decision.reason}"] += 1
                if commit:
                    _close_task(connection, tenant_id=tenant_id, task=task, outcome=decision.reason,
                                reason=decision.note,
                                status="VERIFIED_COMPLETE" if decision.reason == "CHECK_PASSES" else "CANCELLED")
            continue
        if finding["origin_kind"] != "MACHINE" or not finding["rule_key"]:
            continue
        if not mine:
            counts["task:raised_for_open_finding"] += 1
            if commit:
                _raise_task(connection, tenant_id=tenant_id, journey_id=journey_id, code=code, finding=finding,
                            state=state, registry=registry)
            continue
        keep, *extra = sorted(mine, key=lambda t: (t["dedupe_key"] != f"control:{journey_id}:{code}",
                                                   t["created_at_utc"]))
        if (keep["reference"] or {}).get("findingId") != str(finding["audit_finding_id"]):
            counts["task:linked_to_finding"] += 1
            if commit:
                _link_task(connection, tenant_id=tenant_id, task_id=keep["task_id"],
                           finding_id=finding["audit_finding_id"])
        for task in extra:
            counts["task:duplicate_cancelled"] += 1
            if commit:
                _close_task(connection, tenant_id=tenant_id, task=task, outcome="DUPLICATE_TASK",
                            reason="The finding already has its task.", status="CANCELLED")

    # Rule 3: an open task whose finding has closed.
    from audit_core.uc03_p2_task_producer import WAITING_IS_THE_ASK

    finding_ids = {str(t["reference"]["findingId"]) for t in open_tasks
                   if (t["reference"] or {}).get("findingId")}
    if finding_ids:
        statuses = {str(r["audit_finding_id"]): str(r["finding_status"]) for r in connection.execute(
            text("SELECT audit_finding_id, finding_status FROM auditcore.audit_findings "
                 "WHERE tenant_id=:t AND audit_finding_id = ANY(CAST(:ids AS uuid[]))"),
            {"t": tenant_id, "ids": sorted(finding_ids)},
        ).mappings().all()}
        for task in open_tasks:
            fid = str((task["reference"] or {}).get("findingId") or "")
            if not fid or fid in closed or statuses.get(fid) in (None, "OPEN", "ACKNOWLEDGED"):
                continue
            if task["source_code"] in WAITING_IS_THE_ASK:
                continue
            counts["task:finding_already_closed"] += 1
            if commit:
                _close_task(connection, tenant_id=tenant_id, task=task, outcome="FINDING_CLOSED",
                            reason="The finding behind this task is closed.", status="CANCELLED")
    return counts


def enforce_after_evaluation(engine: Engine, *, tenant_id: str, journey_id: UUID) -> None:
    """Run after each evaluation of a Journey's checks; never fails the evaluation."""
    try:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            counts = enforce_journey(connection, tenant_id=tenant_id, journey_id=journey_id)
        if counts:
            logger.info("finding_invariants", tenant_id=tenant_id, journey_id=str(journey_id), **counts)
    except Exception:
        logger.warning("finding_invariants_failed", tenant_id=tenant_id, journey_id=str(journey_id), exc_info=True)


def run_finding_invariants(engine: Engine, tag: str, tenant_ids: list[str]) -> dict[str, Any]:
    """Worker job FINDING_INVARIANTS: every Journey on file. A dry run (counts
    only) unless the tag ends with "-commit"."""
    commit = tag.endswith("-commit")
    registry = get_registry()
    report: dict[str, Any] = {"commit": commit, "tenants": {}}
    for tenant_id in tenant_ids:
        totals: Counter = Counter()
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                rule_engine_on = _rule_engine_on(connection, tenant_id)
                journeys = connection.execute(
                    text(
                        """
                        SELECT DISTINCT journey_id FROM auditcore.audit_findings
                        WHERE tenant_id=:t AND journey_id IS NOT NULL AND finding_status IN ('OPEN','ACKNOWLEDGED')
                        UNION
                        SELECT DISTINCT journey_id FROM auditcore.p2_tasks
                        WHERE tenant_id=:t AND source_type='RULE' AND task_status = ANY(:open)
                        """
                    ),
                    {"t": tenant_id, "open": list(OPEN_TASK)},
                ).scalars().all()
        except Exception:
            logger.warning("finding_invariants_tenant_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        for journey_id in journeys:
            try:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    totals.update(enforce_journey(connection, tenant_id=tenant_id, journey_id=journey_id,
                                                  commit=commit, registry=registry, rule_engine_on=rule_engine_on))
            except Exception:
                totals["failed"] += 1
                logger.warning("finding_invariants_item_failed", tag=tag, tenant_id=tenant_id,
                               journey_id=str(journey_id), exc_info=True)
        totals["journeys"] = len(journeys)
        report["tenants"][tenant_id] = dict(totals)
        logger.info("finding_invariants_finished", tag=tag, tenant_id=tenant_id, commit=commit, **totals)
    return report


__all__ = [
    "base_code",
    "decide",
    "enforce_after_evaluation",
    "enforce_journey",
    "finding_may_open",
    "run_finding_invariants",
    "taskless_codes",
]
