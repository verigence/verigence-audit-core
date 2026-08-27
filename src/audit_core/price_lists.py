from datetime import date
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


def find_effective_price_plan(
    connection: Connection,
    *,
    tenant_id: str,
    effective_on: date,
) -> dict[str, Any] | None:
    """Return the single latest Price List version effective on a business date.

    Runtime lookup deliberately stays small: one query over static versioned master
    rows. RETIRED versions remain eligible for historical business dates so a Booking
    captured on a later day still resolves the master that applied when it occurred.
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
              ON pl.tenant_id = plv.tenant_id
             AND pl.price_list_id = plv.price_list_id
            WHERE plv.tenant_id = :tenant_id
              AND plv.lifecycle_status IN ('PUBLISHED', 'RETIRED')
              AND plv.effective_from <= :effective_on
              AND (plv.effective_to IS NULL OR plv.effective_to >= :effective_on)
            ORDER BY plv.effective_from DESC,
                     plv.version_no DESC,
                     plv.price_list_version_id DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "effective_on": effective_on},
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


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
            detail="No effective Price List version exists for the requested business date.",
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
            detail="No effective Price List version exists for the requested date.",
        )
    return version_id
