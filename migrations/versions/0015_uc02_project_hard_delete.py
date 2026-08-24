from alembic import op

revision = "0015_uc02_project_delete"
down_revision = "0014_uc02_mahindra_seg"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    # Owner-approved UC02 rule: a Project may be hard-deleted only while its
    # Journey count is zero, regardless of CONFIGURING/ACTIVE lifecycle state.
    # Keep destructive access encapsulated in one SECURITY DEFINER function
    # instead of granting broad DELETE privileges to the runtime role.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.hard_delete_zero_journey_project(
            p_tenant_id varchar
        ) RETURNS jsonb
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, auditcore
        AS $$
        DECLARE
            v_project_status varchar;
            v_journey_count bigint;
            v_target record;
            v_deleted bigint;
            v_receipt jsonb := '{}'::jsonb;
        BEGIN
            SELECT project_status
            INTO v_project_status
            FROM auditcore.projects
            WHERE tenant_id=p_tenant_id
            FOR UPDATE;

            IF v_project_status IS NULL THEN
                RETURN jsonb_build_object(
                    'tenantId', p_tenant_id,
                    'projectAlreadyAbsent', true,
                    'deletedRows', v_receipt
                );
            END IF;

            SELECT count(*) INTO v_journey_count
            FROM auditcore.journeys
            WHERE tenant_id=p_tenant_id;

            IF v_journey_count <> 0 THEN
                RAISE EXCEPTION 'PROJECT_HAS_JOURNEYS:%', v_journey_count
                    USING ERRCODE='check_violation';
            END IF;

            -- Delete all tenant-owned tables in FK child-first order. Published
            -- master and append-only protection triggers are intentionally USER
            -- triggers; disable them only inside this owner-approved purge scope.
            -- FK/internal triggers remain enabled, so child-first ordering is still
            -- enforced. DDL is transactional, so any failure restores trigger state.
            FOR v_target IN
                WITH RECURSIVE tenant_tables AS (
                    SELECT c.oid, c.relname
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    JOIN pg_attribute a ON a.attrelid=c.oid
                    WHERE n.nspname='auditcore'
                      AND c.relkind='r'
                      AND a.attname='tenant_id'
                      AND a.attnum > 0
                      AND NOT a.attisdropped
                      AND c.relname NOT IN (
                          'projects', 'journeys', 'administrative_operations'
                      )
                ),
                edges AS (
                    SELECT con.conrelid AS child, con.confrelid AS parent
                    FROM pg_constraint con
                    WHERE con.contype='f'
                      AND con.conrelid IN (SELECT oid FROM tenant_tables)
                      AND con.confrelid IN (SELECT oid FROM tenant_tables)
                      AND con.conrelid <> con.confrelid
                ),
                walk(child, parent, depth, path) AS (
                    SELECT child, parent, 1, ARRAY[child, parent]::oid[]
                    FROM edges
                    UNION ALL
                    SELECT w.child, e.parent, w.depth + 1, w.path || e.parent
                    FROM walk w
                    JOIN edges e ON e.child=w.parent
                    WHERE NOT e.parent = ANY(w.path)
                ),
                depths AS (
                    SELECT t.oid, t.relname, COALESCE(max(w.depth), 0) AS depth
                    FROM tenant_tables t
                    LEFT JOIN walk w ON w.child=t.oid
                    GROUP BY t.oid, t.relname
                )
                SELECT relname
                FROM depths
                ORDER BY depth DESC, relname
            LOOP
                EXECUTE format(
                    'ALTER TABLE auditcore.%I DISABLE TRIGGER USER',
                    v_target.relname
                );
                EXECUTE format(
                    'DELETE FROM auditcore.%I WHERE tenant_id = %L',
                    v_target.relname,
                    p_tenant_id
                );
                GET DIAGNOSTICS v_deleted = ROW_COUNT;
                EXECUTE format(
                    'ALTER TABLE auditcore.%I ENABLE TRIGGER USER',
                    v_target.relname
                );
                IF v_deleted > 0 THEN
                    v_receipt := v_receipt || jsonb_build_object(
                        v_target.relname, v_deleted
                    );
                END IF;
            END LOOP;

            -- Journey count was checked under the Project row lock and the route
            -- also holds a Project-scoped advisory lock. Journeys remain a separate
            -- explicit gate rather than being silently destroyed here.
            DELETE FROM auditcore.projects WHERE tenant_id=p_tenant_id;
            GET DIAGNOSTICS v_deleted = ROW_COUNT;
            v_receipt := v_receipt || jsonb_build_object('projects', v_deleted);

            RETURN jsonb_build_object(
                'tenantId', p_tenant_id,
                'projectStatus', v_project_status,
                'journeyCount', v_journey_count,
                'deletedRows', v_receipt
            );
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION auditcore.hard_delete_zero_journey_project(varchar) FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION auditcore.hard_delete_zero_journey_project(varchar) "
        f"TO {_RUNTIME_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS auditcore.hard_delete_zero_journey_project(varchar)"
    )
