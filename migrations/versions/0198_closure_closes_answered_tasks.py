"""0198_closure_closes_answered_tasks -- tasks a Reviewer's closure answered
are closed with it (2026-11-06).

SHI-SS-261027-013 was "Closed by Reviewer · awaiting TL" while its task "Fix
pages that could not be processed" -- one of the items the closure answered --
stayed open for the PC. From now on ``close_journey`` closes those tasks; this
migration does the same for closures already made (the latest closure of each
journey, unless the Team Lead rejected it): an answered open task, an answered
finding's own task, an answered missing document's Document Missing task. Each
keeps the closure, the answer and its issue hash, so the next sync does not
reopen it for the same problem. Findings themselves are untouched.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0198_closure_closes_answered_tasks"
down_revision = "0197_p2_journey_exports"
branch_labels = None
depends_on = None

_OPEN = ["READY", "IN_PROGRESS", "RETURNED", "ACTION_COMPLETED", "VERIFYING", "AWAITING_REQUESTER_REVIEW"]
_NOT_ITEMS = ["DELIVERY_REVIEW", "NOT_MANDATORY_REVIEW", "REVIEWER_CORRECTIONS_INFO", "CLOSURE_ACCEPTANCE",
              "CLOSURE_REWORK"]


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            WITH latest AS (
              SELECT DISTINCT ON (c.tenant_id, c.journey_id)
                     c.tenant_id, c.journey_id, c.closure_id, c.closed_by_actor_id, c.acceptance_status, c.summary
              FROM auditcore.p2_journey_closures c
              ORDER BY c.tenant_id, c.journey_id, c.closed_at_utc DESC, c.closure_id DESC
            ), answered AS (
              SELECT l.tenant_id, l.journey_id, l.closure_id, l.closed_by_actor_id,
                     split_part(i->>'key', ':', 1) AS kind,
                     substr(i->>'key', length(split_part(i->>'key', ':', 1)) + 2) AS ref,
                     i->>'answer' AS answer
              FROM latest l, jsonb_array_elements(COALESCE(l.summary->'items', '[]'::jsonb)) AS i
              WHERE l.acceptance_status <> 'REJECTED'
            ), hit AS (
              SELECT DISTINCT ON (t.tenant_id, t.task_id)
                     t.tenant_id, t.task_id, t.journey_id, t.reference, a.closure_id, a.closed_by_actor_id, a.answer
              FROM auditcore.p2_tasks t
              JOIN answered a ON a.tenant_id=t.tenant_id AND a.journey_id=t.journey_id AND (
                   (a.kind='task' AND CAST(t.task_id AS text)=a.ref)
                OR (a.kind='finding' AND t.reference->>'findingId'=a.ref)
                OR (a.kind='document' AND t.task_type='DOCUMENT_MISSING' AND t.reference->>'requirementKey'=a.ref))
              WHERE t.task_status = ANY(CAST(:open AS varchar[])) AND t.task_type <> ALL(CAST(:skip AS varchar[]))
            ), closed AS (
              UPDATE auditcore.p2_tasks t
              SET task_status='VERIFIED_COMPLETE', verified_at_utc=now(), updated_at_utc=now(),
                  completed_by_actor_id=h.closed_by_actor_id,
                  completion_result=COALESCE(t.completion_result, '{}'::jsonb) || jsonb_build_object(
                      'outcome', 'CLOSED_BY_REVIEWER_CLOSURE', 'closureId', CAST(h.closure_id AS text),
                      'answer', h.answer, 'closedBy', h.closed_by_actor_id, 'closedRole', 'REVIEWER',
                      'acceptedIssueHash', h.reference->>'issueHash', 'backfilled', true)
              FROM hit h
              WHERE t.tenant_id=h.tenant_id AND t.task_id=h.task_id
              RETURNING t.tenant_id, t.task_id, t.journey_id, h.closure_id, h.closed_by_actor_id, h.answer
            )
            INSERT INTO auditcore.p2_task_events (tenant_id, task_id, journey_id, event_type, actor_id,
                                                  actor_role_code, comment, details)
            SELECT tenant_id, task_id, journey_id, 'CLOSED_BY_REVIEWER_CLOSURE', closed_by_actor_id, 'REVIEWER',
                   answer, jsonb_build_object('closureId', CAST(closure_id AS text), 'backfilled', true)
            FROM closed
            """
        ),
        {"open": _OPEN, "skip": _NOT_ITEMS},
    )


def downgrade() -> None:
    # Closing answered work is not undone; a Team Lead's rejection reopens it for the Reviewer.
    pass
