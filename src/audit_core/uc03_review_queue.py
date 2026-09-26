"""uc03_review_queue.py — the unified cross-journey Task Queue.

A PC opens this to see the data / document gaps and Tasks assigned to them
(manual verification, an auto-spawned self-serve gap, a TL Take-Action
assignment); a TL or PM opens it to see the violations awaiting an
Accept / Reject / Take Action / Escalate verdict, plus any gap or task
that has passed its SLA and escalated up to their level.

  GET /v1/tenants/{tenant_id}/uc03/review-queue           → the items
  GET /v1/tenants/{tenant_id}/uc03/review-queue/summary    → counts for the badge

Everything is scoped to the caller's active business assignments and evaluated
against the caller's held roles. SLA / escalation is computed on read from
``sla_due_at_utc`` — there is no background job.

One queue, three axes -- not three pages:

- **Subject** (``subjectKind``): a Finding or Task belongs to a journey
  (Booking/Delivery) or a Daily Operations run (see migration 0080 for
  findings, 0099 for tasks). The frontend renders this as a "Journey
  Audits" / "Daily Operations" tab toggle.
- **Item kind** (``itemKind``): FINDING (something a role must adjudicate
  or self-serve-resolve) vs EXECUTION_TASK (something a role -- almost
  always PC -- must go *do*: upload a document, answer a gap, respond to
  a TL's Take Action). This is the Unified Work Items spine's own
  discriminator (auditcore.work_items.item_kind); a Finding and the Task
  it auto-spawned deliberately BOTH appear here, exactly as a bug ticket
  and its linked sub-task both show up in an issue tracker. Task rows are
  opt-in (``includeTasks=true``), defaulting False: the frontend deployed
  alongside this backend still renders every item as a Finding (permitted-
  action buttons, rule-key labels) and doesn't know ``itemKind`` yet --
  defaulting to False keeps this endpoint's live behavior unchanged until
  a frontend that understands both kinds asks for them.
- **Open vs closed** (``includeClosed``): the queue defaults to open work
  (a Finding OPEN/ACKNOWLEDGED, a Task not yet COMPLETED/FAILED/CANCELLED/
  DEAD_LETTER); passing ``includeClosed=true`` surfaces resolved/completed
  history too, so a role can see what they've already cleared.

Findings and Tasks are read from their own tables (``audit_findings`` /
``workflow_tasks``), not the ``work_items`` mirror -- each source table
carries native status/columns (finding_status's ACKNOWLEDGED, a Task's
CLAIMED/RETRY_WAIT) that ``permitted_actions()`` and this endpoint's own
business logic already key off directly; going through the spine's own
collapsed 4-state vocabulary would mean re-deriving that logic against a
different vocabulary for no benefit, since the spine's real job (giving
Findings and Tasks one identity for the auto-cancel trigger, etc.) doesn't
require the *read* path to go through it too. Four queries in total (two
subjects × two item kinds), merged into one sorted, filtered list in
Python -- extending the two-queries-merged-in-Python shape this module
already used for the two subjects, for the same reason: genuinely
different join shapes per source table, not worth forcing into one SQL
statement.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
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
# A Task's native task_status collapsed to "still needs doing" vs "done with".
_OPEN_TASK_STATUSES = {"PENDING", "READY", "CLAIMED", "IN_PROGRESS", "RETRY_WAIT"}

QueueScope = Literal["ALL", "MINE", "ESCALATED"]
QueueSubjectKind = Literal["JOURNEY", "DAILY_OPS"]
QueueItemKind = Literal["FINDING", "EXECUTION_TASK"]


class QueueItem(BaseModel):
    flagId: UUID
    itemKind: QueueItemKind = "FINDING"
    subjectKind: QueueSubjectKind = "JOURNEY"
    # JOURNEY subject only.
    journeyId: UUID | None = None
    journeyReference: str | None = None
    stage: str | None = None
    # DAILY_OPS subject only.
    dailyOpsRunId: UUID | None = None
    outletId: UUID | None = None
    businessDate: date | None = None
    # FINDING only -- None for an EXECUTION_TASK row.
    findingClass: str | None = None
    resolutionMode: str | None = None
    # EXECUTION_TASK only -- the Finding this Task was spawned to act on,
    # None for a Finding row (or a Task with no such link).
    relatedFindingId: UUID | None = None
    # FINDING: finding_type_code. EXECUTION_TASK: task_type (e.g.
    # AUTO_SELF_SERVE, TL_TAKE_ACTION, MANUAL_VERIFICATION).
    category: str | None
    severity: str
    status: str
    isOpen: bool
    version: int
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
    # case context -- customer/product/booking are JOURNEY-only; dealerName/
    # outletName are populated for both subjects.
    customerName: str | None = None
    dealerName: str | None
    outletName: str | None
    productLabel: str | None = None
    bookingReference: str | None = None


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
    byKind: dict[str, int]
    # Manual Verification is a rule (ruleKey starting "MANUAL_VERIFICATION:"),
    # not its own findingClass -- every one of these is also counted under
    # byClass.DATA_GAP. Computed here, not just client-side, so the counter
    # tile is accurate regardless of which class filter is currently active
    # (the frontend's own items list is narrowed by class filter server-side;
    # this summary query never is).
    manualVerification: int


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
        f.severity, f.finding_status, f.version_no, f.title, f.description,
        f.rule_key, f.origin_kind, f.created_at_utc, f.finding_class,
        f.owner_role_code, f.sla_due_at_utc, f.disposition,
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
      AND f.subject_kind = 'JOURNEY'
      AND f.stage_code IN ('BOOKING','DELIVERY')
      AND (:include_closed OR f.finding_status IN ('OPEN','ACKNOWLEDGED'))
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

_DAILY_OPS_QUEUE_SQL = """
    SELECT
        f.audit_finding_id, f.daily_ops_run_id, f.finding_type_code,
        f.severity, f.finding_status, f.version_no, f.title, f.description,
        f.rule_key, f.origin_kind, f.created_at_utc, f.finding_class,
        f.owner_role_code, f.sla_due_at_utc, f.disposition,
        dor.outlet_id, dor.business_date,
        d.dealer_name,
        o.outlet_name
    FROM auditcore.audit_findings f
    JOIN auditcore.daily_ops_runs dor
      ON dor.tenant_id = f.tenant_id AND dor.daily_ops_run_id = f.daily_ops_run_id
    JOIN auditcore.dealer_outlets o
      ON o.tenant_id = dor.tenant_id AND o.outlet_id = dor.outlet_id
    JOIN auditcore.dealers d
      ON d.tenant_id = o.tenant_id AND d.dealer_id = o.dealer_id
    WHERE f.tenant_id = :tenant_id
      AND f.subject_kind = 'DAILY_OPS'
      AND (:include_closed OR f.finding_status IN ('OPEN','ACKNOWLEDGED'))
      AND EXISTS (
            SELECT 1 FROM auditcore.business_assignments ba
            WHERE ba.tenant_id = o.tenant_id
              AND ba.security_actor_id = :actor_id
              AND ba.assignment_status = 'ACTIVE'
              AND ba.effective_from <= now()
              AND (ba.effective_to IS NULL OR ba.effective_to >= now())
              AND (
                    ba.dealer_id IS NULL
                    OR (ba.dealer_id = o.dealer_id
                        AND (ba.outlet_id IS NULL OR ba.outlet_id = o.outlet_id))
              )
      )
