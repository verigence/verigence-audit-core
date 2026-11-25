"""Read only: why carried units moved with their reading ended CANCELLED (25 Nov check)."""
import os
from collections import Counter

from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
engine = create_engine(os.environ["DATABASE_URL"])
with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    rows = c.execute(text("""
        SELECT j.journey_reference, q.queue_id, q.unit_kind, q.di_document_id, q.classified_document_type,
               q.last_error, q.status_reason, q.updated_at_utc, q.created_at_utc, q.attempt_count,
               ub.failure_reason, ub.batch_status,
               (SELECT string_agg(e.event_type || '@' || to_char(e.created_at_utc,'HH24:MI:SS') || ' ' ||
                       coalesce(e.details::text,''), ' | ' ORDER BY e.created_at_utc)
                  FROM auditcore.p2_activity_events e WHERE e.tenant_id=q.tenant_id
                   AND e.subject_id IN (CAST(q.queue_id AS text), CAST(ub.batch_id AS text))) AS events
        FROM auditcore.p2_upload_batches ub
        JOIN auditcore.journeys j ON j.tenant_id=ub.tenant_id AND j.journey_id=ub.journey_id
        JOIN auditcore.p2_document_queue q ON q.tenant_id=ub.tenant_id AND q.batch_id=ub.batch_id
             AND q.merged_into_queue_id IS NULL
        WHERE ub.tenant_id=:t AND ub.correlation_id LIKE 'p2-duplicate-merge:%'
          AND ub.created_at_utc >= '2026-11-24 12:30+00' AND q.queue_status='CANCELLED'
        ORDER BY ub.created_at_utc"""), {"t": T}).mappings().all()
    print("cancelled:", len(rows))
    print("by reason:", Counter((r["last_error"], r["status_reason"]) for r in rows).most_common(10))
    print("by journey:", Counter(r["journey_reference"] for r in rows))
    for r in rows[:4]:
        print("  ", {k: (str(v)[:700] if v is not None else None) for k, v in r.items()})
    # Does DI still hold these documents for the target? (DI state as Audit Core last saw it)
    print("di_document_ids distinct:", len({r["di_document_id"] for r in rows}))
