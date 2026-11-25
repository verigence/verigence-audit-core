"""Read only: DOCUMENT_AI_UNAVAILABLE on 24 Nov evening, moved carries vs everything else."""
import os

from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
engine = create_engine(os.environ["DATABASE_URL"])
with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    for r in c.execute(text("""
        SELECT date_trunc('hour', q.created_at_utc) AS hour,
               (q.client_upload_id LIKE 'carried-%') AS moved,
               count(*) AS units,
               count(*) FILTER (WHERE q.last_error='DOCUMENT_AI_UNAVAILABLE') AS ai_unavailable,
               count(*) FILTER (WHERE q.queue_status IN ('READY','NEEDS_REVIEW','SUPPORTING')) AS settled
        FROM auditcore.p2_document_queue q
        WHERE q.tenant_id=:t AND q.created_at_utc >= '2026-11-24 12:00+00' AND q.merged_into_queue_id IS NULL
        GROUP BY 1,2 ORDER BY 1,2""") , {"t": T}).mappings():
        print("  ", dict(r))
    # Moved units that settled vs failed: their DI state as recorded on the queue row
    for r in c.execute(text("""
        SELECT q.queue_status, q.di_state, q.di_processing_status, count(*)
        FROM auditcore.p2_document_queue q
        WHERE q.tenant_id=:t AND q.client_upload_id LIKE 'carried-%' AND q.merged_into_queue_id IS NULL
        GROUP BY 1,2,3 ORDER BY 4 DESC"""), {"t": T}).all():
        print("   moved", tuple(r))