"""

_TASK_QUEUE_SQL = """
    SELECT
        t.workflow_task_id, t.journey_id, t.process_area, t.task_type,
        t.task_status, t.severity, t.assigned_role_code, t.assigned_actor_id,
        t.priority, t.due_at_utc, t.created_at_utc, t.version_no,
        t.related_finding_id, t.task_payload,
        j.journey_reference,
        c.display_name AS customer_name,
        d.dealer_name,
        o.outlet_name,
        b.booking_reference,
        NULLIF(concat_ws(' · ',
            NULLIF(jp.model_name_snapshot, ''),
            NULLIF(jp.variant_name_snapshot, ''),
            NULLIF(jp.colour_name_snapshot, '')), '') AS product_label
    FROM auditcore.workflow_tasks t
    JOIN auditcore.journeys j
      ON j.tenant_id = t.tenant_id AND j.journey_id = t.journey_id
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
    WHERE t.tenant_id = :tenant_id
      AND t.journey_id IS NOT NULL
      -- AUTO_SELF_SERVE is always spawned 1:1 from the DATA_GAP/DOCUMENT_GAP
      -- finding it was raised for (related_finding_id), and _load_queue
      -- already includes that finding, correctly classified, with the same
      -- "go fix it" CTA the task itself carries. Concatenating both here
      -- doubled every self-serve gap into two cards -- the real finding
      -- (tagged Missing Document / Missing Data) plus a second, generically-
      -- labeled "Self-serve gap" card for the exact same thing. Excluded
      -- entirely: the finding row is the one and only place this belongs.
      AND t.task_type <> 'AUTO_SELF_SERVE'
      AND (:include_closed OR t.task_status IN ('PENDING','READY','CLAIMED','IN_PROGRESS','RETRY_WAIT'))
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

