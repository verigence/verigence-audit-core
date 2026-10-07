from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_capture import get_booking_workspace as _base_workspace

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}",
    tags=["uc03-booking-integration"],
)

_DI_AUDIENCE = "di"
def _proposal_payload(fact: Any) -> dict[str, Any]:
    """Preserve the machine value and add optional DI source localization."""
    payload: dict[str, Any] = {"value": fact.value}
    if fact.page_no is not None or fact.evidence_region is not None:
        payload["sourceLocalization"] = {
            "pageNo": fact.page_no,
            "evidenceRegion": fact.evidence_region,
        }
    return payload


def _enrich_workspace_localization(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    body: dict[str, Any],
) -> None:
    """Expose optional DI source localization without changing proposal persistence."""
    rows = connection.execute(
        text(
            """
            SELECT capture_proposal_id, proposed_value
            FROM auditcore.journey_capture_proposals
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    localization_by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        proposed = row["proposed_value"]
        if not isinstance(proposed, dict):
            continue
        localization = proposed.get("sourceLocalization")
        if isinstance(localization, dict):
            localization_by_id[str(row["capture_proposal_id"])] = localization

    proposals = body.get("proposals")
    if not isinstance(proposals, list):
        return
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        localization = localization_by_id.get(str(proposal.get("proposalId") or ""))
        if localization is None:
            proposal["pageNo"] = None
            proposal["evidenceRegion"] = None
            continue
        page_no = localization.get("pageNo")
        region = localization.get("evidenceRegion")
        proposal["pageNo"] = (
            page_no if isinstance(page_no, int) and not isinstance(page_no, bool) else None
        )
        proposal["evidenceRegion"] = region if isinstance(region, dict) else None




@router.get("/uc03-workspace")
def get_booking_workspace_with_typed_exchange(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    body = _base_workspace(
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        connection=connection,
    )
    details = connection.execute(
        text(
            """
            SELECT details
            FROM auditcore.trade_in_cases
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()
    if isinstance(details, dict) and "exchangeTaken" in details:
        body.setdefault("capture", {})["EXCHANGE_TAKEN"] = bool(details["exchangeTaken"])
    _enrich_workspace_localization(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        body=body,
    )
    return body
