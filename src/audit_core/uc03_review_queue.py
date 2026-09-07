"""uc03_review_queue.py — the cross-journey audit review queue.

A PC opens this to see the data / document gaps assigned to them; a TL or PM
opens it to see the violations awaiting an Accept / Reject decision, plus any
gap that has passed its SLA and escalated up to their level.

  GET /v1/tenants/{tenant_id}/uc03/review-queue           → the items
  GET /v1/tenants/{tenant_id}/uc03/review-queue/summary    → counts for the badge

Everything is scoped to the caller's active business assignments and evaluated
against the caller's held roles. SLA / escalation is computed on read from
``sla_due_at_utc`` — there is no background job.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import DependencyUnavailableError
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    SecurityAuthorizationError,
    get_security_authorization_client,
)
from audit_core.uc03_finding_routing import (
    class_profile,
    classify_finding,
    escalation_level,
    permitted_actions,
    resolve_sla_policy,
    sla_due_at,
    visible_to_role,
)

router = APIRouter(prefix="/v1/tenants/{tenant_id}/uc03", tags=["uc03-review-queue"])

_PERMISSION_KEY = "audit.finding.read"
_ROLE_LADDER = ("PC", "TL", "PM", "EXECUTIVE")
_SEVERITY_RANK = {"CRITICAL": 5, "HIGH": 4, "MEDIUM": 3, "LOW": 2, "INFO": 1}

QueueScope = Literal["ALL", "MINE", "ESCALATED"]


class QueueItem(BaseModel):
    flagId: UUID
    journeyId: UUID
    journeyReference: str | None
    stage: str
    findingClass: str
    resolutionMode: str
    category: str | None
    severity: str
    status: str
    title: str
    description: str | None
    ownerRoleCode: str
    disposition: str | None
    originKind: str | None
    ruleKey: str | None
    createdAtUtc: datetime
    slaDueAtUtc: datetime | None
    escalationLevel: int
    overdue: bool
    isMine: bool
    permittedActions: list[str] = Field(default_factory=list)
    # case context
    customerName: str | None
    dealerName: str | None
    outletName: str | None
    productLabel: str | None
    bookingReference: str | None


class ReviewQueueResponse(BaseModel):
    roles: list[str]
    generatedAtUtc: datetime
    items: list[QueueItem]


class ReviewQueueSummary(BaseModel):
    roles: list[str]
    total: int
    mine: int
    escalatedToMe: int
    overdue: int
    byClass: dict[str, int]
    byStage: dict[str, int]


def _authorize(
    client: SecurityAuthorizationClient,
    *,
    human_principal: HumanPrincipal,
    tenant_id: str,
) -> None:
    try:
        decision = client.check_user_permission(
            user_id=human_principal.subject,
            tenant_id=tenant_id,
            permission_key=_PERMISSION_KEY,
        )
    except SecurityAuthorizationError as exc:
        raise DependencyUnavailableError(
            detail="The review queue is temporarily unavailable. Please try again."
        ) from exc
    if not decision.allowed:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )


def _actor_roles(connection: Connection, *, tenant_id: str, actor_id: str) -> list[str]:
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT upper(business_role_code) AS role
            FROM auditcore.business_assignments
            WHERE tenant_id = :tenant_id
              AND security_actor_id = :actor_id
              AND assignment_status = 'ACTIVE'
              AND effective_from <= now()
              AND (effective_to IS NULL OR effective_to >= now())
            """
        ),
        {"tenant_id": tenant_id, "actor_id": actor_id},
    ).scalars().all()
    roles = [r if r != "EXEC" else "EXECUTIVE" for r in rows]
    return [r for r in _ROLE_LADDER if r in roles]


