"""0201_data_archive_runs -- the data-archive job's run log and its one delete path.

The shared database grows ~125 MB a day. A nightly job (audit_core.data_archive)
moves rows nothing reads any more to R2 as gzip JSON-lines files, reads them
back, and only then removes them from the database. Archives are kept; the
job never deletes them.

``archive_runs`` is the job's own record: one row per table, Project and run
(``batch_no`` NULL), plus one row per archived batch, written in the same
transaction as that batch's delete -- so no row ever leaves the database
without a committed record of the object it went to. Append-only for the
runtime role (SELECT, INSERT only) and, like the other audit trails, guarded
by ``prevent_append_only_mutation`` against UPDATE/DELETE by anyone. Row
security: a Project reads and writes its own rows; a SuperAdmin request
(app.platform_super_admin) reads all of them.

``archive_delete_rows`` is the only way the runtime role can remove an
archived row: SECURITY DEFINER, owned by the table owner (the same pattern as
``hard_delete_journey_transactions``, 0025/0133, which the runtime role may
execute but whose DELETE rights it does not hold). It deletes the given
primary keys from an allow-listed table only, refuses any other table, and
requires the transaction's tenant context to be the Project it is told to
delete for. ``rule_executions`` needs no append-only trigger bypass (it has
none; the runtime role simply holds no DELETE grant).

For ``auditcore.rule_executions`` the function re-checks the archive rule
itself and refuses (deleting nothing) if any key is the latest row of its
(journey, rule) or the latest non-ERROR row of its (journey, rule): the rows
the readers need can never be removed through it, whatever the caller sends.
A key that is not there any more (a Journey purged meanwhile), belongs to
another Project or fails that check makes the whole call fail and roll back.

Adding a table later: a new migration replaces this function with one more
explicit branch (no dynamic SQL, so the allow list is the function body).
"""
from __future__ import annotations

from alembic import op

