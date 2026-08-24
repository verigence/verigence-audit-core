"""Seed and enforce the UC03 Booking/Delivery document requirements.

The Project owns a versioned document-requirement profile. Every Project gets a
usable default profile from the approved business process; Admins may supersede it
with a later version, but an empty profile can no longer be published. Existing
Journeys created against an empty profile are repaired without recreating them.
"""
from alembic import op

revision = "0016_uc02_project_delete_uc03"
down_revision = "0016_uc02_project_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE auditcore.document_requirement_profile_versions v
        SET lifecycle_status='RETIRED',
            retired_by_actor_id='migration.uc03-default-documents',
            retired_at_utc=now(),
            updated_at_utc=now()
        WHERE v.lifecycle_status='PUBLISHED'
          AND NOT EXISTS (
              SELECT 1
              FROM auditcore.document_requirement_items i
              WHERE i.tenant_id=v.tenant_id
                AND i.document_requirement_profile_version_id=
                    v.document_requirement_profile_version_id
          )
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.validate_document_profile_publish()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            booking_count integer;
            delivery_count integer;
        BEGIN
            IF NEW.lifecycle_status='PUBLISHED'
               AND OLD.lifecycle_status IS DISTINCT FROM 'PUBLISHED' THEN
                SELECT
                    count(*) FILTER (WHERE upper(process_area)='BOOKING'),
                    count(*) FILTER (WHERE upper(process_area)='DELIVERY')
                INTO booking_count, delivery_count
                FROM auditcore.document_requirement_items
                WHERE tenant_id=NEW.tenant_id
                  AND document_requirement_profile_version_id=
                      NEW.document_requirement_profile_version_id;

                IF booking_count = 0 OR delivery_count = 0 THEN
                    RAISE EXCEPTION
                        'Document Requirement Profile must contain Booking and Delivery requirements before publication';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_document_profile_publish_content
        ON auditcore.document_requirement_profile_versions
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_document_profile_publish_content
        BEFORE UPDATE OF lifecycle_status
        ON auditcore.document_requirement_profile_versions
        FOR EACH ROW
        EXECUTE FUNCTION auditcore.validate_document_profile_publish()
        """
    )

    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION auditcore.ensure_uc03_default_document_profile(
            p_tenant_id varchar,
            p_effective_from date
        ) RETURNS uuid
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_profile_id uuid;
            v_version_id uuid;
            v_version_no integer;
        BEGIN
            INSERT INTO auditcore.document_requirement_profiles (
                tenant_id, profile_code, profile_name, created_by_actor_id
            ) VALUES (
                p_tenant_id,
                'UC03_DEFAULT_VEHICLE_SALES',
                'Default Booking & Delivery Documents',
                'system.uc03-default-documents'
            )
            ON CONFLICT (tenant_id, profile_code) DO NOTHING;

            SELECT document_requirement_profile_id
            INTO v_profile_id
            FROM auditcore.document_requirement_profiles
            WHERE tenant_id=p_tenant_id
              AND profile_code='UC03_DEFAULT_VEHICLE_SALES';

            SELECT v.document_requirement_profile_version_id
            INTO v_version_id
            FROM auditcore.document_requirement_profile_versions v
            WHERE v.tenant_id=p_tenant_id
              AND v.document_requirement_profile_id=v_profile_id
              AND v.lifecycle_status='PUBLISHED'
              AND EXISTS (
                  SELECT 1 FROM auditcore.document_requirement_items i
                  WHERE i.tenant_id=v.tenant_id
                    AND i.document_requirement_profile_version_id=
                        v.document_requirement_profile_version_id
                    AND upper(i.process_area)='BOOKING'
              )
              AND EXISTS (
                  SELECT 1 FROM auditcore.document_requirement_items i
                  WHERE i.tenant_id=v.tenant_id
                    AND i.document_requirement_profile_version_id=
                        v.document_requirement_profile_version_id
                    AND upper(i.process_area)='DELIVERY'
              )
            ORDER BY v.effective_from DESC, v.version_no DESC
            LIMIT 1;

            IF v_version_id IS NOT NULL THEN
                RETURN v_version_id;
            END IF;

            SELECT COALESCE(max(version_no), 0) + 1
            INTO v_version_no
            FROM auditcore.document_requirement_profile_versions
            WHERE tenant_id=p_tenant_id
              AND document_requirement_profile_id=v_profile_id;

            INSERT INTO auditcore.document_requirement_profile_versions (
                tenant_id, document_requirement_profile_id, version_no,
                effective_from, lifecycle_status, created_by_actor_id
            ) VALUES (
                p_tenant_id, v_profile_id, v_version_no,
                p_effective_from, 'DRAFT', 'system.uc03-default-documents'
            )
            RETURNING document_requirement_profile_version_id INTO v_version_id;

            INSERT INTO auditcore.document_requirement_items (
                tenant_id, document_requirement_profile_version_id,
                requirement_key, document_type_key, process_area,
                requirement_level, condition_config, sort_order
            )
            SELECT p_tenant_id, v_version_id,
                   x.requirement_key, x.document_type_key, x.process_area,
                   x.requirement_level, x.condition_config::jsonb, x.sort_order
            FROM (VALUES
                ('booking_docket', 'booking_docket', 'BOOKING', 'REQUIRED', '{}', 10),
                ('pan_card', 'pan_card', 'BOOKING', 'REQUIRED', '{}', 20),
                ('aadhaar', 'aadhaar', 'BOOKING', 'REQUIRED', '{}', 30),
                ('minimum_booking_payment_proof', 'minimum_booking_payment_proof', 'BOOKING', 'REQUIRED', '{}', 40),
                ('gst_certificate', 'gst_certificate', 'BOOKING', 'CONDITIONAL', '{"conditionKey":"corporateCustomer"}', 50),
                ('trade_in_vehicle_rc', 'vehicle_rc', 'BOOKING', 'CONDITIONAL', '{"conditionKey":"exchangeTaken"}', 60),
                ('trade_in_transfer_letter', 'transfer_letter', 'BOOKING', 'OPTIONAL', '{}', 70),
                ('trade_in_authorization_letter', 'authorization_letter', 'BOOKING', 'OPTIONAL', '{}', 80),
                ('wholesale_invoice', 'wholesale_invoice', 'DELIVERY', 'REQUIRED', '{}', 110),
                ('customer_invoice_dms', 'customer_invoice_dms', 'DELIVERY', 'REQUIRED', '{}', 120),
                ('tax_invoice_tally', 'tax_invoice_tally', 'DELIVERY', 'REQUIRED', '{}', 130),
                ('insurance_cover_note', 'insurance_cover', 'DELIVERY', 'REQUIRED', '{}', 140),
                ('accessory_invoice_dms', 'accessory_invoice_dms', 'DELIVERY', 'REQUIRED', '{}', 150),
                ('accessory_invoice_tally', 'accessory_invoice_tally', 'DELIVERY', 'REQUIRED', '{}', 160),
                ('rto_challan', 'rto_challan', 'DELIVERY', 'REQUIRED', '{}', 170),
                ('customer_ledger', 'customer_ledger', 'DELIVERY', 'REQUIRED', '{}', 180),
                ('cost_sheet', 'cost_sheet', 'DELIVERY', 'REQUIRED', '{}', 190),
                ('gate_pass', 'gate_pass', 'DELIVERY', 'REQUIRED', '{}', 200),
                ('customer_kyc', 'customer_kyc', 'DELIVERY', 'REQUIRED', '{}', 210),
                ('ew_invoice', 'ew_invoice', 'DELIVERY', 'REQUIRED', '{}', 220),
                ('rsa_invoice', 'rsa_invoice', 'DELIVERY', 'REQUIRED', '{}', 230),
                ('value_added_service_document', 'value_added_service_document', 'DELIVERY', 'OPTIONAL', '{}', 240),
                ('no_dues_certificate', 'no_dues_certificate', 'DELIVERY', 'REQUIRED', '{}', 250),
                ('payment_receipt', 'payment_receipt', 'DELIVERY', 'REQUIRED', '{}', 260)
            ) AS x(requirement_key, document_type_key, process_area,
                   requirement_level, condition_config, sort_order);

            UPDATE auditcore.document_requirement_profile_versions
            SET lifecycle_status='PUBLISHED',
                published_by_actor_id='system.uc03-default-documents',
                published_at_utc=now(),
                updated_at_utc=now()
            WHERE tenant_id=p_tenant_id
              AND document_requirement_profile_version_id=v_version_id
              AND lifecycle_status='DRAFT';

            RETURN v_version_id;
        END;
        $$
        """
    )

    op.execute(
        """
        SELECT auditcore.ensure_uc03_default_document_profile(
            p.tenant_id,
            CASE
                WHEN p.effective_start_date > CURRENT_DATE THEN p.effective_start_date
                ELSE CURRENT_DATE
            END
        )
        FROM auditcore.projects p
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.seed_uc03_default_documents_for_project()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            PERFORM auditcore.ensure_uc03_default_document_profile(
                NEW.tenant_id,
                NEW.effective_start_date
            );
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_uc03_project_default_documents
        ON auditcore.projects
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_uc03_project_default_documents
        AFTER INSERT ON auditcore.projects
        FOR EACH ROW
        EXECUTE FUNCTION auditcore.seed_uc03_default_documents_for_project()
        """
    )

    op.execute(
        """
        WITH defaults AS (
            SELECT DISTINCT ON (v.tenant_id)
                   v.tenant_id,
                   v.document_requirement_profile_version_id AS default_version_id
            FROM auditcore.document_requirement_profile_versions v
            JOIN auditcore.document_requirement_profiles p
              ON p.tenant_id=v.tenant_id
             AND p.document_requirement_profile_id=v.document_requirement_profile_id
            WHERE p.profile_code='UC03_DEFAULT_VEHICLE_SALES'
              AND v.lifecycle_status='PUBLISHED'
            ORDER BY v.tenant_id, v.effective_from DESC, v.version_no DESC
        )
        UPDATE auditcore.journeys j
        SET document_requirement_profile_version_id = d.default_version_id,
            updated_at_utc=now()
        FROM defaults d
        WHERE d.tenant_id=j.tenant_id
          AND NOT EXISTS (
                  SELECT 1 FROM auditcore.journey_document_requirements r
                  WHERE r.tenant_id=j.tenant_id AND r.journey_id=j.journey_id
              )
          AND (
              j.document_requirement_profile_version_id IS NULL
              OR NOT EXISTS (
                  SELECT 1 FROM auditcore.document_requirement_items i
                  WHERE i.tenant_id=j.tenant_id
                    AND i.document_requirement_profile_version_id=
                        j.document_requirement_profile_version_id
              )
          )
        """
    )

    op.execute(
        """
        INSERT INTO auditcore.journey_document_requirements (
            tenant_id, journey_id, document_requirement_item_id,
            requirement_key, document_type_key, process_area,
            requirement_level, requirement_status, condition_snapshot
        )
        SELECT j.tenant_id, j.journey_id, i.document_requirement_item_id,
               i.requirement_key, i.document_type_key, i.process_area,
               i.requirement_level, 'PENDING', i.condition_config
        FROM auditcore.journeys j
        JOIN auditcore.journey_stage_states s
          ON s.tenant_id=j.tenant_id AND s.journey_id=j.journey_id
        JOIN auditcore.document_requirement_items i
          ON i.tenant_id=j.tenant_id
         AND i.document_requirement_profile_version_id=
             j.document_requirement_profile_version_id
         AND upper(i.process_area)=s.stage_code
        WHERE s.stage_code IN ('BOOKING','DELIVERY')
        ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_uc03_project_default_documents ON auditcore.projects"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS auditcore.seed_uc03_default_documents_for_project()"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_document_profile_publish_content "
        "ON auditcore.document_requirement_profile_versions"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS auditcore.validate_document_profile_publish()"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS auditcore.ensure_uc03_default_document_profile(varchar,date)"
    )
