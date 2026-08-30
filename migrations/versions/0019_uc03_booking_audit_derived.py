from alembic import op

revision = "0019_uc03_booking_audit"
down_revision = "0018_uc03_pc_verification"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    # Keep the source/observed booking_date unchanged. This date is derived only
    # after verified Booking receipts cumulatively reach the configured minimum.
    op.execute(
        "ALTER TABLE auditcore.bookings "
        "ADD COLUMN booking_confirmation_date date"
    )

    # product_sku_id remains the single owner for the system-resolved SKU.
    # Store only the resolution explanation when no unique SKU can be selected.
    op.execute(
        "ALTER TABLE auditcore.journey_products "
        "ADD COLUMN sku_resolution_remarks text"
    )

    # A duplicate is a relationship between two Bookings, not copied customer data.
    # The earlier Booking is original and the later Booking is duplicate.
    op.execute(
        """
        CREATE TABLE auditcore.booking_duplicate_links (
            tenant_id              varchar(128) NOT NULL,
            duplicate_link_id      uuid NOT NULL DEFAULT gen_random_uuid(),
            original_booking_id    uuid NOT NULL,
            duplicate_booking_id   uuid NOT NULL,
            match_reasons          jsonb NOT NULL DEFAULT '[]'::jsonb,
            first_detected_at_utc  timestamptz NOT NULL DEFAULT now(),
            last_confirmed_at_utc  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, duplicate_link_id),
            UNIQUE (tenant_id, original_booking_id, duplicate_booking_id),
            FOREIGN KEY (tenant_id, original_booking_id)
                REFERENCES auditcore.bookings(tenant_id, booking_id),
            FOREIGN KEY (tenant_id, duplicate_booking_id)
                REFERENCES auditcore.bookings(tenant_id, booking_id),
            CHECK (original_booking_id <> duplicate_booking_id),
            CHECK (jsonb_typeof(match_reasons) = 'array')
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_booking_duplicate_links_duplicate "
        "ON auditcore.booking_duplicate_links(tenant_id, duplicate_booking_id)"
    )
    op.execute("ALTER TABLE auditcore.booking_duplicate_links ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE auditcore.booking_duplicate_links FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_booking_duplicate_links
        ON auditcore.booking_duplicate_links
        USING (tenant_id = auditcore.current_tenant_id())
        WITH CHECK (tenant_id = auditcore.current_tenant_id())
        """
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON auditcore.booking_duplicate_links TO {_RUNTIME_ROLE}"
    )
    op.execute(
        f"REVOKE DELETE ON auditcore.booking_duplicate_links FROM {_RUNTIME_ROLE}"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.ix_booking_duplicate_links_duplicate")
    op.execute("DROP TABLE IF EXISTS auditcore.booking_duplicate_links")
    op.execute(
        "ALTER TABLE auditcore.journey_products "
        "DROP COLUMN IF EXISTS sku_resolution_remarks"
    )
    op.execute(
        "ALTER TABLE auditcore.bookings "
        "DROP COLUMN IF EXISTS booking_confirmation_date"
    )
