"""Add the focused UC03 three-screen Booking details/review support.

Revision ID: 0023_uc03_booking_review_flow
Revises: 0022_uc03_part1_default_profile
Create Date: 2026-08-25
"""
from alembic import op

revision = "0023_uc03_booking_review_flow"
down_revision = "0022_uc03_part1_default_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Screen-2 operational facts only. Extracted product facts remain in the
    # evidence/proposal workflow and Delivery-only attributes are not introduced.
    op.execute(
        """
        ALTER TABLE auditcore.bookings
            ADD COLUMN price_list_id uuid,
            ADD COLUMN outright_purchase boolean,
            ADD COLUMN corporate_id_available boolean,
            ADD COLUMN gst_benefit boolean
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.bookings
        ADD CONSTRAINT fk_bookings_price_list
        FOREIGN KEY (tenant_id, price_list_id)
        REFERENCES auditcore.price_lists(tenant_id, price_list_id)
        """
    )

    # DI already returns source localization. Core persists the same optional
    # metadata so Web/Mobile continues to use Audit Core as the only facade.
    op.execute(
        """
        ALTER TABLE auditcore.evidence_facts
            ADD COLUMN page_no integer,
            ADD COLUMN evidence_region jsonb
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.evidence_facts
        ADD CONSTRAINT ck_evidence_facts_page_no_positive
        CHECK (page_no IS NULL OR page_no > 0)
        """
    )

    # Project-scoped Booking dropdown masters. These are Audit Core reference
    # values, not React constants. Registration State/District/Type/Category are
    # intentionally NOT fabricated; Projects configure those domains explicitly.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.ensure_uc03_booking_reference_masters(
            p_tenant_id varchar
        ) RETURNS void
        LANGUAGE plpgsql
        AS $$
        BEGIN
            INSERT INTO auditcore.business_status_codes (
                tenant_id, domain_key, status_code, status_label,
                is_active, created_by_actor_id
            ) VALUES
                (p_tenant_id, 'CUSTOMER_TYPE', 'INDIVIDUAL', 'Individual', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'CUSTOMER_TYPE', 'CORPORATE', 'Corporate', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'CUSTOMER_TYPE', 'LEASE', 'Lease', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'CUSTOMER_TYPE', 'CSD', 'CSD', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'CUSTOMER_TYPE', 'BUSINESS', 'Business', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'DEAL_TYPE', 'IN_SCOPE', 'In-Scope', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'DEAL_TYPE', 'OUT_OF_SCOPE', 'Out of Scope', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'DEAL_TYPE', 'MANAGEMENT_REFERRAL', 'Management Referral', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'DEAL_TYPE', 'OEM_REFERRAL', 'OEM Referral', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'DEAL_SOURCE', 'WALK_IN', 'Walkin', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'DEAL_SOURCE', 'DIGITAL', 'Digital', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'DEAL_SOURCE', 'INCOMING_CALL', 'Incoming Call', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'DEAL_SOURCE', 'CRM', 'CRM', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'DEAL_SOURCE', 'REFERRAL', 'Referral', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'DEAL_SOURCE', 'FIELD_GENERATION', 'Field Generation', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'LEAD_SOURCE', 'IN_HOUSE', 'In House', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'LEAD_SOURCE', 'DSA', 'DSA', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'LEAD_SOURCE', 'LEASING', 'Leasing', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'TERRITORY_CATEGORIZATION', 'SAME_TERRITORY', 'Same Territory', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'TERRITORY_CATEGORIZATION', 'OUT_OF_TERRITORY', 'Out of Territory', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'TERRITORY_CATEGORIZATION', 'OUT_OF_STATE', 'Out of state', true, 'system.uc03-booking-masters')
            ON CONFLICT (tenant_id, domain_key, status_code)
            DO UPDATE SET
                status_label=EXCLUDED.status_label,
                is_active=true,
                updated_at_utc=now();
        END;
        $$
        """
    )
    op.execute(
        """
        SELECT auditcore.ensure_uc03_booking_reference_masters(tenant_id)
        FROM auditcore.projects
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.seed_uc03_booking_reference_masters_on_project()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            PERFORM auditcore.ensure_uc03_booking_reference_masters(NEW.tenant_id);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_projects_uc03_booking_reference_masters
        ON auditcore.projects
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_projects_uc03_booking_reference_masters
        AFTER INSERT ON auditcore.projects
        FOR EACH ROW
        EXECUTE FUNCTION auditcore.seed_uc03_booking_reference_masters_on_project()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_projects_uc03_booking_reference_masters ON auditcore.projects"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS auditcore.seed_uc03_booking_reference_masters_on_project()"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS auditcore.ensure_uc03_booking_reference_masters(varchar)"
    )
    op.execute(
        "ALTER TABLE auditcore.evidence_facts DROP CONSTRAINT IF EXISTS ck_evidence_facts_page_no_positive"
    )
    op.execute(
        "ALTER TABLE auditcore.evidence_facts DROP COLUMN IF EXISTS evidence_region, DROP COLUMN IF EXISTS page_no"
    )
    op.execute("ALTER TABLE auditcore.bookings DROP CONSTRAINT IF EXISTS fk_bookings_price_list")
    op.execute(
        """
        ALTER TABLE auditcore.bookings
            DROP COLUMN IF EXISTS gst_benefit,
            DROP COLUMN IF EXISTS corporate_id_available,
            DROP COLUMN IF EXISTS outright_purchase,
            DROP COLUMN IF EXISTS price_list_id
        """
    )
    # Master rows are deliberately retained on downgrade if they have become
    # referenced configuration; removing reference data would damage audit history.
