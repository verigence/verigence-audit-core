"""0224_pinned_model_names -- a pinned SKU's model and variant names back on the journey (2026-11-14).

Product owner 2026-11-14: "how can model be read differently.. its deterministic thing right..
we are not using AI.. its all SKU". Once a SKU is pinned the journey's model and variant names
are the SKU master's (uc03_model_resolution). The post-extraction materialisation then wrote the
text read off a document back over them ("Scorpion N" over "NEW SCORPIO N", "BOL MAXX PUP HD 1.7
VXI" over "MAXX HD"); it no longer does (uc03_post_extraction_materialization). This puts the
master's names back on every journey whose pinned SKU's names differ. The pin itself, the colour
and every other column are untouched. Row security applies to the migration role too (FORCE):
the Projects are listed as the platform SuperAdmin and each is repaired as itself.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0224_pinned_model_names"
down_revision = "0223_dms_dumps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SELECT set_config('app.platform_super_admin', 'true', true)"))
    tenants = connection.execute(text("SELECT tenant_id FROM auditcore.projects ORDER BY tenant_id")).scalars().all()
    connection.execute(text("SELECT set_config('app.platform_super_admin', '', true)"))
    for tenant_id in tenants:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_products jp
                SET model_code_snapshot = pm.model_code, model_name_snapshot = pm.model_name,
                    variant_code_snapshot = pv.variant_code, variant_name_snapshot = pv.variant_name,
                    updated_at_utc = now()
                FROM auditcore.product_skus s
                JOIN auditcore.product_models pm ON pm.model_id = s.model_id
                JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
                WHERE jp.tenant_id = :t AND jp.product_sku_id = s.product_sku_id
                  AND (jp.model_name_snapshot IS DISTINCT FROM pm.model_name
                       OR jp.variant_name_snapshot IS DISTINCT FROM pv.variant_name)
                """
            ),
            {"t": tenant_id},
        )
    connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))


def downgrade() -> None:
    # The document text that was written over the names is not kept; nothing to restore.
    pass