_DAILY_OPS_TASK_QUEUE_SQL = """
    SELECT
        t.workflow_task_id, t.daily_ops_run_id, t.process_area, t.task_type,
        t.task_status, t.severity, t.assigned_role_code, t.assigned_actor_id,
        t.priority, t.due_at_utc, t.created_at_utc, t.version_no,
        t.related_finding_id, t.task_payload,
        dor.outlet_id, dor.business_date,
        d.dealer_name,
        o.outlet_name
    FROM auditcore.workflow_tasks t
    JOIN auditcore.daily_ops_runs dor
      ON dor.tenant_id = t.tenant_id AND dor.daily_ops_run_id = t.daily_ops_run_id
    JOIN auditcore.dealer_outlets o
      ON o.tenant_id = dor.tenant_id AND o.outlet_id = dor.outlet_id
    JOIN auditcore.dealers d
      ON d.tenant_id = o.tenant_id AND d.dealer_id = o.dealer_id
    WHERE t.tenant_id = :tenant_id
      AND t.daily_ops_run_id IS NOT NULL
      -- Same reasoning as _TASK_QUEUE_SQL above: AUTO_SELF_SERVE duplicates
      -- its own already-shown finding.
      AND t.task_type <> 'AUTO_SELF_SERVE'
      AND (:include_closed OR t.task_status IN ('PENDING','READY','CLAIMED','IN_PROGRESS','RETRY_WAIT'))
      AND EXISTS (
            SELECT 1 FROM auditcore.business_assignments ba
            WHERE ba.tenant_id = o.tenant_id
              AND ba.security_actor_id = :actor_id
              AND ba.assignment_status = 'ACTIVE'
              AND ba.effective_from <= now()
              AND (ba.effective_to IS NULL OR ba.effective_to >= now())
              AND (
                    ba.dealer_id IS NULL
                    OR (ba.dealer_id = o.dealer_id
                        AND (ba.outlet_id IS NULL OR ba.outlet_id = o.outlet_id))
              )
      )
"""


