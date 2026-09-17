"""uc03_duplicate_bookings_report.py — a dedicated, read-only cross-journey
view of every DUPLICATE_BOOKING pairing: which two journeys, at which
dealer/outlet, for which vehicle, and which one actually holds the booking.

  GET /v1/tenants/{tenant_id}/uc03/duplicate-bookings

The finding itself (raised on the journey that does NOT hold the booking --
see uc03_duplicate_booking_detection.py) already surfaces in the ordinary
Task Queue / Journey 360 Flags as a plain VIOLATION with a one-line
description. That's enough to know *that* something needs a look, but not
enough to actually compare the two bookings side by side without opening
both journeys separately -- exactly the gap reported. This endpoint answers
"show me both sides at once": customer, dealer, outlet, vehicle and the
booking-confirm date for each, plus the match basis and a plain-language
confidence estimate (not a model score -- there's no training data for
this -- a deterministic per-basis estimate of how rarely that signal
collides by chance; see _CONFIDENCE_PERCENT_BY_BASIS).

Open to PC, TL, PM and Executive alike -- unlike Audit Review's own
Accept/Reject adjudication (TL/PM/Executive only), this is pure information:
a PC needs to see exactly this to make sense of why their own booking got
flagged (their customer already has one at a different outlet). Scoped by
the caller's ordinary business_assignments, same as the review queue's own
visibility -- a PC naturally only sees pairs touching a journey they're
assigned to, which is exactly the "their own booking got flagged" case;
TL/PM/Executive typically hold broader assignments and see more pairs.
Deliberately read-only: adjudicating the underlying finding (Accept/Reject/
Take Action) stays on the Task Queue -- this endpoint never duplicates that
logic, only points at it.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
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
from audit_core.uc03_duplicate_booking_detection import _BASIS_LABEL

router = APIRouter(prefix="/v1/tenants/{tenant_id}/uc03", tags=["uc03-duplicate-bookings"])

_PERMISSION_KEY = "audit.finding.read"
_FINDING_TYPE = "DUPLICATE_BOOKING"


def _authorize(
    client: SecurityAuthorizationClient, *, human_principal: HumanPrincipal, tenant_id: str
) -> None:
    try:
        decision = client.check_user_permission(
            user_id=human_principal.subject, tenant_id=tenant_id, permission_key=_PERMISSION_KEY,
        )
    except SecurityAuthorizationError as exc:
        raise DependencyUnavailableError(
            detail="Duplicate booking details are temporarily unavailable. Please try again."
        ) from exc
    if not decision.allowed:
        raise AuthorizationError(error_code="VAC-AUTH-002", status_code=403, title="Permission denied")


class DuplicateBookingSide(BaseModel):
    journeyId: UUID | None = None
    journeyReference: str | None = None
    customerName: str | None = None
    dealerName: str | None = None
    outletName: str | None = None
    productLabel: str | None = None
    bookingReference: str | None = None
    bookingConfirmDate: date | None = None


class DuplicateBookingPair(BaseModel):
    findingId: UUID
    status: str
    severity: str
    raisedAtUtc: datetime
    matchBasis: str | None = None
    matchBasisLabel: str | None = None
    matchConfidencePercent: int | None = None
    matchConfidenceLabel: str | None = None
    # The journey the finding lives on -- believed NOT to hold the booking.
    duplicate: DuplicateBookingSide
    # The journey believed to actually hold the booking (whichever paid its
    # minimum booking amount earlier -- see uc03_duplicate_booking_detection).
    holder: DuplicateBookingSide


class DuplicateBookingsResponse(BaseModel):
    roles: list[str]
    generatedAtUtc: datetime
    pairs: list[DuplicateBookingPair]


_JOURNEY_SIDE_JOIN = """
    LEFT JOIN auditcore.journeys {alias}
      ON {alias}.tenant_id = f.tenant_id AND {alias}.journey_id = {journey_id_expr}
    LEFT JOIN auditcore.customers {alias}_c
      ON {alias}_c.tenant_id = {alias}.tenant_id AND {alias}_c.customer_id = {alias}.customer_id
    LEFT JOIN auditcore.dealers {alias}_d
      ON {alias}_d.tenant_id = {alias}.tenant_id AND {alias}_d.dealer_id = {alias}.dealer_id
    LEFT JOIN auditcore.dealer_outlets {alias}_o
      ON {alias}_o.tenant_id = {alias}.tenant_id AND {alias}_o.dealer_id = {alias}.dealer_id
     AND {alias}_o.outlet_id = {alias}.outlet_id
    LEFT JOIN auditcore.bookings {alias}_b
      ON {alias}_b.tenant_id = {alias}.tenant_id AND {alias}_b.journey_id = {alias}.journey_id
    LEFT JOIN auditcore.journey_products {alias}_jp
      ON {alias}_jp.tenant_id = {alias}.tenant_id AND {alias}_jp.journey_id = {alias}.journey_id
    LEFT JOIN auditcore.journey_stage_states {alias}_ss
      ON {alias}_ss.tenant_id = {alias}.tenant_id AND {alias}_ss.journey_id = {alias}.journey_id
     AND {alias}_ss.stage_code = 'BOOKING'
