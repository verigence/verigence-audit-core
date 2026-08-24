from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.errors import AuditCoreError


def create_oem(connection: Connection, *, code: str, name: str) -> UUID:
    return connection.execute(
        text(
            "INSERT INTO auditcore.oems (oem_code, oem_name) "
            "VALUES (:code, :name) RETURNING oem_id"
        ),
        {"code": code, "name": name},
    ).scalar_one()


def create_model(
    connection: Connection,
    *,
    oem_id: UUID,
    code: str,
    name: str,
) -> UUID:
    return connection.execute(
        text(
            "INSERT INTO auditcore.product_models (oem_id, model_code, model_name) "
            "VALUES (:oem_id, :code, :name) RETURNING model_id"
        ),
        {"oem_id": oem_id, "code": code, "name": name},
    ).scalar_one()


def create_variant(
    connection: Connection,
    *,
    model_id: UUID,
    code: str,
    name: str,
) -> UUID:
    return connection.execute(
        text(
            "INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name) "
            "VALUES (:model_id, :code, :name) RETURNING variant_id"
        ),
        {"model_id": model_id, "code": code, "name": name},
    ).scalar_one()


def create_colour(
    connection: Connection,
    *,
    oem_id: UUID,
    code: str,
    name: str,
) -> UUID:
    return connection.execute(
        text(
            "INSERT INTO auditcore.colours (oem_id, colour_code, colour_name) "
            "VALUES (:oem_id, :code, :name) RETURNING colour_id"
        ),
        {"oem_id": oem_id, "code": code, "name": name},
    ).scalar_one()


def create_sku(
    connection: Connection,
    *,
    oem_id: UUID,
    model_id: UUID,
    variant_id: UUID,
    colour_id: UUID | None,
    sku_code: str,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.product_skus (
                oem_id, model_id, variant_id, colour_id, sku_code
            ) VALUES (
                :oem_id, :model_id, :variant_id, :colour_id, :sku_code
            ) RETURNING product_sku_id
            """
        ),
        {
            "oem_id": oem_id,
            "model_id": model_id,
            "variant_id": variant_id,
            "colour_id": colour_id,
            "sku_code": sku_code,
        },
    ).scalar_one()


def resolve_sellable_configuration(
    connection: Connection,
    *,
    product_sku_id: UUID,
) -> dict[str, object]:
    row = connection.execute(
        text(
            """
            SELECT s.product_sku_id, s.sku_code,
                   o.oem_id, o.oem_code, o.oem_name,
                   seg.segment_id, seg.segment_code, seg.segment_name,
                   m.model_id, m.model_code, m.model_name, m.model_year,
                   v.variant_id, v.variant_code, v.variant_name,
                   pc.configuration_id, pc.configuration_code,
                   pc.fuel_powertrain AS configuration_fuel_powertrain,
                   pc.transmission AS configuration_transmission,
                   pc.drive_type, pc.seating_capacity,
                   pc.body_type AS configuration_body_type,
                   pc.attributes AS configuration_attributes,
                   c.colour_id, c.colour_code, c.colour_name
            FROM auditcore.product_skus s
            JOIN auditcore.oems o ON o.oem_id = s.oem_id
            JOIN auditcore.product_models m ON m.model_id = s.model_id
            JOIN auditcore.product_variants v ON v.variant_id = s.variant_id
            LEFT JOIN auditcore.oem_segments seg ON seg.segment_id = m.segment_id
            LEFT JOIN auditcore.product_configurations pc
              ON pc.configuration_id = s.configuration_id
            LEFT JOIN auditcore.colours c ON c.colour_id = s.colour_id
            WHERE s.product_sku_id = :product_sku_id
              AND s.is_active AND o.is_active AND m.is_active AND v.is_active
              AND (seg.segment_id IS NULL OR seg.is_active)
              AND (pc.configuration_id IS NULL OR pc.is_active)
              AND (c.colour_id IS NULL OR c.is_active)
            """
        ),
        {"product_sku_id": product_sku_id},
    ).mappings().one_or_none()
    if row is None:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Product configuration unavailable",
            detail="The requested sellable product configuration is not active.",
        )
    return dict(row)
