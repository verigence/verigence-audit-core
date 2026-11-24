"""Daily Duties HTTP API (docs/DAILY_DUTIES_DESIGN_v1.0.md).

Project users: ``/p2/v1/tenants/{tenant_id}/duties/...``. Security's
``audit.journey.read`` (every operating role holds it) admits the caller; the
caller's operating role in the Project decides what they may do: a PC works their
own list, a Team Lead / PM / Executive sees the team, an Executive keeps the duty
list and the switch.

SuperAdmin: ``/v1/admin/tenants/{tenant_id}/duties/...``, Security attesting super
admin on every request; one Project at a time.
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
from audit_core.daily_duties import (
    DutyConflict,
    DutyError,
    DutyForbidden,
    DutyNotFound,
    add_standard_duties,
    close_day,
    create_duty,
    duties_enabled,
    get_settings,
    list_duties,
    my_day,
    not_received,
    team_view,
    tick,
    undo,
    update_duty,
    update_settings,
)
from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    get_human_principal,
    require_super_admin_request,
)
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.task_manager import SUPER_ADMIN, Caller, caller_role, known_names
from audit_core.uc03_p2_access import check_p2_permission

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}/duties", tags=["daily-duties"])
admin_router = APIRouter(prefix="/v1/admin/tenants/{tenant_id}/duties", tags=["daily-duties-admin"])

_READ_PERMISSION = "audit.journey.read"


def _project_caller(connection: Connection, *, tenant_id: str, principal: HumanPrincipal,
                    client: SecurityAuthorizationClient, named: bool = True) -> Caller:
    check_p2_permission(tenant_id=tenant_id, human_principal=principal, authorization_client=client,
                        permission_key=_READ_PERMISSION)
    set_tenant_context(connection, tenant_id)
    role = caller_role(connection, tenant_id=tenant_id, actor_id=principal.subject)
    if role is None:
        raise AuthorizationError(error_code="VAC-AUTH-002", status_code=403, title="Permission denied")
    if not named:
        return Caller(actor_id=principal.subject, role=role)
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
    except DutyNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DutyForbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DutyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DutyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# --------------------------------------------------------------------------- bodies


class TickCommand(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    comment: str | None = Field(default=None, max_length=1000)


class NotReceivedCommand(BaseModel):
    comment: str | None = Field(default=None, max_length=500)
    # "Have you informed the relevant WhatsApp group?" -- the PC must answer.
    informedGroup: bool | None = None


class ReasonEntry(BaseModel):
    itemId: UUID
    category: str = Field(min_length=1, max_length=30)
    remark: str | None = Field(default=None, max_length=500)
    handledByActorId: str | None = Field(default=None, max_length=200)


class CloseCommand(BaseModel):
    reasons: list[ReasonEntry] = Field(default_factory=list, max_length=200)


class SettingsCommand(BaseModel):
    enabled: bool | None = None
    cutoffTimeIst: str | None = Field(default=None, max_length=5)
    defaultCarryDays: int | None = Field(default=None, ge=0, le=30)
    workingWeekdays: list[int] | None = None


class DutyCommand(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    mandatory: bool | None = None
    weekdays: list[int] | None = None
    outletScope: str | None = None
    outletIds: list[UUID] | None = None
    dayOffset: int | None = None
    closeMode: str | None = None
    checkerCode: str | None = None
    inputs: list[dict[str, Any]] | None = Field(default=None, max_length=6)
    allowNotReceived: bool | None = None
    rolling: bool | None = None
    carryDays: int | None = Field(default=None, ge=0, le=30)
    sortOrder: int | None = Field(default=None, ge=0, le=100000)
    status: str | None = None
    effectiveFrom: date | None = None

    def spec(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in self.model_dump(exclude_unset=True).items():
            if key == "outletIds" and value is not None:
                out[key] = [str(v) for v in value]
            else:
                out[key] = value
        return out


def _reasons(command: CloseCommand) -> list[dict[str, Any]]:
    return [{"itemId": str(r.itemId), "category": r.category, "remark": r.remark,
             "handledByActorId": r.handledByActorId} for r in command.reasons]


# --------------------------------------------------------------------------- project routes

Principal = Annotated[HumanPrincipal, Depends(get_human_principal)]
Client = Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)]
Db = Annotated[Connection, Depends(get_connection)]
From = Annotated[date | None, Query(alias="from")]
To = Annotated[date | None, Query(alias="to")]


@router.get("/my-day")
def duties_my_day(tenant_id: str, principal: Principal, client: Client, connection: Db) -> dict[str, Any]:
    """The signed-in PC's list for today. ``enabled: false`` when the Project has duties off."""
    # Attendance and PC Home ask this in every Project the person works in: a Project with
    # duties off, or a person who is not a PC, is answered without looking up any names.
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client, named=False)
    if caller.role == "PC" and duties_enabled(connection, tenant_id):
        caller = Caller(actor_id=caller.actor_id, role=caller.role,
                        display_name=known_names(connection, tenant_id=tenant_id).get(caller.actor_id))
    return _run(lambda: my_day(connection, tenant_id=tenant_id, caller=caller))


@router.post("/items/{item_id}/tick")
def duties_tick(tenant_id: str, item_id: UUID, command: TickCommand, principal: Principal, client: Client,
                connection: Db) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _run(lambda: tick(connection, tenant_id=tenant_id, caller=caller, item_id=item_id,
                             values=command.values, comment=command.comment))


@router.post("/items/{item_id}/undo")
def duties_undo(tenant_id: str, item_id: UUID, principal: Principal, client: Client,
                connection: Db) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _run(lambda: undo(connection, tenant_id=tenant_id, caller=caller, item_id=item_id))


