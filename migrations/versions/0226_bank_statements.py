"""0226_bank_statements -- the dealership's own bank account statements, reconciled
against the payment receipts on file (product owner 2026-11-14).

"bank statement payment reconcile ... this could be per outlet or per dealership, file
could be .pdf, or excel ... different bank will have different format ... add a section
Upload in the Verigence, and then the above files needs to be read and checked against
the payment receipts for that particular dealership or outlet ... how many payments
doesnt got identified from the bank statement and which all journey got fully verified
and which partially and how much amount and how much left."
"when we do cross check it should be from +- 5 days for which the account statement is..
and only those bookings and journey need to be verified."

- p2_bank_statements          one per file uploaded: dealership (and outlet, when the
                              account is one outlet's), the account it is for (bank,
                              number, holder as printed), the period it covers, whether
                              the reading agrees with the statement's own totals, who
                              uploaded it, and its reconciliation counts. The same
                              account and period uploaded again supersedes the earlier
                              one (kept, never deleted).
- p2_bank_statement_txns      every transaction read, as printed (date, narration,
                              reference / cheque, amount, Cr/Dr, balance), what kind of
                              money a credit is (customer, cash deposit, card / UPI
                              settlement, loan, insurer, own transfer), and the receipts
                              it was tied to -- or the Journey whose customer it names
                              when no receipt on file stands for it.
- p2_bank_receipt_matches     the current verdict for each receipt (or customer ledger
                              posting) whose date falls in an uploaded statement's period
                              +/- 5 days: matched (on what), several possible credits,
                              not found (dated within the statement's own dates),
                              outside the statement's dates (in the 5-day margin only),
                              or cash / card not traceable one by one. A run
                              re-decides the dealership's receipts; rows no longer in any
                              statement's range are kept, marked not current.

- p2_journey_bank_status     per Journey: its bank receipts (every counted non-cash
                              receipt, any date), how much is found in the bank and how
                              much is left; BANK_VERIFIED once every one is found (product
                              owner: "if all got verified, status of that journey should be
                              updated to bank verified"), with the day it first was.

A receipt found in the bank also gets a VERIFIED payment_verification_events row (the
payment's own verification status).

Tenant row security as every Audit Core table; the runtime role may read, add and update,
never delete.
"""
from __future__ import annotations

from alembic import op

revision = "0226_bank_statements"
down_revision = "0225_worker_job_heartbeat"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLES = ("p2_bank_statements", "p2_bank_statement_txns", "p2_bank_receipt_matches", "p2_journey_bank_status")


