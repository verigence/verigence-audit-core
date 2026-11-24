"""Read only: why a carried upload's channel reads direct (ADI-RD-261026-044 etc.)."""
import os

from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
engine = create_engine(os.environ["DATABASE_URL"])
with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    for ref in ("ADI-RD-261026-044", "ADI-BO-261114-018", "ADI-BO-261115-008"):
        j = c.execute(text("SELECT journey_id FROM auditcore.journeys WHERE tenant_id=:t AND journey_reference=:r"),
                      {"t": T, "r": ref}).scalar()
        rows = c.execute(text("""
            SELECT ub.batch_id, ub.created_at_utc, ub.correlation_id, a.event_type, a.subject_id, a.details->>'job' AS job,
                   (SELECT count(*) FROM auditcore.p2_bulk_files bf
                     WHERE bf.tenant_id=ub.tenant_id
                       AND CAST(bf.journey_id AS text) = substr(a.details->>'job', 11)) AS bulk_files_on_read,
                   (SELECT string_agg(bu.source || ':' || bf.outcome, ',') FROM auditcore.p2_bulk_files bf
                     JOIN auditcore.p2_bulk_uploads bu ON bu.tenant_id=bf.tenant_id AND bu.bulk_id=bf.bulk_id
                     WHERE bf.tenant_id=ub.tenant_id AND bf.placed_journey_id=ub.journey_id) AS placed_here
            FROM auditcore.p2_upload_batches ub
            LEFT JOIN auditcore.p2_activity_events a ON a.tenant_id=ub.tenant_id AND a.journey_id=ub.journey_id
                 AND a.subject_id = CAST(ub.batch_id AS text) AND a.event_type='DOCUMENT_COPIED_FROM_DUPLICATE'
            WHERE ub.tenant_id=:t AND ub.journey_id=:j AND ub.created_at_utc >= '2026-11-23 12:00+00'
              AND ub.created_at_utc < '2026-11-23 18:30+00'
            ORDER BY ub.created_at_utc LIMIT 3"""), {"t": T, "j": j}).mappings().all()
        print(ref, j)
        for r in rows:
            print("   ", dict(r))
