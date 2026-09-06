"""Backfill reviewed Booking values into their canonical Audit Core owners.

Revision ID: 0057
Revises: 0056
Create Date: 2026-09-06

The runtime Review path now writes typed Core owners, but journeys reviewed before
that wiring can still have complete reviewed Booking rows while canonical Journey
360 tables contain gaps.  This migration is deliberately conservative: it fills
only missing canonical values and never invents business semantics.
"""

from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Choose the latest reviewed Booking document as the historical source for
    # single-valued Booking/product/owner projections.
    op.execute(
        r"""
        WITH latest AS (
            SELECT DISTINCT ON (tenant_id, journey_id)
                tenant_id,
                journey_id,
                source_di_document_id,
                source_evidence_id,
                booking_reference_number,
                booking_date,
                deal_type,
                expected_delivery,
                expected_delivery_date,
                vehicle_model,
                vehicle_variant,
                vehicle_color,
                registration_by,
                insurance_by
            FROM auditcore.booking_form_review_values
            ORDER BY tenant_id, journey_id, reviewed_at_utc DESC,
                     booking_form_review_value_id DESC
        )
        UPDATE auditcore.bookings b
        SET booking_reference=COALESCE(b.booking_reference, l.booking_reference_number),
            booking_date=COALESCE(b.booking_date, l.booking_date),
            deal_type_code=COALESCE(b.deal_type_code, l.deal_type),
            expected_delivery_text=COALESCE(
                b.expected_delivery_text,
                l.expected_delivery
            ),
            expected_delivery_date=COALESCE(
                b.expected_delivery_date,
                l.expected_delivery_date
            ),
            updated_at_utc=CASE
                WHEN (b.booking_reference IS NULL AND l.booking_reference_number IS NOT NULL)
                  OR (b.booking_date IS NULL AND l.booking_date IS NOT NULL)
                  OR (b.deal_type_code IS NULL AND l.deal_type IS NOT NULL)
                  OR (b.expected_delivery_text IS NULL AND l.expected_delivery IS NOT NULL)
                  OR (b.expected_delivery_date IS NULL AND l.expected_delivery_date IS NOT NULL)
                THEN now()
                ELSE b.updated_at_utc
            END,
            version_no=CASE
                WHEN (b.booking_reference IS NULL AND l.booking_reference_number IS NOT NULL)
                  OR (b.booking_date IS NULL AND l.booking_date IS NOT NULL)
                  OR (b.deal_type_code IS NULL AND l.deal_type IS NOT NULL)
                  OR (b.expected_delivery_text IS NULL AND l.expected_delivery IS NOT NULL)
                  OR (b.expected_delivery_date IS NULL AND l.expected_delivery_date IS NOT NULL)
                THEN b.version_no + 1
                ELSE b.version_no
            END
        FROM latest l
        WHERE b.tenant_id=l.tenant_id
          AND b.journey_id=l.journey_id
        """
    )

    # Product snapshots are evidence-backed descriptive facts.  Populate only
    # missing snapshots; SKU-to-product_sku_id resolution is intentionally not
    # guessed here because that requires a master-data match.
    op.execute(
        r"""
        WITH latest AS (
            SELECT DISTINCT ON (tenant_id, journey_id)
                tenant_id,
                journey_id,
                vehicle_model,
                vehicle_variant,
                vehicle_color
            FROM auditcore.booking_form_review_values
            WHERE vehicle_model IS NOT NULL
               OR vehicle_variant IS NOT NULL
               OR vehicle_color IS NOT NULL
            ORDER BY tenant_id, journey_id, reviewed_at_utc DESC,
                     booking_form_review_value_id DESC
        )
        INSERT INTO auditcore.journey_products (
            tenant_id, journey_id, model_name_snapshot,
            variant_name_snapshot, colour_name_snapshot, selection_source
        )
        SELECT
            tenant_id, journey_id, vehicle_model,
            vehicle_variant, vehicle_color, 'EVIDENCE'
        FROM latest
        ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
            model_name_snapshot=COALESCE(
                auditcore.journey_products.model_name_snapshot,
                EXCLUDED.model_name_snapshot
            ),
            variant_name_snapshot=COALESCE(
                auditcore.journey_products.variant_name_snapshot,
                EXCLUDED.variant_name_snapshot
            ),
            colour_name_snapshot=COALESCE(
                auditcore.journey_products.colour_name_snapshot,
                EXCLUDED.colour_name_snapshot
            ),
            selection_source=COALESCE(
                auditcore.journey_products.selection_source,
                EXCLUDED.selection_source
            ),
            updated_at_utc=now()
        """
    )

    # Explicit Booking field owners already exist in the runtime writer.  Mirror
    # that contract for historical reviewed journeys while preserving any value
    # already owned by Registration/Insurance.
    op.execute(
        r"""
        WITH latest AS (
            SELECT DISTINCT ON (tenant_id, journey_id)
                tenant_id,
                journey_id,
                source_evidence_id,
                registration_by
            FROM auditcore.booking_form_review_values
            WHERE registration_by IS NOT NULL
            ORDER BY tenant_id, journey_id, reviewed_at_utc DESC,
                     booking_form_review_value_id DESC
        )
        INSERT INTO auditcore.registration_records (
            tenant_id, journey_id, registration_by,
            source_kind, source_evidence_id
        )
        SELECT
            tenant_id, journey_id, registration_by,
            'EVIDENCE', source_evidence_id
        FROM latest
        ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
            registration_by=COALESCE(
                auditcore.registration_records.registration_by,
                EXCLUDED.registration_by
            ),
            source_kind=COALESCE(
                auditcore.registration_records.source_kind,
                EXCLUDED.source_kind
            ),
            source_evidence_id=COALESCE(
                auditcore.registration_records.source_evidence_id,
                EXCLUDED.source_evidence_id
            ),
            updated_at_utc=CASE
                WHEN auditcore.registration_records.registration_by IS NULL
                     AND EXCLUDED.registration_by IS NOT NULL
                THEN now()
                ELSE auditcore.registration_records.updated_at_utc
            END,
            version_no=CASE
                WHEN auditcore.registration_records.registration_by IS NULL
                     AND EXCLUDED.registration_by IS NOT NULL
                THEN auditcore.registration_records.version_no + 1
                ELSE auditcore.registration_records.version_no
            END
        """
    )

    op.execute(
        r"""
        WITH latest AS (
            SELECT DISTINCT ON (tenant_id, journey_id)
                tenant_id,
                journey_id,
                source_evidence_id,
                insurance_by
            FROM auditcore.booking_form_review_values
            WHERE insurance_by IS NOT NULL
            ORDER BY tenant_id, journey_id, reviewed_at_utc DESC,
                     booking_form_review_value_id DESC
        )
        INSERT INTO auditcore.insurance_records (
            tenant_id, journey_id, insurance_by,
            source_kind, source_evidence_id
        )
        SELECT
            tenant_id, journey_id, insurance_by,
            'EVIDENCE', source_evidence_id
        FROM latest
        ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
            insurance_by=COALESCE(
                auditcore.insurance_records.insurance_by,
                EXCLUDED.insurance_by
            ),
            source_kind=COALESCE(
                auditcore.insurance_records.source_kind,
                EXCLUDED.source_kind
            ),
            source_evidence_id=COALESCE(
                auditcore.insurance_records.source_evidence_id,
                EXCLUDED.source_evidence_id
            ),
            updated_at_utc=CASE
                WHEN auditcore.insurance_records.insurance_by IS NULL
                     AND EXCLUDED.insurance_by IS NOT NULL
                THEN now()
                ELSE auditcore.insurance_records.updated_at_utc
            END,
            version_no=CASE
                WHEN auditcore.insurance_records.insurance_by IS NULL
                     AND EXCLUDED.insurance_by IS NOT NULL
                THEN auditcore.insurance_records.version_no + 1
                ELSE auditcore.insurance_records.version_no
            END
        """
    )

    # Existing Booking Payment rows created before full receipt materialization can
    # have amount only.  Fill their missing reviewed Dealer Receipt fields by exact
    # DI document identity.  Never overwrite a non-null Payment value.
    op.execute(
        r"""
        UPDATE auditcore.payments p
        SET amount=COALESCE(p.amount, r.amount_paid),
            payment_method_code=COALESCE(p.payment_method_code, r.payment_mode),
            payment_reference=COALESCE(p.payment_reference, r.payment_reference_no),
            receipt_number=COALESCE(p.receipt_number, r.receipt_number),
            receipt_date=COALESCE(p.receipt_date, r.receipt_date),
            receipt_dealer_name=COALESCE(p.receipt_dealer_name, r.dealer_name),
            receipt_dealer_gstin=COALESCE(p.receipt_dealer_gstin, r.dealer_gstin),
            receipt_customer_name=COALESCE(p.receipt_customer_name, r.customer_name),
            receipt_customer_phone=COALESCE(p.receipt_customer_phone, r.customer_phone),
            payment_reference_date=COALESCE(
                p.payment_reference_date,
                r.payment_reference_date
            ),
            receipt_bank_name=COALESCE(p.receipt_bank_name, r.bank_name),
            receipt_bank_location=COALESCE(p.receipt_bank_location, r.bank_location),
            receipt_booking_reference=COALESCE(
                p.receipt_booking_reference,
                r.booking_reference_number
            ),
            receipt_remarks=COALESCE(p.receipt_remarks, r.remarks),
            receipt_amount_in_words=COALESCE(
                p.receipt_amount_in_words,
                r.amount_in_words
            ),
            status_source=COALESCE(p.status_source, 'EVIDENCE'),
            source_evidence_id=COALESCE(p.source_evidence_id, r.source_evidence_id),
            payment_stage=COALESCE(p.payment_stage, 'BOOKING'),
            updated_at_utc=now(),
            version_no=p.version_no+1
        FROM auditcore.dealer_receipt_review_values r
        WHERE p.tenant_id=r.tenant_id
          AND p.journey_id=r.journey_id
          AND p.source_di_document_id=r.source_di_document_id
          AND (
                p.payment_method_code IS NULL
             OR p.payment_reference IS NULL
             OR p.receipt_number IS NULL
             OR p.receipt_date IS NULL
             OR p.receipt_dealer_name IS NULL
             OR p.receipt_dealer_gstin IS NULL
             OR p.receipt_customer_name IS NULL
             OR p.receipt_customer_phone IS NULL
             OR p.payment_reference_date IS NULL
             OR p.receipt_bank_name IS NULL
             OR p.receipt_bank_location IS NULL
             OR p.receipt_booking_reference IS NULL
             OR p.receipt_remarks IS NULL
             OR p.receipt_amount_in_words IS NULL
             OR p.status_source IS NULL
             OR p.source_evidence_id IS NULL
             OR p.payment_stage IS NULL
          )
        """
    )

    # If an accepted historical Booking receipt never produced a Payment row at all,
    # create exactly one document-backed Payment.  Receipts without amount remain in
    # dealer_receipt_review_values because payments.amount is intentionally NOT NULL.
    op.execute(
        r"""
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
            r.tenant_id,
            r.journey_id,
            r.amount_paid,
            r.payment_mode,
            r.payment_reference_no,
            r.receipt_number,
            r.receipt_date,
            r.dealer_name,
            r.dealer_gstin,
            r.customer_name,
            r.customer_phone,
            r.payment_reference_date,
            r.bank_name,
            r.bank_location,
            r.booking_reference_number,
            r.remarks,
            r.amount_in_words,
            'EVIDENCE',
            r.source_evidence_id,
            r.source_di_document_id,
            'BOOKING'
        FROM auditcore.dealer_receipt_review_values r
        WHERE r.amount_paid IS NOT NULL
          AND NOT EXISTS (
                SELECT 1
                FROM auditcore.payments p
                WHERE p.tenant_id=r.tenant_id
                  AND p.journey_id=r.journey_id
                  AND p.source_di_document_id=r.source_di_document_id
          )
        """
    )


def downgrade() -> None:
    # Historical canonical repair is intentionally non-destructive on downgrade.
    pass
