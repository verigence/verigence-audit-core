"""Per-source breakdown of commercial-line and discount actual values.

Revision ID: 0101_deal_source_values
Revises: 0100_uc03_identity_check_hold
Create Date: 2026-09-16

``commercial_lines.actual_amount`` and ``discount_applications.
actual_discount_amount`` are each a single current-best-value row per
(journey, component): when a higher-priority document arrives (e.g. a
retail invoice superseding a booking form), the existing value is
overwritten in place and only ``source_reference`` / ``details`` records
which document currently backs it. That is deliberate and stays exactly as
it is -- every rule, SLA and compliance check that reads these tables keeps
reading one canonical value.

A real user report asked for a different, additive thing on the Journey
Line comparison UI: when the booking form and a later invoice disagree on
the same component (e.g. Accessories Cost: booking form said one amount,
the invoice says another), show both, not just whichever currently wins.
This table is purely that -- one row per (journey, component, source
document type), written alongside the existing canonical upsert, never
read by anything that needs a single value.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0101_deal_source_values"
down_revision = "0100_uc03_identity_check_hold"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            CREATE TABLE auditcore.commercial_line_source_values (
                tenant_id               varchar(128) NOT NULL,
                journey_id              uuid NOT NULL,
                line_kind               varchar(20) NOT NULL
                                        CHECK (line_kind IN ('COMMERCIAL', 'DISCOUNT')),
                component_key           varchar(120) NOT NULL,
                source_document_type    varchar(60) NOT NULL,
                amount                  numeric(18,2) NOT NULL,
                source_evidence_id      uuid,
                source_document_id      uuid,
                created_at_utc          timestamptz NOT NULL DEFAULT now(),
                updated_at_utc          timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (tenant_id, journey_id, line_kind, component_key, source_document_type),
                FOREIGN KEY (tenant_id, journey_id)
                    REFERENCES auditcore.journeys(tenant_id, journey_id),
                FOREIGN KEY (tenant_id, source_evidence_id)
                    REFERENCES auditcore.evidence(tenant_id, evidence_id)
            )
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX ix_commercial_line_source_values_lookup
            ON auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind, component_key)
            """
        )
    )
    # Tenant-scoped like every other per-journey table -- RLS + the runtime
    # role's grants, or every read/write from the API (which runs as
    # audit_core_runtime, not the migration's own role) fails with a
    # permission error the moment this table is touched.
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.commercial_line_source_values ENABLE ROW LEVEL SECURITY
            """
        )
    )
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.commercial_line_source_values FORCE ROW LEVEL SECURITY
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE POLICY tenant_isolation_commercial_line_source_values
            ON auditcore.commercial_line_source_values
            USING (tenant_id = auditcore.current_tenant_id())
            WITH CHECK (tenant_id = auditcore.current_tenant_id())
            """
        )
    )
    conn.execute(
        text(
            f"""
            GRANT SELECT, INSERT, UPDATE ON auditcore.commercial_line_source_values
            TO {_RUNTIME_ROLE}
            """
        )
    )
    conn.execute(
        text(
            f"""
            REVOKE DELETE ON auditcore.commercial_line_source_values FROM {_RUNTIME_ROLE}
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP INDEX IF EXISTS auditcore.ix_commercial_line_source_values_lookup"))
    conn.execute(text("DROP TABLE IF EXISTS auditcore.commercial_line_source_values"))
