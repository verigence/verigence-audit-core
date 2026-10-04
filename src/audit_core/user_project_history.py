"""Which projects a person is, or has been, tagged to (read-only, SuperAdmin only).

Role Mapping closes a person's old assignments and adds new ones; it never overwrites them, so
``business_assignments`` already holds the whole history with its dates. This endpoint reads it
across projects so Project Administration can flag someone who is already tagged elsewhere and
show where they worked before. It writes nothing and changes no existing code path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Engine, text

from audit_core.db import set_security_actor_context, set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_engine,
    require_super_admin_request,
)

router = APIRouter(prefix="/v1/admin", tags=["role-mapping"])

_RUNTIME_ROLE = "audit_core_runtime"
_STATEMENT_TIMEOUT = "5s"
_MAX_PROJECTS = 50


class UserProjectAssignment(BaseModel):
    tenantId: str
    projectCode: str | None
    projectName: str
    roleCode: str
    dealerName: str | None
    outletName: str | None
    since: datetime
    until: datetime | None
    current: bool


class UserProjectAssignments(BaseModel):
    userId: str
    assignments: list[UserProjectAssignment]


_TENANTS_SQL = text(
    "SELECT DISTINCT tenant_id FROM auditcore.business_assignments WHERE security_actor_id = :user_id"
)

_ASSIGNMENTS_SQL = text(
    """
    SELECT a.business_role_code, a.dealer_id, a.outlet_id, a.assignment_status,
           a.effective_from, a.effective_to,
           p.project_name, p.business_code, d.dealer_name, o.outlet_name
    FROM auditcore.business_assignments a
    JOIN auditcore.projects p ON p.tenant_id = a.tenant_id
    LEFT JOIN auditcore.dealers d ON d.tenant_id = a.tenant_id AND d.dealer_id = a.dealer_id
    LEFT JOIN auditcore.dealer_outlets o
           ON o.tenant_id = a.tenant_id AND o.dealer_id = a.dealer_id AND o.outlet_id = a.outlet_id
    WHERE a.tenant_id = :tenant_id AND a.security_actor_id = :user_id
    """
)


def collapse_assignments(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """One line per project, role and outlet: the first start, and either still current or the last end.

    Editing a mapping closes and re-adds the same rows, so the raw rows repeat; this folds them.
    """
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = (row["tenant_id"], row["business_role_code"], row["dealer_id"], row["outlet_id"])
        is_current = (
            row["assignment_status"] == "ACTIVE"
            and row["effective_from"] <= now
            and (row["effective_to"] is None or row["effective_to"] > now)
        )
        end = row["effective_to"]
        line = groups.get(key)
        if line is None:
            groups[key] = {
                "tenantId": str(row["tenant_id"]),
                "projectCode": row["business_code"],
                "projectName": row["project_name"],
                "roleCode": row["business_role_code"],
                "dealerName": row["dealer_name"],
                "outletName": row["outlet_name"],
                "since": row["effective_from"],
                "until": end,
                "current": is_current,
            }
            continue
        line["since"] = min(line["since"], row["effective_from"])
        line["current"] = line["current"] or is_current
        if end is not None and (line["until"] is None or end > line["until"]):
            line["until"] = end
    result = list(groups.values())
    for line in result:
        if line["current"]:
            line["until"] = None
    result.sort(key=lambda line: (not line["current"], -line["since"].timestamp()))
    return result


def load_user_projects(engine: Engine, user_id: str) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    with engine.begin() as connection:
        connection.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
        connection.execute(text(f"SET LOCAL ROLE {_RUNTIME_ROLE}"))
        set_security_actor_context(connection, user_id)  # sees only this person's own assignment rows
        tenants = sorted(str(t) for t in connection.execute(_TENANTS_SQL, {"user_id": user_id}).scalars())
    for tenant_id in tenants[:_MAX_PROJECTS]:
        with engine.begin() as connection:
            connection.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
            connection.execute(text(f"SET LOCAL ROLE {_RUNTIME_ROLE}"))
            set_tenant_context(connection, tenant_id)
            for row in connection.execute(_ASSIGNMENTS_SQL, {"tenant_id": tenant_id, "user_id": user_id}).mappings():
                rows.append({**dict(row), "tenant_id": tenant_id})
    return collapse_assignments(rows, now)


@router.get("/users/{user_id}/project-assignments", response_model=UserProjectAssignments)
def user_project_assignments(
    user_id: str,
    _: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> UserProjectAssignments:
    return UserProjectAssignments(
        userId=user_id,
        assignments=[UserProjectAssignment(**line) for line in load_user_projects(engine, user_id)],
    )
