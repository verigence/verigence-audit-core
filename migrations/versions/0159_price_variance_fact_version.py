"""0159_price_variance_fact_version — the Journey list's cached price variance
knows when it is stale (product owner, 2026-10-24).

``p2_journey_runtime.price_variance`` is a cached copy of the Deal tab's own
``currentVsStandard``. It was only ever written by the stage recompute and by a
one-time backfill of NULLs, so any other change to a deal -- a SKU pinned by a
resolution run, a pricing date moved, an invoice materialised -- left the copy
behind while Journey 360 recomputed live. The two then disagreed: NIRUPAMA
KHUNTIA showed +2,71,181 on the list and -9,407 on the Deal tab, from the same
field.

Chasing every write path would miss the next one. Instead the cache records the
``fact_version`` it was computed at, exactly as ``p2_control_state`` records
``evaluated_fact_version``: every fact change already bumps that version
through ``note_facts_changed``, so a stale copy is one integer comparison away
and no future write path can hide from it.

Additive and idempotent. Existing rows get NULL, which reads as stale.
"""
from __future__ import annotations

from alembic import op

revision = "0159_price_variance_fact_version"
down_revision = "0158_p2_journey_deletions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.p2_journey_runtime "
        "ADD COLUMN IF NOT EXISTS price_variance_fact_version bigint"
    )
    # The sweep looks for rows whose cached variance is older than the facts.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_journey_runtime_price_variance_stale "
        "ON auditcore.p2_journey_runtime (tenant_id) "
        "WHERE price_variance IS NULL "
        "   OR price_variance_fact_version IS NULL "
        "   OR price_variance_fact_version <> fact_version"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.ix_p2_journey_runtime_price_variance_stale")
    op.execute(
        "ALTER TABLE auditcore.p2_journey_runtime DROP COLUMN IF EXISTS price_variance_fact_version"
    )
