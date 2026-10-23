"""0158_p2_journey_deletions — the permanent record of a journey a Team Lead deleted
(product owner, 2026-10-23).

A Team Lead may delete a journey that no reviewer has yet marked reviewed and
that is not closed. The journey's rows, its Document Intelligence documents and
its R2 files are removed, so this table is what remains: who deleted which
journey, when, and why, with the journey's basic details as they stood.

No foreign keys: the journey it describes is gone by design. Rows are never
updated except to finish the deletion (status, counts, completed time) and
never deleted by the runtime role. Additive; idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0158_p2_journey_deletions"
down_revision = "0157_corporate_registry_entries"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLE = "p2_journey_deletions"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_TABLE} (
            deletion_id           uuid         NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
            tenant_id             varchar(128) NOT NULL,
            journey_id            uuid         NOT NULL,
            journey_reference     varchar(200),
            customer_name         varchar(400),
            dealer_id             uuid,
            dealer_name           varchar(400),
            outlet_id             uuid,
            outlet_name           varchar(400),
            outlet_code           varchar(100),
            pc_actor_id           varchar(200),
            pc_name               varchar(400),
            vehicle               varchar(600),
            current_stage         varchar(100),
            journey_created_at_utc timestamptz,
            document_count        integer      NOT NULL DEFAULT 0,
            reason                text         NOT NULL CHECK (length(btrim(reason)) >= 10),
            deleted_by_actor_id   varchar(200) NOT NULL,
            deleted_by_role       varchar(50)  NOT NULL,
            deletion_status       varchar(20)  NOT NULL DEFAULT 'STARTED'
                                  CHECK (deletion_status IN ('STARTED', 'COMPLETED', 'FAILED')),
            di_documents_deleted  integer      NOT NULL DEFAULT 0,
            r2_objects_deleted    integer      NOT NULL DEFAULT 0,
            failure_detail        text,
            correlation_id        varchar(200),
            requested_at_utc      timestamptz  NOT NULL DEFAULT now(),
            completed_at_utc      timestamptz
        )
        """
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_time "
        f"ON auditcore.{_TABLE} (tenant_id, requested_at_utc DESC)"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_journey ON auditcore.{_TABLE} (tenant_id, journey_id)"
    )
    op.execute(f"ALTER TABLE auditcore.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON auditcore.{_TABLE}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_TABLE} ON auditcore.{_TABLE} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT ON auditcore.{_TABLE} TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE ON auditcore.{_TABLE} FROM {_RUNTIME_ROLE}")
    # Finishing a deletion records its outcome; nothing else may change.
    op.execute(
        f"GRANT UPDATE (deletion_status, di_documents_deleted, r2_objects_deleted, "
        f"failure_detail, completed_at_utc) ON auditcore.{_TABLE} TO {_RUNTIME_ROLE}"
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS auditcore.{_TABLE}")
