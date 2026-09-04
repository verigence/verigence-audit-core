"""Add typed Audit Core owners for reviewed Aadhaar address components.

Revision ID: 0054
Revises: 0053
Create Date: 2026-09-05

DI's Aadhaar v1.2 contract publishes pincode, state and district when those
components are explicitly identifiable on the document. Review Confirm is
fail-closed, so each accepted value needs a real Audit Core business owner rather
than only the generic provenance ledger.
"""
import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customer_identity_review_values",
        sa.Column("aadhaar_address_pincode", sa.String(length=32), nullable=True),
        schema="auditcore",
    )
    op.add_column(
        "customer_identity_review_values",
        sa.Column("aadhaar_address_state", sa.String(length=240), nullable=True),
        schema="auditcore",
    )
    op.add_column(
        "customer_identity_review_values",
        sa.Column("aadhaar_address_district", sa.String(length=240), nullable=True),
        schema="auditcore",
    )


def downgrade() -> None:
    op.drop_column(
        "customer_identity_review_values",
        "aadhaar_address_district",
        schema="auditcore",
    )
    op.drop_column(
        "customer_identity_review_values",
        "aadhaar_address_state",
        schema="auditcore",
    )
    op.drop_column(
        "customer_identity_review_values",
        "aadhaar_address_pincode",
        schema="auditcore",
    )
