from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import (
    AuditCoreError,
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
)
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    SecurityAuthorizationError,
    get_security_authorization_client,
)
from audit_core.uc03_booking_commands import _journey_context

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}/uc03",
    tags=["uc03-audit"],
)

StageCode = Literal["BOOKING", "DELIVERY"]
FlagAction = Literal["ACKNOWLEDGE", "REVIEW", "RESOLVE", "REOPEN", "VOID"]

_HUMAN_FLAG_CATEGORIES = {
    "PHYSICAL_OBSERVATION",
    "DOCUMENT_EXCEPTION",
    "PAYMENT_EXCEPTION",
    "CUSTOMER_IDENTITY_CONCERN",
    "COMMERCIAL_EXCEPTION",
    "PROCESS_NON_COMPLIANCE",
    "DELIVERY_EXCEPTION",
    "OTHER",
}
_SEVERITIES = {"INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
_SEVERITY_ORDER = {"INFO": 1, "LOW": 2, "MEDIUM": 3, "HIGH": 4, "CRITICAL": 5}

_PERMISSION_BY_OPERATION = {
    "READ": "audit.finding.read",
    "RAISE": "audit.finding.create",
    "REMARK": "audit.finding.update",
    "ACKNOWLEDGE": "audit.review.decide",
    "REVIEW": "audit.review.decide",
    "RESOLVE": "audit.finding.resolve",
    "REOPEN": "audit.finding.resolve",
    "VOID": "audit.finding.resolve",
    "COMPLETE_AUDIT": "audit.journey.update",
}

_DEFAULT_ROLE_POLICY: dict[str, set[str]] = {
    "READ": {"PC", "TL", "PM", "EXECUTIVE"},
    "RAISE": {"PC", "TL", "PM", "EXECUTIVE"},
    "REMARK": {"PC", "TL", "PM", "EXECUTIVE"},
    "ACKNOWLEDGE": {"TL", "PM", "EXECUTIVE"},
    "REVIEW": {"TL", "PM", "EXECUTIVE"},
    "RESOLVE": {"TL", "PM", "EXECUTIVE"},
    "REOPEN": {"TL", "PM", "EXECUTIVE"},
    # TL/PM void is configurable in the catalog; the conservative Phase-1 default
    # is Executive only unless the published Project policy overrides it.
    "VOID": {"EXECUTIVE"},
    "COMPLETE_AUDIT": {"PC", "TL", "PM", "EXECUTIVE"},
}


class FlagCreateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: StageCode
    category: str = Field(min_length=1, max_length=100)
    severity: str = Field(min_length=1, max_length=20)
    summary: str = Field(min_length=1, max_length=500)
    remarks: str | None = Field(default=None, max_length=4000)
    evidenceIds: list[UUID] = Field(default_factory=list, max_length=20)


class FlagLifecycleCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: FlagAction
    remarks: str | None = Field(default=None, max_length=4000)
    resolutionReason: str | None = Field(default=None, max_length=4000)
    evidenceIds: list[UUID] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def require_reason_for_terminal_or_reopen(self):
        if self.action in {"RESOLVE", "REOPEN", "VOID"}:
            reason = (self.resolutionReason or self.remarks or "").strip()
            if not reason:
                raise ValueError("A reason is required for resolve, reopen, or void actions")
        return self


class FlagRemarkCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remarks: str = Field(min_length=1, max_length=4000)
    evidenceIds: list[UUID] = Field(default_factory=list, max_length=20)


class StageAuditCompleteCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remarks: str | None = Field(default=None, max_length=4000)


class FlagView(BaseModel):
    flagId: UUID
    stage: StageCode
    category: str | None
    severity: str
    status: str
    title: str
    description: str | None
    expectedSummary: str | None
    observedSummary: str | None
    resolutionReason: str | None
    originKind: str | None
    originRole: str | None
    ruleKey: str | None
    ruleVersionId: UUID | None
    blockingCompletion: bool
    evidenceCount: int
    version: int
    createdAtUtc: datetime
    updatedAtUtc: datetime


class FlagMutationResponse(BaseModel):
    flag: FlagView
    eventId: UUID
    idempotent: bool = False


class StageAuditView(BaseModel):
    stage: StageCode
    businessStatus: str | None
    auditState: str
    auditStatus: str
    aggregateVersion: int
    openFlagCount: int
    totalHistoricalFlagCount: int
    blockingOpenFlagCount: int


class AuditSummaryView(BaseModel):
    journeyId: UUID
    operatingRole: str
    booking: StageAuditView | None
    delivery: StageAuditView | None
    openFlagCount: int
    totalHistoricalFlagCount: int
    highestOpenSeverity: str | None
    machineFlagCount: int
    humanFlagCount: int
    permittedActions: list[str]


class TimelineItem(BaseModel):
    kind: Literal["WORKFLOW", "FLAG", "REVIEW"]
    stage: str | None
    eventType: str
    summary: str
    actorRole: str | None
    remarks: str | None
    occurredAtUtc: datetime


class StageAuditCompleteResponse(BaseModel):
    journeyId: UUID
    stage: StageCode
    auditState: Literal["COMPLETE"] = "COMPLETE"
    auditStatus: str
    aggregateVersion: int
    eventId: UUID


def _normalize_role(value: str | None) -> str:
    normalized = (value or "").strip().upper()
    if normalized in {"EXEC", "EXECUTIVE"}:
        return "EXECUTIVE"
    return normalized


def _authorize_security(
    client: SecurityAuthorizationClient,
    *,
    human_principal: HumanPrincipal,
    tenant_id: str,
    permission_key: str,
) -> None:
    try:
        decision = client.check_user_permission(
            user_id=human_principal.subject,
            tenant_id=tenant_id,
            permission_key=permission_key,
        )
    except SecurityAuthorizationError as exc:
        raise DependencyUnavailableError(
            detail="Audit review is temporarily unavailable. Please try again."
        ) from exc
    if not decision.allowed:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )


