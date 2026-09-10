from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.errors import AuditCoreError
from audit_core.uc03_booking_commands import _append_workflow_event
from audit_core.uc03_booking_confirmation_rules import _DISCOUNT_EVIDENCE
from audit_core.uc03_delivery_commands import _machine_flag
from audit_core.uc03_manual_verification import _resolve_finding
from audit_core.uc03_rule_engine_findings import run_rule_engine_phase
from audit_core.workflow import claim_worker_task, get_workflow_task, start_worker_task
from audit_core.workflow_reliability import create_workflow_task_once

logger = structlog.get_logger(__name__)

_WORKFLOW_TYPE = "UC03_BOOKING_AUDIT"
_TASK_TYPE = "BOOKING_RULE_EVALUATION"
_WORKER_ID = "uc03-booking-rule-engine"

# document_type_keys already covered by uc03_booking_confirmation_rules' own
# BK_DISCOUNT_EVIDENCE_MISSING:<label> findings (e.g. a claimed exchange bonus
# requires vehicle_rc). A CONDITIONAL requirement backing one of these is a
# duplicate of that more specific, business-named finding, not a distinct gap
# -- e.g. trade_in_vehicle_rc (document_type_key=vehicle_rc) is exactly what
# BK_DISCOUNT_EVIDENCE_MISSING:exchange_bonus already asks the PC to address.
_DOCUMENT_TYPES_COVERED_BY_DISCOUNT_EVIDENCE = frozenset(
    document_type_key for _label, document_type_key in _DISCOUNT_EVIDENCE.values()
)


