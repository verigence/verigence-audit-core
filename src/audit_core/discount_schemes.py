from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.errors import AuditCoreError


def create_discount_scheme(
    connection: Connection,
    *,
    tenant_id: str,
    code: str,
    name: str,
    category: str | None = None,
    actor_id: str | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.discount_schemes (
                tenant_id, scheme_code, scheme_name, scheme_category, created_by_actor_id
            ) VALUES (:tenant_id, :code, :name, :category, :actor_id)
            RETURNING discount_scheme_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "code": code,
            "name": name,
            "category": category,
            "actor_id": actor_id,
        },
    ).scalar_one()


def create_discount_scheme_version(
    connection: Connection,
    *,
    tenant_id: str,
    discount_scheme_id: UUID,
    version_no: int,
    effective_from: date,
    effective_to: date | None = None,
    actor_id: str | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.discount_scheme_versions (
                tenant_id, discount_scheme_id, version_no, effective_from,
                effective_to, created_by_actor_id
            ) VALUES (
                :tenant_id, :scheme_id, :version_no, :effective_from,
                :effective_to, :actor_id
            ) RETURNING discount_scheme_version_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "scheme_id": discount_scheme_id,
            "version_no": version_no,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "actor_id": actor_id,
        },
    ).scalar_one()


def add_discount_eligibility(
    connection: Connection,
    *,
    tenant_id: str,
    discount_scheme_version_id: UUID,
    product_sku_id: UUID | None = None,
    dealer_id: UUID | None = None,
    outlet_id: UUID | None = None,
    customer_type_code: str | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.discount_scheme_eligibility (
                tenant_id, discount_scheme_version_id, product_sku_id,
                dealer_id, outlet_id, customer_type_code
            ) VALUES (
                :tenant_id, :version_id, :sku_id,
                :dealer_id, :outlet_id, :customer_type_code
            ) RETURNING eligibility_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "version_id": discount_scheme_version_id,
            "sku_id": product_sku_id,
            "dealer_id": dealer_id,
            "outlet_id": outlet_id,
            "customer_type_code": customer_type_code,
        },
    ).scalar_one()


def add_discount_benefit(
    connection: Connection,
    *,
    tenant_id: str,
    discount_scheme_version_id: UUID,
    benefit_key: str,
    benefit_type: str,
    amount_value: Decimal | None = None,
    percentage_value: Decimal | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.discount_scheme_benefits (
                tenant_id, discount_scheme_version_id, benefit_key,
                benefit_type, amount_value, percentage_value
            ) VALUES (
                :tenant_id, :version_id, :benefit_key,
                :benefit_type, :amount_value, :percentage_value
            ) RETURNING benefit_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "version_id": discount_scheme_version_id,
            "benefit_key": benefit_key,
            "benefit_type": benefit_type,
            "amount_value": amount_value,
            "percentage_value": percentage_value,
        },
    ).scalar_one()


def publish_discount_scheme_version(
    connection: Connection,
    *,
    tenant_id: str,
    discount_scheme_version_id: UUID,
    actor_id: str,
) -> None:
    result = connection.execute(
        text(
            """
            UPDATE auditcore.discount_scheme_versions
            SET lifecycle_status = 'PUBLISHED',
                published_by_actor_id = :actor_id,
                published_at_utc = now(), updated_at_utc = now()
            WHERE tenant_id = :tenant_id
              AND discount_scheme_version_id = :version_id
              AND lifecycle_status = 'DRAFT'
            """
        ),
        {"tenant_id": tenant_id, "version_id": discount_scheme_version_id, "actor_id": actor_id},
    )
    if result.rowcount != 1:
        raise AuditCoreError(
            error_code="VAC-MASTER-003",
            status_code=409,
            title="Master publish conflict",
            detail="Discount Scheme version cannot be published from its current state.",
        )


def retire_discount_scheme_version(
    connection: Connection,
    *,
    tenant_id: str,
    discount_scheme_version_id: UUID,
    actor_id: str,
) -> None:
    result = connection.execute(
        text(
            """
            UPDATE auditcore.discount_scheme_versions
            SET lifecycle_status = 'RETIRED',
                retired_by_actor_id = :actor_id,
                retired_at_utc = now(), updated_at_utc = now()
            WHERE tenant_id = :tenant_id
              AND discount_scheme_version_id = :version_id
              AND lifecycle_status = 'PUBLISHED'
            """
        ),
        {"tenant_id": tenant_id, "version_id": discount_scheme_version_id, "actor_id": actor_id},
    )
    if result.rowcount != 1:
        raise AuditCoreError(
            error_code="VAC-MASTER-003",
            status_code=409,
            title="Master retire conflict",
            detail="Discount Scheme version cannot be retired from its current state.",
        )
