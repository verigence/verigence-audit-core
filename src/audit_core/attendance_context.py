from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
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

    This route is intentionally read-only and isolated from Booking/Delivery/Review
    paths. Dealer/Outlet coordinates remain Audit Core master data. PC receives only
    currently assigned active Outlets; non-PC operating roles return no geofence
    Outlets because Phase 1 captures their location without enforcing a work geofence.
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
                WHERE ba.tenant_id = :tenant_id
                  AND ba.security_actor_id = :actor_id
                  AND ba.assignment_status = 'ACTIVE'
                  AND ba.effective_from <= now()
                  AND (ba.effective_to IS NULL OR ba.effective_to >= now())
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
                  ON o.tenant_id = :tenant_id
                 AND o.dealer_id = a.dealer_id
                 AND o.outlet_id = a.outlet_id
                 AND o.status = 'ACTIVE'
                WHERE a.business_role_code = 'PC'
                  AND a.dealer_id IS NOT NULL
                  AND a.outlet_id IS NOT NULL
            )
            SELECT
                rs.operating_role,
                rs.operating_role_count,
                po.outlets
            FROM role_summary rs
            CROSS JOIN pc_outlets po
            """
        ),
        {"tenant_id": tenant_id, "actor_id": human_principal.subject},
    ).mappings().one()

    role_count = int(row["operating_role_count"])
    if role_count == 0:
        raise AuthorizationError(
            error_code="VAC-AUTH-004",
            status_code=403,
            title="Business scope denied",
        )
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
