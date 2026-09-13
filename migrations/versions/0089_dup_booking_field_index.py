"""0089_dup_booking_field_index — index the cross-journey identity scan
DUPLICATE_BOOKING actually needs.

`uc03_duplicate_booking_detection.py::_candidate_journeys` scans
`journey_document_extracted_fields` filtered by `(tenant_id, field_key)`
across EVERY OTHER journey in the tenant (it explicitly excludes the
current journey_id, since the whole point is finding a different journey
that looks like the same customer). Every existing index on this table
leads with `(tenant_id, journey_id, ...)` -- built for "read one journey's
own fields", the shape every other caller needs. Neither helps this query
at all: Postgres has no way to use a journey_id-first index to answer
"every row for this tenant across many journeys, for just these 4 field
keys" and falls back to a sequential scan of the whole table for that
tenant, which only gets slower as more journeys/documents accumulate.

Very likely the cause of a live 13.5s OperationalError observed via Resync
(which re-runs this producer for every document) on a tenant with enough
accumulated test data for the scan to time out. Confirmed missing via
migration history (0031/0051/0056/0063 -- none add this shape); not yet
independently confirmed against a live query plan, since this only shows
up at real data volume.

Partial (`WHERE effective_value IS NOT NULL`) to match the query's own
filter and stay small; safe/additive, no functional change.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0089_dup_booking_field_index"
down_revision = "0088_rule_instrumented_flag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_journey_document_extracted_fields_tenant_field
            ON auditcore.journey_document_extracted_fields (tenant_id, field_key)
            WHERE effective_value IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text("DROP INDEX IF EXISTS auditcore.ix_journey_document_extracted_fields_tenant_field")
    )
