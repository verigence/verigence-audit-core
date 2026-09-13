"""0091_retire_booking_bank_stmt — bank statements are Delivery-only.

Unified Booking/Delivery document capture (2026-09-13): a single upload
screen with no upfront stage picker needs every document_type_key to
resolve to exactly one stage after classification, or dispatch becomes
ambiguous. ``bank_statement_extract`` was the one type registered as a
valid candidate under BOTH Booking's and Delivery's own requirement sets
(migration 0070 -- ``booking_bank_statement`` / ``delivery_bank_statement``,
same document_type_key, different requirement_key per stage). Per explicit
product decision, a bank statement is always a Delivery-time document
going forward -- simpler than teaching dispatch logic to fan a single
upload out to two stages for one edge case.

This retires ``booking_bank_statement`` two ways, matching 0070's own
"mutable per-journey snapshot, immutable published master" precedent:
  1. Deactivates its ``document_capture_v2_requirement_policy`` row (stops
     it from being offered/displayed for any journey from now on).
  2. Removes its unconditional INSERT from
     ``initialize_uc03_booking_requirements()`` so no NEW journey ever gets
     a Booking-stage bank-statement requirement row again.

Deliberately NOT touched: existing ``journey_document_requirements`` rows
with requirement_key='booking_bank_statement' on journeys created before
this migration. They are OPTIONAL-level and never blocked anything; ripping
them out retroactively would just be destructive churn for no behavioral
gain (0070's own downgrade() leaves backfilled rows in place for the same
reason).
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0091_retire_booking_bank_stmt"
down_revision = "0090_field_correction_proposals"
branch_labels = None
depends_on = None

_BOOKING_KEY = "booking_bank_statement"


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        text(
            """
            UPDATE auditcore.document_capture_v2_requirement_policy
            SET is_active=false, updated_at_utc=now()
            WHERE requirement_key=:key
            """
        ),
        {"key": _BOOKING_KEY},
    )
    # Starting from 0078's full body (the current one -- 0073/0078 each added
    # their own unconditional INSERT block on top of 0070's), with ONLY the
    # booking_bank_statement block removed. Re-deriving from 0070's older,
    # narrower body here would have silently reverted 0073's/0078's additions
    # too (credit_note, gst_declaration, scrappage_certificate).
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

            -- 0070's unconditional booking_bank_statement INSERT removed here:
            -- bank statements are Delivery-only from this migration forward.

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


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        text(
            """
            UPDATE auditcore.document_capture_v2_requirement_policy
            SET is_active=true, updated_at_utc=now()
            WHERE requirement_key=:key
            """
        ),
        {"key": _BOOKING_KEY},
    )
    # Restore 0078's full body (the one this migration's upgrade() started
    # from), with the booking_bank_statement block put back.
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