def _policy_roles(context: dict[str, Any], operation: str) -> set[str]:
    defaults = set(_DEFAULT_ROLE_POLICY[operation])
    settings = context.get("policy_settings")
    if not isinstance(settings, dict):
        return defaults
    authority = settings.get("uc03FlagAuthority")
    if not isinstance(authority, dict):
        return defaults
    configured = authority.get(operation)
    if not isinstance(configured, list) or not configured:
        return defaults
    roles = {_normalize_role(str(value)) for value in configured if str(value).strip()}
    return roles or defaults


def _scope(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    operation: str,
    human_principal: HumanPrincipal,
    authorization_client: SecurityAuthorizationClient,
) -> dict[str, Any]:
    _authorize_security(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
        permission_key=_PERMISSION_BY_OPERATION[operation],
    )
    set_tenant_context(connection, tenant_id)
    context = _journey_context(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        actor_id=human_principal.subject,
    )
    role = _normalize_role(context["operating_role"])
    if role not in _policy_roles(context, operation):
        raise AuthorizationError(
            error_code="VAC-AUTH-004",
            status_code=403,
            title="Operating role is not permitted for this audit action",
        )
    context["operating_role"] = role
    return context


def _parse_version(value: str, *, subject: str) -> int:
    candidate = value.strip()
    if candidate.startswith("W/"):
        candidate = candidate[2:].strip()
    if len(candidate) >= 2 and candidate[0] == '"' and candidate[-1] == '"':
        candidate = candidate[1:-1]
    try:
        version = int(candidate)
    except ValueError as exc:
        raise AuditCoreError(
            error_code="VAC-VAL-001",
            status_code=400,
            title="Validation failed",
            detail=f"If-Match must contain the expected {subject} version.",
        ) from exc
    if version < 0:
        raise AuditCoreError(
            error_code="VAC-VAL-001",
            status_code=400,
            title="Validation failed",
            detail=f"If-Match {subject} version cannot be negative.",
        )
    return version


def _set_etag(response: Response, version: int) -> None:
    response.headers["ETag"] = f'"{version}"'


def _stage_state(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    for_update: bool = False,
):
    lock = " FOR UPDATE" if for_update else ""
    row = connection.execute(
        text(
            """
            SELECT stage_code, business_status, audit_state, audit_status,
                   version_no, latest_activity_at_utc
            FROM auditcore.journey_stage_states
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code=:stage_code
            """
            + lock
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "stage_code": stage_code},
    ).mappings().one_or_none()
    return row


def _require_stage(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    for_update: bool = False,
):
    row = _stage_state(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=stage_code,
        for_update=for_update,
    )
    if row is None:
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title=f"{stage_code.title()} has not started",
            detail=f"Start {stage_code.title()} before recording stage audit actions.",
        )
    return row


