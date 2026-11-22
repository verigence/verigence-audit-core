"""0254_asked_again_while_running -- work asked for again while it runs is run when it was asked for (2026-11-22).

Product owner, 22 Nov 2026: "how can a file wait for 1 hour ... we need to
introspect and do regression and fix". Four documents of one Journey sat QUEUED
for an hour and were failed "Not read within 30 minutes" without ever being
sent to Document Intelligence; 109 pages in 20 Journeys the day before.

While a Journey's pages are being read, a check's task is held back and a
follow-up recompute is asked for "in ten minutes" (uc03_p2_task_producer, 16
Nov). Asked from inside the running recompute, the delay was dropped: a
running item's next_attempt_at_utc was left alone by every request, and the
worker ran any item asked for again while it ran straight away. The recompute
ran every 7 seconds for as long as a page was waiting, and the pages waited
behind it (the worker's claim rule, fixed in the same change).

From this change a running item's next_attempt_at_utc holds when it was asked
for again -- now() for "at once" -- and the worker runs it again then
(uc03_p2_worker._complete; uc03_p2_runtime.enqueue_work keeps the sooner
time). These two functions ask for work "at once" from SQL, so on a running
item they now say so. Nothing else in them changes.

With the worker of before this change the new value is harmless: it cleared
the column on finishing whatever it held.
"""
from __future__ import annotations

from alembic import op

revision = "0254_asked_again_while_running"
down_revision = "0253_dms_strong_match_sync"
branch_labels = None
depends_on = None

_REQUEST_STAGE_RECOMPUTE = r"""
        CREATE OR REPLACE FUNCTION auditcore.p2_request_stage_recompute(
            p_tenant_id varchar,
            p_journey_id uuid
        )
        RETURNS bigint
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_version bigint;
        BEGIN
            INSERT INTO auditcore.p2_journey_runtime (
                tenant_id, journey_id, fact_version
            ) VALUES (
                p_tenant_id, p_journey_id, 1
            )
            ON CONFLICT (tenant_id, journey_id)
            DO UPDATE SET fact_version = auditcore.p2_journey_runtime.fact_version + 1,
                          updated_at_utc = now()
            RETURNING fact_version INTO v_version;

            INSERT INTO auditcore.p2_work_queue (
                tenant_id, journey_id, work_type, work_key,
                payload, requested_version, work_status
            ) VALUES (
                p_tenant_id, p_journey_id, 'STAGE_RECOMPUTE',
                'booking:' || p_journey_id::text,
                jsonb_build_object('stage', 'BOOKING'),
                v_version,
                'PENDING'
            )
            ON CONFLICT (tenant_id, work_type, work_key)
            DO UPDATE SET requested_version = GREATEST(
                              COALESCE(auditcore.p2_work_queue.requested_version, 0),
                              EXCLUDED.requested_version
                          ),
                          payload = EXCLUDED.payload,
                          work_status = CASE
                              WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                                  THEN auditcore.p2_work_queue.work_status
                              ELSE 'PENDING'
                          END,
                          next_attempt_at_utc = CASE
                              WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                                  THEN {running}
                              ELSE NULL
                          END,
                          last_error = CASE
                              WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                                  THEN auditcore.p2_work_queue.last_error
                              ELSE NULL
                          END,
                          queued_at_utc = CASE
                              WHEN auditcore.p2_work_queue.work_status
                                   IN ('PENDING','RETRY_WAIT','CLAIMED','PROCESSING')
                                  THEN auditcore.p2_work_queue.queued_at_utc
                              ELSE now()
                          END,
                          updated_at_utc = now();

            PERFORM auditcore.p2_requeue_verifying_controls(
                p_tenant_id, p_journey_id, v_version
            );

            RETURN v_version;
        END;
        $$;
"""

_REQUEUE_VERIFYING_CONTROLS = r"""
        CREATE OR REPLACE FUNCTION auditcore.p2_requeue_verifying_controls(
            p_tenant_id varchar,
            p_journey_id uuid,
            p_fact_version bigint
        )
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        BEGIN
            INSERT INTO auditcore.p2_work_queue (
                tenant_id, journey_id, work_type, work_key,
                payload, requested_version, work_status
            )
            SELECT DISTINCT
                t.tenant_id,
                t.journey_id,
                'CONTROL_EVALUATE',
                t.journey_id::text || ':' || t.source_code,
                jsonb_build_object(
                    'controlCode', t.source_code,
                    'reason', 'FACT_CHANGED'
                ),
                p_fact_version,
                'PENDING'
            FROM auditcore.p2_tasks t
            WHERE t.tenant_id=p_tenant_id
              AND t.journey_id=p_journey_id
              AND t.task_status='VERIFYING'
              AND t.source_type='RULE'
              AND t.source_code IS NOT NULL
            ON CONFLICT (tenant_id, work_type, work_key)
            DO UPDATE SET
                payload=EXCLUDED.payload,
                requested_version=GREATEST(
                    COALESCE(auditcore.p2_work_queue.requested_version,0),
                    COALESCE(EXCLUDED.requested_version,0)
                ),
                work_status=CASE
                    WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                      THEN auditcore.p2_work_queue.work_status
                    ELSE 'PENDING'
                END,
                next_attempt_at_utc=CASE
                    WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                      THEN {running}
                    ELSE NULL
                END,
                updated_at_utc=now();
        END;
        $$;
"""

# Asked again at once, while it runs: the worker runs it again as it finishes.
_AT_ONCE = "now()"
# Before: the column was left alone, and the worker cleared it on finishing.
_LEFT_ALONE = "auditcore.p2_work_queue.next_attempt_at_utc"


def upgrade() -> None:
    op.execute(_REQUEST_STAGE_RECOMPUTE.format(running=_AT_ONCE))
    op.execute(_REQUEUE_VERIFYING_CONTROLS.format(running=_AT_ONCE))


def downgrade() -> None:
    op.execute(_REQUEST_STAGE_RECOMPUTE.format(running=_LEFT_ALONE))
    op.execute(_REQUEUE_VERIFYING_CONTROLS.format(running=_LEFT_ALONE))
