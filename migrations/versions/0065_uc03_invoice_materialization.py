"""Persist reviewed dealer invoices in Audit Core.

Revision ID: 0065_uc03_invoice_materialization
Revises: 0064_uc03_model_not_identified
Create Date: 2026-09-08

The DI generalized-invoice schema (verigence-di ``schemas/invoice.py``) extracts
every dealer invoice type (vehicle tax / retail, wholesale, accessory, extended
warranty, RSA, other) against one lossless superset with deliberately neutral
commercial names (``taxable_amount``, ``invoice_discount_amount``,
``grand_total_amount``, ``line_items[].line_category`` ...). Those keys never
matched Audit Core's booking-form commercial vocabulary, so invoice evidence was
preserved losslessly but never reached ``commercial_lines`` / ``discount_applications``.

One typed row per reviewed invoice document (keyed by the DI document) keeps the
exact printed evidence — multiple invoice types can and do exist on one journey
and must be stored separately. ``uc03_invoice_materialization`` then projects the
derived per-component amounts into the canonical reconciliation tables with the
invoice-first source priority already configured in ``uc03_attribute_mapping``.
"""

from alembic import op

revision = "0065_uc03_invoice_ingest"
down_revision = "0064_uc03_model_not_identified"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auditcore.invoice_review_values (
            tenant_id                       varchar(128) NOT NULL,
            journey_id                      uuid NOT NULL,
            invoice_review_value_id         uuid NOT NULL DEFAULT gen_random_uuid(),
            source_di_document_id           uuid NOT NULL,
            source_evidence_id              uuid,
            document_type_key               varchar(80) NOT NULL,

            invoice_purpose                 varchar(40),
            invoice_nature                  varchar(40),
            invoice_heading_as_printed      varchar(240),
            source_system                   varchar(30),
            issuer_role                     varchar(40),

            invoice_number                  varchar(160),
            invoice_date                    date,
            seller_name                     varchar(240),
            seller_gstin                    varchar(30),
            seller_address                  text,
            buyer_name                      varchar(240),
            buyer_customer_id               varchar(160),
            buyer_gstin                     varchar(30),
            buyer_gstin_status              varchar(20),
            buyer_address                   text,
            financed_by                     varchar(240),

            gross_amount_before_discount    numeric(18,2),
            invoice_discount_amount         numeric(18,2),
            taxable_amount                  numeric(18,2),
            cgst_amount                     numeric(18,2),
            sgst_amount                     numeric(18,2),
            igst_amount                     numeric(18,2),
            cess_amount                     numeric(18,2),
            tcs_amount                      numeric(18,2),
            round_off_amount                numeric(18,2),
            grand_total_amount              numeric(18,2),
            amount_in_words                 text,
            narration                       text,
            line_items                      jsonb NOT NULL DEFAULT '[]'::jsonb,

            vehicle_description_raw          text,
            sku_code                        varchar(160),
            model_name_raw                  varchar(240),
            variant_raw                     varchar(240),
            vin_number                      varchar(80),
            chassis_number                  varchar(80),
            engine_number                   varchar(80),
            vehicle_color                   varchar(120),
            vehicle_registration_number     varchar(120),

            plan_name                       varchar(240),
            coverage_start_date             date,
            coverage_end_date               date,
            tenure_months                   integer,

            reviewed_by_actor_id            varchar(160) NOT NULL,
            reviewed_at_utc                 timestamptz NOT NULL DEFAULT now(),
            created_at_utc                  timestamptz NOT NULL DEFAULT now(),
            updated_at_utc                  timestamptz NOT NULL DEFAULT now(),
            version_no                      bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),

            PRIMARY KEY (tenant_id, invoice_review_value_id),
            UNIQUE (tenant_id, journey_id, source_di_document_id),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id),
            FOREIGN KEY (tenant_id, source_evidence_id)
                REFERENCES auditcore.evidence(tenant_id, evidence_id)
        );

        CREATE INDEX ix_invoice_review_values_journey
            ON auditcore.invoice_review_values
               (tenant_id, journey_id, reviewed_at_utc DESC);

        ALTER TABLE auditcore.invoice_review_values ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.invoice_review_values FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation_invoice_review_values
            ON auditcore.invoice_review_values
            USING (tenant_id = auditcore.current_tenant_id())
            WITH CHECK (tenant_id = auditcore.current_tenant_id());
        CREATE TRIGGER trg_invoice_review_values_updated
            BEFORE UPDATE ON auditcore.invoice_review_values
            FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at();
        GRANT SELECT, INSERT, UPDATE ON auditcore.invoice_review_values
            TO audit_core_runtime;
        REVOKE DELETE ON auditcore.invoice_review_values FROM audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.invoice_review_values")
