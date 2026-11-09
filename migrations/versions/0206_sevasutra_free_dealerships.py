"""0206_sevasutra_free_dealerships -- SevaSutra dealerships not tied to any Project yet (2026-11-09).

"Link demo to a Project": a Super Admin can attach an existing SevaSutra dealership (the demo, with its
sample month) to a service-audit Project instead of creating a new one. SevaSutra reports the dealerships
it holds that no Project owns; Project Administration lists them on the SevaSutra instance card. Platform
data (no tenant), like the OEM list; replaced whole by each report.

Additive; idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0206_sevasutra_free_dealerships"
down_revision = "0205_sevasutra_instances"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.sevasutra_free_dealerships (
            dealer_code      varchar(60)  NOT NULL PRIMARY KEY,
            dealership_name  varchar(240) NOT NULL,
            outlets          integer      NOT NULL DEFAULT 0,
            visits           integer      NOT NULL DEFAULT 0,
            reported_at_utc  timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON auditcore.sevasutra_free_dealerships TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.sevasutra_free_dealerships")
