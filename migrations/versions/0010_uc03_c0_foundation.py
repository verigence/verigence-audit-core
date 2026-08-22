from alembic import op

revision = "0010_uc03_c0_foundation"
down_revision = "0009_uc02_project_refs"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    # C0 Project discovery is intentionally cross-Tenant only for the authenticated
    # Security actor. Keep the runtime role NOBYPASSRLS and express that narrow read
    # path in RLS rather than using an owner/admin connection from the API.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.current_security_actor_id()
        RETURNS varchar
        LANGUAGE sql
        STABLE
        AS $$
            SELECT NULLIF(current_setting('app.security_actor_id', true), '')::varchar;
        $$
        """
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION auditcore.current_security_actor_id() TO {_RUNTIME_ROLE}"
    )

    op.execute("DROP POLICY IF EXISTS tenant_isolation_business_assignments ON auditcore.business_assignments")
    op.execute(
        """
        CREATE POLICY tenant_isolation_business_assignments
        ON auditcore.business_assignments
        FOR ALL
        USING (tenant_id = auditcore.current_tenant_id())
        WITH CHECK (tenant_id = auditcore.current_tenant_id())
        """
    )
    op.execute(
        """
        CREATE POLICY actor_project_discovery_business_assignments
        ON auditcore.business_assignments
        FOR SELECT
        USING (
            auditcore.current_tenant_id() IS NULL
            AND auditcore.current_security_actor_id() IS NOT NULL
            AND security_actor_id = auditcore.current_security_actor_id()
        )
        """
    )

    op.execute("DROP POLICY IF EXISTS tenant_isolation_projects ON auditcore.projects")
    op.execute(
        """
        CREATE POLICY tenant_isolation_projects
        ON auditcore.projects
        FOR ALL
        USING (tenant_id = auditcore.current_tenant_id())
        WITH CHECK (tenant_id = auditcore.current_tenant_id())
        """
    )
    op.execute(
        """
        CREATE POLICY actor_project_discovery_projects
        ON auditcore.projects
        FOR SELECT
        USING (
            auditcore.current_tenant_id() IS NULL
            AND auditcore.current_security_actor_id() IS NOT NULL
            AND EXISTS (
                SELECT 1
                FROM auditcore.business_assignments ba
                WHERE ba.tenant_id = projects.tenant_id
                  AND ba.security_actor_id = auditcore.current_security_actor_id()
                  AND ba.assignment_status = 'ACTIVE'
                  AND ba.effective_from <= now()
                  AND (ba.effective_to IS NULL OR ba.effective_to > now())
            )
        )
        """
    )

    op.execute(
        """
        CREATE TABLE auditcore.journey_stage_states (
            tenant_id                   varchar(128) NOT NULL,
            journey_id                  uuid NOT NULL,
            stage_code                  varchar(30) NOT NULL
                                        CHECK (stage_code IN ('BOOKING','DELIVERY','POST_DELIVERY')),
            business_status             varchar(100),
            closure_disposition         varchar(100),
            audit_state                 varchar(30) NOT NULL DEFAULT 'NOT_STARTED'
                                        CHECK (audit_state IN ('NOT_STARTED','IN_PROGRESS','COMPLETE')),
            audit_status                varchar(30) NOT NULL DEFAULT 'NOT_EVALUATED'
                                        CHECK (audit_status IN ('NOT_EVALUATED','NO_FLAGS','FLAGS_RAISED')),
            business_started_at_utc     timestamptz,
            business_completed_at_utc   timestamptz,
            capture_completed_at_utc    timestamptz,
            latest_activity_at_utc      timestamptz NOT NULL DEFAULT now(),
            version_no                  bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
            created_at_utc              timestamptz NOT NULL DEFAULT now(),
            updated_at_utc              timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, journey_id, stage_code),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id),
            CHECK (
                business_completed_at_utc IS NULL
                OR business_started_at_utc IS NULL
                OR business_completed_at_utc >= business_started_at_utc
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_journey_stage_states_latest_activity
        ON auditcore.journey_stage_states
            (tenant_id, latest_activity_at_utc DESC, journey_id, stage_code)
        """
    )
    op.execute("ALTER TABLE auditcore.journey_stage_states ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE auditcore.journey_stage_states FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_journey_stage_states
        ON auditcore.journey_stage_states
        USING (tenant_id = auditcore.current_tenant_id())
        WITH CHECK (tenant_id = auditcore.current_tenant_id())
        """
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON auditcore.journey_stage_states TO {_RUNTIME_ROLE}"
    )

    op.execute(
        """
        CREATE TABLE auditcore.journey_workflow_events (
            tenant_id                   varchar(128) NOT NULL,
            event_id                    uuid NOT NULL DEFAULT gen_random_uuid(),
            journey_id                  uuid NOT NULL,
            stage_code                  varchar(30) NOT NULL
                                        CHECK (stage_code IN ('BOOKING','DELIVERY','POST_DELIVERY')),
            event_type                  varchar(120) NOT NULL,
            source_kind                 varchar(30) NOT NULL
                                        CHECK (source_kind IN ('HUMAN','MACHINE','SOURCE_SYSTEM')),
            actor_id                    varchar(160),
            actor_role_snapshot         varchar(80),
            idempotency_key             varchar(200),
            correlation_id              varchar(128) NOT NULL,
            safe_payload                jsonb NOT NULL DEFAULT '{}'::jsonb,
            occurred_at_utc             timestamptz NOT NULL,
            recorded_at_utc             timestamptz NOT NULL DEFAULT now(),
            aggregate_version           bigint NOT NULL CHECK (aggregate_version > 0),
            PRIMARY KEY (tenant_id, event_id),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_journey_workflow_events_journey_time
        ON auditcore.journey_workflow_events
            (tenant_id, journey_id, recorded_at_utc, event_id)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_journey_workflow_events_stage_time
        ON auditcore.journey_workflow_events
            (tenant_id, stage_code, occurred_at_utc DESC, event_id)
        """
    )
    op.execute("ALTER TABLE auditcore.journey_workflow_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE auditcore.journey_workflow_events FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_journey_workflow_events
        ON auditcore.journey_workflow_events
        USING (tenant_id = auditcore.current_tenant_id())
        WITH CHECK (tenant_id = auditcore.current_tenant_id())
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_journey_workflow_events_append_only
        BEFORE UPDATE OR DELETE ON auditcore.journey_workflow_events
        FOR EACH ROW EXECUTE FUNCTION auditcore.prevent_append_only_mutation()
        """
    )
    op.execute(
        f"GRANT SELECT, INSERT ON auditcore.journey_workflow_events TO {_RUNTIME_ROLE}"
    )
    op.execute(f"REVOKE UPDATE, DELETE ON auditcore.journey_workflow_events FROM {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_journey_workflow_events_append_only ON auditcore.journey_workflow_events")
    op.execute("DROP TABLE IF EXISTS auditcore.journey_workflow_events")
    op.execute("DROP TABLE IF EXISTS auditcore.journey_stage_states")

    op.execute("DROP POLICY IF EXISTS actor_project_discovery_projects ON auditcore.projects")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_projects ON auditcore.projects")
    op.execute(
        """
        CREATE POLICY tenant_isolation_projects
        ON auditcore.projects
        USING (tenant_id = auditcore.current_tenant_id())
        WITH CHECK (tenant_id = auditcore.current_tenant_id())
        """
    )

    op.execute(
        "DROP POLICY IF EXISTS actor_project_discovery_business_assignments ON auditcore.business_assignments"
    )
    op.execute("DROP POLICY IF EXISTS tenant_isolation_business_assignments ON auditcore.business_assignments")
    op.execute(
        """
        CREATE POLICY tenant_isolation_business_assignments
        ON auditcore.business_assignments
        USING (tenant_id = auditcore.current_tenant_id())
        WITH CHECK (tenant_id = auditcore.current_tenant_id())
        """
    )

    op.execute(
        f"REVOKE EXECUTE ON FUNCTION auditcore.current_security_actor_id() FROM {_RUNTIME_ROLE}"
    )
    op.execute("DROP FUNCTION IF EXISTS auditcore.current_security_actor_id()")
