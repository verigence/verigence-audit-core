"""Register bank_statement_extract as an ordinary, non-checklist document type.

Revision ID: 0070_uc03_bank_statement_req
Revises: 0069_uc03_document_missing
Create Date: 2026-09-08

DI already has a full classification category and extraction schema for
``bank_statement_extract`` (verigence-di schemas/bank_statement.py, registered
in DI's own document-type/profile registry). What was missing is on the
Audit Core side: it was never added to the default document-requirement
profile, so it never appeared in ``candidate_document_type_keys`` sent to
DI's classifier at upload-intent time (``_candidate_type_keys`` /
``_delivery_requirements`` only offer document types that already have a
registered ``journey_document_requirements`` row) -- DI could technically
classify a bank statement, but the V2 capture flow never told it that was an
option for a given journey.

A bank statement is not a checklist item the way pan_card/aadhaar/
gst_certificate are -- nobody declares up front whether one will exist; it
either gets uploaded when a non-cash payment needs matching, or it doesn't.
So this registers it as ``journey_document_requirements`` rows with
``document_requirement_item_id = NULL`` -- present so DI can classify against
it and so evidence/materialization treat it like any other document, but
excluded from the "PC must declare every applicable checklist item" audit-
completion gate (see the accompanying ``uc03_audit_flags.py`` change: that
gate now only counts requirements sourced from the published profile).

Published requirement masters remain immutable (see 0049). This adds the new
requirement by:
  1. Backfilling ``journey_document_requirements`` directly for existing
     journeys (Booking: all of them; Delivery: those that have already
     started Delivery) -- the same "mutable per-journey snapshot" precedent
     0049 used.
  2. Patching both ``initialize_uc03_*_requirements()`` snapshot triggers so
     every future journey/Delivery-start gets it too, alongside (not
     replacing) whatever the published profile version already provides.

Different requirement_key per stage (booking_bank_statement /
delivery_bank_statement) because journey_document_requirements is unique on
(tenant_id, journey_id, requirement_key) -- one journey can be mid-Delivery
while its Booking-stage snapshot still exists, so the two must not collide.
Both keys are added to Booking's repeatable-requirement set (a customer's
statement may span more than one page/period, same as receipts).
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0070_uc03_bank_statement_req"
down_revision = "0069_uc03_document_missing"
branch_labels = None
depends_on = None

_DOCUMENT_TYPE = "bank_statement_extract"
_BOOKING_KEY = "booking_bank_statement"
_DELIVERY_KEY = "delivery_bank_statement"


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

    # 2a. Presentation metadata (falls back to the requirement_key otherwise).
    bind.execute(
        text(
            """
            INSERT INTO auditcore.document_capture_v2_requirement_policy (
                requirement_key, process_area, display_label, condition_key,
                extension_document_type_key, extension_requirement_level,
                sort_order, is_extension, is_active
            ) VALUES
                (:booking_key, 'BOOKING', 'Bank Statement', NULL, NULL, NULL, 45, false, true),
                (:delivery_key, 'DELIVERY', 'Bank Statement', NULL, NULL, NULL, 255, false, true)
            ON CONFLICT (requirement_key) DO UPDATE
            SET process_area=EXCLUDED.process_area,
                display_label=EXCLUDED.display_label,
                sort_order=EXCLUDED.sort_order,
                is_active=true,
                updated_at_utc=now()
            """
        ),
        {"booking_key": _BOOKING_KEY, "delivery_key": _DELIVERY_KEY},
    )

    # 2b. Patch the Booking snapshot trigger (0020's body) to also add the new
    # requirement, unconditionally, alongside whatever the pinned profile
    # version already provides.
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

    # 2c. Patch the Delivery snapshot trigger (0049's body, which itself
    # already carries corrections on top of 0020's original) the same way.
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


def downgrade() -> None:
    # Restore the trigger functions to their pre-0070 bodies (0020 / 0049).
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

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        DELETE FROM auditcore.document_capture_v2_requirement_policy
        WHERE requirement_key IN ('booking_bank_statement', 'delivery_bank_statement')
        """
    )
    # Backfilled journey_document_requirements rows are intentionally left in
    # place (0020/0049 precedent: mutable per-journey audit state is not
    # destructively removed on downgrade).
