from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.errors import AuditCoreError
from audit_core.price_lists import (
    no_price_master_detail,
    price_plan_by_version,
    resolve_effective_price_list_version,
    resolve_effective_price_plan,
)

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def _normalize_label(value: str) -> str:
    return " ".join(part for part in _NON_ALNUM.sub(" ", value.upper()).split() if part)


def _normalized_key(value: str) -> str:
    return _normalize_label(value).replace(" ", "")


def _label_similarity(observed: str, master: str) -> Decimal:
    """Return 1 only for formatting-normalized exact equality; never fuzzy-match."""

    left = _normalized_key(observed)
    right = _normalized_key(master)
    if not left or not right:
        return Decimal(0)
    return Decimal(1) if left == right else Decimal(0)


def journey_pricing_date(connection: Connection, *, tenant_id: str, journey_id: UUID) -> date | None:
    """The ONE date a Journey's deal is priced on (2026-10-20 product owner
    rules, R5): the date applied to it (``bookings.pricing_effective_on``:
    the PC's booking/invoice choice or a Team Lead's other-month approval),
    else the booking date on the booking form (as read, or as the PC
    corrected it), else the booking date entered by hand. Never today: with
    none of them the deal is not priced yet.

    Every price-master lookup for a Journey (SKU candidates and resolution,
    the SKU pricing panel, deal reconciliation) takes its date from here."""
    from audit_core.uc03_p2_dates import booking_form_date

    row = connection.execute(
        text(
            """
            SELECT pricing_effective_on, booking_date
            FROM auditcore.bookings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is not None and row["pricing_effective_on"] is not None:
        return row["pricing_effective_on"]
    form = booking_form_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    if form and form.get("date"):
        return form["date"]
    return row["booking_date"] if row is not None else None


def journey_price_plan(
    connection: Connection, *, tenant_id: str, journey_id: UUID, effective_on: date | None = None
) -> dict[str, Any]:
    """The price master version a Journey is priced from, and why not when
    it is not: {"onDate", "plan", "reason"}.

    ``onDate`` is ``effective_on`` when given, else ``journey_pricing_date``.
    ``plan`` is the version actually used (price_list_name, version_no,
    effective_from, in_force_to, ...): name THIS on the Deal tab, not
    ``journeys.price_list_version_id`` (which is set when the Journey is
    created). ``plan`` is None with ``reason`` "No booking date ..." or
    "No price master in force on <date>"; nothing falls back to another
    month or to the newest book."""
    on = effective_on or journey_pricing_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    if on is None:
        return {"onDate": None, "plan": None, "reason": "No booking date to price the deal on"}
    try:
        plan = _price_plan_for_journey(connection, tenant_id=tenant_id, journey_id=journey_id, effective_on=on)
    except AuditCoreError as exc:
        return {"onDate": on, "plan": None, "reason": exc.detail or no_price_master_detail(on)}
    return {"onDate": on, "plan": plan, "reason": None}


PRICE_MASTER_NOT_IN_FORCE_TASK = "PRICE_MASTER_NOT_IN_FORCE"


def _not_in_force_key(journey_id: UUID) -> str:
    return f"price-master-not-in-force:{journey_id}"


def sync_price_master_in_force_task(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> dict[str, Any]:
    """Raise "No price master in force on <date>" for a Journey whose
    pricing date no book covers, and close it once one does (R5: say so and
    raise a task; never price on another month silently). No pricing date:
    nothing here (PC_BOOKING_DATE_MISSING asks for it)."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh, resolve_if_open

    found = journey_price_plan(connection, tenant_id=tenant_id, journey_id=journey_id)
    on = found["onDate"]
    if on is None or found["plan"] is not None:
        # No pricing date any more (cleared): the question is gone, and
        # PC_BOOKING_DATE_MISSING asks for the date.
        resolve_if_open(
            connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=_not_in_force_key(journey_id),
            evidence={
                "priceListVersionId": str(found["plan"]["price_list_version_id"]) if found["plan"] else None,
                "onDate": on.isoformat() if on else None,
            },
        )
        return found
    raise_or_refresh(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        dedupe_key=_not_in_force_key(journey_id),
        task_type=PRICE_MASTER_NOT_IN_FORCE_TASK,
        source_type="RESOLVER",
        source_code=PRICE_MASTER_NOT_IN_FORCE_TASK,
        title=no_price_master_detail(on),
        description=(
            f"{no_price_master_detail(on)}: this deal is priced on {on.isoformat()} and no price master "
            "version covers that date, so it has no standard price and no SKU can be checked against one. "
            "Load the OEM price list for that date, or correct the pricing date."
        ),
        reference={"sourceCode": PRICE_MASTER_NOT_IN_FORCE_TASK, "onDate": on.isoformat(), "reason": found["reason"]},
        severity="MEDIUM",
        registry=get_registry(),
    )
    return found


SKU_NOT_IN_PRICE_MASTER_TASK = "SKU_NOT_IN_PRICE_MASTER"
# The open statuses of uc03_p2_task_producer (imported lazily there).
_OPEN_TASK_STATUSES = ("READY", "IN_PROGRESS", "RETURNED", "ACTION_COMPLETED", "VERIFYING")


def _sku_not_in_book_key(journey_id: UUID) -> str:
    return f"sku-not-in-price-master:{journey_id}"


def sync_sku_in_price_master_task(
    connection: Connection, *, tenant_id: str, journey_id: UUID, found: dict[str, Any] | None = None,
) -> bool:
    """The Journey's SKU is not in the price master in force on its pricing
    date (a complete book withdrew it): raise "SKU <code> is not in the price
    master for <date> — set the SKU" for the PC and clear the standards the
    deal still carries from an older book, so no stale figure is compared
    (2026-10-20 product owner rules, review of PR 468). Closes once the SKU
    is in the book in force (another SKU set, the date corrected, a book
    re-loaded). Returns True when the SKU is missing from the book."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh, resolve_if_open

    sku = connection.execute(
        text(
            """
            SELECT jp.product_sku_id, s.sku_code
            FROM auditcore.journey_products jp
            JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id
            WHERE jp.tenant_id=:tenant_id AND jp.journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    found = found or journey_price_plan(connection, tenant_id=tenant_id, journey_id=journey_id)
    plan, on = found["plan"], found["onDate"]
    missing = bool(sku and plan) and connection.execute(
        text(
            "SELECT NOT EXISTS (SELECT 1 FROM auditcore.price_list_items WHERE tenant_id=:t "
            "AND price_list_version_id=:v AND product_sku_id=:s)"
        ),
        {"t": tenant_id, "v": plan["price_list_version_id"], "s": sku["product_sku_id"]},
    ).scalar_one()
    if not missing:
        resolve_if_open(
            connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=_sku_not_in_book_key(journey_id),
            evidence={"skuCode": sku["sku_code"] if sku else None, "reason": "SKU priced or no longer set"},
        )
        return False
    connection.execute(
        text(
            "UPDATE auditcore.commercial_lines SET standard_amount = NULL, price_list_item_id = NULL "
            "WHERE tenant_id=:t AND journey_id=:j AND price_list_item_id IS NOT NULL"
        ),
        {"t": tenant_id, "j": journey_id},
    )
    label = f"{plan.get('price_list_name') or plan.get('price_list_code')} v{plan.get('version_no')}"
    raise_or_refresh(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        dedupe_key=_sku_not_in_book_key(journey_id),
        task_type=SKU_NOT_IN_PRICE_MASTER_TASK,
        source_type="RESOLVER",
        source_code=SKU_NOT_IN_PRICE_MASTER_TASK,
        title=f"SKU {sku['sku_code']} is not in the price master for {on.isoformat()} — set the SKU",
        description=(
            f"The vehicle on this Journey ({sku['sku_code']}) is not listed in the price master in force on "
            f"{on.isoformat()} ({label}, from {plan.get('effective_from')}). The OEM withdrew or renamed it; "
            "the deal has no standard price until the SKU is set to the vehicle sold."
        ),
        reference={
            "sourceCode": SKU_NOT_IN_PRICE_MASTER_TASK,
            "skuCode": sku["sku_code"],
            "onDate": on.isoformat(),
            "priceListVersionId": str(plan["price_list_version_id"]),
        },
        severity="MEDIUM",
        registry=get_registry(),
    )
    return True


def sweep_price_master_tasks(connection: Connection, *, tenant_id: str) -> int:
    """After a price master is published: re-check every Journey with an open
    "no price master in force" or "SKU not in the price master" task, so the
    ones the new book covers close at once. Returns the Journeys checked."""
    journeys = connection.execute(
        text(
            """
            SELECT DISTINCT journey_id FROM auditcore.p2_tasks
            WHERE tenant_id=:t AND task_type = ANY(:types)
              AND task_status = ANY(:open)
            """
        ),
        {"t": tenant_id, "types": [PRICE_MASTER_NOT_IN_FORCE_TASK, SKU_NOT_IN_PRICE_MASTER_TASK],
         "open": list(_OPEN_TASK_STATUSES)},
    ).scalars().all()
    for journey_id in journeys:
        found = sync_price_master_in_force_task(connection, tenant_id=tenant_id, journey_id=journey_id)
        sync_sku_in_price_master_task(connection, tenant_id=tenant_id, journey_id=journey_id, found=found)
    return len(journeys)


def _price_plan_for_journey(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    effective_on: date,
) -> dict[str, Any]:
    """The version in force for the Journey on ``effective_on``.

    A Team Lead-approved version first, whatever the date. Otherwise the
    version in force on the date: of the booking's chosen price list, else
    of the tenant's OEM book. Nothing in force raises AuditCoreError
    VAC-MASTER-002 "No price master in force on <date>" (2026-10-20 product
    owner rules, R5). The old fallback to the newest published version
    "regardless of its effective window" is gone: it priced an August
    booking on the October book without a word."""
    booking = connection.execute(
        text(
            """
            SELECT price_list_id, pricing_price_list_version_id
            FROM auditcore.bookings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none() or {}
    selected_price_list_id = booking.get("price_list_id")
    approved_version_id = booking.get("pricing_price_list_version_id")
    if approved_version_id is not None:
        # A Team Lead approved pricing this Journey on one specific version
        # (another month's list, or another version of the same day): that
        # version, whatever the date says.
        approved = _plan_row(connection, tenant_id=tenant_id, version_id=approved_version_id)
        if approved is not None:
            return approved

    if selected_price_list_id is None:
        return resolve_effective_price_plan(connection, tenant_id=tenant_id, effective_on=effective_on)

    version_id = resolve_effective_price_list_version(
        connection,
        tenant_id=tenant_id,
        price_list_id=selected_price_list_id,
        effective_on=effective_on,
    )
    row = _plan_row(connection, tenant_id=tenant_id, version_id=version_id)
    assert row is not None
    return row


def _plan_row(connection: Connection, *, tenant_id: str, version_id: Any) -> dict[str, Any] | None:
    return price_plan_by_version(connection, tenant_id=tenant_id, version_id=version_id)
