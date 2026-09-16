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
from audit_core.uc03_finding_classification import resolve_classification
from audit_core.uc03_finding_routing import (
    class_profile,
    classify_finding,
    escalation_level,
    permitted_actions,
    resolve_sla_policy,
    sla_due_at,
)

# Reused, not duplicated: uc03_tl_supervisory.py already looks up "the PC
# who submitted this Booking" for its own document-reupload-request Task;
# Take Action needs the exact same lookup. Importing a private helper
# across these modules already matches this codebase's own convention
# (uc03_tl_supervisory.py itself imports _aggregate_lock/_parse_if_match
# from uc03_booking_commands the same way).
from audit_core.uc03_tl_supervisory import _responsible_pc_actor
from audit_core.workflow import create_workflow_task

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}/uc03",
    tags=["uc03-audit"],
)

StageCode = Literal["BOOKING", "DELIVERY"]
FlagAction = Literal[
    "ACKNOWLEDGE", "REVIEW", "RESOLVE", "REOPEN", "VOID",
    "CONFIRM_BREACH", "MARK_FALSE_POSITIVE", "TAKE_ACTION", "ESCALATE",
]

# Human-readable label for an action code -- used in error titles instead of
# a blind .title() call, which would render "Confirm_Breach".
_ACTION_LABEL: dict[str, str] = {
    "ACKNOWLEDGE": "Acknowledge",
    "REVIEW": "Review",
    "RESOLVE": "Resolve",
    "REOPEN": "Reopen",
    "VOID": "Void",
    "CONFIRM_BREACH": "Confirm Breach",
    "MARK_FALSE_POSITIVE": "Mark False Positive",
    "TAKE_ACTION": "Take Action",
    "ESCALATE": "Escalate to PM",
}

# v1.1 design: a required category alongside the existing 50-word free-text
# remark on Reject (Mark False Positive) -- cheap now, avoids retrofitting
# categorization onto historical rejections later for Compliance reporting.
_REJECTION_CATEGORIES = {
    "NOT_APPLICABLE", "DATA_ALREADY_CORRECT", "SYSTEM_MISCLASSIFIED",
    "DUPLICATE", "OTHER",
}
_REJECT_REMARK_MAX_WORDS = 50
# Escalate walks one step up this ladder from the finding's current owner.
_ROLE_LADDER = ("PC", "TL", "PM", "EXECUTIVE")

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
    # A separate operation key from RAISE, deliberately -- both call
    # _scope() with the same underlying Security permission
    # (audit.finding.create), but RAISE (create a brand-new Audit Flag
    # observation) and PROPOSE_CORRECTION (submit_field_correction in
    # uc03_document_field_corrections.py -- a PC correcting a low-
    # confidence extracted field) now have genuinely different role
    # policies below. Collapsing them back onto one key would silently
    # re-open or re-close the wrong one.
    "PROPOSE_CORRECTION": "audit.finding.create",
    "REMARK": "audit.finding.update",
    "ACKNOWLEDGE": "audit.review.decide",
    "REVIEW": "audit.review.decide",
    "CONFIRM_BREACH": "audit.review.decide",
    "MARK_FALSE_POSITIVE": "audit.review.decide",
    "TAKE_ACTION": "audit.review.decide",
    "ESCALATE": "audit.review.decide",
    "RESOLVE": "audit.finding.resolve",
    "REOPEN": "audit.finding.resolve",
    "VOID": "audit.finding.resolve",
    "COMPLETE_AUDIT": "audit.journey.update",
}

