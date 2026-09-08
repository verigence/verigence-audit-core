"""UC03 MODEL_NOT_IDENTIFIED finding type.

Revision ID: 0064_uc03_model_not_identified
Revises: 0063
Create Date: 2026-09-08

When a booking's reviewed model / total / ex-showroom price cannot be matched
to exactly one SKU in the effective OEM price list, the journey gets one
``MODEL_NOT_IDENTIFIED`` finding — "no matching model" or "matched multiple
models" — for the PC to fix (the PC sets the right model, escalating through
the TL flow). Once a single SKU resolves, the finding auto-resolves and the
per-journey price/discount standards are materialised.

  finding_class = DATA_GAP  (owner PC, self-serve)

Producer: ``audit_core.uc03_model_resolution.sync_model_resolution`` — mirrors
the MANUAL_VERIFICATION producer (0063): idempotent, self-heals on read, never
raises.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0064_uc03_model_not_identified"
down_revision = "0063"
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
                'MODEL_NOT_IDENTIFIED', 'DATA_GAP', 'PC', 'SELF_SERVICE', 'ACTIVE',
                'The booking model could not be matched to exactly one SKU in the '
                'effective OEM price list (no match, or more than one). The PC must '
                'confirm the vehicle model so the deal can be checked against the '
                'price and discount masters.'
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
            "WHERE finding_type_code = 'MODEL_NOT_IDENTIFIED'"
        )
    )
