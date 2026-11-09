"""0207_people_employment -- who has left, and on which day, as HR reports it (2026-11-09).

People who quit, were suspended or were terminated kept showing in reports and dashboards until
someone remembered to end their role in Audit Core. HR now reports everyone who is not active,
with the day they left; Audit Core ends what is still open for them after that day and keeps them
in reports only up to it. Platform-level, like people_names: a person is the same in every
Project. Replaced whole by each report from HR.

Additive; idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0207_people_employment"
down_revision = "0206_sevasutra_free_dealerships"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.people_employment (
            actor_id           varchar(128) NOT NULL PRIMARY KEY,
            employee_code      varchar(40),
            employment_status  varchar(20)  NOT NULL,
            exit_date          date,
            reported_at_utc    timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON auditcore.people_employment TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.people_employment")