def _load_daily_ops_queue(
    connection: Connection,
    *,
    tenant_id: str,
    actor_id: str,
    roles: list[str],
    policy,
    now: datetime,
    finding_class: str | None,
    include_closed: bool = False,
) -> list[tuple[QueueItem, bool]]:
    """Same shape as _load_queue, for Daily Ops run flags instead of journeys."""
    rows = connection.execute(
        text(_DAILY_OPS_QUEUE_SQL),
        {"tenant_id": tenant_id, "actor_id": actor_id, "include_closed": include_closed},
    ).mappings().all()
    top_role = roles[-1] if roles else ""
    out: list[tuple[QueueItem, bool]] = []

    for row in rows:
        cls = row["finding_class"] or classify_finding(row["rule_key"], row["finding_type_code"])
        if finding_class and cls != finding_class:
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
        is_open = row["finding_status"] in {"OPEN", "ACKNOWLEDGED"}

        item = QueueItem(
            flagId=row["audit_finding_id"],
            itemKind="FINDING",
            subjectKind="DAILY_OPS",
            dailyOpsRunId=row["daily_ops_run_id"],
            outletId=row["outlet_id"],
            businessDate=row["business_date"],
            findingClass=cls,
            resolutionMode=profile.resolution_mode,
            category=row["finding_type_code"],
            severity=row["severity"],
            status=row["finding_status"],
            isOpen=is_open,
            version=int(row["version_no"]),
            title=row["title"],
            description=row["description"],
            ownerRoleCode=owner_role,
            disposition=row["disposition"],
            originKind=row["origin_kind"],
            ruleKey=row["rule_key"],
            createdAtUtc=row["created_at_utc"],
            slaDueAtUtc=due_at,
            escalationLevel=level,
            overdue=now > due_at if due_at is not None and is_open else False,
            isMine=is_mine,
            permittedActions=permitted_actions(
                finding_class=cls,
                role=seeing_roles[-1] if seeing_roles else top_role,
                finding_status=row["finding_status"],
            ),
            dealerName=row["dealer_name"],
            outletName=row["outlet_name"],
        )
        out.append((item, escalated_to_me))
    return out