def _finding(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    flag_id: UUID,
    for_update: bool = False,
):
    lock = " FOR UPDATE" if for_update else ""
    row = connection.execute(
        text(
            """
            SELECT audit_finding_id, journey_id, finding_type_code, severity,
                   finding_status, title, description, expected_summary,
                   observed_summary, resolution_reason, stage_code, origin_kind,
                   origin_actor_id, origin_role_snapshot, rule_key, rule_version_id,
                   blocking_completion, version_no, created_at_utc, updated_at_utc
            FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND audit_finding_id=:flag_id
            """
            + lock
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "flag_id": flag_id},
    ).mappings().one_or_none()
    if row is None or row["stage_code"] not in {"BOOKING", "DELIVERY"}:
        raise NotFoundError(
            error_code="VAC-NF-014",
            title="Audit flag not found",
            detail="The requested Booking/Delivery audit flag was not found.",
        )
    return row


def _validate_evidence(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    evidence_ids: list[UUID],
) -> None:
    for evidence_id in evidence_ids:
        found = connection.execute(
            text(
                """
                SELECT 1 FROM auditcore.evidence
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND evidence_id=:evidence_id AND association_status='ACTIVE'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "evidence_id": evidence_id},
        ).scalar_one_or_none()
        if found is None:
            raise AuditCoreError(
                error_code="VAC-VAL-003",
                status_code=400,
                title="Unsupported evidence",
                detail="One or more selected evidence items are not linked to this Booking/Delivery.",
            )


def _link_evidence(
    connection: Connection,
    *,
    tenant_id: str,
    flag_id: UUID,
    evidence_ids: list[UUID],
    purpose: str,
) -> None:
    for evidence_id in evidence_ids:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.finding_evidence (
                    tenant_id, audit_finding_id, evidence_id, linkage_purpose
                )
                SELECT :tenant_id, :flag_id, :evidence_id, :purpose
                WHERE NOT EXISTS (
                    SELECT 1 FROM auditcore.finding_evidence
                    WHERE tenant_id=:tenant_id
                      AND audit_finding_id=:flag_id
                      AND evidence_id=:evidence_id
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "flag_id": flag_id,
                "evidence_id": evidence_id,
                "purpose": purpose,
            },
        )


def _append_finding_event(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    flag_id: UUID,
    stage_code: str,
    event_type: str,
    actor_id: str | None,
    actor_role: str | None,
    reason: str | None,
    correlation_id: str,
    safe_payload: dict[str, Any] | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_finding_events (
                tenant_id, audit_finding_id, journey_id, stage_code,
                event_type, actor_id, actor_role_snapshot, reason,
                safe_payload, correlation_id
            ) VALUES (
                :tenant_id, :flag_id, :journey_id, :stage_code,
                :event_type, :actor_id, :actor_role, :reason,
                CAST(:safe_payload AS jsonb), :correlation_id
            ) RETURNING finding_event_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "flag_id": flag_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
            "event_type": event_type,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "reason": reason,
            "safe_payload": json.dumps(safe_payload or {}, default=str),
            "correlation_id": correlation_id,
        },
    ).scalar_one()


def _append_stage_event(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    event_type: str,
    actor_id: str,
    actor_role: str,
    idempotency_key: str,
    correlation_id: str,
    aggregate_version: int,
    remarks: str | None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_workflow_events (
                tenant_id, journey_id, stage_code, event_type, source_kind,
                actor_id, actor_role_snapshot, idempotency_key,
                correlation_id, safe_payload, occurred_at_utc, aggregate_version
            ) VALUES (
                :tenant_id, :journey_id, :stage_code, :event_type, 'HUMAN',
                :actor_id, :actor_role, :idempotency_key,
                :correlation_id, CAST(:payload AS jsonb), now(), :aggregate_version
            ) RETURNING event_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
            "event_type": event_type,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "idempotency_key": idempotency_key,
            "correlation_id": correlation_id,
            "payload": json.dumps({"remarksPresent": bool((remarks or "").strip())}),
            "aggregate_version": aggregate_version,
        },
    ).scalar_one()


def _evidence_count(connection: Connection, *, tenant_id: str, flag_id: UUID) -> int:
    return int(
        connection.execute(
            text(
                """
                SELECT count(*) FROM auditcore.finding_evidence
                WHERE tenant_id=:tenant_id AND audit_finding_id=:flag_id
                """
            ),
            {"tenant_id": tenant_id, "flag_id": flag_id},
        ).scalar_one()
    )


def _flag_view(connection: Connection, *, tenant_id: str, row) -> FlagView:
    return FlagView(
        flagId=row["audit_finding_id"],
        stage=row["stage_code"],
        category=row["finding_type_code"],
        severity=row["severity"],
        status=row["finding_status"],
        title=row["title"],
        description=row["description"],
        expectedSummary=row["expected_summary"],
        observedSummary=row["observed_summary"],
        resolutionReason=row["resolution_reason"],
        originKind=row["origin_kind"],
        originRole=row["origin_role_snapshot"],
        ruleKey=row["rule_key"],
        ruleVersionId=row["rule_version_id"],
        blockingCompletion=bool(row["blocking_completion"]),
        evidenceCount=_evidence_count(
            connection,
            tenant_id=tenant_id,
            flag_id=row["audit_finding_id"],
        ),
        version=int(row["version_no"]),
        createdAtUtc=row["created_at_utc"],
        updatedAtUtc=row["updated_at_utc"],
    )


def _list_flags(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str | None,
) -> list[FlagView]:
    stage_filter = " AND stage_code=:stage_code" if stage_code is not None else ""
    parameters: dict[str, Any] = {
        "tenant_id": tenant_id,
        "journey_id": journey_id,
    }
    if stage_code is not None:
        parameters["stage_code"] = stage_code
    rows = connection.execute(
        text(
            """
            SELECT audit_finding_id, journey_id, finding_type_code, severity,
                   finding_status, title, description, expected_summary,
                   observed_summary, resolution_reason, stage_code, origin_kind,
                   origin_actor_id, origin_role_snapshot, rule_key, rule_version_id,
                   blocking_completion, version_no, created_at_utc, updated_at_utc
            FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code IN ('BOOKING','DELIVERY')
            """
            + stage_filter
            + """
            ORDER BY
              CASE severity
                WHEN 'CRITICAL' THEN 5 WHEN 'HIGH' THEN 4 WHEN 'MEDIUM' THEN 3
                WHEN 'LOW' THEN 2 ELSE 1
              END DESC,
              created_at_utc DESC, audit_finding_id DESC
            """
        ),
        parameters,
    ).mappings().all()
    return [_flag_view(connection, tenant_id=tenant_id, row=row) for row in rows]


def _stage_summary(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: StageCode,
) -> StageAuditView | None:
    stage = _stage_state(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=stage_code,
    )
    if stage is None:
        return None
    counts = connection.execute(
        text(
            """
            SELECT
              count(*) FILTER (WHERE finding_status IN ('OPEN','ACKNOWLEDGED')) AS open_count,
              count(*) FILTER (WHERE finding_status <> 'VOIDED') AS historical_count,
              count(*) FILTER (
                WHERE finding_status IN ('OPEN','ACKNOWLEDGED') AND blocking_completion=true
              ) AS blocking_count
            FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code=:stage_code
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "stage_code": stage_code},
    ).mappings().one()
    historical = int(counts["historical_count"] or 0)
    effective_status = (
        "FLAGS_RAISED"
        if stage["audit_status"] == "FLAGS_RAISED" or historical > 0
        else stage["audit_status"]
    )
    return StageAuditView(
        stage=stage_code,
        businessStatus=stage["business_status"],
        auditState=stage["audit_state"],
        auditStatus=effective_status,
        aggregateVersion=int(stage["version_no"]),
        openFlagCount=int(counts["open_count"] or 0),
        totalHistoricalFlagCount=historical,
        blockingOpenFlagCount=int(counts["blocking_count"] or 0),
    )


