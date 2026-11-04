"""0192_reviewer_journey_closures — a Reviewer's closure of a journey and its summary
(product owner, 2026-11-04).

"Reviewer should be given a privilege to close a journey with comments. It
should be covered which all documents which were mandatory has been skipped
for this journey. Any violations which are reported has been not considered
reason for it ... a very deterministic report ... it will act as add to our
existing audit report."

One row per closure: who, when, the overall comment, and the summary -- every
item that was open at closure (mandatory document not on file, open finding,
open task) with the specific answer given for it. The latest row for a journey
is the one the audit report shows.

The Reviewer's closure is the journey's final review; the Team Lead accepts it
or rejects specific items, and one not acted on in 96 hours is accepted
automatically (product owner 2026-11-04). Only those decision columns can
change after the row is written; the summary never does. Additive; idempotent.
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
            acceptance_status   varchar(20)  NOT NULL DEFAULT 'PENDING'
                                CHECK (acceptance_status IN ('PENDING', 'ACCEPTED', 'AUTO_ACCEPTED', 'REJECTED')),
            acceptance_due_utc  timestamptz  NOT NULL,
            acceptance_task_id  uuid,
            decided_by_actor_id varchar(200),
            decided_by_role     varchar(50),
            decided_at_utc      timestamptz,
            -- [{{key, label, reason}}] -- the items the Team Lead rejected, each with why.
            rejections          jsonb        NOT NULL DEFAULT '[]'::jsonb,
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
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_pending ON auditcore.{_TABLE} (acceptance_due_utc) "
        "WHERE acceptance_status = 'PENDING'"
    )
    op.execute(f"GRANT SELECT, INSERT ON auditcore.{_TABLE} TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE ON auditcore.{_TABLE} FROM {_RUNTIME_ROLE}")
    # The Team Lead's decision (or the automatic one) is the only change after insert.
    op.execute(
        f"GRANT UPDATE (acceptance_status, acceptance_task_id, decided_by_actor_id, decided_by_role, "
        f"decided_at_utc, rejections) ON auditcore.{_TABLE} TO {_RUNTIME_ROLE}"
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS auditcore.{_TABLE}")
