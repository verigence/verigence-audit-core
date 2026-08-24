from alembic import op

revision = "0016_uc03_doc_profile"
down_revision = "0015_uc02_global_segments"
branch_labels = None
depends_on = None


_PROFILE_CODE = "VERIGENCE_AUTO_STANDARD"
_PROFILE_NAME = "Verigence Automotive Standard"
_MIGRATION_ACTOR = "migration.0016.uc03-document-profile"


def upgrade() -> None:
    # UC03 requires a real Project-level Document Requirement Profile before a
    # Journey can snapshot its Booking checklist. UC02 previously exposed the
    # master but did not seed one. The Phase-1 Booking baseline confirmed by the
    # product owner is intentionally small:
    #   - Booking Docket
    #   - Aadhaar and/or PAN (at least one identity document)
    #   - Minimum Booking Amount Receipt
    # Address Proof is deliberately NOT part of the Booking baseline.
    #
    # Aadhaar/PAN are stored as two uploadable requirements because DI has
    # distinct canonical Document Types. Their shared condition metadata records
    # the business rule that one of the two is sufficient; runtime completion
    # logic may consume this metadata without changing the published profile.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION auditcore.ensure_verigence_auto_document_profile(
            p_tenant_id varchar,
            p_effective_from date,
            p_actor_id varchar
        )
        RETURNS uuid
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_profile_id uuid;
            v_version_id uuid;
        BEGIN
            SELECT document_requirement_profile_id
            INTO v_profile_id
            FROM auditcore.document_requirement_profiles
            WHERE tenant_id = p_tenant_id
              AND profile_code = '{_PROFILE_CODE}'
            ORDER BY created_at_utc, document_requirement_profile_id
            LIMIT 1;

            IF v_profile_id IS NULL THEN
                INSERT INTO auditcore.document_requirement_profiles (
                    tenant_id,
                    profile_code,
                    profile_name,
                    created_by_actor_id
                ) VALUES (
                    p_tenant_id,
                    '{_PROFILE_CODE}',
                    '{_PROFILE_NAME}',
                    p_actor_id
                )
                RETURNING document_requirement_profile_id INTO v_profile_id;
            END IF;

            SELECT document_requirement_profile_version_id
            INTO v_version_id
            FROM auditcore.document_requirement_profile_versions
            WHERE tenant_id = p_tenant_id
              AND document_requirement_profile_id = v_profile_id
              AND version_no = 1
            LIMIT 1;

            IF v_version_id IS NULL THEN
                INSERT INTO auditcore.document_requirement_profile_versions (
                    tenant_id,
                    document_requirement_profile_id,
                    version_no,
                    lifecycle_status,
                    effective_from,
                    created_by_actor_id
                ) VALUES (
                    p_tenant_id,
                    v_profile_id,
                    1,
                    'DRAFT',
                    COALESCE(p_effective_from, CURRENT_DATE),
                    p_actor_id
                )
                RETURNING document_requirement_profile_version_id INTO v_version_id;

                INSERT INTO auditcore.document_requirement_items (
                    tenant_id,
                    document_requirement_profile_version_id,
                    requirement_key,
                    document_type_key,
                    process_area,
                    requirement_level,
                    condition_config,
                    sort_order
                ) VALUES
                    (
                        p_tenant_id,
                        v_version_id,
                        'BOOKING_DOCKET',
                        'booking_docket',
                        'BOOKING',
                        'REQUIRED',
                        '{{}}'::jsonb,
                        10
                    ),
                    (
                        p_tenant_id,
                        v_version_id,
                        'CUSTOMER_AADHAAR',
                        'aadhaar',
                        'BOOKING',
                        'OPTIONAL',
                        '{{"requirementGroup":"BOOKING_IDENTITY","groupRule":"AT_LEAST_ONE","minimumRequired":1,"groupLabel":"Aadhaar or PAN"}}'::jsonb,
                        20
                    ),
                    (
                        p_tenant_id,
                        v_version_id,
                        'CUSTOMER_PAN',
                        'pan_card',
                        'BOOKING',
                        'OPTIONAL',
                        '{{"requirementGroup":"BOOKING_IDENTITY","groupRule":"AT_LEAST_ONE","minimumRequired":1,"groupLabel":"Aadhaar or PAN"}}'::jsonb,
                        30
                    ),
                    (
                        p_tenant_id,
                        v_version_id,
                        'MINIMUM_BOOKING_AMOUNT_RECEIPT',
                        'supporting_document',
                        'BOOKING',
                        'REQUIRED',
                        '{{"evidencePurpose":"MINIMUM_BOOKING_AMOUNT"}}'::jsonb,
                        40
                    );

                UPDATE auditcore.document_requirement_profile_versions
                SET lifecycle_status = 'PUBLISHED',
                    published_by_actor_id = p_actor_id,
                    published_at_utc = now(),
                    updated_at_utc = now()
                WHERE tenant_id = p_tenant_id
                  AND document_requirement_profile_version_id = v_version_id
                  AND lifecycle_status = 'DRAFT';
            END IF;

            RETURN v_version_id;
        END;
        $$;
        """
    )

    # Seed the standard profile for every Project that already exists.
    op.execute(
        f"""
        DO $$
        DECLARE
            project_row record;
        BEGIN
            FOR project_row IN
                SELECT tenant_id, effective_start_date, created_by_actor_id
                FROM auditcore.projects
            LOOP
                PERFORM auditcore.ensure_verigence_auto_document_profile(
                    project_row.tenant_id,
                    project_row.effective_start_date,
                    COALESCE(project_row.created_by_actor_id, '{_MIGRATION_ACTOR}')
                );
            END LOOP;
        END;
        $$;
        """
    )

    # Future Projects receive the same baseline automatically. This keeps the
    # Project-onboarding UI free of technical master setup for the standard case.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION auditcore.seed_verigence_auto_document_profile()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            PERFORM auditcore.ensure_verigence_auto_document_profile(
                NEW.tenant_id,
                NEW.effective_start_date,
                COALESCE(NEW.created_by_actor_id, '{_MIGRATION_ACTOR}')
            );
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_project_seed_verigence_auto_document_profile
        AFTER INSERT ON auditcore.projects
        FOR EACH ROW
        EXECUTE FUNCTION auditcore.seed_verigence_auto_document_profile()
        """
    )

    # When callers do not explicitly choose a custom Document Requirement Profile,
    # bind the standard published profile at Journey creation. This closes the
    # UC02 -> UC03 gap that previously produced an empty Booking checklist.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION auditcore.bind_default_document_profile_to_journey()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.document_requirement_profile_version_id IS NULL THEN
                SELECT v.document_requirement_profile_version_id
                INTO NEW.document_requirement_profile_version_id
                FROM auditcore.document_requirement_profiles p
                JOIN auditcore.document_requirement_profile_versions v
                  ON v.tenant_id = p.tenant_id
                 AND v.document_requirement_profile_id = p.document_requirement_profile_id
                WHERE p.tenant_id = NEW.tenant_id
                  AND p.profile_code = '{_PROFILE_CODE}'
                  AND v.lifecycle_status = 'PUBLISHED'
                  AND v.effective_from <= CURRENT_DATE
                  AND (v.effective_to IS NULL OR v.effective_to >= CURRENT_DATE)
                ORDER BY v.version_no DESC, v.effective_from DESC
                LIMIT 1;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_journey_bind_default_document_profile
        BEFORE INSERT ON auditcore.journeys
        FOR EACH ROW
        EXECUTE FUNCTION auditcore.bind_default_document_profile_to_journey()
        """
    )

    # Repair existing Journeys created while UC02 had no default profile.
    op.execute(
        f"""
        UPDATE auditcore.journeys j
        SET document_requirement_profile_version_id = (
                SELECT v.document_requirement_profile_version_id
                FROM auditcore.document_requirement_profiles p
                JOIN auditcore.document_requirement_profile_versions v
                  ON v.tenant_id = p.tenant_id
                 AND v.document_requirement_profile_id = p.document_requirement_profile_id
                WHERE p.tenant_id = j.tenant_id
                  AND p.profile_code = '{_PROFILE_CODE}'
                  AND v.lifecycle_status = 'PUBLISHED'
                ORDER BY v.version_no DESC, v.effective_from DESC
                LIMIT 1
            ),
            updated_at_utc = now()
        WHERE j.document_requirement_profile_version_id IS NULL
          AND EXISTS (
                SELECT 1
                FROM auditcore.document_requirement_profiles p
                JOIN auditcore.document_requirement_profile_versions v
                  ON v.tenant_id = p.tenant_id
                 AND v.document_requirement_profile_id = p.document_requirement_profile_id
                WHERE p.tenant_id = j.tenant_id
                  AND p.profile_code = '{_PROFILE_CODE}'
                  AND v.lifecycle_status = 'PUBLISHED'
            )
        """
    )

    # A Booking that was already started before this repair has already missed the
    # AFTER INSERT snapshot trigger from migration 0011. Backfill only missing
    # Booking requirements; existing snapshots/evidence are never overwritten.
    op.execute(
        """
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
        JOIN auditcore.journey_stage_states s
          ON s.tenant_id = j.tenant_id
         AND s.journey_id = j.journey_id
         AND s.stage_code = 'BOOKING'
        JOIN auditcore.document_requirement_items dri
          ON dri.tenant_id = j.tenant_id
         AND dri.document_requirement_profile_version_id =
                j.document_requirement_profile_version_id
         AND upper(dri.process_area) = 'BOOKING'
        ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING
        """
    )


def downgrade() -> None:
    # Do not delete published profile/evidence history. Remove only automatic
    # creation/binding behavior; published configuration remains immutable audit
    # history and may be retired explicitly through the normal master lifecycle.
    op.execute(
        "DROP TRIGGER IF EXISTS trg_journey_bind_default_document_profile "
        "ON auditcore.journeys"
    )
    op.execute("DROP FUNCTION IF EXISTS auditcore.bind_default_document_profile_to_journey()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_project_seed_verigence_auto_document_profile "
        "ON auditcore.projects"
    )
    op.execute("DROP FUNCTION IF EXISTS auditcore.seed_verigence_auto_document_profile()")
    op.execute(
        "DROP FUNCTION IF EXISTS auditcore.ensure_verigence_auto_document_profile(varchar,date,varchar)"
    )
