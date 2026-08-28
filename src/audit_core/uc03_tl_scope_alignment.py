from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core import uc03_tl_supervisory as tl_supervisory
from audit_core.authorization import AuthorizationError
from audit_core.errors import NotFoundError


# TL is an approved project-wide operating role in role_mapping_policy. A project-wide
# assignment is represented by dealer_id=NULL and outlet_id=NULL. The supervisory
# query must therefore scope by Tenant + active TL assignment, not require a Dealer ID.
TL_SCOPE_WHERE_SQL = """
    WHERE j.tenant_id=:tenant_id
      AND EXISTS (
            SELECT 1
            FROM auditcore.business_assignments tl
            WHERE tl.tenant_id=j.tenant_id
              AND tl.security_actor_id=:actor_id
              AND tl.business_role_code='TL'
              AND tl.assignment_status='ACTIVE'
              AND tl.effective_from <= now()
              AND (tl.effective_to IS NULL OR tl.effective_to >= now())
              AND tl.dealer_id IS NULL
              AND tl.outlet_id IS NULL
      )
      AND (
            bs.capture_completed_at_utc IS NOT NULL
            OR delivery.delivery_id IS NOT NULL
            OR ds.business_status IS NOT NULL
      )
"""


def require_project_wide_tl_case_scope(
    connection: Connection,
    *,
    tenant_id: str,
    actor_id: str,
    journey_id: UUID | None = None,
) -> dict[str, Any] | None:
    """Require the existing project-wide TL mapping and keep PC drafts hidden."""

    if journey_id is None:
        assigned = connection.execute(
            text(
                """
                SELECT 1
                FROM auditcore.business_assignments
                WHERE tenant_id=:tenant_id
                  AND security_actor_id=:actor_id
                  AND business_role_code='TL'
                  AND assignment_status='ACTIVE'
                  AND effective_from <= now()
                  AND (effective_to IS NULL OR effective_to >= now())
                  AND dealer_id IS NULL
                  AND outlet_id IS NULL
                LIMIT 1
                """
            ),
            {"tenant_id": tenant_id, "actor_id": actor_id},
        ).scalar_one_or_none()
        if assigned is None:
            raise AuthorizationError(
                error_code="VAC-AUTH-002",
                status_code=403,
                title="Permission denied",
            )
        return None

    row = connection.execute(
        text(
            """
            SELECT j.journey_id, j.customer_id, j.dealer_id, j.outlet_id,
                   bs.capture_completed_at_utc,
                   COALESCE(bs.business_status, b.actual_status_code) AS booking_status,
                   COALESCE(ds.business_status, d.actual_delivery_status_code) AS delivery_status
            FROM auditcore.journeys j
            LEFT JOIN auditcore.bookings b
              ON b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id
            LEFT JOIN auditcore.deliveries d
              ON d.tenant_id=j.tenant_id AND d.journey_id=j.journey_id
            LEFT JOIN auditcore.journey_stage_states bs
              ON bs.tenant_id=j.tenant_id
             AND bs.journey_id=j.journey_id
             AND bs.stage_code='BOOKING'
            LEFT JOIN auditcore.journey_stage_states ds
              ON ds.tenant_id=j.tenant_id
             AND ds.journey_id=j.journey_id
             AND ds.stage_code='DELIVERY'
            WHERE j.tenant_id=:tenant_id
              AND j.journey_id=:journey_id
              AND EXISTS (
                    SELECT 1
                    FROM auditcore.business_assignments tl
                    WHERE tl.tenant_id=j.tenant_id
                      AND tl.security_actor_id=:actor_id
                      AND tl.business_role_code='TL'
                      AND tl.assignment_status='ACTIVE'
                      AND tl.effective_from <= now()
                      AND (tl.effective_to IS NULL OR tl.effective_to >= now())
                      AND tl.dealer_id IS NULL
                      AND tl.outlet_id IS NULL
              )
              AND (
                    bs.capture_completed_at_utc IS NOT NULL
                    OR d.delivery_id IS NOT NULL
                    OR ds.business_status IS NOT NULL
              )
            """
        ),
        {"tenant_id": tenant_id, "actor_id": actor_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Submitted case not found",
            detail="The submitted Booking/Delivery case is not available in this Team Lead scope.",
        )
    return dict(row)


def install_tl_scope_alignment() -> None:
    """Align the TL router with the already-approved project-wide mapping model."""

    tl_supervisory._require_tl_case_scope = require_project_wide_tl_case_scope
    tl_supervisory._SCOPE_WHERE_SQL = TL_SCOPE_WHERE_SQL
