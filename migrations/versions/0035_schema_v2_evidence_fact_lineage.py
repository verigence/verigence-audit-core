"""Add role-aware DI extraction lineage to Audit Core evidence facts.

Revision ID: 0035_schema_v2_fact_lineage
Revises: 0034_uc03_hot_indexes
Create Date: 2026-08-29

The fields are snapshots of the exact DI fact/current-value lineage returned at
refresh time.  They are intentionally nullable for historical rows created before
Schema V2; new refreshes populate them when the DI contract supplies them.
"""
from alembic import op

revision = "0035_schema_v2_fact_lineage"
down_revision = "0034_uc03_hot_indexes"
branch_labels = None
depends_on = None

_ALLOWED_ROLES_SQL = """
'UNSPECIFIED',
'SUBJECT_VEHICLE',
'EXCHANGE_VEHICLE',
'SUBJECT_TRANSACTION',
'CUSTOMER',
'PAYER',
'TRANSFEROR',
'TRANSFEREE',
'ORGANISATION'
"""


def upgrade() -> None:
    op.execute("""
        ALTER TABLE auditcore.evidence_facts
        ADD COLUMN IF NOT EXISTS fact_role varchar(40)
            NOT NULL DEFAULT 'UNSPECIFIED'
    """)
    op.execute("""
        ALTER TABLE auditcore.evidence_facts
        ADD COLUMN IF NOT EXISTS di_value_version_no integer
    """)
    op.execute("""
        ALTER TABLE auditcore.evidence_facts
        ADD COLUMN IF NOT EXISTS di_extracted_fact_id uuid
    """)
    op.execute("""
        ALTER TABLE auditcore.evidence_facts
        ADD COLUMN IF NOT EXISTS di_processing_run_id uuid
    """)
    op.execute("""
        ALTER TABLE auditcore.evidence_facts
        ADD COLUMN IF NOT EXISTS di_extraction_profile_id uuid
    """)
    op.execute("""
        ALTER TABLE auditcore.evidence_facts
        ADD COLUMN IF NOT EXISTS di_extraction_profile_version integer
    """)
    op.execute("""
        ALTER TABLE auditcore.evidence_facts
        ADD COLUMN IF NOT EXISTS di_invocation_id uuid
    """)
    op.execute("""
        ALTER TABLE auditcore.evidence_facts
        ADD COLUMN IF NOT EXISTS di_pipeline_version varchar(40)
    """)

    op.execute(f"""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_evidence_facts_fact_role'
                  AND conrelid = 'auditcore.evidence_facts'::regclass
            ) THEN
                ALTER TABLE auditcore.evidence_facts
                ADD CONSTRAINT ck_evidence_facts_fact_role
                CHECK (fact_role IN ({_ALLOWED_ROLES_SQL}));
            END IF;
        END $$
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_evidence_facts_role_current
        ON auditcore.evidence_facts(
            tenant_id, journey_id, field_key, fact_role, fetched_at_utc DESC
        )
        WHERE superseded_at_utc IS NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_evidence_facts_di_lineage
        ON auditcore.evidence_facts(
            tenant_id, di_processing_run_id, di_extracted_fact_id
        )
        WHERE di_processing_run_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.ix_evidence_facts_di_lineage")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_evidence_facts_role_current")
    op.execute("""
        ALTER TABLE auditcore.evidence_facts
        DROP CONSTRAINT IF EXISTS ck_evidence_facts_fact_role
    """)
    for column in (
        "di_pipeline_version",
        "di_invocation_id",
        "di_extraction_profile_version",
        "di_extraction_profile_id",
        "di_processing_run_id",
        "di_extracted_fact_id",
        "di_value_version_no",
        "fact_role",
    ):
        op.execute(f"ALTER TABLE auditcore.evidence_facts DROP COLUMN IF EXISTS {column}")
