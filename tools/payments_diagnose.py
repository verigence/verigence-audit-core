"""Read-only: the closed files that do not meet the money condition -- how much each is short.
Amounts and counts only; no customer details, no references."""
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


def group(gate) -> str:
    reason = gate.get("reason") or ""
    if gate["state"] == "NO_RECEIPTS":
        return "no dealership receipt on file"
    if gate["state"] == "FAIL":
        return "short of the NDC (compared)"
    for needle, label in (("No account statement", "no NDC read"), ("could not be read with certainty", "NDC lines in doubt"),
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
    print("Diagnose stage recomputes:", j({r[0]: r[1] for r in c.execute(text(
        "SELECT work_status, count(*) FROM auditcore.p2_work_queue WHERE tenant_id=:t AND work_type='STAGE_RECOMPUTE' "
        "AND updated_at_utc > now() - interval '2 hours' GROUP BY 1"), {"t": T})}))
    rows = c.execute(text("""
        SELECT j.journey_id, split_part(d.dealer_name, ' ', 1) AS dealer
        FROM auditcore.journey_stage_states st
        JOIN auditcore.journeys j ON j.tenant_id=st.tenant_id AND j.journey_id=st.journey_id
        JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
        WHERE st.tenant_id=:t AND st.stage_code='DELIVERY' AND st.business_completed_at_utc IS NOT NULL
        ORDER BY md5(CAST(j.journey_id AS text))"""), {"t": T}).mappings().all()
    print("Diagnose delivery files closed:", len(rows))
    counts = Counter()
    short = defaultdict(list)
    started, done = time.monotonic(), 0
    for row in rows:
        if time.monotonic() - started > 620:
            break
        try:
            with c.begin_nested():
                gate = rules.money_equals_what_is_billed(c, tenant_id=T, journey_id=row["journey_id"])
        except Exception as error:  # noqa: BLE001
            gate = {"passed": False, "state": "ERROR", "reason": f"error {type(error).__name__}"}
        done += 1
        if gate["passed"]:
            counts["meets the money condition"] += 1
            continue
        label = group(gate)
        counts[label] += 1
        if gate.get("difference") is not None:
            short[label].append((row["dealer"], round(-float(gate["difference"]))))
    print("Diagnose closed files judged:", done)
    print("Diagnose closed files:", j(dict(counts.most_common())))
    for label, items in short.items():
        items.sort(key=lambda item: -item[1])
        print(f"Diagnose shortfall, {label} ({len(items)} files, total Rs {sum(a for _, a in items):,}):",
              ", ".join(f"{dealer} {amount:,}" for dealer, amount in items))
    no_figure = sum(v for k, v in counts.items() if k != "meets the money condition") - sum(len(v) for v in short.values())
    print("Diagnose closed files with no shortfall figure (nothing to compare with):", no_figure)
