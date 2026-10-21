"""0154_corporate_registry_entries — the corporate registry, dated and per
tenant (2026-10-20 product owner rules, R5; the review of PR 472, S5).

``corporate_privilege_registry`` holds one OEM-wide list that every corporate
policy upload replaces: the October list (2,687 companies) re-categorises a
September deal reconciled later, and one tenant's upload replaces another's.
This adds ``corporate_registry_entries``: every upload's companies, kept
under the tenant and the upload's effective date. A deal reads the list of
the newest upload in force on its pricing date. The old table stays as it is
(the Masters page still reads it); nothing is dropped.

Backfilled from the old table for the uploads it came from (tenant and date
from ``oem_master_uploads``, read one tenant at a time under its row
security). Additive; idempotent.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0154_corporate_registry_entries"
down_revision = "0153_source_values_per_document"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLE = "corporate_registry_entries"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_TABLE} (
            tenant_id            varchar(128) NOT NULL REFERENCES auditcore.projects(tenant_id),
            oem_code             varchar(80)  NOT NULL,
            source_upload_id     uuid         NOT NULL,
            effective_from       date         NOT NULL,
            corporate_code       varchar(60)  NOT NULL,
            corporate_name       varchar(400) NOT NULL,
            corporate_type       varchar(200),
            privilege_category   varchar(20)  NOT NULL
                                 CHECK (privilege_category IN ('Z','Y','F','A','B')),
            created_at_utc       timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, oem_code, source_upload_id, corporate_code)
        )
        """
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_in_force "
        f"ON auditcore.{_TABLE} (tenant_id, oem_code, effective_from DESC, created_at_utc DESC)"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_name "
        f"ON auditcore.{_TABLE} (tenant_id, source_upload_id, lower(corporate_name))"
    )
    # Backfill, one tenant at a time: oem_master_uploads is under forced row
    # security, so its rows are read with that tenant set (as 0151 does).
    bind = op.get_bind()
    bind.execute(text("SELECT set_config('app.platform_super_admin', 'true', true)"))
    tenants = [r[0] for r in bind.execute(text("SELECT tenant_id FROM auditcore.projects ORDER BY tenant_id")).all()]
    for tenant in tenants:
        bind.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
        bind.execute(
            text(
                f"""
                INSERT INTO auditcore.{_TABLE} (
                    tenant_id, oem_code, source_upload_id, effective_from,
                    corporate_code, corporate_name, corporate_type, privilege_category, created_at_utc
                )
                SELECT u.tenant_id, r.oem_code, r.source_upload_id, COALESCE(u.effective_from, r.effective_from),
                       r.corporate_code, r.corporate_name, r.corporate_type, r.privilege_category, r.created_at_utc
                FROM auditcore.corporate_privilege_registry r
                JOIN auditcore.oem_master_uploads u ON u.upload_id = r.source_upload_id AND u.tenant_id = :t
                WHERE COALESCE(u.effective_from, r.effective_from) IS NOT NULL
                ON CONFLICT DO NOTHING
                """
            ),
            {"t": tenant},
        )
    bind.execute(text("SELECT set_config('app.tenant_id', '', true)"))
    op.execute(f"ALTER TABLE auditcore.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON auditcore.{_TABLE}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_TABLE} ON auditcore.{_TABLE} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT ON auditcore.{_TABLE} TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE ON auditcore.{_TABLE} FROM {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS auditcore.{_TABLE}")
