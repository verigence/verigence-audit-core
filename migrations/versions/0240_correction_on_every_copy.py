"""0240_correction_on_every_copy -- a value the PC corrected is corrected on every stored copy
of it, so its manual verification task can close.

Product owner, 2026-11-18: "reoccuring error... PC closed manual verification but it is not
closed.. it is still open, why so".

``journey_document_extracted_fields`` keeps a value per stage. The document screen wrote a
correction under the page's stage as the screen had it; where the value had been stored under
the other stage, the correction became a second row and the value as read stayed "not reviewed"
-- which is what the manual verification task waits for. UTK-JI-261019-002 (Suresh Kumar Barik):
two receipt dates corrected on 12 and 17 Nov, both "Check ... date on Payment Receipt" tasks
still open on 18 Nov, the PC writing "I have completed the payment receipt date verification".
The screen now writes onto the stored rows (uc03_p2_documents.correct_p2_document_field); this
brings the rows already split into line:

* a copy nobody reviewed takes the review of its reviewed copy (the corrected value, who and
  when); the value as read (``extracted_value``) is left as it is;
* each such Journey gets one stage recompute, so its task is checked again and closes.

Nothing is deleted. Idempotent: a second run finds no unreviewed copy beside a reviewed one.
"""
from __future__ import annotations

from alembic import op

revision = "0240_correction_on_every_copy"
down_revision = "0239_observation_severity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH reviewed AS (
            SELECT DISTINCT ON (tenant_id, journey_id, di_document_id, source_canonical_field_id, source_fact_version)
                   tenant_id, journey_id, di_document_id, source_canonical_field_id, source_fact_version,
                   modified_value, effective_value, is_modified, modified_by_actor_id, modified_at_utc,
                   reviewed_by_actor_id, reviewed_at_utc
            FROM auditcore.journey_document_extracted_fields
            WHERE source_canonical_field_id IS NOT NULL AND reviewed_at_utc IS NOT NULL
            ORDER BY tenant_id, journey_id, di_document_id, source_canonical_field_id, source_fact_version,
                     reviewed_at_utc DESC
        ),
        brought AS (
            UPDATE auditcore.journey_document_extracted_fields f
            SET modified_value = r.modified_value,
                effective_value = r.effective_value,
                is_modified = r.is_modified,
                modified_by_actor_id = r.modified_by_actor_id,
                modified_at_utc = r.modified_at_utc,
                reviewed_by_actor_id = r.reviewed_by_actor_id,
                reviewed_at_utc = r.reviewed_at_utc,
                updated_at_utc = now()
            FROM reviewed r
            WHERE f.tenant_id = r.tenant_id AND f.journey_id = r.journey_id
              AND f.di_document_id = r.di_document_id
              AND f.source_canonical_field_id = r.source_canonical_field_id
              AND f.source_fact_version = r.source_fact_version
              AND f.reviewed_at_utc IS NULL AND f.is_modified = false
            RETURNING f.tenant_id, f.journey_id
        )
        SELECT auditcore.p2_request_stage_recompute(b.tenant_id, b.journey_id)
        FROM (SELECT DISTINCT tenant_id, journey_id FROM brought) b
        """
    )


def downgrade() -> None:
    # A review copied onto the same value's other row: nothing to undo.
    pass
