"""Task Manager HTTP API (docs/TASK_MANAGER_DESIGN_v1.0.md section 11).

Project users: ``/p2/v1/tenants/{tenant_id}/...``. Security's
``audit.journey.read`` (every operating role holds it) admits the caller; the
Task Manager's own role matrix, from the caller's business assignment in the
Project, decides what they may do.

Superadmin: ``/v1/admin/tenants/{tenant_id}/task-manager/...``, Security
attesting super admin on every request; one Project at a time.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    get_human_principal,
    require_super_admin_request,
)
from audit_core.di_dashboard import build_di_dashboard
from audit_core.engagement_reports import build_engagement
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.task_manager import (
    SCHEDULE_OWNERS,
    SUPER_ADMIN,
    Caller,
    TaskManagerConflict,
    TaskManagerError,
    TaskManagerForbidden,
    TaskManagerNotFound,
    act,
    assignable_roles,
    caller_role,
    create_schedule,
    get_task,
    ist_today,
    known_names,
    list_schedules,
    list_tasks,
    project_people,
    raise_tasks,
    skip_occurrence,
    update_schedule,
)
from audit_core.task_manager_reports import (
    build_task_status,
    build_utilisation,
    record_activity,
    utilisation_allowed,
)
from audit_core.uc03_p2_access import check_p2_permission

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["task-manager"])
admin_router = APIRouter(prefix="/v1/admin/tenants/{tenant_id}/task-manager", tags=["task-manager-admin"])

_READ_PERMISSION = "audit.journey.read"
_STATUS_ROLES = frozenset({"TL", "PM", "EXECUTIVE"})

def _project_caller(
    connection: Connection, *, tenant_id: str, principal: HumanPrincipal, client: SecurityAuthorizationClient,
    require_role: bool = True,
) -> Caller:
    check_p2_permission(tenant_id=tenant_id, human_principal=principal, authorization_client=client,
                        permission_key=_READ_PERMISSION)
    set_tenant_context(connection, tenant_id)
    role = caller_role(connection, tenant_id=tenant_id, actor_id=principal.subject)
    if role is None and require_role:
        raise AuthorizationError(error_code="VAC-AUTH-002", status_code=403, title="Permission denied")
    names = known_names(connection, tenant_id=tenant_id)
    return Caller(actor_id=principal.subject, role=role, display_name=names.get(principal.subject))


def _admin_caller(connection: Connection, *, tenant_id: str, admin: HumanAdminRequest) -> Caller:
    if not admin.admin_context.is_super_admin:
        raise AuthorizationError(error_code="VAC-AUTH-002", status_code=403, title="Permission denied")
    set_tenant_context(connection, tenant_id)
    exists = connection.execute(
        text("SELECT 1 FROM auditcore.projects WHERE tenant_id = :t"), {"t": tenant_id},
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    names = known_names(connection, tenant_id=tenant_id)
    return Caller(actor_id=admin.user_id, role=SUPER_ADMIN, display_name=names.get(admin.user_id))


def _run[T](work: Callable[[], T]) -> T:
    try:
        return work()
    except TaskManagerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TaskManagerForbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except TaskManagerConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TaskManagerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# --------------------------------------------------------------------------- bodies


class RaiseCommand(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=4000)
    assigneeActorIds: list[str] = Field(min_length=1, max_length=200)
    expectedCloseMinutes: int = Field(ge=1, le=60 * 24 * 60)
    priority: str = "NORMAL"
    outletId: UUID | None = None


class ActionCommand(BaseModel):
    action: str
    comment: str | None = Field(default=None, max_length=4000)
    details: dict[str, Any] = Field(default_factory=dict)


class ScheduleCommand(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=4000)
    priority: str | None = None
    frequency: str | None = None
    weekdays: list[int] | None = None
    monthDay: int | None = None
    runTimeIst: str | None = None
    expectedCloseMinutes: int | None = None
    assigneeMode: str | None = None
    assignedRole: str | None = None
    assignedActorId: str | None = None
    outletId: UUID | None = None
    startsOn: date | None = None
    endsOn: date | None = None
    status: str | None = None

    def spec(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in self.model_dump(exclude_unset=True).items():
            out[key] = value.isoformat() if isinstance(value, date) else (str(value) if isinstance(value, UUID)
                                                                           else value)
        return out


class SkipCommand(BaseModel):
    date: date
    reason: str = Field(min_length=1, max_length=1000)


class ActivityCommand(BaseModel):
    date: str
    clientKind: str = "WEB"
    displayName: str | None = Field(default=None, max_length=200)
    activeSeconds: int = Field(ge=0, le=86400)
    idleSeconds: int = Field(ge=0, le=86400)
    backgroundSeconds: int = Field(ge=0, le=86400)
    hourlyActiveMinutes: list[int] = Field(default_factory=list, max_length=24)
    firstActiveAt: str | None = None
    lastActiveAt: str | None = None
    sessions: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


# --------------------------------------------------------------------------- shared handlers


def _context(connection: Connection, *, tenant_id: str, caller: Caller) -> dict[str, Any]:
    people = project_people(connection, tenant_id=tenant_id)
    roles = assignable_roles(caller)
    return {
        "role": caller.role,
        "actorId": caller.actor_id,
        "canAssignTo": sorted(roles),
        "canCreateSchedules": caller.role in SCHEDULE_OWNERS,
        "canSeeSchedules": caller.role in SCHEDULE_OWNERS or caller.role == "PM",
        "canSeeAll": caller.role in {"PM", "EXECUTIVE", SUPER_ADMIN},
        "canSeeUtilisation": utilisation_allowed(caller),
        "assignees": [p for p in people if p["roleCode"] in roles and p["actorId"] != caller.actor_id],
        "outlets": [dict(r) for r in connection.execute(
            text("SELECT outlet_id::text AS \"outletId\", outlet_name AS \"outletName\" "
                 "FROM auditcore.dealer_outlets WHERE tenant_id = :t ORDER BY outlet_name"),
            {"t": tenant_id},
        ).mappings().all()],
        "today": ist_today().isoformat(),
    }


def _utilisation(connection: Connection, *, tenant_id: str, caller: Caller, start: date | None,
                 end: date | None) -> dict[str, Any]:
    if not utilisation_allowed(caller):
        raise HTTPException(status_code=403, detail="Only an Executive or Superadmin can see utilisation.")
    end = end or ist_today()
    start = start or end
    return _run(lambda: build_utilisation(connection, tenant_id=tenant_id, start_day=start, end_day=end))


# --------------------------------------------------------------------------- project routes

Principal = Annotated[HumanPrincipal, Depends(get_human_principal)]
Client = Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)]
Db = Annotated[Connection, Depends(get_connection)]


@router.get("/task-manager/context")
def task_manager_context(tenant_id: str, principal: Principal, client: Client, connection: Db) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _context(connection, tenant_id=tenant_id, caller=caller)


@router.get("/task-manager/tasks")
def task_manager_list(
    tenant_id: str, principal: Principal, client: Client, connection: Db,
    view: Annotated[str, Query(pattern="^(mine|raised|review|all)$")] = "mine",
    status: Annotated[str, Query(pattern="^(open|done|all)$")] = "open",
) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _run(lambda: list_tasks(connection, tenant_id=tenant_id, caller=caller, view=view, status=status))


@router.post("/task-manager/tasks", status_code=201)
def task_manager_raise(tenant_id: str, command: RaiseCommand, principal: Principal, client: Client,
                       connection: Db) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    ids = _run(lambda: raise_tasks(
        connection, tenant_id=tenant_id, caller=caller, title=command.title, description=command.description,
        assignee_actor_ids=command.assigneeActorIds, expected_close_minutes=command.expectedCloseMinutes,
        priority=command.priority.upper(), outlet_id=command.outletId,
    ))
    return {"taskIds": ids}


@router.get("/task-manager/tasks/{task_id}")
def task_manager_get(tenant_id: str, task_id: UUID, principal: Principal, client: Client,
                     connection: Db) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _run(lambda: get_task(connection, tenant_id=tenant_id, caller=caller, task_id=task_id))


@router.post("/task-manager/tasks/{task_id}/actions")
def task_manager_act(tenant_id: str, task_id: UUID, command: ActionCommand, principal: Principal, client: Client,
                     connection: Db) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _run(lambda: act(connection, tenant_id=tenant_id, caller=caller, task_id=task_id,
                            action=command.action, comment=command.comment, details=command.details))


@router.get("/task-manager/schedules")
def task_manager_schedules(tenant_id: str, principal: Principal, client: Client, connection: Db) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return {"items": _run(lambda: list_schedules(connection, tenant_id=tenant_id, caller=caller))}


@router.post("/task-manager/schedules", status_code=201)
def task_manager_create_schedule(tenant_id: str, command: ScheduleCommand, principal: Principal, client: Client,
                                 connection: Db) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _run(lambda: create_schedule(connection, tenant_id=tenant_id, caller=caller, spec=command.spec()))


@router.patch("/task-manager/schedules/{schedule_id}")
def task_manager_update_schedule(tenant_id: str, schedule_id: UUID, command: ScheduleCommand, principal: Principal,
                                 client: Client, connection: Db) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _run(lambda: update_schedule(connection, tenant_id=tenant_id, caller=caller, schedule_id=schedule_id,
                                        spec=command.spec()))


@router.post("/task-manager/schedules/{schedule_id}/skip")
def task_manager_skip(tenant_id: str, schedule_id: UUID, command: SkipCommand, principal: Principal, client: Client,
                      connection: Db) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _run(lambda: skip_occurrence(connection, tenant_id=tenant_id, caller=caller, schedule_id=schedule_id,
                                        day=command.date, reason=command.reason))


@router.get("/reports/task-status")
def task_status_report(tenant_id: str, principal: Principal, client: Client, connection: Db,
                       day: date | None = None) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    if caller.role not in _STATUS_ROLES:
        raise HTTPException(status_code=403, detail="Daily task status is for Team Leads, PMs and Executives.")
    return build_task_status(connection, tenant_id=tenant_id, day=day or ist_today())


@router.get("/reports/employee-utilisation")
def employee_utilisation(tenant_id: str, principal: Principal, client: Client, connection: Db,
                         start: Annotated[date | None, Query(alias="from")] = None,
                         end: Annotated[date | None, Query(alias="to")] = None) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _utilisation(connection, tenant_id=tenant_id, caller=caller, start=start, end=end)


def _engagement(connection: Connection, *, tenant_id: str, caller: Caller, start: date | None, end: date | None,
                period: str) -> dict[str, Any]:
    if not utilisation_allowed(caller):
        raise HTTPException(status_code=403, detail="Only an Executive or Superadmin can see engagement.")
    end = end or ist_today()
    start = start or end
    return _run(lambda: build_engagement(connection, tenant_id=tenant_id, start_day=start, end_day=end, period=period))


def _di_dashboard(connection: Connection, *, tenant_id: str, caller: Caller, start: date | None,
                  end: date | None) -> dict[str, Any]:
    if not utilisation_allowed(caller):
        raise HTTPException(status_code=403, detail="Only an Executive or Superadmin can see the DI dashboard.")
    end = end or ist_today()
    start = start or end
    return _run(lambda: build_di_dashboard(connection, tenant_id=tenant_id, start_day=start, end_day=end))


Period = Annotated[str, Query(pattern="^(day|week|month)$")]
From = Annotated[date | None, Query(alias="from")]
To = Annotated[date | None, Query(alias="to")]


@router.get("/reports/engagement")
def engagement(tenant_id: str, principal: Principal, client: Client, connection: Db,
               start: From = None, end: To = None, period: Period = "day") -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _engagement(connection, tenant_id=tenant_id, caller=caller, start=start, end=end, period=period)


@router.get("/reports/di-dashboard")
def di_dashboard(tenant_id: str, principal: Principal, client: Client, connection: Db,
                 start: From = None, end: To = None) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _di_dashboard(connection, tenant_id=tenant_id, caller=caller, start=start, end=end)


@router.post("/activity", status_code=202)
def activity(tenant_id: str, command: ActivityCommand, principal: Principal, client: Client,
             connection: Db) -> dict[str, Any]:
    """The browser's tally of time on Verigence for today (one small upsert)."""
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client,
                             require_role=False)
    if caller.role is None:
        return {"recorded": False}
    recorded = _run(lambda: record_activity(connection, tenant_id=tenant_id, caller=caller,
                                            payload=command.model_dump()))
    return {"recorded": recorded}