def _role_permitted_actions(context: dict[str, Any]) -> list[str]:
    role = _normalize_role(context["operating_role"])
    operations = [
        "RAISE",
        "REMARK",
        "ACKNOWLEDGE",
        "REVIEW",
        "RESOLVE",
        "REOPEN",
        "VOID",
        "COMPLETE_AUDIT",
    ]
    return [operation for operation in operations if role in _policy_roles(context, operation)]


def _highest_open_severity(flags: list[FlagView]) -> str | None:
    active = [flag.severity for flag in flags if flag.status in {"OPEN", "ACKNOWLEDGED"}]
    if not active:
        return None
    return max(active, key=lambda value: _SEVERITY_ORDER.get(value, 0))


def _transition(action: FlagAction, current_status: str) -> str:
    allowed: dict[str, tuple[set[str], str]] = {
        "ACKNOWLEDGE": ({"OPEN"}, "ACKNOWLEDGED"),
        "REVIEW": ({"OPEN", "ACKNOWLEDGED"}, "ACKNOWLEDGED"),
        "RESOLVE": ({"OPEN", "ACKNOWLEDGED"}, "RESOLVED"),
        "REOPEN": ({"RESOLVED"}, "OPEN"),
        "VOID": ({"OPEN", "ACKNOWLEDGED", "RESOLVED"}, "VOIDED"),
    }
    accepted, next_status = allowed[action]
    if current_status not in accepted:
        raise ConflictError(
            error_code="VAC-CONFLICT-010",
            title="Audit flag state conflict",
            detail="The flag changed or this action is not valid for its current state. Refresh and retry.",
        )
    return next_status


