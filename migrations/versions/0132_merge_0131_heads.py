"""Merge the two 0131 heads.

0131_uc03_p2_journey_pc_name (Phase 2: the PC's name on a journey) and
0131_onboarding_workbook (Excel-templated onboarding) were written on
separate branches from 0130 and both reached dev, which left alembic with
two heads and stopped every deployment at ``alembic upgrade head``. This
empty merge revision gives the chain one head again; each database gets
whichever of the two it is still missing.

Revision ID: 0132_merge_0131_heads
Revises: 0131_uc03_p2_journey_pc_name, 0131_onboarding_workbook
"""

from __future__ import annotations

revision = "0132_merge_0131_heads"
down_revision = ("0131_uc03_p2_journey_pc_name", "0131_onboarding_workbook")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