# --------------------------------------------------------------------------- superadmin routes

Admin = Annotated[HumanAdminRequest, Depends(require_super_admin_request)]


@admin_router.get("/context")
def admin_context(tenant_id: str, admin: Admin, connection: Db) -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return _context(connection, tenant_id=tenant_id, caller=caller)


@admin_router.get("/tasks")
def admin_list(tenant_id: str, admin: Admin, connection: Db,
               view: Annotated[str, Query(pattern="^(mine|raised|review|all)$")] = "all",
               status: Annotated[str, Query(pattern="^(open|done|all)$")] = "open") -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return _run(lambda: list_tasks(connection, tenant_id=tenant_id, caller=caller, view=view, status=status))


@admin_router.post("/tasks", status_code=201)
def admin_raise(tenant_id: str, command: RaiseCommand, admin: Admin, connection: Db) -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    ids = _run(lambda: raise_tasks(
        connection, tenant_id=tenant_id, caller=caller, title=command.title, description=command.description,
        assignee_actor_ids=command.assigneeActorIds, expected_close_minutes=command.expectedCloseMinutes,
        priority=command.priority.upper(), outlet_id=command.outletId,
    ))
    return {"taskIds": ids}


@admin_router.get("/tasks/{task_id}")
def admin_get(tenant_id: str, task_id: UUID, admin: Admin, connection: Db) -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return _run(lambda: get_task(connection, tenant_id=tenant_id, caller=caller, task_id=task_id))


