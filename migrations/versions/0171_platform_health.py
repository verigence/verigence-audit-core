"""0171_platform_health -- Superadmin System Health (product owner 2026-10-27).

Platform-level, not per Project: the health of the services and of the
database that every Project shares.

- platform_ops.service_heartbeats: one row per process start (boot). Each
  process updates last_seen_at_utc every minute and stamps stopped_at_utc on a
  clean shutdown. A restart is a new boot; a boot that stopped seeing itself
  without stopped_at_utc ended unclean (crash, OOM kill, platform restart).
  Audit Core, the P2 worker, DI and Security all write here (each connects as
  the database owner; Audit Core's API drops to audit_core_runtime, hence the
  grants).
- platform_ops.http_minute_stats: request counts per service per minute
  (total, 4xx, 5xx, slow) and the routes that failed, flushed by the heartbeat.
- platform_ops.service_resource_minutes: each process's container CPU and
  memory, average and peak per minute, against the container's limits, read
  from its own cgroup. No Railway token is needed anywhere.
- platform_ops.railway_events: deployment status changes and CPU/RAM monitor
  alerts that Railway posts to Audit Core's webhook. The webhook carries a
  secret header; only its SHA-256 is stored (platform_ops.webhook_secrets).
- platform_ops.db_counter_snapshots: deadlock / rollback / commit counters
  every 15 minutes, so the page can show the change over a window.
- platform_ops.pipeline_health() / db_health(): SECURITY DEFINER reads that
  return counts and error codes only, never Journey or customer data. They read
  across Projects (the queues have forced RLS) and DI's schema, which the
  runtime role cannot read. EXECUTE goes to audit_core_runtime only; the one
  route that calls them is SuperAdmin-only.
- pg_stat_statements is enabled so the slowest queries can be listed.
"""
from __future__ import annotations

from alembic import op

