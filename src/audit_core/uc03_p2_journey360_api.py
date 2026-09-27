"""Phase 2 Journey 360 HTTP surface.

GET /journeys/{id}/360            summary (header, stage, money, counts)
GET /journeys/{id}/360/{section}  one lazily-loaded tab

Both carry the Journey's ETag; a matching If-None-Match is answered 304
without building the body, so re-opening an unchanged Journey is one cheap
indexed read."""
from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import Connection

from audit_core import uc03_p2_journey360 as model
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import authorize_p2

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-journey-360"])

_READ_PERMISSION = "audit.journey.read"


def _serve(request: Request, response: Response, etag: str, build) -> Any:
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, no-cache"
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, no-cache"})
    return build()


@router.get("/journeys/{journey_id}/360")
def journey_360_summary(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    response: Response,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> Any:
    authorize_p2(
        connection, tenant_id=tenant_id, journey_id=journey_id, human_principal=human_principal,
        authorization_client=authorization_client, permission_key=_READ_PERMISSION,
    )
    etag = model.journey_etag(connection, tenant_id=tenant_id, journey_id=journey_id)
    return _serve(request, response, etag,
                  lambda: model.summary(connection, tenant_id=tenant_id, journey_id=journey_id))


@router.get("/journeys/{journey_id}/360/{section}")
def journey_360_section(
    tenant_id: str,
    journey_id: UUID,
    section: str,
    request: Request,
    response: Response,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> Any:
    builder = model.BUILDERS.get(section)
    if builder is None:
        raise HTTPException(status_code=404, detail=f"Unknown Journey 360 section {section}.")
    authorize_p2(
        connection, tenant_id=tenant_id, journey_id=journey_id, human_principal=human_principal,
        authorization_client=authorization_client, permission_key=_READ_PERMISSION,
    )
    etag = model.journey_etag(connection, tenant_id=tenant_id, journey_id=journey_id)
    return _serve(request, response, f'{etag[:-1]}-{section}"',
                  lambda: builder(connection, tenant_id=tenant_id, journey_id=journey_id))
