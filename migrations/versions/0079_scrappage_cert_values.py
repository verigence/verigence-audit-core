"""Persist reviewed Scrappage Certificate of Deposit documents in Audit Core.

Revision ID: 0079_scrappage_cert_values
Revises: 0078_scrappage_cert_req
Create Date: 2026-09-10

DI now classifies and extracts Vehicle Scrappage Certificates of Deposit
(verigence-di#71: new document type ``scrappage_certificate_of_deposit``),
but nothing on the Audit Core side persisted that evidence anywhere typed --
the business ask was explicitly to "extract these details ... and capture
old vehicle details" (the scrapped vehicle's own make/model/spec sheet), not
just leave it in the generic extracted-fields log.

One typed row per reviewed certificate document, same shape as
``invoice_review_values`` (0065): a journey can plausibly hold more than one
(the original Certificate of Deposit plus a Transfer Certificate of Deposit
recording its resale), so this is keyed per source document, not one row per
journey. ``uc03_scrappage_certificate_materialization.py`` populates it via
the same shared ``_upsert_review_value_row`` helper the invoice materializer
uses, wired into both the Booking and Delivery review orchestrators.
"""

from alembic import op

revision = "0079_scrappage_cert_values"
down_revision = "0078_scrappage_cert_req"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auditcore.scrappage_certificate_review_values (
            tenant_id                              varchar(128) NOT NULL,
            journey_id                              uuid NOT NULL,
            scrappage_certificate_review_value_id   uuid NOT NULL DEFAULT gen_random_uuid(),
            source_di_document_id                   uuid NOT NULL,
            source_evidence_id                      uuid,
            document_type_key                       varchar(80) NOT NULL,

            certificate_variant                     varchar(20),
            certificate_number                      varchar(160),
            old_vehicle_registration_number         varchar(80),
            old_vehicle_make                        varchar(160),
            old_vehicle_model                       varchar(160),
            old_vehicle_category                    varchar(80),
            old_vehicle_type                        varchar(80),
            old_vehicle_fuel_type                   varchar(60),
            old_vehicle_cubic_capacity              numeric(10,2),
            old_vehicle_seating_capacity             integer,
            old_vehicle_year_of_manufacturing       varchar(20),
            old_vehicle_unladen_weight_kg           numeric(10,2),
            old_vehicle_number_of_cylinders         integer,
            old_vehicle_gross_vehicle_weight_kg     numeric(10,2),
            old_vehicle_wheelbase_mm                numeric(10,2),

            original_owner_name                     varchar(240),
            current_holder_name                     varchar(240),
            current_holder_mobile                   varchar(40),
            current_holder_pan                      varchar(20),
            trade_date                              date,
            trade_number                            varchar(80),
            certificate_issue_date                  date,
            certificate_valid_until_date            date,
            scrapping_facility_name                 varchar(240),
            rvsf_registration_number                varchar(80),
            state_of_scrapping                      varchar(80),

            reviewed_by_actor_id                    varchar(160) NOT NULL,
            reviewed_at_utc                         timestamptz NOT NULL DEFAULT now(),
            created_at_utc                          timestamptz NOT NULL DEFAULT now(),
            updated_at_utc                          timestamptz NOT NULL DEFAULT now(),
            version_no                              bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),

            PRIMARY KEY (tenant_id, scrappage_certificate_review_value_id),
            UNIQUE (tenant_id, journey_id, source_di_document_id),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id),
            FOREIGN KEY (tenant_id, source_evidence_id)
                REFERENCES auditcore.evidence(tenant_id, evidence_id)
        );

        CREATE INDEX ix_scrappage_certificate_review_values_journey
            ON auditcore.scrappage_certificate_review_values
               (tenant_id, journey_id, reviewed_at_utc DESC);

        ALTER TABLE auditcore.scrappage_certificate_review_values ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.scrappage_certificate_review_values FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation_scrappage_certificate_review_values
            ON auditcore.scrappage_certificate_review_values
            USING (tenant_id = auditcore.current_tenant_id())
            WITH CHECK (tenant_id = auditcore.current_tenant_id());
        CREATE TRIGGER trg_scrappage_certificate_review_values_updated
            BEFORE UPDATE ON auditcore.scrappage_certificate_review_values
            FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at();
        GRANT SELECT, INSERT, UPDATE ON auditcore.scrappage_certificate_review_values
            TO audit_core_runtime;
        REVOKE DELETE ON auditcore.scrappage_certificate_review_values FROM audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.scrappage_certificate_review_values")
