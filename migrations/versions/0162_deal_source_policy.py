"""0162_deal_source_policy -- which document a Project's Deal takes its billed
figures from, and Mahindra Orissa on the account statement (product owner,
2026-10-25).

Every Project keeps the default ``INVOICE``. Mahindra Orissa is set to
``ACCOUNT_STATEMENT``: the dealer's account statement (stored as customer_ledger)
is the source of truth, the invoices are withheld from the Deal, and a Journey
without a statement shows no billed figures.

For a Project set to ACCOUNT_STATEMENT this migration also clears what the
invoices had already put in the Deal -- the per-invoice source values, the
canonical actuals an invoice set, and the discounts an invoice set -- after
copying every one of those rows into ``auditcore.deal_source_snapshots``, so
the change can be read back and reversed. The invoice documents and everything
read from them are NOT touched: they stay on file as evidence. Entitlements
(standards) are NOT touched either.

The rebuild from the statements is done by the worker (AUDIT_CORE_ACCOUNT_
STATEMENT_TAG), dry run first.

Additive and idempotent: run again, the policy is the same and there is
nothing left to snapshot or clear.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0162_deal_source_policy"
down_revision = "0161_discount_variance_restamp"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_SNAPSHOTS = "deal_source_snapshots"
_MAHINDRA_ORISSA = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
_TAG = "0162_deal_source_policy"
# Mirrors uc03_deal_source_policy.INVOICE_SOURCE_TYPES (a migration must not
# import application code that can change after it ran).
_INVOICE_TYPES = [
    "tax_invoice_tally", "customer_invoice_dms", "customer_invoice_dms_v2", "invoice_generic", "tax_invoice",
    "tax_invoice_dms", "wholesale_invoice", "accessory_invoice_dms", "accessory_invoice_tally", "ew_invoice",
    "rsa_invoice", "credit_note", "debit_note",
]
_NORMALISED = "trim(both '_' from regexp_replace(lower({col}), '[^a-z0-9]+', '_', 'g'))"


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.projects ADD COLUMN IF NOT EXISTS deal_source_policy "
               "varchar(30) NOT NULL DEFAULT 'INVOICE'")
    op.execute("ALTER TABLE auditcore.projects DROP CONSTRAINT IF EXISTS ck_projects_deal_source_policy")
    op.execute("ALTER TABLE auditcore.projects ADD CONSTRAINT ck_projects_deal_source_policy "
               "CHECK (deal_source_policy IN ('INVOICE', 'ACCOUNT_STATEMENT'))")

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_SNAPSHOTS} (
            snapshot_id   bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tag           varchar(80)  NOT NULL,
            tenant_id     varchar(128) NOT NULL,
            journey_id    uuid,
            table_name    varchar(80)  NOT NULL,
            row_data      jsonb        NOT NULL,
            taken_at_utc  timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_SNAPSHOTS}_tag ON auditcore.{_SNAPSHOTS} (tenant_id, tag, table_name)")
    op.execute(f"ALTER TABLE auditcore.{_SNAPSHOTS} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{_SNAPSHOTS} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_SNAPSHOTS} ON auditcore.{_SNAPSHOTS}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_SNAPSHOTS} ON auditcore.{_SNAPSHOTS} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT ON auditcore.{_SNAPSHOTS} TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE ON auditcore.{_SNAPSHOTS} FROM {_RUNTIME_ROLE}")

    connection = op.get_bind()
    connection.execute(
        text("UPDATE auditcore.projects SET deal_source_policy = 'ACCOUNT_STATEMENT' WHERE tenant_id = :t"),
        {"t": _MAHINDRA_ORISSA},
    )
    tenants = connection.execute(
        text("SELECT tenant_id FROM auditcore.projects WHERE deal_source_policy = 'ACCOUNT_STATEMENT'")
    ).scalars().all()
    for tenant_id in tenants:
        _clear_invoice_values(connection, tenant_id)


def _clear_invoice_values(connection, tenant_id: str) -> None:
    # Row level security applies to the migration role too (FORCE): act as the tenant.
    connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
    params = {"t": tenant_id, "types": _INVOICE_TYPES, "tag": _TAG}
    source_type = _NORMALISED.format(col="source_document_type")
    reference_type = _NORMALISED.format(col="split_part(source_reference, ':', 1)")

    # 1. keep a copy of every row that is about to change
    connection.execute(
        text(
            f"""
            INSERT INTO auditcore.{_SNAPSHOTS} (tag, tenant_id, journey_id, table_name, row_data)
            SELECT :tag, tenant_id, journey_id, 'commercial_line_source_values', to_jsonb(s)
            FROM auditcore.commercial_line_source_values s
            WHERE tenant_id = :t AND {source_type} = ANY(:types)
            """
        ),
        params,
    )
    connection.execute(
        text(
            f"""
            INSERT INTO auditcore.{_SNAPSHOTS} (tag, tenant_id, journey_id, table_name, row_data)
            SELECT :tag, tenant_id, journey_id, 'commercial_lines', to_jsonb(l)
            FROM auditcore.commercial_lines l
            WHERE tenant_id = :t AND source_reference IS NOT NULL AND {reference_type} = ANY(:types)
            """
        ),
        params,
    )
    connection.execute(
        text(
            f"""
            INSERT INTO auditcore.{_SNAPSHOTS} (tag, tenant_id, journey_id, table_name, row_data)
            SELECT :tag, tenant_id, journey_id, 'discount_applications', to_jsonb(d)
            FROM auditcore.discount_applications d
            WHERE tenant_id = :t AND details->>'origin' IN ('INVOICE_MATERIALIZATION', 'INVOICE_WITHDRAWN')
              AND actual_discount_amount IS NOT NULL
            """
        ),
        params,
    )

    # 2. clear what the invoices put in the Deal (documents and reads stay)
    connection.execute(
        text(
            f"DELETE FROM auditcore.commercial_line_source_values "
            f"WHERE tenant_id = :t AND {source_type} = ANY(:types)"
        ),
        params,
    )
    connection.execute(
        text(
            f"""
            UPDATE auditcore.commercial_lines
            SET actual_amount = NULL, actual_source_kind = NULL, source_evidence_id = NULL,
                source_reference = NULL, updated_at_utc = now()
            WHERE tenant_id = :t AND source_reference IS NOT NULL AND {reference_type} = ANY(:types)
            """
        ),
        params,
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.discount_applications
            SET actual_discount_amount = NULL, actual_source_kind = NULL, source_evidence_id = NULL,
                updated_at_utc = now()
            WHERE tenant_id = :t AND details->>'origin' IN ('INVOICE_MATERIALIZATION', 'INVOICE_WITHDRAWN')
              AND actual_discount_amount IS NOT NULL
            """
        ),
        params,
    )
    # a discount row an invoice created and nothing else claims is now empty: gone
    connection.execute(
        text(
            """
            DELETE FROM auditcore.discount_applications
            WHERE tenant_id = :t AND details->>'origin' IN ('INVOICE_MATERIALIZATION', 'INVOICE_WITHDRAWN')
              AND actual_discount_amount IS NULL AND standard_eligible_amount IS NULL
              AND discount_scheme_version_id IS NULL
            """
        ),
        params,
    )
    # the cached Deal variance is stale: hand the Journeys back to the sweep
    connection.execute(
        text("UPDATE auditcore.p2_journey_runtime SET price_variance_fact_version = NULL WHERE tenant_id = :t"),
        params,
    )


def downgrade() -> None:
    # The snapshots stay: they are the only copy of what was cleared.
    op.execute("ALTER TABLE auditcore.projects DROP CONSTRAINT IF EXISTS ck_projects_deal_source_policy")
    op.execute("ALTER TABLE auditcore.projects DROP COLUMN IF EXISTS deal_source_policy")
