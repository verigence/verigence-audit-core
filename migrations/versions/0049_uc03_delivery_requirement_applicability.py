"""Correct UC03 Delivery document applicability.

Revision ID: 0049_uc03_delivery_req_applicability
Revises: 0048_uc03_di_core_fields
Create Date: 2026-08-30

The original default Delivery profile marked conditional evidence such as
Accessories, Extended Warranty, RSA and dealer-registration evidence as REQUIRED.
That made the Delivery capture screen report every such document as mandatory.

This migration corrects both the versioned profile and already-snapshotted Journey
requirements. Delivery submission remains non-blocking; these levels are audit
expectations and presentation/applicability metadata only.
"""

from alembic import op

revision = "0049_uc03_delivery_req_applicability"
down_revision = "0048_uc03_di_core_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
        UPDATE auditcore.document_requirement_items i
        SET requirement_level='CONDITIONAL',
            condition_config=jsonb_build_object('conditionKey', c.condition_key)
        FROM corrections c
        WHERE i.requirement_key=c.requirement_key
          AND upper(i.process_area)='DELIVERY'
        """
    )

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


def downgrade() -> None:
    # Restore only the five levels changed by this corrective migration. The
    # original default profile used REQUIRED for each of them.
    op.execute(
        """
        UPDATE auditcore.document_requirement_items
        SET requirement_level='REQUIRED',
            condition_config='{}'::jsonb
        WHERE requirement_key IN (
            'accessory_invoice_dms', 'accessory_invoice_tally', 'rto_challan',
            'ew_invoice', 'rsa_invoice'
        )
          AND upper(process_area)='DELIVERY'
        """
    )
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