# v1.1 correction: an earlier reading of "PC can't edit or update Audit
# Findings directly" narrowed this to touching an EXISTING finding only,
# leaving RAISE (a brand-new observation) open to PC. Corrected per direct
# instruction -- PC never raises a Finding either; every observation is
# TL/PM's to record (manually) or the machine's (automatically). PC's own
# work is exclusively the Task Queue.
# A self-serve finding still normally closes itself once its auto-spawned
# Task is completed and the underlying gap is actually fixed -- TL/PM's own
# RESOLVE here is a manual override, not how PC participates.
_DEFAULT_ROLE_POLICY: dict[str, set[str]] = {
    "READ": {"PC", "TL", "PM", "EXECUTIVE"},
    "RAISE": {"TL", "PM", "EXECUTIVE"},
    # Unaffected by the RAISE change above -- a PC proposing a correction to
    # a low-confidence extracted field (Journey Documents, not the Audit
    # Review "Raise Audit Flag" form) is a different, already-shipped
    # workflow: the Unified document review redesign made <90%-confidence
    # corrections PC's own, immediate, self-serve action.
    "PROPOSE_CORRECTION": {"PC", "TL", "PM", "EXECUTIVE"},
    "REMARK": {"TL", "PM", "EXECUTIVE"},
    "ACKNOWLEDGE": {"TL", "PM", "EXECUTIVE"},
    "REVIEW": {"TL", "PM", "EXECUTIVE"},
    # TL's four verdicts on a VIOLATION: Accept (Confirm Breach) / Reject
    # (Mark False Positive) / Take Action (assign to PC) / Escalate (hand
    # to PM). PM has the identical set once escalated to.
    "CONFIRM_BREACH": {"TL", "PM", "EXECUTIVE"},
    "MARK_FALSE_POSITIVE": {"TL", "PM", "EXECUTIVE"},
    "TAKE_ACTION": {"TL", "PM", "EXECUTIVE"},
    "ESCALATE": {"TL", "PM", "EXECUTIVE"},
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
    # Required, not optional: this is the ONLY thing that becomes the
    # finding's description (create_flag's execute() sets description=
    # (payload.remarks or "").strip() or None). Confirmed live: every
    # machine-raised finding in this codebase carries a real, specific
    # description; a human-raised flag with remarks left blank -- the
    # common case when it was merely optional -- was the actual gap,
    # landing with nothing beyond its one-line title to explain it.
    remarks: str = Field(min_length=1, max_length=4000)
    evidenceIds: list[UUID] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def require_non_blank_remarks(self):
        if not self.remarks.strip():
            raise ValueError("Remarks are required and cannot be blank.")
        return self


class FlagLifecycleCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: FlagAction
    remarks: str | None = Field(default=None, max_length=4000)
    resolutionReason: str | None = Field(default=None, max_length=4000)
    evidenceIds: list[UUID] = Field(default_factory=list, max_length=20)
    # TAKE_ACTION only: the severity TL sets on the Task it spawns for PC.
    severity: str | None = Field(default=None, max_length=20)
    # MARK_FALSE_POSITIVE only: the required category alongside the
    # existing free-text remark (v1.1 design).
    rejectionCategory: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def require_reason_for_terminal_or_reopen(self):
        if self.action in {
            "RESOLVE", "REOPEN", "VOID", "CONFIRM_BREACH", "MARK_FALSE_POSITIVE",
            "TAKE_ACTION", "ESCALATE",
        }:
            reason = (self.resolutionReason or self.remarks or "").strip()
            if not reason:
                raise ValueError("A reason is required for this action.")
            if self.action == "MARK_FALSE_POSITIVE":
                word_count = len(reason.split())
                if word_count > _REJECT_REMARK_MAX_WORDS:
                    raise ValueError(
                        f"Reject remarks must be {_REJECT_REMARK_MAX_WORDS} words "
                        f"or fewer (got {word_count})."
                    )
                if (self.rejectionCategory or "").strip().upper() not in _REJECTION_CATEGORIES:
                    raise ValueError(
                        "rejectionCategory is required to Reject a finding, and must "
                        f"be one of {sorted(_REJECTION_CATEGORIES)}."
                    )
        if self.action == "TAKE_ACTION" and not (self.severity or "").strip():
            raise ValueError("severity is required when taking action.")
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
    # ── routing / SLA ───────────────────────────────────────────────
    findingClass: str | None
    resolutionMode: str | None
    ownerRoleCode: str | None
    disposition: str | None
    slaDueAtUtc: datetime | None
    escalationLevel: int
    overdue: bool
    permittedActions: list[str] = Field(default_factory=list)
    # v1.1 additions
    rejectionCategory: str | None = None
    escalationPriority: str | None = None
    # How many times this finding has bounced PC->TL->PC (count of
    # TL_TAKE_ACTION tasks raised against it) -- the visible round-counter
    # guardrail from the v1.1 design, so a stuck item is visible without
    # anyone having to notice by hand.
    bounceCount: int = 0


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
                   blocking_completion, version_no, created_at_utc, updated_at_utc,
                   finding_class, owner_role_code, sla_due_at_utc, disposition,
                   rejection_category, escalation_priority
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
                SELECT CAST(:tenant_id AS varchar), CAST(:flag_id AS uuid),
                       CAST(:evidence_id AS uuid), CAST(:purpose AS varchar)
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


def _flag_view(
    connection: Connection,
    *,
    tenant_id: str,
    row,
    role: str,
    policy,
    now: datetime | None = None,
) -> FlagView:
    moment = now or datetime.now(UTC)
    finding_class = row["finding_class"] or classify_finding(
        row["rule_key"], row["finding_type_code"]
    )
    profile = class_profile(finding_class)
    owner_role = row["owner_role_code"] or profile.owner_role
    due_at = row["sla_due_at_utc"]
    if due_at is None:
        due_at = sla_due_at(
            row["created_at_utc"],
            finding_class=finding_class,
            severity=row["severity"],
            policy=policy,
        )
    level = escalation_level(due_at, moment, policy)
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
        findingClass=finding_class,
        resolutionMode=profile.resolution_mode,
        ownerRoleCode=owner_role,
        disposition=row["disposition"],
        slaDueAtUtc=due_at,
        escalationLevel=level,
        overdue=moment > due_at if due_at is not None else False,
        permittedActions=permitted_actions(
            finding_class=finding_class,
            role=role,
            finding_status=row["finding_status"],
        ),
        rejectionCategory=row.get("rejection_category"),
        escalationPriority=row.get("escalation_priority"),
        bounceCount=int(
            connection.execute(
                text(
                    """
                    SELECT count(*) FROM auditcore.workflow_tasks
                    WHERE tenant_id=:tenant_id AND related_finding_id=:flag_id
                      AND task_type='TL_TAKE_ACTION'
                    """
                ),
                {"tenant_id": tenant_id, "flag_id": row["audit_finding_id"]},
            ).scalar_one()
        ),
    )


