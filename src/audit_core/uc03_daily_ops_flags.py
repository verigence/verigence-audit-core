"""uc03_daily_ops_flags.py — Audit Flags raised against a Daily Operations
run rather than a specific journey (a cash-count discrepancy, a run left
open past business close, an outlet-level process gap).

Shares the same ``auditcore.audit_findings`` table as Booking/Delivery
journey flags (finding_class/severity/owner_role_code/sla_due_at_utc all
mean the same thing, resolved by the same ``uc03_finding_routing``
functions) via ``subject_kind='DAILY_OPS'`` and ``daily_ops_run_id`` (see
migration 0080) -- so the tenant-wide Review Queue
(``uc03_review_queue.py``) can list both kinds side by side under one
Journey/Daily Operations tab toggle.

Deliberately a separate, smaller module rather than a generalization of
``uc03_audit_flags.py`` (UC03's Booking/Delivery flag module): that
module's raise/act functions are tightly coupled to
``journey_stage_states``' own per-stage aggregate-version state machine,
evidence linked to journey documents, and Booking/Delivery completion
gating -- none of which exists for a Daily Operations run, whose own
aggregate is ``daily_ops_runs.version_no`` with no stage concept at all.
Retrofitting that module to also carry a fundamentally different subject
was judged riskier than a purpose-built, smaller sibling.

No automated Daily Ops audit rules exist yet -- every flag here is raised
manually by a TL/PM, mirroring ``uc03_audit_flags.py``'s own manual "Raise
Audit Flag" path exactly, just against a different subject and without
document-evidence linking (a Daily Operations run has no journey documents
to link).

Authorization deliberately matches ``daily_operations_api.py`` (the
existing owner of this resource), not ``uc03_audit_flags.py``: the plain
``authorize()``/``Principal`` permission check plus
``require_business_scope()``, reusing its own already-provisioned
``audit.daily_ops.read``/``audit.daily_ops.execute`` permission keys
rather than inventing new ones that would need separate Security-service
provisioning before they could ever succeed.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError, authorize
from audit_core.business_assignments import require_business_scope
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_principal
from audit_core.errors import AuditCoreError, ConflictError, NotFoundError
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import Principal
from audit_core.uc03_audit_flags import (
    _ACTION_DISPOSITION,
    _HUMAN_FLAG_CATEGORIES,
    FlagLifecycleCommand,
    FlagRemarkCommand,
    _normalize_role,
    _transition,
)
from audit_core.uc03_finding_routing import (
    class_profile,
    classify_finding,
    escalation_level,
    permitted_actions,
    resolve_sla_policy,
    sla_due_at,
)

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/outlets/{outlet_id}/daily-ops/{daily_ops_run_id}",
    tags=["uc03-daily-ops-audit"],
)

_SEVERITIES = {"INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
# No per-project SLA/escalation policy override exists for Daily Ops runs
# (unlike journeys, which can carry one via project_policy_versions) --
# every Daily Ops flag uses the default policy.
_DEFAULT_POLICY = resolve_sla_policy(None)


class DailyOpsFlagCreateCommand(BaseModel):
    category: str = Field(min_length=1, max_length=100)
    severity: str = Field(min_length=1, max_length=20)
    summary: str = Field(min_length=1, max_length=500)
    remarks: str | None = Field(default=None, max_length=4000)


class DailyOpsFlagView(BaseModel):
    flagId: UUID
    dailyOpsRunId: UUID
    category: str | None
    severity: str
    status: str
    title: str
    description: str | None
    resolutionReason: str | None
    version: int
    createdAtUtc: datetime
    updatedAtUtc: datetime
    findingClass: str | None
    resolutionMode: str | None
    ownerRoleCode: str | None
    disposition: str | None
    slaDueAtUtc: datetime | None
    escalationLevel: int
    overdue: bool
    permittedActions: list[str] = Field(default_factory=list)


class DailyOpsFlagMutationResponse(BaseModel):
    flag: DailyOpsFlagView
    eventId: UUID
    idempotent: bool = False


def _run_context(
    connection: Connection,
    *,
    tenant_id: str,
    outlet_id: UUID,
    daily_ops_run_id: UUID,
    principal: Principal,
    for_update: bool = False,
) -> dict[str, Any]:
    lock = " FOR UPDATE" if for_update else ""
    run = connection.execute(
        text(
            f"""
            SELECT dor.daily_ops_run_id, dor.outlet_id, dor.version_no, do_.dealer_id
            FROM auditcore.daily_ops_runs dor
            JOIN auditcore.dealer_outlets do_
              ON do_.tenant_id = dor.tenant_id AND do_.outlet_id = dor.outlet_id
            WHERE dor.tenant_id=:tenant_id AND dor.daily_ops_run_id=:run_id
              AND dor.outlet_id=:outlet_id
            {lock}
            """
        ),
        {"tenant_id": tenant_id, "run_id": daily_ops_run_id, "outlet_id": outlet_id},
    ).mappings().one_or_none()
    if run is None:
        raise NotFoundError(
            error_code="VAC-NF-020",
            title="Daily Operations run not found",
            detail="The requested Daily Operations run was not found for this outlet.",
        )
    require_business_scope(
        connection, principal, tenant_id=tenant_id, dealer_id=run["dealer_id"], outlet_id=outlet_id,
    )
    roles = connection.execute(
        text(
            """
            SELECT array_agg(DISTINCT business_role_code ORDER BY business_role_code)
            FROM auditcore.business_assignments
            WHERE tenant_id=:tenant_id AND security_actor_id=:actor_id
              AND assignment_status='ACTIVE'
              AND effective_from <= now()
              AND (effective_to IS NULL OR effective_to >= now())
              AND (dealer_id IS NULL OR (dealer_id=:dealer_id AND (outlet_id IS NULL OR outlet_id=:outlet_id)))
            """
        ),
        {
            "tenant_id": tenant_id,
            "actor_id": principal.subject,
            "dealer_id": run["dealer_id"],
            "outlet_id": outlet_id,
        },
    ).scalar_one()
    if not roles:
        raise AuthorizationError(error_code="VAC-AUTH-002", status_code=403, title="Permission denied")
    if len(roles) != 1:
        raise ConflictError(
            error_code="VAC-CONFLICT-006",
            title="Ambiguous operating role",
            detail="The current assignments resolve to more than one operating role for this outlet.",
        )
    result = dict(run)
    result["operating_role"] = _normalize_role(roles[0])
    return result


def _finding(connection: Connection, *, tenant_id: str, flag_id: UUID, for_update: bool = False):
    lock = " FOR UPDATE" if for_update else ""
    row = connection.execute(
        text(
            f"""
            SELECT audit_finding_id, daily_ops_run_id, finding_type_code, severity,
                   finding_status, title, description, resolution_reason,
                   version_no, created_at_utc, updated_at_utc,
                   finding_class, owner_role_code, sla_due_at_utc, disposition
            FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND subject_kind='DAILY_OPS'
              AND audit_finding_id=:flag_id
            {lock}
            """
        ),
        {"tenant_id": tenant_id, "flag_id": flag_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-021",
            title="Daily Operations flag not found",
            detail="The requested Daily Operations audit flag was not found.",
        )
    return row


def _flag_view(row, *, role: str, now: datetime | None = None) -> DailyOpsFlagView:
    moment = now or datetime.now(UTC)
    finding_class = row["finding_class"] or classify_finding(None, row["finding_type_code"])
    profile = class_profile(finding_class)
    owner_role = row["owner_role_code"] or profile.owner_role
    due_at = row["sla_due_at_utc"]
    if due_at is None:
        due_at = sla_due_at(
            row["created_at_utc"], finding_class=finding_class, severity=row["severity"], policy=_DEFAULT_POLICY,
        )
    level = escalation_level(due_at, moment, _DEFAULT_POLICY)
    return DailyOpsFlagView(
        flagId=row["audit_finding_id"],
        dailyOpsRunId=row["daily_ops_run_id"],
        category=row["finding_type_code"],
        severity=row["severity"],
        status=row["finding_status"],
        title=row["title"],
        description=row["description"],
        resolutionReason=row["resolution_reason"],
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
        permittedActions=permitted_actions(finding_class=finding_class, role=role, finding_status=row["finding_status"]),
    )


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
            error_code="VAC-VAL-001", status_code=400, title="Validation failed",
            detail=f"If-Match must contain the expected {subject} version.",
        ) from exc
    if version < 0:
        raise AuditCoreError(
            error_code="VAC-VAL-001", status_code=400, title="Validation failed",
            detail=f"If-Match {subject} version cannot be negative.",
        )
    return version


def _append_event(
    connection: Connection, *, tenant_id: str, daily_ops_run_id: UUID, flag_id: UUID,
    event_type: str, actor_id: str, actor_role: str, reason: str | None, correlation_id: str,
    safe_payload: dict[str, Any],
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_finding_events (
                tenant_id, audit_finding_id, daily_ops_run_id, stage_code,
                event_type, actor_id, actor_role_snapshot, reason,
                safe_payload, correlation_id
            ) VALUES (
                :tenant_id, :flag_id, :daily_ops_run_id, 'DAILY_OPS',
                :event_type, :actor_id, :actor_role, :reason,
                CAST(:safe_payload AS jsonb), :correlation_id
            ) RETURNING finding_event_id
            """
        ),
        {
            "tenant_id": tenant_id, "flag_id": flag_id, "daily_ops_run_id": daily_ops_run_id,
            "event_type": event_type, "actor_id": actor_id, "actor_role": actor_role,
            "reason": reason, "safe_payload": json.dumps(safe_payload, default=str),
            "correlation_id": correlation_id,
        },
    ).scalar_one()


