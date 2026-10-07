"""Phase 2 control ledger: one authoritative state per Journey control.

Audit Core owns the state of every control in ``p2_templates/control_templates.yaml``
regardless of who evaluates it (blueprint v2.2 section 21). Controls are
evaluated in *units* so each executor is called once per Journey change:

    NATIVE:<stage>       Audit Core native runner (RERUN controls)
    RULE_ENGINE:<stage>  external Rule Engine phase + readiness (EXTERNAL)
    P2:DERIVED           stage gates (GATE), page outcomes (PAGES), the existing
                         document sync (SYNC) and process events (EVENT)

States follow blueprint 8.2 with no silent skip:
    WAITING_FOR_FACTS  inputs/evidence not available yet (or event not reached)
    PASS / FAIL        evaluated against the current facts
    NOT_APPLICABLE     the business condition does not apply
    RETRY_PENDING      technical failure; the unit is retried durably
    ERROR_TERMINAL     retries exhausted or executor not configured

A unit whose fact fingerprint has not changed since its last successful run
is skipped, so bursts of fact changes cost one evaluation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_registry import ControlTemplate, Registry, get_registry
from audit_core.uc03_p2_runtime import enqueue_work, fact_fingerprint, record_activity

logger = structlog.get_logger(__name__)

UNITS = ("NATIVE:BOOKING", "NATIVE:DELIVERY", "RULE_ENGINE:BOOKING", "RULE_ENGINE:DELIVERY", "P2:DERIVED")
_OUTCOME_TO_STATE = {"PASS": "PASS", "FAIL": "FAIL", "SKIPPED": "WAITING_FOR_FACTS", "ERROR": "RETRY_PENDING"}


class ControlUnitError(RuntimeError):
    """A unit could not be evaluated for technical reasons; retry durably."""


@dataclass(frozen=True)
class Transition:
    control_code: str
    previous: str | None
    current: str
    stage: str | None
    details: dict[str, Any]
    finding_id: UUID | None


# ------------------------------------------------------------------ requests

def delivery_active(connection: Connection, *, tenant_id: str, journey_id: UUID) -> bool:
    return bool(
        connection.execute(
            text(
                """
                SELECT EXISTS (
                  SELECT 1 FROM auditcore.evidence e
                  WHERE e.tenant_id=:tenant_id AND e.journey_id=:journey_id
                    AND e.association_status='ACTIVE'
                    AND upper(COALESCE(e.process_area,''))='DELIVERY'
                ) OR EXISTS (
                  SELECT 1 FROM auditcore.p2_journey_runtime r
                  WHERE r.tenant_id=:tenant_id AND r.journey_id=:journey_id
                    AND r.current_stage LIKE 'DELIVERY_%'
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
    )


def request_control_evaluation(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    correlation_id: str | None = None,
    delay_seconds: int = 3,
    force: bool = False,
) -> list[str]:
    """Coalesce evaluation of every unit relevant to the Journey's stage."""
    units = ["NATIVE:BOOKING", "RULE_ENGINE:BOOKING", "P2:DERIVED"]
    if delivery_active(connection, tenant_id=tenant_id, journey_id=journey_id):
        units[2:2] = ["NATIVE:DELIVERY", "RULE_ENGINE:DELIVERY"]
    for unit in units:
        enqueue_work(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            work_type="CONTROL_EVALUATE",
            work_key=f"unit:{journey_id}:{unit}",
            payload={"unit": unit, "force": force},
            correlation_id=correlation_id,
            delay_seconds=delay_seconds,
        )
    return units


# -------------------------------------------------------------------- ledger

