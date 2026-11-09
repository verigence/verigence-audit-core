"""Every foreign key that a journey delete has to check is indexed (2026-11-09).

ADI-RD-261022-026 could not be deleted: removing its 116 evidence rows made Postgres check
journey_document_extracted_fields (520k rows) for references with no index on
(tenant_id, evidence_id), a full scan per row, and the delete hit the 10 s statement timeout.
Migration 0208 indexed every such key. A new table with a foreign key to one of these tables needs
an index that starts with the key's columns, or deleting a journey slows down again.
"""
from __future__ import annotations

from p2_support import database_engine
from sqlalchemy import text

# Tables the journey delete removes rows from.
DELETED_WITH_A_JOURNEY = (
    "journeys", "evidence", "evidence_facts", "audit_findings", "audit_evaluations", "p2_tasks",
    "workflow_tasks", "workflow_instances", "journey_document_requirements", "deliveries",
    "payments", "customers", "bank_statement_lines",
)

_UNINDEXED = text("""
    WITH fk AS (
        SELECT c.conname, c.conrelid, c.confrelid, c.conkey
        FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
        WHERE c.contype = 'f' AND n.nspname = 'auditcore'
    )
    SELECT fk.conrelid::regclass::text AS child, fk.conname
    FROM fk JOIN pg_class parent ON parent.oid = fk.confrelid
    WHERE parent.relname = ANY(:parents)
      AND NOT EXISTS (
          SELECT 1 FROM pg_index i
          WHERE i.indrelid = fk.conrelid AND i.indisvalid
            AND (string_to_array(i.indkey::text, ' ')::int2[])[1:array_length(fk.conkey, 1)] @> fk.conkey
            AND (string_to_array(i.indkey::text, ' ')::int2[])[1:array_length(fk.conkey, 1)] <@ fk.conkey
      )
    ORDER BY 1, 2
""")


def test_every_foreign_key_a_journey_delete_checks_is_indexed():
    engine = database_engine()
    try:
        with engine.connect() as connection:
            missing = connection.execute(_UNINDEXED, {"parents": list(DELETED_WITH_A_JOURNEY)}).all()
    finally:
        engine.dispose()
    assert missing == [], (
        "Add an index starting with the foreign key's columns (see migration 0208): "
        + ", ".join(f"{child}.{name}" for child, name in missing)
    )
