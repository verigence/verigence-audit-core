"""Align UC03 Booking Part-1 evidence requirements.

Part 1 is intentionally narrow: Booking Docket, KYC satisfied by PAN or Aadhaar
(both preferred), and repeatable Booking payment receipts. Existing published
profiles are not mutated; a new profile version is published and Journey snapshots
are reconciled without deleting evidence.
"""
from alembic import op

revision = "0020_uc03_part1_evidence"
down_revision = "0018_uc03_correction_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Publish a corrected forward version of the default UC03 document profile for
    # each Project. The old published version is retired, not edited/deleted.
    op.execute(
        r"""
        DO $$
        DECLARE
            p record;
            v_profile_id uuid;
            v_old_version_id uuid;
            v_new_version_id uuid;
            v_version_no integer;
        BEGIN
            FOR p IN SELECT tenant_id, effective_start_date FROM auditcore.projects LOOP
                SELECT document_requirement_profile_id
                INTO v_profile_id
                FROM auditcore.document_requirement_profiles
                WHERE tenant_id=p.tenant_id
                  AND profile_code='UC03_DEFAULT_VEHICLE_SALES';

                IF v_profile_id IS NULL THEN
                    CONTINUE;
                END IF;

                SELECT v.document_requirement_profile_version_id
                INTO v_old_version_id
                FROM auditcore.document_requirement_profile_versions v
                WHERE v.tenant_id=p.tenant_id
                  AND v.document_requirement_profile_id=v_profile_id
                  AND v.lifecycle_status='PUBLISHED'
                ORDER BY v.effective_from DESC, v.version_no DESC
                LIMIT 1;

                IF v_old_version_id IS NULL THEN
                    CONTINUE;
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM auditcore.document_requirement_items i
                    WHERE i.tenant_id=p.tenant_id
                      AND i.document_requirement_profile_version_id=v_old_version_id
                      AND i.requirement_key='booking_payment_receipt'
                      AND i.document_type_key='dealer_receipt'
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM auditcore.document_requirement_items i
                    WHERE i.tenant_id=p.tenant_id
                      AND i.document_requirement_profile_version_id=v_old_version_id
                      AND i.requirement_key IN ('pan_card','aadhaar')
                      AND i.requirement_level='REQUIRED'
                ) THEN
                    CONTINUE;
                END IF;

                SELECT COALESCE(max(version_no), 0) + 1
                INTO v_version_no
                FROM auditcore.document_requirement_profile_versions
                WHERE tenant_id=p.tenant_id
                  AND document_requirement_profile_id=v_profile_id;

                INSERT INTO auditcore.document_requirement_profile_versions (
                    tenant_id, document_requirement_profile_id, version_no,
                    effective_from, lifecycle_status, created_by_actor_id
                ) VALUES (
                    p.tenant_id, v_profile_id, v_version_no,
                    p.effective_start_date, 'DRAFT', 'migration.uc03-part1-evidence'
                )
                RETURNING document_requirement_profile_version_id INTO v_new_version_id;

                INSERT INTO auditcore.document_requirement_items (
                    tenant_id, document_requirement_profile_version_id,
                    requirement_key, document_type_key, process_area,
                    requirement_level, condition_config, sort_order
                )
                SELECT
                    i.tenant_id,
                    v_new_version_id,
                    CASE
                        WHEN i.requirement_key='minimum_booking_payment_proof'
                            THEN 'booking_payment_receipt'
                        ELSE i.requirement_key
                    END,
                    CASE
                        WHEN i.requirement_key='minimum_booking_payment_proof'
                            THEN 'dealer_receipt'
                        ELSE i.document_type_key
                    END,
                    i.process_area,
                    CASE
                        WHEN upper(i.process_area)='BOOKING'
                         AND i.requirement_key IN ('pan_card','aadhaar')
                            THEN 'OPTIONAL'
                        ELSE i.requirement_level
                    END,
                    i.condition_config,
                    i.sort_order
                FROM auditcore.document_requirement_items i
                WHERE i.tenant_id=p.tenant_id
                  AND i.document_requirement_profile_version_id=v_old_version_id;

                UPDATE auditcore.document_requirement_profile_versions
                SET lifecycle_status='PUBLISHED',
                    published_by_actor_id='migration.uc03-part1-evidence',
                    published_at_utc=now(),
                    updated_at_utc=now()
                WHERE tenant_id=p.tenant_id
                  AND document_requirement_profile_version_id=v_new_version_id
                  AND lifecycle_status='DRAFT';

                UPDATE auditcore.document_requirement_profile_versions
                SET lifecycle_status='RETIRED',
                    retired_by_actor_id='migration.uc03-part1-evidence',
                    retired_at_utc=now(),
                    updated_at_utc=now()
                WHERE tenant_id=p.tenant_id
                  AND document_requirement_profile_version_id=v_old_version_id
                  AND lifecycle_status='PUBLISHED';
            END LOOP;
        END;
        $$
        """
    )

    # Existing in-flight Journeys keep all evidence. Normalize only their snapshot
    # semantics; do not delete or detach anything.
    op.execute(
        """
        UPDATE auditcore.journey_document_requirements
        SET requirement_level='OPTIONAL', updated_at_utc=now()
        WHERE upper(process_area)='BOOKING'
          AND requirement_key IN ('pan_card','aadhaar')
          AND requirement_level='REQUIRED'
        """
    )
    op.execute(
        """
        UPDATE auditcore.journey_document_requirements r
        SET requirement_key='booking_payment_receipt',
            document_type_key='dealer_receipt',
            requirement_level='REQUIRED',
            updated_at_utc=now()
        WHERE upper(r.process_area)='BOOKING'
          AND r.requirement_key='minimum_booking_payment_proof'
          AND NOT EXISTS (
              SELECT 1
              FROM auditcore.journey_document_requirements x
              WHERE x.tenant_id=r.tenant_id
                AND x.journey_id=r.journey_id
                AND x.requirement_key='booking_payment_receipt'
          )
        """
    )

    # The existing trigger calls a zero-argument trigger function. Normalize the
    # Journey snapshot at creation time even when a historical profile is selected.
    op.execute(
        r"""
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


def downgrade() -> None:
    # Published version/history is intentionally not destructively removed.
    op.execute(
        r"""
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
            SELECT j.tenant_id, j.journey_id, dri.document_requirement_item_id,
                   dri.requirement_key, dri.document_type_key, dri.process_area,
                   dri.requirement_level, 'PENDING', dri.condition_config
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
