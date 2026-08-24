from alembic import op

revision = "0015_uc02_global_segments"
down_revision = "0014_uc02_mahindra_seg"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    # Segment is a universal business classification. The three existing Mahindra
    # rows become the global Segment master while preserving their UUIDs, so all
    # Project/master references remain valid across the migration.
    op.execute("ALTER TABLE auditcore.oem_segments RENAME TO segments")
    op.execute(
        "ALTER TABLE auditcore.segments "
        "DROP CONSTRAINT IF EXISTS oem_segments_oem_id_segment_code_key"
    )
    op.execute(
        "ALTER TABLE auditcore.segments "
        "DROP CONSTRAINT IF EXISTS oem_segments_oem_id_fkey"
    )
    op.execute("ALTER TABLE auditcore.segments DROP COLUMN oem_id")
    op.execute(
        "ALTER TABLE auditcore.segments "
        "ADD CONSTRAINT segments_segment_code_key UNIQUE (segment_code)"
    )
    op.execute(f"GRANT SELECT ON auditcore.segments TO {_RUNTIME_ROLE}")

    # Defensive normalization: only the universal Segment set is supported in
    # Phase 1. Existing UUIDs are intentionally retained.
    op.execute(
        """
        UPDATE auditcore.segments
        SET segment_name = CASE segment_code
            WHEN 'PASSENGER_VEHICLE' THEN 'Passenger Vehicle'
            WHEN 'COMMERCIAL' THEN 'Commercial'
            WHEN 'BATTERY_ELECTRIC' THEN 'Battery Electric'
            ELSE segment_name
        END,
        is_active = CASE
            WHEN segment_code IN ('PASSENGER_VEHICLE','COMMERCIAL','BATTERY_ELECTRIC')
            THEN true ELSE false
        END,
        updated_at_utc = now()
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.segments "
        "DROP CONSTRAINT IF EXISTS segments_segment_code_key"
    )
    op.execute("ALTER TABLE auditcore.segments ADD COLUMN oem_id uuid")
    op.execute(
        """
        UPDATE auditcore.segments s
        SET oem_id = o.oem_id
        FROM auditcore.oems o
        WHERE o.oem_code = 'MAHINDRA'
        """
    )
    op.execute("ALTER TABLE auditcore.segments ALTER COLUMN oem_id SET NOT NULL")
    op.execute(
        "ALTER TABLE auditcore.segments ADD CONSTRAINT oem_segments_oem_id_fkey "
        "FOREIGN KEY (oem_id) REFERENCES auditcore.oems(oem_id)"
    )
    op.execute(
        "ALTER TABLE auditcore.segments "
        "ADD CONSTRAINT oem_segments_oem_id_segment_code_key "
        "UNIQUE (oem_id, segment_code)"
    )
    op.execute("ALTER TABLE auditcore.segments RENAME TO oem_segments")