@dataclass(frozen=True)
class _RuleSpec:
    rule_key: str
    finding_type: str
    severity: Literal["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
    title: str
    description: str
    requirement_keys: tuple[str, ...]


def _requirement_snapshot(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT jdr.requirement_key, jdr.requirement_level,
                   jdr.requirement_status, jdr.document_type_key,
                   COALESCE(jda.answer, 'UNANSWERED') AS answer,
                   EXISTS (
                       SELECT 1 FROM auditcore.evidence e
                       WHERE e.tenant_id=jdr.tenant_id
                         AND e.journey_document_requirement_id=jdr.journey_document_requirement_id
                         AND e.association_status='ACTIVE'
                   ) AS has_evidence
            FROM auditcore.journey_document_requirements jdr
            LEFT JOIN auditcore.journey_document_assessments jda
              ON jda.tenant_id=jdr.tenant_id
             AND jda.journey_id=jdr.journey_id
             AND jda.stage_code='BOOKING'
             AND jda.requirement_key=jdr.requirement_key
            WHERE jdr.tenant_id=:tenant_id
              AND jdr.journey_id=:journey_id
              AND upper(jdr.process_area)='BOOKING'
              AND jdr.requirement_level <> 'OPTIONAL'
              AND jdr.requirement_status <> 'NOT_APPLICABLE'
            ORDER BY jdr.requirement_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _requirement_satisfied(row: dict[str, Any]) -> bool:
    # journey_document_requirements.requirement_status is never actually
    # transitioned to SATISFIED for Booking anywhere in the current codebase
    # (Delivery has its own explicit per-document answer/evidence endpoint
    # that does this; Booking's V2 "upload everything" flow never asks the
    # PC to explicitly answer a per-document applicability question, so
    # journey_document_assessments.answer stays UNANSWERED forever too).
    # This checkpoint-rule engine went unreachable (shadowed route) until it
    # was wired up live this session, which is why this gap was never
    # observed before: every requirement with real, active evidence linked
    # was flagged outstanding regardless of how thoroughly the PC reviewed
    # it. Booking's actual Submit gate (_mandatory_booking_documents_complete
    # in uc03_booking_v2.py) already treats a classified, linked document as
    # sufficient rather than requiring an explicit answer -- matching that
    # here too, in addition to (not instead of) the explicit-answer path in
    # case a future flow starts using it.
    if row.get("has_evidence"):
        return True
    return (
        str(row.get("requirement_status") or "").upper() in ("SATISFIED", "WAIVED")
        and str(row.get("answer") or "UNANSWERED").upper() == "YES"
    )


def _booking_requirement_rule_specs(rows: list[dict[str, Any]]) -> list[_RuleSpec]:
    if not rows:
        return []

    by_key = {str(row["requirement_key"]): row for row in rows}
    identity_satisfied = any(
        key in by_key and _requirement_satisfied(by_key[key])
        for key in ("pan_card", "aadhaar")
    )

    outstanding = [row for row in rows if not _requirement_satisfied(row)]
    if identity_satisfied:
        outstanding = [
            row
            for row in outstanding
            if str(row["requirement_key"]) not in {"pan_card", "aadhaar"}
        ]

    outstanding_by_key = {str(row["requirement_key"]): row for row in outstanding}
    specs: list[_RuleSpec] = []

    if "booking_docket" in outstanding_by_key:
        specs.append(
            _RuleSpec(
                rule_key="BK_DOCKET_PRESENT",
                finding_type="DOCUMENT_EXCEPTION",
                severity="HIGH",
                title="Booking docket evidence requires follow-up",
                description="The Booking docket requirement is not fully satisfied at Review confirmation.",
                requirement_keys=("booking_docket",),
            )
        )

    if not identity_satisfied and any(
        key in outstanding_by_key for key in ("pan_card", "aadhaar")
    ):
        specs.append(
            _RuleSpec(
                rule_key="BK_PAN_PRESENT",
                finding_type="CUSTOMER_IDENTITY_CONCERN",
                severity="HIGH",
                title="Customer identity evidence requires follow-up",
                description="Neither configured Booking identity document is fully satisfied at Review confirmation.",
                requirement_keys=tuple(
                    key for key in ("pan_card", "aadhaar") if key in outstanding_by_key
                ),
            )
        )

    if "minimum_booking_payment_proof" in outstanding_by_key:
        specs.append(
            _RuleSpec(
                rule_key="BK_MIN_BOOKING_PROOF_PRESENT",
                finding_type="PAYMENT_EXCEPTION",
                severity="HIGH",
                title="Minimum Booking payment proof requires follow-up",
                description="The minimum Booking payment proof requirement is not fully satisfied at Review confirmation.",
                requirement_keys=("minimum_booking_payment_proof",),
            )
        )

    conditional_keys = tuple(
        sorted(
            str(row["requirement_key"])
            for row in outstanding
            if str(row.get("requirement_level") or "").upper() == "CONDITIONAL"
            and str(row.get("document_type_key") or "")
            not in _DOCUMENT_TYPES_COVERED_BY_DISCOUNT_EVIDENCE
        )
    )
    if conditional_keys:
        specs.append(
            _RuleSpec(
                rule_key="BK_CONDITIONAL_DOCS_ADDRESSED",
                finding_type="DOCUMENT_EXCEPTION",
                severity="HIGH",
                title="Applicable conditional Booking evidence requires follow-up",
                description="One or more applicable conditional Booking requirements are not fully satisfied.",
                requirement_keys=conditional_keys,
            )
        )

    handled = {
        "booking_docket",
        "pan_card",
        "aadhaar",
        "minimum_booking_payment_proof",
        *conditional_keys,
    }
    other_required = tuple(
        sorted(
            str(row["requirement_key"])
            for row in outstanding
            if str(row.get("requirement_level") or "").upper() == "REQUIRED"
            and str(row["requirement_key"]) not in handled
        )
    )
    if other_required:
        specs.append(
            _RuleSpec(
                rule_key="BK_REQUIRED_CAPTURE_COMPLETE",
                finding_type="PROCESS_NON_COMPLIANCE",
                severity="HIGH",
                title="Required Booking capture requires follow-up",
                description="One or more required Booking evidence requirements are not fully satisfied.",
                requirement_keys=other_required,
            )
        )

    return specs


def _complete_worker_task(
    connection: Connection,
    *,
    tenant_id: str,
    workflow_task_id: UUID,
    worker_id: str,
) -> None:
    row = connection.execute(
        text(
            """
            UPDATE auditcore.workflow_tasks
            SET task_status='COMPLETED',
                completed_at_utc=now(),
                lease_owner=NULL,
                lease_acquired_at_utc=NULL,
                lease_heartbeat_at_utc=NULL,
                lease_expires_at_utc=NULL,
                next_attempt_at_utc=NULL,
                last_error_code=NULL,
                last_error_summary=NULL,
                updated_at_utc=now(),
                version_no=version_no+1
            WHERE tenant_id=:tenant_id
              AND workflow_task_id=:task_id
              AND task_status='IN_PROGRESS'
              AND lease_owner=:worker_id
            RETURNING workflow_instance_id, journey_id,
                      correlation_id, attempt_count
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": workflow_task_id,
            "worker_id": worker_id,
        },
    ).mappings().one_or_none()
    if row is None:
        return

    connection.execute(
        text(
            """
            UPDATE auditcore.workflow_task_attempts
            SET ended_at_utc=now(), attempt_result='SUCCEEDED'
            WHERE tenant_id=:tenant_id
              AND workflow_task_id=:task_id
              AND attempt_no=:attempt_no
              AND ended_at_utc IS NULL
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": workflow_task_id,
            "attempt_no": row["attempt_count"],
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.workflow_task_events (
                tenant_id, workflow_task_id, workflow_instance_id,
                journey_id, event_type, from_status, to_status,
                actor_type, correlation_id
            ) VALUES (
                :tenant_id, :task_id, :workflow_instance_id,
                :journey_id, 'WORKER_COMPLETED', 'IN_PROGRESS', 'COMPLETED',
                'SYSTEM', :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": workflow_task_id,
            "workflow_instance_id": row["workflow_instance_id"],
            "journey_id": row["journey_id"],
            "correlation_id": row["correlation_id"],
        },
    )


def _run_booking_rules(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    workflow_task_id: UUID,
    correlation_id: str,
    aggregate_version: int,
) -> tuple[list[str], list[str]]:
    rows = _requirement_snapshot(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    specs = _booking_requirement_rule_specs(rows)
    evaluated = [
        "BK_REQUIRED_CAPTURE_COMPLETE",
        "BK_DOCKET_PRESENT",
        "BK_PAN_PRESENT",
        "BK_MIN_BOOKING_PROOF_PRESENT",
        "BK_CONDITIONAL_DOCS_ADDRESSED",
    ]
    flagged: list[str] = []

    connection.execute(
        text(
            """
            UPDATE auditcore.journey_stage_states
            SET audit_state=CASE
                    WHEN audit_state='NOT_STARTED' THEN 'IN_PROGRESS'
                    ELSE audit_state
                END,
                updated_at_utc=now()
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND stage_code='BOOKING'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    )

    for spec in specs:
        _machine_flag(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code="BOOKING",
            rule_key=spec.rule_key,
            finding_type=spec.finding_type,
            severity=spec.severity,
            title=spec.title,
            description=spec.description,
            correlation_id=correlation_id,
            safe_payload={
                "trigger": "PC_BOOKING_ATTRIBUTE_REVIEW_CONFIRMED",
                "requirementKeys": list(spec.requirement_keys),
            },
            blocking_completion=False,
        )
        flagged.append(spec.rule_key)

    # Self-heal: a rule that was evaluated this pass but did not fire (its
    # outstanding requirements are now met -- e.g. evidence just landed) must
    # close any finding it previously raised. Without this, once a checkpoint
    # rule fires it can never clear even after the PC addresses it -- only
    # `flagged` rules were ever written here, nothing resolved the rest.
    for rule_key in evaluated:
        if rule_key in flagged:
            continue
        open_finding_id = connection.execute(
            text(
                """
                SELECT audit_finding_id
                FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND rule_key=:rule_key AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "rule_key": rule_key},
        ).scalar_one_or_none()
        if open_finding_id is None:
            continue
        _resolve_finding(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code="BOOKING",
            finding_id=open_finding_id,
            actor_id=None,
            correlation_id=correlation_id,
            note="Outstanding Booking requirements are now satisfied.",
        )

    if not flagged:
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET audit_status=CASE
                        WHEN audit_status='NOT_EVALUATED' THEN 'NO_FLAGS'
                        ELSE audit_status
                    END,
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id
                  AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        )

    _append_workflow_event(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        event_type="BOOKING_RULE_EVALUATION_COMPLETED",
        source_kind="MACHINE",
        actor_id=None,
        actor_role_snapshot="SYSTEM",
        idempotency_key=f"booking-rule-evaluation:{workflow_task_id}",
        correlation_id=correlation_id,
        safe_payload={
            "trigger": "PC_BOOKING_ATTRIBUTE_REVIEW_CONFIRMED",
            "workflowTaskId": str(workflow_task_id),
            "evaluatedRuleKeys": evaluated,
            "flaggedRuleKeys": sorted(flagged),
            "outstandingRequirementCount": sum(
                1 for row in rows if not _requirement_satisfied(row)
            ),
        },
        aggregate_version=aggregate_version,
    )
    return evaluated, flagged


def run_booking_review_rule_task(
    engine: Engine,
    tenant_id: str,
    journey_id: UUID,
    workflow_task_id: UUID,
    correlation_id: str,
    aggregate_version: int,
) -> None:
    try:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            task = get_workflow_task(
                connection,
                tenant_id=tenant_id,
                workflow_task_id=workflow_task_id,
            )
            if str(task["task_status"]) == "COMPLETED":
                return
            if str(task["task_status"]) != "READY":
                logger.info(
                    "uc03_booking_rule_task_not_ready",
                    tenant_id=tenant_id,
                    journey_id=str(journey_id),
                    task_id=str(workflow_task_id),
                    task_status=str(task["task_status"]),
                )
                return

            try:
                claim_worker_task(
                    connection,
                    tenant_id=tenant_id,
                    workflow_task_id=workflow_task_id,
                    worker_id=_WORKER_ID,
                    lease_seconds=120,
                )
            except AuditCoreError:
                # Two independent triggers call schedule_booking_checkpoint_rules
                # for the same task by design (the async document-sync path and
                # PC Review Confirm's safety net) -- claim_worker_task's UPDATE is
                # atomic, so losing this race just means the other trigger claimed
                # it between our READY check above and this call. Expected, not a
                # failure: whoever won proceeds to evaluate, we have nothing left
                # to do. (Previously fell through to the broad `except Exception`
                # below, logging a full traceback as uc03_booking_rule_evaluation_
                # failed for a routine race -- noisy, and easy to mistake for a
                # real defect.)
                logger.info(
                    "uc03_booking_rule_task_already_claimed",
                    tenant_id=tenant_id,
                    journey_id=str(journey_id),
                    task_id=str(workflow_task_id),
                )
                return
            start_worker_task(
                connection,
                tenant_id=tenant_id,
                workflow_task_id=workflow_task_id,
                worker_id=_WORKER_ID,
                lease_seconds=120,
            )
            evaluated, flagged = _run_booking_rules(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                workflow_task_id=workflow_task_id,
                correlation_id=correlation_id,
                aggregate_version=aggregate_version,
            )
            _complete_worker_task(
                connection,
                tenant_id=tenant_id,
                workflow_task_id=workflow_task_id,
                worker_id=_WORKER_ID,
            )
            logger.info(
                "uc03_booking_rule_evaluation_completed",
                tenant_id=tenant_id,
                journey_id=str(journey_id),
                task_id=str(workflow_task_id),
                evaluated_rule_count=len(evaluated),
                flagged_rule_count=len(flagged),
            )
    except Exception:
        logger.exception(
            "uc03_booking_rule_evaluation_failed",
            tenant_id=tenant_id,
            journey_id=str(journey_id),
            task_id=str(workflow_task_id),
        )

    # Cross-document anomaly rules run in the rule-engine service. Best-effort and
    # independent of the checkpoint rules above — dormant unless RULE_ENGINE_BASE_URL
    # is configured; never raises.
    run_rule_engine_phase(engine, tenant_id, journey_id, "BOOKING", "BOOKING", correlation_id)


def schedule_booking_checkpoint_rules(
    engine: Engine,
    *,
    tenant_id: str,
    journey_id: UUID,
    correlation_id: str,
    trigger: str,
) -> None:
    """Evaluate Booking checkpoint rules + the external rule-engine phase for
    the journey's CURRENT aggregate version, deduped per version.

    Async by design, matching the document-sync pipeline's own philosophy
    (see _sync_booking_document): rule evaluation must not depend on the PC
    remembering to click Confirm. This is called from three places --
    - the DI document-link webhook's background sync, every time a document
      confirms (the primary trigger: fully async, fires whether or not the
      PC has looked at the Booking since);
    - PC Review Confirm, as a final safety net, exactly like Submit is a
      safety net for document sync rather than the trigger;
    each call reads the journey's current journey_stage_states.version_no
    and keys the workflow task's effect_key on it
    (uc03.booking.review-rule-evaluation:{journey_id}:{version}), so several
    calls for the same unchanged version collapse to the one task
    create_workflow_task_once already returns, and a version bump (a new
    document synced, or a PC correction at confirm) always gets a fresh
    evaluation. Opens its own connection deliberately -- callers must invoke
    this only after their own transaction has committed, never nested inside
    one, or it would evaluate rules against not-yet-visible data.
    """

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        state = connection.execute(
            text(
                """
                SELECT version_no
                FROM auditcore.journey_stage_states
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).mappings().one_or_none()
        if state is None:
            return
        aggregate_version = int(state["version_no"])

        journey = connection.execute(
            text(
                """
                SELECT dealer_id, outlet_id
                FROM auditcore.journeys
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).mappings().one()

        effect_key = f"uc03.booking.review-rule-evaluation:{journey_id}:{aggregate_version}"
        task_id = create_workflow_task_once(
            connection,
            tenant_id=tenant_id,
            effect_key=effect_key,
            journey_id=journey_id,
            workflow_type=_WORKFLOW_TYPE,
            process_area="BOOKING",
            task_type=_TASK_TYPE,
            dealer_id=journey["dealer_id"],
            outlet_id=journey["outlet_id"],
            task_payload={"trigger": trigger, "aggregateVersion": aggregate_version},
            correlation_id=correlation_id,
        )

    run_booking_review_rule_task(
        engine,
        tenant_id,
        journey_id,
        task_id,
        correlation_id,
        aggregate_version,
    )


# confirm_booking_review_v2_and_trigger_rules and
# install_uc03_booking_review_rule_trigger removed: install_uc03_confidence_
# review_policy's later _replace_route call always discarded this route
# registration anyway (confirmed: confirm_booking_review_v2_confidence_policy
# is the actually-live handler and never called run_booking_review_rule_task
# or run_rule_engine_phase -- the Booking checkpoint rules and the external
# rule-engine call were silently never firing on a live Review confirm).
# schedule_booking_checkpoint_rules above replaces both: it's called
# directly from the live confirm handler and from the async document-sync
# path, not installed via a route that a later installer can silently win
# over.
