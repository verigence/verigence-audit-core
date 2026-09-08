"""UC03 AUTOMATED_SYNC_FAILURE finding type.

Revision ID: 0067_uc03_automated_sync_failure
Revises: 0066_uc03_bank_recon
Create Date: 2026-09-08

Both SKU resolution (against the OEM price masters) and payment reconciliation
(receipts vs the bank statement) run automatically, inline, whenever the
document that feeds them is confirmed by DI -- never gated on PC Verify/
Submit. Both producers are already best-effort and never raise; on an
unexpected internal error they return ``{"error": True}`` instead of the
"nothing to do yet" / "0 or many matches" outcomes they already flag through
their own finding types (MODEL_NOT_IDENTIFIED, PAYMENT_UNVERIFIED).

Without this, a persistent automation failure (a code bug, a bad row) would
leave the Deal or Payments panel silently blank forever with nothing telling
the PC why. ``AUTOMATED_SYNC_FAILURE`` surfaces that instead, and auto-resolves
the next time the same automatic step succeeds.

  finding_class = DATA_GAP  (owner PC, self-serve)

Producer: ``audit_core.uc03_async_sync_tasks`` wraps ``sync_model_resolution``
and ``reconcile_payments`` -- idempotent, self-heals on read, never raises.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0067_uc03_automated_sync_failure"
down_revision = "0066_uc03_bank_recon"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        text(
            """
            INSERT INTO auditcore.finding_types
                (finding_type_code, finding_class, default_owner_role, resolution_mode,
                 status, description)
            VALUES (
                'AUTOMATED_SYNC_FAILURE', 'DATA_GAP', 'PC', 'SELF_SERVICE', 'ACTIVE',
                'An automatic background step (SKU resolution against the price '
                'masters, or payment reconciliation against the bank statement) hit '
                'an unexpected error and could not complete. This does not block the '
                'journey, but a PC needs to check the affected panel directly.'
            )
            ON CONFLICT (finding_type_code) DO UPDATE SET
                finding_class = 'DATA_GAP',
                default_owner_role = 'PC',
                resolution_mode = 'SELF_SERVICE',
                status = 'ACTIVE',
                description = EXCLUDED.description,
                updated_at_utc = now()
            """
        )
    )


def downgrade() -> None:
    op.get_bind().execute(
        text(
            "DELETE FROM auditcore.finding_types "
            "WHERE finding_type_code = 'AUTOMATED_SYNC_FAILURE'"
        )
    )
