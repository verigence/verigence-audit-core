"""0236_insurance_policy_type -- the insurance dump's Policy Type, kept on each policy
(product owner, 2026-11-17).

"one modification for existing insurance dump upload is to include insurance type as New
only while running the query."

The Mahindra "Policy Details Report" carries a Policy Type column (New for a new vehicle's
first policy; a renewal or a rollover otherwise). The two insurance reports -- OD discount
over the agreed grid, and Self in Verigence while the dealership insured -- are about new
vehicle sales, so they read the New policies only (uc03_p2_insurance_dumps). The column
is filled for the dumps already on file from the row as it was uploaded (raw). Additive;
idempotent.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0236_insurance_policy_type"
down_revision = "0235_background_location"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_insurance_dump_rows ADD COLUMN IF NOT EXISTS policy_type varchar(40)")
    connection = op.get_bind()
    connection.execute(text("SELECT set_config('app.platform_super_admin', 'true', true)"))
    tenants = connection.execute(text("SELECT tenant_id FROM auditcore.projects ORDER BY tenant_id")).scalars().all()
    connection.execute(text("SELECT set_config('app.platform_super_admin', '', true)"))
    for tenant_id in tenants:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_insurance_dump_rows r
                SET policy_type = left(btrim(v.value), 40)
                FROM (SELECT x.tenant_id, x.dump_id, x.row_no,
                             (SELECT e.value FROM jsonb_each_text(x.raw) e
                               WHERE regexp_replace(lower(e.key), '[^a-z0-9]', '', 'g') = 'policytype' LIMIT 1) AS value
                      FROM auditcore.p2_insurance_dump_rows x WHERE x.tenant_id = :t AND x.policy_type IS NULL) v
                WHERE r.tenant_id = v.tenant_id AND r.dump_id = v.dump_id AND r.row_no = v.row_no
                  AND NULLIF(btrim(v.value), '') IS NOT NULL
                """
            ),
            {"t": tenant_id},
        )


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_insurance_dump_rows DROP COLUMN IF EXISTS policy_type")
