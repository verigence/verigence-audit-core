"""0218_job_schedules -- the time of each daily job, set by a SuperAdmin.

Product owner, 13 Nov 2026: "create dashboard of all jobs which get executed for
Superadmin and allow him to change the time" and "lets move all jobs before
10:00 pm, so we can close all things by mid night and have system sleep".

Until now each daily job's time was a constant or an environment variable of
the worker (nightly review and day-end clean-up at 02:00 IST, data archive at
03:00 IST): changing one needed a deployment, and nobody could see them in one
place.

- platform_ops.job_schedules: one row per daily job a SuperAdmin has set a time
  for (India time). A job with no row runs at its default (audit_core.job_schedule).
- platform_ops.job_schedule_changes: who changed which time, from what to what.

Platform-level, like the rest of platform_ops: the jobs run for every Project.
"""
from __future__ import annotations

from alembic import op

revision = "0218_job_schedules"
down_revision = "0217_unrecognised_message"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE SCHEMA IF NOT EXISTS platform_ops;
        GRANT USAGE ON SCHEMA platform_ops TO audit_core_runtime;

        CREATE TABLE IF NOT EXISTS platform_ops.job_schedules (
          job_key varchar(60) PRIMARY KEY,
          run_time_ist time NOT NULL,
          updated_by varchar(160),
          updated_at_utc timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS platform_ops.job_schedule_changes (
          change_id bigserial PRIMARY KEY,
          job_key varchar(200) NOT NULL,
          old_time_ist time,
          new_time_ist time NOT NULL,
          changed_by varchar(160),
          changed_at_utc timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_job_schedule_changes_at
          ON platform_ops.job_schedule_changes (changed_at_utc DESC);

        GRANT SELECT, INSERT, UPDATE ON platform_ops.job_schedules TO audit_core_runtime;
        GRANT SELECT, INSERT ON platform_ops.job_schedule_changes TO audit_core_runtime;
        GRANT USAGE, SELECT ON SEQUENCE platform_ops.job_schedule_changes_change_id_seq TO audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS platform_ops.job_schedule_changes;
        DROP TABLE IF EXISTS platform_ops.job_schedules;
        """
    )
