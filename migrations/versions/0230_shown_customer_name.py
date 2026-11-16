"""0230_shown_customer_name -- a customer's name as shown is never an id (closing list item 5, 2026-11-16).

A Journey's customer carries the Journey id as its entered name until a KYC
document names them (0058). Every screen and report showed
COALESCE(legal_name, display_name), so a Journey whose KYC was never read
showed a UUID as the customer (58 journeys on 15 Nov). auditcore.shown_name
gives the verified name, else the entered name when it is a name, else
"Name not read". The entered name itself is unchanged (it is immutable once
set, 0018 / 0185).
"""
from __future__ import annotations

from alembic import op

revision = "0230_shown_customer_name"
down_revision = "0229_document_waivers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION auditcore.shown_name(p_legal text, p_entered text)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        AS $$
            SELECT COALESCE(
                NULLIF(btrim(p_legal), ''),
                CASE
                    WHEN p_entered IS NULL OR btrim(p_entered) = '' THEN NULL
                    WHEN btrim(p_entered) ~* '^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$'
                        THEN NULL
                    WHEN btrim(p_entered) ~ '^[0-9 ]+$' THEN NULL
                    ELSE btrim(p_entered)
                END,
                'Name not read'
            )
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS auditcore.shown_name(text, text)")
