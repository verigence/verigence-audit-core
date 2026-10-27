"""0169_people_names -- people's display names from Security (2026-10-27).

The Task Manager's assign list showed ids for people who had not yet used the
app. Security now lets Audit Core read display names by user id (read-only);
the P2 worker copies them here every few hours, and every name lookup in Audit
Core reads this first. Platform-level: a person is the same in every Project.
"""
from __future__ import annotations

from alembic import op

revision = "0169_people_names"
down_revision = "0168_gate_pass_pages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.people_names (
          actor_id varchar(128) PRIMARY KEY,
          display_name text NOT NULL,
          refreshed_at_utc timestamptz NOT NULL DEFAULT now()
        );
        GRANT SELECT, INSERT, UPDATE ON auditcore.people_names TO audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.people_names")