_FLAG_LIST_COLUMNS = """
    audit_finding_id, journey_id, finding_type_code, severity,
    finding_status, title, description, expected_summary,
    observed_summary, resolution_reason, stage_code, origin_kind,
    origin_actor_id, origin_role_snapshot, rule_key, rule_version_id,
    blocking_completion, version_no, created_at_utc, updated_at_utc,
    finding_class, owner_role_code, sla_due_at_utc, disposition,
    rejection_category, escalation_priority
"""


def _list_flags(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str | None,
    role: str,
    policy,
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
            f"SELECT {_FLAG_LIST_COLUMNS} FROM auditcore.audit_findings "
            "WHERE tenant_id=:tenant_id AND journey_id=:journey_id "
            "AND stage_code IN ('BOOKING','DELIVERY')"
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
    now = datetime.now(UTC)
    return [
        _flag_view(connection, tenant_id=tenant_id, row=row, role=role, policy=policy, now=now)
        for row in rows
    ]


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
        "CONFIRM_BREACH",
        "MARK_FALSE_POSITIVE",
        "TAKE_ACTION",
        "ESCALATE",
        "RESOLVE",
        "REOPEN",
        "VOID",
        "COMPLETE_AUDIT",
    ]
    return [operation for operation in operations if role in _policy_roles(context, operation)]


def _view_context(context: dict[str, Any]) -> tuple[str, Any]:
    """(operating role, resolved SLA policy) for building FlagViews."""
    return (
        _normalize_role(context["operating_role"]),
        resolve_sla_policy(context.get("policy_settings")),
    )


def _highest_open_severity(flags: list[FlagView]) -> str | None:
    active = [flag.severity for flag in flags if flag.status in {"OPEN", "ACKNOWLEDGED"}]
    if not active:
        return None
    return max(active, key=lambda value: _SEVERITY_ORDER.get(value, 0))


# action → disposition written on the finding when it terminates.
_ACTION_DISPOSITION: dict[str, str] = {
    "RESOLVE": "FIXED",
    "CONFIRM_BREACH": "CONFIRMED_BREACH",
    "MARK_FALSE_POSITIVE": "FALSE_POSITIVE",
}


