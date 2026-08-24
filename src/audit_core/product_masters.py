from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.errors import AuditCoreError
from audit_core.product_catalogue import resolve_sellable_configuration


def ensure_project_product_master(
    connection: Connection,
    *,
    tenant_id: str,
    segment_id: UUID | None = None,
) -> UUID:
    existing = connection.execute(
        text(
            """
            SELECT product_master_id
            FROM auditcore.project_product_masters
            WHERE tenant_id = :tenant_id
              AND status = 'ACTIVE'
              AND segment_id IS NOT DISTINCT FROM :segment_id
            ORDER BY created_at_utc, product_master_id
            """
        ),
        {"tenant_id": tenant_id, "segment_id": segment_id},
    ).scalars().all()
    if len(existing) > 1:
        raise AuditCoreError(
            error_code="VAC-MASTER-003",
            status_code=409,
            title="Master configuration conflict",
            detail="Project has more than one active Product Master identity for the Segment.",
        )
    if existing:
        return existing[0]

    return connection.execute(
        text(
            """
            INSERT INTO auditcore.project_product_masters (tenant_id, segment_id)
            VALUES (:tenant_id, :segment_id)
            RETURNING product_master_id
            """
        ),
        {"tenant_id": tenant_id, "segment_id": segment_id},
    ).scalar_one()


def create_project_product_master_version(
    connection: Connection,
    *,
    tenant_id: str,
    effective_from: date,
    actor_id: str,
    source_import_id: UUID | None = None,
    segment_id: UUID | None = None,
) -> UUID:
    product_master_id = ensure_project_product_master(
        connection,
        tenant_id=tenant_id,
        segment_id=segment_id,
    )
    version_no = connection.execute(
        text(
            """
            SELECT COALESCE(MAX(version_no), 0) + 1
            FROM auditcore.project_product_master_versions
            WHERE tenant_id = :tenant_id
              AND product_master_id = :product_master_id
            """
        ),
        {"tenant_id": tenant_id, "product_master_id": product_master_id},
    ).scalar_one()
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.project_product_master_versions (
                tenant_id, product_master_id, version_no, effective_from,
                source_import_id, created_by_user_id
            ) VALUES (
                :tenant_id, :product_master_id, :version_no, :effective_from,
                :source_import_id, :actor_id
            )
            RETURNING version_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "product_master_id": product_master_id,
            "version_no": version_no,
            "effective_from": effective_from,
            "source_import_id": source_import_id,
            "actor_id": actor_id,
        },
    ).scalar_one()


def add_project_product_master_item(
    connection: Connection,
    *,
    tenant_id: str,
    version_id: UUID,
    product_sku_id: UUID,
    source_import_row_no: int | None = None,
) -> UUID:
    version = connection.execute(
        text(
            """
            SELECT product_master_id, lifecycle_status
            FROM auditcore.project_product_master_versions
            WHERE tenant_id = :tenant_id AND version_id = :version_id
            """
        ),
        {"tenant_id": tenant_id, "version_id": version_id},
    ).mappings().one_or_none()
    if version is None:
        raise AuditCoreError(
            error_code="VAC-MASTER-002",
            status_code=422,
            title="Master version unavailable",
            detail="Product Master version does not exist for this Project.",
        )
    if version["lifecycle_status"] != "DRAFT":
        raise AuditCoreError(
            error_code="VAC-MASTER-003",
            status_code=409,
            title="Master lifecycle conflict",
            detail="Product Master items may only be changed while the version is DRAFT.",
        )

    canonical_snapshot = resolve_sellable_configuration(
        connection,
        product_sku_id=product_sku_id,
    )
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.project_product_master_items (
                tenant_id, product_master_id, version_id, product_sku_id,
                approved_product_snapshot, source_import_row_no
            ) VALUES (
                :tenant_id, :product_master_id, :version_id, :product_sku_id,
                CAST(:snapshot AS jsonb), :source_import_row_no
            )
            RETURNING item_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "product_master_id": version["product_master_id"],
            "version_id": version_id,
            "product_sku_id": product_sku_id,
            "snapshot": _snapshot_json(canonical_snapshot),
            "source_import_row_no": source_import_row_no,
        },
    ).scalar_one()


