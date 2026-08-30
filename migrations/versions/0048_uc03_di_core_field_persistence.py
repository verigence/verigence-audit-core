"""Persist reviewed Booking/PAN/Aadhaar/Receipt fields in Audit Core.

Revision ID: 0048_uc03_di_core_fields
Revises: 0047_uc03_journey_search
Create Date: 2026-08-30

DI remains the extraction/evidence system. Once Booking Review accepts an extracted
value, Audit Core persists the reviewed business value in typed Core storage.
No generic DI payload copy is introduced.
"""

from alembic import op

revision = "0048_uc03_di_core_fields"
down_revision = "0047_uc03_journey_search"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    # One typed row per reviewed Booking Form document. Field names intentionally
    # mirror the DI Booking Form v1.4 contract so schema drift is visible rather
    # than silently discarded. product_sku_id remains a separate Core-derived
    # business interpretation; sku_code here is only the explicit reviewed text
    # when DI actually extracted it from the document.
    op.execute(
        """
        CREATE TABLE auditcore.booking_form_review_values (
            tenant_id                       varchar(128) NOT NULL,
            journey_id                      uuid NOT NULL,
            booking_form_review_value_id    uuid NOT NULL DEFAULT gen_random_uuid(),
            source_di_document_id            uuid NOT NULL,
            source_evidence_id               uuid,

            dealer_name                      varchar(240),
            dealer_branch                    varchar(240),
            booking_reference_number         varchar(160),
            booking_date                     date,
            customer_name                    varchar(240),
            customer_phone                   varchar(40),
            customer_email                   varchar(320),
            customer_address                 text,
            vehicle_model                    varchar(240),
            vehicle_variant                  varchar(240),
            vehicle_color                    varchar(200),
            sku_code                         varchar(160),
            sales_person                     varchar(240),
            registration_by                  varchar(240),
            registration_type                varchar(120),
            insurance_by                     varchar(240),
            exchange_applicable              boolean,
            exchange_value                   numeric(18,2),
            ex_showroom_price                numeric(18,2),
            insurance_amount                 numeric(18,2),
            registration_charges             numeric(18,2),
            road_tax_amount                  numeric(18,2),
            road_tax_registration            numeric(18,2),
            tcs_amount                       numeric(18,2),
            rsa_amount                       numeric(18,2),
            additional_warranty_amount       numeric(18,2),
            accessories_cost                 numeric(18,2),
            other_charges                    numeric(18,2),
            discount_amount                  numeric(18,2),
            bonus_amount                     numeric(18,2),
            total_price                      numeric(18,2),
            net_amount                       numeric(18,2),
            booking_amount_paid              numeric(18,2),
            balance_amount                   numeric(18,2),
            mode_of_payment                  varchar(80),
            payment_reference_no             varchar(240),
            expected_delivery                text,
            expected_delivery_date           date,

            reviewed_by_actor_id             varchar(160) NOT NULL,
            reviewed_at_utc                  timestamptz NOT NULL DEFAULT now(),
            created_at_utc                   timestamptz NOT NULL DEFAULT now(),
            updated_at_utc                   timestamptz NOT NULL DEFAULT now(),
            version_no                       bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),

            PRIMARY KEY (tenant_id, booking_form_review_value_id),
            UNIQUE (tenant_id, journey_id, source_di_document_id),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id),
            FOREIGN KEY (tenant_id, source_evidence_id)
                REFERENCES auditcore.evidence(tenant_id, evidence_id)
        );

        CREATE INDEX ix_booking_form_review_values_journey
            ON auditcore.booking_form_review_values
               (tenant_id, journey_id, reviewed_at_utc DESC);

        ALTER TABLE auditcore.booking_form_review_values ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.booking_form_review_values FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation_booking_form_review_values
            ON auditcore.booking_form_review_values
            USING (tenant_id = auditcore.current_tenant_id())
            WITH CHECK (tenant_id = auditcore.current_tenant_id());
        CREATE TRIGGER trg_booking_form_review_values_updated
            BEFORE UPDATE ON auditcore.booking_form_review_values
            FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at();
        GRANT SELECT, INSERT, UPDATE ON auditcore.booking_form_review_values
            TO audit_core_runtime;
        REVOKE DELETE ON auditcore.booking_form_review_values FROM audit_core_runtime;
        """
    )

    # PAN and Aadhaar can both exist and can disagree. A single customers.* value
    # cannot preserve both reviewed source facts, therefore the dependent table is
    # keyed by the DI document and keeps the exact source-specific business fields.
    op.execute(
        """
        CREATE TABLE auditcore.customer_identity_review_values (
            tenant_id                       varchar(128) NOT NULL,
            journey_id                      uuid NOT NULL,
            customer_id                     uuid NOT NULL,
            customer_identity_review_value_id uuid NOT NULL DEFAULT gen_random_uuid(),
            source_di_document_id            uuid NOT NULL,
            source_evidence_id               uuid,
            document_type_key                varchar(20) NOT NULL
                                             CHECK (document_type_key IN ('PAN','AADHAAR')),

            pan_number                       varchar(32),
            pan_name                         varchar(240),
            pan_father_name                  varchar(240),
            pan_relationship_type            varchar(3),
            pan_relationship_name            varchar(240),
            pan_date_of_birth                date,

            aadhaar_number                   varchar(64),
            aadhaar_name                     varchar(240),
            aadhaar_date_of_birth            date,
            aadhaar_gender                   varchar(40),
            aadhaar_address                  text,
            aadhaar_relationship_type        varchar(3),
            aadhaar_relationship_name        varchar(240),

            reviewed_by_actor_id             varchar(160) NOT NULL,
            reviewed_at_utc                  timestamptz NOT NULL DEFAULT now(),
            created_at_utc                   timestamptz NOT NULL DEFAULT now(),
            updated_at_utc                   timestamptz NOT NULL DEFAULT now(),
            version_no                       bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),

            PRIMARY KEY (tenant_id, customer_identity_review_value_id),
            UNIQUE (tenant_id, journey_id, source_di_document_id),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id),
            FOREIGN KEY (tenant_id, customer_id)
                REFERENCES auditcore.customers(tenant_id, customer_id),
            FOREIGN KEY (tenant_id, source_evidence_id)
                REFERENCES auditcore.evidence(tenant_id, evidence_id),
            CHECK (pan_relationship_type IS NULL OR pan_relationship_type IN ('S/O','W/O','D/O')),
            CHECK (aadhaar_relationship_type IS NULL OR aadhaar_relationship_type IN ('S/O','W/O','D/O')),
            CHECK (
                (document_type_key = 'PAN'
                 AND aadhaar_number IS NULL
                 AND aadhaar_name IS NULL
                 AND aadhaar_date_of_birth IS NULL
                 AND aadhaar_gender IS NULL
                 AND aadhaar_address IS NULL
                 AND aadhaar_relationship_type IS NULL
                 AND aadhaar_relationship_name IS NULL)
                OR
                (document_type_key = 'AADHAAR'
                 AND pan_number IS NULL
                 AND pan_name IS NULL
                 AND pan_father_name IS NULL
                 AND pan_relationship_type IS NULL
                 AND pan_relationship_name IS NULL
                 AND pan_date_of_birth IS NULL)
            )
        );

        CREATE INDEX ix_customer_identity_review_values_customer
            ON auditcore.customer_identity_review_values
               (tenant_id, customer_id, document_type_key, reviewed_at_utc DESC);

        ALTER TABLE auditcore.customer_identity_review_values ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.customer_identity_review_values FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation_customer_identity_review_values
            ON auditcore.customer_identity_review_values
            USING (tenant_id = auditcore.current_tenant_id())
            WITH CHECK (tenant_id = auditcore.current_tenant_id());
        CREATE TRIGGER trg_customer_identity_review_values_updated
            BEFORE UPDATE ON auditcore.customer_identity_review_values
            FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at();
        GRANT SELECT, INSERT, UPDATE ON auditcore.customer_identity_review_values
            TO audit_core_runtime;
        REVOKE DELETE ON auditcore.customer_identity_review_values FROM audit_core_runtime;
        """
    )

    # Keep every reviewed Dealer Receipt field even when amount is absent. A Payment
    # row cannot be created in that case because payments.amount is intentionally
    # NOT NULL; this dependent row prevents any accepted receipt field from being lost.
    op.execute(
        """
        CREATE TABLE auditcore.dealer_receipt_review_values (
            tenant_id                       varchar(128) NOT NULL,
            journey_id                      uuid NOT NULL,
            dealer_receipt_review_value_id  uuid NOT NULL DEFAULT gen_random_uuid(),
            source_di_document_id            uuid NOT NULL,
            source_evidence_id               uuid,

            dealer_name                      varchar(240),
            dealer_gstin                     varchar(40),
            customer_name                    varchar(240),
            customer_phone                   varchar(40),
            receipt_number                   varchar(160),
            receipt_date                     date,
            amount_paid                      numeric(18,2),
            payment_mode                     varchar(80),
            payment_reference_no             varchar(240),
            payment_reference_date           date,
            bank_name                        varchar(240),
            bank_location                    varchar(240),
            booking_reference_number         varchar(160),
            remarks                          text,
            amount_in_words                  text,

            reviewed_by_actor_id             varchar(160) NOT NULL,
            reviewed_at_utc                  timestamptz NOT NULL DEFAULT now(),
            created_at_utc                   timestamptz NOT NULL DEFAULT now(),
            updated_at_utc                   timestamptz NOT NULL DEFAULT now(),
            version_no                       bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),

            PRIMARY KEY (tenant_id, dealer_receipt_review_value_id),
            UNIQUE (tenant_id, journey_id, source_di_document_id),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id),
            FOREIGN KEY (tenant_id, source_evidence_id)
                REFERENCES auditcore.evidence(tenant_id, evidence_id)
        );

        CREATE INDEX ix_dealer_receipt_review_values_journey
            ON auditcore.dealer_receipt_review_values
               (tenant_id, journey_id, reviewed_at_utc DESC);

        ALTER TABLE auditcore.dealer_receipt_review_values ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.dealer_receipt_review_values FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation_dealer_receipt_review_values
            ON auditcore.dealer_receipt_review_values
            USING (tenant_id = auditcore.current_tenant_id())
            WITH CHECK (tenant_id = auditcore.current_tenant_id());
        CREATE TRIGGER trg_dealer_receipt_review_values_updated
            BEFORE UPDATE ON auditcore.dealer_receipt_review_values
            FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at();
        GRANT SELECT, INSERT, UPDATE ON auditcore.dealer_receipt_review_values
            TO audit_core_runtime;
        REVOKE DELETE ON auditcore.dealer_receipt_review_values FROM audit_core_runtime;
        """
    )

    # Dealer Receipt v1.1 has 15 fields. Five already had typed Payment columns;
    # promote the remaining ten out of receipt_details JSON into first-class columns.
    op.execute(
        """
        ALTER TABLE auditcore.payments
            ADD COLUMN receipt_dealer_name varchar(240),
            ADD COLUMN receipt_dealer_gstin varchar(40),
            ADD COLUMN receipt_customer_name varchar(240),
            ADD COLUMN receipt_customer_phone varchar(40),
            ADD COLUMN payment_reference_date date,
            ADD COLUMN receipt_bank_name varchar(240),
            ADD COLUMN receipt_bank_location varchar(240),
            ADD COLUMN receipt_booking_reference varchar(160),
            ADD COLUMN receipt_remarks text,
            ADD COLUMN receipt_amount_in_words text;

        UPDATE auditcore.payments
        SET receipt_dealer_name = NULLIF(receipt_details->>'dealer_name', ''),
            receipt_dealer_gstin = NULLIF(receipt_details->>'dealer_gstin', ''),
            receipt_customer_name = NULLIF(receipt_details->>'customer_name', ''),
            receipt_customer_phone = NULLIF(receipt_details->>'customer_phone', ''),
            payment_reference_date = CASE
                WHEN COALESCE(receipt_details->>'payment_reference_date', '') ~ '^\\d{4}-\\d{2}-\\d{2}$'
                THEN (receipt_details->>'payment_reference_date')::date
                ELSE NULL
            END,
            receipt_bank_name = NULLIF(receipt_details->>'bank_name', ''),
            receipt_bank_location = NULLIF(receipt_details->>'bank_location', ''),
            receipt_booking_reference = NULLIF(receipt_details->>'booking_reference_number', ''),
            receipt_remarks = NULLIF(receipt_details->>'remarks', ''),
            receipt_amount_in_words = NULLIF(receipt_details->>'amount_in_words', '')
        WHERE receipt_details IS NOT NULL;
        """
    )

    # Backward compatibility for legacy/V1 paths that still write receipt_details.
    # The trigger only fills a typed column when the caller did not supply it.
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION auditcore.sync_payment_receipt_columns()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.receipt_details IS NULL THEN
                RETURN NEW;
            END IF;
            NEW.receipt_dealer_name := COALESCE(NEW.receipt_dealer_name, NULLIF(NEW.receipt_details->>'dealer_name', ''));
            NEW.receipt_dealer_gstin := COALESCE(NEW.receipt_dealer_gstin, NULLIF(NEW.receipt_details->>'dealer_gstin', ''));
            NEW.receipt_customer_name := COALESCE(NEW.receipt_customer_name, NULLIF(NEW.receipt_details->>'customer_name', ''));
            NEW.receipt_customer_phone := COALESCE(NEW.receipt_customer_phone, NULLIF(NEW.receipt_details->>'customer_phone', ''));
            IF NEW.payment_reference_date IS NULL
               AND COALESCE(NEW.receipt_details->>'payment_reference_date', '') ~ '^\d{4}-\d{2}-\d{2}$' THEN
                NEW.payment_reference_date := (NEW.receipt_details->>'payment_reference_date')::date;
            END IF;
            NEW.receipt_bank_name := COALESCE(NEW.receipt_bank_name, NULLIF(NEW.receipt_details->>'bank_name', ''));
            NEW.receipt_bank_location := COALESCE(NEW.receipt_bank_location, NULLIF(NEW.receipt_details->>'bank_location', ''));
            NEW.receipt_booking_reference := COALESCE(NEW.receipt_booking_reference, NULLIF(NEW.receipt_details->>'booking_reference_number', ''));
            NEW.receipt_remarks := COALESCE(NEW.receipt_remarks, NULLIF(NEW.receipt_details->>'remarks', ''));
            NEW.receipt_amount_in_words := COALESCE(NEW.receipt_amount_in_words, NULLIF(NEW.receipt_details->>'amount_in_words', ''));
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_sync_payment_receipt_columns
        BEFORE INSERT OR UPDATE OF receipt_details,
            receipt_dealer_name, receipt_dealer_gstin, receipt_customer_name,
            receipt_customer_phone, payment_reference_date, receipt_bank_name,
            receipt_bank_location, receipt_booking_reference, receipt_remarks,
            receipt_amount_in_words
        ON auditcore.payments
        FOR EACH ROW EXECUTE FUNCTION auditcore.sync_payment_receipt_columns();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_sync_payment_receipt_columns ON auditcore.payments")
    op.execute("DROP FUNCTION IF EXISTS auditcore.sync_payment_receipt_columns()")
    op.execute(
        """
        ALTER TABLE auditcore.payments
            DROP COLUMN IF EXISTS receipt_amount_in_words,
            DROP COLUMN IF EXISTS receipt_remarks,
            DROP COLUMN IF EXISTS receipt_booking_reference,
            DROP COLUMN IF EXISTS receipt_bank_location,
            DROP COLUMN IF EXISTS receipt_bank_name,
            DROP COLUMN IF EXISTS payment_reference_date,
            DROP COLUMN IF EXISTS receipt_customer_phone,
            DROP COLUMN IF EXISTS receipt_customer_name,
            DROP COLUMN IF EXISTS receipt_dealer_gstin,
            DROP COLUMN IF EXISTS receipt_dealer_name;
        """
    )
    op.execute("DROP TABLE IF EXISTS auditcore.dealer_receipt_review_values")
    op.execute("DROP TABLE IF EXISTS auditcore.customer_identity_review_values")
    op.execute("DROP TABLE IF EXISTS auditcore.booking_form_review_values")
