from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.errors import AuditCoreError, NotFoundError


def create_price_list(
    connection: Connection,
    *,
    tenant_id: str,
    code: str,
    name: str,
    actor_id: str | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.price_lists (
                tenant_id, price_list_code, price_list_name, created_by_actor_id
            ) VALUES (:tenant_id, :code, :name, :actor_id)
            RETURNING price_list_id
            """
        ),
        {"tenant_id": tenant_id, "code": code, "name": name, "actor_id": actor_id},
    ).scalar_one()


def create_price_list_version(
    connection: Connection,
    *,
    tenant_id: str,
    price_list_id: UUID,
    version_no: int,
    effective_from: date,
    effective_to: date | None = None,
    currency_code: str = "INR",
    actor_id: str | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.price_list_versions (
                tenant_id, price_list_id, version_no, effective_from,
                effective_to, currency_code, created_by_actor_id
            ) VALUES (
                :tenant_id, :price_list_id, :version_no, :effective_from,
                :effective_to, :currency_code, :actor_id
            ) RETURNING price_list_version_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "price_list_id": price_list_id,
            "version_no": version_no,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "currency_code": currency_code,
            "actor_id": actor_id,
        },
    ).scalar_one()


def add_price_list_item(
    connection: Connection,
    *,
    tenant_id: str,
    price_list_version_id: UUID,
    product_sku_id: UUID,
    component_key: str,
    standard_amount: Decimal,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.price_list_items (
                tenant_id, price_list_version_id, product_sku_id,
                component_key, standard_amount
            ) VALUES (
                :tenant_id, :version_id, :sku_id, :component_key, :amount
            ) RETURNING price_list_item_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "version_id": price_list_version_id,
            "sku_id": product_sku_id,
            "component_key": component_key,
            "amount": standard_amount,
        },
    ).scalar_one()


def publish_price_list_version(
    connection: Connection,
    *,
    tenant_id: str,
    price_list_version_id: UUID,
    actor_id: str,
) -> None:
    result = connection.execute(
        text(
            """
            UPDATE auditcore.price_list_versions
            SET lifecycle_status = 'PUBLISHED',
                published_by_actor_id = :actor_id,
                published_at_utc = now(),
                updated_at_utc = now()
            WHERE tenant_id = :tenant_id
              AND price_list_version_id = :version_id
              AND lifecycle_status = 'DRAFT'
            """
        ),
        {"tenant_id": tenant_id, "version_id": price_list_version_id, "actor_id": actor_id},
    )
    if result.rowcount != 1:
        raise AuditCoreError(
            error_code="VAC-MASTER-003",
            status_code=409,
            title="Master publish conflict",
            detail="Price List version cannot be published from its current state.",
        )


def retire_price_list_version(
    connection: Connection,
    *,
    tenant_id: str,
    price_list_version_id: UUID,
    actor_id: str,
) -> None:
    result = connection.execute(
        text(
            """
            UPDATE auditcore.price_list_versions
            SET lifecycle_status = 'RETIRED',
                retired_by_actor_id = :actor_id,
                retired_at_utc = now(),
                updated_at_utc = now()
            WHERE tenant_id = :tenant_id
              AND price_list_version_id = :version_id
              AND lifecycle_status = 'PUBLISHED'
            """
        ),
        {"tenant_id": tenant_id, "version_id": price_list_version_id, "actor_id": actor_id},
    )
    if result.rowcount != 1:
        raise AuditCoreError(
            error_code="VAC-MASTER-003",
            status_code=409,
            title="Master retire conflict",
            detail="Price List version cannot be retired from its current state.",
        )


# The OEM's own book (2026-10-20 product owner rules, defect 14): a tenant
# that has the OEM native price list is priced from it alone. A segment
# template list published with a later date must never shadow it.
OEM_NATIVE_PRICE_LIST_CODE = "OEM_NATIVE_PRICE_LIST"

# A version is in force from its effective_from until the day before the
# next version of the same list starts (or its own effective_to, if one was
# set). Published versions are immutable, so the end is derived on read.
_PLAN_COLUMNS = """
       pl.price_list_id,
       pl.price_list_code,
       pl.price_list_name,
       plv.price_list_version_id,
       plv.version_no,
       plv.effective_from,
       plv.effective_to,
       plv.currency_code,
       plv.lifecycle_status,
       (SELECT min(nxt.effective_from)
          FROM auditcore.price_list_versions nxt
         WHERE nxt.tenant_id = plv.tenant_id
           AND nxt.price_list_id = plv.price_list_id
           AND nxt.lifecycle_status IN ('PUBLISHED', 'RETIRED')
           AND nxt.effective_from > plv.effective_from) AS next_effective_from
"""


def _with_in_force_to(row: Any) -> dict[str, Any]:
    plan = dict(row)
    nxt = plan.get("next_effective_from")
    ends = [d for d in (plan.get("effective_to"), nxt - timedelta(days=1) if nxt else None) if d is not None]
    plan["in_force_to"] = min(ends) if ends else None
    return plan


def no_price_master_detail(effective_on: date) -> str:
    """The one wording for "nothing priced this date" (R5: say so, never
    fall back to another month)."""
    return f"No price master in force on {effective_on.isoformat()}"


def find_effective_price_plan(
    connection: Connection,
    *,
    tenant_id: str,
    effective_on: date,
) -> dict[str, Any] | None:
    """The Price List version in force on a business date, or None.

    In force: the latest version whose effective_from is on or before the
    date (the highest version number on a shared date), which stays in force
    until the next version's effective_from. ``in_force_to`` carries that
    end (None while no later version exists). Nothing before the first
    version: None, never the newest book (2026-10-20 product owner rules,
    R5). RETIRED versions remain eligible for historical business dates.
    Only the OEM native list counts when the tenant has one.
    """

    row = connection.execute(
        text(
            f"""
            SELECT {_PLAN_COLUMNS}
            FROM auditcore.price_list_versions plv
            JOIN auditcore.price_lists pl
              ON pl.tenant_id = plv.tenant_id
             AND pl.price_list_id = plv.price_list_id
            WHERE plv.tenant_id = :tenant_id
              AND plv.lifecycle_status IN ('PUBLISHED', 'RETIRED')
              AND plv.effective_from <= :effective_on
              AND (plv.effective_to IS NULL OR plv.effective_to >= :effective_on)
              AND (
                  pl.price_list_code = :native
                  OR NOT EXISTS (
                      SELECT 1 FROM auditcore.price_lists own
                      WHERE own.tenant_id = plv.tenant_id AND own.price_list_code = :native
                  )
              )
            ORDER BY plv.effective_from DESC,
                     plv.version_no DESC,
                     plv.price_list_version_id DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "effective_on": effective_on, "native": OEM_NATIVE_PRICE_LIST_CODE},
    ).mappings().one_or_none()
    return _with_in_force_to(row) if row is not None else None