def publish_project_product_master_version(
    connection: Connection,
    *,
    tenant_id: str,
    version_id: UUID,
    actor_id: str,
) -> None:
    item_count = connection.execute(
        text(
            """
            SELECT count(*)
            FROM auditcore.project_product_master_items
            WHERE tenant_id = :tenant_id AND version_id = :version_id
            """
        ),
        {"tenant_id": tenant_id, "version_id": version_id},
    ).scalar_one()
    if item_count == 0:
        raise AuditCoreError(
            error_code="VAC-MASTER-002",
            status_code=422,
            title="Master version incomplete",
            detail="Product Master version must contain at least one Product/SKU before publish.",
        )

    result = connection.execute(
        text(
            """
            UPDATE auditcore.project_product_master_versions
            SET lifecycle_status = 'PUBLISHED',
                published_by_user_id = :actor_id,
                published_at_utc = now()
            WHERE tenant_id = :tenant_id
              AND version_id = :version_id
              AND lifecycle_status = 'DRAFT'
            """
        ),
        {"tenant_id": tenant_id, "version_id": version_id, "actor_id": actor_id},
    )
    if result.rowcount != 1:
        raise AuditCoreError(
            error_code="VAC-MASTER-003",
            status_code=409,
            title="Master lifecycle conflict",
            detail="Product Master version cannot be published from its current state.",
        )


def retire_project_product_master_version(
    connection: Connection,
    *,
    tenant_id: str,
    version_id: UUID,
    actor_id: str,
) -> None:
    result = connection.execute(
        text(
            """
            UPDATE auditcore.project_product_master_versions
            SET lifecycle_status = 'RETIRED',
                retired_by_user_id = :actor_id,
                retired_at_utc = now()
            WHERE tenant_id = :tenant_id
              AND version_id = :version_id
              AND lifecycle_status = 'PUBLISHED'
            """
        ),
        {"tenant_id": tenant_id, "version_id": version_id, "actor_id": actor_id},
    )
    if result.rowcount != 1:
        raise AuditCoreError(
            error_code="VAC-MASTER-003",
            status_code=409,
            title="Master lifecycle conflict",
            detail="Product Master version cannot be retired from its current state.",
        )


def resolve_effective_project_product_master_version(
    connection: Connection,
    *,
    tenant_id: str,
    effective_on: date,
    segment_id: UUID | None = None,
) -> UUID:
    rows = connection.execute(
        text(
            """
            SELECT v.version_id, v.effective_from
            FROM auditcore.project_product_master_versions v
            JOIN auditcore.project_product_masters m
              ON m.tenant_id = v.tenant_id
             AND m.product_master_id = v.product_master_id
            WHERE v.tenant_id = :tenant_id
              AND m.segment_id IS NOT DISTINCT FROM :segment_id
              AND v.lifecycle_status = 'PUBLISHED'
              AND v.effective_from <= :effective_on
            ORDER BY v.effective_from DESC, v.version_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "effective_on": effective_on,
            "segment_id": segment_id,
        },
    ).mappings().all()
    if not rows:
        raise AuditCoreError(
            error_code="VAC-MASTER-002",
            status_code=422,
            title="No effective master version",
            detail="No published Product Master version exists for the requested date and Segment.",
        )

    latest_wef = rows[0]["effective_from"]
    winners = [row for row in rows if row["effective_from"] == latest_wef]
    if len(winners) != 1:
        raise AuditCoreError(
            error_code="VAC-MASTER-003",
            status_code=409,
            title="Master configuration conflict",
            detail=(
                "Multiple published Product Master versions share the latest applicable WEF "
                "for the Segment; an effective version cannot be selected deterministically."
            ),
        )
    return winners[0]["version_id"]


def _sku_segment_id(connection: Connection, product_sku_id: UUID) -> UUID | None:
    return connection.execute(
        text(
            """
            SELECT m.segment_id
            FROM auditcore.product_skus s
            JOIN auditcore.product_models m ON m.model_id = s.model_id
            WHERE s.product_sku_id=:product_sku_id
            """
        ),
        {"product_sku_id": product_sku_id},
    ).scalar_one_or_none()


def product_sku_is_in_effective_master(
    connection: Connection,
    *,
    tenant_id: str,
    product_sku_id: UUID,
    effective_on: date,
) -> bool:
    segment_id = _sku_segment_id(connection, product_sku_id)
    version_id = resolve_effective_project_product_master_version(
        connection,
        tenant_id=tenant_id,
        effective_on=effective_on,
        segment_id=segment_id,
    )
    return bool(
        connection.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM auditcore.project_product_master_items
                    WHERE tenant_id = :tenant_id
                      AND version_id = :version_id
                      AND product_sku_id = :product_sku_id
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "version_id": version_id,
                "product_sku_id": product_sku_id,
            },
        ).scalar_one()
    )


def _snapshot_json(snapshot: dict[str, object]) -> str:
    import json

    return json.dumps(snapshot, default=str, sort_keys=True, separators=(",", ":"))
