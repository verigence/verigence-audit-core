"""Backfill canonical UC03 business tables from already-reviewed DI evidence.

Revision ID: 0056
Revises: 0055
Create Date: 2026-09-06

Earlier V2 Review flows could reach VERIFIED after storing reviewed values only in
review/provenance tables.  Journey 360 reads the canonical business tables instead.
This migration repairs already-reviewed journeys without calling DI and without
replacing a non-null canonical value.
"""

from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Booking Form/Docket commercial values are already typed numeric columns.  Pick
    # the latest reviewed source per Journey/component and fill only missing Core
    # commercial rows/amounts.  New Review confirms continue to use the runtime writer.
    op.execute(
        r"""
        WITH candidates AS (
            SELECT
                v.tenant_id,
                v.journey_id,
                v.source_di_document_id,
                v.source_evidence_id,
                v.reviewed_at_utc,
                COALESCE(d.classified_document_type_key, 'booking_form') AS document_type_key,
                x.component_key,
                x.amount
            FROM auditcore.booking_form_review_values v
            LEFT JOIN auditcore.document_capture_v2_documents d
              ON d.tenant_id=v.tenant_id
             AND d.journey_id=v.journey_id
             AND d.di_document_id=v.source_di_document_id
            CROSS JOIN LATERAL (
                VALUES
                    ('exchange_value', v.exchange_value),
                    ('ex_showroom_price', v.ex_showroom_price),
                    ('insurance_amount', v.insurance_amount),
                    ('registration_charges', v.registration_charges),
                    ('road_tax_amount', v.road_tax_amount),
                    ('road_tax_registration', v.road_tax_registration),
                    ('tcs_amount', v.tcs_amount),
                    ('rsa_amount', v.rsa_amount),
                    ('additional_warranty_amount', v.additional_warranty_amount),
                    ('extended_warranty_amount', v.extended_warranty_amount),
                    ('accessories_cost', v.accessories_cost),
                    ('essential_kit_amount', v.essential_kit_amount),
                    ('genuine_accessories_amount', v.genuine_accessories_amount),
                    ('non_genuine_accessories_amount', v.non_genuine_accessories_amount),
                    ('fastag_amount', v.fastag_amount),
                    ('green_tax_amount', v.green_tax_amount),
                    ('service_package_amount', v.service_package_amount),
                    ('other_charges', v.other_charges),
                    ('discount_amount', v.discount_amount),
                    ('sales_discount_amount', v.sales_discount_amount),
                    ('buffer_discount_amount', v.buffer_discount_amount),
                    ('exchange_discount_amount', v.exchange_discount_amount),
                    ('corporate_discount_amount', v.corporate_discount_amount),
                    ('loyalty_discount_amount', v.loyalty_discount_amount),
                    ('inhouse_insurance_discount_amount', v.inhouse_insurance_discount_amount),
                    ('mr_discount_amount', v.mr_discount_amount),
                    ('oem_referral_discount_amount', v.oem_referral_discount_amount),
                    ('other_discount_amount', v.other_discount_amount),
                    ('free_accessory_discount_amount', v.free_accessory_discount_amount),
                    ('bonus_amount', v.bonus_amount),
                    ('dsa_commission_amount', v.dsa_commission_amount),
                    ('total_price', v.total_price),
                    ('net_amount', v.net_amount),
                    ('booking_amount_paid', v.booking_amount_paid),
                    ('balance_amount', v.balance_amount)
            ) AS x(component_key, amount)
            WHERE x.amount IS NOT NULL
        ),
        selected AS (
            SELECT DISTINCT ON (tenant_id, journey_id, component_key)
                tenant_id,
                journey_id,
                component_key,
                amount,
                source_evidence_id,
                source_di_document_id,
                document_type_key
            FROM candidates
            ORDER BY tenant_id, journey_id, component_key,
                     reviewed_at_utc DESC, source_di_document_id DESC
        )
        INSERT INTO auditcore.commercial_lines (
            tenant_id, journey_id, component_key, actual_amount,
            actual_source_kind, source_evidence_id, source_reference
        )
        SELECT
            tenant_id,
            journey_id,
            component_key,
            amount,
            'EVIDENCE',
            source_evidence_id,
            lower(document_type_key) || ':' || source_di_document_id::text
        FROM selected
        ON CONFLICT (tenant_id, journey_id, component_key) DO UPDATE SET
            actual_amount=COALESCE(
                auditcore.commercial_lines.actual_amount,
                EXCLUDED.actual_amount
            ),
            actual_source_kind=COALESCE(
                auditcore.commercial_lines.actual_source_kind,
                EXCLUDED.actual_source_kind
            ),
            source_evidence_id=COALESCE(
                auditcore.commercial_lines.source_evidence_id,
                EXCLUDED.source_evidence_id
            ),
            source_reference=COALESCE(
                auditcore.commercial_lines.source_reference,
                EXCLUDED.source_reference
            ),
            updated_at_utc=CASE
                WHEN auditcore.commercial_lines.actual_amount IS NULL THEN now()
                ELSE auditcore.commercial_lines.updated_at_utc
            END
        """
    )

    # Reviewed Delivery VIN/chassis/invoice identifiers were previously kept only in
    # journey_document_extracted_fields.  Fill missing vehicle columns from exact DI
    # field keys; never replace an existing canonical identifier.
    op.execute(
        r"""
        WITH ranked AS (
            SELECT
                tenant_id,
                journey_id,
                field_key,
                NULLIF(btrim(effective_value #>> '{}'), '') AS value,
                evidence_id,
                reviewed_at_utc,
                row_number() OVER (
                    PARTITION BY tenant_id, journey_id, lower(field_key)
                    ORDER BY reviewed_at_utc DESC, source_fact_version DESC,
                             di_document_id DESC
                ) AS rn
            FROM auditcore.journey_document_extracted_fields
            WHERE stage_code='DELIVERY'
              AND effective_value IS NOT NULL
              AND lower(field_key) IN (
                    'vin', 'vin_number', 'chassis_number', 'chassis_no',
                    'invoice_reference', 'invoice_number', 'dms_invoice_number',
                    'dms_reference'
              )
        ),
        values_by_journey AS (
            SELECT
                tenant_id,
                journey_id,
                max(value) FILTER (WHERE lower(field_key) IN ('vin','vin_number') AND rn=1) AS vin,
                max(value) FILTER (WHERE lower(field_key) IN ('chassis_number','chassis_no') AND rn=1) AS chassis_number,
                max(value) FILTER (WHERE lower(field_key)='dms_reference' AND rn=1) AS dms_reference,
                max(value) FILTER (WHERE lower(field_key) IN ('invoice_reference','invoice_number','dms_invoice_number') AND rn=1) AS invoice_reference,
                max(evidence_id) FILTER (WHERE rn=1) AS source_evidence_id
            FROM ranked
            GROUP BY tenant_id, journey_id
        )
        INSERT INTO auditcore.vehicle_records (
            tenant_id, journey_id, vin, chassis_number, dms_reference,
            invoice_reference, source_kind, source_evidence_id
        )
        SELECT
            tenant_id, journey_id, vin, chassis_number, dms_reference,
            invoice_reference, 'EVIDENCE', source_evidence_id
        FROM values_by_journey
        WHERE vin IS NOT NULL OR chassis_number IS NOT NULL
           OR dms_reference IS NOT NULL OR invoice_reference IS NOT NULL
        ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
            vin=COALESCE(auditcore.vehicle_records.vin, EXCLUDED.vin),
            chassis_number=COALESCE(
                auditcore.vehicle_records.chassis_number,
                EXCLUDED.chassis_number
            ),
            dms_reference=COALESCE(
                auditcore.vehicle_records.dms_reference,
                EXCLUDED.dms_reference
            ),
            invoice_reference=COALESCE(
                auditcore.vehicle_records.invoice_reference,
                EXCLUDED.invoice_reference
            ),
            source_kind=COALESCE(auditcore.vehicle_records.source_kind, 'EVIDENCE'),
            source_evidence_id=COALESCE(
                auditcore.vehicle_records.source_evidence_id,
                EXCLUDED.source_evidence_id
            ),
            updated_at_utc=now(),
            version_no=auditcore.vehicle_records.version_no+1
        """
    )

    # Registration can be present directly or as insured_vehicle_reg on the reviewed
    # insurance policy.  Direct registration_number wins when both exist.
    op.execute(
        r"""
        WITH ranked AS (
            SELECT
                tenant_id,
                journey_id,
                lower(field_key) AS field_key,
                NULLIF(btrim(effective_value #>> '{}'), '') AS value,
                evidence_id,
                row_number() OVER (
                    PARTITION BY tenant_id, journey_id
                    ORDER BY
                        CASE lower(field_key)
                            WHEN 'registration_number' THEN 0
                            ELSE 1
                        END,
                        reviewed_at_utc DESC,
                        source_fact_version DESC,
                        di_document_id DESC
                ) AS rn
            FROM auditcore.journey_document_extracted_fields
            WHERE stage_code='DELIVERY'
              AND effective_value IS NOT NULL
              AND lower(field_key) IN ('registration_number', 'insured_vehicle_reg')
        )
        INSERT INTO auditcore.registration_records (
            tenant_id, journey_id, registration_number,
            source_kind, source_evidence_id
        )
        SELECT tenant_id, journey_id, value, 'EVIDENCE', evidence_id
        FROM ranked
        WHERE rn=1 AND value IS NOT NULL
        ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
            registration_number=COALESCE(
                auditcore.registration_records.registration_number,
                EXCLUDED.registration_number
            ),
            source_kind=COALESCE(
                auditcore.registration_records.source_kind,
                'EVIDENCE'
            ),
            source_evidence_id=COALESCE(
                auditcore.registration_records.source_evidence_id,
                EXCLUDED.source_evidence_id
            ),
            updated_at_utc=now(),
            version_no=auditcore.registration_records.version_no+1
        """
    )

    # The global DI insurance_cover contract uses insurer_name, policy_number and
    # premium_amount.  Only values from an insurance document are eligible here.
    op.execute(
        r"""
        WITH fields AS (
            SELECT
                f.tenant_id,
                f.journey_id,
                lower(f.field_key) AS field_key,
                NULLIF(btrim(f.effective_value #>> '{}'), '') AS value,
                f.evidence_id,
                f.reviewed_at_utc,
                row_number() OVER (
                    PARTITION BY f.tenant_id, f.journey_id, lower(f.field_key)
                    ORDER BY f.reviewed_at_utc DESC, f.source_fact_version DESC,
                             f.di_document_id DESC
                ) AS rn
            FROM auditcore.journey_document_extracted_fields f
            WHERE f.stage_code='DELIVERY'
              AND f.effective_value IS NOT NULL
              AND lower(COALESCE(f.source_document_type_key,'')) IN (
                    'insurance_cover', 'insurance_cover_note', 'insurance_policy'
              )
              AND lower(f.field_key) IN (
                    'insurer_name', 'policy_number', 'premium_amount'
              )
        ),
        aggregated AS (
            SELECT
                tenant_id,
                journey_id,
                max(value) FILTER (WHERE field_key='insurer_name' AND rn=1) AS insurer_name,
                max(value) FILTER (WHERE field_key='policy_number' AND rn=1) AS policy_reference,
                max(value) FILTER (WHERE field_key='premium_amount' AND rn=1) AS premium_text,
                max(evidence_id) FILTER (WHERE rn=1) AS source_evidence_id
            FROM fields
            GROUP BY tenant_id, journey_id
        ),
        normalized AS (
            SELECT
                tenant_id,
                journey_id,
                insurer_name,
                policy_reference,
                CASE
                    WHEN regexp_replace(COALESCE(premium_text,''), '[^0-9.-]', '', 'g')
                         ~ '^-?[0-9]+(?:\.[0-9]+)?$'
                    THEN regexp_replace(premium_text, '[^0-9.-]', '', 'g')::numeric
                    ELSE NULL
                END AS actual_premium_amount,
                source_evidence_id
            FROM aggregated
        )
        INSERT INTO auditcore.insurance_records (
            tenant_id, journey_id, insurer_name, policy_reference,
            actual_premium_amount, source_kind, source_evidence_id
        )
        SELECT
            tenant_id, journey_id, insurer_name, policy_reference,
            actual_premium_amount, 'EVIDENCE', source_evidence_id
        FROM normalized
        WHERE insurer_name IS NOT NULL OR policy_reference IS NOT NULL
           OR actual_premium_amount IS NOT NULL
        ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
            insurer_name=COALESCE(
                auditcore.insurance_records.insurer_name,
                EXCLUDED.insurer_name
            ),
            policy_reference=COALESCE(
                auditcore.insurance_records.policy_reference,
                EXCLUDED.policy_reference
            ),
            actual_premium_amount=COALESCE(
                auditcore.insurance_records.actual_premium_amount,
                EXCLUDED.actual_premium_amount
            ),
            source_kind=COALESCE(auditcore.insurance_records.source_kind, 'EVIDENCE'),
            source_evidence_id=COALESCE(
                auditcore.insurance_records.source_evidence_id,
                EXCLUDED.source_evidence_id
            ),
            updated_at_utc=now(),
            version_no=auditcore.insurance_records.version_no+1
        """
    )

    # Historical Delivery dealer receipts were losslessly retained but were not
    # projected into payments.  Reconstruct one additive Payment per reviewed DI
    # receipt document.  Existing document-backed payments are left untouched.
    op.execute(
        r"""
        WITH receipt_fields AS (
            SELECT
                tenant_id,
                journey_id,
                di_document_id,
                max(evidence_id) AS evidence_id,
                max(effective_value #>> '{}') FILTER (WHERE lower(field_key)='receipt_number') AS receipt_number,
                max(effective_value #>> '{}') FILTER (WHERE lower(field_key)='receipt_date') AS receipt_date_text,
                max(effective_value #>> '{}') FILTER (WHERE lower(field_key)='amount_paid') AS amount_text,
                max(effective_value #>> '{}') FILTER (WHERE lower(field_key)='payment_mode') AS payment_method_code,
                max(effective_value #>> '{}') FILTER (WHERE lower(field_key)='payment_reference_no') AS payment_reference,
                max(effective_value #>> '{}') FILTER (WHERE lower(field_key)='dealer_name') AS receipt_dealer_name,
                max(effective_value #>> '{}') FILTER (WHERE lower(field_key)='dealer_gstin') AS receipt_dealer_gstin,
                max(effective_value #>> '{}') FILTER (WHERE lower(field_key)='customer_name') AS receipt_customer_name,
                max(effective_value #>> '{}') FILTER (WHERE lower(field_key)='customer_phone') AS receipt_customer_phone,
                max(effective_value #>> '{}') FILTER (WHERE lower(field_key)='payment_reference_date') AS payment_reference_date_text,
                max(effective_value #>> '{}') FILTER (WHERE lower(field_key)='bank_name') AS receipt_bank_name,
                max(effective_value #>> '{}') FILTER (WHERE lower(field_key)='bank_location') AS receipt_bank_location,
                max(effective_value #>> '{}') FILTER (WHERE lower(field_key)='booking_reference_number') AS receipt_booking_reference,
                max(effective_value #>> '{}') FILTER (WHERE lower(field_key)='remarks') AS receipt_remarks,
                max(effective_value #>> '{}') FILTER (WHERE lower(field_key)='amount_in_words') AS receipt_amount_in_words
            FROM auditcore.journey_document_extracted_fields
            WHERE stage_code='DELIVERY'
              AND lower(COALESCE(source_document_type_key,''))='dealer_receipt'
              AND effective_value IS NOT NULL
            GROUP BY tenant_id, journey_id, di_document_id
        ),
        normalized AS (
            SELECT
                *,
                CASE
                    WHEN regexp_replace(COALESCE(amount_text,''), '[^0-9.-]', '', 'g')
                         ~ '^-?[0-9]+(?:\.[0-9]+)?$'
                    THEN regexp_replace(amount_text, '[^0-9.-]', '', 'g')::numeric
                    ELSE NULL
                END AS amount,
                CASE
                    WHEN receipt_date_text ~ '^\d{4}-\d{2}-\d{2}$'
                    THEN receipt_date_text::date
                    ELSE NULL
                END AS receipt_date,
                CASE
                    WHEN payment_reference_date_text ~ '^\d{4}-\d{2}-\d{2}$'
                    THEN payment_reference_date_text::date
                    ELSE NULL
                END AS payment_reference_date
            FROM receipt_fields
        )
        INSERT INTO auditcore.payments (
            tenant_id, journey_id, amount, payment_method_code,
            payment_reference, receipt_number, receipt_date,
            receipt_dealer_name, receipt_dealer_gstin,
            receipt_customer_name, receipt_customer_phone,
            payment_reference_date, receipt_bank_name, receipt_bank_location,
            receipt_booking_reference, receipt_remarks, receipt_amount_in_words,
            status_source, source_evidence_id, source_di_document_id, payment_stage
        )
        SELECT
            n.tenant_id, n.journey_id, n.amount, n.payment_method_code,
            n.payment_reference, n.receipt_number, n.receipt_date,
            n.receipt_dealer_name, n.receipt_dealer_gstin,
            n.receipt_customer_name, n.receipt_customer_phone,
            n.payment_reference_date, n.receipt_bank_name, n.receipt_bank_location,
            n.receipt_booking_reference, n.receipt_remarks, n.receipt_amount_in_words,
            'EVIDENCE', n.evidence_id, n.di_document_id, 'DELIVERY'
        FROM normalized n
        WHERE n.amount IS NOT NULL
          AND NOT EXISTS (
                SELECT 1
                FROM auditcore.payments p
                WHERE p.tenant_id=n.tenant_id
                  AND p.journey_id=n.journey_id
                  AND p.source_di_document_id=n.di_document_id
          )
        """
    )


def downgrade() -> None:
    # This migration repairs business data from reviewed evidence.  Reversing it would
    # delete legitimate canonical facts and cannot distinguish rows created later by
    # normal operations, so downgrade is intentionally non-destructive.
    pass
