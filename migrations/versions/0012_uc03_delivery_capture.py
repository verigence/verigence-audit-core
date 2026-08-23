from alembic import op

revision = "0012_uc03_delivery_capture"
down_revision = "0011_uc03_booking_capture"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    # C2's Delivery lifecycle is frozen by the UC03 handoff. The existing typed
    # deliveries/history tables enforce their status catalogue by FK, so seed the
    # three canonical Delivery statuses for every existing Project and for Projects
    # created after this migration. Existing tenant labels/configuration win.
    op.execute(
        """
        INSERT INTO auditcore.business_status_codes (
            tenant_id, domain_key, status_code, status_label, description
        )
        SELECT
            p.tenant_id,
            'DELIVERY',
            seed.status_code,
            seed.status_label,
            seed.description
        FROM auditcore.projects p
        CROSS JOIN (
            VALUES
                ('DELIVERY_STARTED', 'Delivery Started', 'Physical Delivery workflow has started.'),
                ('DELIVERY_IN_PROGRESS', 'Delivery In Progress', 'Physical Delivery workflow is in progress.'),
                ('DELIVERY_COMPLETED', 'Delivery Completed', 'Physical Delivery has been recorded as completed.')
        ) AS seed(status_code, status_label, description)
        ON CONFLICT (tenant_id, domain_key, status_code) DO NOTHING
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.seed_uc03_delivery_status_codes()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            INSERT INTO auditcore.business_status_codes (
                tenant_id, domain_key, status_code, status_label, description
            ) VALUES
                (
                    NEW.tenant_id, 'DELIVERY', 'DELIVERY_STARTED',
                    'Delivery Started', 'Physical Delivery workflow has started.'
                ),
                (
                    NEW.tenant_id, 'DELIVERY', 'DELIVERY_IN_PROGRESS',
                    'Delivery In Progress', 'Physical Delivery workflow is in progress.'
                ),
                (
                    NEW.tenant_id, 'DELIVERY', 'DELIVERY_COMPLETED',
                    'Delivery Completed', 'Physical Delivery has been recorded as completed.'
                )
            ON CONFLICT (tenant_id, domain_key, status_code) DO NOTHING;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_uc03_project_delivery_status_codes
        AFTER INSERT ON auditcore.projects
        FOR EACH ROW EXECUTE FUNCTION auditcore.seed_uc03_delivery_status_codes()
        """
    )

    # Delivery business status already lives in auditcore.deliveries and
    # auditcore.delivery_status_history. C2 adds only the missing audit facts
    # required for intimation and VIN/chassis reconciliation.
    op.execute(
        """
        CREATE TABLE auditcore.journey_delivery_audit_facts (
            tenant_id                    varchar(128) NOT NULL,
            journey_id                   uuid NOT NULL,
            intimation_answer            varchar(20) NOT NULL DEFAULT 'UNANSWERED'
                                         CHECK (intimation_answer IN ('YES','NO','UNANSWERED')),
            non_intimation_reason         text,
            observed_vin                  varchar(120),
            observed_chassis_number       varchar(120),
            observed_source_evidence_id   uuid,
            vin_reconciliation_status     varchar(30) NOT NULL DEFAULT 'NOT_EVALUATED'
                                         CHECK (vin_reconciliation_status IN (
                                             'NOT_EVALUATED','MATCH','MISMATCH','REVIEW_REQUIRED'
                                         )),
            vin_evaluator_key             varchar(120),
            vin_evaluated_at_utc          timestamptz,
            updated_by_actor_id           varchar(160),
            version_no                    bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
            created_at_utc                timestamptz NOT NULL DEFAULT now(),
            updated_at_utc                timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, journey_id),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id),
            FOREIGN KEY (tenant_id, observed_source_evidence_id)
                REFERENCES auditcore.evidence(tenant_id, evidence_id),
            CHECK (
                intimation_answer <> 'NO'
                OR NULLIF(btrim(non_intimation_reason), '') IS NOT NULL
            )
        )
        """
    )
    op.execute(
        "ALTER TABLE auditcore.journey_delivery_audit_facts ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE auditcore.journey_delivery_audit_facts FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        """
        CREATE POLICY tenant_isolation_journey_delivery_audit_facts
        ON auditcore.journey_delivery_audit_facts
        USING (tenant_id = auditcore.current_tenant_id())
        WITH CHECK (tenant_id = auditcore.current_tenant_id())
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_journey_delivery_audit_facts_updated
        BEFORE UPDATE ON auditcore.journey_delivery_audit_facts
        FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at()
        """
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON auditcore.journey_delivery_audit_facts TO {_RUNTIME_ROLE}"
    )
    op.execute(
        f"REVOKE DELETE ON auditcore.journey_delivery_audit_facts FROM {_RUNTIME_ROLE}"
    )

    # Snapshot configured Delivery requirements when Delivery starts. This is
    # additive to the existing Booking trigger and preserves the same Journey.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.initialize_uc03_delivery_requirements()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.stage_code <> 'DELIVERY' THEN
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
              AND upper(dri.process_area) = 'DELIVERY'
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_uc03_delivery_initialize_requirements
        AFTER INSERT ON auditcore.journey_stage_states
        FOR EACH ROW EXECUTE FUNCTION auditcore.initialize_uc03_delivery_requirements()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_uc03_delivery_initialize_requirements "
        "ON auditcore.journey_stage_states"
    )
    op.execute("DROP FUNCTION IF EXISTS auditcore.initialize_uc03_delivery_requirements()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_journey_delivery_audit_facts_updated "
        "ON auditcore.journey_delivery_audit_facts"
    )
    op.execute("DROP TABLE IF EXISTS auditcore.journey_delivery_audit_facts")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_uc03_project_delivery_status_codes "
        "ON auditcore.projects"
    )
    op.execute("DROP FUNCTION IF EXISTS auditcore.seed_uc03_delivery_status_codes()")
