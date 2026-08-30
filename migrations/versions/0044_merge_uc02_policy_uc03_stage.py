"""Merge UC02 dealer-policy and UC03 booking-stage migration heads.

Revision ID: 0044_merge_uc02_policy_uc03
Revises: 0041_uc02_dealer_policy, 0043_uc03_booking_stage_link
Create Date: 2026-08-30

This is a lineage-only Alembic merge revision. Both parent migrations remain
independent and unchanged; no schema or business data is modified here.
"""

revision = "0044_merge_uc02_policy_uc03"
down_revision = (
    "0041_uc02_dealer_policy",
    "0043_uc03_booking_stage_link",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
