"""Keep UC03 Part-1 evidence rules for Projects created after this release."""
from alembic import op

revision = "0022_uc03_part1_default_profile"
down_revision = "0021_uc03_receipt_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
                ('pan_card', 'pan_card', 'BOOKING', 'OPTIONAL', '{}', 20),
                ('aadhaar', 'aadhaar', 'BOOKING', 'OPTIONAL', '{}', 30),
                ('booking_payment_receipt', 'dealer_receipt', 'BOOKING', 'REQUIRED', '{}', 40),
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


def downgrade() -> None:
    # The function body is forward-compatible with the prior schema and leaving it
    # in place avoids recreating the known-invalid PAN+AADHAAR mandatory default.
    pass
