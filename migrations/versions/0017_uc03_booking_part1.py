from alembic import op

revision = "0017_uc03_booking_part1"
down_revision = "0016_uc03_doc_profile"
branch_labels = None
depends_on = None

_PROFILE_CODE = "VERIGENCE_AUTO_STANDARD"
_PROFILE_NAME = "Verigence Automotive Standard"
_MIGRATION_ACTOR = "migration.0017.uc03-booking-part1"


def upgrade() -> None:
    # Receipt number and transaction reference are different business facts.
    op.execute(
        """
        ALTER TABLE auditcore.payments
            ADD COLUMN IF NOT EXISTS receipt_number varchar(160),
            ADD COLUMN IF NOT EXISTS receipt_date date
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_payments_source_evidence
        ON auditcore.payments (tenant_id, journey_id, source_evidence_id)
        WHERE source_evidence_id IS NOT NULL
        """
    )

    # A Part-1 Product Master decision must retain the effective master version and
    # the Booking Docket evidence that drove the resolution. product_sku_id remains
    # nullable because Model+Variant may identify one canonical pair while several
    # sellable SKUs differ below it by colour/configuration.
    op.execute(
        """
        ALTER TABLE auditcore.journey_products
            ADD COLUMN IF NOT EXISTS product_master_version_id uuid,
            ADD COLUMN IF NOT EXISTS source_evidence_id uuid
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.journey_products
        ADD CONSTRAINT fk_journey_products_master_version
        FOREIGN KEY (product_master_version_id)
        REFERENCES auditcore.project_product_master_versions(version_id)
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.journey_products
        ADD CONSTRAINT fk_journey_products_source_evidence
        FOREIGN KEY (tenant_id, source_evidence_id)
        REFERENCES auditcore.evidence(tenant_id, evidence_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_journey_products_master_version
        ON auditcore.journey_products (tenant_id, product_master_version_id)
        WHERE product_master_version_id IS NOT NULL
        """
    )

    # Version 2 is the owner-confirmed Part-1 baseline:
    # Booking Docket + PAN-or-Aadhaar + repeatable Booking Payment Receipt.
    # Address Proof is deliberately absent.
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
                    tenant_id, profile_code, profile_name, created_by_actor_id
                ) VALUES (
                    p_tenant_id, '{_PROFILE_CODE}', '{_PROFILE_NAME}', p_actor_id
                )
                RETURNING document_requirement_profile_id INTO v_profile_id;
            END IF;

            SELECT document_requirement_profile_version_id
            INTO v_version_id
            FROM auditcore.document_requirement_profile_versions
            WHERE tenant_id = p_tenant_id
              AND document_requirement_profile_id = v_profile_id
              AND version_no = 2
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
                    2,
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
                        '{{"requirementGroup":"BOOKING_IDENTITY","groupRule":"AT_LEAST_ONE","minimumRequired":1,"groupLabel":"PAN or Aadhaar","preferred":"BOTH"}}'::jsonb,
                        20
                    ),
                    (
                        p_tenant_id,
                        v_version_id,
                        'CUSTOMER_PAN',
                        'pan_card',
                        'BOOKING',
                        'OPTIONAL',
                        '{{"requirementGroup":"BOOKING_IDENTITY","groupRule":"AT_LEAST_ONE","minimumRequired":1,"groupLabel":"PAN or Aadhaar","preferred":"BOTH"}}'::jsonb,
                        30
                    ),
                    (
                        p_tenant_id,
                        v_version_id,
                        'BOOKING_PAYMENT_RECEIPT',
                        'dealer_receipt',
                        'BOOKING',
                        'REQUIRED',
                        '{{"evidencePurpose":"BOOKING_PAYMENT","allowMultiple":true,"minimumEvidenceCount":1}}'::jsonb,
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

    # Ensure every existing Project owns the new standard version.
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

    # Standard-profile Journeys move to v2. The Journey requirement rows keep their
    # stable IDs so existing evidence links remain valid; the payment requirement is
    # renamed and future uploads use the real dealer_receipt DI type.
    op.execute(
        """
        WITH v2 AS (
            SELECT p.tenant_id,
                   v.document_requirement_profile_version_id AS version_id
            FROM auditcore.document_requirement_profiles p
            JOIN auditcore.document_requirement_profile_versions v
              ON v.tenant_id=p.tenant_id
             AND v.document_requirement_profile_id=p.document_requirement_profile_id
            WHERE p.profile_code='VERIGENCE_AUTO_STANDARD'
              AND v.version_no=2
              AND v.lifecycle_status='PUBLISHED'
        ), standard_journeys AS (
            SELECT j.tenant_id, j.journey_id, v2.version_id
            FROM auditcore.journeys j
            JOIN auditcore.document_requirement_profile_versions current_v
              ON current_v.tenant_id=j.tenant_id
             AND current_v.document_requirement_profile_version_id=
                    j.document_requirement_profile_version_id
            JOIN auditcore.document_requirement_profiles current_p
              ON current_p.tenant_id=current_v.tenant_id
             AND current_p.document_requirement_profile_id=
                    current_v.document_requirement_profile_id
            JOIN v2 ON v2.tenant_id=j.tenant_id
            WHERE current_p.profile_code='VERIGENCE_AUTO_STANDARD'
        )
        UPDATE auditcore.journeys j
        SET document_requirement_profile_version_id=sj.version_id,
            updated_at_utc=now()
        FROM standard_journeys sj
        WHERE j.tenant_id=sj.tenant_id AND j.journey_id=sj.journey_id
        """
    )

    op.execute(
        """
        WITH target AS (
            SELECT j.tenant_id, j.journey_id,
                   i.document_requirement_item_id
            FROM auditcore.journeys j
            JOIN auditcore.document_requirement_items i
              ON i.tenant_id=j.tenant_id
             AND i.document_requirement_profile_version_id=
                    j.document_requirement_profile_version_id
             AND i.requirement_key='BOOKING_PAYMENT_RECEIPT'
        )
        UPDATE auditcore.journey_document_requirements r
        SET document_requirement_item_id=t.document_requirement_item_id,
            requirement_key='BOOKING_PAYMENT_RECEIPT',
            document_type_key='dealer_receipt',
            requirement_level='REQUIRED',
            condition_snapshot='{"evidencePurpose":"BOOKING_PAYMENT","allowMultiple":true,"minimumEvidenceCount":1}'::jsonb,
            updated_at_utc=now()
        FROM target t
        WHERE r.tenant_id=t.tenant_id
          AND r.journey_id=t.journey_id
          AND r.requirement_key='MINIMUM_BOOKING_AMOUNT_RECEIPT'
        """
    )

    op.execute(
        """
        UPDATE auditcore.journey_document_assessments a
        SET requirement_key='BOOKING_PAYMENT_RECEIPT',
            document_requirement_profile_version_id=j.document_requirement_profile_version_id,
            updated_at_utc=now()
        FROM auditcore.journeys j
        WHERE a.tenant_id=j.tenant_id
          AND a.journey_id=j.journey_id
          AND a.stage_code='BOOKING'
          AND a.requirement_key='MINIMUM_BOOKING_AMOUNT_RECEIPT'
        """
    )

    # Re-point the three unchanged requirement identities to the v2 item records and
    # refresh the KYC group metadata. This does not remove or replace linked evidence.
    op.execute(
        """
        WITH target AS (
            SELECT j.tenant_id, j.journey_id, i.requirement_key,
                   i.document_requirement_item_id, i.condition_config
            FROM auditcore.journeys j
            JOIN auditcore.document_requirement_items i
              ON i.tenant_id=j.tenant_id
             AND i.document_requirement_profile_version_id=
                    j.document_requirement_profile_version_id
             AND i.requirement_key IN ('BOOKING_DOCKET','CUSTOMER_PAN','CUSTOMER_AADHAAR')
        )
        UPDATE auditcore.journey_document_requirements r
        SET document_requirement_item_id=t.document_requirement_item_id,
            condition_snapshot=t.condition_config,
            updated_at_utc=now()
        FROM target t
        WHERE r.tenant_id=t.tenant_id
          AND r.journey_id=t.journey_id
          AND r.requirement_key=t.requirement_key
        """
    )


def downgrade() -> None:
    # Published document-profile history is intentionally retained. Downgrade only
    # removes the additive Part-1 persistence columns/FKs; it does not rewrite
    # historical Journey evidence or delete the published v2 profile.
    op.execute(
        "ALTER TABLE auditcore.journey_products "
        "DROP CONSTRAINT IF EXISTS fk_journey_products_source_evidence"
    )
    op.execute(
        "ALTER TABLE auditcore.journey_products "
        "DROP CONSTRAINT IF EXISTS fk_journey_products_master_version"
    )
    op.execute("DROP INDEX IF EXISTS auditcore.ix_journey_products_master_version")
    op.execute(
        """
        ALTER TABLE auditcore.journey_products
            DROP COLUMN IF EXISTS source_evidence_id,
            DROP COLUMN IF EXISTS product_master_version_id
        """
    )
    op.execute("DROP INDEX IF EXISTS auditcore.ix_payments_source_evidence")
    op.execute(
        """
        ALTER TABLE auditcore.payments
            DROP COLUMN IF EXISTS receipt_date,
            DROP COLUMN IF EXISTS receipt_number
        """
    )
