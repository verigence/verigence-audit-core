"""Establish explicit UC03 Journey/Booking/Delivery/Customer/Payment linkage.

Revision ID: 0040_uc03_stage_linkage
Revises: 0039_uc03_booking_fields
Create Date: 2026-08-30

Forward foreign keys remain the integrity source of truth. Reverse identifiers on
Customer and Journey are intentionally maintained as lightweight query pointers so
we do not introduce cyclic delete dependencies into the existing Journey model.
"""
from alembic import op

revision = "0040_uc03_stage_linkage"
down_revision = "0039_uc03_booking_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.customers
            ADD COLUMN journey_id uuid,
            ADD COLUMN booking_id uuid;

        ALTER TABLE auditcore.journeys
            ADD COLUMN booking_id uuid,
            ADD COLUMN delivery_id uuid;

        ALTER TABLE auditcore.deliveries
            ADD COLUMN booking_id uuid;

        ALTER TABLE auditcore.payments
            ADD COLUMN booking_id uuid,
            ADD COLUMN delivery_id uuid,
            ADD COLUMN payment_stage varchar(20) NOT NULL DEFAULT 'BOOKING';
        """
    )

    # Every Journey is a Booking Journey. Older rows that pre-date this explicit
    # linkage receive a lightweight Booking row; no document/extraction data is
    # copied and no business value is invented.
    op.execute(
        """
        INSERT INTO auditcore.bookings (tenant_id, journey_id)
        SELECT j.tenant_id, j.journey_id
        FROM auditcore.journeys j
        WHERE NOT EXISTS (
            SELECT 1
            FROM auditcore.bookings b
            WHERE b.tenant_id = j.tenant_id
              AND b.journey_id = j.journey_id
        );
        """
    )

    op.execute(
        """
        UPDATE auditcore.journeys j
        SET booking_id = b.booking_id
        FROM auditcore.bookings b
        WHERE b.tenant_id = j.tenant_id
          AND b.journey_id = j.journey_id;

        UPDATE auditcore.deliveries d
        SET booking_id = b.booking_id
        FROM auditcore.bookings b
        WHERE b.tenant_id = d.tenant_id
          AND b.journey_id = d.journey_id;

        UPDATE auditcore.journeys j
        SET delivery_id = d.delivery_id
        FROM auditcore.deliveries d
        WHERE d.tenant_id = j.tenant_id
          AND d.journey_id = j.journey_id;

        UPDATE auditcore.payments p
        SET booking_id = b.booking_id
        FROM auditcore.bookings b
        WHERE b.tenant_id = p.tenant_id
          AND b.journey_id = p.journey_id;
        """
    )

    # Current UC03 Create Booking creates one Customer row per Journey. For any
    # legacy/shared Customer, keep the most recent Journey as the reverse pointer;
    # all Journey->Customer forward history remains unchanged.
    op.execute(
        """
        WITH current_customer_journey AS (
            SELECT DISTINCT ON (j.tenant_id, j.customer_id)
                j.tenant_id,
                j.customer_id,
                j.journey_id,
                b.booking_id
            FROM auditcore.journeys j
            JOIN auditcore.bookings b
              ON b.tenant_id = j.tenant_id
             AND b.journey_id = j.journey_id
            ORDER BY j.tenant_id, j.customer_id,
                     j.created_at_utc DESC, j.journey_id DESC
        )
        UPDATE auditcore.customers c
        SET journey_id = x.journey_id,
            booking_id = x.booking_id
        FROM current_customer_journey x
        WHERE x.tenant_id = c.tenant_id
          AND x.customer_id = c.customer_id;
        """
    )

    op.execute(
        """
        ALTER TABLE auditcore.deliveries
            ALTER COLUMN booking_id SET NOT NULL;
        ALTER TABLE auditcore.payments
            ALTER COLUMN booking_id SET NOT NULL;

        ALTER TABLE auditcore.deliveries
            ADD CONSTRAINT fk_deliveries_booking
            FOREIGN KEY (tenant_id, booking_id)
            REFERENCES auditcore.bookings(tenant_id, booking_id);

        ALTER TABLE auditcore.payments
            ADD CONSTRAINT fk_payments_booking
            FOREIGN KEY (tenant_id, booking_id)
            REFERENCES auditcore.bookings(tenant_id, booking_id),
            ADD CONSTRAINT fk_payments_delivery
            FOREIGN KEY (tenant_id, delivery_id)
            REFERENCES auditcore.deliveries(tenant_id, delivery_id),
            ADD CONSTRAINT ck_payments_payment_stage
            CHECK (payment_stage IN ('BOOKING','DELIVERY')),
            ADD CONSTRAINT ck_payments_stage_delivery_link
            CHECK (
                (payment_stage = 'BOOKING' AND delivery_id IS NULL)
                OR
                (payment_stage = 'DELIVERY' AND delivery_id IS NOT NULL)
            );
        """
    )

    op.execute(
        """
        CREATE UNIQUE INDEX uq_journeys_booking_pointer
            ON auditcore.journeys(tenant_id, booking_id)
            WHERE booking_id IS NOT NULL;
        CREATE UNIQUE INDEX uq_journeys_delivery_pointer
            ON auditcore.journeys(tenant_id, delivery_id)
            WHERE delivery_id IS NOT NULL;
        CREATE UNIQUE INDEX uq_deliveries_booking
            ON auditcore.deliveries(tenant_id, booking_id);
        CREATE INDEX ix_customers_journey_booking
            ON auditcore.customers(tenant_id, journey_id, booking_id);
        CREATE INDEX ix_payments_booking_stage
            ON auditcore.payments(tenant_id, booking_id, payment_stage, payment_id);
        CREATE INDEX ix_payments_delivery
            ON auditcore.payments(tenant_id, delivery_id, payment_id)
            WHERE delivery_id IS NOT NULL;
        """
    )

    # Journey creation now guarantees an immediately addressable Booking ID. The
    # existing UC03 create command can remain small: the database creates only the
    # linkage row, while Booking details continue to be captured later.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.ensure_booking_for_journey()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            INSERT INTO auditcore.bookings (tenant_id, journey_id)
            VALUES (NEW.tenant_id, NEW.journey_id)
            ON CONFLICT (tenant_id, journey_id) DO NOTHING;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_ensure_booking_for_journey
        AFTER INSERT ON auditcore.journeys
        FOR EACH ROW
        EXECUTE FUNCTION auditcore.ensure_booking_for_journey();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.sync_booking_reverse_links()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_customer_id uuid;
        BEGIN
            SELECT j.customer_id
            INTO v_customer_id
            FROM auditcore.journeys j
            WHERE j.tenant_id = NEW.tenant_id
              AND j.journey_id = NEW.journey_id;

            UPDATE auditcore.journeys
            SET booking_id = NEW.booking_id
            WHERE tenant_id = NEW.tenant_id
              AND journey_id = NEW.journey_id
              AND booking_id IS DISTINCT FROM NEW.booking_id;

            IF v_customer_id IS NOT NULL THEN
                UPDATE auditcore.customers
                SET journey_id = NEW.journey_id,
                    booking_id = NEW.booking_id
                WHERE tenant_id = NEW.tenant_id
                  AND customer_id = v_customer_id
                  AND (
                      journey_id IS DISTINCT FROM NEW.journey_id
                      OR booking_id IS DISTINCT FROM NEW.booking_id
                  );
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_sync_booking_reverse_links
        AFTER INSERT OR UPDATE OF journey_id ON auditcore.bookings
        FOR EACH ROW
        EXECUTE FUNCTION auditcore.sync_booking_reverse_links();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.prepare_delivery_booking_link()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_booking_id uuid;
        BEGIN
            SELECT b.booking_id
            INTO v_booking_id
            FROM auditcore.bookings b
            WHERE b.tenant_id = NEW.tenant_id
              AND b.journey_id = NEW.journey_id;

            IF v_booking_id IS NULL THEN
                RAISE EXCEPTION 'Delivery requires a Booking linked to the same Journey';
            END IF;

            IF NEW.booking_id IS NOT NULL AND NEW.booking_id <> v_booking_id THEN
                RAISE EXCEPTION 'Delivery Booking ID does not belong to the Journey';
            END IF;

            NEW.booking_id := v_booking_id;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_prepare_delivery_booking_link
        BEFORE INSERT OR UPDATE OF journey_id, booking_id ON auditcore.deliveries
        FOR EACH ROW
        EXECUTE FUNCTION auditcore.prepare_delivery_booking_link();

        CREATE OR REPLACE FUNCTION auditcore.sync_delivery_reverse_link()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            UPDATE auditcore.journeys
            SET delivery_id = NEW.delivery_id
            WHERE tenant_id = NEW.tenant_id
              AND journey_id = NEW.journey_id
              AND delivery_id IS DISTINCT FROM NEW.delivery_id;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_sync_delivery_reverse_link
        AFTER INSERT OR UPDATE OF journey_id ON auditcore.deliveries
        FOR EACH ROW
        EXECUTE FUNCTION auditcore.sync_delivery_reverse_link();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.prepare_payment_stage_link()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_booking_id uuid;
            v_delivery_id uuid;
            v_delivery_journey_id uuid;
        BEGIN
            SELECT b.booking_id
            INTO v_booking_id
            FROM auditcore.bookings b
            WHERE b.tenant_id = NEW.tenant_id
              AND b.journey_id = NEW.journey_id;

            IF v_booking_id IS NULL THEN
                RAISE EXCEPTION 'Payment requires a Booking linked to the same Journey';
            END IF;

            IF NEW.booking_id IS NOT NULL AND NEW.booking_id <> v_booking_id THEN
                RAISE EXCEPTION 'Payment Booking ID does not belong to the Journey';
            END IF;
            NEW.booking_id := v_booking_id;

            IF NEW.payment_stage = 'DELIVERY' AND NEW.delivery_id IS NULL THEN
                SELECT j.delivery_id
                INTO v_delivery_id
                FROM auditcore.journeys j
                WHERE j.tenant_id = NEW.tenant_id
                  AND j.journey_id = NEW.journey_id;
                NEW.delivery_id := v_delivery_id;
            END IF;

            IF NEW.delivery_id IS NOT NULL THEN
                SELECT d.journey_id
                INTO v_delivery_journey_id
                FROM auditcore.deliveries d
                WHERE d.tenant_id = NEW.tenant_id
                  AND d.delivery_id = NEW.delivery_id;
                IF v_delivery_journey_id IS NULL OR v_delivery_journey_id <> NEW.journey_id THEN
                    RAISE EXCEPTION 'Payment Delivery ID does not belong to the Journey';
                END IF;
            END IF;

            IF NEW.payment_stage = 'DELIVERY' AND NEW.delivery_id IS NULL THEN
                RAISE EXCEPTION 'Delivery-stage payment requires a Delivery linked to the Journey';
            END IF;
            IF NEW.payment_stage = 'BOOKING' AND NEW.delivery_id IS NOT NULL THEN
                RAISE EXCEPTION 'Booking-stage payment cannot reference a Delivery';
            END IF;

            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_prepare_payment_stage_link
        BEFORE INSERT OR UPDATE OF journey_id, booking_id, delivery_id, payment_stage
        ON auditcore.payments
        FOR EACH ROW
        EXECUTE FUNCTION auditcore.prepare_payment_stage_link();
        """
    )

    op.execute(
        """
        COMMENT ON COLUMN auditcore.customers.journey_id IS
            'Reverse query pointer to the current/owning UC03 Journey; forward Journey->Customer remains authoritative.';
        COMMENT ON COLUMN auditcore.customers.booking_id IS
            'Reverse query pointer to the Booking for the current/owning UC03 Journey.';
        COMMENT ON COLUMN auditcore.journeys.booking_id IS
            'Reverse pointer to the one Booking row for this Journey.';
        COMMENT ON COLUMN auditcore.journeys.delivery_id IS
            'Reverse pointer to the one Delivery row for this Journey when Delivery exists.';
        COMMENT ON COLUMN auditcore.deliveries.booking_id IS
            'Booking owning this Delivery; derived from and constrained to the same Journey.';
        COMMENT ON COLUMN auditcore.payments.booking_id IS
            'Booking owning this payment; derived from the Journey.';
        COMMENT ON COLUMN auditcore.payments.delivery_id IS
            'Delivery owning a DELIVERY-stage payment; null for BOOKING-stage payments.';
        COMMENT ON COLUMN auditcore.payments.payment_stage IS
            'Business stage for the payment: BOOKING or DELIVERY.';
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_prepare_payment_stage_link ON auditcore.payments")
    op.execute("DROP FUNCTION IF EXISTS auditcore.prepare_payment_stage_link()")
    op.execute("DROP TRIGGER IF EXISTS trg_sync_delivery_reverse_link ON auditcore.deliveries")
    op.execute("DROP FUNCTION IF EXISTS auditcore.sync_delivery_reverse_link()")
    op.execute("DROP TRIGGER IF EXISTS trg_prepare_delivery_booking_link ON auditcore.deliveries")
    op.execute("DROP FUNCTION IF EXISTS auditcore.prepare_delivery_booking_link()")
    op.execute("DROP TRIGGER IF EXISTS trg_sync_booking_reverse_links ON auditcore.bookings")
    op.execute("DROP FUNCTION IF EXISTS auditcore.sync_booking_reverse_links()")
    op.execute("DROP TRIGGER IF EXISTS trg_ensure_booking_for_journey ON auditcore.journeys")
    op.execute("DROP FUNCTION IF EXISTS auditcore.ensure_booking_for_journey()")

    op.execute("DROP INDEX IF EXISTS auditcore.ix_payments_delivery")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_payments_booking_stage")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_customers_journey_booking")
    op.execute("DROP INDEX IF EXISTS auditcore.uq_deliveries_booking")
    op.execute("DROP INDEX IF EXISTS auditcore.uq_journeys_delivery_pointer")
    op.execute("DROP INDEX IF EXISTS auditcore.uq_journeys_booking_pointer")

    op.execute("ALTER TABLE auditcore.payments DROP CONSTRAINT IF EXISTS ck_payments_stage_delivery_link")
    op.execute("ALTER TABLE auditcore.payments DROP CONSTRAINT IF EXISTS ck_payments_payment_stage")
    op.execute("ALTER TABLE auditcore.payments DROP CONSTRAINT IF EXISTS fk_payments_delivery")
    op.execute("ALTER TABLE auditcore.payments DROP CONSTRAINT IF EXISTS fk_payments_booking")
    op.execute("ALTER TABLE auditcore.deliveries DROP CONSTRAINT IF EXISTS fk_deliveries_booking")

    op.execute(
        """
        ALTER TABLE auditcore.payments
            DROP COLUMN IF EXISTS payment_stage,
            DROP COLUMN IF EXISTS delivery_id,
            DROP COLUMN IF EXISTS booking_id;
        ALTER TABLE auditcore.deliveries
            DROP COLUMN IF EXISTS booking_id;
        ALTER TABLE auditcore.journeys
            DROP COLUMN IF EXISTS delivery_id,
            DROP COLUMN IF EXISTS booking_id;
        ALTER TABLE auditcore.customers
            DROP COLUMN IF EXISTS booking_id,
            DROP COLUMN IF EXISTS journey_id;
        """
    )