@router.get("/flags", response_model=list[DailyOpsFlagView])
def list_daily_ops_flags(
    tenant_id: str,
    outlet_id: UUID,
    daily_ops_run_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[DailyOpsFlagView]:
    authorize(principal, tenant_id=tenant_id, permission="audit.daily_ops.read")
    set_tenant_context(connection, tenant_id)
    context = _run_context(
        connection, tenant_id=tenant_id, outlet_id=outlet_id, daily_ops_run_id=daily_ops_run_id, principal=principal,
    )
    rows = connection.execute(
        text(
            """
            SELECT audit_finding_id, daily_ops_run_id, finding_type_code, severity,
                   finding_status, title, description, resolution_reason,
                   version_no, created_at_utc, updated_at_utc,
                   finding_class, owner_role_code, sla_due_at_utc, disposition
            FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND subject_kind='DAILY_OPS' AND daily_ops_run_id=:run_id
            ORDER BY
              CASE severity WHEN 'CRITICAL' THEN 5 WHEN 'HIGH' THEN 4 WHEN 'MEDIUM' THEN 3 WHEN 'LOW' THEN 2 ELSE 1 END DESC,
              created_at_utc DESC, audit_finding_id DESC
            """
        ),
        {"tenant_id": tenant_id, "run_id": daily_ops_run_id},
    ).mappings().all()
    now = datetime.now(UTC)
    return [_flag_view(row, role=context["operating_role"], now=now) for row in rows]


@router.post("/flags", response_model=DailyOpsFlagMutationResponse)
def create_daily_ops_flag(
    tenant_id: str,
    outlet_id: UUID,
    daily_ops_run_id: UUID,
    payload: DailyOpsFlagCreateCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DailyOpsFlagMutationResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.daily_ops.execute")
    set_tenant_context(connection, tenant_id)
    category = payload.category.strip().upper()
    severity = payload.severity.strip().upper()
    if category not in _HUMAN_FLAG_CATEGORIES or severity not in _SEVERITIES:
        raise AuditCoreError(
            error_code="VAC-VAL-002", status_code=422, title="Business validation failed",
            detail="The selected audit flag category or severity is not enabled.",
        )
    expected_version = _parse_version(if_match, subject="Daily Operations run")
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        context = _run_context(
            connection, tenant_id=tenant_id, outlet_id=outlet_id, daily_ops_run_id=daily_ops_run_id,
            principal=principal, for_update=True,
        )
        if int(context["version_no"]) != expected_version:
            raise ConflictError(
                error_code="VAC-CONFLICT-005", title="Daily Operations run version conflict",
                detail="The run changed since it was loaded. Refresh and retry the action.",
            )
        finding_class = classify_finding(None, category)
        profile = class_profile(finding_class)
        due_at = sla_due_at(
            datetime.now(UTC), finding_class=finding_class, severity=severity, policy=_DEFAULT_POLICY,
        )
        flag_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_findings (
                    tenant_id, subject_kind, daily_ops_run_id, finding_type_code, severity,
                    finding_status, title, description, created_by_actor_id,
                    correlation_id, origin_kind, origin_actor_id, origin_role_snapshot,
                    blocking_completion, finding_class, owner_role_code, sla_due_at_utc
                ) VALUES (
                    :tenant_id, 'DAILY_OPS', :run_id, :category, :severity,
                    'OPEN', :title, :description, :actor_id,
                    :correlation_id, 'HUMAN', :actor_id, :actor_role,
                    false, :finding_class, :owner_role_code, :sla_due_at
                ) RETURNING audit_finding_id
                """
            ),
            {
                "tenant_id": tenant_id, "run_id": daily_ops_run_id, "category": category,
                "severity": severity, "title": payload.summary.strip(),
                "description": (payload.remarks or "").strip() or None,
                "actor_id": principal.subject, "correlation_id": correlation_id,
                "actor_role": context["operating_role"], "finding_class": finding_class,
                "owner_role_code": profile.owner_role, "sla_due_at": due_at,
            },
        ).scalar_one()
        event_id = _append_event(
            connection, tenant_id=tenant_id, daily_ops_run_id=daily_ops_run_id, flag_id=flag_id,
            event_type="RAISED", actor_id=principal.subject, actor_role=context["operating_role"],
            reason=(payload.remarks or "").strip() or None, correlation_id=correlation_id,
            safe_payload={"originKind": "HUMAN", "category": category, "severity": severity},
        )
        row = _finding(connection, tenant_id=tenant_id, flag_id=flag_id)
        return {
            "flag": _flag_view(row, role=context["operating_role"]).model_dump(mode="json"),
            "eventId": str(event_id),
        }

    body, replay = execute_idempotent_json_command(
        connection, tenant_id=tenant_id,
        operation_key=f"uc03.daily-ops-flag.raise:{daily_ops_run_id}",
        idempotency_key=idempotency_key,
        request_payload={"expectedVersion": expected_version, "payload": payload.model_dump(mode="json")},
        execute=execute,
    )
    flag = DailyOpsFlagView.model_validate(body["flag"])
    response.headers["ETag"] = f'"{flag.version}"'
    return DailyOpsFlagMutationResponse(flag=flag, eventId=UUID(body["eventId"]), idempotent=replay)


@router.post("/flags/{flag_id}/actions", response_model=DailyOpsFlagMutationResponse)
def act_on_daily_ops_flag(
    tenant_id: str,
    outlet_id: UUID,
    daily_ops_run_id: UUID,
    flag_id: UUID,
    payload: FlagLifecycleCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DailyOpsFlagMutationResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.daily_ops.execute")
    set_tenant_context(connection, tenant_id)
    expected_version = _parse_version(if_match, subject="Daily Operations flag")
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        context = _run_context(
            connection, tenant_id=tenant_id, outlet_id=outlet_id, daily_ops_run_id=daily_ops_run_id, principal=principal,
        )
        row = _finding(connection, tenant_id=tenant_id, flag_id=flag_id, for_update=True)
        if int(row["version_no"]) != expected_version:
            raise ConflictError(
                error_code="VAC-CONFLICT-005", title="Daily Operations flag version conflict",
                detail="The flag changed since it was loaded. Refresh and retry the action.",
            )
        finding_class = row["finding_class"] or classify_finding(None, row["finding_type_code"])
        resolution_mode = class_profile(finding_class).resolution_mode
        if payload.action in {"ACCEPT", "REJECT"} and resolution_mode != "ADJUDICATED":
            raise AuthorizationError(
                error_code="VAC-AUTH-005", status_code=403,
                title=f"{payload.action.title()} is not available for a {finding_class.replace('_', ' ').lower()}",
            )
        if payload.action == "RESOLVE" and context["operating_role"] == "PC" and resolution_mode == "ADJUDICATED":
            raise AuthorizationError(
                error_code="VAC-AUTH-005", status_code=403,
                title="A violation must be accepted or rejected by a Team Lead or PM",
            )
        next_status = _transition(payload.action, row["finding_status"])
        reason = (payload.resolutionReason or payload.remarks or "").strip() or None
        disposition = _ACTION_DISPOSITION.get(payload.action)
        connection.execute(
            text(
                """
                UPDATE auditcore.audit_findings
                SET finding_status=:status,
                    resolution_reason=CASE
                        WHEN :action IN ('RESOLVE','VOID','ACCEPT','REJECT') THEN CAST(:reason AS text)
                        WHEN :action='REOPEN' THEN NULL
                        ELSE resolution_reason
                    END,
                    disposition=CASE
                        WHEN :action='REOPEN' THEN NULL
                        WHEN :set_disposition THEN CAST(:disposition AS varchar)
                        ELSE disposition
                    END,
                    updated_at_utc=now(), version_no=version_no+1
                WHERE tenant_id=:tenant_id AND audit_finding_id=:flag_id
                """
            ),
            {
                "tenant_id": tenant_id, "flag_id": flag_id, "status": next_status, "action": payload.action,
                "reason": reason, "set_disposition": disposition is not None, "disposition": disposition,
            },
        )
        event_id = _append_event(
            connection, tenant_id=tenant_id, daily_ops_run_id=daily_ops_run_id, flag_id=flag_id,
            event_type=payload.action, actor_id=principal.subject, actor_role=context["operating_role"],
            reason=(payload.remarks or payload.resolutionReason or "").strip() or None,
            correlation_id=correlation_id,
            safe_payload={"fromStatus": row["finding_status"], "toStatus": next_status},
        )
        updated = _finding(connection, tenant_id=tenant_id, flag_id=flag_id)
        return {
            "flag": _flag_view(updated, role=context["operating_role"]).model_dump(mode="json"),
            "eventId": str(event_id),
        }

    body, replay = execute_idempotent_json_command(
        connection, tenant_id=tenant_id,
        operation_key=f"uc03.daily-ops-flag.{payload.action.lower()}:{flag_id}",
        idempotency_key=idempotency_key,
        request_payload={"expectedVersion": expected_version, "payload": payload.model_dump(mode="json")},
        execute=execute, logical_result_id=str(flag_id),
    )
    flag = DailyOpsFlagView.model_validate(body["flag"])
    response.headers["ETag"] = f'"{flag.version}"'
    return DailyOpsFlagMutationResponse(flag=flag, eventId=UUID(body["eventId"]), idempotent=replay)


@router.post("/flags/{flag_id}/remarks", response_model=DailyOpsFlagMutationResponse)
def add_daily_ops_flag_remark(
    tenant_id: str,
    outlet_id: UUID,
    daily_ops_run_id: UUID,
    flag_id: UUID,
    payload: FlagRemarkCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DailyOpsFlagMutationResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.daily_ops.execute")
    set_tenant_context(connection, tenant_id)
    expected_version = _parse_version(if_match, subject="Daily Operations flag")
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        context = _run_context(
            connection, tenant_id=tenant_id, outlet_id=outlet_id, daily_ops_run_id=daily_ops_run_id, principal=principal,
        )
        row = _finding(connection, tenant_id=tenant_id, flag_id=flag_id, for_update=True)
        if int(row["version_no"]) != expected_version:
            raise ConflictError(
                error_code="VAC-CONFLICT-005", title="Daily Operations flag version conflict",
                detail="The flag changed since it was loaded. Refresh and retry the action.",
            )
        connection.execute(
            text(
                "UPDATE auditcore.audit_findings SET updated_at_utc=now(), version_no=version_no+1 "
                "WHERE tenant_id=:tenant_id AND audit_finding_id=:flag_id"
            ),
            {"tenant_id": tenant_id, "flag_id": flag_id},
        )
        event_id = _append_event(
            connection, tenant_id=tenant_id, daily_ops_run_id=daily_ops_run_id, flag_id=flag_id,
            event_type="REMARK", actor_id=principal.subject, actor_role=context["operating_role"],
            reason=payload.remarks.strip(), correlation_id=correlation_id, safe_payload={},
        )
        updated = _finding(connection, tenant_id=tenant_id, flag_id=flag_id)
        return {
            "flag": _flag_view(updated, role=context["operating_role"]).model_dump(mode="json"),
            "eventId": str(event_id),
        }

    body, replay = execute_idempotent_json_command(
        connection, tenant_id=tenant_id,
        operation_key=f"uc03.daily-ops-flag.remark:{flag_id}",
        idempotency_key=idempotency_key,
        request_payload={"expectedVersion": expected_version, "payload": payload.model_dump(mode="json")},
        execute=execute, logical_result_id=str(flag_id),
    )
    flag = DailyOpsFlagView.model_validate(body["flag"])
    response.headers["ETag"] = f'"{flag.version}"'
    return DailyOpsFlagMutationResponse(flag=flag, eventId=UUID(body["eventId"]), idempotent=replay)
