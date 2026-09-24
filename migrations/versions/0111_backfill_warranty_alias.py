"""Backfill: merge existing extended_warranty_amount commercial_lines rows
onto additional_warranty_amount.

Revision ID: 0111_backfill_warranty_alias
Revises: 0110_booking_minimal_doc_set
Create Date: 2026-09-24

The code fix (uc03_v2_review_materialization.py's _COMMERCIAL_LINE_FIELD_ALIAS,
same PR as this migration) only changes how a Booking Form is materialized
from now on -- it does nothing for a journey whose Booking Form was already
reviewed before this shipped, which already has its real value sitting
under the wrong component_key. Confirmed live: the Deal page still showed
the same duplicate empty "Extended Warranty Amount" row on an
already-processed journey immediately after the code fix deployed.

Two cases per (tenant_id, journey_id), same coalesce-preference as the code
fix (never let extended_warranty_amount clobber a real additional_warranty_
amount value):

1. Only extended_warranty_amount exists -- rename it in place (same row,
   same commercial_line_id/evidence linkage, just the correct key).
2. Both exist -- additional_warranty_amount already has its own value;
   drop the now-redundant extended_warranty_amount row.

Deliberately NOT touched: commercial_line_source_values (migration 0101).
It is an immutable per-source audit trail (DELETE is revoked for the
runtime role by design) -- a handful of historical "via Booking Form"
rows staying under the old key is a minor, cosmetic gap in that trail's
own componentKey grouping, not a reason to break its append-only
guarantee.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0111_backfill_warranty_alias"
down_revision = "0110_booking_minimal_doc_set"
branch_labels = None
depends_on = None

_OLD_KEY = "extended_warranty_amount"
_NEW_KEY = "additional_warranty_amount"


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Rename in place wherever additional_warranty_amount doesn't
    # already exist for that journey.
    bind.execute(
        text(
            """
            UPDATE auditcore.commercial_lines cl
            SET component_key = :new_key, updated_at_utc = now()
            WHERE cl.component_key = :old_key
              AND NOT EXISTS (
                  SELECT 1 FROM auditcore.commercial_lines cl2
                  WHERE cl2.tenant_id = cl.tenant_id
                    AND cl2.journey_id = cl.journey_id
                    AND cl2.component_key = :new_key
              )
            """
        ),
        {"old_key": _OLD_KEY, "new_key": _NEW_KEY},
    )

    # 2. Anything still under the old key at this point is a journey that
    # already had a real additional_warranty_amount row -- drop the
    # now-redundant duplicate.
    bind.execute(
        text("DELETE FROM auditcore.commercial_lines WHERE component_key = :old_key"),
        {"old_key": _OLD_KEY},
    )


def downgrade() -> None:
    # Not reversible -- which rows were renamed vs. deleted, and their
    # original values, is not recoverable from the post-migration state.
    # Matching this repo's own precedent (e.g. 0091, 0025) for backfills
    # that merge or remove mutable per-journey operational data.
    pass
