"""Correct UC03 Delivery document applicability.

Revision ID: 0049_uc03_delivery_applicability
Revises: 0048_uc03_di_core_fields
Create Date: 2026-08-30

The original default Delivery profile marked conditional evidence such as
Accessories, Extended Warranty, RSA and dealer-registration evidence as REQUIRED.
That made Delivery capture report these documents as universally mandatory and
could raise false missing-document audit flags.

Published requirement masters remain immutable. This migration repairs effective
Journey snapshots already created and corrects the Delivery-start snapshot function
for future journeys. Delivery remains non-blocking.
"""

from alembic import op

revision = "0049_uc03_delivery_applicability"
down_revision = "0048_uc03_di_core_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Repair snapshots already created for active/existing journeys. These are the
    # effective per-Journey requirements and are intentionally mutable audit state,
    # unlike the published master version from which they were originally copied.
    op.execute(
        """
        WITH corrections(requirement_key, condition_key) AS (
            VALUES
                ('accessory_invoice_dms', 'accessoriesTaken'),
                ('accessory_invoice_tally', 'accessoriesTaken'),
                ('rto_challan', 'registrationByDealer'),
                ('ew_invoice', 'extendedWarrantyTaken'),
                ('rsa_invoice', 'rsaTaken')
        )
        UPDATE auditcore.journey_document_requirements jdr
        SET requirement_level='CONDITIONAL',
            condition_snapshot=jsonb_build_object('conditionKey', c.condition_key),
            updated_at_utc=now()
        FROM corrections c
        WHERE jdr.requirement_key=c.requirement_key
          AND upper(jdr.process_area)='DELIVERY'
        """
    )

    # V2 presentation/applicability metadata. This is not a second source of
    # business truth; it records the condition attached to the effective requirement.
    op.execute(
        """
        INSERT INTO auditcore.document_capture_v2_requirement_policy (
            requirement_key, process_area, display_label, condition_key,
            extension_document_type_key, extension_requirement_level,
            sort_order, is_extension, is_active
        ) VALUES
            ('accessory_invoice_dms', 'DELIVERY', 'Accessory Invoice - DMS', 'accessoriesTaken', NULL, NULL, 150, false, true),
            ('accessory_invoice_tally', 'DELIVERY', 'Accessory Invoice - Tally', 'accessoriesTaken', NULL, NULL, 160, false, true),
            ('rto_challan', 'DELIVERY', 'RTO Challan', 'registrationByDealer', NULL, NULL, 170, false, true),
            ('ew_invoice', 'DELIVERY', 'Extended Warranty Invoice', 'extendedWarrantyTaken', NULL, NULL, 220, false, true),
            ('rsa_invoice', 'DELIVERY', 'RSA Invoice', 'rsaTaken', NULL, NULL, 230, false, true)
        ON CONFLICT (requirement_key) DO UPDATE
        SET process_area=EXCLUDED.process_area,
            display_label=EXCLUDED.display_label,
            condition_key=EXCLUDED.condition_key,
            sort_order=EXCLUDED.sort_order,
            is_active=true,
            updated_at_utc=now()
        """
    )

    # Preserve published master immutability. When Delivery starts, normalize the
    # known conditional requirements while copying the profile into Journey state.
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
                CASE
                    WHEN dri.requirement_key IN (
                        'accessory_invoice_dms', 'accessory_invoice_tally',
                        'rto_challan', 'ew_invoice', 'rsa_invoice'
                    ) THEN 'CONDITIONAL'
                    ELSE dri.requirement_level
                END,
                'PENDING',
                CASE dri.requirement_key
                    WHEN 'accessory_invoice_dms' THEN jsonb_build_object('conditionKey', 'accessoriesTaken')
                    WHEN 'accessory_invoice_tally' THEN jsonb_build_object('conditionKey', 'accessoriesTaken')
                    WHEN 'rto_challan' THEN jsonb_build_object('conditionKey', 'registrationByDealer')
                    WHEN 'ew_invoice' THEN jsonb_build_object('conditionKey', 'extendedWarrantyTaken')
                    WHEN 'rsa_invoice' THEN jsonb_build_object('conditionKey', 'rsaTaken')
                    ELSE dri.condition_config
                END
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


def downgrade() -> None:
    op.execute(
        """
        UPDATE auditcore.journey_document_requirements
        SET requirement_level='REQUIRED',
            condition_snapshot='{}'::jsonb,
            updated_at_utc=now()
        WHERE requirement_key IN (
            'accessory_invoice_dms', 'accessory_invoice_tally', 'rto_challan',
            'ew_invoice', 'rsa_invoice'
        )
          AND upper(process_area)='DELIVERY'
        """
    )
    op.execute(
        """
        DELETE FROM auditcore.document_capture_v2_requirement_policy
        WHERE requirement_key IN (
            'accessory_invoice_dms', 'accessory_invoice_tally', 'rto_challan',
            'ew_invoice', 'rsa_invoice'
        )
          AND process_area='DELIVERY'
        """
    )
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
