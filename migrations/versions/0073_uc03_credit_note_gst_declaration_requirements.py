"""Register credit_note and gst_declaration as ordinary, non-checklist document types.

Revision ID: 0073_uc03_credit_note_gst_decl
Revises: 0072_uc03_booking_confirmation_rules
Create Date: 2026-09-10

Two real gaps found by reviewing actual uploaded Delivery documents on one
journey, both fixed on DI's side in verigence-di#69 (new document types +
extraction profiles: credit_note, gst_declaration; also payment_receipt and
customer_kyc finally getting their own profiles instead of DI's generic
fallback):

- A real Credit Note had no registered document type at all -- DI's
  classifier had no candidate to place it in.
- A real "Declaration of GST (for Sales Department)" form had nowhere
  correct to land either, so DI's classifier confidently misfiled it as
  customer_kyc for lack of a better bucket.

Same exact gap and same exact fix as 0070's bank_statement_extract: DI now
has a full classification category and extraction schema for both, but
Audit Core never told the V2 capture flow they were valid candidates for a
given journey (``_candidate_type_keys``/``_delivery_requirements`` only offer
document types with a registered ``journey_document_requirements`` row).

Neither is a checklist item a PC declares up front -- a credit note or a GST
declaration either gets uploaded because one exists for this deal, or it
doesn't. So both are registered exactly like 0070's bank_statement:
``journey_document_requirements`` rows with ``document_requirement_item_id =
NULL``, present so DI can classify against them and evidence/materialization
treat them like any other document, excluded from the "PC must declare every
applicable checklist item" audit-completion gate.

Registered for both Booking and Delivery (like bank_statement) since either
document can plausibly surface at either stage -- a credit note adjusting an
earlier invoice, or the GST declaration signed at the original sale but only
scanned in later.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0073_uc03_credit_note_gst_decl"
down_revision = "0072_uc03_booking_confirmation"
branch_labels = None
depends_on = None

_TYPES = (
    # document_type_key, booking requirement_key, delivery requirement_key, display label
    ("credit_note", "booking_credit_note", "delivery_credit_note", "Credit Note"),
    ("gst_declaration", "booking_gst_declaration", "delivery_gst_declaration", "GST Declaration"),
)


def upgrade() -> None:
    bind = op.get_bind()

    for document_type_key, booking_key, delivery_key, display_label in _TYPES:
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
            {"requirement_key": booking_key, "document_type_key": document_type_key},
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
            {"requirement_key": delivery_key, "document_type_key": document_type_key},
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
                    (:booking_key, 'BOOKING', :display_label, NULL, NULL, NULL, 46, false, true),
                    (:delivery_key, 'DELIVERY', :display_label, NULL, NULL, NULL, 256, false, true)
                ON CONFLICT (requirement_key) DO UPDATE
                SET process_area=EXCLUDED.process_area,
                    display_label=EXCLUDED.display_label,
                    sort_order=EXCLUDED.sort_order,
                    is_active=true,
                    updated_at_utc=now()
                """
            ),
            {"booking_key": booking_key, "delivery_key": delivery_key, "display_label": display_label},
        )

    # 3. Patch the Booking snapshot trigger (0070's body) to also add both new
    # requirements, unconditionally, alongside whatever the pinned profile
    # version and 0070's own bank_statement addition already provide.
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

    # 4. Patch the Delivery snapshot trigger (0070's body) the same way.
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


def downgrade() -> None:
    # Restore the trigger functions to their pre-0073 bodies (0070's own).
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

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        DELETE FROM auditcore.document_capture_v2_requirement_policy
        WHERE requirement_key IN (
            'booking_credit_note', 'delivery_credit_note',
            'booking_gst_declaration', 'delivery_gst_declaration'
        )
        """
    )
    # Backfilled journey_document_requirements rows are intentionally left in
    # place (0070/0049/0020 precedent: mutable per-journey audit state is not
    # destructively removed on downgrade).
