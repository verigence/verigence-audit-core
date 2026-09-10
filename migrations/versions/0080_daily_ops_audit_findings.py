"""Let an Audit Flag be raised against a Daily Operations run, not just a journey.

Revision ID: 0080_daily_ops_findings
Revises: 0079_scrappage_cert_values
Create Date: 2026-09-10

Every audit_findings row today is hard-tied to a journey (journey_id is
NOT NULL with a foreign key to auditcore.journeys). There was no way to
raise a flag against something that genuinely isn't a journey -- a cash
count discrepancy on a Daily Operations run, a run left open past business
close, an outlet-level process gap.

auditcore.daily_ops_runs already exists (per-outlet, per-business-date PC
run) and is the second subject Audit Flags need to attach to. Rather than
retrofit the whole tightly-coupled Booking/Delivery flag module
(uc03_audit_flags.py -- journey_stage_states' own per-stage aggregate
version, evidence linked to journey documents, Booking/Delivery
completion gating), this makes the shared audit_findings/audit_finding_
events tables subject-flexible and a new, smaller, purpose-built module
(uc03_daily_ops_flags.py) owns the Daily Ops side -- see that module's own
docstring for the full reasoning.

subject_kind is the discriminator; exactly one of journey_id/
daily_ops_run_id is set, enforced by CHECK. stage_code stays NOT NULL on
audit_finding_events (unchanged shape for the existing BOOKING/DELIVERY/
POST_DELIVERY rows) with 'DAILY_OPS' added as a valid value for the new
subject rather than making it nullable -- less disruptive than reopening
that column's nullability for every existing caller.
"""
from __future__ import annotations

from alembic import op

revision = "0080_daily_ops_findings"
down_revision = "0079_scrappage_cert_values"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.audit_findings
            ALTER COLUMN journey_id DROP NOT NULL,
            ADD COLUMN daily_ops_run_id uuid,
            ADD COLUMN subject_kind varchar(20) NOT NULL DEFAULT 'JOURNEY'
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.audit_findings
        ADD CONSTRAINT ck_audit_findings_subject_kind
        CHECK (subject_kind IN ('JOURNEY', 'DAILY_OPS'))
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.audit_findings
        ADD CONSTRAINT ck_audit_findings_subject_ref
        CHECK (
            (subject_kind = 'JOURNEY' AND journey_id IS NOT NULL AND daily_ops_run_id IS NULL)
            OR
            (subject_kind = 'DAILY_OPS' AND daily_ops_run_id IS NOT NULL AND journey_id IS NULL)
        )
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.audit_findings
        ADD CONSTRAINT fk_audit_findings_daily_ops_run
        FOREIGN KEY (tenant_id, daily_ops_run_id)
        REFERENCES auditcore.daily_ops_runs(tenant_id, daily_ops_run_id)
        """
    )
    op.execute(
        "CREATE INDEX ix_audit_findings_daily_ops_run "
        "ON auditcore.audit_findings (tenant_id, daily_ops_run_id) "
        "WHERE daily_ops_run_id IS NOT NULL"
    )

    op.execute(
        """
        ALTER TABLE auditcore.audit_finding_events
            ALTER COLUMN journey_id DROP NOT NULL,
            ADD COLUMN daily_ops_run_id uuid
        """
    )
    op.execute(
        "ALTER TABLE auditcore.audit_finding_events "
        "DROP CONSTRAINT audit_finding_events_stage_code_check"
    )
    op.execute(
        """
        ALTER TABLE auditcore.audit_finding_events
        ADD CONSTRAINT audit_finding_events_stage_code_check
        CHECK (stage_code IN ('BOOKING', 'DELIVERY', 'POST_DELIVERY', 'DAILY_OPS'))
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.audit_finding_events
        ADD CONSTRAINT fk_audit_finding_events_daily_ops_run
        FOREIGN KEY (tenant_id, daily_ops_run_id)
        REFERENCES auditcore.daily_ops_runs(tenant_id, daily_ops_run_id)
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.audit_finding_events "
        "DROP CONSTRAINT IF EXISTS fk_audit_finding_events_daily_ops_run"
    )
    op.execute(
        "ALTER TABLE auditcore.audit_finding_events "
        "DROP CONSTRAINT IF EXISTS audit_finding_events_stage_code_check"
    )
    op.execute(
        """
        ALTER TABLE auditcore.audit_finding_events
        ADD CONSTRAINT audit_finding_events_stage_code_check
        CHECK (stage_code IN ('BOOKING', 'DELIVERY', 'POST_DELIVERY'))
        """
    )
    op.execute(
        "ALTER TABLE auditcore.audit_finding_events "
        "DROP COLUMN IF EXISTS daily_ops_run_id"
    )
    op.execute(
        "ALTER TABLE auditcore.audit_finding_events "
        "ALTER COLUMN journey_id SET NOT NULL"
    )

    op.execute("DROP INDEX IF EXISTS auditcore.ix_audit_findings_daily_ops_run")
    op.execute(
        "ALTER TABLE auditcore.audit_findings "
        "DROP CONSTRAINT IF EXISTS fk_audit_findings_daily_ops_run"
    )
    op.execute(
        "ALTER TABLE auditcore.audit_findings "
        "DROP CONSTRAINT IF EXISTS ck_audit_findings_subject_ref"
    )
    op.execute(
        "ALTER TABLE auditcore.audit_findings "
        "DROP CONSTRAINT IF EXISTS ck_audit_findings_subject_kind"
    )
    op.execute(
        "ALTER TABLE auditcore.audit_findings "
        "DROP COLUMN IF EXISTS subject_kind, "
        "DROP COLUMN IF EXISTS daily_ops_run_id"
    )
    op.execute(
        "ALTER TABLE auditcore.audit_findings "
        "ALTER COLUMN journey_id SET NOT NULL"
    )
