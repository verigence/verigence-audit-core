"""Read-only: did the Excess Discount check run again and raise its task on the rechecked bookings."""
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
    cols = [r[0] for r in c.execute(text("SELECT column_name FROM information_schema.columns "
                                         "WHERE table_schema='auditcore' AND table_name='rule_executions'"))]
    print("rule_executions columns:", cols)
    for ref in ("UTK-SR-261030-001", "UTK-SR-261103-001", "ADI-BM-261114-028"):
        j = c.execute(text("SELECT journey_id FROM auditcore.journeys WHERE tenant_id=:t AND journey_reference=:r"),
                      {"t": T, "r": ref}).scalar_one()
        p = {"t": T, "j": j, "k": f"control:{j}:EXCESS_DISCOUNT"}
        print("\n" + ref)
        for t in c.execute(text("""
            SELECT task_status, completion_result, assigned_role_code, left(description, 140) AS description,
                   created_at_utc, updated_at_utc
            FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k"""), p).mappings():
            print("  control task:", json.dumps(dict(t), default=str))
        for f in c.execute(text("""
            SELECT finding_status, disposition, left(resolution_reason, 160) AS why, created_at_utc, resolved_at_utc
            FROM auditcore.audit_findings WHERE tenant_id=:t AND journey_id=:j AND rule_key='EXCESS_DISCOUNT'
            ORDER BY created_at_utc"""), p).mappings():
            print("  finding:", json.dumps(dict(f), default=str))
        for e in c.execute(text("""
            SELECT to_jsonb(x) AS x FROM auditcore.rule_executions x
            WHERE x.tenant_id=:t AND x.journey_id=:j AND x.rule_code='EXCESS_DISCOUNT'
            ORDER BY x.executed_at_utc DESC LIMIT 3"""), p).mappings():
            x = e["x"]
            print("  execution:", json.dumps({k: x.get(k) for k in ("executed_at_utc", "outcome", "triggering_event", "audit_finding_id")}, default=str),
                  "|", str(x.get("reason") or "")[:200])
    print()
    print("EXCESS_DISCOUNT control tasks by status:", c.execute(text("""
        SELECT task_status, count(*) FROM auditcore.p2_tasks
        WHERE tenant_id=:t AND source_code='EXCESS_DISCOUNT' GROUP BY 1 ORDER BY 1"""), {"t": T}).all())
    print("open EXCESS_DISCOUNT tasks created or reopened today:", c.execute(text("""
        SELECT count(*) FROM auditcore.p2_tasks WHERE tenant_id=:t AND source_code='EXCESS_DISCOUNT'
          AND task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')
          AND updated_at_utc > now() - interval '2 hours'"""), {"t": T}).scalar())
