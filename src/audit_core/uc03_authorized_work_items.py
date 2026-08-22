from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection

from audit_core.authorization import AuthorizationError
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import DependencyUnavailableError
from audit_core.security import HumanPrincipal, Principal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    SecurityAuthorizationError,
    get_security_authorization_client,
)
from audit_core.uc03_work_items import WorkItemPage, WorkType, list_work_items

router = APIRouter(prefix="/v1/tenants/{tenant_id}/uc03", tags=["uc03-work-items"])
_PERMISSION_KEY = "audit.journey.read"


def _authorize_workspace(
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
            detail="Project work is temporarily unavailable. Please try again."
        ) from exc
    if not decision.allowed:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )


@router.get("/work-items", response_model=WorkItemPage)
def list_authorized_work_items(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    work_type: Annotated[WorkType, Query(alias="workType")] = "ALL",
    from_date: Annotated[date | None, Query(alias="fromDate")] = None,
    to_date: Annotated[date | None, Query(alias="toDate")] = None,
    limit: Annotated[int, Query(ge=1, le=10)] = 10,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> WorkItemPage:
    """Return C0 work items after live Security v2 authorization.

    The human JWT is identity-only. Security remains the live functional authorization
    source of truth, while Audit Core continues to enforce Project Dealer/Outlet scope
    through business_assignments inside the delegated query.
    """

    _authorize_workspace(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    delegated_principal = Principal(
        subject=human_principal.subject,
        tenant_id=tenant_id,
        permissions=(_PERMISSION_KEY,),
    )
    return list_work_items(
        tenant_id=tenant_id,
        principal=delegated_principal,
        connection=connection,
        work_type=work_type,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        cursor=cursor,
    )
