"""0136_uc03_price_since — every price row remembers since when its figure holds.

A mid-month price upload carries only the models that changed; the new
version is still complete (the other models are carried forward at their
standing prices) and every row keeps its own price-since date (decision
2026-09-30). Existing rows date from their version.
"""
from __future__ import annotations

from alembic import op

revision = "0136_uc03_price_since"
down_revision = "0135_uc03_p2_insurance_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.price_list_items ADD COLUMN price_since date")
    # The backfill dates existing rows from their version; the guard that
    # keeps published versions immutable is stepped around for it alone.
    op.execute("ALTER TABLE auditcore.price_list_items DISABLE TRIGGER USER")
    op.execute(
        """
        UPDATE auditcore.price_list_items pli
        SET price_since = plv.effective_from
        FROM auditcore.price_list_versions plv
        WHERE plv.tenant_id = pli.tenant_id AND plv.price_list_version_id = pli.price_list_version_id
        """
    )
    op.execute("ALTER TABLE auditcore.price_list_items ENABLE TRIGGER USER")


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.price_list_items DROP COLUMN price_since")
