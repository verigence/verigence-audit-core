"""Move customer_kyc from a Delivery-stage requirement to Booking.

Revision ID: 0109_customer_kyc_booking_stage
Revises: 0108_finance_disbursement
Create Date: 2026-09-21

Confirmed live: migration 0017/0022 seeded ``customer_kyc`` as a
Delivery-only requirement (``process_area='DELIVERY'``). A real Booking-side
KYC document classified as ``customer_kyc`` therefore has no requirement to
bind to under Booking at all -- ``resolve_document_stage`` only matches
Delivery's own requirement set for that type, and a document uploaded
against a stage where the type has no requirement is left with
``requirement_key=NULL``, permanently unable to link to evidence.

Per explicit product correction: customer_kyc is a Booking-stage
requirement, not Delivery. Both ``initialize_uc03_booking_requirements()``
and ``initialize_uc03_delivery_requirements()`` (0017/0073) read
``document_requirement_items.process_area`` live at journey-stage-start
time via a generic ``WHERE upper(process_area)=...`` join -- customer_kyc
was never one of the types hardcoded directly into either trigger body, so
moving the source row's process_area is sufficient to fix both triggers for
every future Booking/Delivery start, with no trigger-body edit needed.
"""
from __future__ import annotations

from alembic import op

revision = "0109_customer_kyc_booking_stage"
down_revision = "0108_finance_disbursement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. The master catalog every future Booking-stage-start reads from, for
    # every tenant that already has a published profile version.
    bind.execute(
        """
        UPDATE auditcore.document_requirement_items
        SET process_area = 'BOOKING'
        WHERE requirement_key = 'customer_kyc'
          AND upper(process_area) = 'DELIVERY'
        """
    )

    # 2. Every already-instantiated journey's own requirement row -- moved
    # in place (same requirement_key, same journey_document_requirement_id,
    # so any evidence already bound to it stays bound and simply now shows
    # under Booking).
    bind.execute(
        """
        UPDATE auditcore.journey_document_requirements
        SET process_area = 'BOOKING'
        WHERE requirement_key = 'customer_kyc'
          AND upper(process_area) = 'DELIVERY'
        """
    )

    # 3. ensure_uc03_default_document_profile (0022) only ever creates a new
    # profile version once per tenant -- steps 1-2 above are what fixes
    # every tenant that already has one. This step is purely so a brand new
    # tenant provisioned from today gets the correction from day one;
    # otherwise byte-identical to 0022's own function body.
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
                ('customer_kyc', 'customer_kyc', 'BOOKING', 'REQUIRED', '{}', 90),
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
    bind = op.get_bind()
    bind.execute(
        """
        UPDATE auditcore.document_requirement_items
        SET process_area = 'DELIVERY'
        WHERE requirement_key = 'customer_kyc'
          AND upper(process_area) = 'BOOKING'
        """
    )
    bind.execute(
        """
        UPDATE auditcore.journey_document_requirements
        SET process_area = 'DELIVERY'
        WHERE requirement_key = 'customer_kyc'
          AND upper(process_area) = 'BOOKING'
        """
    )
    # The pre-0109 function body (0022's) is restored so a fresh tenant
    # provisioned after a downgrade gets the old (Delivery-side) default
    # again, matching this repo's own precedent (0073's downgrade does the
    # same for its own trigger-function edits).
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
