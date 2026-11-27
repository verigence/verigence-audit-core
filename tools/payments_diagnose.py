"""Read-only: the files already closed, against the money condition as decided on 27 Nov
(the closure gate's own answer). Counts only; no customer details, no references, no amounts."""
from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict

from sqlalchemy import create_engine, text

from audit_core import uc03_p2_audit_rules as rules
from audit_core.db import set_tenant_context

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=300000"})


def j(value):
    return json.dumps(value, default=str, sort_keys=True)


def why(gate) -> str:
    if gate["passed"]:
        return "meets it: equal" if gate["state"] == "PASS" else (
            "meets it: more than the NDC" if "more than billed" in (gate.get("reason") or "") else "meets it: short by under Rs 100")
    reason = gate.get("reason") or ""
    if gate["state"] == "NO_RECEIPTS":
        return "no dealership receipt on file"
    if gate["state"] == "FAIL":
        gap = abs(float(gate.get("difference") or 0))
        return "short by Rs 100 to 1,000" if gap <= 1000 else "short by Rs 1,000 to 10,000" if gap <= 10000 else "short by more than Rs 10,000"
    for needle, label in (("No account statement", "no NDC read"), ("could not be read with certainty", "NDC lines in doubt"),
                          ("do not add up to the Total Receipts", "payment lines do not add up to Total Receipts"),
                          ("no net value", "no net value and no Total Receipts"),
                          ("loan amount is not on file", "loan amount not on file"),
                          ("no payment receipt on file", "receipts the NDC shows are not uploaded"),
                          ("cannot be", "NDC payments cannot be read")):
        if needle in reason:
            return label
    return "not compared: other"


with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    print("Diagnose stage recomputes pending:", c.execute(text(
        "SELECT count(*) FROM auditcore.p2_work_queue WHERE tenant_id=:t AND work_type='STAGE_RECOMPUTE' "
        "AND work_status IN ('PENDING','CLAIMED','RETRY_WAIT')"), {"t": T}).scalar_one())
    rows = c.execute(text("""
        SELECT j.journey_id, split_part(d.dealer_name, ' ', 1) AS dealer,
               st.business_completed_at_utc > now() - interval '12 hours' AS today
        FROM auditcore.journey_stage_states st
        JOIN auditcore.journeys j ON j.tenant_id=st.tenant_id AND j.journey_id=st.journey_id
        JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
        WHERE st.tenant_id=:t AND st.stage_code='DELIVERY' AND st.business_completed_at_utc IS NOT NULL
        ORDER BY md5(CAST(j.journey_id AS text))"""), {"t": T}).mappings().all()
    print("Diagnose delivery files closed:", len(rows), "| today:", sum(1 for r in rows if r["today"]))
    out = defaultdict(Counter)
    started, done = time.monotonic(), 0
    for row in rows:
        if time.monotonic() - started > 600:
            break
        try:
            with c.begin_nested():
                gate = rules.money_equals_what_is_billed(c, tenant_id=T, journey_id=row["journey_id"])
        except Exception as error:  # noqa: BLE001
            gate = {"passed": False, "state": "ERROR", "reason": f"error {type(error).__name__}"}
        done += 1
        label = why(gate)
        out["all closed"][label] += 1
        out["closed today" if row["today"] else "closed before today"][label] += 1
        out[row["dealer"]]["meets it" if gate["passed"] else "does not / cannot be compared"] += 1
    print("Diagnose closed files judged:", done)
    for key in ("all closed", "closed today", "closed before today", "Aditya", "Shivnath", "Utkal"):
        print(f"Diagnose {key} ({sum(out[key].values())}):", j(dict(out[key].most_common())))
