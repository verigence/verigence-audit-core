"""Model-selection correction flow: PC proposes, TL confirms/rejects.

Revision ID: 0102_sku_corrections
Revises: 0101_deal_source_values
Create Date: 2026-09-16

A real, confirmed gap: ``uc03_model_resolution._pin_sku`` (used by both the
automatic resolver and the PC's own manual "confirm SKU" picker) refuses to
overwrite a ``journey_products`` row whose ``selection_status`` is already
'CONFIRMED' -- intentional, so a routine automatic re-run can never silently
flip a locked-in deal, but it leaves no path at all to correct a SKU that
was confirmed wrong (whether by the resolver's own now-fixed matching bugs,
or by a document-reader misread of the model/variant text in the first
place).

Mirrors ``journey_document_field_correction_proposals``'s own shape and
its PC-proposes / TL-Confirm-Breach-applies / TL-Mark-False-Positive-
rejects flow (``uc03_document_field_corrections.py``), adapted for a SKU
reassignment rather than a single field value: one row per proposal,
1:1 with the ``audit_findings`` row it raises, holding both the previous
and proposed SKU for a clean audit trail either way.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0102_sku_corrections"
down_revision = "0101_deal_source_values"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            CREATE TABLE auditcore.model_selection_correction_proposals (
                tenant_id                varchar(128) NOT NULL,
                audit_finding_id         uuid NOT NULL,
                journey_id               uuid NOT NULL,
                previous_product_sku_id  uuid NOT NULL REFERENCES auditcore.product_skus(product_sku_id),
                proposed_product_sku_id  uuid NOT NULL REFERENCES auditcore.product_skus(product_sku_id),
                reason                   text NOT NULL,
                proposed_by_actor_id     varchar(160) NOT NULL,
                applied_at_utc           timestamptz,
                created_at_utc           timestamptz NOT NULL DEFAULT now(),
                updated_at_utc           timestamptz NOT NULL DEFAULT now(),
                version_no               bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),

                PRIMARY KEY (tenant_id, audit_finding_id),
                FOREIGN KEY (tenant_id, audit_finding_id)
                    REFERENCES auditcore.audit_findings(tenant_id, audit_finding_id),
                FOREIGN KEY (tenant_id, journey_id)
                    REFERENCES auditcore.journeys(tenant_id, journey_id)
            )
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX ix_model_selection_correction_proposals_journey
                ON auditcore.model_selection_correction_proposals (tenant_id, journey_id, created_at_utc DESC)
            """
        )
    )
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.model_selection_correction_proposals ENABLE ROW LEVEL SECURITY
            """
        )
    )
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.model_selection_correction_proposals FORCE ROW LEVEL SECURITY
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE POLICY tenant_isolation_model_selection_correction_proposals
            ON auditcore.model_selection_correction_proposals
            USING (tenant_id = auditcore.current_tenant_id())
            WITH CHECK (tenant_id = auditcore.current_tenant_id())
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE TRIGGER trg_model_selection_correction_proposals_updated
                BEFORE UPDATE ON auditcore.model_selection_correction_proposals
                FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at()
            """
        )
    )
    conn.execute(
        text(
            f"""
            GRANT SELECT, INSERT, UPDATE ON auditcore.model_selection_correction_proposals
            TO {_RUNTIME_ROLE}
            """
        )
    )
    conn.execute(
        text(
            f"""
            REVOKE DELETE ON auditcore.model_selection_correction_proposals FROM {_RUNTIME_ROLE}
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP TABLE IF EXISTS auditcore.model_selection_correction_proposals CASCADE"))
