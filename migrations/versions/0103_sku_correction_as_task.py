"""Rework model-selection correction proposals onto the Task Queue,
not a rule-classified audit finding.

Revision ID: 0103_sku_correction_task
Revises: 0102_sku_corrections
Create Date: 2026-09-16

Direct user correction of 0102's own design: a PC-proposed SKU correction
is a human review-and-decide workflow item, not a rule-detected violation
-- Audit Review should stay reserved for what a rule actually found, and
the Task Queue already gives PC/TL full workflow visibility for a given
Journey. 0102's ``model_selection_correction_proposals`` (keyed 1:1 with
an ``audit_findings`` row, adjudicated through the finding-verdict
machinery) is replaced here with the same shape keyed 1:1 with a
``workflow_tasks`` row instead, decided through the ordinary Task Queue
Complete/Cancel actions (``uc03_model_selection_corrections.py`` /
``tasks_api.py``).

0102 shipped only hours earlier with no UI wired to it yet -- there is no
real proposal data to migrate, so this drops and recreates rather than
carrying an ``audit_finding_id`` column forward unused.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0103_sku_correction_task"
down_revision = "0102_sku_corrections"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP TABLE IF EXISTS auditcore.model_selection_correction_proposals CASCADE"))
    conn.execute(
        text(
            """
            CREATE TABLE auditcore.model_selection_correction_proposals (
                tenant_id                varchar(128) NOT NULL,
                workflow_task_id         uuid NOT NULL,
                journey_id               uuid NOT NULL,
                previous_product_sku_id  uuid NOT NULL REFERENCES auditcore.product_skus(product_sku_id),
                proposed_product_sku_id  uuid NOT NULL REFERENCES auditcore.product_skus(product_sku_id),
                reason                   text NOT NULL,
                proposed_by_actor_id     varchar(160) NOT NULL,
                applied_at_utc           timestamptz,
                created_at_utc           timestamptz NOT NULL DEFAULT now(),
                updated_at_utc           timestamptz NOT NULL DEFAULT now(),
                version_no               bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),

                PRIMARY KEY (tenant_id, workflow_task_id),
                FOREIGN KEY (tenant_id, workflow_task_id)
                    REFERENCES auditcore.workflow_tasks(tenant_id, workflow_task_id),
                FOREIGN KEY (tenant_id, journey_id)
                    REFERENCES auditcore.journeys(tenant_id, journey_id)
            )
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX ix_model_selection_correction_proposals_journey
                ON auditcore.model_selection_correction_proposals (tenant_id, journey_id, created_at_utc DESC)
            """
        )
    )
    conn.execute(
        text(
            "ALTER TABLE auditcore.model_selection_correction_proposals ENABLE ROW LEVEL SECURITY"
        )
    )
    conn.execute(
        text(
            "ALTER TABLE auditcore.model_selection_correction_proposals FORCE ROW LEVEL SECURITY"
        )
    )
    conn.execute(
        text(
            """
            CREATE POLICY tenant_isolation_model_selection_correction_proposals
            ON auditcore.model_selection_correction_proposals
            USING (tenant_id = auditcore.current_tenant_id())
            WITH CHECK (tenant_id = auditcore.current_tenant_id())
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE TRIGGER trg_model_selection_correction_proposals_updated
                BEFORE UPDATE ON auditcore.model_selection_correction_proposals
                FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at()
            """
        )
    )
    conn.execute(
        text(
            f"""
            GRANT SELECT, INSERT, UPDATE ON auditcore.model_selection_correction_proposals
            TO {_RUNTIME_ROLE}
            """
        )
    )
    conn.execute(
        text(
            f"REVOKE DELETE ON auditcore.model_selection_correction_proposals FROM {_RUNTIME_ROLE}"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP TABLE IF EXISTS auditcore.model_selection_correction_proposals CASCADE"))
