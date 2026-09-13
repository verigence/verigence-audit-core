"""0092_delivery_req_seed_fn — callable Delivery requirement seeding.

Unified Booking/Delivery capture screen: the DI document-link webhook
(``acknowledge_booking_document_link_with_auto_sync``) requires every
uploaded document's classification to bind to an EXISTING
``journey_document_requirements`` row (``_discover_requirement_for_
callback`` hard-fails with VAC-NF-006 otherwise) -- but Delivery's own
requirement rows only get created today by
``initialize_uc03_delivery_requirements()``, a trigger that fires on
INSERT into ``journey_stage_states`` for stage_code='DELIVERY', i.e. only
once Delivery has actually "started". A single upload screen with no
upfront stage picker needs to offer Delivery's document types to DI's
classifier (and have a real requirement to bind a match to) even before
Delivery has started -- otherwise a genuinely Delivery-relevant document
uploaded early would misclassify or fail the webhook outright.

This extracts the trigger's own INSERT logic into a standalone, directly
callable function, ``auditcore.seed_delivery_document_requirements``, so
the unified upload-intent endpoint can call it EAGERLY (materializing the
requirement rows without starting Delivery -- no journey_stage_states
row, no "Booking incomplete" flag, nothing business-meaningful happens)
while the trigger keeps calling the exact same function when Delivery
actually starts. One body, two callers, zero drift risk -- rather than a
second, hand-copied list of Delivery's document types in Python that
could silently fall out of sync with this function.

Idempotent either way (``ON CONFLICT DO NOTHING`` throughout, as today).
"""
from __future__ import annotations

from alembic import op

revision = "0092_delivery_req_seed_fn"
down_revision = "0091_retire_booking_bank_stmt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.seed_delivery_document_requirements(
            p_tenant_id varchar, p_journey_id uuid
        )
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        BEGIN
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
            WHERE j.tenant_id = p_tenant_id
              AND j.journey_id = p_journey_id
              AND upper(dri.process_area) = 'DELIVERY'
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            ) VALUES (
                p_tenant_id, p_journey_id, NULL,
                'delivery_bank_statement', 'bank_statement_extract', 'DELIVERY',
                'OPTIONAL', 'PENDING', '{}'::jsonb
            )
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            ) VALUES
                (p_tenant_id, p_journey_id, NULL,
                 'delivery_credit_note', 'credit_note', 'DELIVERY',
                 'OPTIONAL', 'PENDING', '{}'::jsonb),
                (p_tenant_id, p_journey_id, NULL,
                 'delivery_gst_declaration', 'gst_declaration', 'DELIVERY',
                 'OPTIONAL', 'PENDING', '{}'::jsonb)
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            ) VALUES (
                p_tenant_id, p_journey_id, NULL,
                'delivery_scrappage_certificate', 'scrappage_certificate_of_deposit', 'DELIVERY',
                'OPTIONAL', 'PENDING', '{}'::jsonb
            )
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;
        END;
        $$
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

            PERFORM auditcore.seed_delivery_document_requirements(
                NEW.tenant_id, NEW.journey_id
            );

            RETURN NEW;
        END;
        $$
        """
    )


def downgrade() -> None:
    # Restore the trigger to its pre-0092 body (0078's own, inlined) and
    # drop the now-unused standalone function.
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

            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            ) VALUES (
                NEW.tenant_id, NEW.journey_id, NULL,
                'delivery_bank_statement', 'bank_statement_extract', 'DELIVERY',
                'OPTIONAL', 'PENDING', '{}'::jsonb
            )
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            ) VALUES
                (NEW.tenant_id, NEW.journey_id, NULL,
                 'delivery_credit_note', 'credit_note', 'DELIVERY',
                 'OPTIONAL', 'PENDING', '{}'::jsonb),
                (NEW.tenant_id, NEW.journey_id, NULL,
                 'delivery_gst_declaration', 'gst_declaration', 'DELIVERY',
                 'OPTIONAL', 'PENDING', '{}'::jsonb)
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            ) VALUES (
                NEW.tenant_id, NEW.journey_id, NULL,
                'delivery_scrappage_certificate', 'scrappage_certificate_of_deposit', 'DELIVERY',
                'OPTIONAL', 'PENDING', '{}'::jsonb
            )
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute("DROP FUNCTION IF EXISTS auditcore.seed_delivery_document_requirements(varchar, uuid)")
