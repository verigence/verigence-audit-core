"""Phase 2 deal actions: pricing date, model catalogue by date, recheck.

* Pricing date -- the date whose price-list and discount-scheme versions
  price the deal. Defaults to the booking date (unchanged behaviour); a
  reviewer can apply the invoice date or another date, with a reason, when
  the deal genuinely belongs under a later master. Applying it re-prices the
  deal through the same deal reconciliation Phase 1 uses and re-runs the
  checks.
* Catalogue by date -- every SKU in the price list effective on a date, for
  the manual model picker (selection itself goes through the existing
  confirm-sku / propose-correction flows).
* Recheck -- Phase 1's "Recheck documents" for Phase 2: pull in documents
  that finished late, recompute the stage and re-run every check.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import authorize_p2
from audit_core.uc03_p2_controls import request_control_evaluation
from audit_core.uc03_p2_dates import booking_form_date
from audit_core.uc03_p2_runtime import enqueue_work, note_facts_changed, record_activity

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-deal"])

_READ = "audit.journey.read"
_UPDATE = "audit.journey.update"


def _today() -> date:
    return datetime.now(UTC).date()


def _auth(connection, tenant_id, journey_id, principal, client, permission):
    return authorize_p2(
        connection, tenant_id=tenant_id, journey_id=journey_id, human_principal=principal,
        authorization_client=client, permission_key=permission,
    )


def _plan(connection: Connection, *, tenant_id: str, journey_id: UUID, on: date) -> dict[str, Any] | None:
    from audit_core.uc03_sku_candidates import _price_plan_for_journey

    try:
        plan = _price_plan_for_journey(connection, tenant_id=tenant_id, journey_id=journey_id, effective_on=on)
    except AuditCoreError:  # no price list effective on that date
        return None
    return {
        "priceListVersionId": str(plan["price_list_version_id"]),
        "priceList": plan.get("price_list_name") or plan.get("price_list_code"),
        "version": plan.get("version_no"),
        "effectiveFrom": plan.get("effective_from"),
        "effectiveTo": plan.get("effective_to"),
    }


def _schemes(connection: Connection, *, tenant_id: str, row: dict[str, Any], on: date) -> list[str]:
    from audit_core.uc03_deal_reconciliation import _applicable_benefits
    from audit_core.uc03_masters_alignment import registration_basis

    if row.get("model_id") is None:
        return []
    basis = registration_basis(
        customer_type_code=row.get("customer_type_code"), registration_type_code=row.get("registration_type_code"),
    )
    try:
        # Savepoint: a failed lookup must not abort the caller's transaction.
        with connection.begin_nested():
            benefits = _applicable_benefits(
                connection, tenant_id=tenant_id, model_id=row["model_id"], variant_id=row.get("variant_id"),
                effective_on=on, basis=basis,
            )
    except Exception:
        logger.warning("p2_pricing_schemes_unavailable", tenant_id=tenant_id, model_id=str(row["model_id"]),
                       exc_info=True)
        return []
    return sorted({str(b["discount_scheme_version_id"]) for b in benefits})


def _pricing_row(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT b.booking_date, b.pricing_effective_on, b.pricing_basis, b.pricing_reason,
                   b.pricing_set_by_actor_id, b.pricing_set_at_utc,
                   jp.product_sku_id, jp.selection_status, jp.model_name_snapshot, jp.variant_name_snapshot,
                   jp.colour_name_snapshot, s.sku_code, s.model_id, s.variant_id,
                   cu.customer_type_code, rr.registration_type_code
            FROM auditcore.journeys j
            LEFT JOIN auditcore.bookings b ON b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id
            LEFT JOIN auditcore.journey_products jp ON jp.tenant_id=j.tenant_id AND jp.journey_id=j.journey_id
            LEFT JOIN auditcore.product_skus s ON s.product_sku_id=jp.product_sku_id
            LEFT JOIN auditcore.customers cu ON cu.tenant_id=j.tenant_id AND cu.customer_id=j.customer_id
            LEFT JOIN auditcore.registration_records rr ON rr.tenant_id=j.tenant_id AND rr.journey_id=j.journey_id
            WHERE j.tenant_id=:t AND j.journey_id=:j
            LIMIT 1
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    return dict(row)


def booking_date_for_pricing(connection: Connection, *, tenant_id: str, journey_id: UUID,
                             row: dict[str, Any] | None = None) -> date | None:
    """The date the deal is priced on by default: the booking date on the
    booking form (as read, or as the PC corrected it), else the booking
    date entered by hand on the booking. Never today."""
    form = booking_form_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    if form and form["date"]:
        return form["date"]
    row = row or _pricing_row(connection, tenant_id=tenant_id, journey_id=journey_id)
    return row["booking_date"]


def pricing_summary(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    from audit_core.uc03_journey_overview_projection import _primary_invoice_date

    row = _pricing_row(connection, tenant_id=tenant_id, journey_id=journey_id)
    invoice_date = _primary_invoice_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    booking_date = booking_date_for_pricing(connection, tenant_id=tenant_id, journey_id=journey_id, row=row)
    # No booking date means no price until the PC enters it on the booking
    # form (a Medium task asks); the deal is never silently priced on today.
    applied = row["pricing_effective_on"] or booking_date
    options = []
    for basis, on in (("BOOKING_DATE", booking_date), ("INVOICE_DATE", invoice_date)):
        if on is None:
            continue
        options.append({
            "basis": basis, "date": on,
            "priceList": _plan(connection, tenant_id=tenant_id, journey_id=journey_id, on=on),
            "schemeVersions": _schemes(connection, tenant_id=tenant_id, row=row, on=on),
        })
    applied_plan = _plan(connection, tenant_id=tenant_id, journey_id=journey_id, on=applied) if applied else None
    applied_schemes = _schemes(connection, tenant_id=tenant_id, row=row, on=applied) if applied else []
    for option in options:
        option["differsFromApplied"] = (
            (option["priceList"] or {}).get("priceListVersionId") != (applied_plan or {}).get("priceListVersionId")
            or option["schemeVersions"] != applied_schemes
        )
    return {
        "bookingDate": booking_date,
        "bookingDateMissing": booking_date is None,
        "invoiceDate": invoice_date,
        "appliedDate": applied,
        "basis": row["pricing_basis"] or "BOOKING_DATE",
        "reason": row["pricing_reason"],
        "setByActorId": row["pricing_set_by_actor_id"],
        "setAtUtc": row["pricing_set_at_utc"],
        "appliedPriceList": applied_plan,
        "appliedSchemeCount": len(applied_schemes),
        "options": options,
        "sku": {
            "productSkuId": str(row["product_sku_id"]) if row["product_sku_id"] else None,
            "skuCode": row["sku_code"],
            "model": row["model_name_snapshot"],
            "variant": row["variant_name_snapshot"],
            "colour": row["colour_name_snapshot"],
            "selectionStatus": row["selection_status"],
        },
        # A confirmed SKU is changed through a Team Lead reviewed proposal;
        # an unconfirmed one is picked directly (existing Phase 1 flows).
        "modelChange": "PROPOSE_CORRECTION" if row["selection_status"] == "CONFIRMED" else "CONFIRM_SKU",
    }


@router.get("/journeys/{journey_id}/pricing")
def get_pricing(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    _auth(connection, tenant_id, journey_id, human_principal, authorization_client, _READ)
    return pricing_summary(connection, tenant_id=tenant_id, journey_id=journey_id)


@router.get("/journeys/{journey_id}/pricing/catalog")
def get_pricing_catalog(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    onDate: date | None = None,
) -> dict[str, Any]:
    from audit_core.errors import AuditCoreError
    from audit_core.uc03_model_resolution import get_model_catalog

    _auth(connection, tenant_id, journey_id, human_principal, authorization_client, _READ)
    row = _pricing_row(connection, tenant_id=tenant_id, journey_id=journey_id)
    # The catalogue is for picking the model; with no pricing date yet it
    # shows today's masters (the deal itself is not priced on today).
    on = (onDate or row["pricing_effective_on"]
          or booking_date_for_pricing(connection, tenant_id=tenant_id, journey_id=journey_id, row=row) or _today())
    try:
        catalog = get_model_catalog(connection, tenant_id=tenant_id, journey_id=journey_id, effective_on=on)
    except AuditCoreError as exc:
        raise HTTPException(status_code=422, detail=f"No price list is effective on {on.isoformat()}.") from exc
    return {
        "onDate": on,
        "priceList": _plan(connection, tenant_id=tenant_id, journey_id=journey_id, on=on),
        "skus": catalog["skus"],
    }


class PricingCommand(BaseModel):
    """Two choices only (decision 2026-09-30): the booking date or the
    invoice date. Any other date is not offered."""

    basis: Literal["BOOKING_DATE", "INVOICE_DATE"]
    reason: str | None = Field(default=None, max_length=1000)


@router.put("/journeys/{journey_id}/pricing")
def set_pricing(
    tenant_id: str,
    journey_id: UUID,
    command: PricingCommand,
    request: Request,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    from audit_core.uc03_deal_reconciliation import sync_deal_reconciliation
    from audit_core.uc03_journey_overview_projection import _primary_invoice_date

    _auth(connection, tenant_id, journey_id, human_principal, authorization_client, _UPDATE)
    reason = (command.reason or "").strip()
    if command.basis == "BOOKING_DATE":
        on = None
        if booking_date_for_pricing(connection, tenant_id=tenant_id, journey_id=journey_id) is None:
            raise HTTPException(
                status_code=422,
                detail="The booking form has no booking date yet. Enter it on the booking form first.",
            )
    else:
        on = _primary_invoice_date(connection, tenant_id=tenant_id, journey_id=journey_id)
        if on is None:
            raise HTTPException(status_code=422, detail="No reviewed vehicle invoice date is on file yet.")
        if len(reason) < 5:
            raise HTTPException(status_code=422, detail="Give a short reason for pricing this deal on the invoice date.")
        if _plan(connection, tenant_id=tenant_id, journey_id=journey_id, on=on) is None:
            raise HTTPException(status_code=422, detail=f"No price list is effective on {on.isoformat()}.")

    previous = _pricing_row(connection, tenant_id=tenant_id, journey_id=journey_id)
    connection.execute(
        text(
            """
            INSERT INTO auditcore.bookings (tenant_id, journey_id, pricing_effective_on, pricing_basis,
                                            pricing_reason, pricing_set_by_actor_id, pricing_set_at_utc)
            VALUES (:t, :j, :on, :basis, :reason, :actor, now())
            ON CONFLICT (tenant_id, journey_id) DO UPDATE
               SET pricing_effective_on=EXCLUDED.pricing_effective_on,
                   pricing_basis=EXCLUDED.pricing_basis,
                   pricing_reason=EXCLUDED.pricing_reason,
                   pricing_set_by_actor_id=EXCLUDED.pricing_set_by_actor_id,
                   pricing_set_at_utc=now(),
                   updated_at_utc=now()
            """
        ),
        {"t": tenant_id, "j": journey_id, "on": on, "basis": command.basis, "reason": reason or None,
         "actor": human_principal.subject},
    )
    correlation_id = get_correlation_id(request)
    deal = sync_deal_reconciliation(
        connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
    )
    record_activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type="PRICING_DATE_CHANGED",
        subject_type="JOURNEY", subject_id=str(journey_id),
        details={"from": str(previous["pricing_effective_on"] or previous["booking_date"] or ""),
                 "to": str(on or previous["booking_date"] or ""), "basis": command.basis, "reason": reason or None},
        correlation_id=correlation_id,
    )
    note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id,
                       reason="PRICING_DATE_CHANGED", correlation_id=correlation_id)
    summary = pricing_summary(connection, tenant_id=tenant_id, journey_id=journey_id)
    summary["repriced"] = not deal.get("skipped") and not deal.get("error")
    summary["repriceNote"] = deal.get("reason") if deal.get("skipped") else None
    return summary


@router.post("/journeys/{journey_id}:recheck", status_code=202)
def recheck_journey(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Pick up documents that finished late, recompute the stage and re-run
    every check now. Idempotent: repeated clicks coalesce in the queue."""
    _auth(connection, tenant_id, journey_id, human_principal, authorization_client, _UPDATE)
    correlation_id = get_correlation_id(request)
    # A page shown as "nothing read" gets its values copied from DI once
    # more (a read of what DI already holds, never a re-upload or re-read).
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue
            SET queue_status='SYNCING_TO_AUDIT_CORE', status_reason=NULL, updated_at_utc=now()
            WHERE tenant_id=:t AND journey_id=:j AND queue_status='NEEDS_REVIEW' AND di_document_id IS NOT NULL
            """
        ),
        {"t": tenant_id, "j": journey_id},
    )
    enqueue_work(
        connection, tenant_id=tenant_id, journey_id=journey_id, work_type="JOURNEY_RECONCILE",
        work_key=str(journey_id), payload={"reason": "MANUAL_RECHECK"}, correlation_id=correlation_id,
    )
    version = note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id,
                                 reason="MANUAL_RECHECK", correlation_id=correlation_id)
    units = request_control_evaluation(
        connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
        delay_seconds=0, force=True,
    )
    record_activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type="RECHECK_REQUESTED",
        subject_type="JOURNEY", subject_id=str(journey_id), details={"units": units},
        correlation_id=correlation_id,
    )
    return {"journeyId": str(journey_id), "factVersion": version, "checks": units}