revision = "0201_data_archive_runs"
down_revision = "0200_access_location_watch"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLE = "archive_runs"
_FUNCTION = "auditcore.archive_delete_rows(varchar, text, uuid[])"
# One call deletes at most this many keys: a batch is 5,000 by default.
_MAX_KEYS_PER_CALL = 10000


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_TABLE} (
            entry_id          uuid         NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
            run_id            uuid         NOT NULL,
            tenant_id         varchar(128),
            table_key         varchar(160) NOT NULL,
            mode              varchar(10)  NOT NULL CHECK (mode IN ('DRY_RUN', 'LIVE')),
            batch_no          integer      CHECK (batch_no IS NULL OR batch_no >= 1),
            status            varchar(20)  NOT NULL
                              CHECK (status IN ('DRY_RUN', 'BATCH_ARCHIVED', 'SUCCEEDED', 'FAILED', 'REFUSED')),
            rule_description  text         NOT NULL,
            cutoff_utc        timestamptz,
            candidate_count   bigint       NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
            archived_count    bigint       NOT NULL DEFAULT 0 CHECK (archived_count >= 0),
            object_keys       jsonb        NOT NULL DEFAULT '[]'::jsonb,
            checksums         jsonb        NOT NULL DEFAULT '{{}}'::jsonb,
            error             text,
            started_by        varchar(160) NOT NULL,
            started_at_utc    timestamptz  NOT NULL,
            finished_at_utc   timestamptz  NOT NULL DEFAULT now(),
            CONSTRAINT archive_runs_batch_status CHECK (
                (batch_no IS NULL AND status <> 'BATCH_ARCHIVED')
                OR (batch_no IS NOT NULL AND status = 'BATCH_ARCHIVED' AND mode = 'LIVE')
            )
        )
        """
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_started "
        f"ON auditcore.{_TABLE} (started_at_utc DESC, run_id)"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_table "
        f"ON auditcore.{_TABLE} (table_key, tenant_id, started_at_utc DESC)"
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{_TABLE}_append_only
        BEFORE UPDATE OR DELETE ON auditcore.{_TABLE}
        FOR EACH ROW EXECUTE FUNCTION auditcore.prevent_append_only_mutation()
        """
    )
    op.execute(f"ALTER TABLE auditcore.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_TABLE}_read ON auditcore.{_TABLE} FOR SELECT "
        "USING (tenant_id = auditcore.current_tenant_id() "
        "OR auditcore.current_platform_super_admin())"
    )
    # A Project's rows are written under that Project's tenant context; a row
    # with no Project (a refused run, a table without tenant_id) only with no
    # tenant context at all.
    op.execute(
        f"CREATE POLICY {_TABLE}_insert ON auditcore.{_TABLE} FOR INSERT "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id() "
        "OR (tenant_id IS NULL AND auditcore.current_tenant_id() IS NULL))"
    )
    op.execute(f"GRANT SELECT, INSERT ON auditcore.{_TABLE} TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON auditcore.{_TABLE} FROM {_RUNTIME_ROLE}")

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION auditcore.archive_delete_rows(
            p_tenant_id varchar,
            p_table text,
            p_ids uuid[]
        ) RETURNS integer
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, auditcore
        AS $$
        DECLARE
            v_requested integer := COALESCE(cardinality(p_ids), 0);
            v_deleted integer := 0;
        BEGIN
            -- The allow list. Anything else is refused before any other check.
            IF p_table IS DISTINCT FROM 'auditcore.rule_executions' THEN
                RAISE EXCEPTION 'ARCHIVE_TABLE_NOT_ALLOWED:%', p_table
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF p_tenant_id IS NULL OR btrim(p_tenant_id) = '' THEN
                RAISE EXCEPTION 'TENANT_ID_REQUIRED' USING ERRCODE = 'invalid_parameter_value';
            END IF;
            -- Row security does not bind a SECURITY DEFINER owner, so the
            -- function holds the caller to its own tenant context instead.
            IF auditcore.current_tenant_id() IS DISTINCT FROM p_tenant_id THEN
                RAISE EXCEPTION 'ARCHIVE_TENANT_CONTEXT_MISMATCH'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF v_requested = 0 THEN
                RETURN 0;
            END IF;
            IF v_requested > {_MAX_KEYS_PER_CALL} THEN
                RAISE EXCEPTION 'ARCHIVE_BATCH_TOO_LARGE:%', v_requested
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF array_position(p_ids, NULL) IS NOT NULL
               OR (SELECT count(DISTINCT k) FROM unnest(p_ids) k) <> v_requested THEN
                RAISE EXCEPTION 'ARCHIVE_KEYS_INVALID' USING ERRCODE = 'invalid_parameter_value';
            END IF;

            IF p_table = 'auditcore.rule_executions' THEN
                -- Only rows with a newer row of the same (journey, rule), and,
                -- unless the row is an ERROR, a newer non-ERROR row too. The
                -- subqueries see the table as it was when the statement began,
                -- so the newest row of a pair can never qualify.
                DELETE FROM auditcore.rule_executions re
                WHERE re.tenant_id = p_tenant_id
                  AND re.rule_execution_id = ANY(p_ids)
                  AND EXISTS (
                        SELECT 1 FROM auditcore.rule_executions n
                        WHERE n.tenant_id = re.tenant_id
                          AND n.journey_id = re.journey_id
                          AND n.rule_code = re.rule_code
                          AND n.evaluated_at_utc > re.evaluated_at_utc
                  )
                  AND (
                        re.outcome = 'ERROR'
                        OR EXISTS (
                            SELECT 1 FROM auditcore.rule_executions n
                            WHERE n.tenant_id = re.tenant_id
                              AND n.journey_id = re.journey_id
                              AND n.rule_code = re.rule_code
                              AND n.outcome <> 'ERROR'
                              AND n.evaluated_at_utc > re.evaluated_at_utc
                        )
                  );
                GET DIAGNOSTICS v_deleted = ROW_COUNT;
            END IF;

            IF v_deleted <> v_requested THEN
                RAISE EXCEPTION 'ARCHIVE_SCOPE_MISMATCH:%/%', v_deleted, v_requested
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN v_deleted;
        END;
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_FUNCTION} TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION}")
    op.execute(f"DROP TABLE IF EXISTS auditcore.{_TABLE}")
