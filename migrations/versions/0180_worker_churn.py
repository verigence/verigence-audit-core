"""0180_worker_churn -- a run that changed nothing asks for no other; the queue's age is honest (2026-10-31).

Product owner, System Health: "318 work item(s) waiting for more than 30
minutes (oldest since 2026-10-19)", 9 deadlocks, and does Audit Core need more
replicas? It does not (the worker peaked at 0.59 of 8 cores): the queue was
asking itself for work.

- auditcore.p2_manual_task_change_trigger: a MANUAL_VERIFICATION_REVIEW task
  opened or closed asks for a recompute -- unless the transaction is the
  worker's own recompute (audit_core.p2_derived_writes, set by
  uc03_p2_runtime.mark_derived_writes), which closes that task *because* it
  read the PC's confirmation. Before, each such close bumped the Journey's
  fact version silently, the recompute could not store its fingerprint (the
  version had moved), the 30-second fact sweep then asked again, and the
  Journey ran a second recompute and all five control units for nothing
  (live, journey 0a523608 on 31 Oct: FIELD_CORRECTED v51, silent v52,
  FACT_SWEEP v53, two recomputes and ten unit runs for one correction). Since
  19 Oct the Project has 4,632 fact versions with no FACTS_CHANGED record
  (this trigger) and 9,030 FACT_SWEEP requests next to 7,954 announced fact
  changes; its review tasks were raised or closed by the system 2,766 times
  and acted on by a PC 33 times.
- auditcore.p2_request_stage_recompute: a recompute asked for again after it
  finished joins the back of the line (queued_at_utc = now()), as every other
  request has since 0175. It kept its first queue time, so a Journey's
  recompute ran ahead of other Journeys' work and the health page dated it to
  the Journey's first day.
- platform_ops.pipeline_health: an open work item is "waiting" from when it
  last became due (queued_at_utc, or the end of its retry delay), not from
  when its reused row was created; finished counts are the items settled in
  the window (updated_at_utc), not the rows first created in it.
"""
from __future__ import annotations

from alembic import op

revision = "0180_worker_churn"
down_revision = "0179_neon_db_minutes"
branch_labels = None
depends_on = None

_MANUAL_TASK_TRIGGER = r"""
        CREATE OR REPLACE FUNCTION auditcore.p2_manual_task_change_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.task_type <> 'MANUAL_VERIFICATION_REVIEW' THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE'
               AND NEW.task_status IS NOT DISTINCT FROM OLD.task_status THEN
                RETURN NEW;
            END IF;
            {derived_guard}
            PERFORM auditcore.p2_request_stage_recompute(
                NEW.tenant_id,
                NEW.journey_id
            );
            RETURN NEW;
        END;
        $$;
"""

_DERIVED_GUARD = """-- The worker's recompute closing the task it reviewed: no new fact.
            IF current_setting('audit_core.p2_derived_writes', true) = 'on' THEN
                RETURN NEW;
            END IF;"""

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
                                  THEN auditcore.p2_work_queue.next_attempt_at_utc
                              ELSE NULL
                          END,
                          last_error = CASE
                              WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                                  THEN auditcore.p2_work_queue.last_error
                              ELSE NULL
                          END,
                          {queued_at}
                          updated_at_utc = now();

            PERFORM auditcore.p2_requeue_verifying_controls(
                p_tenant_id, p_journey_id, v_version
            );

            RETURN v_version;
        END;
        $$;