"""

_DUPLICATE_BOOKINGS_SQL = f"""
    WITH raised_payload AS (
        SELECT DISTINCT ON (audit_finding_id) audit_finding_id, safe_payload
        FROM auditcore.audit_finding_events
        WHERE tenant_id = :tenant_id AND event_type = 'RAISED'
        ORDER BY audit_finding_id, occurred_at_utc ASC
    )
    SELECT
        f.audit_finding_id, f.severity, f.finding_status, f.created_at_utc AS raised_at_utc,
        rp.safe_payload ->> 'matchBasis' AS match_basis,
        NULLIF(rp.safe_payload ->> 'matchConfidencePercent', '')::int AS match_confidence_percent,
        rp.safe_payload ->> 'matchConfidenceLabel' AS match_confidence_label,

        dj.journey_id AS duplicate_journey_id,
        dj.journey_reference AS duplicate_journey_reference,
        dj_c.display_name AS duplicate_customer_name,
        dj_d.dealer_name AS duplicate_dealer_name,
        dj_o.outlet_name AS duplicate_outlet_name,
        dj_b.booking_reference AS duplicate_booking_reference,
        dj_ss.booking_confirm_date AS duplicate_booking_confirm_date,
        NULLIF(concat_ws(' · ', NULLIF(dj_jp.model_name_snapshot, ''),
            NULLIF(dj_jp.variant_name_snapshot, ''), NULLIF(dj_jp.colour_name_snapshot, '')), '') AS duplicate_product_label,

        hj.journey_id AS holder_journey_id,
        hj.journey_reference AS holder_journey_reference,
        hj_c.display_name AS holder_customer_name,
        hj_d.dealer_name AS holder_dealer_name,
        hj_o.outlet_name AS holder_outlet_name,
        hj_b.booking_reference AS holder_booking_reference,
        hj_ss.booking_confirm_date AS holder_booking_confirm_date,
        NULLIF(concat_ws(' · ', NULLIF(hj_jp.model_name_snapshot, ''),
            NULLIF(hj_jp.variant_name_snapshot, ''), NULLIF(hj_jp.colour_name_snapshot, '')), '') AS holder_product_label

    FROM auditcore.audit_findings f
    JOIN raised_payload rp ON rp.audit_finding_id = f.audit_finding_id
    {_JOURNEY_SIDE_JOIN.format(alias="dj", journey_id_expr="f.journey_id")}
    {_JOURNEY_SIDE_JOIN.format(alias="hj", journey_id_expr="(rp.safe_payload ->> 'believedOriginalJourneyId')::uuid")}

    WHERE f.tenant_id = :tenant_id
      AND f.finding_type_code = '{_FINDING_TYPE}'
      AND (:include_closed OR f.finding_status IN ('OPEN','ACKNOWLEDGED'))
      AND EXISTS (
            SELECT 1 FROM auditcore.business_assignments ba
            WHERE ba.tenant_id = dj.tenant_id
              AND ba.security_actor_id = :actor_id
              AND ba.assignment_status = 'ACTIVE'
              AND ba.effective_from <= now()
              AND (ba.effective_to IS NULL OR ba.effective_to >= now())
              AND (
                    ba.dealer_id IS NULL
                    OR (ba.dealer_id = dj.dealer_id
                        AND (ba.outlet_id IS NULL OR ba.outlet_id = dj.outlet_id))
              )
      )
    ORDER BY f.created_at_utc DESC
"""


def _side(row: dict[str, Any], prefix: str) -> DuplicateBookingSide:
    return DuplicateBookingSide(
        journeyId=row[f"{prefix}_journey_id"],
        journeyReference=row[f"{prefix}_journey_reference"],
        customerName=row[f"{prefix}_customer_name"],
        dealerName=row[f"{prefix}_dealer_name"],
        outletName=row[f"{prefix}_outlet_name"],
        productLabel=row[f"{prefix}_product_label"],
        bookingReference=row[f"{prefix}_booking_reference"],
        bookingConfirmDate=row[f"{prefix}_booking_confirm_date"],
    )


def _actor_roles(connection: Connection, *, tenant_id: str, actor_id: str) -> list[str]:
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT upper(business_role_code) AS role
            FROM auditcore.business_assignments
            WHERE tenant_id = :tenant_id AND security_actor_id = :actor_id
              AND assignment_status = 'ACTIVE'
              AND effective_from <= now() AND (effective_to IS NULL OR effective_to >= now())
            """
        ),
        {"tenant_id": tenant_id, "actor_id": actor_id},
    ).scalars().all()
    return [r if r != "EXEC" else "EXECUTIVE" for r in rows]


@router.get("/duplicate-bookings", response_model=DuplicateBookingsResponse)
def get_duplicate_bookings(
    tenant_id: str,
    includeClosed: Annotated[bool, Query()] = False,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)] = None,
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ] = None,
    connection: Annotated[Connection, Depends(get_connection)] = None,
) -> DuplicateBookingsResponse:
    _authorize(authorization_client, human_principal=human_principal, tenant_id=tenant_id)
    set_tenant_context(connection, tenant_id)
    roles = _actor_roles(connection, tenant_id=tenant_id, actor_id=human_principal.subject)

    rows = connection.execute(
        text(_DUPLICATE_BOOKINGS_SQL),
        {
            "tenant_id": tenant_id,
            "actor_id": human_principal.subject,
            "include_closed": includeClosed,
        },
    ).mappings().all()

    pairs = [
        DuplicateBookingPair(
            findingId=row["audit_finding_id"],
            status=row["finding_status"],
            severity=row["severity"],
            raisedAtUtc=row["raised_at_utc"],
            matchBasis=row["match_basis"],
            matchBasisLabel=_BASIS_LABEL.get(row["match_basis"] or "", row["match_basis"]),
            matchConfidencePercent=row["match_confidence_percent"],
            matchConfidenceLabel=row["match_confidence_label"],
            duplicate=_side(row, "duplicate"),
            holder=_side(row, "holder"),
        )
        for row in rows
    ]
    return DuplicateBookingsResponse(roles=roles, generatedAtUtc=datetime.now(UTC), pairs=pairs)


__all__ = ["router"]
