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
        CREATE TABLE auditcore.journey_document_assessments (
            tenant_id                               varchar(128) NOT NULL,
            journey_document_assessment_id          uuid NOT NULL DEFAULT gen_random_uuid(),
            journey_id                              uuid NOT NULL,
            stage_code                              varchar(30) NOT NULL
                                                    CHECK (stage_code IN ('BOOKING','DELIVERY','POST_DELIVERY')),
            journey_document_requirement_id         uuid NOT NULL,
            requirement_key                         varchar(120) NOT NULL,
            document_requirement_profile_version_id uuid,
            applicability_state                     varchar(30) NOT NULL DEFAULT 'APPLICABLE'
                                                    CHECK (applicability_state IN ('APPLICABLE','NOT_APPLICABLE')),
            applicability_reason                    text,
            answer                                  varchar(20) NOT NULL DEFAULT 'UNANSWERED'
                                                    CHECK (answer IN ('YES','NO','NA','UNANSWERED')),
            evidence_id                             uuid,
            remarks                                 text,
            answered_by_actor_id                    varchar(160),
            answered_by_role                        varchar(80),
            answered_at_utc                         timestamptz,
            version_no                              bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
            created_at_utc                          timestamptz NOT NULL DEFAULT now(),
            updated_at_utc                          timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, journey_document_assessment_id),
            UNIQUE (tenant_id, journey_id, stage_code, requirement_key),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id),
            FOREIGN KEY (tenant_id, journey_document_requirement_id)
                REFERENCES auditcore.journey_document_requirements(
                    tenant_id, journey_document_requirement_id
                ),
            FOREIGN KEY (tenant_id, document_requirement_profile_version_id)
                REFERENCES auditcore.document_requirement_profile_versions(
                    tenant_id, document_requirement_profile_version_id
                ),
            FOREIGN KEY (tenant_id, evidence_id)
                REFERENCES auditcore.evidence(tenant_id, evidence_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_journey_document_assessments_stage
        ON auditcore.journey_document_assessments
            (tenant_id, journey_id, stage_code, applicability_state, answer)
        """
    )
    op.execute(
        "ALTER TABLE auditcore.journey_document_assessments ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE auditcore.journey_document_assessments FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        """
        CREATE POLICY tenant_isolation_journey_document_assessments
        ON auditcore.journey_document_assessments
        USING (tenant_id = auditcore.current_tenant_id())
        WITH CHECK (tenant_id = auditcore.current_tenant_id())
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_journey_document_assessments_updated
        BEFORE UPDATE ON auditcore.journey_document_assessments
        FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at()
        """
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON auditcore.journey_document_assessments TO {_RUNTIME_ROLE}"
    )
    op.execute(
        f"REVOKE DELETE ON auditcore.journey_document_assessments FROM {_RUNTIME_ROLE}"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.initialize_uc03_booking_requirements()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.stage_code <> 'BOOKING' THEN
                RETURN NEW;
            END IF;

            INSERT INTO auditcore.journey_document_requirements (
                tenant_id,
                journey_id,
                document_requirement_item_id,
                requirement_key,
                document_type_key,
                process_area,
                requirement_level,
                requirement_status,
                condition_snapshot
            )
            SELECT
                j.tenant_id,
                j.journey_id,
                dri.document_requirement_item_id,
                dri.requirement_key,
                dri.document_type_key,
                dri.process_area,
                dri.requirement_level,
                'PENDING',
                dri.condition_config
            FROM auditcore.journeys j
            JOIN auditcore.document_requirement_items dri
              ON dri.tenant_id = j.tenant_id
             AND dri.document_requirement_profile_version_id =
                    j.document_requirement_profile_version_id
            WHERE j.tenant_id = NEW.tenant_id
              AND j.journey_id = NEW.journey_id
              AND upper(dri.process_area) = 'BOOKING'
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_uc03_booking_initialize_requirements
        AFTER INSERT ON auditcore.journey_stage_states
        FOR EACH ROW EXECUTE FUNCTION auditcore.initialize_uc03_booking_requirements()
        """
    )

    # Machine values remain immutable proposals until a human accepts or corrects
    # them into the existing typed Audit Core domain. proposed_value is never
    # overwritten by human correction, preserving machine provenance.
    op.execute(
        """
        CREATE TABLE auditcore.journey_capture_proposals (
            tenant_id                  varchar(128) NOT NULL,
            capture_proposal_id        uuid NOT NULL DEFAULT gen_random_uuid(),
            journey_id                 uuid NOT NULL,
            stage_code                 varchar(30) NOT NULL
                                       CHECK (stage_code IN ('BOOKING','DELIVERY','POST_DELIVERY')),
            field_key                  varchar(160) NOT NULL,
            source_evidence_id         uuid NOT NULL,
            source_evidence_fact_id    varchar(160) NOT NULL,
            source_fact_version        integer NOT NULL DEFAULT 1 CHECK (source_fact_version > 0),
            source_document_type_key   varchar(120),
            value_source               varchar(80),
            proposed_value             jsonb NOT NULL,
            confidence_score           numeric(8,7)
                                       CHECK (confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)),
            proposal_status            varchar(20) NOT NULL DEFAULT 'PENDING'
                                       CHECK (proposal_status IN ('PENDING','ACCEPTED','CORRECTED','REJECTED','SUPERSEDED')),
            accepted_value             jsonb,
            accepted_by_actor_id       varchar(160),
            accepted_by_role           varchar(80),
            accepted_at_utc            timestamptz,
            owning_domain_key          varchar(100),
            owning_record_reference    varchar(240),
            created_at_utc             timestamptz NOT NULL DEFAULT now(),
            updated_at_utc             timestamptz NOT NULL DEFAULT now(),
            version_no                 bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
            PRIMARY KEY (tenant_id, capture_proposal_id),
            UNIQUE (
                tenant_id, source_evidence_id, source_evidence_fact_id,
                source_fact_version
            ),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id),
            FOREIGN KEY (tenant_id, source_evidence_id)
                REFERENCES auditcore.evidence(tenant_id, evidence_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_journey_capture_proposals_stage_status
        ON auditcore.journey_capture_proposals
            (tenant_id, journey_id, stage_code, proposal_status, created_at_utc)
        """
    )
    op.execute(
        "ALTER TABLE auditcore.journey_capture_proposals ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE auditcore.journey_capture_proposals FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        """
        CREATE POLICY tenant_isolation_journey_capture_proposals
        ON auditcore.journey_capture_proposals
        USING (tenant_id = auditcore.current_tenant_id())
        WITH CHECK (tenant_id = auditcore.current_tenant_id())
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_journey_capture_proposals_updated
        BEFORE UPDATE ON auditcore.journey_capture_proposals
        FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at()
        """
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON auditcore.journey_capture_proposals TO {_RUNTIME_ROLE}"
    )
    op.execute(
        f"REVOKE DELETE ON auditcore.journey_capture_proposals FROM {_RUNTIME_ROLE}"
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
    op.execute(
        f"GRANT SELECT, INSERT ON auditcore.audit_finding_events TO {_RUNTIME_ROLE}"
    )
    op.execute(
        f"REVOKE UPDATE, DELETE ON auditcore.audit_finding_events FROM {_RUNTIME_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_audit_finding_events_append_only "
        "ON auditcore.audit_finding_events"
    )
    op.execute("DROP TABLE IF EXISTS auditcore.audit_finding_events")

    op.execute(
        "ALTER TABLE auditcore.audit_findings "
        "DROP CONSTRAINT IF EXISTS fk_audit_findings_uc03_rule_version"
    )
    op.execute(
        "ALTER TABLE auditcore.audit_findings "
        "DROP CONSTRAINT IF EXISTS ck_audit_findings_uc03_origin"
    )
    op.execute(
        "ALTER TABLE auditcore.audit_findings "
        "DROP CONSTRAINT IF EXISTS ck_audit_findings_uc03_stage"
    )
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
        "DROP TRIGGER IF EXISTS trg_journey_capture_proposals_updated "
        "ON auditcore.journey_capture_proposals"
    )
    op.execute("DROP TABLE IF EXISTS auditcore.journey_capture_proposals")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_uc03_booking_initialize_requirements "
        "ON auditcore.journey_stage_states"
    )
    op.execute("DROP FUNCTION IF EXISTS auditcore.initialize_uc03_booking_requirements()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_journey_document_assessments_updated "
        "ON auditcore.journey_document_assessments"
    )
    op.execute("DROP TABLE IF EXISTS auditcore.journey_document_assessments")

    op.execute(
        "ALTER TABLE auditcore.journey_stage_states "
        "DROP CONSTRAINT IF EXISTS ck_journey_stage_states_booking_closure_fields"
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
