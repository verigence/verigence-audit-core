"""0179_neon_db_minutes -- the database's own health, every minute (product owner 2026-10-31).

"i need NeonDB status health status.. spikes, CPU and all". The 15-minute
counter snapshots (0171) are too coarse to show a spike, so the API's
heartbeat now also writes one row a minute:

- platform_ops.db_minute_stats: connections by state against max_connections,
  sessions waiting on locks, the longest running query and transaction, and
  the cumulative counters (transactions, buffer hits/reads, deadlocks, temp
  bytes, Neon's local file cache hits/misses) from which the page derives
  transactions per second, cache hit ratio and their per-minute peaks.
  One row per minute whichever API replica writes first.
- platform_ops.db_sample(): the SECURITY DEFINER read behind it (the runtime
  role cannot see other sessions' states), counts and durations only, never a
  query's text.
- The neon extension, when the server has it, for neon_stat_file_cache (the
  compute's local file cache). Elsewhere it is skipped and the column stays null.
"""
from __future__ import annotations

from alembic import op

revision = "0179_neon_db_minutes"
down_revision = "0178_p2_tenant_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          CREATE EXTENSION IF NOT EXISTS neon;
        EXCEPTION WHEN OTHERS THEN
          RAISE NOTICE 'neon extension not available: %', SQLERRM;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_ops.db_minute_stats (
          minute_utc timestamptz PRIMARY KEY,
          connections_active integer NOT NULL DEFAULT 0,
          connections_idle integer NOT NULL DEFAULT 0,
          connections_idle_in_tx integer NOT NULL DEFAULT 0,
          connections_other integer NOT NULL DEFAULT 0,
          max_connections integer,
          waiting_on_locks integer NOT NULL DEFAULT 0,
          longest_query_seconds numeric(12,1),
          longest_transaction_seconds numeric(12,1),
          xact_commit bigint,
          xact_rollback bigint,
          blks_hit bigint,
          blks_read bigint,
          deadlocks bigint,
          temp_bytes bigint,
          size_bytes bigint,
          stats_reset_at_utc timestamptz,
          lfc_hits bigint,
          lfc_misses bigint
        );
        GRANT SELECT, INSERT, DELETE ON platform_ops.db_minute_stats TO audit_core_runtime;
        """
    )
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION platform_ops.db_sample()
        RETURNS jsonb
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $fn$
        DECLARE
          base jsonb;
          neon_schema name;
          lfc_hits bigint := NULL;
          lfc_misses bigint := NULL;
        BEGIN
          SELECT jsonb_build_object(
            'connectionsActive', count(*) FILTER (WHERE a.state = 'active'),
            'connectionsIdle', count(*) FILTER (WHERE a.state = 'idle'),
            'connectionsIdleInTx', count(*) FILTER (WHERE a.state LIKE 'idle in transaction%'),
            'connectionsOther', count(*) FILTER (WHERE a.state IS NULL OR a.state NOT IN ('active', 'idle')
                                                   AND a.state NOT LIKE 'idle in transaction%'),
            'waitingOnLocks', count(*) FILTER (WHERE a.wait_event_type = 'Lock'),
            'longestQuerySeconds', round(COALESCE(max(extract(epoch FROM now() - a.query_start))
                                      FILTER (WHERE a.state = 'active' AND a.pid <> pg_backend_pid()), 0)::numeric, 1),
            'longestTransactionSeconds', round(COALESCE(max(extract(epoch FROM now() - a.xact_start))
                                      FILTER (WHERE a.xact_start IS NOT NULL AND a.pid <> pg_backend_pid()), 0)::numeric, 1)
          )
          INTO base
          FROM pg_stat_activity a
          WHERE a.datname = current_database() AND a.backend_type = 'client backend';

          SELECT base || jsonb_build_object(
            'maxConnections', current_setting('max_connections')::int,
            'commits', d.xact_commit, 'rollbacks', d.xact_rollback,
            'blocksHit', d.blks_hit, 'blocksRead', d.blks_read,
            'deadlocks', d.deadlocks, 'tempBytes', d.temp_bytes,
            'sizeBytes', pg_database_size(d.datname),
            'statsResetAtUtc', d.stats_reset)
          INTO base
          FROM pg_stat_database d
          WHERE d.datname = current_database();

          SELECT n.nspname INTO neon_schema
          FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
          WHERE e.extname = 'neon';
          IF neon_schema IS NOT NULL THEN
            BEGIN
              EXECUTE format('SELECT file_cache_hits::bigint, file_cache_misses::bigint FROM %I.neon_stat_file_cache',
                             neon_schema)
                INTO lfc_hits, lfc_misses;
            EXCEPTION WHEN OTHERS THEN
              lfc_hits := NULL;
              lfc_misses := NULL;
            END;
          END IF;
          RETURN base || jsonb_build_object('lfcHits', lfc_hits, 'lfcMisses', lfc_misses);
        END
        $fn$;

        REVOKE ALL ON FUNCTION platform_ops.db_sample() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION platform_ops.db_sample() TO audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION IF EXISTS platform_ops.db_sample();
        DROP TABLE IF EXISTS platform_ops.db_minute_stats;
        """
    )
