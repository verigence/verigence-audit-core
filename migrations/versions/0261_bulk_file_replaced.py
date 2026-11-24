"""0261_bulk_file_replaced -- a bulk file uploaded again is REPLACED, not FAILED (2026-11-24).

Product owner, 24 Nov 2026: radhabpd98 (Aditya Motors - Cuttack) submitted a bulk upload of 37
files; 14 showed "Failed -- Not uploaded before submit". None was lost: the browser's first put
of those 14 to storage broke off, the PC uploaded them again, and each retry was added as a
"(2)" copy of the same file (same size) and read. The first record of each was left waiting and
was failed at submit -- and raised a "not uploaded" notification the PC could do nothing about.

From now on a retry reuses the file's own record (uc03_p2_bulk_upload._same_file_earlier) and,
at submit, a record still waiting whose file the drop holds again is REPLACED (hidden from the
PC, no notification). This allows the outcome and re-files the records failed that way before:
a FAILED, never-uploaded file whose drop holds an uploaded file of the same name (numbered or
not) and the same size. A file failed with no such copy stays FAILED.
"""
from __future__ import annotations

from alembic import op

revision = "0261_bulk_file_replaced"
down_revision = "0260_file_tasks_to_uploader"
branch_labels = None
depends_on = None

_OUTCOMES = ("PENDING", "EXACT_DUPLICATE", "READING", "NEW_BOOKING", "ADDED_TO_BOOKING", "KEPT_FOR_REVIEW",
             "FAILED")


def _check(outcomes: tuple[str, ...]) -> None:
    listed = ", ".join(f"'{o}'" for o in outcomes)
    op.execute("ALTER TABLE auditcore.p2_bulk_files DROP CONSTRAINT IF EXISTS p2_bulk_files_outcome_check")
    op.execute(f"ALTER TABLE auditcore.p2_bulk_files ADD CONSTRAINT p2_bulk_files_outcome_check "
               f"CHECK (outcome IN ({listed}))")


def upgrade() -> None:
    _check((*_OUTCOMES, "REPLACED"))
    op.execute(r"""
        UPDATE auditcore.p2_bulk_files f
           SET outcome='REPLACED', detail='Uploaded again as ' || m.again, updated_at_utc=now()
          FROM (
            SELECT DISTINCT ON (w.tenant_id, w.file_id) w.tenant_id, w.file_id, g.original_filename AS again
              FROM auditcore.p2_bulk_files w
              JOIN auditcore.p2_bulk_files g
                ON g.tenant_id=w.tenant_id AND g.bulk_id=w.bulk_id AND g.file_id <> w.file_id
               AND g.upload_status='UPLOADED' AND g.size_bytes=w.size_bytes
               AND regexp_replace(g.original_filename, ' \(\d+\)(\.[^.]*)?$', '\1')
                 = regexp_replace(w.original_filename, ' \(\d+\)(\.[^.]*)?$', '\1')
             WHERE w.upload_status='AWAITING_UPLOAD' AND w.outcome='FAILED'
               AND w.detail='Not uploaded before submit'
             ORDER BY w.tenant_id, w.file_id, g.created_at_utc
          ) m
         WHERE f.tenant_id=m.tenant_id AND f.file_id=m.file_id
    """)


def downgrade() -> None:
    op.execute("UPDATE auditcore.p2_bulk_files SET outcome='FAILED', detail='Not uploaded before submit' "
               "WHERE outcome='REPLACED'")
    _check(_OUTCOMES)