revision = "0171_platform_health"
down_revision = "0170_gate_pass_not_notified"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Not every Postgres preloads it (a plain CI container does not); the function
    # below copes with it missing or unloaded.
    op.execute(
        """
        DO $$
        BEGIN
          CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
        EXCEPTION WHEN OTHERS THEN
          RAISE NOTICE 'pg_stat_statements not available: %', SQLERRM;
        END $$;
        """
    )
    op.execute(
        """
        CREATE SCHEMA IF NOT EXISTS platform_ops;
        GRANT USAGE ON SCHEMA platform_ops TO audit_core_runtime;

        CREATE TABLE IF NOT EXISTS platform_ops.service_heartbeats (
          boot_id uuid PRIMARY KEY,
          service_name varchar(80) NOT NULL,
          replica_id varchar(120),
          deployment_id varchar(120),
          version varchar(80),
          started_at_utc timestamptz NOT NULL DEFAULT now(),
          last_seen_at_utc timestamptz NOT NULL DEFAULT now(),
          stopped_at_utc timestamptz,
          stop_reason varchar(80)
        );
        CREATE INDEX IF NOT EXISTS ix_service_heartbeats_service_started
          ON platform_ops.service_heartbeats (service_name, started_at_utc DESC);

        CREATE TABLE IF NOT EXISTS platform_ops.http_minute_stats (
          service_name varchar(80) NOT NULL,
          boot_id uuid NOT NULL,
          minute_utc timestamptz NOT NULL,
          total integer NOT NULL DEFAULT 0,
          client_errors integer NOT NULL DEFAULT 0,
          server_errors integer NOT NULL DEFAULT 0,
          slow integer NOT NULL DEFAULT 0,
          failed_routes jsonb NOT NULL DEFAULT '{}'::jsonb,
          PRIMARY KEY (service_name, boot_id, minute_utc)
        );
        CREATE INDEX IF NOT EXISTS ix_http_minute_stats_minute
          ON platform_ops.http_minute_stats (minute_utc DESC);

        CREATE TABLE IF NOT EXISTS platform_ops.service_resource_minutes (
          boot_id uuid NOT NULL,
          service_name varchar(80) NOT NULL,
          minute_utc timestamptz NOT NULL,
          cpu_cores_avg numeric(8,3),
          cpu_cores_max numeric(8,3),
          cpu_limit_cores numeric(8,3),
          memory_bytes_max bigint,
          memory_limit_bytes bigint,
          PRIMARY KEY (boot_id, minute_utc)
        );
        CREATE INDEX IF NOT EXISTS ix_service_resource_minutes_minute
          ON platform_ops.service_resource_minutes (service_name, minute_utc DESC);

        CREATE TABLE IF NOT EXISTS platform_ops.railway_events (
          event_id bigserial PRIMARY KEY,
          received_at_utc timestamptz NOT NULL DEFAULT now(),
          occurred_at_utc timestamptz,
          event_type varchar(120) NOT NULL,
          severity varchar(40),
          status varchar(40),
          environment_name varchar(120),
          service_name varchar(120),
          deployment_id varchar(120),
          payload jsonb NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_railway_events_received
          ON platform_ops.railway_events (received_at_utc DESC);

        CREATE TABLE IF NOT EXISTS platform_ops.webhook_secrets (
          name varchar(40) PRIMARY KEY,
          secret_sha256 char(64) NOT NULL,
          created_at_utc timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS platform_ops.db_counter_snapshots (
          taken_at_utc timestamptz PRIMARY KEY DEFAULT now(),
          counters jsonb NOT NULL
        );

        GRANT SELECT, INSERT, UPDATE, DELETE ON platform_ops.service_heartbeats TO audit_core_runtime;
        GRANT SELECT, INSERT, UPDATE, DELETE ON platform_ops.http_minute_stats TO audit_core_runtime;
        GRANT SELECT, INSERT, UPDATE, DELETE ON platform_ops.service_resource_minutes TO audit_core_runtime;
        GRANT SELECT, INSERT, DELETE ON platform_ops.railway_events TO audit_core_runtime;
        GRANT USAGE ON SEQUENCE platform_ops.railway_events_event_id_seq TO audit_core_runtime;
        GRANT SELECT ON platform_ops.webhook_secrets TO audit_core_runtime;
        GRANT SELECT, INSERT, DELETE ON platform_ops.db_counter_snapshots TO audit_core_runtime;
        """
    )
    op.execute(
        r"""
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
    )
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION platform_ops.db_health()
        RETURNS jsonb
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $fn$
        DECLARE
          base jsonb;
          slow jsonb := NULL;
          slow_note text := NULL;
        BEGIN
          SELECT jsonb_build_object(
            'database', d.datname,
            'sizeBytes', pg_database_size(d.datname),
            'maxConnections', current_setting('max_connections')::int,
            'commits', d.xact_commit, 'rollbacks', d.xact_rollback,
            'deadlocks', d.deadlocks, 'conflicts', d.conflicts,
            'tempFiles', d.temp_files, 'tempBytes', d.temp_bytes,
            'blocksHit', d.blks_hit, 'blocksRead', d.blks_read,
            'statsResetAtUtc', d.stats_reset,
            'connections', COALESCE((
              SELECT jsonb_object_agg(state, n) FROM (
                SELECT COALESCE(a.state, 'unknown') AS state, count(*) AS n
                FROM pg_stat_activity a WHERE a.datname = d.datname AND a.backend_type = 'client backend'
                GROUP BY 1) c), '{}'::jsonb),
            'idleInTransactionOver5m', (
              SELECT count(*) FROM pg_stat_activity a
              WHERE a.datname = d.datname AND a.state LIKE 'idle in transaction%'
                AND a.state_change < now() - interval '5 minutes'),
            'activeOver1m', (
              SELECT count(*) FROM pg_stat_activity a
              WHERE a.datname = d.datname AND a.state = 'active' AND a.backend_type = 'client backend'
                AND a.query_start < now() - interval '1 minute'),
            'waitingOnLocks', (
              SELECT count(*) FROM pg_stat_activity a
              WHERE a.datname = d.datname AND a.wait_event_type = 'Lock')
          )
          INTO base
          FROM pg_stat_database d
          WHERE d.datname = current_database();

          IF to_regclass('public.pg_stat_statements') IS NULL THEN
            slow_note := 'pg_stat_statements is not installed';
          ELSE
            BEGIN
              EXECUTE $q$
                SELECT COALESCE(jsonb_agg(jsonb_build_object(
                  'query', left(regexp_replace(query, '\s+', ' ', 'g'), 300),
                  'calls', calls,
                  'meanMs', round(mean_exec_time::numeric, 1),
                  'maxMs', round(max_exec_time::numeric, 1),
                  'totalMs', round(total_exec_time::numeric, 0))), '[]'::jsonb)
                FROM (SELECT * FROM public.pg_stat_statements s
                      WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                        AND s.query NOT ILIKE '%pg_stat_statements%'
                      ORDER BY s.mean_exec_time DESC LIMIT 10) top
              $q$ INTO slow;
            EXCEPTION WHEN OTHERS THEN
              slow_note := 'pg_stat_statements is not loaded: ' || left(SQLERRM, 200);
            END;
          END IF;
          RETURN base || jsonb_build_object('slowQueries', slow, 'slowQueriesNote', slow_note);
        END
        $fn$;

        REVOKE ALL ON FUNCTION platform_ops.pipeline_health(timestamptz) FROM PUBLIC;
        REVOKE ALL ON FUNCTION platform_ops.db_health() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION platform_ops.pipeline_health(timestamptz) TO audit_core_runtime;
        GRANT EXECUTE ON FUNCTION platform_ops.db_health() TO audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS platform_ops CASCADE")
