"""Add agent/intermediary, MISP, and add-ons columns to insurance_records.

Revision ID: 0076_insurance_agent_misp
Revises: 0075_uc03_finding_descriptions
Create Date: 2026-09-10

Business ask: capture Agent/Intermediary Details, Agent/Intermediary Code,
MISP (Motor Insurance Service Provider) code, and add-ons taken (zero
depreciation, engine protection, etc.) for a Delivery's insurance cover.

DI already extracts all four (verigence-di#... insurance_cover.py: three new
fields plus the pre-existing add_ons array), but insurance_records had no
typed home for any of them.
"""
from __future__ import annotations

from alembic import op

revision = "0076_insurance_agent_misp"
down_revision = "0075_uc03_finding_descriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.insurance_records
            ADD COLUMN IF NOT EXISTS agent_intermediary_name varchar(200),
            ADD COLUMN IF NOT EXISTS agent_intermediary_code varchar(80),
            ADD COLUMN IF NOT EXISTS misp_code varchar(80),
            ADD COLUMN IF NOT EXISTS add_ons jsonb
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.insurance_records
            DROP COLUMN IF EXISTS agent_intermediary_name,
            DROP COLUMN IF EXISTS agent_intermediary_code,
            DROP COLUMN IF EXISTS misp_code,
            DROP COLUMN IF EXISTS add_ons
        """
    )
