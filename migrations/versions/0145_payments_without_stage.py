"""0145_payments_without_stage — a receipt is the Journey's, not a stage's.

Decision 2026-10-19 (product owner): there is one upload screen and receipts
carry no Booking or Delivery stage. The only rules are:

  * Booking is confirmed once the receipts, taken in receipt-date order,
    reach the tenant's minimum booking amount;
  * all receipts together equal all invoice amounts (to within Rs 1).

Which receipts make up the Booking payment is derived when needed
(uc03_p2_stage.receipt_split), never stored on the row.

0041 tied payment_stage to delivery_id (a CHECK constraint and the
prepare_payment_stage_link() trigger): only a 'DELIVERY' payment could
reference a Delivery, and a 'DELIVERY' payment had to. The Booking and
Delivery receipt writers stamped opposite stages on the same receipt, and the
trigger refused the flip back to 'BOOKING' ("Only a Delivery-stage payment may
reference a Delivery"), failing the whole Journey's document sync on every
retry (found live 2026-10-19: three Journeys rescheduled every five minutes
for days).

This migration:
  * drops ck_payments_stage_delivery_link;
  * redefines prepare_payment_stage_link() to keep only the Booking linkage
    (every payment belongs to its Journey's Booking) and the integrity check
    that a delivery_id, if one is ever given, belongs to the same Journey --
    no stage rule, no automatic delivery_id;
  * clears the stamps already written: payment_stage='UNSPECIFIED',
    delivery_id=NULL on every payment. Nothing reads payments.delivery_id,
    and payment_stage is no longer read anywhere either.

The column and its value CHECK stay (dropping them is not needed and would
break an older build still running during a deploy).
"""
from __future__ import annotations

from alembic import op

revision = "0145_payments_without_stage"
down_revision = "0144_sku_resolution_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.payments DROP CONSTRAINT IF EXISTS ck_payments_stage_delivery_link")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.prepare_payment_stage_link()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_booking_id uuid;
            v_delivery_journey_id uuid;
        BEGIN
            INSERT INTO auditcore.bookings (tenant_id, journey_id)
            VALUES (NEW.tenant_id, NEW.journey_id)
            ON CONFLICT (tenant_id, journey_id) DO NOTHING;

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

            -- Receipts carry no stage (0145): no stage rule, no automatic
            -- Delivery link. A delivery_id, if one is ever set, must still be
            -- this Journey's.
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

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        UPDATE auditcore.payments
        SET payment_stage = 'UNSPECIFIED', delivery_id = NULL
        WHERE payment_stage <> 'UNSPECIFIED' OR delivery_id IS NOT NULL
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN auditcore.payments.payment_stage IS
            'Unused since 0145 (2026-10-19): receipts carry no stage. The Booking payment is derived '
            '(receipt-date order up to the minimum booking amount, uc03_p2_stage.receipt_split).';
        """
    )


def downgrade() -> None:
    # The stamps cleared above are not restored: they were the inconsistent
    # state this migration removes. Any stamp written since (nothing in the
    # application writes one, but a script or test may) is cleared first, so
    # every row is UNSPECIFIED with no delivery_id when the 0041 trigger and
    # constraint come back -- which satisfies both.
    op.execute(
        """
        UPDATE auditcore.payments
        SET payment_stage = 'UNSPECIFIED', delivery_id = NULL
        WHERE payment_stage <> 'UNSPECIFIED' OR delivery_id IS NOT NULL
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
            INSERT INTO auditcore.bookings (tenant_id, journey_id)
            VALUES (NEW.tenant_id, NEW.journey_id)
            ON CONFLICT (tenant_id, journey_id) DO NOTHING;

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
            IF NEW.payment_stage IN ('UNSPECIFIED','BOOKING') AND NEW.delivery_id IS NOT NULL THEN
                RAISE EXCEPTION 'Only a Delivery-stage payment may reference a Delivery';
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.payments
            ADD CONSTRAINT ck_payments_stage_delivery_link
            CHECK (
                (payment_stage IN ('UNSPECIFIED','BOOKING') AND delivery_id IS NULL)
                OR
                (payment_stage = 'DELIVERY' AND delivery_id IS NOT NULL)
            )
        """
    )
