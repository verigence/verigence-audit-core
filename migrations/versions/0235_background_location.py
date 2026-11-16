"""0235_background_location -- location from the phone in the background, and
readings sent later (product owner, 2026-11-16).

"can you pls do offline check for the location on the mobile device ... though
app may be in background or may be killed.. some way to collect the data".
Decided the same day: everyone who marks attendance on the phone is tracked,
from their myHR check-in to their check-out, within 10:00-19:30 IST.

``p2_location_checks`` gains:

- ``received_at_utc``: when the server received the reading. ``checked_at_utc``
  stays the moment of the check: for a reading the phone kept while it was
  offline, that is when it was read, not when it arrived.
- ``check_trigger``: FOREGROUND (the random checks while DealGuard is open) or
  BACKGROUND (the phone's tracking service between check-in and check-out).
- ``queued``: the reading was kept on the device and sent later.
- ``client_check_id``: the device's own id for the reading, so a reading sent
  twice (the answer to the first send was lost) is logged once.

``p2_location_tracking_keys`` holds the key a phone's tracking service signs its
readings with. The service runs while the app is closed, when the person's
sign-in token cannot be refreshed; the key can only add location readings for
that one person in that one Project, and lapses at the end of the day. Only its
SHA-256 is stored. Additive; idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0235_background_location"
down_revision = "0234_engagement_output_window"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_CHECKS = "p2_location_checks"
_KEYS = "p2_location_tracking_keys"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE auditcore.{_CHECKS}
          ADD COLUMN IF NOT EXISTS received_at_utc timestamptz,
          ADD COLUMN IF NOT EXISTS check_trigger varchar(12) NOT NULL DEFAULT 'FOREGROUND',
          ADD COLUMN IF NOT EXISTS queued boolean NOT NULL DEFAULT false,
          ADD COLUMN IF NOT EXISTS client_check_id varchar(64)
        """
    )
    op.execute(f"UPDATE auditcore.{_CHECKS} SET received_at_utc = checked_at_utc WHERE received_at_utc IS NULL")
    op.execute(
        f"""
        ALTER TABLE auditcore.{_CHECKS}
          ALTER COLUMN received_at_utc SET DEFAULT now(),
          ALTER COLUMN received_at_utc SET NOT NULL
        """
    )
    op.execute(f"ALTER TABLE auditcore.{_CHECKS} DROP CONSTRAINT IF EXISTS {_CHECKS}_trigger_check")
    op.execute(
        f"ALTER TABLE auditcore.{_CHECKS} ADD CONSTRAINT {_CHECKS}_trigger_check "
        "CHECK (check_trigger IN ('FOREGROUND', 'BACKGROUND'))"
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{_CHECKS}_client ON auditcore.{_CHECKS} "
        "(tenant_id, actor_id, client_check_id) WHERE client_check_id IS NOT NULL"
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_KEYS} (
            key_id           uuid          NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
            tenant_id        varchar(128)  NOT NULL,
            actor_id         varchar(160)  NOT NULL,
            key_hash         char(64)      NOT NULL UNIQUE,
            device_type      varchar(20),
            created_at_utc   timestamptz   NOT NULL DEFAULT now(),
            expires_at_utc   timestamptz   NOT NULL,
            revoked_at_utc   timestamptz,
            last_used_at_utc timestamptz
        )
        """
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_KEYS}_actor ON auditcore.{_KEYS} (tenant_id, actor_id, expires_at_utc DESC)"
    )
    op.execute(f"ALTER TABLE auditcore.{_KEYS} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{_KEYS} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_KEYS} ON auditcore.{_KEYS}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_KEYS} ON auditcore.{_KEYS} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.{_KEYS} TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE DELETE ON auditcore.{_KEYS} FROM {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS auditcore.{_KEYS}")
    op.execute(f"DROP INDEX IF EXISTS auditcore.ux_{_CHECKS}_client")
    op.execute(f"ALTER TABLE auditcore.{_CHECKS} DROP CONSTRAINT IF EXISTS {_CHECKS}_trigger_check")
    op.execute(
        f"""
        ALTER TABLE auditcore.{_CHECKS}
          DROP COLUMN IF EXISTS client_check_id,
          DROP COLUMN IF EXISTS queued,
          DROP COLUMN IF EXISTS check_trigger,
          DROP COLUMN IF EXISTS received_at_utc
        """
    )
