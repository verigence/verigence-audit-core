"""UC03 payment bank-statement reconciliation.

Revision ID: 0066_uc03_bank_recon
Revises: 0065_uc03_invoice_ingest
Create Date: 2026-09-08

DI already extracts ``bank_statement_extract`` (verigence-di
``schemas/bank_statement.py``). Audit Core stores every reviewed statement line
per journey and matches it against the captured Dealer Receipts / Payments so a
non-cash payment that is not evidenced by a bank credit raises a
``PAYMENT_UNVERIFIED`` data gap, and a matched one records a VERIFIED payment
verification event (which already satisfies the Delivery-completion gate).
"""

from alembic import op

revision = "0066_uc03_bank_recon"
down_revision = "0065_uc03_invoice_ingest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auditcore.bank_statement_lines (
            tenant_id                       varchar(128) NOT NULL,
            journey_id                      uuid NOT NULL,
            bank_statement_line_id          uuid NOT NULL DEFAULT gen_random_uuid(),
            source_di_document_id           uuid NOT NULL,
            source_evidence_id              uuid,

            bank_name                       varchar(240),
            account_holder_name             varchar(240),
            account_number                  varchar(64),
            transaction_date                date,
            value_date                      date,
            transaction_description         text,
            reference_no                    varchar(240),
            counterparty_name               varchar(240),
            debit_amount                    numeric(18,2),
            credit_amount                   numeric(18,2),
            running_balance                 numeric(18,2),
            manually_flagged                boolean,

            reviewed_by_actor_id            varchar(160) NOT NULL,
            reviewed_at_utc                 timestamptz NOT NULL DEFAULT now(),
            created_at_utc                  timestamptz NOT NULL DEFAULT now(),
            updated_at_utc                  timestamptz NOT NULL DEFAULT now(),
            version_no                      bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),

            PRIMARY KEY (tenant_id, bank_statement_line_id),
            UNIQUE (tenant_id, journey_id, source_di_document_id),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id),
            FOREIGN KEY (tenant_id, source_evidence_id)
                REFERENCES auditcore.evidence(tenant_id, evidence_id)
        );

        CREATE INDEX ix_bank_statement_lines_journey
            ON auditcore.bank_statement_lines
               (tenant_id, journey_id, reviewed_at_utc DESC);

        ALTER TABLE auditcore.bank_statement_lines ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.bank_statement_lines FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation_bank_statement_lines
            ON auditcore.bank_statement_lines
            USING (tenant_id = auditcore.current_tenant_id())
            WITH CHECK (tenant_id = auditcore.current_tenant_id());
        CREATE TRIGGER trg_bank_statement_lines_updated
            BEFORE UPDATE ON auditcore.bank_statement_lines
            FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at();
        GRANT SELECT, INSERT, UPDATE ON auditcore.bank_statement_lines
            TO audit_core_runtime;
        REVOKE DELETE ON auditcore.bank_statement_lines FROM audit_core_runtime;
        """
    )

    op.execute(
        """
        CREATE TABLE auditcore.payment_bank_matches (
            tenant_id                       varchar(128) NOT NULL,
            journey_id                      uuid NOT NULL,
            payment_bank_match_id           uuid NOT NULL DEFAULT gen_random_uuid(),
            payment_id                      uuid NOT NULL,
            bank_statement_line_id          uuid,
            match_status                    varchar(20) NOT NULL
                                            CHECK (match_status IN
                                                ('MATCHED','UNMATCHED','NOT_APPLICABLE','AMBIGUOUS')),
            match_method                    varchar(30) NOT NULL DEFAULT 'NONE'
                                            CHECK (match_method IN
                                                ('REFERENCE_EXACT','UTR_SUFFIX','AMOUNT_DATE','NONE')),
            candidate_line_ids              jsonb NOT NULL DEFAULT '[]'::jsonb,
            details                         jsonb NOT NULL DEFAULT '{}'::jsonb,
            matched_at_utc                  timestamptz,
            created_at_utc                  timestamptz NOT NULL DEFAULT now(),
            updated_at_utc                  timestamptz NOT NULL DEFAULT now(),

            PRIMARY KEY (tenant_id, payment_bank_match_id),
            UNIQUE (tenant_id, payment_id),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id),
            FOREIGN KEY (tenant_id, payment_id)
                REFERENCES auditcore.payments(tenant_id, payment_id),
            FOREIGN KEY (tenant_id, bank_statement_line_id)
                REFERENCES auditcore.bank_statement_lines(tenant_id, bank_statement_line_id)
        );

        CREATE INDEX ix_payment_bank_matches_journey
            ON auditcore.payment_bank_matches (tenant_id, journey_id);

        ALTER TABLE auditcore.payment_bank_matches ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.payment_bank_matches FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation_payment_bank_matches
            ON auditcore.payment_bank_matches
            USING (tenant_id = auditcore.current_tenant_id())
            WITH CHECK (tenant_id = auditcore.current_tenant_id());
        CREATE TRIGGER trg_payment_bank_matches_updated
            BEFORE UPDATE ON auditcore.payment_bank_matches
            FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at();
        GRANT SELECT, INSERT, UPDATE ON auditcore.payment_bank_matches
            TO audit_core_runtime;
        REVOKE DELETE ON auditcore.payment_bank_matches FROM audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.payment_bank_matches")
    op.execute("DROP TABLE IF EXISTS auditcore.bank_statement_lines")
