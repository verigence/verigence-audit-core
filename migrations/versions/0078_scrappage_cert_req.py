"""Register scrappage_certificate_of_deposit as an ordinary, non-checklist document type.

Revision ID: 0078_scrappage_cert_req
Revises: 0077_finance_hypothecation
Create Date: 2026-09-10

Real uploaded Vehicle Scrappage Certificates of Deposit (a "Certificate of
Deposit" issued by an RVSF, and its "Transfer Certificate of Deposit"
recording a resale) had nowhere to land on the Audit Core side, mirroring the
DI-side gap just closed in verigence-di#71 (new document type
``scrappage_certificate_of_deposit`` + extraction profile).

This is the exact gap ``uc03_booking_confirmation_rules.py`` has referenced
since migration 0072/0033: the Booking Form's own ``scrappage_discount_amount``
is cross-checked against required supporting evidence, but scrappage always
raised unconditionally because "Scrappage has no registered, classifiable
document type in DI yet." That module is updated in the same PR to actually
check presence now that this exists.

Same pattern as 0070 (bank_statement_extract) and 0073 (credit_note/
gst_declaration): ``journey_document_requirements`` rows with
``document_requirement_item_id = NULL`` -- present so DI can classify against
it and evidence/materialization treat it like any other document, excluded
from the "PC must declare every applicable checklist item" audit-completion
gate. Registered for both Booking and Delivery -- a customer may already hold
a certificate at Booking time, or only produce it once RTO registration
benefits are actually claimed at Delivery.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0078_scrappage_cert_req"
down_revision = "0077_finance_hypothecation"
branch_labels = None
depends_on = None

_DOCUMENT_TYPE = "scrappage_certificate_of_deposit"
_BOOKING_KEY = "booking_scrappage_certificate"
_DELIVERY_KEY = "delivery_scrappage_certificate"
_DISPLAY_LABEL = "Scrappage Certificate of Deposit"


def upgrade() -> None:
    bind = op.get_bind()

    # 1a. Backfill existing Booking-stage journeys.
    bind.execute(
        text(
            """
            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            )
            SELECT j.tenant_id, j.journey_id, NULL::uuid,
                   :requirement_key, :document_type_key, 'BOOKING',
                   'OPTIONAL', 'PENDING', '{}'::jsonb
            FROM auditcore.journeys j
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING
            """
        ),
        {"requirement_key": _BOOKING_KEY, "document_type_key": _DOCUMENT_TYPE},
    )

    # 1b. Backfill journeys that have already started Delivery.
    bind.execute(
        text(
            """
            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            )
            SELECT DISTINCT jss.tenant_id, jss.journey_id, NULL::uuid,
                   :requirement_key, :document_type_key, 'DELIVERY',
                   'OPTIONAL', 'PENDING', '{}'::jsonb
            FROM auditcore.journey_stage_states jss
            WHERE jss.stage_code = 'DELIVERY'
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING
            """
        ),
        {"requirement_key": _DELIVERY_KEY, "document_type_key": _DOCUMENT_TYPE},
    )

    # 2. Presentation metadata (falls back to the requirement_key otherwise).
    bind.execute(
        text(
            """
            INSERT INTO auditcore.document_capture_v2_requirement_policy (
                requirement_key, process_area, display_label, condition_key,
                extension_document_type_key, extension_requirement_level,
                sort_order, is_extension, is_active
            ) VALUES
                (:booking_key, 'BOOKING', :display_label, NULL, NULL, NULL, 47, false, true),
                (:delivery_key, 'DELIVERY', :display_label, NULL, NULL, NULL, 257, false, true)
            ON CONFLICT (requirement_key) DO UPDATE
            SET process_area=EXCLUDED.process_area,
                display_label=EXCLUDED.display_label,
                sort_order=EXCLUDED.sort_order,
                is_active=true,
                updated_at_utc=now()
            """
        ),
        {"booking_key": _BOOKING_KEY, "delivery_key": _DELIVERY_KEY, "display_label": _DISPLAY_LABEL},
    )

    # 3. Patch the Booking snapshot trigger (0073's body) to also add the new
    # requirement, unconditionally, alongside everything it already provides.
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
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            )
            SELECT
                j.tenant_id,
                j.journey_id,
                dri.document_requirement_item_id,
                CASE
                    WHEN dri.requirement_key='minimum_booking_payment_proof'
                        THEN 'booking_payment_receipt'
                    ELSE dri.requirement_key
                END,
                CASE
                    WHEN dri.requirement_key='minimum_booking_payment_proof'
                        THEN 'dealer_receipt'
                    ELSE dri.document_type_key
                END,
                dri.process_area,
                CASE
                    WHEN dri.requirement_key IN ('pan_card','aadhaar')
                        THEN 'OPTIONAL'
                    ELSE dri.requirement_level
                END,
                'PENDING',
                dri.condition_config
            FROM auditcore.journeys j
            JOIN auditcore.document_requirement_items dri
              ON dri.tenant_id=j.tenant_id
             AND dri.document_requirement_profile_version_id=
                    j.document_requirement_profile_version_id
            WHERE j.tenant_id=NEW.tenant_id
              AND j.journey_id=NEW.journey_id
              AND upper(dri.process_area)='BOOKING'
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            ) VALUES (
                NEW.tenant_id, NEW.journey_id, NULL,
                'booking_bank_statement', 'bank_statement_extract', 'BOOKING',
                'OPTIONAL', 'PENDING', '{}'::jsonb
            )
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            ) VALUES
                (NEW.tenant_id, NEW.journey_id, NULL,
                 'booking_credit_note', 'credit_note', 'BOOKING',
                 'OPTIONAL', 'PENDING', '{}'::jsonb),
                (NEW.tenant_id, NEW.journey_id, NULL,
                 'booking_gst_declaration', 'gst_declaration', 'BOOKING',
                 'OPTIONAL', 'PENDING', '{}'::jsonb)
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            ) VALUES (
                NEW.tenant_id, NEW.journey_id, NULL,
                'booking_scrappage_certificate', 'scrappage_certificate_of_deposit', 'BOOKING',
                'OPTIONAL', 'PENDING', '{}'::jsonb
            )
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            RETURN NEW;
        END;
        $$
        """
    )

    # 4. Patch the Delivery snapshot trigger (0073's body) the same way.
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