@admin_router.post("/tasks/{task_id}/actions")
def admin_act(tenant_id: str, task_id: UUID, command: ActionCommand, admin: Admin, connection: Db) -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return _run(lambda: act(connection, tenant_id=tenant_id, caller=caller, task_id=task_id,
                            action=command.action, comment=command.comment, details=command.details))


@admin_router.get("/schedules")
def admin_schedules(tenant_id: str, admin: Admin, connection: Db) -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return {"items": _run(lambda: list_schedules(connection, tenant_id=tenant_id, caller=caller))}


@admin_router.post("/schedules", status_code=201)
def admin_create_schedule(tenant_id: str, command: ScheduleCommand, admin: Admin, connection: Db) -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return _run(lambda: create_schedule(connection, tenant_id=tenant_id, caller=caller, spec=command.spec()))


@admin_router.patch("/schedules/{schedule_id}")
def admin_update_schedule(tenant_id: str, schedule_id: UUID, command: ScheduleCommand, admin: Admin,
                          connection: Db) -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return _run(lambda: update_schedule(connection, tenant_id=tenant_id, caller=caller, schedule_id=schedule_id,
                                        spec=command.spec()))


@admin_router.post("/schedules/{schedule_id}/skip")
def admin_skip(tenant_id: str, schedule_id: UUID, command: SkipCommand, admin: Admin, connection: Db) -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return _run(lambda: skip_occurrence(connection, tenant_id=tenant_id, caller=caller, schedule_id=schedule_id,
                                        day=command.date, reason=command.reason))


@admin_router.get("/task-status")
def admin_task_status(tenant_id: str, admin: Admin, connection: Db, day: date | None = None) -> dict[str, Any]:
    _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return build_task_status(connection, tenant_id=tenant_id, day=day or ist_today())


@admin_router.get("/employee-utilisation")
def admin_utilisation(tenant_id: str, admin: Admin, connection: Db,
                      start: Annotated[date | None, Query(alias="from")] = None,
                      end: Annotated[date | None, Query(alias="to")] = None) -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return _utilisation(connection, tenant_id=tenant_id, caller=caller, start=start, end=end)


@admin_router.get("/engagement")
def admin_engagement(tenant_id: str, admin: Admin, connection: Db, start: From = None, end: To = None,
                     period: Period = "day") -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return _engagement(connection, tenant_id=tenant_id, caller=caller, start=start, end=end, period=period)


@admin_router.get("/di-dashboard")
def admin_di_dashboard(tenant_id: str, admin: Admin, connection: Db, start: From = None,
                       end: To = None) -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return _di_dashboard(connection, tenant_id=tenant_id, caller=caller, start=start, end=end)


__all__ = ["admin_router", "router"]