def price_plan_by_version(connection: Connection, *, tenant_id: str, version_id: Any) -> dict[str, Any] | None:
    """One version as a plan (same shape as find_effective_price_plan)."""
    row = connection.execute(
        text(
            f"""
            SELECT {_PLAN_COLUMNS}
            FROM auditcore.price_list_versions plv
            JOIN auditcore.price_lists pl
              ON pl.tenant_id = plv.tenant_id
             AND pl.price_list_id = plv.price_list_id
            WHERE plv.tenant_id = :tenant_id
              AND plv.price_list_version_id = :version_id
            """
        ),
        {"tenant_id": tenant_id, "version_id": version_id},
    ).mappings().one_or_none()
    return _with_in_force_to(row) if row is not None else None


def resolve_effective_price_plan(
    connection: Connection,
    *,
    tenant_id: str,
    effective_on: date,
) -> dict[str, Any]:
    plan = find_effective_price_plan(
        connection,
        tenant_id=tenant_id,
        effective_on=effective_on,
    )
    if plan is None:
        raise AuditCoreError(
            error_code="VAC-MASTER-002",
            status_code=422,
            title="No effective master version",
            detail=no_price_master_detail(effective_on),
        )
    return plan


def get_price_matrix(
    connection: Connection,
    *,
    tenant_id: str,
    price_list_version_id: UUID,
    product_sku_id: UUID,
) -> list[dict[str, Any]]:
    """Return static price components for one Price List version and one SKU."""

    rows = connection.execute(
        text(
            """
            SELECT plv.currency_code,
                   pli.component_key,
                   pli.standard_amount,
                   pli.tax_inclusive
            FROM auditcore.price_list_items pli
            JOIN auditcore.price_list_versions plv
              ON plv.tenant_id = pli.tenant_id
             AND plv.price_list_version_id = pli.price_list_version_id
            WHERE pli.tenant_id = :tenant_id
              AND pli.price_list_version_id = :price_list_version_id
              AND pli.product_sku_id = :product_sku_id
            ORDER BY pli.component_key
            """
        ),
        {
            "tenant_id": tenant_id,
            "price_list_version_id": price_list_version_id,
            "product_sku_id": product_sku_id,
        },
    ).mappings().all()
    if not rows:
        raise NotFoundError(
            error_code="VAC-NF-012",
            title="Price matrix not found",
            detail="No Price Master rows exist for the selected Price List version and Product SKU.",
        )
    return [dict(row) for row in rows]


def resolve_effective_price_list_version(
    connection: Connection,
    *,
    tenant_id: str,
    price_list_id: UUID,
    effective_on: date,
) -> UUID:
    version_id = connection.execute(
        text(
            """
            SELECT price_list_version_id
            FROM auditcore.price_list_versions
            WHERE tenant_id = :tenant_id
              AND price_list_id = :price_list_id
              AND lifecycle_status IN ('PUBLISHED', 'RETIRED')
              AND effective_from <= :effective_on
              AND (effective_to IS NULL OR effective_to >= :effective_on)
            ORDER BY effective_from DESC, version_no DESC, price_list_version_id DESC
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "price_list_id": price_list_id,
            "effective_on": effective_on,
        },
    ).scalar_one_or_none()
    if version_id is None:
        raise AuditCoreError(
            error_code="VAC-MASTER-002",
            status_code=422,
            title="No effective master version",
            detail=no_price_master_detail(effective_on),
        )
    return version_id
