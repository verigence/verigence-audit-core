"""0106_field_correction_as_task — rework field-correction proposals onto
the Task Queue, not a rule-classified audit finding.

Direct user correction, same shape as 0103's fix for model-selection
corrections: a PC/TL-proposed field correction is a human review-and-decide
(or, below 90% confidence, a self-serve) workflow item, not a rule-detected
violation or a compliance gap -- Audit Review stays reserved for what a
rule actually found wrong with the business process, and the Task Queue
already gives PC/TL/PM full workflow visibility for a given Journey,
searchable by journey_id.

Unlike 0103 (which shipped only hours before its own fix, with no real
data yet), ``journey_document_field_correction_proposals`` has carried
real production rows since 2026-09-13 -- this ALTERs the table in place
rather than dropping it, so every historical row (and its audit_finding_id)
stays exactly as it was:

  - a new nullable ``workflow_task_id`` column, FK'd to
    ``auditcore.workflow_tasks`` -- how every NEW >=90%-confidence
    correction is now keyed (PC proposes, TL Completes/Cancels via the
    ordinary Task Queue actions, same as ``uc03_model_selection_
    corrections.py``'s own MODEL_SELECTION_CORRECTION_REVIEW task).
  - ``audit_finding_id`` becomes nullable: every historical row still has
    it set (and those findings still resolve through the existing
    finding-verdict machinery -- see uc03_audit_flags.py::act_on_flag's
    unchanged CONFIRM_BREACH hook, kept for exactly this backward-
    compatibility reason); a NEW <90%-confidence correction sets NEITHER
    column -- nothing is pending for it (the value was already applied at
    submit time), so it needs no representation in either Audit or the
    Task Queue, only its own row here as the audit-trail record.
  - the old ``PRIMARY KEY (tenant_id, audit_finding_id)`` can no longer
    hold now that the column is nullable -- replaced with a proper
    surrogate ``correction_id``.
  - a CHECK constraint keeps a row from ever claiming to belong to both a
    finding AND a task at once (historical rows: finding only; new >=90%
    rows: task only; new <90% rows: neither).
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0106_field_correction_task"
down_revision = "0105_scrappage_discount_col"
branch_labels = None
depends_on = None

_TABLE = "auditcore.journey_document_field_correction_proposals"


def upgrade() -> None:
    conn = op.get_bind()
    # The old PK's own constraint implies NOT NULL on audit_finding_id --
    # that has to go first, or DROP NOT NULL below fails with "column
    # audit_finding_id is in a primary key" (confirmed live in CI).
    conn.execute(
        text(
            f"ALTER TABLE {_TABLE} DROP CONSTRAINT journey_document_field_correction_proposals_pkey"
        )
    )
    conn.execute(
        text(
            f"""
            ALTER TABLE {_TABLE}
                ADD COLUMN correction_id uuid NOT NULL DEFAULT gen_random_uuid(),
                ADD COLUMN workflow_task_id uuid,
                ALTER COLUMN audit_finding_id DROP NOT NULL
            """
        )
    )
    conn.execute(
        text(f"ALTER TABLE {_TABLE} ADD PRIMARY KEY (tenant_id, correction_id)")
    )
    conn.execute(
        text(
            f"""
            ALTER TABLE {_TABLE}
                ADD CONSTRAINT fk_field_correction_proposals_task
                    FOREIGN KEY (tenant_id, workflow_task_id)
                    REFERENCES auditcore.workflow_tasks (tenant_id, workflow_task_id),
                ADD CONSTRAINT chk_field_correction_proposals_single_owner
                    CHECK (num_nonnulls(audit_finding_id, workflow_task_id) <= 1)
            """
        )
    )
    conn.execute(
        text(
            f"""
            CREATE INDEX ix_document_field_correction_proposals_task
                ON {_TABLE} (tenant_id, workflow_task_id)
                WHERE workflow_task_id IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text("DROP INDEX IF EXISTS auditcore.ix_document_field_correction_proposals_task")
    )
    conn.execute(
        text(
            f"""
            ALTER TABLE {_TABLE}
                DROP CONSTRAINT IF EXISTS chk_field_correction_proposals_single_owner,
                DROP CONSTRAINT IF EXISTS fk_field_correction_proposals_task
            """
        )
    )
    # Rows created after 0106 (workflow_task_id set, audit_finding_id NULL)
    # cannot satisfy the old NOT NULL / old PK on downgrade -- this is a
    # forward-fix migration, not intended to round-trip live post-cutover
    # data; deleting them here would be the wrong silent choice, so this
    # only restores the constraint shape for a downgrade run before any
    # such row exists (matching this repo's own downgrade conventions,
    # which favor a clean schema revert over silent data loss).
    conn.execute(
        text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT journey_document_field_correction_proposals_pkey")
    )
    conn.execute(
        text(
            f"""
            ALTER TABLE {_TABLE}
                ALTER COLUMN audit_finding_id SET NOT NULL,
                DROP COLUMN workflow_task_id,
                DROP COLUMN correction_id
            """
        )
    )
    conn.execute(
        text(f"ALTER TABLE {_TABLE} ADD PRIMARY KEY (tenant_id, audit_finding_id)")
    )
