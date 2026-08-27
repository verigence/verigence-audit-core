from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import DependencyUnavailableError, NotFoundError
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    SecurityAuthorizationError,
    get_security_authorization_client,
)
from audit_core.uc03_work_items import (
    WorkItemFilters,
    WorkItemPage,
    _encode_cursor,
    _filter_fingerprint,
    _row_to_item,
)

router = APIRouter(prefix="/v1/tenants/{tenant_id}/uc03", tags=["uc03-work-items-fast"])
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


@router.get("/work-items-fast", response_model=WorkItemPage)
def list_first_work_items_fast(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    outlet_id: Annotated[UUID | None, Query(alias="outletId")] = None,
    limit: Annotated[int, Query(ge=1, le=10)] = 10,
) -> WorkItemPage:
    """Return the first Work Queue page with one database statement after Security.

    This endpoint is intentionally narrow: it serves only the initial ALL-work landing
    page. Tenant RLS context, Project metadata, authorized Journey selection, flags,
    Evidence activity and ingestion activity are resolved inside one PostgreSQL
    statement. Filtered/paginated reads continue to use the general work-items route.
    """

    _authorize_workspace(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )

    rows = list(
        connection.execute(
            text(
                """
                WITH runtime_context AS MATERIALIZED (
                    SELECT set_config('app.tenant_id', :tenant_id, true) AS tenant_context
                ),
                project_context AS MATERIALIZED (
                    SELECT p.project_name, p.timezone_name
                    FROM runtime_context rc
                    CROSS JOIN auditcore.projects p
                    WHERE p.tenant_id = :tenant_id
                      AND p.project_status = 'ACTIVE'
                ),
                base AS MATERIALIZED (
                    SELECT
                        j.journey_id,
                        j.dealer_id,
                        j.outlet_id,
                        c.display_name AS customer_display_name,
                        c.mobile_last4 AS customer_mobile_last4,
                        d.dealer_name,
                        o.outlet_name,
                        (b.booking_id IS NOT NULL OR bs.journey_id IS NOT NULL) AS has_booking,
                        (dl.delivery_id IS NOT NULL OR ds.journey_id IS NOT NULL) AS has_delivery,
                        b.booking_reference,
                        NULLIF(
                            concat_ws(
                                ' · ',
                                NULLIF(jp.model_name_snapshot, ''),
                                NULLIF(jp.variant_name_snapshot, ''),
                                NULLIF(jp.colour_name_snapshot, '')
                            ),
                            ''
                        ) AS product_label,
                        COALESCE(bs.business_status, b.actual_status_code) AS booking_business_status,
                        COALESCE(bs.audit_state, 'NOT_STARTED') AS booking_audit_state,
                        COALESCE(bs.audit_status, 'NOT_EVALUATED') AS booking_audit_status,
                        COALESCE(
                            b.booking_date,
                            (bs.first_started_at_utc AT TIME ZONE pc.timezone_name)::date,
                            (b.created_at_utc AT TIME ZONE pc.timezone_name)::date
                        ) AS booking_business_date,
                        COALESCE(ds.business_status, dl.actual_delivery_status_code) AS delivery_business_status,
                        COALESCE(ds.audit_state, 'NOT_STARTED') AS delivery_audit_state,
                        COALESCE(ds.audit_status, 'NOT_EVALUATED') AS delivery_audit_status,
                        COALESCE(
                            (dl.actual_delivered_at AT TIME ZONE pc.timezone_name)::date,
                            (ds.first_started_at_utc AT TIME ZONE pc.timezone_name)::date,
                            (dl.created_at_utc AT TIME ZONE pc.timezone_name)::date
                        ) AS delivery_business_date,
                        COALESCE(fc.open_flag_count, 0) AS open_flag_count,
                        COALESCE(fc.total_flag_count, 0) AS total_flag_count,
                        fc.highest_open_severity,
                        COALESCE(proc.processing_document_count, 0) AS processing_document_count,
                        GREATEST(
                            j.updated_at_utc,
                            bs.latest_activity_at_utc,
                            ds.latest_activity_at_utc,
                            b.updated_at_utc,
                            dl.updated_at_utc,
                            ev.latest_evidence_activity,
                            fc.latest_finding_activity,
                            proc.latest_processing_activity
                        ) AS latest_activity_at_utc
                    FROM project_context pc
                    JOIN auditcore.journeys j
                      ON j.tenant_id = :tenant_id
                    JOIN auditcore.customers c
                      ON c.tenant_id = j.tenant_id
                     AND c.customer_id = j.customer_id
                    JOIN auditcore.dealers d
                      ON d.tenant_id = j.tenant_id
                     AND d.dealer_id = j.dealer_id
                    JOIN auditcore.dealer_outlets o
                      ON o.tenant_id = j.tenant_id
                     AND o.dealer_id = j.dealer_id
                     AND o.outlet_id = j.outlet_id
                    LEFT JOIN auditcore.bookings b
                      ON b.tenant_id = j.tenant_id
                     AND b.journey_id = j.journey_id
                    LEFT JOIN auditcore.deliveries dl
                      ON dl.tenant_id = j.tenant_id
                     AND dl.journey_id = j.journey_id
                    LEFT JOIN auditcore.journey_products jp
                      ON jp.tenant_id = j.tenant_id
                     AND jp.journey_id = j.journey_id
                    LEFT JOIN auditcore.journey_stage_states bs
                      ON bs.tenant_id = j.tenant_id
                     AND bs.journey_id = j.journey_id
                     AND bs.stage_code = 'BOOKING'
                    LEFT JOIN auditcore.journey_stage_states ds
                      ON ds.tenant_id = j.tenant_id
                     AND ds.journey_id = j.journey_id
                     AND ds.stage_code = 'DELIVERY'
                    LEFT JOIN LATERAL (
                        SELECT
                            count(*) FILTER (
                                WHERE af.finding_status IN ('OPEN','ACKNOWLEDGED')
                            ) AS open_flag_count,
                            count(*) FILTER (
                                WHERE af.finding_status <> 'VOIDED'
                            ) AS total_flag_count,
                            (
                                array_agg(
                                    af.severity
                                    ORDER BY
                                        CASE af.severity
                                            WHEN 'CRITICAL' THEN 5
                                            WHEN 'HIGH' THEN 4
                                            WHEN 'MEDIUM' THEN 3
                                            WHEN 'LOW' THEN 2
                                            WHEN 'INFO' THEN 1
                                            ELSE 0
                                        END DESC,
                                        af.severity
                                ) FILTER (
                                    WHERE af.finding_status IN ('OPEN','ACKNOWLEDGED')
                                )
                            )[1] AS highest_open_severity,
                            max(af.updated_at_utc) AS latest_finding_activity
                        FROM auditcore.audit_findings af
                        WHERE af.tenant_id = j.tenant_id
                          AND af.journey_id = j.journey_id
                    ) fc ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT
                            max(COALESCE(e.cache_updated_at_utc, e.linked_at_utc)) AS latest_evidence_activity
                        FROM auditcore.evidence e
                        WHERE e.tenant_id = j.tenant_id
                          AND e.journey_id = j.journey_id
                          AND e.association_status = 'ACTIVE'
                    ) ev ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT
                            count(*) FILTER (
                                WHERE op.operation_status IN (
                                    'RECEIVED','DI_SUBMITTING','DI_ACCEPTED','RETRY_WAIT'
                                )
                            ) AS processing_document_count,
                            max(op.updated_at_utc) FILTER (
                                WHERE op.operation_status IN (
                                    'RECEIVED','DI_SUBMITTING','DI_ACCEPTED','RETRY_WAIT'
                                )
                            ) AS latest_processing_activity
                        FROM auditcore.evidence_ingestion_operations op
                        WHERE op.tenant_id = j.tenant_id
                          AND op.journey_id = j.journey_id
                    ) proc ON TRUE
                    WHERE (
                            CAST(:outlet_id AS uuid) IS NULL
                            OR j.outlet_id = CAST(:outlet_id AS uuid)
                        )
                      AND (
                            b.booking_id IS NOT NULL
                            OR dl.delivery_id IS NOT NULL
                            OR bs.journey_id IS NOT NULL
                            OR ds.journey_id IS NOT NULL
                        )
                      AND EXISTS (
                            SELECT 1
                            FROM auditcore.business_assignments ba
                            WHERE ba.tenant_id = j.tenant_id
                              AND ba.security_actor_id = :actor_id
                              AND ba.assignment_status = 'ACTIVE'
                              AND ba.effective_from <= now()
                              AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                              AND (
                                    ba.dealer_id IS NULL
                                    OR (
                                        ba.dealer_id = j.dealer_id
                                        AND (ba.outlet_id IS NULL OR ba.outlet_id = j.outlet_id)
                                    )
                              )
                        )
                ),
                ranked AS MATERIALIZED (
                    SELECT *
                    FROM base
                    ORDER BY latest_activity_at_utc DESC, journey_id DESC
                    LIMIT :fetch_limit
                )
                SELECT
                    pc.project_name AS _project_name,
                    pc.timezone_name AS _timezone_name,
                    (r.journey_id IS NOT NULL) AS _has_item,
                    r.*
                FROM project_context pc
                LEFT JOIN ranked r ON TRUE
                ORDER BY r.latest_activity_at_utc DESC NULLS LAST,
                         r.journey_id DESC NULLS LAST
                """
            ),
            {
                "tenant_id": tenant_id,
                "actor_id": human_principal.subject,
                "outlet_id": outlet_id,
                "fetch_limit": limit + 1,
            },
        ).mappings()
    )

    if not rows:
        raise NotFoundError(
            error_code="VAC-NF-001",
            title="Project not found",
            detail="Active Project not found for the requested tenant.",
        )

    project_name = str(rows[0]["_project_name"])
    timezone_name = str(rows[0]["_timezone_name"])
    item_rows = [row for row in rows if bool(row["_has_item"])]
    has_more = len(item_rows) > limit
    page_rows = item_rows[:limit]
    items = [_row_to_item(row, project_name=project_name) for row in page_rows]

    fingerprint = _filter_fingerprint(
        tenant_id=tenant_id,
        work_type="ALL",
        from_date=None,
        to_date=None,
        timezone_name=timezone_name,
        outlet_id=outlet_id,
    )
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_cursor(
            latest_activity=last["latest_activity_at_utc"],
            journey_id=last["journey_id"],
            fingerprint=fingerprint,
        )

    return WorkItemPage(
        items=items,
        pageSize=len(items),
        nextCursor=next_cursor,
        previousCursor=None,
        filters=WorkItemFilters(
            workType="ALL",
            fromDate=None,
            toDate=None,
            timezoneName=timezone_name,
            outletId=outlet_id,
        ),
    )
