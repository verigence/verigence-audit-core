from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal

router = APIRouter(tags=["attendance-context"])


class AttendanceOutletContext(BaseModel):
    dealerId: UUID
    outletId: UUID
    outletName: str
    latitude: float | None = None
    longitude: float | None = None


class AttendanceWorkContext(BaseModel):
    userId: UUID
    operatingRole: str
    geofenceRequired: bool
    outlets: list[AttendanceOutletContext]


@router.get(
    "/v1/tenants/{tenant_id}/attendance-context/me",
    response_model=AttendanceWorkContext,
)
def current_attendance_context(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> AttendanceWorkContext:
    """Return only the authenticated user's effective work-location context.

    This route is read-only and isolated from Booking/Delivery/Review paths. PC
    receives currently assigned active Outlet coordinates. Other operating roles
    return their role only because Phase 1 captures location without geofencing them.
    A user with no Audit Core operating assignment gets 404 so a secondary HRADMIN
    role can still use Attendance without being forced into a business assignment.
    """

    row = connection.execute(
        text(
            """
            WITH runtime_context AS MATERIALIZED (
                SELECT set_config('app.security_actor_id', :actor_id, true) AS actor_context
            ),
            active_assignments AS MATERIALIZED (
                SELECT
                    ba.business_role_code,
                    ba.dealer_id,
                    ba.outlet_id
                FROM runtime_context rc
                CROSS JOIN auditcore.business_assignments ba
                JOIN auditcore.projects p
                  ON p.tenant_id=ba.tenant_id
                 AND p.project_status='ACTIVE'
                WHERE ba.tenant_id=:tenant_id
                  AND ba.security_actor_id=:actor_id
                  AND ba.assignment_status='ACTIVE'
                  AND ba.effective_from<=now()
                  AND (ba.effective_to IS NULL OR ba.effective_to>=now())
            ),
            role_summary AS (
                SELECT
                    min(business_role_code) AS operating_role,
                    count(DISTINCT business_role_code) AS operating_role_count
                FROM active_assignments
            ),
            pc_outlets AS (
                SELECT COALESCE(
                    jsonb_agg(
                        jsonb_build_object(
                            'dealerId', a.dealer_id::text,
                            'outletId', a.outlet_id::text,
                            'outletName', o.outlet_name,
                            'latitude', o.latitude,
                            'longitude', o.longitude
                        )
                        ORDER BY lower(o.outlet_name), a.outlet_id
                    ),
                    '[]'::jsonb
                ) AS outlets
                FROM active_assignments a
                JOIN auditcore.dealer_outlets o
                  ON o.tenant_id=:tenant_id
                 AND o.dealer_id=a.dealer_id
                 AND o.outlet_id=a.outlet_id
                 AND o.status='ACTIVE'
                WHERE a.business_role_code='PC'
                  AND a.dealer_id IS NOT NULL
                  AND a.outlet_id IS NOT NULL
            )
            SELECT rs.operating_role, rs.operating_role_count, po.outlets
            FROM role_summary rs
            CROSS JOIN pc_outlets po
            """
        ),
        {"tenant_id": tenant_id, "actor_id": human_principal.subject},
    ).mappings().one()

    role_count = int(row["operating_role_count"])
    if role_count == 0:
        raise HTTPException(status_code=404, detail="No active operating assignment for this Project")
    if role_count != 1:
        raise RuntimeError("Attendance context has inconsistent operating roles")

    operating_role = str(row["operating_role"])
    outlets = [AttendanceOutletContext.model_validate(item) for item in row["outlets"]]
    return AttendanceWorkContext(
        userId=UUID(human_principal.subject),
        operatingRole=operating_role,
        geofenceRequired=operating_role == "PC",
        outlets=outlets if operating_role == "PC" else [],
    )