def _stage_completion_blockers(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: StageCode,
) -> list[str]:
    blockers: list[str] = []
    blocking_flags = connection.execute(
        text(
            """
            SELECT count(*) FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code=:stage_code
              AND finding_status IN ('OPEN','ACKNOWLEDGED')
              AND blocking_completion=true
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "stage_code": stage_code},
    ).scalar_one()
    if blocking_flags:
        blockers.append("A configured audit-completion guard still requires review.")

    unanswered = connection.execute(
        text(
            """
            SELECT count(*)
            FROM auditcore.journey_document_requirements jdr
            LEFT JOIN auditcore.journey_document_assessments jda
              ON jda.tenant_id=jdr.tenant_id
             AND jda.journey_id=jdr.journey_id
             AND jda.stage_code=:stage_code
             AND jda.requirement_key=jdr.requirement_key
            WHERE jdr.tenant_id=:tenant_id AND jdr.journey_id=:journey_id
              AND upper(jdr.process_area)=:stage_code
              AND jdr.requirement_status <> 'NOT_APPLICABLE'
              AND COALESCE(jda.answer, 'UNANSWERED')='UNANSWERED'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "stage_code": stage_code},
    ).scalar_one()
    if unanswered:
        blockers.append("Applicable document audit questions are still unanswered.")

    if stage_code == "DELIVERY":
        facts = connection.execute(
            text(
                """
                SELECT intimation_answer, vin_reconciliation_status
                FROM auditcore.journey_delivery_audit_facts
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).mappings().one_or_none()
        if facts is None or facts["intimation_answer"] == "UNANSWERED":
            blockers.append("Delivery intimation audit is still unanswered.")
        if facts is not None and facts["vin_reconciliation_status"] == "REVIEW_REQUIRED":
            blockers.append("Vehicle identifier reconciliation still requires review.")

    return blockers


@router.get("/audit-summary", response_model=AuditSummaryView)
def get_audit_summary(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> AuditSummaryView:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        operation="READ",
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    flags = _list_flags(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=None,
    )
    return AuditSummaryView(
        journeyId=journey_id,
        operatingRole=context["operating_role"],
        booking=_stage_summary(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code="BOOKING",
        ),
        delivery=_stage_summary(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code="DELIVERY",
        ),
        openFlagCount=sum(1 for flag in flags if flag.status in {"OPEN", "ACKNOWLEDGED"}),
        totalHistoricalFlagCount=sum(1 for flag in flags if flag.status != "VOIDED"),
        highestOpenSeverity=_highest_open_severity(flags),
        machineFlagCount=sum(1 for flag in flags if flag.originKind == "MACHINE"),
        humanFlagCount=sum(1 for flag in flags if flag.originKind == "HUMAN"),
        permittedActions=_role_permitted_actions(context),
    )


@router.get("/flags", response_model=list[FlagView])
def list_flags(
    tenant_id: str,
    journey_id: UUID,
    stage: Annotated[StageCode | None, Query()] = None,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)] = None,
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ] = None,
    connection: Annotated[Connection, Depends(get_connection)] = None,
) -> list[FlagView]:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        operation="READ",
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    return _list_flags(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=stage,
    )


@router.post("/flags", response_model=FlagMutationResponse)
def create_flag(
    tenant_id: str,
    journey_id: UUID,
    payload: FlagCreateCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
    ],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> FlagMutationResponse:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        operation="RAISE",
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    category = payload.category.strip().upper()
    severity = payload.severity.strip().upper()
    if category not in _HUMAN_FLAG_CATEGORIES or severity not in _SEVERITIES:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Business validation failed",
            detail="The selected audit flag category or severity is not enabled.",
        )
    expected_version = _parse_version(if_match, subject=f"{payload.stage.title()} audit aggregate")
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        stage = _require_stage(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=payload.stage,
            for_update=True,
        )
        if int(stage["version_no"]) != expected_version:
            raise ConflictError(
                error_code="VAC-CONFLICT-005",
                title="Audit version conflict",
                detail="The audit changed since it was loaded. Refresh and retry the action.",
            )
        _validate_evidence(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            evidence_ids=payload.evidenceIds,
        )
        flag_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_findings (
                    tenant_id, journey_id, finding_type_code, severity,
                    finding_status, title, description, created_by_actor_id,
                    correlation_id, stage_code, origin_kind, origin_actor_id,
                    origin_role_snapshot, blocking_completion
                ) VALUES (
                    :tenant_id, :journey_id, :category, :severity,
                    'OPEN', :title, :description, :actor_id,
                    :correlation_id, :stage_code, 'HUMAN', :actor_id,
                    :actor_role, false
                ) RETURNING audit_finding_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "category": category,
                "severity": severity,
                "title": payload.summary.strip(),
                "description": (payload.remarks or "").strip() or None,
                "actor_id": human_principal.subject,
                "correlation_id": correlation_id,
                "stage_code": payload.stage,
                "actor_role": context["operating_role"],
            },
        ).scalar_one()
        _link_evidence(
            connection,
            tenant_id=tenant_id,
            flag_id=flag_id,
            evidence_ids=payload.evidenceIds,
            purpose="FLAG_RAISED",
        )
        event_id = _append_finding_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            flag_id=flag_id,
            stage_code=payload.stage,
            event_type="RAISED",
            actor_id=human_principal.subject,
            actor_role=context["operating_role"],
            reason=(payload.remarks or "").strip() or None,
            correlation_id=correlation_id,
            safe_payload={"originKind": "HUMAN", "category": category, "severity": severity},
        )
        next_version = expected_version + 1
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET audit_status='FLAGS_RAISED',
                    audit_state=CASE WHEN audit_state='NOT_STARTED' THEN 'IN_PROGRESS' ELSE audit_state END,
                    latest_activity_at_utc=now(), updated_at_utc=now(), version_no=:version
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code=:stage_code
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "stage_code": payload.stage,
                "version": next_version,
            },
        )
        row = _finding(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            flag_id=flag_id,
        )
        return {
            "flag": _flag_view(connection, tenant_id=tenant_id, row=row).model_dump(mode="json"),
            "eventId": str(event_id),
            "aggregateVersion": next_version,
        }

    body, replay = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.flag.raise:{journey_id}:{payload.stage}",
        idempotency_key=idempotency_key,
        request_payload={
            "expectedVersion": expected_version,
            "payload": payload.model_dump(mode="json"),
        },
        execute=execute,
    )
    flag = FlagView.model_validate(body["flag"])
    _set_etag(response, flag.version)
    return FlagMutationResponse(flag=flag, eventId=UUID(body["eventId"]), idempotent=replay)


@router.post("/flags/{flag_id}/actions", response_model=FlagMutationResponse)
def act_on_flag(
    tenant_id: str,
    journey_id: UUID,
    flag_id: UUID,
    payload: FlagLifecycleCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
    ],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> FlagMutationResponse:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        operation=payload.action,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_version = _parse_version(if_match, subject="audit flag")
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        row = _finding(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            flag_id=flag_id,
            for_update=True,
        )
        if int(row["version_no"]) != expected_version:
            raise ConflictError(
                error_code="VAC-CONFLICT-005",
                title="Audit flag version conflict",
                detail="The flag changed since it was loaded. Refresh and retry the action.",
            )
        next_status = _transition(payload.action, row["finding_status"])
        _validate_evidence(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            evidence_ids=payload.evidenceIds,
        )
        _link_evidence(
            connection,
            tenant_id=tenant_id,
            flag_id=flag_id,
            evidence_ids=payload.evidenceIds,
            purpose=f"FLAG_{payload.action}",
        )
        reason = (payload.resolutionReason or payload.remarks or "").strip() or None
        connection.execute(
            text(
                """
                UPDATE auditcore.audit_findings
                SET finding_status=:status,
                    resolution_reason=CASE
                        WHEN :action IN ('RESOLVE','VOID') THEN :reason
                        WHEN :action='REOPEN' THEN NULL
                        ELSE resolution_reason
                    END,
                    updated_at_utc=now(), version_no=version_no+1
                WHERE tenant_id=:tenant_id AND audit_finding_id=:flag_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "flag_id": flag_id,
                "status": next_status,
                "action": payload.action,
                "reason": reason,
            },
        )
        event_id = _append_finding_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            flag_id=flag_id,
            stage_code=row["stage_code"],
            event_type=payload.action,
            actor_id=human_principal.subject,
            actor_role=context["operating_role"],
            reason=(payload.remarks or payload.resolutionReason or "").strip() or None,
            correlation_id=correlation_id,
            safe_payload={"fromStatus": row["finding_status"], "toStatus": next_status},
        )
        # A reopened completion-guard flag means the stage has actionable work again.
        # Non-blocking open flags may coexist with Audit State COMPLETE by design.
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET audit_status='FLAGS_RAISED',
                    audit_state=CASE
                        WHEN :action='REOPEN' AND :blocking_completion=true
                             AND audit_state='COMPLETE'
                        THEN 'IN_PROGRESS'
                        ELSE audit_state
                    END,
                    latest_activity_at_utc=now(), updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code=:stage_code
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "stage_code": row["stage_code"],
                "action": payload.action,
                "blocking_completion": bool(row["blocking_completion"]),
            },
        )
        updated = _finding(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            flag_id=flag_id,
        )
        return {
            "flag": _flag_view(connection, tenant_id=tenant_id, row=updated).model_dump(mode="json"),
            "eventId": str(event_id),
        }

    body, replay = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.flag.{payload.action.lower()}:{flag_id}",
        idempotency_key=idempotency_key,
        request_payload={
            "expectedVersion": expected_version,
            "payload": payload.model_dump(mode="json"),
        },
        execute=execute,
        logical_result_id=str(flag_id),
    )
    flag = FlagView.model_validate(body["flag"])
    _set_etag(response, flag.version)
    return FlagMutationResponse(flag=flag, eventId=UUID(body["eventId"]), idempotent=replay)


