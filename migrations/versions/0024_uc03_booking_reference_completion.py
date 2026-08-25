"""Complete UC03 Booking Screen-2 reference masters.

Revision ID: 0024_uc03_booking_reference_completion
Revises: 0023_uc03_booking_review_flow
Create Date: 2026-08-25

0023 intentionally left Registration State / District / Registration Type /
Registration Category empty. The Web contract disables a select when its
Project master has no effective options, which made Booking Screen 2
impossible to complete. This migration keeps those values in Audit Core,
seeds finite India-wide registration values once per Project, and derives a
small useful District list from configured Outlet cities instead of copying a
huge national district catalogue into every Project.
"""
from alembic import op

revision = "0024_uc03_booking_reference_completion"
down_revision = "0023_uc03_booking_review_flow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Replace the 0023 helper so Projects created after this release receive the
    # complete Booking reference set as well. Existing Project custom values are
    # preserved because every row is upserted by (tenant, domain, code).
    op.execute(
        r"""
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
                -- Type of Customer
                (p_tenant_id, 'CUSTOMER_TYPE', 'INDIVIDUAL', 'Individual', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'CUSTOMER_TYPE', 'CORPORATE', 'Corporate', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'CUSTOMER_TYPE', 'LEASE', 'Lease', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'CUSTOMER_TYPE', 'CSD', 'CSD', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'CUSTOMER_TYPE', 'BUSINESS', 'Business', true, 'system.uc03-booking-masters'),

                -- Type of Deal
                (p_tenant_id, 'DEAL_TYPE', 'IN_SCOPE', 'In-Scope', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'DEAL_TYPE', 'OUT_OF_SCOPE', 'Out of Scope', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'DEAL_TYPE', 'MANAGEMENT_REFERRAL', 'Management Referral', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'DEAL_TYPE', 'OEM_REFERRAL', 'OEM Referral', true, 'system.uc03-booking-masters'),

                -- Deal Source
                (p_tenant_id, 'DEAL_SOURCE', 'WALK_IN', 'Walk-in', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'DEAL_SOURCE', 'DIGITAL', 'Digital', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'DEAL_SOURCE', 'INCOMING_CALL', 'Incoming Call', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'DEAL_SOURCE', 'CRM', 'CRM', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'DEAL_SOURCE', 'REFERRAL', 'Referral', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'DEAL_SOURCE', 'FIELD_GENERATION', 'Field Generation', true, 'system.uc03-booking-masters'),

                -- Lead Generated Through
                (p_tenant_id, 'LEAD_SOURCE', 'IN_HOUSE', 'In House', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'LEAD_SOURCE', 'DSA', 'DSA', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'LEAD_SOURCE', 'LEASING', 'Leasing', true, 'system.uc03-booking-masters'),

                -- Territory
                (p_tenant_id, 'TERRITORY_CATEGORIZATION', 'SAME_TERRITORY', 'Same Territory', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'TERRITORY_CATEGORIZATION', 'OUT_OF_TERRITORY', 'Out of Territory', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'TERRITORY_CATEGORIZATION', 'OUT_OF_STATE', 'Out of State', true, 'system.uc03-booking-masters'),

                -- Registration Type. This is about registration lifecycle/type,
                -- not Customer Type (which is already captured separately).
                (p_tenant_id, 'REGISTRATION_TYPE', 'PERMANENT', 'Permanent', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_TYPE', 'TEMPORARY', 'Temporary', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_TYPE', 'BH_SERIES', 'BH Series', true, 'system.uc03-booking-masters'),

                -- Registration Category.
                (p_tenant_id, 'REGISTRATION_CATEGORY', 'PRIVATE', 'Private', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_CATEGORY', 'COMMERCIAL', 'Commercial', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_CATEGORY', 'GOVERNMENT', 'Government', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_CATEGORY', 'DIPLOMATIC', 'Diplomatic', true, 'system.uc03-booking-masters'),

                -- Registration State / UT. Codes follow the familiar Indian
                -- vehicle/administrative abbreviations used in registration.
                (p_tenant_id, 'REGISTRATION_STATE', 'AN', 'Andaman and Nicobar Islands', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'AP', 'Andhra Pradesh', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'AR', 'Arunachal Pradesh', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'AS', 'Assam', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'BR', 'Bihar', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'CH', 'Chandigarh', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'CG', 'Chhattisgarh', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'DN', 'Dadra and Nagar Haveli and Daman and Diu', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'DL', 'Delhi', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'GA', 'Goa', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'GJ', 'Gujarat', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'HR', 'Haryana', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'HP', 'Himachal Pradesh', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'JK', 'Jammu and Kashmir', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'JH', 'Jharkhand', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'KA', 'Karnataka', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'KL', 'Kerala', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'LA', 'Ladakh', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'LD', 'Lakshadweep', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'MP', 'Madhya Pradesh', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'MH', 'Maharashtra', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'MN', 'Manipur', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'ML', 'Meghalaya', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'MZ', 'Mizoram', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'NL', 'Nagaland', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'OD', 'Odisha', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'PY', 'Puducherry', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'PB', 'Punjab', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'RJ', 'Rajasthan', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'SK', 'Sikkim', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'TN', 'Tamil Nadu', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'TS', 'Telangana', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'TR', 'Tripura', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'UP', 'Uttar Pradesh', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'UK', 'Uttarakhand', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'WB', 'West Bengal', true, 'system.uc03-booking-masters'),
                (p_tenant_id, 'REGISTRATION_STATE', 'OTHER', 'Other / Not Listed', true, 'system.uc03-booking-masters'),

                -- District is deliberately not a 700+ row Project copy. Local
                -- Outlet cities are added below and OTHER remains available for
                -- registrations outside configured locations.
                (p_tenant_id, 'DISTRICT', 'OTHER', 'Other / Not Listed', true, 'system.uc03-booking-masters')
            ON CONFLICT (tenant_id, domain_key, status_code)
            DO UPDATE SET
                status_label=EXCLUDED.status_label,
                is_active=true,
                updated_at_utc=now();

            INSERT INTO auditcore.business_status_codes (
                tenant_id, domain_key, status_code, status_label,
                description, is_active, created_by_actor_id
            )
            SELECT
                p_tenant_id,
                'DISTRICT',
                LEFT(REGEXP_REPLACE(UPPER(BTRIM(o.city)), '[^A-Z0-9]+', '_', 'g'), 100),
                BTRIM(o.city),
                'Derived from a configured Dealer Outlet city; Project admins may add additional registration districts.',
                true,
                'system.uc03-booking-masters'
            FROM auditcore.dealer_outlets o
            WHERE o.tenant_id=p_tenant_id
              AND o.city IS NOT NULL
              AND BTRIM(o.city) <> ''
            ON CONFLICT (tenant_id, domain_key, status_code)
            DO UPDATE SET
                status_label=EXCLUDED.status_label,
                description=EXCLUDED.description,
                is_active=true,
                updated_at_utc=now();
        END;
        $$
        """
    )

    # Repair every already-created Project immediately.
    op.execute(
        """
        SELECT auditcore.ensure_uc03_booking_reference_masters(tenant_id)
        FROM auditcore.projects
        """
    )

    # Keep District choices useful as new Dealer Outlets are configured without
    # introducing a second geographic master subsystem.
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION auditcore.seed_uc03_booking_district_from_outlet()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_code varchar(100);
        BEGIN
            IF NEW.city IS NULL OR BTRIM(NEW.city) = '' THEN
                RETURN NEW;
            END IF;

            v_code := LEFT(REGEXP_REPLACE(UPPER(BTRIM(NEW.city)), '[^A-Z0-9]+', '_', 'g'), 100);
            IF v_code = '' THEN
                RETURN NEW;
            END IF;

            INSERT INTO auditcore.business_status_codes (
                tenant_id, domain_key, status_code, status_label,
                description, is_active, created_by_actor_id
            ) VALUES (
                NEW.tenant_id,
                'DISTRICT',
                v_code,
                BTRIM(NEW.city),
                'Derived from a configured Dealer Outlet city; Project admins may add additional registration districts.',
                true,
                'system.uc03-booking-masters'
            )
            ON CONFLICT (tenant_id, domain_key, status_code)
            DO UPDATE SET
                status_label=EXCLUDED.status_label,
                description=EXCLUDED.description,
                is_active=true,
                updated_at_utc=now();

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_dealer_outlets_uc03_booking_district
        ON auditcore.dealer_outlets
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_dealer_outlets_uc03_booking_district
        AFTER INSERT OR UPDATE OF city ON auditcore.dealer_outlets
        FOR EACH ROW
        EXECUTE FUNCTION auditcore.seed_uc03_booking_district_from_outlet()
        """
    )


def downgrade() -> None:
    # Do not delete reference rows: they may already be referenced by Booking
    # history. Remove only the additional synchronization trigger/function. The
    # 0023 Project trigger continues to call the compatible helper signature.
    op.execute(
        "DROP TRIGGER IF EXISTS trg_dealer_outlets_uc03_booking_district ON auditcore.dealer_outlets"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS auditcore.seed_uc03_booking_district_from_outlet()"
    )
