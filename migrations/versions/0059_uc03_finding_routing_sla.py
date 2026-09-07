"""UC03 audit finding routing: finding class, owner role, SLA due, disposition.

Revision ID: 0059
Revises: 0058
Create Date: 2026-09-07

Every audit finding is classified as DATA_GAP / DOCUMENT_GAP (PC self-serve) or
VIOLATION (TL/PM Accept or Reject). The class fixes the starting owner role and an
SLA due time; when a finding passes its SLA it is not reassigned — its escalation
level (derived at read time) rises so the next role up the ladder sees it.

This migration adds the columns and backfills every still-open finding using the
same classifier the application uses (imported from audit_core.uc03_finding_routing)
and the built-in default SLA table.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        text(
            """
            ALTER TABLE auditcore.audit_findings
                ADD COLUMN IF NOT EXISTS finding_class   varchar(30),
                ADD COLUMN IF NOT EXISTS owner_role_code  varchar(80),
                ADD COLUMN IF NOT EXISTS sla_due_at_utc   timestamptz,
                ADD COLUMN IF NOT EXISTS disposition      varchar(30)
            """
        )
    )
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.audit_findings
                DROP CONSTRAINT IF EXISTS ck_audit_findings_finding_class
            """
        )
    )
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.audit_findings
                ADD CONSTRAINT ck_audit_findings_finding_class
                CHECK (finding_class IS NULL OR finding_class IN
                       ('DATA_GAP','DOCUMENT_GAP','VIOLATION'))
            """
        )
    )
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.audit_findings
                DROP CONSTRAINT IF EXISTS ck_audit_findings_disposition
            """
        )
    )
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.audit_findings
                ADD CONSTRAINT ck_audit_findings_disposition
                CHECK (disposition IS NULL OR disposition IN
                       ('FIXED','CONFIRMED_BREACH','NOT_A_BREACH'))
            """
        )
    )

    # Queue index: still-open findings ordered by SLA due.
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_audit_findings_open_sla
            ON auditcore.audit_findings (tenant_id, sla_due_at_utc)
            WHERE finding_status IN ('OPEN','ACKNOWLEDGED')
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_audit_findings_owner_role
            ON auditcore.audit_findings (tenant_id, owner_role_code, finding_status)
            """
        )
    )

    # ── Backfill still-open findings (set-based; mirrors uc03_finding_routing) ───
    conn.execute(
        text(
            """
            WITH classified AS (
                SELECT
                    tenant_id,
                    audit_finding_id,
                    severity,
                    created_at_utc,
                    CASE
                        WHEN split_part(coalesce(rule_key,''), ':', 1) = ANY(:doc_gap_rules)
                            THEN 'DOCUMENT_GAP'
                        WHEN split_part(coalesce(rule_key,''), ':', 1) = ANY(:data_gap_rules)
                            THEN 'DATA_GAP'
                        WHEN split_part(coalesce(rule_key,''), ':', 1) = ANY(:violation_rules)
                            THEN 'VIOLATION'
                        WHEN rule_key ~ '^RE_.*_MISSING$'
                            THEN 'DOCUMENT_GAP'
                        WHEN rule_key ~ '^RE_'
                            THEN 'VIOLATION'
                        WHEN upper(coalesce(finding_type_code,'')) = ANY(:doc_gap_types)
                            THEN 'DOCUMENT_GAP'
                        WHEN upper(coalesce(finding_type_code,'')) = ANY(:data_gap_types)
                            THEN 'DATA_GAP'
                        WHEN upper(coalesce(finding_type_code,'')) = ANY(:violation_types)
                            THEN 'VIOLATION'
                        ELSE 'VIOLATION'
                    END AS finding_class
                FROM auditcore.audit_findings
                WHERE finding_status IN ('OPEN','ACKNOWLEDGED')
                  AND finding_class IS NULL
            )
            UPDATE auditcore.audit_findings f
            SET finding_class  = c.finding_class,
                owner_role_code = CASE WHEN c.finding_class = 'VIOLATION' THEN 'TL' ELSE 'PC' END,
                sla_due_at_utc = c.created_at_utc + make_interval(hours =>
                    CASE
                      WHEN c.finding_class = 'VIOLATION' THEN
                        CASE upper(coalesce(c.severity,'MEDIUM'))
                          WHEN 'CRITICAL' THEN 8 WHEN 'HIGH' THEN 24 WHEN 'MEDIUM' THEN 48
                          WHEN 'LOW' THEN 96 WHEN 'INFO' THEN 120 ELSE 48 END
                      ELSE
                        CASE upper(coalesce(c.severity,'MEDIUM'))
                          WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 8 WHEN 'MEDIUM' THEN 24
                          WHEN 'LOW' THEN 48 WHEN 'INFO' THEN 72 ELSE 24 END
                    END)
            FROM classified c
            WHERE f.tenant_id = c.tenant_id
              AND f.audit_finding_id = c.audit_finding_id
            """
        ),
        {
            "doc_gap_rules": [
                "BK_DOCKET_PRESENT", "BK_PAN_PRESENT", "BK_MIN_BOOKING_PROOF_PRESENT",
                "BK_CONDITIONAL_DOCS_ADDRESSED", "BK_REQUIRED_CAPTURE_COMPLETE",
                "DOC_REQUIRED_ANSWER_NO", "DL_V2_REQUIRED_DOCUMENT_MISSING",
                "DL_V2_DOCUMENT_PROCESSING_FAILED",
            ],
            "data_gap_rules": ["DL_NOT_INTIMATED", "PAY_UNVERIFIED_RECEIPT"],
            "violation_rules": [
                "DL_VIN_RECONCILIATION", "WF_BOOKING_INCOMPLETE_AT_DELIVERY_START",
                "WF_DELIVERY_COMPLETED_WITH_AUDIT_INCOMPLETE",
            ],
            "doc_gap_types": [
                "DOCUMENT_EXCEPTION", "DELIVERY_DOCUMENT_MISSING", "REQUIRED_DOCUMENT_ANSWER_NO",
            ],
            "data_gap_types": ["PAYMENT_EXCEPTION", "PAYMENT_UNVERIFIED", "DELIVERY_NOT_INTIMATED"],
            "violation_types": [
                "VIN_RECONCILIATION_MISMATCH", "DELIVERY_COMPLETED_WITH_AUDIT_INCOMPLETE",
                "BOOKING_PREREQUISITES_INCOMPLETE_AT_DELIVERY", "COMMERCIAL_EXCEPTION",
                "PROCESS_NON_COMPLIANCE", "CUSTOMER_IDENTITY_CONCERN", "PHYSICAL_OBSERVATION",
                "DELIVERY_EXCEPTION", "PRICING_ANOMALY", "DISCOUNT_ANOMALY", "ACCESSORY_ANOMALY",
                "INSURANCE_ANOMALY", "RTO_ANOMALY", "VEHICLE_IDENTITY_ANOMALY",
                "CROSS_CASE_DUPLICATE", "RULE_ENGINE_ANOMALY",
            ],
        },
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP INDEX IF EXISTS auditcore.ix_audit_findings_owner_role"))
    conn.execute(text("DROP INDEX IF EXISTS auditcore.ix_audit_findings_open_sla"))
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.audit_findings
                DROP CONSTRAINT IF EXISTS ck_audit_findings_disposition,
                DROP CONSTRAINT IF EXISTS ck_audit_findings_finding_class,
                DROP COLUMN IF EXISTS disposition,
                DROP COLUMN IF EXISTS sla_due_at_utc,
                DROP COLUMN IF EXISTS owner_role_code,
                DROP COLUMN IF EXISTS finding_class
            """
        )
    )
