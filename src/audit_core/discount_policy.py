from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text


def find_effective_discount_policy_version(
    connection: Connection,
    *,
    tenant_id: str,
    effective_on: date,
) -> dict[str, Any] | None:
    """Return the latest published policy version effective on a business date."""

    row = connection.execute(
        text(
            """
            SELECT discount_policy_version_id, version_no, effective_from,
                   effective_to, lifecycle_status
            FROM auditcore.discount_policy_versions
            WHERE tenant_id = :tenant_id
              AND lifecycle_status IN ('PUBLISHED', 'RETIRED')
              AND effective_from <= :effective_on
              AND (effective_to IS NULL OR effective_to >= :effective_on)
            ORDER BY effective_from DESC,
                     version_no DESC,
                     discount_policy_version_id DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "effective_on": effective_on},
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def resolve_numeric_policy_parameter(
    connection: Connection,
    *,
    tenant_id: str,
    parameter_key: str,
    effective_on: date,
    dealer_id: UUID | None = None,
) -> dict[str, Any] | None:
    """Resolve a numeric Project policy, preferring a Dealer override when supplied."""

    version = find_effective_discount_policy_version(
        connection,
        tenant_id=tenant_id,
        effective_on=effective_on,
    )
    if version is None:
        return None

    common_parameters = {
        "tenant_id": tenant_id,
        "version_id": version["discount_policy_version_id"],
        "parameter_key": parameter_key.upper(),
    }
    if dealer_id is None:
        row = connection.execute(
            text(
                """
                SELECT parameter_id, scope_type, dealer_id, parameter_key,
                       value_number, unit, notes
                FROM auditcore.discount_policy_parameters
                WHERE tenant_id = :tenant_id
                  AND discount_policy_version_id = :version_id
                  AND parameter_key = :parameter_key
                  AND value_type = 'NUMBER'
                  AND scope_type = 'PROJECT'
                ORDER BY parameter_id
                LIMIT 1
                """
            ),
            common_parameters,
        ).mappings().one_or_none()
    else:
        row = connection.execute(
            text(
                """
                SELECT parameter_id, scope_type, dealer_id, parameter_key,
                       value_number, unit, notes
                FROM auditcore.discount_policy_parameters
                WHERE tenant_id = :tenant_id
                  AND discount_policy_version_id = :version_id
                  AND parameter_key = :parameter_key
                  AND value_type = 'NUMBER'
                  AND (
                        scope_type = 'PROJECT'
                        OR (scope_type = 'DEALER' AND dealer_id = :dealer_id)
                  )
                ORDER BY
                    CASE
                        WHEN scope_type = 'DEALER' AND dealer_id = :dealer_id THEN 0
                        ELSE 1
                    END,
                    parameter_id
                LIMIT 1
                """
            ),
            {**common_parameters, "dealer_id": dealer_id},
        ).mappings().one_or_none()
    if row is None:
        return None

    resolved = dict(row)
    resolved["discount_policy_version_id"] = version["discount_policy_version_id"]
    resolved["version_no"] = int(version["version_no"])
    resolved["effective_from"] = version["effective_from"]
    resolved["effective_to"] = version["effective_to"]
    resolved["value_number"] = Decimal(str(resolved["value_number"]))
    return resolved