@router.post("/flags/{flag_id}/remarks", response_model=FlagMutationResponse)
def add_flag_remark(
    tenant_id: str,
    journey_id: UUID,
    flag_id: UUID,
    payload: FlagRemarkCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
    ],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> FlagMutationResponse:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        operation="REMARK",
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_version = _parse_version(if_match, subject="audit flag")
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        row = _finding(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            flag_id=flag_id,
            for_update=True,
        )
        if int(row["version_no"]) != expected_version:
            raise ConflictError(
                error_code="VAC-CONFLICT-005",
                title="Audit flag version conflict",
                detail="The flag changed since it was loaded. Refresh and retry the action.",
            )
        _validate_evidence(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            evidence_ids=payload.evidenceIds,
        )
        _link_evidence(
            connection,
            tenant_id=tenant_id,
            flag_id=flag_id,
            evidence_ids=payload.evidenceIds,
            purpose="FLAG_REMARK",
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.audit_findings
                SET updated_at_utc=now(), version_no=version_no+1
                WHERE tenant_id=:tenant_id AND audit_finding_id=:flag_id
                """
            ),
            {"tenant_id": tenant_id, "flag_id": flag_id},
        )
        event_id = _append_finding_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            flag_id=flag_id,
            stage_code=row["stage_code"],
            event_type="REMARK_ADDED",
            actor_id=human_principal.subject,
            actor_role=context["operating_role"],
            reason=payload.remarks.strip(),
            correlation_id=correlation_id,
            safe_payload={"evidenceLinked": bool(payload.evidenceIds)},
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET latest_activity_at_utc=now(), updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code=:stage_code
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "stage_code": row["stage_code"],
            },
        )
        updated = _finding(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            flag_id=flag_id,
        )
        return {
            "flag": _flag_view(connection, tenant_id=tenant_id, row=updated).model_dump(mode="json"),
            "eventId": str(event_id),
        }

    body, replay = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.flag.remark:{flag_id}",
        idempotency_key=idempotency_key,
        request_payload={
            "expectedVersion": expected_version,
            "payload": payload.model_dump(mode="json"),
        },
        execute=execute,
        logical_result_id=str(flag_id),
    )
    flag = FlagView.model_validate(body["flag"])
    _set_etag(response, flag.version)
    return FlagMutationResponse(flag=flag, eventId=UUID(body["eventId"]), idempotent=replay)


@router.post("/stages/{stage_code}/audit/complete", response_model=StageAuditCompleteResponse)
def complete_stage_audit(
    tenant_id: str,
    journey_id: UUID,
    stage_code: StageCode,
    payload: StageAuditCompleteCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
    ],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> StageAuditCompleteResponse:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        operation="COMPLETE_AUDIT",
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_version = _parse_version(if_match, subject=f"{stage_code.title()} audit aggregate")
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        stage = _require_stage(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage_code,
            for_update=True,
        )
        if int(stage["version_no"]) != expected_version:
            raise ConflictError(
                error_code="VAC-CONFLICT-005",
                title="Audit version conflict",
                detail="The audit changed since it was loaded. Refresh and retry the action.",
            )
        blockers = _stage_completion_blockers(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage_code,
        )
        if blockers:
            raise ConflictError(
                error_code="VAC-CONFLICT-009",
                title="Audit checkpoint is incomplete",
                detail=" ".join(blockers),
            )
        historical_flags = connection.execute(
            text(
                """
                SELECT count(*) FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code=:stage_code AND finding_status <> 'VOIDED'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "stage_code": stage_code},
        ).scalar_one()
        next_version = expected_version + 1
        effective_status = (
            "FLAGS_RAISED"
            if stage["audit_status"] == "FLAGS_RAISED" or historical_flags
            else "NO_FLAGS"
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET audit_state='COMPLETE', audit_status=:audit_status,
                    capture_completed_at_utc=COALESCE(capture_completed_at_utc, now()),
                    latest_activity_at_utc=now(), updated_at_utc=now(),
                    version_no=:version
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code=:stage_code
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "stage_code": stage_code,
                "audit_status": effective_status,
                "version": next_version,
            },
        )
        event_id = _append_stage_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage_code,
            event_type=f"{stage_code}_AUDIT_COMPLETED",
            actor_id=human_principal.subject,
            actor_role=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            aggregate_version=next_version,
            remarks=payload.remarks,
        )
        return {
            "journeyId": str(journey_id),
            "stage": stage_code,
            "auditState": "COMPLETE",
            "auditStatus": effective_status,
            "aggregateVersion": next_version,
            "eventId": str(event_id),
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.audit.complete:{journey_id}:{stage_code}",
        idempotency_key=idempotency_key,
        request_payload={
            "expectedVersion": expected_version,
            "stage": stage_code,
            "payload": payload.model_dump(mode="json"),
        },
        execute=execute,
        logical_result_id=f"{journey_id}:{stage_code}",
    )
    _set_etag(response, int(body["aggregateVersion"]))
    return StageAuditCompleteResponse.model_validate(body)


