"""Seed system Trade-In status codes required by UC03 Booking capture.

Revision ID: 0029_uc03_trade_in_status
Revises: 0028_uc03_pc_verification
Create Date: 2026-08-26
"""
from alembic import op

revision = "0029_uc03_trade_in_status"
down_revision = "0028_uc03_pc_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The UC03 Booking capture contract uses these two system-level Trade-In
    # states. They are not Project-configurable business choices, so Audit Core
    # must guarantee they exist before writing trade_in_cases.actual_status_code.
    op.execute(
        """
        INSERT INTO auditcore.business_status_codes (
            tenant_id,
            domain_key,
            status_code,
            status_label,
            description,
            is_active
        )
        SELECT
            p.tenant_id,
            'TRADE_IN',
            defaults.status_code,
            defaults.status_label,
            defaults.description,
            true
        FROM auditcore.projects p
        CROSS JOIN (
            VALUES
                ('EXCHANGE_TAKEN', 'Exchange Taken', 'Customer vehicle is being taken in exchange.'),
                ('NO_EXCHANGE', 'No Exchange', 'No customer vehicle is being taken in exchange.')
        ) AS defaults(status_code, status_label, description)
        ON CONFLICT (tenant_id, domain_key, status_code) DO NOTHING
        """
    )

    # Keep the invariant true for Projects created after this migration.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.ensure_trade_in_status_defaults()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = auditcore, pg_temp
        AS $$
        BEGIN
            INSERT INTO auditcore.business_status_codes (
                tenant_id,
                domain_key,
                status_code,
                status_label,
                description,
                is_active
            ) VALUES
                (
                    NEW.tenant_id,
                    'TRADE_IN',
                    'EXCHANGE_TAKEN',
                    'Exchange Taken',
                    'Customer vehicle is being taken in exchange.',
                    true
                ),
                (
                    NEW.tenant_id,
                    'TRADE_IN',
                    'NO_EXCHANGE',
                    'No Exchange',
                    'No customer vehicle is being taken in exchange.',
                    true
                )
            ON CONFLICT (tenant_id, domain_key, status_code) DO NOTHING;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_projects_trade_in_status_defaults
        ON auditcore.projects
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_projects_trade_in_status_defaults
        AFTER INSERT ON auditcore.projects
        FOR EACH ROW EXECUTE FUNCTION auditcore.ensure_trade_in_status_defaults()
        """
    )


def downgrade() -> None:
    # Preserve the seeded rows because existing trade_in_cases may reference
    # them. Downgrade only removes automatic synchronization for future Projects.
    op.execute(
        "DROP TRIGGER IF EXISTS trg_projects_trade_in_status_defaults ON auditcore.projects"
    )
    op.execute("DROP FUNCTION IF EXISTS auditcore.ensure_trade_in_status_defaults()")