_QUEUE_SQL = """
    SELECT
        f.audit_finding_id, f.journey_id, f.stage_code, f.finding_type_code,
        f.severity, f.finding_status, f.title, f.description, f.rule_key,
        f.origin_kind, f.created_at_utc, f.finding_class, f.owner_role_code,
        f.sla_due_at_utc, f.disposition,
        j.journey_reference,
        c.display_name AS customer_name,
        d.dealer_name,
        o.outlet_name,
        b.booking_reference,
        NULLIF(concat_ws(' · ',
            NULLIF(jp.model_name_snapshot, ''),
            NULLIF(jp.variant_name_snapshot, ''),
            NULLIF(jp.colour_name_snapshot, '')), '') AS product_label
    FROM auditcore.audit_findings f
    JOIN auditcore.journeys j
      ON j.tenant_id = f.tenant_id AND j.journey_id = f.journey_id
    JOIN auditcore.customers c
      ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
    JOIN auditcore.dealers d
      ON d.tenant_id = j.tenant_id AND d.dealer_id = j.dealer_id
    JOIN auditcore.dealer_outlets o
      ON o.tenant_id = j.tenant_id AND o.dealer_id = j.dealer_id AND o.outlet_id = j.outlet_id
    LEFT JOIN auditcore.journey_products jp
      ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
    LEFT JOIN auditcore.bookings b
      ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
    WHERE f.tenant_id = :tenant_id
      AND f.finding_status IN ('OPEN','ACKNOWLEDGED')
      AND f.stage_code IN ('BOOKING','DELIVERY')
      AND EXISTS (
            SELECT 1 FROM auditcore.business_assignments ba
            WHERE ba.tenant_id = j.tenant_id
              AND ba.security_actor_id = :actor_id
              AND ba.assignment_status = 'ACTIVE'
              AND ba.effective_from <= now()
              AND (ba.effective_to IS NULL OR ba.effective_to >= now())
              AND (
                    ba.dealer_id IS NULL
                    OR (ba.dealer_id = j.dealer_id
                        AND (ba.outlet_id IS NULL OR ba.outlet_id = j.outlet_id))
              )
      )
"""


def _load_queue(
    connection: Connection,
    *,
    tenant_id: str,
    actor_id: str,
    roles: list[str],
    policy,
    now: datetime,
    finding_class: str | None,
    stage: str | None,
) -> list[tuple[QueueItem, bool]]:
    """Returns (item, escalated_to_me) for every finding visible to the caller."""
    rows = connection.execute(text(_QUEUE_SQL), {"tenant_id": tenant_id, "actor_id": actor_id}).mappings().all()
    top_role = roles[-1] if roles else ""
    out: list[tuple[QueueItem, bool]] = []

    for row in rows:
        cls = row["finding_class"] or classify_finding(row["rule_key"], row["finding_type_code"])
        if finding_class and cls != finding_class:
            continue
        if stage and row["stage_code"] != stage:
            continue

        profile = class_profile(cls)
        owner_role = row["owner_role_code"] or profile.owner_role
        due_at = row["sla_due_at_utc"] or sla_due_at(
            row["created_at_utc"], finding_class=cls, severity=row["severity"], policy=policy
        )
        level = escalation_level(due_at, now, policy)

        seeing_roles = [r for r in roles if visible_to_role(owner_role, level, r)]
        if not seeing_roles:
            continue
        is_mine = owner_role in roles
        escalated_to_me = (not is_mine) and bool(seeing_roles)

        item = QueueItem(
            flagId=row["audit_finding_id"],
            journeyId=row["journey_id"],
            journeyReference=row["journey_reference"],
            stage=row["stage_code"],
            findingClass=cls,
            resolutionMode=profile.resolution_mode,
            category=row["finding_type_code"],
            severity=row["severity"],
            status=row["finding_status"],
            title=row["title"],
            description=row["description"],
            ownerRoleCode=owner_role,
            disposition=row["disposition"],
            originKind=row["origin_kind"],
            ruleKey=row["rule_key"],
            createdAtUtc=row["created_at_utc"],
            slaDueAtUtc=due_at,
            escalationLevel=level,
            overdue=now > due_at if due_at is not None else False,
            isMine=is_mine,
            permittedActions=permitted_actions(
                finding_class=cls,
                role=seeing_roles[-1] if seeing_roles else top_role,
                finding_status=row["finding_status"],
            ),
            customerName=row["customer_name"],
            dealerName=row["dealer_name"],
            outletName=row["outlet_name"],
            productLabel=row["product_label"],
            bookingReference=row["booking_reference"],
        )
        out.append((item, escalated_to_me))
    return out