def _transition(action: FlagAction, current_status: str) -> str:
    allowed: dict[str, tuple[set[str], str]] = {
        "ACKNOWLEDGE": ({"OPEN"}, "ACKNOWLEDGED"),
        "REVIEW": ({"OPEN", "ACKNOWLEDGED"}, "ACKNOWLEDGED"),
        "RESOLVE": ({"OPEN", "ACKNOWLEDGED"}, "RESOLVED"),
        "CONFIRM_BREACH": ({"OPEN", "ACKNOWLEDGED"}, "RESOLVED"),
        "MARK_FALSE_POSITIVE": ({"OPEN", "ACKNOWLEDGED"}, "RESOLVED"),
        # Neither closes the finding -- Take Action hands work to PC and
        # Escalate hands the verdict to PM; both leave it open, acknowledged
        # by whoever just acted, awaiting the next step.
        "TAKE_ACTION": ({"OPEN", "ACKNOWLEDGED"}, "ACKNOWLEDGED"),
        "ESCALATE": ({"OPEN", "ACKNOWLEDGED"}, "ACKNOWLEDGED"),
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
              -- Only requirements sourced from the published checklist profile
              -- (document_requirement_item_id set) go through PC declaration.
              -- A requirement registered directly on the journey outside that
              -- profile (bank_statement_extract, 0070) is just another
              -- document type DI can classify against -- upload/classify/
              -- extract/reconcile, no "do you have this" checklist question.
              AND jdr.document_requirement_item_id IS NOT NULL
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
    role, policy = _view_context(context)
    flags = _list_flags(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=None,
        role=role,
        policy=policy,
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
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        operation="READ",
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    role, policy = _view_context(context)
    return _list_flags(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=stage,
        role=role,
        policy=policy,
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
    # Hard guard, not just a hidden button: PC never raises a Finding,
    # regardless of what Security's own RBAC grant for audit.finding.create
    # happens to allow -- corrected per direct instruction (an earlier
    # reading of "PC can't edit/update Findings" wrongly left RAISE open).
    if _normalize_role(context["operating_role"]) == "PC":
        raise AuthorizationError(
            error_code="VAC-AUTH-005",
            status_code=403,
            title="A Process Coordinator raises no Audit Findings -- record this via your assigned Task, or ask a Team Lead to raise it.",
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
        routing = resolve_classification(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            rule_key=None,
            finding_type_code=category,
            severity=severity,
        )
        flag_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_findings (
                    tenant_id, journey_id, finding_type_code, severity,
                    finding_status, title, description, created_by_actor_id,
                    correlation_id, stage_code, origin_kind, origin_actor_id,
                    origin_role_snapshot, blocking_completion,
                    finding_class, owner_role_code, sla_due_at_utc
                ) VALUES (
                    :tenant_id, :journey_id, :category, :severity,
                    'OPEN', :title, :description, :actor_id,
                    :correlation_id, :stage_code, 'HUMAN', :actor_id,
                    :actor_role, false,
                    :finding_class, :owner_role_code, :sla_due_at_utc
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
                **routing,
            },
        ).scalar_one()
        _link_evidence(
            connection,
            tenant_id=tenant_id,
            flag_id=flag_id,
            evidence_ids=payload.evidenceIds,
            purpose="FLAG_RAISED",
        )
        # v1.1 design: "a PC never opens a Finding" applies no matter who
        # raised it -- a TL/PM observation against a self-serve category
        # (e.g. DOCUMENT_EXCEPTION) needs the same auto-spawned Task PC
        # gets from a machine-raised gap (uc03_delivery_commands.py::
        # _machine_flag carries the identical block for that path).
        if routing.get("finding_class") in {"DATA_GAP", "DOCUMENT_GAP"}:
            try:
                pc_actor_id = _responsible_pc_actor(
                    connection, tenant_id=tenant_id, journey_id=journey_id
                )
            except AuditCoreError:
                pc_actor_id = None
            create_workflow_task(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                workflow_type="UC03_SELF_SERVE_FINDING",
                process_area=payload.stage,
                task_type="AUTO_SELF_SERVE",
                assigned_role_code="PC",
                assigned_actor_id=pc_actor_id,
                related_finding_id=flag_id,
                severity=severity,
                task_payload={"category": category, "findingId": str(flag_id)},
                effect_key=f"task:{flag_id}:round:0",
                correlation_id=correlation_id,
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
        role, policy = _view_context(context)
        return {
            "flag": _flag_view(
                connection, tenant_id=tenant_id, row=row, role=role, policy=policy
            ).model_dump(mode="json"),
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


def apply_finding_verdict(
    connection: Connection,
    *,
    tenant_id: str,
    flag_id: UUID,
    journey_id: UUID | None,
    daily_ops_run_id: UUID | None,
    process_area: str,
    finding_class: str,
    resolution_mode: str,
    current_status: str,
    current_owner_role: str | None,
    payload: FlagLifecycleCommand,
    operating_role: str,
    actor_id: str,
    correlation_id: str | None,
) -> dict[str, Any]:
    """Apply one of TL/PM's verdicts (Accept/Reject/Take Action/Escalate), or
    a plain Resolve/Reopen/Void/Acknowledge/Review, to a Finding -- shared by
    the journey (this module's own act_on_flag) and Daily Operations
    (uc03_daily_ops_flags.py::act_on_daily_ops_flag) endpoints, which
    otherwise differ only in which subject (journey vs daily_ops_run) and
    case-context columns they carry. Journey-only side effects (evidence
    linking, the DI-value-correction auto-apply, journey_stage_states
    bookkeeping) stay local to act_on_flag; this covers everything
    genuinely shared: the guards, the audit_findings UPDATE, and the
    Take-Action task spawn.

    Returns the fields the caller needs to append its own finding event and
    build its response.
    """
    # Accept / Reject / Take Action / Escalate are TL's four verdicts on a
    # rule breach -- only for adjudicated findings (VIOLATION). A data /
    # document gap is fixed by its auto-spawned Task, not adjudicated.
    if (
        payload.action in {"CONFIRM_BREACH", "MARK_FALSE_POSITIVE", "TAKE_ACTION", "ESCALATE"}
        and resolution_mode != "ADJUDICATED"
    ):
        raise AuthorizationError(
            error_code="VAC-AUTH-005",
            status_code=403,
            title=(
                f"{_ACTION_LABEL.get(payload.action, payload.action)} is not "
                f"available for a {finding_class.replace('_', ' ').lower()}"
            ),
        )
    # Race-condition guardrail (v1.1 design): at most one open Task per
    # Finding at a time. Take Action always creates one; Escalate must not
    # leave an existing one orphaned with two people each thinking they're
    # the next step.
    if payload.action in {"TAKE_ACTION", "ESCALATE"}:
        open_task = connection.execute(
            text(
                """
                SELECT workflow_task_id FROM auditcore.workflow_tasks
                WHERE tenant_id=:tenant_id AND related_finding_id=:flag_id
                  AND task_status IN ('PENDING','READY','CLAIMED','IN_PROGRESS','RETRY_WAIT')
                LIMIT 1
                """
            ),
            {"tenant_id": tenant_id, "flag_id": flag_id},
        ).scalar_one_or_none()
        if open_task is not None:
            raise ConflictError(
                error_code="VAC-CONFLICT-011",
                title="A task is already open on this finding",
                detail="Wait for the assigned Task to complete, or cancel it, before taking this action again.",
            )
    # A PC may close their own data / document gap, but never a VIOLATION --
    # that needs a TL / PM verdict.
    if (
        payload.action == "RESOLVE"
        and _normalize_role(operating_role) == "PC"
        and resolution_mode == "ADJUDICATED"
    ):
        raise AuthorizationError(
            error_code="VAC-AUTH-005",
            status_code=403,
            title="A violation must be Confirmed Breach or Marked False Positive by a Team Lead or PM",
        )
    next_status = _transition(payload.action, current_status)
    reason = (payload.resolutionReason or payload.remarks or "").strip() or None
    disposition = _ACTION_DISPOSITION.get(payload.action)
    rejection_category = (
        payload.rejectionCategory.strip().upper()
        if payload.action == "MARK_FALSE_POSITIVE" and payload.rejectionCategory
        else None
    )
    # Escalate hands ownership to the next role up the ladder (TL -> PM, PM
    # -> EXECUTIVE) and tags the escalation itself high-priority -- the
    # finding's own severity (the rule's original assessment) is
    # deliberately never overwritten (v1.1 design).
    escalated_owner_role = None
    escalation_priority = None
    if payload.action == "ESCALATE":
        try:
            current_rank = _ROLE_LADDER.index(_normalize_role(current_owner_role or "TL"))
        except ValueError:
            current_rank = _ROLE_LADDER.index("TL")
        escalated_owner_role = _ROLE_LADDER[min(current_rank + 1, len(_ROLE_LADDER) - 1)]
        escalation_priority = "HIGH"
    connection.execute(
        text(
            """
            UPDATE auditcore.audit_findings
            SET finding_status=:status,
                resolution_reason=CASE
                    WHEN :action IN ('RESOLVE','VOID','CONFIRM_BREACH','MARK_FALSE_POSITIVE')
                        THEN CAST(:reason AS text)
                    WHEN :action='REOPEN' THEN NULL
                    ELSE resolution_reason
                END,
                disposition=CASE
                    WHEN :action='REOPEN' THEN NULL
                    WHEN :set_disposition THEN CAST(:disposition AS varchar)
                    ELSE disposition
                END,
                rejection_category=CASE
                    WHEN :action='MARK_FALSE_POSITIVE' THEN CAST(:rejection_category AS varchar)
                    ELSE rejection_category
                END,
                owner_role_code=COALESCE(CAST(:escalated_owner_role AS varchar), owner_role_code),
                escalation_priority=COALESCE(CAST(:escalation_priority AS varchar), escalation_priority),
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
            "set_disposition": disposition is not None,
            "disposition": disposition,
            "rejection_category": rejection_category,
            "escalated_owner_role": escalated_owner_role,
            "escalation_priority": escalation_priority,
        },
    )
    # Race-condition guardrail (v1.1 design): closing a Finding always
    # cleans up after itself instead of being blocked while a Task is open
    # -- implemented once, as a trigger on auditcore.audit_findings
    # (migration 0098), not duplicated here.
    if payload.action == "TAKE_ACTION":
        # Best-effort specific assignee -- a Delivery-stage journey finding
        # (no recorded Booking-capture submitter yet) or a Daily Operations
        # finding (no such lookup exists at all) falls back to role-only
        # assignment (any PC with business-scope access can claim it)
        # rather than failing the whole Take Action over an assignee that
        # doesn't resolve.
        pc_actor_id = None
        if journey_id is not None:
            try:
                pc_actor_id = _responsible_pc_actor(
                    connection, tenant_id=tenant_id, journey_id=journey_id
                )
            except AuditCoreError:
                pc_actor_id = None
        round_no = int(
            connection.execute(
                text(
                    """
                    SELECT count(*) FROM auditcore.workflow_tasks
                    WHERE tenant_id=:tenant_id AND related_finding_id=:flag_id
                      AND task_type='TL_TAKE_ACTION'
                    """
                ),
                {"tenant_id": tenant_id, "flag_id": flag_id},
            ).scalar_one()
        ) + 1
        create_workflow_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            daily_ops_run_id=daily_ops_run_id,
            workflow_type="UC03_FINDING_TAKE_ACTION",
            process_area=process_area,
            task_type="TL_TAKE_ACTION",
            assigned_role_code="PC",
            assigned_actor_id=pc_actor_id,
            related_finding_id=flag_id,
            severity=payload.severity,
            task_payload={
                "comment": reason,
                "severity": (payload.severity or "").strip().upper(),
                "issuedByActorId": actor_id,
                "issuedByRole": operating_role,
                "round": round_no,
            },
            effect_key=f"task:{flag_id}:round:{round_no}",
            correlation_id=correlation_id,
        )
    return {
        "next_status": next_status,
        "reason": reason,
        "disposition": disposition,
        "rejection_category": rejection_category,
        "escalated_owner_role": escalated_owner_role,
        "escalation_priority": escalation_priority,
    }


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
        finding_class = row["finding_class"] or classify_finding(
            row["rule_key"], row["finding_type_code"]
        )
        resolution_mode = class_profile(finding_class).resolution_mode
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
        verdict = apply_finding_verdict(
            connection,
            tenant_id=tenant_id,
            flag_id=flag_id,
            journey_id=journey_id,
            daily_ops_run_id=None,
            process_area=row["stage_code"],
            finding_class=finding_class,
            resolution_mode=resolution_mode,
            current_status=row["finding_status"],
            current_owner_role=row["owner_role_code"],
            payload=payload,
            operating_role=context["operating_role"],
            actor_id=human_principal.subject,
            correlation_id=correlation_id,
        )
        next_status = verdict["next_status"]
        # Unified Documents review (2026-09-13): a Confirm-Breach verdict on a
        # DI_VALUE_CORRECTION_PROPOSED finding actually applies the proposed
        # value -- the one place this generic handler has a finding-type-
        # specific side effect, deliberately narrow (see
        # uc03_document_field_corrections.py's module docstring for why it
        # isn't a parallel approval system). Mark-False-Positive needs no
        # extra step: the finding resolves and the original DI value stands.
        # Local import to avoid a circular import (that module imports this
        # one's _scope/_finding/_flag_view for its own propose endpoint).
        if (
            payload.action == "CONFIRM_BREACH"
            and row["finding_type_code"] == "DI_VALUE_CORRECTION_PROPOSED"
        ):
            from audit_core.uc03_document_field_corrections import (
                apply_confirmed_field_correction,
            )

            apply_confirmed_field_correction(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                audit_finding_id=flag_id,
                actor_id=human_principal.subject,
            )
        # Same pattern, a second finding-type-specific side effect: WRONG_
        # DOCUMENT holds its document's fields out of materialization the
        # instant it's raised (uc03_customer_identity_consistency.py) --
        # the TL's verdict here is what actually resolves that hold, one
        # way or the other. Only the customer-name variant of this finding
        # (rule_key "WRONG_DOCUMENT:{document_id}", two segments) carries a
        # hold to resolve; the sibling receipt-vs-dealer check (three
        # segments, "WRONG_DOCUMENT:DEALER:{document_id}") is a narrower
        # question with nothing to release or reject.
        if row["finding_type_code"] == "WRONG_DOCUMENT":
            rule_parts = str(row["rule_key"] or "").split(":")
            if len(rule_parts) == 2:
                from audit_core.uc03_customer_identity_consistency import (
                    reject_wrong_document,
                    release_wrong_document_hold,
                )

                di_document_id = UUID(rule_parts[1])
                if payload.action == "CONFIRM_BREACH":
                    reject_wrong_document(
                        connection,
                        tenant_id=tenant_id,
                        journey_id=journey_id,
                        di_document_id=di_document_id,
                        stage_code=row["stage_code"],
                        actor_id=human_principal.subject,
                        reason=verdict["reason"],
                        correlation_id=correlation_id,
                        related_finding_id=flag_id,
                    )
                elif payload.action == "MARK_FALSE_POSITIVE":
                    release_wrong_document_hold(
                        connection, tenant_id=tenant_id, di_document_id=di_document_id,
                    )
                    # The hold's whole point is to keep a wrong document's
                    # data out of materialization before a human decides --
                    # releasing it must take effect now, not wait for some
                    # unrelated future document sync to happen to re-run
                    # materialization. Booking-only: materialize_machine_
                    # booking_values' own scope, matching every other
                    # caller of it.
                    if str(row["stage_code"]).upper() == "BOOKING":
                        from audit_core.uc03_post_extraction_materialization import (
                            materialize_machine_booking_values,
                        )

                        materialize_machine_booking_values(
                            connection, tenant_id=tenant_id, journey_id=journey_id,
                        )
        # Same pattern, a third finding-type-specific side effect: a
        # Confirm-Breach on a PC-proposed correction to an already-CONFIRMED
        # SKU selection actually reassigns it (uc03_model_selection_
        # corrections.py) -- the one path allowed to bypass _pin_sku's own
        # guard against overwriting a locked-in deal, precisely because this
        # IS the deliberate human override that guard exists to require.
        # Mark-False-Positive needs no extra step: the original SKU stands.
        if (
            payload.action == "CONFIRM_BREACH"
            and row["finding_type_code"] == "MODEL_SELECTION_CORRECTION_PROPOSED"
        ):
            from audit_core.uc03_model_selection_corrections import (
                apply_confirmed_model_selection_correction,
            )

            apply_confirmed_model_selection_correction(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                audit_finding_id=flag_id,
                correlation_id=correlation_id,
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
        role, policy = _view_context(context)
        return {
            "flag": _flag_view(
                connection, tenant_id=tenant_id, row=updated, role=role, policy=policy
            ).model_dump(mode="json"),
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
        role, policy = _view_context(context)
        return {
            "flag": _flag_view(
                connection, tenant_id=tenant_id, row=updated, role=role, policy=policy
            ).model_dump(mode="json"),
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
