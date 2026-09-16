"""Hold a document's fields out of materialization pending identity review.

Revision ID: 0100_uc03_identity_check_hold
Revises: 0099_unified_task_queue
Create Date: 2026-09-16

Closes a real gap: ``uc03_customer_identity_consistency.py`` already raises
a ``WRONG_DOCUMENT`` finding when a document's extracted name doesn't match
the Journey's KYC name (or when no KYC document has been extracted yet to
check against at all) -- but until now that was purely advisory. The
document's extracted fields still materialized into ``journey_products``,
``commercial_lines``, and every other canonical owner exactly as if nothing
were wrong, with no way to keep a wrong customer's paperwork from actually
being used before a Team Lead has looked at it.

``evidence.identity_check_status`` is the new hold flag:

- ``PASSED`` (default): either the name matched, this document type carries
  no name to check, or it's the KYC document itself (nothing to check it
  against). Materialization treats it exactly as before.
- ``HELD``: a name check couldn't yet clear this document -- either it
  mismatches the KYC name (a WRONG_DOCUMENT finding is open, pending a
  Team Lead's Confirm Breach / Mark False Positive verdict) or no KYC
  document has been extracted on this Journey yet to check against. The
  document stays visibly "Received" (this is not a delete), but its
  fields never win precedence in materialization while held.
- ``REJECTED``: a Team Lead confirmed via Confirm Breach that this really
  is the wrong customer's document. Set together with
  ``association_status='VOIDED'`` (soft-delete -- the row and its audit
  trail survive, but it is no longer the active evidence for its
  requirement, exactly like the existing supersede-on-reupload pattern).

Scoped to the customer-name check only (``WRONG_DOCUMENT:{document_id}``,
i.e. `rule_key` with no additional segment); the sibling receipt-vs-dealer
check (``WRONG_DOCUMENT:DEALER:{document_id}``) is a different, narrower
question (the wrong dealership's receipt, not the wrong customer's
document) and is intentionally left with its existing finding-only
behaviour.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0100_uc03_identity_check_hold"
down_revision = "0099_unified_task_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.evidence
            ADD COLUMN identity_check_status varchar(20) NOT NULL DEFAULT 'PASSED'
                CHECK (identity_check_status IN ('PASSED', 'HELD', 'REJECTED'))
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX ix_evidence_identity_check_status
            ON auditcore.evidence (tenant_id, journey_id, identity_check_status)
            WHERE identity_check_status <> 'PASSED'
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP INDEX IF EXISTS auditcore.ix_evidence_identity_check_status"))
    conn.execute(text("ALTER TABLE auditcore.evidence DROP COLUMN IF EXISTS identity_check_status"))