"""

# The same rule as uc03_p2_runtime.enqueue_work (0175): an item already
# waiting keeps its place, one that had finished joins the back of the line,
# one running joins it when it finishes (the worker's _complete).
_QUEUED_AT = """queued_at_utc = CASE
                              WHEN auditcore.p2_work_queue.work_status
                                   IN ('PENDING','RETRY_WAIT','CLAIMED','PROCESSING')
                                  THEN auditcore.p2_work_queue.queued_at_utc
                              ELSE now()
                          END,"""

_PIPELINE_HEALTH_UP = r"""
        CREATE OR REPLACE FUNCTION platform_ops.pipeline_health(p_since timestamptz)
        RETURNS jsonb
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $fn$
        DECLARE
          core jsonb;
          part jsonb;
          di jsonb := NULL;
        BEGIN
          SELECT jsonb_build_object(
            'documentQueue', COALESCE((
              SELECT jsonb_agg(jsonb_build_object(
                'tenantId', tenant_id, 'status', queue_status, 'count', n,
                'over30m', over_30m, 'oldestAtUtc', oldest))
              FROM (
                SELECT tenant_id, queue_status, count(*) AS n,
                       count(*) FILTER (WHERE queue_status IN (
                         'QUEUED','PREPARING_PAGE','DI_UPLOAD_PREPARING','DI_UPLOADING','DI_FINALIZING',
                         'CLASSIFYING','EXTRACTING','SYNCING_TO_AUDIT_CORE','RETRY_WAIT')
                         AND created_at_utc < now() - interval '30 minutes') AS over_30m,
                       min(created_at_utc) AS oldest
                FROM auditcore.p2_document_queue
                WHERE retired_at_utc IS NULL
                  AND (created_at_utc >= p_since OR queue_status IN (
                    'QUEUED','PREPARING_PAGE','DI_UPLOAD_PREPARING','DI_UPLOADING','DI_FINALIZING',
                    'CLASSIFYING','EXTRACTING','SYNCING_TO_AUDIT_CORE','RETRY_WAIT'))
                GROUP BY 1, 2
              ) q), '[]'::jsonb),
            'documentFailureReasons', COALESCE((
              SELECT jsonb_agg(jsonb_build_object('tenantId', tenant_id, 'reason', reason, 'count', n) ORDER BY n DESC)
              FROM (
                SELECT tenant_id, left(COALESCE(NULLIF(last_error, ''), NULLIF(status_reason, ''), 'unknown'), 120) AS reason,
                       count(*) AS n
                FROM auditcore.p2_document_queue
                WHERE queue_status IN ('FAILED','DEAD_LETTER') AND updated_at_utc >= p_since
                GROUP BY 1, 2
                ORDER BY 3 DESC
                LIMIT 25
              ) r), '[]'::jsonb),
            'workQueue', COALESCE((
              SELECT jsonb_agg(jsonb_build_object(
                'tenantId', tenant_id, 'status', work_status, 'count', n,
                'over30m', over_30m, 'oldestAtUtc', oldest))
              FROM (
                -- An item is waiting from when it last became due: it joined
                -- the line (queued_at_utc) and any retry delay ran out. Not
                -- created_at_utc: a Journey's recompute and check rows are
                -- reused for every request, so their creation time is the
                -- Journey's first day (2026-10-31: "318 waiting for more
                -- than 30 minutes, oldest since 19 Oct" when none was).
                SELECT tenant_id, work_status, count(*) AS n,
                       count(*) FILTER (WHERE work_status IN ('PENDING','CLAIMED','PROCESSING','RETRY_WAIT')
                         AND GREATEST(queued_at_utc, COALESCE(next_attempt_at_utc, queued_at_utc))
                             < now() - interval '30 minutes') AS over_30m,
                       min(GREATEST(queued_at_utc, COALESCE(next_attempt_at_utc, queued_at_utc))) AS oldest
                FROM auditcore.p2_work_queue
                WHERE updated_at_utc >= p_since OR work_status IN ('PENDING','CLAIMED','PROCESSING','RETRY_WAIT')
                GROUP BY 1, 2
              ) w), '[]'::jsonb),
            'workFailureReasons', COALESCE((
              SELECT jsonb_agg(jsonb_build_object('tenantId', tenant_id, 'workType', work_type, 'reason', reason, 'count', n)
                               ORDER BY n DESC)
              FROM (
                SELECT tenant_id, work_type, left(COALESCE(NULLIF(last_error, ''), 'unknown'), 120) AS reason, count(*) AS n
                FROM auditcore.p2_work_queue
                WHERE work_status = 'DEAD_LETTER' AND updated_at_utc >= p_since
                GROUP BY 1, 2, 3
                ORDER BY 4 DESC
                LIMIT 25
              ) r), '[]'::jsonb),
            'deadLetters', COALESCE((
              SELECT jsonb_agg(jsonb_build_object('tenantId', tenant_id, 'open', open_n, 'since', since_n))
              FROM (
                SELECT tenant_id, count(*) FILTER (WHERE resolution_status = 'OPEN') AS open_n,
                       count(*) FILTER (WHERE dead_lettered_at_utc >= p_since) AS since_n
                FROM auditcore.workflow_dead_letters
                GROUP BY 1
                HAVING count(*) FILTER (WHERE resolution_status = 'OPEN') > 0
                    OR count(*) FILTER (WHERE dead_lettered_at_utc >= p_since) > 0
              ) d), '[]'::jsonb),
            'outbox', COALESCE((
              SELECT jsonb_agg(jsonb_build_object('tenantId', tenant_id, 'status', event_status, 'count', n,
                                                  'oldestAtUtc', oldest))
              FROM (
                SELECT tenant_id, event_status, count(*) AS n, min(occurred_at_utc) AS oldest
                FROM auditcore.outbox_events
                WHERE event_status <> 'PUBLISHED'
                GROUP BY 1, 2
              ) o), '[]'::jsonb),
            'inbox', COALESCE((
              SELECT jsonb_agg(jsonb_build_object('tenantId', tenant_id, 'status', processing_status, 'count', n))
              FROM (
                SELECT tenant_id, processing_status, count(*) AS n
                FROM auditcore.inbox_events
                WHERE processing_status IN ('FAILED','RECEIVED','PROCESSING')
                  AND (processing_status = 'FAILED' OR received_at_utc < now() - interval '30 minutes')
                GROUP BY 1, 2
              ) i), '[]'::jsonb),
            'workerJobs', COALESCE((
              SELECT jsonb_agg(jsonb_build_object('kind', job_kind, 'status', status, 'count', n))
              FROM (
                SELECT job_kind, status, count(*) AS n
                FROM auditcore.p2_worker_jobs
                WHERE requested_at_utc >= p_since
                GROUP BY 1, 2
              ) j), '[]'::jsonb)
          ) INTO core;

          -- Document Intelligence lives in its own schema, which may not exist (a fresh
          -- Audit Core database). Read it dynamically.
          IF to_regclass('docintel.processing_jobs') IS NOT NULL THEN
            EXECUTE $q$
              SELECT jsonb_build_object(
                'processingJobs', COALESCE((
                  SELECT jsonb_agg(jsonb_build_object('status', job_status, 'count', n))
                  FROM (SELECT job_status, count(*) AS n FROM docintel.processing_jobs
                        WHERE created_at_utc >= $1 GROUP BY 1) p), '[]'::jsonb),
                'processingErrors', COALESCE((
                  SELECT jsonb_agg(jsonb_build_object('errorCode', error_code, 'count', n) ORDER BY n DESC)
                  FROM (SELECT COALESCE(error_code, 'unknown') AS error_code, count(*) AS n
                        FROM docintel.processing_jobs
                        WHERE job_status = 'FAILED' AND created_at_utc >= $1
                        GROUP BY 1 ORDER BY 2 DESC LIMIT 15) e), '[]'::jsonb)
              )
            $q$ INTO di USING p_since;
          END IF;
          IF to_regclass('docintel.document_capture_v2_classification_jobs') IS NOT NULL THEN
            EXECUTE $q$
              SELECT COALESCE(jsonb_agg(jsonb_build_object('status', job_status, 'count', n)), '[]'::jsonb)
              FROM (SELECT job_status, count(*) AS n FROM docintel.document_capture_v2_classification_jobs
                    WHERE created_at_utc >= $1 GROUP BY 1) c
            $q$ INTO part USING p_since;
            di := COALESCE(di, '{}'::jsonb) || jsonb_build_object('classificationJobs', part);
          END IF;
          IF to_regclass('docintel.scheduler_runs') IS NOT NULL THEN
            EXECUTE $q$
              SELECT COALESCE(jsonb_agg(jsonb_build_object('runName', run_name, 'runDate', run_date,
                       'startedAtUtc', started_at_utc, 'completedAtUtc', completed_at_utc, 'errorCode', error_code)
                       ORDER BY started_at_utc DESC), '[]'::jsonb)
              FROM (SELECT * FROM docintel.scheduler_runs
                    WHERE started_at_utc >= $1 AND (error_code IS NOT NULL OR completed_at_utc IS NULL)
                    ORDER BY started_at_utc DESC LIMIT 20) s
            $q$ INTO part USING p_since;
            di := COALESCE(di, '{}'::jsonb) || jsonb_build_object('schedulerProblems', part);
          END IF;
          RETURN core || jsonb_build_object('documentIntelligence', di);
        END
        $fn$;
