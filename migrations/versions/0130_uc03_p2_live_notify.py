"""0130_uc03_p2_live_notify — push Journey changes to open screens.

The PC watches documents being uploaded, identified and read. Instead of
the screen polling, every change that matters to that view -- a page or
document status, an upload batch, a task, the stage -- sends a NOTIFY on
channel ``p2_journey`` with ``<tenant_id>|<journey_id>``. Audit Core
listens once per process and pushes a fresh status to each open screen
(uc03_p2_live). Postgres folds identical notifications inside one
transaction, so a batch of page updates is one push.
"""
from __future__ import annotations

from alembic import op

revision = "0130_uc03_p2_live_notify"
down_revision = "0129_uc03_p2_vehicle_identity"
branch_labels = None
depends_on = None

_TRIGGERS = (
    # table, trigger, events, condition
    ("p2_document_queue", "trg_p2_document_queue_notify", "INSERT OR UPDATE",
     ("TG_OP = 'INSERT' OR OLD.queue_status IS DISTINCT FROM NEW.queue_status"
      " OR OLD.template_key IS DISTINCT FROM NEW.template_key"
      " OR OLD.classified_document_type IS DISTINCT FROM NEW.classified_document_type")),
    ("p2_upload_batches", "trg_p2_upload_batches_notify", "INSERT OR UPDATE",
     "TG_OP = 'INSERT' OR OLD.batch_status IS DISTINCT FROM NEW.batch_status"),
    ("p2_tasks", "trg_p2_tasks_notify", "INSERT OR UPDATE",
     "TG_OP = 'INSERT' OR OLD.task_status IS DISTINCT FROM NEW.task_status"),
    ("p2_journey_runtime", "trg_p2_journey_runtime_notify", "INSERT OR UPDATE",
     ("TG_OP = 'INSERT' OR OLD.current_stage IS DISTINCT FROM NEW.current_stage"
      " OR OLD.booking_completion_state IS DISTINCT FROM NEW.booking_completion_state"
      " OR OLD.delivery_completion_state IS DISTINCT FROM NEW.delivery_completion_state")),
    ("p2_activity_events", "trg_p2_activity_events_notify", "INSERT", "true"),
)


def upgrade() -> None:
    for table, trigger, _, condition in _TRIGGERS:
        # One small function per table: trigger WHEN clauses cannot mention
        # OLD on INSERT, so the change test lives in the function.
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION auditcore.{trigger}_fn() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
              IF {condition} THEN
                PERFORM pg_notify('p2_journey', NEW.tenant_id || '|' || NEW.journey_id::text);
              END IF;
              RETURN NULL;
            END;
            $$
            """
        )
    for table, trigger, events, _ in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON auditcore.{table}")
        op.execute(
            f"CREATE TRIGGER {trigger} AFTER {events} ON auditcore.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION auditcore.{trigger}_fn()"
        )


def downgrade() -> None:
    for table, trigger, _, _ in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON auditcore.{table}")
        op.execute(f"DROP FUNCTION IF EXISTS auditcore.{trigger}_fn()")