def _sort_key(item: QueueItem) -> tuple[Any, ...]:
    return (
        0 if item.overdue else 1,
        -item.escalationLevel,
        -_SEVERITY_RANK.get(item.severity, 0),
        item.slaDueAtUtc or datetime.max.replace(tzinfo=UTC),
    )


@router.get("/review-queue", response_model=ReviewQueueResponse)
def get_review_queue(
    tenant_id: str,
    scope: Annotated[QueueScope, Query()] = "ALL",
    findingClass: Annotated[str | None, Query()] = None,
    stage: Annotated[str | None, Query()] = None,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)] = None,
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ] = None,
    connection: Annotated[Connection, Depends(get_connection)] = None,
) -> ReviewQueueResponse:
    _authorize(authorization_client, human_principal=human_principal, tenant_id=tenant_id)
    set_tenant_context(connection, tenant_id)
    now = datetime.now(UTC)
    roles = _actor_roles(connection, tenant_id=tenant_id, actor_id=human_principal.subject)
    policy = resolve_sla_policy(_first_policy_settings(connection, tenant_id))

    loaded = _load_queue(
        connection,
        tenant_id=tenant_id,
        actor_id=human_principal.subject,
        roles=roles,
        policy=policy,
        now=now,
        finding_class=(findingClass or "").upper() or None,
        stage=(stage or "").upper() or None,
    )
    if scope == "MINE":
        loaded = [pair for pair in loaded if pair[0].isMine]
    elif scope == "ESCALATED":
        loaded = [pair for pair in loaded if pair[1]]

    items = sorted((pair[0] for pair in loaded), key=_sort_key)
    return ReviewQueueResponse(roles=roles, generatedAtUtc=now, items=items)


@router.get("/review-queue/summary", response_model=ReviewQueueSummary)
def get_review_queue_summary(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)] = None,
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ] = None,
    connection: Annotated[Connection, Depends(get_connection)] = None,
) -> ReviewQueueSummary:
    _authorize(authorization_client, human_principal=human_principal, tenant_id=tenant_id)
    set_tenant_context(connection, tenant_id)
    now = datetime.now(UTC)
    roles = _actor_roles(connection, tenant_id=tenant_id, actor_id=human_principal.subject)
    policy = resolve_sla_policy(_first_policy_settings(connection, tenant_id))

    loaded = _load_queue(
        connection,
        tenant_id=tenant_id,
        actor_id=human_principal.subject,
        roles=roles,
        policy=policy,
        now=now,
        finding_class=None,
        stage=None,
    )
    by_class: dict[str, int] = {}
    by_stage: dict[str, int] = {}
    for item, _ in loaded:
        by_class[item.findingClass] = by_class.get(item.findingClass, 0) + 1
        by_stage[item.stage] = by_stage.get(item.stage, 0) + 1
    return ReviewQueueSummary(
        roles=roles,
        total=len(loaded),
        mine=sum(1 for item, _ in loaded if item.isMine),
        escalatedToMe=sum(1 for _, escalated in loaded if escalated),
        overdue=sum(1 for item, _ in loaded if item.overdue),
        byClass=by_class,
        byStage=by_stage,
    )


def _first_policy_settings(connection: Connection, tenant_id: str) -> dict[str, Any]:
    """A tenant-wide SLA policy: use the newest published project policy settings.

    Per-journey overrides still apply when a finding is created (see _machine_flag);
    this is only the read-time fallback for the queue's derived SLA.
    """
    row = connection.execute(
        text(
            """
            SELECT policy_settings
            FROM auditcore.project_policy_versions
            WHERE tenant_id = :tenant_id
            ORDER BY (lifecycle_status = 'PUBLISHED') DESC, version_no DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id},
    ).scalar_one_or_none()
    return row if isinstance(row, dict) else {}
