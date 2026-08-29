"""Add isolated UC03 Document Capture V2 state.

Revision ID: 0036_uc03_document_capture_v2
Revises: 0035_schema_v2_fact_lineage
Create Date: 2026-08-29

V2 is additive. Legacy document requirement profiles, V1 endpoints and evidence
storage are not changed. The existing requirement master remains the base list;
V2-only policy adds/overrides presentation/applicability metadata without changing
what legacy clients see.
"""
from alembic import op

revision = "0036_uc03_document_capture_v2"
down_revision = "0035_schema_v2_fact_lineage"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auditcore.document_capture_v2_requirement_policy (
            requirement_key          varchar(160) PRIMARY KEY,
            process_area             varchar(20) NOT NULL
                                     CHECK (process_area IN ('BOOKING','DELIVERY')),
            display_label            varchar(240) NOT NULL,
            condition_key            varchar(120),
            extension_document_type_key varchar(120),
            extension_requirement_level varchar(20)
                                     CHECK (
                                         extension_requirement_level IS NULL
                                         OR extension_requirement_level IN ('REQUIRED','CONDITIONAL','OPTIONAL')
                                     ),
            sort_order               integer,
            is_extension             boolean NOT NULL DEFAULT false,
            is_active                boolean NOT NULL DEFAULT true,
            created_at_utc           timestamptz NOT NULL DEFAULT now(),
            updated_at_utc           timestamptz NOT NULL DEFAULT now(),
            CHECK (
                (is_extension = false
                 AND extension_document_type_key IS NULL
                 AND extension_requirement_level IS NULL)
                OR
                (is_extension = true
                 AND extension_document_type_key IS NOT NULL
                 AND extension_requirement_level IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        INSERT INTO auditcore.document_capture_v2_requirement_policy (
            requirement_key, process_area, display_label, condition_key,
            extension_document_type_key, extension_requirement_level,
            sort_order, is_extension
        ) VALUES
            ('booking_docket', 'BOOKING', 'Booking Form', NULL, NULL, NULL, 10, false),
            ('pan_card', 'BOOKING', 'PAN', NULL, NULL, NULL, 20, false),
            ('aadhaar', 'BOOKING', 'Aadhaar', NULL, NULL, NULL, 30, false),
            ('minimum_booking_payment_proof', 'BOOKING', 'Minimum Booking Amount Proof', NULL, NULL, NULL, 40, false),
            ('gst_certificate', 'BOOKING', 'GST Certificate', 'gstApplicable', NULL, NULL, 50, false),
            ('corporate_id', 'BOOKING', 'Corporate ID', 'corporateCustomer', 'corporate_id', 'CONDITIONAL', 60, true),
            ('trade_in_vehicle_rc', 'BOOKING', 'Trade-In RC', 'exchangeTaken', NULL, NULL, 70, false),
            ('trade_in_transfer_letter', 'BOOKING', 'Trade-In Transfer Letter', 'exchangeTaken', NULL, NULL, 80, false),
            ('trade_in_authorization_letter', 'BOOKING', 'Trade-In Authorization Letter', 'exchangeTaken', NULL, NULL, 90, false)
        ON CONFLICT (requirement_key) DO NOTHING
        """
    )

    op.execute(
        """
        CREATE TABLE auditcore.document_capture_v2_documents (
            tenant_id               varchar(128) NOT NULL,
            journey_id              uuid NOT NULL,
            stage_code              varchar(20) NOT NULL
                                    CHECK (stage_code IN ('BOOKING','DELIVERY')),
            di_document_id          uuid NOT NULL,
            client_upload_id        varchar(160) NOT NULL,
            requirement_key         varchar(160),
            classified_document_type_key varchar(120),
            capture_status          varchar(30) NOT NULL DEFAULT 'RECEIVING'
                                    CHECK (capture_status IN (
                                        'RECEIVING','STORED','CLASSIFYING','CLASSIFIED',
                                        'UNKNOWN','FAILED','SUPERSEDED'
                                    )),
            original_filename       varchar(500),
            content_type            varchar(160),
            created_by_actor_id     varchar(160) NOT NULL,
            classified_at_utc       timestamptz,
            created_at_utc          timestamptz NOT NULL DEFAULT now(),
            updated_at_utc          timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, journey_id, di_document_id),
            UNIQUE (tenant_id, journey_id, client_upload_id),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_document_capture_v2_documents_active
        ON auditcore.document_capture_v2_documents
            (tenant_id, journey_id, stage_code, capture_status, requirement_key)
        """
    )

    op.execute(
        """
        CREATE TABLE auditcore.document_capture_v2_declarations (
            tenant_id               varchar(128) NOT NULL,
            journey_id              uuid NOT NULL,
            stage_code              varchar(20) NOT NULL
                                    CHECK (stage_code IN ('BOOKING','DELIVERY')),
            condition_key           varchar(120) NOT NULL,
            applicable              boolean NOT NULL,
            document_available      boolean,
            declared_by_actor_id    varchar(160) NOT NULL,
            declared_at_utc         timestamptz NOT NULL DEFAULT now(),
            updated_at_utc          timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, journey_id, stage_code, condition_key),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id),
            CHECK (applicable OR document_available IS NULL),
            CHECK (NOT applicable OR document_available IS NOT NULL)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE auditcore.document_capture_v2_source_truth_rules (
            source_sheet_row         integer PRIMARY KEY,
            section_label            varchar(240),
            attribute_label          varchar(240) NOT NULL,
            attribute_type           varchar(240),
            source_labels            jsonb NOT NULL DEFAULT '[]'::jsonb
                                     CHECK (jsonb_typeof(source_labels) = 'array'),
            final_source_label       varchar(300),
            final_document_type_key  varchar(120),
            due_stage                varchar(20)
                                     CHECK (due_stage IS NULL OR due_stage IN ('BOOKING','DELIVERY')),
            created_at_utc           timestamptz NOT NULL DEFAULT now(),
            updated_at_utc           timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    for table in (
        "document_capture_v2_documents",
        "document_capture_v2_declarations",
    ):
        op.execute(f"ALTER TABLE auditcore.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE auditcore.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation_{table}
            ON auditcore.{table}
            USING (tenant_id = auditcore.current_tenant_id())
            WITH CHECK (tenant_id = auditcore.current_tenant_id())
            """
        )

    op.execute(
        """
        CREATE TRIGGER trg_document_capture_v2_policy_updated
        BEFORE UPDATE ON auditcore.document_capture_v2_requirement_policy
        FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_document_capture_v2_documents_updated
        BEFORE UPDATE ON auditcore.document_capture_v2_documents
        FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_document_capture_v2_declarations_updated
        BEFORE UPDATE ON auditcore.document_capture_v2_declarations
        FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_document_capture_v2_source_truth_updated
        BEFORE UPDATE ON auditcore.document_capture_v2_source_truth_rules
        FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at()
        """
    )

    op.execute(
        f"GRANT SELECT ON auditcore.document_capture_v2_requirement_policy TO {_RUNTIME_ROLE}"
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON auditcore.document_capture_v2_documents TO {_RUNTIME_ROLE}"
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON auditcore.document_capture_v2_declarations TO {_RUNTIME_ROLE}"
    )
    op.execute(
        f"GRANT SELECT ON auditcore.document_capture_v2_source_truth_rules TO {_RUNTIME_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_document_capture_v2_source_truth_updated "
        "ON auditcore.document_capture_v2_source_truth_rules"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_document_capture_v2_declarations_updated "
        "ON auditcore.document_capture_v2_declarations"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_document_capture_v2_documents_updated "
        "ON auditcore.document_capture_v2_documents"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_document_capture_v2_policy_updated "
        "ON auditcore.document_capture_v2_requirement_policy"
    )
    op.execute("DROP TABLE IF EXISTS auditcore.document_capture_v2_source_truth_rules")
    op.execute("DROP TABLE IF EXISTS auditcore.document_capture_v2_declarations")
    op.execute("DROP TABLE IF EXISTS auditcore.document_capture_v2_documents")
    op.execute("DROP TABLE IF EXISTS auditcore.document_capture_v2_requirement_policy")