@router.post("/items/{item_id}/not-received")
def duties_not_received(tenant_id: str, item_id: UUID, command: NotReceivedCommand, principal: Principal,
                        client: Client, connection: Db) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _run(lambda: not_received(connection, tenant_id=tenant_id, caller=caller, item_id=item_id,
                                     comment=command.comment, informed_group=command.informedGroup))


@router.post("/my-day/close")
def duties_close_day(tenant_id: str, command: CloseCommand, principal: Principal, client: Client,
                     connection: Db) -> dict[str, Any]:
    """Day close, at check-out: a reason for every mandatory duty not done."""
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _run(lambda: close_day(connection, tenant_id=tenant_id, caller=caller, reasons=_reasons(command)))


@router.get("/team")
def duties_team(tenant_id: str, principal: Principal, client: Client, connection: Db, start: From = None,
                end: To = None) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _run(lambda: team_view(connection, tenant_id=tenant_id, caller=caller, start=start, end=end))


@router.get("/evidence")
def duties_evidence(tenant_id: str, principal: Principal, client: Client, connection: Db, start: From = None,
                    end: To = None) -> dict[str, Any]:
    """Each PC, day by day: gate register, cash register, bank statements and photos
    (product owner 2026-11-24)."""
    from audit_core.daily_duty_evidence import evidence_view

    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _run(lambda: evidence_view(connection, tenant_id=tenant_id, caller=caller, start=start, end=end))


@router.get("/settings")
def duties_settings(tenant_id: str, principal: Principal, client: Client, connection: Db) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return get_settings(connection, tenant_id=tenant_id, caller=caller)


@router.put("/settings")
def duties_update_settings(tenant_id: str, command: SettingsCommand, principal: Principal, client: Client,
                           connection: Db) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _run(lambda: update_settings(connection, tenant_id=tenant_id, caller=caller,
                                        spec=command.model_dump(exclude_unset=True)))


@router.get("/definitions")
def duties_definitions(tenant_id: str, principal: Principal, client: Client, connection: Db) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _run(lambda: list_duties(connection, tenant_id=tenant_id, caller=caller))


@router.post("/definitions", status_code=201)
def duties_create(tenant_id: str, command: DutyCommand, principal: Principal, client: Client,
                  connection: Db) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _run(lambda: create_duty(connection, tenant_id=tenant_id, caller=caller, spec=command.spec()))


@router.post("/definitions/standard")
def duties_add_standard(tenant_id: str, principal: Principal, client: Client, connection: Db) -> dict[str, Any]:
    """Add the duties the product owner named that are not on the list yet."""
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _run(lambda: add_standard_duties(connection, tenant_id=tenant_id, caller=caller))


@router.patch("/definitions/{duty_id}")
def duties_update(tenant_id: str, duty_id: UUID, command: DutyCommand, principal: Principal, client: Client,
                  connection: Db) -> dict[str, Any]:
    caller = _project_caller(connection, tenant_id=tenant_id, principal=principal, client=client)
    return _run(lambda: update_duty(connection, tenant_id=tenant_id, caller=caller, duty_id=duty_id,
                                    spec=command.spec()))


# --------------------------------------------------------------------------- superadmin routes

Admin = Annotated[HumanAdminRequest, Depends(require_super_admin_request)]


@admin_router.get("/team")
def admin_team(tenant_id: str, admin: Admin, connection: Db, start: From = None, end: To = None) -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return _run(lambda: team_view(connection, tenant_id=tenant_id, caller=caller, start=start, end=end))


@admin_router.get("/evidence")
def admin_evidence(tenant_id: str, admin: Admin, connection: Db, start: From = None, end: To = None) -> dict[str, Any]:
    from audit_core.daily_duty_evidence import evidence_view

    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return _run(lambda: evidence_view(connection, tenant_id=tenant_id, caller=caller, start=start, end=end))


@admin_router.get("/settings")
def admin_settings(tenant_id: str, admin: Admin, connection: Db) -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return get_settings(connection, tenant_id=tenant_id, caller=caller)


@admin_router.put("/settings")
def admin_update_settings(tenant_id: str, command: SettingsCommand, admin: Admin, connection: Db) -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return _run(lambda: update_settings(connection, tenant_id=tenant_id, caller=caller,
                                        spec=command.model_dump(exclude_unset=True)))


@admin_router.get("/definitions")
def admin_definitions(tenant_id: str, admin: Admin, connection: Db) -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return _run(lambda: list_duties(connection, tenant_id=tenant_id, caller=caller))


@admin_router.post("/definitions", status_code=201)
def admin_create(tenant_id: str, command: DutyCommand, admin: Admin, connection: Db) -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return _run(lambda: create_duty(connection, tenant_id=tenant_id, caller=caller, spec=command.spec()))


@admin_router.post("/definitions/standard")
def admin_add_standard(tenant_id: str, admin: Admin, connection: Db) -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return _run(lambda: add_standard_duties(connection, tenant_id=tenant_id, caller=caller))


@admin_router.patch("/definitions/{duty_id}")
def admin_update(tenant_id: str, duty_id: UUID, command: DutyCommand, admin: Admin, connection: Db) -> dict[str, Any]:
    caller = _admin_caller(connection, tenant_id=tenant_id, admin=admin)
    return _run(lambda: update_duty(connection, tenant_id=tenant_id, caller=caller, duty_id=duty_id,
                                    spec=command.spec()))


__all__ = ["admin_router", "router"]
