"""0154_sku_run_checked_by -- an "Apply" SKU run is linked to the "Check" run it applies.

Revision ID: 0154_sku_run_checked_by
Revises: 0152_sku_match_basis
Create Date: 2026-10-21

The product owner reviews and applies SKU changes from Administration > Run SKU
Resolution. Applying (RERESOLVE_COMMIT) is allowed only after a complete check
(RERESOLVE_DRY_RUN) of the Project, and the apply run keeps the check run's id
here so the page and the logs show which check was applied.
"""
from __future__ import annotations

from alembic import op

revision = "0154_sku_run_checked_by"
down_revision = "0152_sku_match_basis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.sku_resolution_runs ADD COLUMN IF NOT EXISTS checked_by_run_id uuid")


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.sku_resolution_runs DROP COLUMN IF EXISTS checked_by_run_id")
