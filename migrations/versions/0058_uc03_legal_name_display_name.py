"""Promote verified KYC Legal Name over the UC03 Journey-ID placeholder.

Revision ID: 0058
Revises: 0057
Create Date: 2026-09-06

The simplified Booking flow deliberately removed PC-entered customer name and used
the Journey UUID only as the temporary technical reference while documents were
being captured.  Once PAN/Aadhaar establishes Legal Name, that temporary UUID must
not remain the customer-facing display name.

Entered names remain immutable for normal Customers.  The only permitted mutation
is the one-time transition from the exact linked Journey UUID placeholder to the
verified Legal Name.
"""
from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION auditcore.protect_customer_entered_name()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_placeholder boolean := false;
        BEGIN
            IF NEW.display_name IS DISTINCT FROM OLD.display_name
               AND EXISTS (
                    SELECT 1
                    FROM auditcore.journeys j
                    WHERE j.tenant_id = OLD.tenant_id
                      AND j.customer_id = OLD.customer_id
               ) THEN
                SELECT EXISTS (
                    SELECT 1
                    FROM auditcore.journeys j
                    WHERE j.tenant_id = OLD.tenant_id
                      AND j.customer_id = OLD.customer_id
                      AND OLD.display_name = j.journey_id::text
                )
                INTO v_placeholder;

                IF NOT (
                    v_placeholder
                    AND NEW.legal_name IS NOT NULL
                    AND btrim(NEW.legal_name) <> ''
                    AND NEW.display_name = NEW.legal_name
                ) THEN
                    NEW.display_name := OLD.display_name;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )

    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION auditcore.sync_uc03_display_name_from_legal_name()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.legal_name IS NULL
               OR btrim(NEW.legal_name) = ''
               OR NEW.legal_name_status <> 'VERIFIED' THEN
                RETURN NEW;
            END IF;

            UPDATE auditcore.customers c
            SET display_name = NEW.legal_name,
                updated_at_utc = now()
            WHERE c.tenant_id = NEW.tenant_id
              AND c.customer_id = NEW.customer_id
              AND c.display_name IS DISTINCT FROM NEW.legal_name
              AND EXISTS (
                    SELECT 1
                    FROM auditcore.journeys j
                    WHERE j.tenant_id = c.tenant_id
                      AND j.customer_id = c.customer_id
                      AND c.display_name = j.journey_id::text
              );
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_uc03_sync_display_name_from_legal_name
        ON auditcore.customers
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_uc03_sync_display_name_from_legal_name
        AFTER INSERT OR UPDATE OF legal_name, legal_name_status
        ON auditcore.customers
        FOR EACH ROW
        EXECUTE FUNCTION auditcore.sync_uc03_display_name_from_legal_name()
        """
    )

    # Repair already-verified simplified-flow Customers without touching genuine
    # historical entered names.
    op.execute(
        """
        UPDATE auditcore.customers c
        SET display_name = c.legal_name,
            updated_at_utc = now()
        WHERE c.legal_name IS NOT NULL
          AND btrim(c.legal_name) <> ''
          AND c.legal_name_status = 'VERIFIED'
          AND EXISTS (
                SELECT 1
                FROM auditcore.journeys j
                WHERE j.tenant_id = c.tenant_id
                  AND j.customer_id = c.customer_id
                  AND c.display_name = j.journey_id::text
          )
        """
    )

    op.execute(
        """
        COMMENT ON COLUMN auditcore.customers.display_name IS
        'Customer-facing display name. UC03 simplified capture may initially use the linked Journey UUID as a technical placeholder; verified PAN/Aadhaar Legal Name replaces that placeholder exactly once.'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_uc03_sync_display_name_from_legal_name
        ON auditcore.customers
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS auditcore.sync_uc03_display_name_from_legal_name()"
    )
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION auditcore.protect_customer_entered_name()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.display_name IS DISTINCT FROM OLD.display_name
               AND EXISTS (
                    SELECT 1
                    FROM auditcore.journeys j
                    WHERE j.tenant_id = OLD.tenant_id
                      AND j.customer_id = OLD.customer_id
               ) THEN
                NEW.display_name := OLD.display_name;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
