from alembic import op

revision = "0011_uc03_booking_capture"
down_revision = "0010_uc03_c0_foundation"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.journey_stage_states
            ADD COLUMN close_reason_code varchar(100),
            ADD COLUMN closure_remarks text,
            ADD COLUMN closed_by_actor_id varchar(160),
            ADD COLUMN closed_at_utc timestamptz
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.journey_stage_states
        ADD CONSTRAINT ck_journey_stage_states_booking_closure_fields
        CHECK (
            stage_code = 'BOOKING'
            OR (
                close_reason_code IS NULL
                AND closure_remarks IS NULL
                AND closed_by_actor_id IS NULL
                AND closed_at_utc IS NULL
            )
        )
        """
    )

    op.execute(
        """
        ALTER TABLE auditcore.audit_findings
            ADD COLUMN stage_code varchar(30),
            ADD COLUMN origin_kind varchar(20),
            ADD COLUMN origin_actor_id varchar(160),
            ADD COLUMN origin_role_snapshot varchar(80),
            ADD COLUMN rule_key varchar(160),
            ADD COLUMN rule_version_id uuid,
            ADD COLUMN blocking_completion boolean NOT NULL DEFAULT false
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.audit_findings
        ADD CONSTRAINT ck_audit_findings_uc03_stage
        CHECK (stage_code IS NULL OR stage_code IN ('BOOKING','DELIVERY','POST_DELIVERY'))
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.audit_findings
        ADD CONSTRAINT ck_audit_findings_uc03_origin
        CHECK (origin_kind IS NULL OR origin_kind IN ('MACHINE','HUMAN'))
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.audit_findings
        ADD CONSTRAINT fk_audit_findings_uc03_rule_version
        FOREIGN KEY (tenant_id, rule_version_id)
        REFERENCES auditcore.audit_control_versions(tenant_id, audit_control_version_id)
        """
    )

    op.execute(
        """
        CREATE TABLE auditcore.audit_finding_events (
            tenant_id               varchar(128) NOT NULL,
            finding_event_id        uuid NOT NULL DEFAULT gen_random_uuid(),
            audit_finding_id        uuid NOT NULL,
            journey_id              uuid NOT NULL,
            stage_code              varchar(30) NOT NULL
                                    CHECK (stage_code IN ('BOOKING','DELIVERY','POST_DELIVERY')),
            event_type              varchar(80) NOT NULL,
            actor_id                varchar(160),
            actor_role_snapshot     varchar(80),
            reason                  text,
            safe_payload            jsonb NOT NULL DEFAULT '{}'::jsonb,
            correlation_id          varchar(128),
            occurred_at_utc         timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, finding_event_id),
            FOREIGN KEY (tenant_id, audit_finding_id)
                REFERENCES auditcore.audit_findings(tenant_id, audit_finding_id),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_audit_finding_events_finding_time
        ON auditcore.audit_finding_events
            (tenant_id, audit_finding_id, occurred_at_utc, finding_event_id)
        """
    )
    op.execute("ALTER TABLE auditcore.audit_finding_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE auditcore.audit_finding_events FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_audit_finding_events
        ON auditcore.audit_finding_events
        USING (tenant_id = auditcore.current_tenant_id())
        WITH CHECK (tenant_id = auditcore.current_tenant_id())
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_finding_events_append_only
        BEFORE UPDATE OR DELETE ON auditcore.audit_finding_events
        FOR EACH ROW EXECUTE FUNCTION auditcore.prevent_append_only_mutation()
        """
    )
    op.execute(f"GRANT SELECT, INSERT ON auditcore.audit_finding_events TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE ON auditcore.audit_finding_events FROM {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_audit_finding_events_append_only ON auditcore.audit_finding_events"
    )
    op.execute("DROP TABLE IF EXISTS auditcore.audit_finding_events")

    op.execute("ALTER TABLE auditcore.audit_findings DROP CONSTRAINT IF EXISTS fk_audit_findings_uc03_rule_version")
    op.execute("ALTER TABLE auditcore.audit_findings DROP CONSTRAINT IF EXISTS ck_audit_findings_uc03_origin")
    op.execute("ALTER TABLE auditcore.audit_findings DROP CONSTRAINT IF EXISTS ck_audit_findings_uc03_stage")
    op.execute(
        """
        ALTER TABLE auditcore.audit_findings
            DROP COLUMN IF EXISTS blocking_completion,
            DROP COLUMN IF EXISTS rule_version_id,
            DROP COLUMN IF EXISTS rule_key,
            DROP COLUMN IF EXISTS origin_role_snapshot,
            DROP COLUMN IF EXISTS origin_actor_id,
            DROP COLUMN IF EXISTS origin_kind,
            DROP COLUMN IF EXISTS stage_code
        """
    )

    op.execute(
        "ALTER TABLE auditcore.journey_stage_states DROP CONSTRAINT IF EXISTS ck_journey_stage_states_booking_closure_fields"
    )
    op.execute(
        """
        ALTER TABLE auditcore.journey_stage_states
            DROP COLUMN IF EXISTS closed_at_utc,
            DROP COLUMN IF EXISTS closed_by_actor_id,
            DROP COLUMN IF EXISTS closure_remarks,
            DROP COLUMN IF EXISTS close_reason_code
        """
    )
