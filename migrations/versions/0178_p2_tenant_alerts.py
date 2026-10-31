"""0178_p2_tenant_alerts -- an alert every signed-in member of a Project sees (2026-10-31).

Product owner: "When ever a PC enters a Journey which has existing PAN, Mobile
No, Customer Name, Model same, Pls raise an Alert across all PC telling Same
File Being Uploaded. Give the name of customer, Model, PC Name and Dealership,
and delete the journey completely. This alert should be seen by all team on
their respective screens, if they are active."

``p2_tenant_alerts`` holds one row per alert, for the whole Project (tenant),
not one Journey. A same-file alert also marks its Journey as handled: the
unique key (tenant, kind, subject journey) means a recomputed check never
alerts twice, and the row outlives the Journey it describes (no foreign key --
the new file is deleted by design). An alert is PENDING while the new file is
being deleted and PUBLISHED, with its expiry, once the outcome is known; the
screens poll only PUBLISHED, unexpired rows.

The work queue gains SAME_FILE_DELETE: the deletion runs as its own work item,
after the check that found it has committed, never inside it.
"""
from __future__ import annotations

from alembic import op

revision = "0178_p2_tenant_alerts"
down_revision = "0177_activity_minutes"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLE = "p2_tenant_alerts"
_WORK_TYPES = (
    "'SPLIT_BATCH','DOCUMENT_INGEST','DOCUMENT_RECONCILE','JOURNEY_RECONCILE',"
    "'STAGE_RECOMPUTE','CONTROL_EVALUATE','TASK_VERIFY','BATCH_GROUP','PAGE_REREAD',"
    "'DI_DOCUMENT_WITHDRAW'"
)


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_TABLE} (
            alert_id                  uuid         NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
            tenant_id                 varchar(128) NOT NULL,
            alert_kind                varchar(40)  NOT NULL CHECK (alert_kind IN ('SAME_FILE_UPLOADED')),
            subject_journey_id        uuid,
            subject_journey_reference varchar(200),
            related_journey_id        uuid,
            alert_status              varchar(20)  NOT NULL DEFAULT 'PENDING'
                                      CHECK (alert_status IN ('PENDING', 'PUBLISHED')),
            outcome                   varchar(40),
            title                     varchar(300) NOT NULL,
            body                      text         NOT NULL,
            payload                   jsonb        NOT NULL DEFAULT '{{}}'::jsonb,
            created_at_utc            timestamptz  NOT NULL DEFAULT now(),
            published_at_utc          timestamptz,
            expires_at_utc            timestamptz,
            updated_at_utc            timestamptz  NOT NULL DEFAULT now(),
            CONSTRAINT uq_{_TABLE}_subject UNIQUE (tenant_id, alert_kind, subject_journey_id)
        )
        """
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_active ON auditcore.{_TABLE} "
        f"(tenant_id, published_at_utc DESC) WHERE alert_status = 'PUBLISHED'"
    )
    op.execute(f"ALTER TABLE auditcore.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON auditcore.{_TABLE}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_TABLE} ON auditcore.{_TABLE} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.{_TABLE} TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE DELETE ON auditcore.{_TABLE} FROM {_RUNTIME_ROLE}")
    op.execute(
        f"""
        ALTER TABLE auditcore.p2_work_queue
          DROP CONSTRAINT IF EXISTS p2_work_queue_work_type_check,
          ADD CONSTRAINT p2_work_queue_work_type_check
            CHECK (work_type IN ({_WORK_TYPES},'SAME_FILE_DELETE'));
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DELETE FROM auditcore.p2_work_queue WHERE work_type='SAME_FILE_DELETE';
        ALTER TABLE auditcore.p2_work_queue
          DROP CONSTRAINT IF EXISTS p2_work_queue_work_type_check,
          ADD CONSTRAINT p2_work_queue_work_type_check CHECK (work_type IN ({_WORK_TYPES}));
        DROP TABLE IF EXISTS auditcore.{_TABLE};
        """
    )