"""

_PIPELINE_HEALTH_DOWN = r"""
        CREATE OR REPLACE FUNCTION platform_ops.pipeline_health(p_since timestamptz)
        RETURNS jsonb
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $fn$
        DECLARE
          core jsonb;
          part jsonb;
          di jsonb := NULL;
        BEGIN
          SELECT jsonb_build_object(
            'documentQueue', COALESCE((
              SELECT jsonb_agg(jsonb_build_object(
                'tenantId', tenant_id, 'status', queue_status, 'count', n,
                'over30m', over_30m, 'oldestAtUtc', oldest))
              FROM (
                SELECT tenant_id, queue_status, count(*) AS n,
                       count(*) FILTER (WHERE queue_status IN (
                         'QUEUED','PREPARING_PAGE','DI_UPLOAD_PREPARING','DI_UPLOADING','DI_FINALIZING',
                         'CLASSIFYING','EXTRACTING','SYNCING_TO_AUDIT_CORE','RETRY_WAIT')
                         AND created_at_utc < now() - interval '30 minutes') AS over_30m,
                       min(created_at_utc) AS oldest
                FROM auditcore.p2_document_queue
                WHERE retired_at_utc IS NULL
                  AND (created_at_utc >= p_since OR queue_status IN (
                    'QUEUED','PREPARING_PAGE','DI_UPLOAD_PREPARING','DI_UPLOADING','DI_FINALIZING',
                    'CLASSIFYING','EXTRACTING','SYNCING_TO_AUDIT_CORE','RETRY_WAIT'))
                GROUP BY 1, 2
              ) q), '[]'::jsonb),
            'documentFailureReasons', COALESCE((
              SELECT jsonb_agg(jsonb_build_object('tenantId', tenant_id, 'reason', reason, 'count', n) ORDER BY n DESC)
              FROM (
                SELECT tenant_id, left(COALESCE(NULLIF(last_error, ''), NULLIF(status_reason, ''), 'unknown'), 120) AS reason,
                       count(*) AS n
                FROM auditcore.p2_document_queue
                WHERE queue_status IN ('FAILED','DEAD_LETTER') AND updated_at_utc >= p_since
                GROUP BY 1, 2
                ORDER BY 3 DESC
                LIMIT 25
              ) r), '[]'::jsonb),
            'workQueue', COALESCE((
              SELECT jsonb_agg(jsonb_build_object(
                'tenantId', tenant_id, 'status', work_status, 'count', n,
                'over30m', over_30m, 'oldestAtUtc', oldest))
              FROM (
                SELECT tenant_id, work_status, count(*) AS n,
                       count(*) FILTER (WHERE work_status IN ('PENDING','CLAIMED','PROCESSING','RETRY_WAIT')
                         AND created_at_utc < now() - interval '30 minutes') AS over_30m,
                       min(created_at_utc) AS oldest
                FROM auditcore.p2_work_queue
                WHERE created_at_utc >= p_since OR work_status IN ('PENDING','CLAIMED','PROCESSING','RETRY_WAIT')
                GROUP BY 1, 2
              ) w), '[]'::jsonb),
            'workFailureReasons', COALESCE((
              SELECT jsonb_agg(jsonb_build_object('tenantId', tenant_id, 'workType', work_type, 'reason', reason, 'count', n)
                               ORDER BY n DESC)
              FROM (
                SELECT tenant_id, work_type, left(COALESCE(NULLIF(last_error, ''), 'unknown'), 120) AS reason, count(*) AS n
                FROM auditcore.p2_work_queue
                WHERE work_status = 'DEAD_LETTER' AND updated_at_utc >= p_since
                GROUP BY 1, 2, 3
                ORDER BY 4 DESC
                LIMIT 25
              ) r), '[]'::jsonb),
            'deadLetters', COALESCE((
              SELECT jsonb_agg(jsonb_build_object('tenantId', tenant_id, 'open', open_n, 'since', since_n))
              FROM (
                SELECT tenant_id, count(*) FILTER (WHERE resolution_status = 'OPEN') AS open_n,
                       count(*) FILTER (WHERE dead_lettered_at_utc >= p_since) AS since_n
                FROM auditcore.workflow_dead_letters
                GROUP BY 1
                HAVING count(*) FILTER (WHERE resolution_status = 'OPEN') > 0
                    OR count(*) FILTER (WHERE dead_lettered_at_utc >= p_since) > 0
              ) d), '[]'::jsonb),
            'outbox', COALESCE((
              SELECT jsonb_agg(jsonb_build_object('tenantId', tenant_id, 'status', event_status, 'count', n,
                                                  'oldestAtUtc', oldest))
              FROM (
                SELECT tenant_id, event_status, count(*) AS n, min(occurred_at_utc) AS oldest
                FROM auditcore.outbox_events
                WHERE event_status <> 'PUBLISHED'
                GROUP BY 1, 2
              ) o), '[]'::jsonb),
            'inbox', COALESCE((
              SELECT jsonb_agg(jsonb_build_object('tenantId', tenant_id, 'status', processing_status, 'count', n))
              FROM (
                SELECT tenant_id, processing_status, count(*) AS n
                FROM auditcore.inbox_events
                WHERE processing_status IN ('FAILED','RECEIVED','PROCESSING')
                  AND (processing_status = 'FAILED' OR received_at_utc < now() - interval '30 minutes')
                GROUP BY 1, 2
              ) i), '[]'::jsonb),
            'workerJobs', COALESCE((
              SELECT jsonb_agg(jsonb_build_object('kind', job_kind, 'status', status, 'count', n))
              FROM (
                SELECT job_kind, status, count(*) AS n
                FROM auditcore.p2_worker_jobs
                WHERE requested_at_utc >= p_since
                GROUP BY 1, 2
              ) j), '[]'::jsonb)
          ) INTO core;

          -- Document Intelligence lives in its own schema, which may not exist (a fresh
          -- Audit Core database). Read it dynamically.
          IF to_regclass('docintel.processing_jobs') IS NOT NULL THEN
            EXECUTE $q$
              SELECT jsonb_build_object(
                'processingJobs', COALESCE((
                  SELECT jsonb_agg(jsonb_build_object('status', job_status, 'count', n))
                  FROM (SELECT job_status, count(*) AS n FROM docintel.processing_jobs
                        WHERE created_at_utc >= $1 GROUP BY 1) p), '[]'::jsonb),
                'processingErrors', COALESCE((
                  SELECT jsonb_agg(jsonb_build_object('errorCode', error_code, 'count', n) ORDER BY n DESC)
                  FROM (SELECT COALESCE(error_code, 'unknown') AS error_code, count(*) AS n
                        FROM docintel.processing_jobs
                        WHERE job_status = 'FAILED' AND created_at_utc >= $1
                        GROUP BY 1 ORDER BY 2 DESC LIMIT 15) e), '[]'::jsonb)
              )
            $q$ INTO di USING p_since;
          END IF;
          IF to_regclass('docintel.document_capture_v2_classification_jobs') IS NOT NULL THEN
            EXECUTE $q$
              SELECT COALESCE(jsonb_agg(jsonb_build_object('status', job_status, 'count', n)), '[]'::jsonb)
              FROM (SELECT job_status, count(*) AS n FROM docintel.document_capture_v2_classification_jobs
                    WHERE created_at_utc >= $1 GROUP BY 1) c
            $q$ INTO part USING p_since;
            di := COALESCE(di, '{}'::jsonb) || jsonb_build_object('classificationJobs', part);
          END IF;
          IF to_regclass('docintel.scheduler_runs') IS NOT NULL THEN
            EXECUTE $q$
              SELECT COALESCE(jsonb_agg(jsonb_build_object('runName', run_name, 'runDate', run_date,
                       'startedAtUtc', started_at_utc, 'completedAtUtc', completed_at_utc, 'errorCode', error_code)
                       ORDER BY started_at_utc DESC), '[]'::jsonb)
              FROM (SELECT * FROM docintel.scheduler_runs
                    WHERE started_at_utc >= $1 AND (error_code IS NOT NULL OR completed_at_utc IS NULL)
                    ORDER BY started_at_utc DESC LIMIT 20) s
            $q$ INTO part USING p_since;
            di := COALESCE(di, '{}'::jsonb) || jsonb_build_object('schedulerProblems', part);
          END IF;
          RETURN core || jsonb_build_object('documentIntelligence', di);
        END
        $fn$;
"""


def upgrade() -> None:
    op.execute(_MANUAL_TASK_TRIGGER.replace("{derived_guard}", _DERIVED_GUARD))
    op.execute(_REQUEST_STAGE_RECOMPUTE.replace("{queued_at}", _QUEUED_AT))
    op.execute(_PIPELINE_HEALTH_UP)


def downgrade() -> None:
    op.execute(_PIPELINE_HEALTH_DOWN)
    op.execute(_REQUEST_STAGE_RECOMPUTE.replace("{queued_at}", ""))
    op.execute(_MANUAL_TASK_TRIGGER.replace("{derived_guard}", ""))
