"""0204_activity_reading_window -- the reading window of "time on Verigence" (2026-11-09).

Product owner, 9 Nov 2026: a minute counts as active only when there was a
click, tap or key in that minute or in the few minutes before it (reading time
after a click); scrolling alone no longer counts. The reading window is 3
minutes by default and SuperAdmin may set it from 1 to 10 minutes, for every
Project or for one Project, every change recorded with who and when.

``p2_activity_settings`` holds at most one row per scope, as p2_upload_pause:
a Project's own row (tenant_id) or the all-Projects row (tenant_id NULL), unique
on coalesce(tenant_id, '*'). A Project row with no minutes follows the
all-Projects value; the all-Projects row always carries a value. No row: 3.

``p2_activity_setting_changes`` is the change log: append-only (no UPDATE or
DELETE for anyone at runtime).

Row security: every Project reads its own row and the all-Projects row (the
app reads the window it classifies with); only a SuperAdmin request
(app.platform_super_admin, set after Security's attestation) writes.

The minute-by-minute record (p2_user_activity_daily.minute_states, 0177) also
takes S: a minute visible and scrolled with no click, tap or key in the reading
window (idle, kept apart so reports can show scrolling with no click).
"""
from __future__ import annotations

from alembic import op

revision = "0204_activity_reading_window"
down_revision = "0202_gate_pass_past_dates"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_SETTINGS = "p2_activity_settings"
_CHANGES = "p2_activity_setting_changes"
_READ = ("USING (tenant_id IS NULL OR tenant_id = auditcore.current_tenant_id() "
         "OR auditcore.current_platform_super_admin())")


_MINUTES_CHECK = "p2_user_activity_daily_minute_states_check"


def _minute_states_check(characters: str) -> None:
    op.execute(f"ALTER TABLE auditcore.p2_user_activity_daily DROP CONSTRAINT IF EXISTS {_MINUTES_CHECK}")
    op.execute(
        f"ALTER TABLE auditcore.p2_user_activity_daily ADD CONSTRAINT {_MINUTES_CHECK} "
        f"CHECK (minute_states IS NULL OR (length(minute_states) = 1440 AND minute_states ~ '^[{characters}]*$'))"
    )


def upgrade() -> None:
    _minute_states_check(".BISAK")
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_SETTINGS} (
            setting_id              uuid         NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
            tenant_id               varchar(128),
            reading_window_minutes  smallint     CHECK (reading_window_minutes BETWEEN 1 AND 10),
            set_by_actor_id         varchar(160),
            set_at_utc              timestamptz  NOT NULL DEFAULT now(),
            CONSTRAINT ck_{_SETTINGS}_all_projects_has_value
                CHECK (tenant_id IS NOT NULL OR reading_window_minutes IS NOT NULL)
        )
        """
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{_SETTINGS}_scope "
        f"ON auditcore.{_SETTINGS} ((COALESCE(tenant_id, '*')))"
    )
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_CHANGES} (
            change_id                uuid         NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
            tenant_id                varchar(128),
            previous_minutes         smallint,
            reading_window_minutes   smallint,
            changed_by_actor_id      varchar(160) NOT NULL,
            changed_at_utc           timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_CHANGES}_at ON auditcore.{_CHANGES} (changed_at_utc DESC)")
    for table in (_SETTINGS, _CHANGES):
        op.execute(f"ALTER TABLE auditcore.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE auditcore.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS {table}_read ON auditcore.{table}")
        op.execute(f"CREATE POLICY {table}_read ON auditcore.{table} FOR SELECT {_READ}")
        op.execute(f"DROP POLICY IF EXISTS {table}_admin_insert ON auditcore.{table}")
        op.execute(
            f"CREATE POLICY {table}_admin_insert ON auditcore.{table} FOR INSERT "
            "WITH CHECK (auditcore.current_platform_super_admin())"
        )
        op.execute(f"REVOKE DELETE ON auditcore.{table} FROM {_RUNTIME_ROLE}")
    op.execute(f"DROP POLICY IF EXISTS {_SETTINGS}_admin_update ON auditcore.{_SETTINGS}")
    op.execute(
        f"CREATE POLICY {_SETTINGS}_admin_update ON auditcore.{_SETTINGS} FOR UPDATE "
        "USING (auditcore.current_platform_super_admin()) "
        "WITH CHECK (auditcore.current_platform_super_admin())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.{_SETTINGS} TO {_RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON auditcore.{_CHANGES} TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE UPDATE ON auditcore.{_CHANGES} FROM {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute("UPDATE auditcore.p2_user_activity_daily SET minute_states = translate(minute_states, 'S', 'I') "
               "WHERE minute_states LIKE '%S%'")
    _minute_states_check(".BIAK")
    op.execute(f"DROP TABLE IF EXISTS auditcore.{_CHANGES}")
    op.execute(f"DROP TABLE IF EXISTS auditcore.{_SETTINGS}")