def _secure(table: str) -> None:
    op.execute(f"ALTER TABLE auditcore.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON auditcore.{table}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{table} ON auditcore.{table} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.{table} TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE DELETE ON auditcore.{table} FROM {_RUNTIME_ROLE}")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_bank_statements (
            tenant_id             varchar(128)  NOT NULL,
            statement_id          uuid          NOT NULL,
            dealer_id             uuid          NOT NULL,
            outlet_id             uuid,
            bank_name             varchar(120),
            account_number        varchar(40),
            account_holder        varchar(300),
            ifsc                  varchar(20),
            period_from           date          NOT NULL,
            period_to             date          NOT NULL,
            opening_balance       numeric(16,2),
            closing_balance       numeric(16,2),
            credit_count          integer       NOT NULL DEFAULT 0,
            credit_total          numeric(16,2) NOT NULL DEFAULT 0,
            debit_count           integer       NOT NULL DEFAULT 0,
            debit_total           numeric(16,2) NOT NULL DEFAULT 0,
            printed_credit_total  numeric(16,2),
            printed_debit_total   numeric(16,2),
            reading_check         varchar(12)   NOT NULL DEFAULT 'UNCHECKED'
                                    CHECK (reading_check IN ('VERIFIED', 'UNCHECKED', 'MISMATCH')),
            file_name             varchar(300)  NOT NULL,
            file_format           varchar(20)   NOT NULL,
            status                varchar(12)   NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'SUPERSEDED')),
            counts                jsonb         NOT NULL DEFAULT '{}'::jsonb,
            uploaded_by           varchar(200)  NOT NULL,
            uploaded_by_name      varchar(300),
            uploaded_at_utc       timestamptz   NOT NULL DEFAULT now(),
            reconciled_at_utc     timestamptz,
            PRIMARY KEY (tenant_id, statement_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_bank_statements_dealer ON auditcore.p2_bank_statements "
        "(tenant_id, dealer_id, status, period_from, period_to)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_bank_statement_txns (
            tenant_id          varchar(128)  NOT NULL,
            statement_id       uuid          NOT NULL,
            row_no             integer       NOT NULL,
            txn_date           date,
            value_date         date,
            narration          varchar(1000) NOT NULL DEFAULT '',
            reference          varchar(80),
            cheque_number      varchar(40),
            bank_txn_id        varchar(80),
            amount             numeric(16,2) NOT NULL,
            direction          varchar(2)    NOT NULL CHECK (direction IN ('CR', 'DR')),
            balance            numeric(16,2),
            credit_kind        varchar(12),
            outcome            varchar(16),
            match_basis        varchar(24),
            receipt_keys       jsonb         NOT NULL DEFAULT '[]'::jsonb,
            journey_id         uuid,
            named_journey_id   uuid,
            duplicate_of       varchar(80),
            PRIMARY KEY (tenant_id, statement_id, row_no)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_bank_statement_txns_journey ON auditcore.p2_bank_statement_txns "
        "(tenant_id, journey_id) WHERE journey_id IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_bank_receipt_matches (
            tenant_id          varchar(128)  NOT NULL,
            journey_id         uuid          NOT NULL,
            receipt_key        varchar(160)  NOT NULL,
            payment_id         uuid,
            dealer_id          uuid          NOT NULL,
            outlet_id          uuid,
            source             varchar(10)   NOT NULL CHECK (source IN ('RECEIPT', 'LEDGER')),
            receipt_date       date,
            amount             numeric(16,2) NOT NULL,
            mode               varchar(40),
            receipt_number     varchar(120),
            reference          varchar(200),
            outcome            varchar(16)   NOT NULL
                                 CHECK (outcome IN ('MATCHED', 'AMBIGUOUS', 'NOT_FOUND', 'OUTSIDE_DATES', 'CASH', 'CARD')),
            match_basis        varchar(24),
            credits            jsonb         NOT NULL DEFAULT '[]'::jsonb,
            note               varchar(400),
            is_current         boolean       NOT NULL DEFAULT true,
            reconciled_at_utc  timestamptz   NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, journey_id, receipt_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_bank_receipt_matches_dealer ON auditcore.p2_bank_receipt_matches "
        "(tenant_id, dealer_id, receipt_date) WHERE is_current"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_bank_receipt_matches_payment ON auditcore.p2_bank_receipt_matches "
        "(tenant_id, payment_id) WHERE payment_id IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_journey_bank_status (
            tenant_id          varchar(128)  NOT NULL,
            journey_id         uuid          NOT NULL,
            dealer_id          uuid          NOT NULL,
            status             varchar(16)   NOT NULL
                                 CHECK (status IN ('BANK_VERIFIED', 'PARTIAL', 'NOT_VERIFIED', 'CASH_ONLY')),
            receipts           integer       NOT NULL DEFAULT 0,
            bank_receipts      numeric(16,2) NOT NULL DEFAULT 0,
            verified           numeric(16,2) NOT NULL DEFAULT 0,
            left_to_verify     numeric(16,2) NOT NULL DEFAULT 0,
            cash               numeric(16,2) NOT NULL DEFAULT 0,
            bank_verified_at   timestamptz,
            updated_at_utc     timestamptz   NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, journey_id)
        )
        """
    )
    for table in _TABLES:
        _secure(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS auditcore.{table}")
