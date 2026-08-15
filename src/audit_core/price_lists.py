from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.errors import AuditCoreError


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
              AND lifecycle_status = 'PUBLISHED'
              AND effective_from <= :effective_on
              AND (effective_to IS NULL OR effective_to >= :effective_on)
            ORDER BY version_no DESC
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
            detail="No effective published Price List version exists for the requested date.",
        )
    return version_id
