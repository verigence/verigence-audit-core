"""0229_document_waivers -- one missing-documents task per journey, and the per-document answers (2026-11-15).

Product owner 2026-11-15: "missing docs should be one task not one for each". A journey has
ONE missing-documents task listing every document still missing. What a PC (or a TL / the
Reviewer) answers for one document of it -- "Document uploaded", "Dealer not provided",
"Temporary registration" (with its number), or "Document not required" (the PC's claim,
which the Reviewer accepts or rejects) -- is kept here, one row per journey and document:

- p2_document_waivers   journey, requirement key, the choice, its status (ACCEPTED counts as
                        received for the checklist; CLAIMED waits for the Reviewer; REJECTED is
                        back with the PC), the reason, who / when, the decision, the round.

Backfill: the answers given so far on the per-document tasks (closed with a choice, claims
accepted, waiting or rejected), so nothing already answered is asked again. The requirement
keys of the two new groups are mapped: a DMS or Tally vehicle invoice -> VEHICLE_INVOICE; a
gate pass or a delivery receipt -> GATE_PASS.
"""
from __future__ import annotations

from alembic import op

revision = "0229_document_waivers"
down_revision = "0228_my_documents_alert"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLE = "p2_document_waivers"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_TABLE} (
            tenant_id        varchar(128) NOT NULL,
            waiver_id        uuid NOT NULL DEFAULT gen_random_uuid(),
            journey_id       uuid NOT NULL,
            requirement_key  varchar(80) NOT NULL,
            label            varchar(200),
            stage            varchar(20),
            choice           varchar(80) NOT NULL,
            status           varchar(20) NOT NULL CHECK (status IN ('ACCEPTED', 'CLAIMED', 'REJECTED')),
            reason           text,
            claimed_by       varchar(160),
            claimed_role     varchar(40),
            claimed_at_utc   timestamptz NOT NULL DEFAULT now(),
            decided_by       varchar(160),
            decided_role     varchar(40),
            decided_at_utc   timestamptz,
            decision_reason  text,
            round            integer NOT NULL DEFAULT 1,
            source_task_id   uuid,
            created_at_utc   timestamptz NOT NULL DEFAULT now(),
            updated_at_utc   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, waiver_id),
            UNIQUE (tenant_id, journey_id, requirement_key),
            FOREIGN KEY (tenant_id, journey_id) REFERENCES auditcore.journeys (tenant_id, journey_id) ON DELETE CASCADE
        )
        """
    )
    # The answers already given on the per-document tasks.
    op.execute(
        f"""
        INSERT INTO auditcore.{_TABLE} (tenant_id, journey_id, requirement_key, label, stage, choice, status, reason,
                                        claimed_by, claimed_role, claimed_at_utc, decided_by, decided_role,
                                        decided_at_utc, decision_reason, round, source_task_id)
        SELECT DISTINCT ON (t.tenant_id, t.journey_id, key.requirement_key)
               t.tenant_id, t.journey_id, key.requirement_key, t.title, t.reference->>'stage',
               CASE WHEN r->>'outcome' LIKE 'NOT_MANDATORY_%' THEN 'Document not required to be uploaded'
                    ELSE r->>'choice' END,
               CASE WHEN r->>'outcome' = 'NOT_MANDATORY_CLAIMED' THEN 'CLAIMED'
                    WHEN r->>'outcome' = 'NOT_MANDATORY_REJECTED' THEN 'REJECTED'
                    ELSE 'ACCEPTED' END,
               COALESCE(r->>'claimReason', r->>'reason'),
               COALESCE(r->>'claimedBy', r->>'closedBy'), COALESCE(r->>'claimedRole', r->>'closedRole'),
               COALESCE((r->>'claimedAt')::timestamptz, (r->>'closedAt')::timestamptz, t.updated_at_utc),
               r->>'decidedBy', r->>'decidedRole', (r->>'decidedAt')::timestamptz, r->>'decisionReason',
               t.round_number, t.task_id
        FROM auditcore.p2_tasks t
        CROSS JOIN LATERAL (SELECT t.completion_result AS r) c
        CROSS JOIN LATERAL (SELECT CASE
                 WHEN t.reference->>'requirementKey' IN ('customer_invoice_dms', 'tax_invoice_tally') THEN 'VEHICLE_INVOICE'
                 WHEN t.reference->>'requirementKey' IN ('gate_pass', 'delivery_receipt') THEN 'GATE_PASS'
                 ELSE t.reference->>'requirementKey' END AS requirement_key) key
        WHERE t.task_type = 'DOCUMENT_MISSING' AND t.source_code = 'DOCUMENT_MISSING'
          AND key.requirement_key IS NOT NULL
          AND ((t.task_status = 'VERIFIED_COMPLETE'
                AND (r->>'choice' IN ('Document uploaded', 'Dealer not provided', 'Temporary registration',
                                      'Document not required to be uploaded')
                     OR r->>'outcome' = 'NOT_MANDATORY_ACCEPTED'))
               OR (t.task_status = 'AWAITING_REQUESTER_REVIEW' AND r->>'outcome' = 'NOT_MANDATORY_CLAIMED')
               OR (t.task_status = 'RETURNED' AND r->>'outcome' = 'NOT_MANDATORY_REJECTED'))
        ORDER BY t.tenant_id, t.journey_id, key.requirement_key, t.updated_at_utc DESC
        ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING
        """
    )

    op.execute(f"ALTER TABLE auditcore.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON auditcore.{_TABLE}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_TABLE} ON auditcore.{_TABLE} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.{_TABLE} TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE DELETE ON auditcore.{_TABLE} FROM {_RUNTIME_ROLE}")

def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS auditcore.{_TABLE}")