def _humanize_event(value: str) -> str:
    words = value.replace("_", " ").strip().lower()
    return words[:1].upper() + words[1:]


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@router.get("/timeline", response_model=list[TimelineItem])
def get_timeline(
    tenant_id: str,
    journey_id: UUID,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)] = None,
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ] = None,
    connection: Annotated[Connection, Depends(get_connection)] = None,
) -> list[TimelineItem]:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        operation="READ",
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    items: list[TimelineItem] = []
    workflow_rows = connection.execute(
        text(
            """
            SELECT stage_code, event_type, actor_role_snapshot, occurred_at_utc
            FROM auditcore.journey_workflow_events
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code IN ('BOOKING','DELIVERY')
            ORDER BY occurred_at_utc DESC, event_id DESC
            LIMIT :limit
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "limit": limit},
    ).mappings().all()
    items.extend(
        TimelineItem(
            kind="WORKFLOW",
            stage=row["stage_code"],
            eventType=row["event_type"],
            summary=_humanize_event(row["event_type"]),
            actorRole=row["actor_role_snapshot"],
            remarks=None,
            occurredAtUtc=_as_utc(row["occurred_at_utc"]),
        )
        for row in workflow_rows
    )
    flag_rows = connection.execute(
        text(
            """
            SELECT afe.stage_code, afe.event_type, afe.actor_role_snapshot,
                   afe.reason, afe.occurred_at_utc, af.title
            FROM auditcore.audit_finding_events afe
            JOIN auditcore.audit_findings af
              ON af.tenant_id=afe.tenant_id
             AND af.audit_finding_id=afe.audit_finding_id
            WHERE afe.tenant_id=:tenant_id AND afe.journey_id=:journey_id
              AND afe.stage_code IN ('BOOKING','DELIVERY')
            ORDER BY afe.occurred_at_utc DESC, afe.finding_event_id DESC
            LIMIT :limit
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "limit": limit},
    ).mappings().all()
    items.extend(
        TimelineItem(
            kind="FLAG",
            stage=row["stage_code"],
            eventType=row["event_type"],
            summary=f"{_humanize_event(row['event_type'])}: {row['title']}",
            actorRole=row["actor_role_snapshot"],
            remarks=row["reason"],
            occurredAtUtc=_as_utc(row["occurred_at_utc"]),
        )
        for row in flag_rows
    )
    review_rows = connection.execute(
        text(
            """
            SELECT decision, reviewer_role_code, remarks, decided_at_utc
            FROM auditcore.review_decisions
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            ORDER BY decided_at_utc DESC, review_decision_id DESC
            LIMIT :limit
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "limit": limit},
    ).mappings().all()
    items.extend(
        TimelineItem(
            kind="REVIEW",
            stage=None,
            eventType=f"AUDIT_{row['decision']}",
            summary=f"Audit review: {_humanize_event(row['decision'])}",
            actorRole=row["reviewer_role_code"],
            remarks=row["remarks"],
            occurredAtUtc=_as_utc(row["decided_at_utc"]),
        )
        for row in review_rows
    )
    items.sort(key=lambda item: item.occurredAtUtc, reverse=True)
    return items[:limit]