def write_control_state(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    control: ControlTemplate,
    status: str,
    stage: str | None,
    reason: str | None,
    details: dict[str, Any] | None = None,
    finding_id: UUID | None = None,
    fact_version: int | None = None,
) -> Transition:
    previous = connection.execute(
        text(
            """
            SELECT control_status FROM auditcore.p2_control_state
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND control_code=:code
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "code": control.code},
    ).scalar_one_or_none()
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_control_state (
                tenant_id, journey_id, control_code, executor_type, control_mode,
                control_status, stage_code, evaluated_fact_version, finding_id,
                status_reason, last_error, details, last_evaluated_at_utc,
                evaluation_count, status_changed_at_utc, updated_at_utc
            ) VALUES (
                :tenant_id, :journey_id, :code, :executor, :mode,
                CAST(:status AS varchar), :stage, :fact_version, :finding_id,
                CAST(:reason AS text),
                CASE WHEN CAST(:status AS varchar) IN ('RETRY_PENDING','ERROR_TERMINAL')
                     THEN CAST(:reason AS text) END,
                CAST(:details AS jsonb), now(), 1, now(), now()
            )
            ON CONFLICT (tenant_id, journey_id, control_code) DO UPDATE SET
                executor_type=EXCLUDED.executor_type,
                control_mode=EXCLUDED.control_mode,
                control_status=EXCLUDED.control_status,
                stage_code=EXCLUDED.stage_code,
                evaluated_fact_version=EXCLUDED.evaluated_fact_version,
                finding_id=EXCLUDED.finding_id,
                status_reason=EXCLUDED.status_reason,
                last_error=EXCLUDED.last_error,
                details=EXCLUDED.details,
                last_evaluated_at_utc=now(),
                evaluation_count=auditcore.p2_control_state.evaluation_count + 1,
                status_changed_at_utc=CASE
                  WHEN auditcore.p2_control_state.control_status IS DISTINCT FROM EXCLUDED.control_status
                  THEN now() ELSE auditcore.p2_control_state.status_changed_at_utc END,
                updated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "code": control.code,
            "executor": "EXTERNAL_RULE_ENGINE" if control.mode == "EXTERNAL"
            else ("NATIVE" if control.mode == "RERUN" else "P2"),
            "mode": control.mode,
            "status": status,
            "stage": stage,
            "fact_version": fact_version,
            "finding_id": finding_id,
            "reason": reason,
            "details": json.dumps(details or {}, default=str),
        },
    )
    if previous != status:
        record_activity(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="CONTROL_CHANGED",
            subject_type="CONTROL",
            subject_id=control.code,
            details={"from": previous, "to": status, "stage": stage, "reason": reason},
        )
    return Transition(control.code, previous, status, stage, details or {}, finding_id)


def _fact_version(connection: Connection, *, tenant_id: str, journey_id: UUID) -> int | None:
    value = connection.execute(
        text("SELECT fact_version FROM auditcore.p2_journey_runtime WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one_or_none()
    return int(value) if value is not None else None


def _latest_executions(
    connection: Connection, *, tenant_id: str, journey_id: UUID, codes: list[str],
) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (rule_code) rule_code, outcome, reason, audit_finding_id, evaluated_at_utc
            FROM auditcore.rule_executions
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND rule_code = ANY(:codes)
            ORDER BY rule_code, evaluated_at_utc DESC
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "codes": codes},
    ).mappings().all()
    return {str(row["rule_code"]): dict(row) for row in rows}


def _finding_summary(connection: Connection, *, tenant_id: str, finding_id: UUID | None) -> dict[str, Any]:
    if finding_id is None:
        return {}
    row = connection.execute(
        text(
            """
            SELECT audit_finding_id, title, description, expected_summary, observed_summary,
                   severity, finding_status
            FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND audit_finding_id=:finding_id
            """
        ),
        {"tenant_id": tenant_id, "finding_id": finding_id},
    ).mappings().one_or_none()
    if row is None:
        return {}
    return {
        "findingTitle": row["title"],
        "findingDescription": row["description"],
        "expected": row["expected_summary"],
        "observed": row["observed_summary"],
        "findingSeverity": row["severity"],
        "findingStatus": row["finding_status"],
    }


def _open_finding_for(connection: Connection, *, tenant_id: str, journey_id: UUID,
                      code: str) -> UUID | None:
    """The open finding a native rule raised, when the execution log did not
    link it (the legacy runner records executions without a finding id).
    Rule keys are the control code, or code:<qualifier> for per-pair rules
    such as DUPLICATE_BOOKING:<other journey>."""
    value = connection.execute(
        text(
            """
            SELECT audit_finding_id FROM auditcore.audit_findings
            WHERE tenant_id=:t AND journey_id=:j AND finding_status IN ('OPEN','ACKNOWLEDGED')
              AND (rule_key=:code OR rule_key LIKE :prefix OR finding_type_code=:code)
            ORDER BY CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END,
                     created_at_utc DESC
            LIMIT 1
            """
        ),
        {"t": tenant_id, "j": journey_id, "code": code, "prefix": f"{code}:%"},
    ).scalar_one_or_none()
    return UUID(str(value)) if value is not None else None


def _raised_payload(connection: Connection, *, tenant_id: str, finding_id: UUID | None) -> dict[str, Any]:
    if finding_id is None:
        return {}
    payload = connection.execute(
        text(
            """
            SELECT safe_payload FROM auditcore.audit_finding_events
            WHERE tenant_id=:t AND audit_finding_id=:f
            ORDER BY occurred_at_utc LIMIT 1
            """
        ),
        {"t": tenant_id, "f": finding_id},
    ).scalar_one_or_none()
    return dict(payload) if isinstance(payload, dict) else {}


def _resolve_stale_rule_engine_findings(connection: Connection, *, tenant_id: str, journey_id: UUID,
                                        stage: str, codes: list[str], correlation_id: str) -> int:
    """A Rule Engine anomaly that is no longer reported is fixed: close its
    finding so Findings, the compliance report and the legacy queue agree
    with the control ledger."""
    if not codes:
        return 0
    from audit_core.uc03_manual_verification import _resolve_finding
    from audit_core.uc03_rule_engine_findings import _RULE_KEY_PREFIX

    rows = connection.execute(
        text(
            """
            SELECT audit_finding_id FROM auditcore.audit_findings
            WHERE tenant_id=:t AND journey_id=:j AND finding_status IN ('OPEN','ACKNOWLEDGED')
              AND rule_key = ANY(:keys)
            """
        ),
        {"t": tenant_id, "j": journey_id, "keys": [f"{_RULE_KEY_PREFIX}{c}" for c in codes]},
    ).scalars().all()
    for finding_id in rows:
        _resolve_finding(
            connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage,
            finding_id=finding_id, actor_id=None, correlation_id=correlation_id,
            note="The Rule Engine no longer reports this anomaly.",
        )
    return len(rows)


def _native_details(connection: Connection, *, tenant_id: str, control: ControlTemplate,
                    finding_id: UUID | None) -> dict[str, Any]:
    """Structured facts a reviewer needs from a native finding's payload."""
    payload = _raised_payload(connection, tenant_id=tenant_id, finding_id=finding_id)
    if not payload:
        return {}
    if control.code == "DUPLICATE_BOOKING":
        return {
            "matchBasis": payload.get("matchBasis"),
            "matchConfidencePercent": payload.get("matchConfidencePercent"),
            "matchConfidenceLabel": payload.get("matchConfidenceLabel"),
            "otherJourneyId": payload.get("otherJourneyId") or payload.get("believedOriginalJourneyId"),
            "believedOriginalJourneyId": payload.get("believedOriginalJourneyId"),
            "originalityBasis": payload.get("originalityBasis"),
        }
    keep = ("leftValue", "rightValue", "expected", "observed", "variance", "standardNet", "actualNet")
    return {k: payload[k] for k in keep if k in payload}


_DISCOUNT_CONDITIONS = frozenset({"corporateDiscount", "exchangeBenefit", "scrappageClaimed"})


# ----------------------------------------------------------------- executors

def _evaluate_native(engine: Engine, *, tenant_id: str, journey_id: UUID, stage: str,
                     correlation_id: str, registry: Registry) -> list[Transition]:
    from audit_core.uc03_run_all_rules import _run_audit_core_rules_for_stage

    controls = [c for c in registry.controls_by_mode("RERUN") if c.applies_to_stage(stage)]
    transitions: list[Transition] = []
    errors: list[str] = []
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        results = _run_audit_core_rules_for_stage(
            connection, tenant_id=tenant_id, journey_id=journey_id, stage=stage,
            correlation_id=correlation_id,
        )
        by_code = {r.ruleCode: r for r in results}
        executions = _latest_executions(
            connection, tenant_id=tenant_id, journey_id=journey_id, codes=[c.code for c in controls],
        )
        version = _fact_version(connection, tenant_id=tenant_id, journey_id=journey_id)
        for control in controls:
            result = by_code.get(control.code)
            if result is None:
                # The runner evaluates some controls once per Journey (BOOKING).
                continue
            status = _OUTCOME_TO_STATE.get(result.outcome, "RETRY_PENDING")
            execution = executions.get(control.code) or {}
            finding_id = execution.get("audit_finding_id") if status == "FAIL" else None
            if status == "FAIL" and finding_id is None:
                finding_id = _open_finding_for(
                    connection, tenant_id=tenant_id, journey_id=journey_id, code=control.code,
                )
            reason = execution.get("reason") or getattr(result, "reason", None)
            if status == "RETRY_PENDING":
                errors.append(control.code)
                reason = reason or "The Audit Core check could not complete; it will be retried."
            transitions.append(
                write_control_state(
                    connection, tenant_id=tenant_id, journey_id=journey_id, control=control,
                    status=status, stage=stage, reason=reason, finding_id=finding_id,
                    details={
                        **_finding_summary(connection, tenant_id=tenant_id, finding_id=finding_id),
                        **_native_details(connection, tenant_id=tenant_id, control=control,
                                          finding_id=finding_id),
                        **(getattr(result, "details", None) or {}),
                    },
                    fact_version=version,
                )
            )
    if errors:
        raise ControlUnitError(f"Native controls errored: {', '.join(errors)}")
    return transitions


def _evaluate_external(engine: Engine, *, tenant_id: str, journey_id: UUID, stage: str,
                       correlation_id: str, registry: Registry) -> list[Transition]:
    from audit_core.rule_engine_client import (
        RULE_ENGINE_AUDIENCE,
        build_rule_engine_client,
    )
    from audit_core.uc03_rule_engine_findings import (
        _PHASE_TRIGGERING_EVENT,
        _build_security_oauth_client,
        _materialize_anomalies,
        _resolve_di_subject_id,
    )
    from audit_core.uc03_rule_execution_log import record_executions_bulk

    controls = [c for c in registry.controls_by_mode("EXTERNAL") if c.applies_to_stage(stage)]

    def write_all(status: str, reason: str) -> list[Transition]:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            version = _fact_version(connection, tenant_id=tenant_id, journey_id=journey_id)
            return [
                write_control_state(
                    connection, tenant_id=tenant_id, journey_id=journey_id, control=control,
                    status=status, stage=stage, reason=reason, fact_version=version,
                )
                for control in controls
            ]

    client = build_rule_engine_client()
    security = _build_security_oauth_client()
    if client is None or security is None:
        # Never report a clean audit when the executor is not wired up.
        return write_all("ERROR_TERMINAL", "The Rule Engine is not configured for this environment.")
    try:
        token = security.get_service_token(audience=RULE_ENGINE_AUDIENCE)
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            subject_id = _resolve_di_subject_id(connection, tenant_id=tenant_id, journey_id=journey_id)
        if subject_id is None:
            return write_all("WAITING_FOR_FACTS", "No documents have been processed for this Journey yet.")
        result = client.evaluate_phase(token=token, tenant_id=tenant_id, subject_id=str(subject_id), phase=stage)
        catalog = client.list_rules(token=token, tenant_id=tenant_id)
        readiness = client.readiness(token=token, tenant_id=tenant_id, subject_id=str(subject_id))
    except Exception as exc:
        raise ControlUnitError(f"Rule Engine evaluation failed: {exc.__class__.__name__}") from exc
    finally:
        security.close()
        client.close()

    relevant = {
        rule.rule_code for rule in catalog
        if stage in rule.phases or "FULL" in rule.phases
    }
    anomalies = {a.rule_code: a for a in result.anomalies}
    ready = set(readiness.ready)
    not_ready = {n.rule_code: n.reason for n in readiness.not_ready}
    # A rule the Rule Engine still evaluates but the catalogue no longer
    # carries (superseded by a native check) raises nothing here.
    catalogued = {c.code for c in registry.controls_by_mode("EXTERNAL")}
    retired = sorted((relevant | set(anomalies)) - catalogued)

    transitions: list[Transition] = []
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        flagged = _materialize_anomalies(
            connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage,
            anomalies=[a for a in result.anomalies if a.rule_code in catalogued],
            correlation_id=correlation_id, audit_run_id=result.audit_run_id,
        )
        # Keep the existing Execution Log history complete.
        record_executions_bulk(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            triggering_event=_PHASE_TRIGGERING_EVENT.get(stage, stage), correlation_id=correlation_id,
            pass_rule_codes=tuple(c for c in ready & relevant if c not in flagged),
            fail_rule_codes=flagged,
            skipped_rule_codes={c: r for c, r in not_ready.items() if c in relevant},
        )
        version = _fact_version(connection, tenant_id=tenant_id, journey_id=journey_id)
        for control in controls:
            code = control.code
            details: dict[str, Any] = {"operands": control.operands}
            finding_id = None
            if code in anomalies:
                anomaly = anomalies[code]
                status, reason = "FAIL", anomaly.detail or "The compared values do not agree."
                details.update({"leftValue": anomaly.left_value, "rightValue": anomaly.right_value,
                                "ruleEngineSeverity": anomaly.severity})
                finding_id = flagged.get(code)
            elif code not in relevant:
                status, reason = "NOT_APPLICABLE", f"The Rule Engine does not evaluate this rule for {stage.title()}."
            elif code in ready:
                status, reason = "PASS", None
            else:
                status = "WAITING_FOR_FACTS"
                reason = not_ready.get(code) or "Waiting for the documents this rule compares."
            transitions.append(
                write_control_state(
                    connection, tenant_id=tenant_id, journey_id=journey_id, control=control,
                    status=status, stage=stage, reason=reason, details=details,
                    finding_id=finding_id, fact_version=version,
                )
            )
        _resolve_stale_rule_engine_findings(
            connection, tenant_id=tenant_id, journey_id=journey_id, stage=stage,
            codes=[t.control_code for t in transitions if t.current in {"PASS", "NOT_APPLICABLE"}] + retired,
            correlation_id=correlation_id,
        )
    return transitions


def _evaluate_derived(engine: Engine, *, tenant_id: str, journey_id: UUID, registry: Registry) -> list[Transition]:
    transitions: list[Transition] = []
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        version = _fact_version(connection, tenant_id=tenant_id, journey_id=journey_id)
        gate_rows = connection.execute(
            text(
                """
                SELECT stage_code, gate_key, gate_status, details
                FROM auditcore.p2_stage_gate_state
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).mappings().all()
        gates = {f"{r['stage_code']}:{r['gate_key']}": r for r in gate_rows}
        page_counts = dict(
            connection.execute(
                text(
                    """
                    SELECT queue_status, COUNT(*) FROM auditcore.p2_document_queue
                    WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND queue_status <> 'MERGED'
                    GROUP BY queue_status
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id},
            ).all()
        )
        sync_codes = [c.code for c in registry.controls.values() if c.mode in {"SYNC", "EVENT"}]
        from audit_core.uc03_p2_stage import condition_reasons, requirement_items

        reasons = condition_reasons(connection, tenant_id=tenant_id, journey_id=journey_id)
        requirement_rows = [
            item for stage_code in ("BOOKING", "DELIVERY")
            for item in requirement_items(connection, registry, tenant_id=tenant_id, journey_id=journey_id,
                                          stage=stage_code, reasons=reasons)
        ]
        executions = _latest_executions(connection, tenant_id=tenant_id, journey_id=journey_id, codes=sync_codes)

        for control in registry.controls.values():
            stage = control.phases[0] if len(control.phases) == 1 else None
            details: dict[str, Any] = {}
            finding_id = None
            if control.mode == "GATE":
                rows = [gates.get(g) for g in control.gates]
                if any(row is None for row in rows):
                    status, reason = "WAITING_FOR_FACTS", "Not evaluated yet."
                elif all(row["gate_status"] == "PASS" for row in rows):
                    status, reason = "PASS", None
                else:
                    unmet = [row for row in rows if row["gate_status"] != "PASS"]
                    payment = next((r for r in unmet if r["gate_key"] == "MINIMUM_BOOKING_PAYMENT"), None)
                    if payment is not None and int((payment["details"] or {}).get("receiptCount") or 0) > 0:
                        status = "FAIL"  # receipts exist but do not reach the minimum
                    else:
                        status = "WAITING_FOR_FACTS"
                    reason = " ".join(str((r["details"] or {}).get("action") or "") for r in unmet).strip() or None
                    details = {"gates": {r["gate_key"]: dict(r["details"] or {}) for r in unmet}}
            elif control.mode == "PAGES":
                total = sum(int(v) for v in page_counts.values())
                flagged = sum(int(page_counts.get(s, 0)) for s in control.page_statuses)
                if total == 0:
                    status, reason = "WAITING_FOR_FACTS", "No documents uploaded yet."
                elif flagged:
                    status, reason = "FAIL", f"{flagged} page(s) need attention."
                    details = {"pageCount": flagged}
                else:
                    status, reason = "PASS", None
            elif control.mode == "REQUIREMENTS":
                # Conditional documents made mandatory by evidence. Missing
                # ones already have a Document Missing task, so this check
                # waits rather than failing (it never raises a second task).
                missing = [
                    item for item in requirement_rows
                    if item["requirement"] == "CONDITIONAL" and item["required"] and not item["received"]
                    and (control.code != "BK_DISCOUNT_EVIDENCE_MISSING"
                         or set(item["conditions"]) & _DISCOUNT_CONDITIONS)
                ]
                if missing:
                    status = "WAITING_FOR_FACTS"
                    reason = "Waiting for: " + "; ".join(f"{m['label']} ({m['reason']})" for m in missing)
                    details = {"missing": [m["label"] for m in missing]}
                else:
                    status, reason = "PASS", None
            elif control.mode in {"SYNC", "EVENT"}:
                execution = executions.get(control.code)
                if execution is None:
                    status = "WAITING_FOR_FACTS"
                    reason = (
                        f"Evaluated at {control.event}." if control.mode == "EVENT"
                        else "Evaluated when the related documents are processed."
                    )
                else:
                    outcome = str(execution["outcome"])
                    status = {"ERROR": "ERROR_TERMINAL"}.get(outcome, _OUTCOME_TO_STATE.get(outcome, "WAITING_FOR_FACTS"))
                    reason = execution["reason"]
                    finding_id = execution["audit_finding_id"] if status == "FAIL" else None
                    details = _finding_summary(connection, tenant_id=tenant_id, finding_id=finding_id)
            else:
                continue
            transitions.append(
                write_control_state(
                    connection, tenant_id=tenant_id, journey_id=journey_id, control=control,
                    status=status, stage=stage, reason=reason, details=details,
                    finding_id=finding_id, fact_version=version,
                )
            )
    return transitions


# ---------------------------------------------------------------- unit runner

def _unit_row(connection: Connection, *, tenant_id: str, journey_id: UUID, unit: str) -> dict[str, Any] | None:
    row = connection.execute(
        text(
            """
            SELECT unit_status, evaluated_fingerprint FROM auditcore.p2_control_units
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND unit_key=:unit
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "unit": unit},
    ).mappings().one_or_none()
    return dict(row) if row else None


def _set_unit(connection: Connection, *, tenant_id: str, journey_id: UUID, unit: str, status: str,
              fingerprint: str | None, error: str | None) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_control_units (
                tenant_id, journey_id, unit_key, unit_status, evaluated_fingerprint,
                last_error, last_evaluated_at_utc, updated_at_utc
            ) VALUES (:tenant_id, :journey_id, :unit, :status, :fingerprint, :error, now(), now())
            ON CONFLICT (tenant_id, journey_id, unit_key) DO UPDATE SET
                unit_status=EXCLUDED.unit_status,
                evaluated_fingerprint=COALESCE(EXCLUDED.evaluated_fingerprint,
                                               auditcore.p2_control_units.evaluated_fingerprint),
                last_error=EXCLUDED.last_error,
                last_evaluated_at_utc=now(),
                updated_at_utc=now()
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "unit": unit, "status": status,
         "fingerprint": fingerprint, "error": error},
    )


def _unit_fingerprint(connection: Connection, *, tenant_id: str, journey_id: UUID, unit: str) -> str:
    facts = fact_fingerprint(connection, tenant_id=tenant_id, journey_id=journey_id)
    if unit != "P2:DERIVED":
        return facts
    # Derived controls also read gates, page outcomes and the execution log.
    extra = connection.execute(
        text(
            """
            SELECT concat_ws('|',
              (SELECT string_agg(gate_key || gate_status, ',' ORDER BY gate_key)
                 FROM auditcore.p2_stage_gate_state WHERE tenant_id=:t AND journey_id=:j),
              (SELECT string_agg(queue_status, ',' ORDER BY queue_id)
                 FROM auditcore.p2_document_queue WHERE tenant_id=:t AND journey_id=:j),
              (SELECT MAX(evaluated_at_utc)::text FROM auditcore.rule_executions
                 WHERE tenant_id=:t AND journey_id=:j))
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()
    import hashlib

    return hashlib.sha256(f"{facts}|{extra}".encode()).hexdigest()


def evaluate_unit(
    engine: Engine,
    *,
    tenant_id: str,
    journey_id: UUID,
    unit: str,
    correlation_id: str | None = None,
    force: bool = False,
    registry: Registry | None = None,
) -> list[Transition]:
    """Evaluate one unit if its inputs changed. Raises ControlUnitError on a
    technical failure after recording RETRY_PENDING."""
    registry = registry or get_registry()
    if unit not in UNITS:
        raise ValueError(f"Unknown control unit {unit}")
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        fingerprint = _unit_fingerprint(connection, tenant_id=tenant_id, journey_id=journey_id, unit=unit)
        current = _unit_row(connection, tenant_id=tenant_id, journey_id=journey_id, unit=unit)
    if not force and current and current["unit_status"] == "OK" and current["evaluated_fingerprint"] == fingerprint:
        return []

    kind, _, stage = unit.partition(":")
    try:
        if kind == "NATIVE":
            transitions = _evaluate_native(engine, tenant_id=tenant_id, journey_id=journey_id, stage=stage,
                                           correlation_id=correlation_id or "", registry=registry)
        elif kind == "RULE_ENGINE":
            transitions = _evaluate_external(engine, tenant_id=tenant_id, journey_id=journey_id, stage=stage,
                                             correlation_id=correlation_id or "", registry=registry)
        else:
            transitions = _evaluate_derived(engine, tenant_id=tenant_id, journey_id=journey_id, registry=registry)
    except ControlUnitError as exc:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            _set_unit(connection, tenant_id=tenant_id, journey_id=journey_id, unit=unit,
                      status="RETRY_PENDING", fingerprint=None, error=str(exc)[:1800])
            mark_unit_controls(connection, tenant_id=tenant_id, journey_id=journey_id, unit=unit,
                               status="RETRY_PENDING", reason=str(exc), registry=registry,
                               only_unsettled=True)
        raise
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        _set_unit(connection, tenant_id=tenant_id, journey_id=journey_id, unit=unit,
                  status="OK", fingerprint=fingerprint, error=None)
    return transitions


def mark_unit_controls(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    unit: str,
    status: str,
    reason: str,
    registry: Registry | None = None,
    only_unsettled: bool = False,
) -> None:
    """Set every control of a unit to a technical state (retry / terminal).

    ``only_unsettled`` keeps controls that already have a real PASS/FAIL result
    from the current facts untouched while a retry is pending."""
    registry = registry or get_registry()
    kind, _, stage = unit.partition(":")
    mode = {"NATIVE": "RERUN", "RULE_ENGINE": "EXTERNAL"}.get(kind)
    if mode is None:
        return
    for control in registry.controls_by_mode(mode):
        if not control.applies_to_stage(stage):
            continue
        if only_unsettled:
            existing = connection.execute(
                text(
                    "SELECT control_status FROM auditcore.p2_control_state "
                    "WHERE tenant_id=:t AND journey_id=:j AND control_code=:c"
                ),
                {"t": tenant_id, "j": journey_id, "c": control.code},
            ).scalar_one_or_none()
            if existing in {"PASS", "FAIL", "NOT_APPLICABLE"}:
                continue
        write_control_state(
            connection, tenant_id=tenant_id, journey_id=journey_id, control=control,
            status=status, stage=stage, reason=reason,
        )


def control_statistics(connection: Connection, *, tenant_id: str, journey_id: UUID,
                       registry: Registry | None = None) -> dict[str, dict[str, int]]:
    """Per-stage control counts by state (blueprint 24.1). A control applying
    to both stages is counted in each."""
    registry = registry or get_registry()
    rows = connection.execute(
        text(
            """
            SELECT control_code, control_status FROM auditcore.p2_control_state
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).all()
    state = {str(code): str(status) for code, status in rows}
    stats: dict[str, dict[str, int]] = {}
    for stage in ("BOOKING", "DELIVERY"):
        counts = {k: 0 for k in ("total", "pass", "fail", "waiting", "notApplicable", "retry", "error")}
        for control in registry.controls.values():
            if not control.applies_to_stage(stage):
                continue
            counts["total"] += 1
            key = {
                "PASS": "pass", "FAIL": "fail", "NOT_APPLICABLE": "notApplicable",
                "RETRY_PENDING": "retry", "ERROR_TERMINAL": "error",
            }.get(state.get(control.code, "WAITING_FOR_FACTS"), "waiting")
            counts[key] += 1
        stats[stage] = counts
    return stats
