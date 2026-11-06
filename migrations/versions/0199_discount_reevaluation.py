"""0199_discount_reevaluation -- every Journey's Excess Discount rows and
discount checks read again under the 2026-11-06 re-evaluation.

Product owner, 2026-11-06 (re-evaluation of the Excess Discount list):
a line with no master to compare with is "entitlement unknown", the part of a
consumer offer the unused accessories kit covers is "accessories kit given as
cash" (its own check, KIT_GIVEN_AS_CASH), an eligible benefit the
reconciliation saw as unclaimed keeps the master's amount, and the document a
figure came from names the NDC / cost sheet first.

The cached rows (``p2_journey_runtime.discount_variance``) are rebuilt only
when a Journey's facts move; nothing about the facts moved, only the reading.
So:

* every cached copy is marked stale (``price_variance_fact_version`` NULL,
  as 0160 / 0161 did): the worker's price-variance sweep rebuilds them, 25 a
  pass, writing only the cache;
* each Journey under review whose discount checks could change -- one with an
  open "Discount given beyond the entitlement" task or finding, or a cached
  over-grant -- gets one stage recompute, so EXCESS_DISCOUNT and the new
  KIT_GIVEN_AS_CASH check run again and an over-grant that was not one is
  resolved.

Entitlements stored on ``discount_applications`` (an empty scheme version now
MASTER_UNMATCHED) change when the deal is reconciled again: the master reload
job with a ``-reprice-commit`` tag does that for every Journey.
"""
from __future__ import annotations

from alembic import op

revision = "0199_discount_reevaluation"
down_revision = "0198_closure_closes_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE auditcore.p2_journey_runtime SET price_variance_fact_version = NULL")
    op.execute(
        """
        SELECT auditcore.p2_request_stage_recompute(j.tenant_id, j.journey_id)
        FROM auditcore.journeys j
        JOIN auditcore.p2_journey_runtime r ON r.tenant_id = j.tenant_id AND r.journey_id = j.journey_id
        WHERE j.review_completed_at_utc IS NULL
          AND (
            EXISTS (SELECT 1 FROM jsonb_array_elements(COALESCE(r.discount_variance, '[]'::jsonb)) AS e(item)
                    WHERE e.item->>'status' = 'OVER_ENTITLEMENT')
            OR EXISTS (SELECT 1 FROM auditcore.p2_tasks t
                       WHERE t.tenant_id = j.tenant_id AND t.journey_id = j.journey_id
                         AND t.source_code = 'EXCESS_DISCOUNT'
                         AND t.task_status NOT IN ('VERIFIED_COMPLETE', 'CANCELLED', 'FAILED', 'DEAD_LETTER'))
            OR EXISTS (SELECT 1 FROM auditcore.audit_findings f
                       WHERE f.tenant_id = j.tenant_id AND f.journey_id = j.journey_id
                         AND f.rule_key = 'EXCESS_DISCOUNT' AND f.finding_status IN ('OPEN', 'ACKNOWLEDGED'))
          )
        """
    )


def downgrade() -> None:
    # A cache rebuilt and checks run again: nothing to undo.
    pass
