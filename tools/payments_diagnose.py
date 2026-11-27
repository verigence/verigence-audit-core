"""Read-only: is the re-settle of 27 Nov finished, and how many delivery files are closed. Counts only."""
from __future__ import annotations

import json
import os

from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=300000"})

with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    print("Diagnose stage recomputes not finished:", json.dumps({r[0]: r[1] for r in c.execute(text(
        "SELECT work_status, count(*) FROM auditcore.p2_work_queue WHERE tenant_id=:t AND work_type='STAGE_RECOMPUTE' "
        "AND work_status NOT IN ('COMPLETED') AND updated_at_utc > now() - interval '6 hours' GROUP BY 1"), {"t": T})}))
    print("Diagnose delivery files closed, whole project:", c.execute(text(
        "SELECT count(*) FROM auditcore.journey_stage_states WHERE tenant_id=:t AND stage_code='DELIVERY' "
        "AND business_completed_at_utc IS NOT NULL"), {"t": T}).scalar_one(),
        "| closed since 14:30 IST:", c.execute(text(
        "SELECT count(*) FROM auditcore.journey_stage_states WHERE tenant_id=:t AND stage_code='DELIVERY' "
        "AND business_completed_at_utc > TIMESTAMPTZ '2026-11-27 09:00:00+00'"), {"t": T}).scalar_one())
    print("Diagnose money gate on open deliveries:", json.dumps({r[0]: r[1] for r in c.execute(text("""
        SELECT COALESCE(g.details->>'state', '(not evaluated)'), count(*)
        FROM auditcore.p2_journey_runtime r
        LEFT JOIN auditcore.p2_stage_gate_state g ON g.tenant_id=r.tenant_id AND g.journey_id=r.journey_id
             AND g.stage_code='DELIVERY' AND g.gate_key='MONEY_EQUALS_BILLED'
        WHERE r.tenant_id=:t AND r.current_stage LIKE 'DELIVERY%' AND r.delivery_completion_state <> 'COMPLETE'
        GROUP BY 1 ORDER BY 2 DESC"""), {"t": T})}))
