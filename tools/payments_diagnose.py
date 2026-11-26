"""Read-only: the Excess Discount list after the recompute job. References and figures only."""
from __future__ import annotations

import json
import os

from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=120000"})
with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    for ref in ("UTK-SR-261030-001", "ADI-BM-261114-009", "UTK-PP-261023-009"):
        row = c.execute(text("""
            SELECT r.discount_variance, r.fact_version, r.price_variance_fact_version, j.journey_id
            FROM auditcore.journeys j JOIN auditcore.p2_journey_runtime r
              ON r.tenant_id = j.tenant_id AND r.journey_id = j.journey_id
            WHERE j.tenant_id = :t AND j.journey_reference = :r"""), {"t": T, "r": ref}).mappings().one()
        print(ref, "| facts", row["fact_version"], "list built at", row["price_variance_fact_version"])
        for item in row["discount_variance"] or []:
            print("   row:", json.dumps({k: item.get(k) for k in ("key", "status", "entitled", "invoice", "variance", "reason")}))
        for t in c.execute(text("""
            SELECT task_status, left(description, 160) AS description, created_at_utc, updated_at_utc
            FROM auditcore.p2_tasks WHERE tenant_id = :t AND journey_id = :j AND source_code = 'EXCESS_DISCOUNT'
            ORDER BY created_at_utc"""), {"t": T, "j": row["journey_id"]}).mappings():
            print("   task:", json.dumps(dict(t), default=str))
        tier = c.execute(text("""
            SELECT COALESCE(details->'reconciliation'->'grid', details->'grid')->>'tier', standard_eligible_amount, updated_at_utc
            FROM auditcore.discount_applications WHERE tenant_id = :t AND journey_id = :j
              AND discount_key = 'ADDITIONAL_DISCOUNT'"""), {"t": T, "j": row["journey_id"]}).first()
        print("   stored tier:", tuple(str(v) for v in tier) if tier else None)
    print("stored rows by status:", json.dumps([dict(r) for r in c.execute(text("""
        SELECT e.item->>'status' AS status, count(*) AS rows, sum((e.item->>'variance')::numeric) AS variance
        FROM auditcore.p2_journey_runtime r
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(r.discount_variance, '[]'::jsonb)) AS e(item)
        WHERE r.tenant_id = :t GROUP BY 1 ORDER BY 1"""), {"t": T}).mappings()], default=str))
    print("stage recomputes waiting:", c.execute(text("""
        SELECT work_status, count(*) FROM auditcore.p2_work_queue
        WHERE tenant_id = :t AND work_type = 'STAGE_RECOMPUTE' AND updated_at_utc > now() - interval '3 hours'
        GROUP BY 1"""), {"t": T}).all())
