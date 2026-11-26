"""Read-only: after the two fixes -- the Excess Discount check on the rechecked bookings and
the Payments tab of the booking with receipts on file twice. References and figures only."""
from __future__ import annotations

import json
import os
from uuid import UUID

from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_journey360 import payments

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=120000"})
with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    print("forced evaluations of today's job by status:", c.execute(text("""
        SELECT work_status, count(*) FROM auditcore.p2_work_queue
        WHERE tenant_id=:t AND work_type='CONTROL_EVALUATE' AND correlation_id LIKE 'rules_rerun:excess-and-receipts%'
        GROUP BY 1 ORDER BY 1"""), {"t": T}).all())
    for ref in ("UTK-SR-261030-001", "UTK-SR-261103-001", "ADI-BM-261114-028"):
        j = c.execute(text("SELECT journey_id FROM auditcore.journeys WHERE tenant_id=:t AND journey_reference=:r"),
                      {"t": T, "r": ref}).scalar_one()
        p = {"t": T, "j": j, "k": f"control:{j}:EXCESS_DISCOUNT"}
        print("\n" + ref)
        for t in c.execute(text("""
            SELECT task_status, assigned_role_code, left(description, 170) AS description, updated_at_utc
            FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k"""), p).mappings():
            print("  task:", json.dumps(dict(t), default=str))
        for f in c.execute(text("""
            SELECT finding_status, disposition, created_at_utc FROM auditcore.audit_findings
            WHERE tenant_id=:t AND journey_id=:j AND rule_key='EXCESS_DISCOUNT' ORDER BY created_at_utc DESC LIMIT 1"""), p).mappings():
            print("  latest finding:", json.dumps(dict(f), default=str))
        for e in c.execute(text("""
            SELECT evaluated_at_utc, outcome, left(reason, 170) AS reason FROM auditcore.rule_executions
            WHERE tenant_id=:t AND journey_id=:j AND rule_code='EXCESS_DISCOUNT'
            ORDER BY evaluated_at_utc DESC LIMIT 1"""), p).mappings():
            print("  latest run of the check:", json.dumps(dict(e), default=str))
    print()
    print("EXCESS_DISCOUNT tasks by status:", c.execute(text("""
        SELECT task_status, count(*) FROM auditcore.p2_tasks
        WHERE tenant_id=:t AND source_code='EXCESS_DISCOUNT' GROUP BY 1 ORDER BY 1"""), {"t": T}).all())
    print()
    J = UUID("1e14ed84-2e7b-44ae-a8ce-c2c426f29dd6")
    view = payments(c, tenant_id=T, journey_id=J)
    print("UTK-JP-261031-007 payments:", json.dumps({k: view[k] for k in ("duplicates", "receiptsTotal", "paidTotal")}))
    for item in view["items"]:
        if item["amount"] in ("199000.00", "500000.00"):
            print("  ", item["receiptNumber"], "|", item["amount"], "|", "counted" if item["counted"] else "NOT counted",
                  "|", item["notCountedReason"])