def _load_daily_ops_task_queue(
    connection: Connection,
    *,
    tenant_id: str,
    actor_id: str,
    roles: list[str],
    now: datetime,
    finding_class: str | None = None,
    include_closed: bool = False,
) -> list[tuple[QueueItem, bool]]:
    rows = connection.execute(
        text(_DAILY_OPS_TASK_QUEUE_SQL),
        {"tenant_id": tenant_id, "actor_id": actor_id, "include_closed": include_closed},
    ).mappings().all()
    return _tasks_to_items(
        rows, roles=roles, now=now, subject_kind="DAILY_OPS", finding_class=finding_class
    )


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
    include_closed: bool = False,
) -> list[tuple[QueueItem, bool]]:
    """Returns (item, escalated_to_me) for every finding visible to the caller."""
    rows = connection.execute(
        text(_QUEUE_SQL),
        {"tenant_id": tenant_id, "actor_id": actor_id, "include_closed": include_closed},
    ).mappings().all()
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
        is_open = row["finding_status"] in {"OPEN", "ACKNOWLEDGED"}

        item = QueueItem(
            flagId=row["audit_finding_id"],
            itemKind="FINDING",
            journeyId=row["journey_id"],
            journeyReference=row["journey_reference"],
            stage=row["stage_code"],
            findingClass=cls,
            resolutionMode=profile.resolution_mode,
            category=row["finding_type_code"],
            severity=row["severity"],
            status=row["finding_status"],
            isOpen=is_open,
            version=int(row["version_no"]),
            title=row["title"],
            description=row["description"],
            ownerRoleCode=owner_role,
            disposition=row["disposition"],
            originKind=row["origin_kind"],
            ruleKey=row["rule_key"],
            createdAtUtc=row["created_at_utc"],
            slaDueAtUtc=due_at,
            escalationLevel=level,
            overdue=now > due_at if due_at is not None and is_open else False,
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


def _load_task_queue(
    connection: Connection,
    *,
    tenant_id: str,
    actor_id: str,
    roles: list[str],
    now: datetime,
    stage: str | None,
    finding_class: str | None = None,
    include_closed: bool = False,
) -> list[tuple[QueueItem, bool]]:
    rows = connection.execute(
        text(_TASK_QUEUE_SQL),
        {"tenant_id": tenant_id, "actor_id": actor_id, "include_closed": include_closed},
    ).mappings().all()
    if stage:
        rows = [r for r in rows if r["process_area"] == stage]
    return _tasks_to_items(
        rows, roles=roles, now=now, subject_kind="JOURNEY", finding_class=finding_class
    )


def _task_specific_suffix(payload: dict[str, Any], *, derived_class: str | None) -> str | None:
    """A short, specific label to append to a Task's generic title -- the
    document name for a DOCUMENT_GAP-class task, the field name (falling
    back to the document name if no field is present) for a DATA_GAP-class
    one. Tries every payload key shape the current producers actually use
    (they don't share one convention); returns None when a producer's
    payload genuinely carries nothing specific to show, in which case the
    title falls back to today's plain category label, unchanged."""

    def fmt(raw: Any) -> str | None:
        text_value = str(raw).strip() if raw else ""
        return text_value.replace("_", " ").title() if text_value else None

    doc_label = (
        fmt(payload.get("documentLabel"))
        or fmt(payload.get("originalFilename"))
        or fmt(payload.get("documentTypeKey"))
        or fmt(payload.get("provider_name"))
    )
    field_keys = payload.get("fieldKeys")
    if isinstance(field_keys, list) and field_keys:
        field_label = fmt(field_keys[0])
        if field_label and len(field_keys) > 1:
            field_label = f"{field_label} +{len(field_keys) - 1} more"
    else:
        field_label = fmt(payload.get("fieldKey"))

    if derived_class == "DATA_GAP" and field_label:
        return f"{doc_label}: {field_label}" if doc_label else field_label
    return doc_label


def _tasks_to_items(
    rows: list[Any],
    *,
    roles: list[str],
    now: datetime,
    subject_kind: QueueSubjectKind,
    finding_class: str | None = None,
) -> list[tuple[QueueItem, bool]]:
    """Shared Task→QueueItem mapping for both subjects.

    A Task has no SLA-driven escalation model (that's a Finding concept,
    computed from sla_due_at_utc / a project SLA policy) -- keeping it
    simple per the v1.1 decision to not build machinery a real workflow
    doesn't need yet: escalationLevel is always 0, visibility is just
    "assigned role, the specific assignee, or anyone ranked above the
    assigned role" (so a TL/PM can always see PC's open tasks), and
    overdue is a plain due_at_utc comparison with no step function.
    permittedActions is deliberately empty -- the Task-completion action
    (PC upload + comment) doesn't exist yet; this queue only lists Tasks
    today, it doesn't yet let anyone act on one from here.

    findingClass is derived from task_type via _TASK_TYPE_FINDING_CLASS,
    not read off any column -- a Task genuinely has none. Bug fix: before
    this, every Task left findingClass null, so it was invisible to every
    Documents/Data/Violations class filter (findingClass=X only ever
    matched Finding rows) while _load_task_queue applied no class filter
    of its own -- meaning every open Task showed up under every class tab
    regardless of which was clicked, and none of them counted toward any
    tab's own badge, which only ever summed Finding rows. Filtering here
    (skip a Task whose derived class doesn't match the requested one) is
    what actually fixes the segregation; get_review_queue_summary's byClass
    count is then correct for free, since it already sums item.findingClass
    across everything _load_all returns.
    """
    out: list[tuple[QueueItem, bool]] = []
    for row in rows:
        derived_class = _TASK_TYPE_FINDING_CLASS.get(row["task_type"])
        if finding_class and derived_class != finding_class:
            continue
        assigned_role = (row["assigned_role_code"] or "PC").upper()
        # A Task has no SLA-driven escalation to gate on (visible_to_role's
        # own job) -- oversight here is simply "the assigned role, or
        # anyone ranked at or above it can see it", always, not only once
        # escalated.
        assigned_rank = _ROLE_LADDER.index(assigned_role) if assigned_role in _ROLE_LADDER else 0
        seeing_roles = [r for r in roles if _ROLE_LADDER.index(r) >= assigned_rank]
        if not seeing_roles:
            continue
        is_mine = assigned_role in roles
        is_open = row["task_status"] in _OPEN_TASK_STATUSES
        due_at = row["due_at_utc"]
        payload = row["task_payload"] or {}
        payload = payload if isinstance(payload, dict) else {}
        rule_key = payload.get("ruleKey")
        rule_key_stem = rule_key.split(":")[0] if rule_key else None
        base_title = (
            _TASK_TITLE_BY_RULE_KEY_STEM.get(rule_key_stem)
            or _TASK_TITLE.get(row["task_type"], row["task_type"].replace("_", " ").title())
        )
        specific = _task_specific_suffix(payload, derived_class=derived_class)

        item = QueueItem(
            flagId=row["workflow_task_id"],
            itemKind="EXECUTION_TASK",
            subjectKind=subject_kind,
            journeyId=row["journey_id"] if subject_kind == "JOURNEY" else None,
            journeyReference=row.get("journey_reference") if subject_kind == "JOURNEY" else None,
            stage=row["process_area"] if subject_kind == "JOURNEY" else None,
            dailyOpsRunId=row["daily_ops_run_id"] if subject_kind == "DAILY_OPS" else None,
            outletId=row.get("outlet_id"),
            businessDate=row.get("business_date"),
            relatedFindingId=row["related_finding_id"],
            category=row["task_type"],
            findingClass=derived_class,
            severity=row["severity"] or "MEDIUM",
            status=row["task_status"],
            isOpen=is_open,
            version=int(row["version_no"]),
            title=f"{base_title} — {specific}" if specific else base_title,
            description=payload.get("comment"),
            ownerRoleCode=assigned_role,
            disposition=None,
            originKind="SYSTEM",
            ruleKey=rule_key,
            createdAtUtc=row["created_at_utc"],
            slaDueAtUtc=due_at,
            escalationLevel=0,
            overdue=now > due_at if due_at is not None and is_open else False,
            isMine=is_mine,
            permittedActions=[],
            customerName=row.get("customer_name"),
            dealerName=row["dealer_name"],
            outletName=row["outlet_name"],
            productLabel=row.get("product_label"),
            bookingReference=row.get("booking_reference"),
        )
        out.append((item, False))
    return out


_TASK_TITLE = {
    "AUTO_SELF_SERVE": "Resolve data / document gap",
    "TL_TAKE_ACTION": "Take Action requested",
    "PC_VERIFY_UNRECOGNIZED_DOCUMENT": "Verify unrecognized document",
    "PC_RESOLVE_DOCUMENT_PROCESSING_FAILURE": "Re-upload a document that failed processing",
    "FIELD_CORRECTION_REVIEW": "Review proposed field correction",
    "MANUAL_VERIFICATION_REVIEW": "Verify low-confidence fields",
    "DUPLICATE_RECEIPT_NOTICE": "Duplicate receipt -- won't be counted",
    "WRONG_DOCUMENT_REVIEW": "Verify customer name mismatch",
    "WRONG_DOCUMENT_DEALER_NOTICE": "Receipt dealer name mismatch",
    "DELIVERY_VEHICLE_PHOTOS_MISSING": "Provide Delivery vehicle proof",
    "DELIVERY_VIN_MANUAL_ENTRY_REVIEW": "Approve manually-entered VIN/Chassis",
}

# AUTO_SELF_SERVE's generic title tells a PC nothing about what to actually
# do -- override it per rule_key stem wherever a specific, actionable title
# exists, so the Task Queue reads as an instruction, not a category label.
_TASK_TITLE_BY_RULE_KEY_STEM = {
    "MODEL_NOT_IDENTIFIED": "Select the vehicle SKU",
}

# A Task has no finding_class column of its own (that's a Finding concept),
# but every task_type below is unambiguously "about a document" or "about a
# data value" -- bug fix: without this, every Task row left item.findingClass
# null, so it was invisible to the Documents/Data/Violations class filters
# (findingClass=X only ever matched Finding rows) while still counted in the
# unfiltered "All" total, AND -- since _load_task_queue applied no class
# filter of its own either -- every open Task showed up under every one of
# those tabs regardless of which was clicked. AUTO_SELF_SERVE isn't listed:
# it's excluded from this query entirely (see _TASK_QUEUE_SQL), already
# classified correctly via the DATA_GAP/DOCUMENT_GAP finding it was spawned
# from.
#
# Confirmed live the first pass at this list was incomplete (grep across the
# whole codebase for every task_type actually used, not just the ones
# touched most recently) -- DUPLICATE_DOCUMENT_NOTICE and PC_DOCUMENT_
# REUPLOAD were both real, already-firing task types this dict never
# mentioned, so the exact same "All" vs. tab-sum mismatch this dict was
# built to fix was still showing up for them. Every task_type found by that
# audit is accounted for below, either mapped or explicitly left out with a
# reason -- not just the ones a screenshot happened to catch this time.
#
# Deliberately left unmapped: task types that are a call-to-action ("go
# review/call/claim") rather than a document-or-data gap, where forcing one
# of the two labels would misrepresent what the item actually is --
# TL_TAKE_ACTION, TL_REVIEW, TL_DELIVERY_REVIEW, CRM_CALL, ESCALATION_
# FOLLOW_UP, PC_CORRECTION (a TL/PM send-back with too free-form a reason to
# classify from task_type alone). PC_DELIVERY_CAPTURE is created already-
# completed (see submit_delivery_capture_v2) so it never appears as an open
# item to classify. BOOKING_RULE_EVALUATION is a machine-worker task (the
# one real caller of the claim/lease lifecycle, see workflow_stale_task_
# recovery.py's own docstring) never assigned to a human role.
_TASK_TYPE_FINDING_CLASS: dict[str, str] = {
    "PC_VERIFY_UNRECOGNIZED_DOCUMENT": "DOCUMENT_GAP",
    "PC_RESOLVE_DOCUMENT_PROCESSING_FAILURE": "DOCUMENT_GAP",
    "DUPLICATE_RECEIPT_NOTICE": "DOCUMENT_GAP",
    "DUPLICATE_DOCUMENT_NOTICE": "DOCUMENT_GAP",
    "PC_DOCUMENT_REUPLOAD": "DOCUMENT_GAP",
    "WRONG_DOCUMENT_REVIEW": "DOCUMENT_GAP",
    "WRONG_DOCUMENT_DEALER_NOTICE": "DOCUMENT_GAP",
    "FIELD_CORRECTION_REVIEW": "DATA_GAP",
    "MANUAL_VERIFICATION_REVIEW": "DATA_GAP",
    "FINANCE_DISBURSEMENT_REVIEW": "DATA_GAP",
    "FINANCE_DISBURSEMENT_CONFIRMED_NOTICE": "DATA_GAP",
    "PAYMENT_RECONCILIATION": "DATA_GAP",
    "SKU_RESOLUTION": "DATA_GAP",
    "SKU_RESOLUTION_INVOICE": "DATA_GAP",
    "DEAL_TOTAL_VARIANCE_REVIEW": "DATA_GAP",
    "INVOICE_DISCREPANCY_REVIEW": "DATA_GAP",
    "MODEL_SELECTION_CORRECTION_REVIEW": "DATA_GAP",
    "INVOICE_SKU_MISMATCH_REASON": "DATA_GAP",
    "DELIVERY_VEHICLE_PHOTOS_MISSING": "DOCUMENT_GAP",
    "DELIVERY_VIN_MANUAL_ENTRY_REVIEW": "DATA_GAP",
}


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
    subjectKind: Annotated[QueueSubjectKind, Query()] = "JOURNEY",
    findingClass: Annotated[str | None, Query()] = None,
    stage: Annotated[str | None, Query()] = None,
    itemKind: Annotated[QueueItemKind | None, Query()] = None,
    includeClosed: Annotated[bool, Query()] = False,
    includeTasks: Annotated[bool, Query()] = False,
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

    loaded = _load_all(
        connection,
        tenant_id=tenant_id,
        actor_id=human_principal.subject,
        roles=roles,
        policy=policy,
        now=now,
        subject_kind=subjectKind,
        finding_class=(findingClass or "").upper() or None,
        stage=(stage or "").upper() or None,
        include_closed=includeClosed,
        include_tasks=includeTasks,
    )
    if itemKind:
        loaded = [pair for pair in loaded if pair[0].itemKind == itemKind]
    if scope == "MINE":
        loaded = [pair for pair in loaded if pair[0].isMine]
    elif scope == "ESCALATED":
        loaded = [pair for pair in loaded if pair[1]]

    items = sorted((pair[0] for pair in loaded), key=_sort_key)
    return ReviewQueueResponse(roles=roles, generatedAtUtc=now, items=items)


def _load_all(
    connection: Connection,
    *,
    tenant_id: str,
    actor_id: str,
    roles: list[str],
    policy,
    now: datetime,
    subject_kind: QueueSubjectKind,
    finding_class: str | None,
    stage: str | None,
    include_closed: bool = False,
    include_tasks: bool = False,
) -> list[tuple[QueueItem, bool]]:
    # include_tasks defaults False: the currently-deployed frontend renders
    # this endpoint's items as Findings only (permittedActions buttons,
    # ruleKey-based labels, ...) and doesn't yet know about itemKind --
    # returning Task rows by default would silently change what today's
    # live Review Queue page shows before it has any way to render them.
    # A frontend that knows about itemKind opts in explicitly.
    if subject_kind == "DAILY_OPS":
        loaded = _load_daily_ops_queue(
            connection, tenant_id=tenant_id, actor_id=actor_id, roles=roles, policy=policy,
            now=now, finding_class=finding_class, include_closed=include_closed,
        )
        if include_tasks:
            loaded = loaded + _load_daily_ops_task_queue(
                connection, tenant_id=tenant_id, actor_id=actor_id, roles=roles, now=now,
                finding_class=finding_class, include_closed=include_closed,
            )
        return loaded
    loaded = _load_queue(
        connection, tenant_id=tenant_id, actor_id=actor_id, roles=roles, policy=policy,
        now=now, finding_class=finding_class, stage=stage, include_closed=include_closed,
    )
    if include_tasks:
        loaded = loaded + _load_task_queue(
            connection, tenant_id=tenant_id, actor_id=actor_id, roles=roles, now=now,
            stage=stage, finding_class=finding_class, include_closed=include_closed,
        )
    return loaded


@router.get("/review-queue/summary", response_model=ReviewQueueSummary)
def get_review_queue_summary(
    tenant_id: str,
    subjectKind: Annotated[QueueSubjectKind, Query()] = "JOURNEY",
    includeTasks: Annotated[bool, Query()] = False,
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

    loaded = _load_all(
        connection, tenant_id=tenant_id, actor_id=human_principal.subject, roles=roles,
        policy=policy, now=now, subject_kind=subjectKind, finding_class=None, stage=None,
        include_closed=False, include_tasks=includeTasks,
    )
    by_class: dict[str, int] = {}
    by_stage: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    manual_verification = 0
    for item, _ in loaded:
        if item.findingClass:
            by_class[item.findingClass] = by_class.get(item.findingClass, 0) + 1
        if item.stage:
            by_stage[item.stage] = by_stage.get(item.stage, 0) + 1
        by_kind[item.itemKind] = by_kind.get(item.itemKind, 0) + 1
        if (item.ruleKey or "").startswith("MANUAL_VERIFICATION:"):
            manual_verification += 1
    return ReviewQueueSummary(
        roles=roles,
        total=len(loaded),
        mine=sum(1 for item, _ in loaded if item.isMine),
        escalatedToMe=sum(1 for _, escalated in loaded if escalated),
        manualVerification=manual_verification,
        overdue=sum(1 for item, _ in loaded if item.overdue),
        byClass=by_class,
        byStage=by_stage,
        byKind=by_kind,
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
