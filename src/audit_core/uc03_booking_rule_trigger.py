from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import UUID

import structlog
from fastapi import BackgroundTasks, Depends, Header, Request, Response
from fastapi.routing import APIRoute
from sqlalchemy import Connection, Engine, text

from audit_core import uc03_document_review_v2 as review_v2
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_engine, get_human_principal
from audit_core.evidence import get_di_client, get_security_oauth_client
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient
from audit_core.uc03_booking_commands import _append_workflow_event
from audit_core.uc03_booking_review_decisions import (
    BookingReviewV2ConfirmWithDecisionsResponse,
    confirm_booking_review_v2_with_decisions,
)
from audit_core.uc03_delivery_commands import _machine_flag
from audit_core.workflow import claim_worker_task, get_workflow_task, start_worker_task
from audit_core.workflow_reliability import create_workflow_task_once

logger = structlog.get_logger(__name__)

_WORKFLOW_TYPE = "UC03_BOOKING_AUDIT"
_TASK_TYPE = "BOOKING_RULE_EVALUATION"
_WORKER_ID = "uc03-booking-rule-engine"


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
                   jdr.requirement_status,
                   COALESCE(jda.answer, 'UNANSWERED') AS answer
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
    return (
        str(row.get("requirement_status") or "").upper() == "SATISFIED"
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
            SET ended_at_utc=now(), attempt_result='SUCCESS'
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

            claim_worker_task(
                connection,
                tenant_id=tenant_id,
                workflow_task_id=workflow_task_id,
                worker_id=_WORKER_ID,
                lease_seconds=120,
            )
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


def confirm_booking_review_v2_and_trigger_rules(
    tenant_id: str,
    journey_id: UUID,
    background_tasks: BackgroundTasks,
    request: Request,
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[
        SecurityOAuthClient,
        Depends(get_security_oauth_client),
    ],
    di_client: Annotated[review_v2.DiClient, Depends(get_di_client)],
    v2_client: Annotated[
        review_v2.DiCaptureV2Client,
        Depends(review_v2.get_di_capture_v2_client),
    ],
) -> BookingReviewV2ConfirmWithDecisionsResponse:
    result = confirm_booking_review_v2_with_decisions(
        tenant_id=tenant_id,
        journey_id=journey_id,
        request=request,
        response=response,
        if_match=if_match,
        idempotency_key=idempotency_key,
        human_principal=human_principal,
        authorization_client=authorization_client,
        connection=connection,
        engine=engine,
        security_client=security_client,
        di_client=di_client,
        v2_client=v2_client,
    )

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
    correlation_id = get_correlation_id(request)
    effect_key = (
        f"uc03.booking.review-rule-evaluation:{journey_id}:"
        f"{result.aggregateVersion}"
    )
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
        task_payload={
            "trigger": "PC_BOOKING_ATTRIBUTE_REVIEW_CONFIRMED",
            "aggregateVersion": result.aggregateVersion,
        },
        correlation_id=correlation_id,
    )
    background_tasks.add_task(
        run_booking_review_rule_task,
        engine,
        tenant_id,
        journey_id,
        task_id,
        correlation_id,
        result.aggregateVersion,
    )
    return result


def install_uc03_booking_review_rule_trigger() -> None:
    """Run Booking checkpoint rules after a successful V2 Review confirmation."""

    if getattr(review_v2, "_booking_review_rule_trigger_installed", False):
        return
    retained = []
    for route in review_v2.router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path.endswith("/booking/review/confirm")
            and "POST" in route.methods
        ):
            continue
        retained.append(route)
    review_v2.router.routes[:] = retained
    review_v2.router.add_api_route(
        "/booking/review/confirm",
        confirm_booking_review_v2_and_trigger_rules,
        methods=["POST"],
        response_model=BookingReviewV2ConfirmWithDecisionsResponse,
    )
    review_v2._booking_review_rule_trigger_installed = True
