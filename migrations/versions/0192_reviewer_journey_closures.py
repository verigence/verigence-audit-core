"""0192_reviewer_journey_closures — a Reviewer's closure of a journey and its summary
(product owner, 2026-11-04).

"Reviewer should be given a privilege to close a journey with comments. It
should be covered which all documents which were mandatory has been skipped
for this journey. Any violations which are reported has been not considered
reason for it ... a very deterministic report ... it will act as add to our
existing audit report."

One row per closure: who, when, the overall comment, and the summary -- every
item that was open at closure (mandatory document not on file, open finding,
open task) with the specific answer given for it. Written once, never updated
or deleted by the runtime role; the latest row for a journey is the one the
audit report shows. Additive; idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0192_reviewer_journey_closures"
down_revision = "0191_gate_pass_count_edits"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLE = "p2_journey_closures"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_TABLE} (
            closure_id          uuid         NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
            tenant_id           varchar(128) NOT NULL,
            journey_id          uuid         NOT NULL,
            closed_by_actor_id  varchar(200) NOT NULL,
            closed_by_role      varchar(50)  NOT NULL,
            comment             text         NOT NULL CHECK (length(btrim(comment)) >= 10),
            item_count          integer      NOT NULL DEFAULT 0,
            summary             jsonb        NOT NULL,
            delivery_outcome    varchar(40)  NOT NULL,
            correlation_id      varchar(200),
            closed_at_utc       timestamptz  NOT NULL DEFAULT now(),
            -- Goes with its journey, as every Phase 2 table does (journey delete / housekeeping).
            FOREIGN KEY (tenant_id, journey_id)
              REFERENCES auditcore.journeys(tenant_id, journey_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_journey ON auditcore.{_TABLE} (tenant_id, journey_id, closed_at_utc DESC)"
    )
    op.execute(f"ALTER TABLE auditcore.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON auditcore.{_TABLE}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_TABLE} ON auditcore.{_TABLE} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT ON auditcore.{_TABLE} TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE ON auditcore.{_TABLE} FROM {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS auditcore.{_TABLE}")
