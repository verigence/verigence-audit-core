"""Add typed Audit Core owners for Booking Docket-only reviewed fields.

Revision ID: 0053
Revises: 0052_uc03_final_source
Create Date: 2026-09-04

Booking Form and Booking Docket are both UC03 Booking sales-contract evidence.
Every accepted reviewed business value must therefore land in explicit typed
Audit Core storage.  These three Docket-only fields were the remaining gaps in
``booking_form_review_values``.
"""
import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0052_uc03_final_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "booking_form_review_values",
        sa.Column("deal_type", sa.Text(), nullable=True),
        schema="auditcore",
    )
    op.add_column(
        "booking_form_review_values",
        sa.Column("out_of_scope_reasons", sa.Text(), nullable=True),
        schema="auditcore",
    )
    op.add_column(
        "booking_form_review_values",
        sa.Column("dsa_commission_amount", sa.Numeric(18, 2), nullable=True),
        schema="auditcore",
    )


def downgrade() -> None:
    op.drop_column(
        "booking_form_review_values",
        "dsa_commission_amount",
        schema="auditcore",
    )
    op.drop_column(
        "booking_form_review_values",
        "out_of_scope_reasons",
        schema="auditcore",
    )
    op.drop_column(
        "booking_form_review_values",
        "deal_type",
        schema="auditcore",
    )
