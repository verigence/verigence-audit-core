from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.errors import AuditCoreError
from audit_core.price_lists import (
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


def _latest_published_price_plan(
    connection: Connection, *, tenant_id: str, price_list_id: UUID | None
) -> dict[str, Any] | None:
    """The newest PUBLISHED version, regardless of its effective window.

    Fallback only: used when nothing is effective as of the booking's own
    date -- e.g. a real Booking Form dated well before the tenant's OEM
    master was ever ingested (the master's effective_from is necessarily
    ``today`` at ingestion time, so any historical booking_date predates it).
    Requiring strict historical effectiveness there would leave SKU
    resolution permanently unable to run, for every such booking, with no
    way for it to ever become effective later. A fresh onboarding's only
    master is meant to apply to the deals already in flight.
    """
    row = connection.execute(
        text(
            """
            SELECT pl.price_list_id,
                   pl.price_list_code,
                   pl.price_list_name,
                   plv.price_list_version_id,
                   plv.version_no,
                   plv.effective_from,
                   plv.effective_to,
                   plv.currency_code,
                   plv.lifecycle_status
            FROM auditcore.price_list_versions plv
            JOIN auditcore.price_lists pl
              ON pl.tenant_id=plv.tenant_id
             AND pl.price_list_id=plv.price_list_id
            WHERE plv.tenant_id=:tenant_id
              AND plv.lifecycle_status='PUBLISHED'
              AND (CAST(:price_list_id AS uuid) IS NULL OR plv.price_list_id=CAST(:price_list_id AS uuid))
            ORDER BY plv.effective_from DESC, plv.version_no DESC, plv.price_list_version_id DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "price_list_id": str(price_list_id) if price_list_id else None},
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def _price_plan_for_journey(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    effective_on: date,
) -> dict[str, Any]:
    selected_price_list_id = connection.execute(
        text(
            """
            SELECT price_list_id
            FROM auditcore.bookings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()

    if selected_price_list_id is None:
        try:
            return resolve_effective_price_plan(
                connection,
                tenant_id=tenant_id,
                effective_on=effective_on,
            )
        except AuditCoreError:
            fallback = _latest_published_price_plan(
                connection, tenant_id=tenant_id, price_list_id=None
            )
            if fallback is None:
                raise
            return fallback

    try:
        version_id = resolve_effective_price_list_version(
            connection,
            tenant_id=tenant_id,
            price_list_id=selected_price_list_id,
            effective_on=effective_on,
        )
    except AuditCoreError:
        fallback = _latest_published_price_plan(
            connection, tenant_id=tenant_id, price_list_id=selected_price_list_id
        )
        if fallback is None:
            raise
        return fallback
    row = connection.execute(
        text(
            """
            SELECT pl.price_list_id,
                   pl.price_list_code,
                   pl.price_list_name,
                   plv.price_list_version_id,
                   plv.version_no,
                   plv.effective_from,
                   plv.effective_to,
                   plv.currency_code,
                   plv.lifecycle_status
            FROM auditcore.price_list_versions plv
            JOIN auditcore.price_lists pl
              ON pl.tenant_id=plv.tenant_id
             AND pl.price_list_id=plv.price_list_id
            WHERE plv.tenant_id=:tenant_id
              AND plv.price_list_version_id=:version_id
            """
        ),
        {"tenant_id": tenant_id, "version_id": version_id},
    ).mappings().one()
    return dict(row)


