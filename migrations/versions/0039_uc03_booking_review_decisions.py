"""Add reference-only UC03 Booking Review exception decisions.

Revision ID: 0039_uc03_review_decisions
Revises: 0038_uc03_tentative_sku
Create Date: 2026-08-30

The decision ledger stores only DI source references and reviewer decisions. Raw
extracted values, confidence, page numbers, bounding regions and document content
remain owned by Document Intelligence.
"""

from alembic import op

revision = "0039_uc03_review_decisions"
down_revision = "0038_uc03_tentative_sku"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auditcore.journey_attribute_review_decisions (
            tenant_id                  varchar(128) NOT NULL,
            review_decision_id         uuid NOT NULL DEFAULT gen_random_uuid(),
            journey_id                 uuid NOT NULL,
            stage_code                 varchar(30) NOT NULL DEFAULT 'BOOKING'
                                       CHECK (stage_code IN ('BOOKING')),
            review_key                 varchar(240) NOT NULL,
            review_kind                varchar(20) NOT NULL
                                       CHECK (review_kind IN ('ATTRIBUTE','RAW_FIELD')),
            decision                   varchar(20) NOT NULL
                                       CHECK (decision IN ('ACCEPTED','REJECTED')),
            source_set_ref             text NOT NULL,
            source_di_document_id      uuid NOT NULL,
            source_canonical_field_id  varchar(160),
            source_field_key           varchar(160) NOT NULL,
            source_fact_version        integer NOT NULL CHECK (source_fact_version > 0),
            decided_by_actor_id        varchar(160) NOT NULL,
            decided_at_utc             timestamptz NOT NULL DEFAULT now(),
            created_at_utc             timestamptz NOT NULL DEFAULT now(),
            updated_at_utc             timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, review_decision_id),
            UNIQUE (tenant_id, journey_id, stage_code, review_key),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_journey_attribute_review_decisions_journey
        ON auditcore.journey_attribute_review_decisions
            (tenant_id, journey_id, stage_code, review_key)
        """
    )
    op.execute(
        "ALTER TABLE auditcore.journey_attribute_review_decisions ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE auditcore.journey_attribute_review_decisions FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        """
        CREATE POLICY tenant_isolation_journey_attribute_review_decisions
        ON auditcore.journey_attribute_review_decisions
        USING (tenant_id = auditcore.current_tenant_id())
        WITH CHECK (tenant_id = auditcore.current_tenant_id())
        """
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON "
        f"auditcore.journey_attribute_review_decisions TO {_RUNTIME_ROLE}"
    )
    op.execute(
        f"REVOKE DELETE ON auditcore.journey_attribute_review_decisions FROM {_RUNTIME_ROLE}"
    )
    op.execute(
        """
        COMMENT ON TABLE auditcore.journey_attribute_review_decisions IS
        'Reference-only human decisions for UC03 Booking Review exceptions. Raw DI values, confidence, page and bounding regions remain in DI.'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN auditcore.journey_attribute_review_decisions.source_set_ref IS
        'Deterministic reference set of DI document/canonical-field/field-key/fact-version metadata used to invalidate stale decisions without copying raw values.'
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.journey_attribute_review_decisions")