def downgrade() -> None:
    # Restore the trigger functions to their pre-0078 bodies (0073's own).
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
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            )
            SELECT
                j.tenant_id,
                j.journey_id,
                dri.document_requirement_item_id,
                CASE
                    WHEN dri.requirement_key='minimum_booking_payment_proof'
                        THEN 'booking_payment_receipt'
                    ELSE dri.requirement_key
                END,
                CASE
                    WHEN dri.requirement_key='minimum_booking_payment_proof'
                        THEN 'dealer_receipt'
                    ELSE dri.document_type_key
                END,
                dri.process_area,
                CASE
                    WHEN dri.requirement_key IN ('pan_card','aadhaar')
                        THEN 'OPTIONAL'
                    ELSE dri.requirement_level
                END,
                'PENDING',
                dri.condition_config
            FROM auditcore.journeys j
            JOIN auditcore.document_requirement_items dri
              ON dri.tenant_id=j.tenant_id
             AND dri.document_requirement_profile_version_id=
                    j.document_requirement_profile_version_id
            WHERE j.tenant_id=NEW.tenant_id
              AND j.journey_id=NEW.journey_id
              AND upper(dri.process_area)='BOOKING'
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            ) VALUES (
                NEW.tenant_id, NEW.journey_id, NULL,
                'booking_bank_statement', 'bank_statement_extract', 'BOOKING',
                'OPTIONAL', 'PENDING', '{}'::jsonb
            )
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            ) VALUES
                (NEW.tenant_id, NEW.journey_id, NULL,
                 'booking_credit_note', 'credit_note', 'BOOKING',
                 'OPTIONAL', 'PENDING', '{}'::jsonb),
                (NEW.tenant_id, NEW.journey_id, NULL,
                 'booking_gst_declaration', 'gst_declaration', 'BOOKING',
                 'OPTIONAL', 'PENDING', '{}'::jsonb)
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            RETURN NEW;
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

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        DELETE FROM auditcore.document_capture_v2_requirement_policy
        WHERE requirement_key IN ('booking_scrappage_certificate', 'delivery_scrappage_certificate')
        """
    )
    # Backfilled journey_document_requirements rows are intentionally left in
    # place (0070/0073/0049/0020 precedent: mutable per-journey audit state
    # is not destructively removed on downgrade).
