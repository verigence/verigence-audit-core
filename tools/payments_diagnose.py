"""Read-only: how the deliveries stand on "receipts equal the NDC" (the rule's own stored answer),
closed and open. Counts only; no customer details, no journey references."""
from __future__ import annotations

import json
import os
import re
from collections import Counter

from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_delivery_start import DELIVERY_STARTED_SQL

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=300000"})


def j(value):
    return json.dumps(value, default=str, sort_keys=True)


def why(status: str, reason: str) -> str:
    reason = reason or ""
    if status == "PASS":
        return "PASS (receipts equal the NDC)"
    if status == "FAIL":
        return "FAIL: short of the NDC" if "short of" in reason else "FAIL: more than the NDC"
    for needle, label in (("settlement window", "waiting: settlement window (7 days after delivery)"),
                          ("No account statement", "no NDC read yet"), ("could not be read with certainty", "NDC lines in doubt"),
                          ("no net value", "NDC shows no net value"), ("loan amount is not on file", "loan amount not on file"),
                          ("no payment receipt on file", "waiting: receipts the NDC shows are not uploaded"),
                          ("cannot be", "NDC payments cannot be read"), ("delivery date", "no delivery date")):
        if needle in reason:
            return label
    return f"{status}: other" if status else "not evaluated"


with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    print("Diagnose stage recomputes in the queue:", j({r[0]: r[1] for r in c.execute(text(
        "SELECT work_status, count(*) FROM auditcore.p2_work_queue WHERE tenant_id=:t AND work_type='STAGE_RECOMPUTE' "
        "AND updated_at_utc > now() - interval '90 minutes' GROUP BY 1"), {"t": T})}))
    rows = c.execute(text(f"""
        SELECT CAST(j.journey_id AS text) AS id,
               COALESCE(r.delivery_completion_state, '') = 'COMPLETE' AS closed,
               st.business_completed_at_utc > now() - interval '6 hours' AS closed_today,
               cs.control_status AS status, cs.details->>'reason' AS reason, cs.details->>'difference' AS difference,
               to_jsonb(cs) AS raw
        FROM auditcore.journeys j
        LEFT JOIN auditcore.p2_journey_runtime r ON r.tenant_id=j.tenant_id AND r.journey_id=j.journey_id
        LEFT JOIN auditcore.journey_stage_states st ON st.tenant_id=j.tenant_id AND st.journey_id=j.journey_id
             AND st.stage_code='DELIVERY'
        LEFT JOIN auditcore.p2_control_state cs ON cs.tenant_id=j.tenant_id AND cs.journey_id=j.journey_id
             AND cs.control_code='RECEIPTS_EQUAL_INVOICES'
        WHERE j.tenant_id=:t AND {DELIVERY_STARTED_SQL.format(t=":t", j="j.journey_id")}"""), {"t": T}).mappings().all()
    print("Diagnose deliveries, whole project:", len(rows), "| closed:", sum(1 for r in rows if r["closed"]),
          "| closed in the last 6 hours:", sum(1 for r in rows if r["closed_today"]))
    sample = next((r["raw"] for r in rows if r["raw"]), None)
    if sample:
        print("Diagnose control state columns:", sorted(k for k in sample if k not in ("journey_id", "tenant_id")))
        reason_key = next((k for k in ("status_reason", "reason") if k in sample), None)
    else:
        reason_key = None

    def reason_of(r):
        raw = r["raw"] or {}
        return str(raw.get(reason_key) or r["reason"] or "") if reason_key else str(r["reason"] or "")

    for label, pick in (("closed before today", lambda r: r["closed"] and not r["closed_today"]),
                        ("closed today by the re-settle", lambda r: r["closed_today"]),
                        ("open", lambda r: not r["closed"])):
        part = [r for r in rows if pick(r)]
        print(f"Diagnose receipts against the NDC, {label} ({len(part)}):",
              j(Counter(why(str(r["status"] or ""), reason_of(r)) for r in part)))
    gaps = Counter()
    for r in rows:
        if str(r["status"] or "") == "FAIL" and r["difference"]:
            amount = abs(float(re.sub(r"[^0-9.\\-]", "", r["difference"]) or 0))
            gaps["up to Rs 100" if amount <= 100 else "Rs 100 to 1,000" if amount <= 1000
                 else "Rs 1,000 to 10,000" if amount <= 10000 else "above Rs 10,000"] += 1
    print("Diagnose size of the difference where it fails:", j(gaps))
