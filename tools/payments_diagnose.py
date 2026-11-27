"""Read-only: what the receipts rule (and so the closure gate) says, with Total Receipts taken for
the forms that print no net value. Counts only; no customer details, no references, no amounts."""
from __future__ import annotations

import json
import os
import time
from collections import Counter

from sqlalchemy import create_engine, text

from audit_core import uc03_p2_audit_rules as rules
from audit_core.db import set_tenant_context
from audit_core.uc03_delivery_start import DELIVERY_STARTED_SQL

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=300000"})


def j(value):
    return json.dumps(value, default=str, sort_keys=True)


def why(outcome) -> str:
    reason = outcome.reason or ""
    if outcome.outcome == "PASS":
        return "PASS"
    if outcome.outcome == "FAIL":
        gap = abs(float((outcome.details or {}).get("difference") or 0))
        size = "up to Rs 100" if gap <= 100 else "Rs 100 to 1,000" if gap <= 1000 else "Rs 1,000 to 10,000" if gap <= 10000 else "above Rs 10,000"
        return ("FAIL short, " if "short of" in reason else "FAIL more, ") + size
    for needle, label in (("No account statement", "no NDC read"), ("could not be read with certainty", "NDC lines in doubt"),
                          ("do not add up to the Total Receipts", "payment lines do not add up to Total Receipts"),
                          ("no net value", "no net value and no Total Receipts"),
                          ("loan amount is not on file", "loan amount not on file"),
                          ("no payment receipt on file", "waiting: receipts the NDC shows are not uploaded"),
                          ("cannot be", "NDC payments cannot be read")):
        if needle in reason:
            return label
    return "skipped: other"


with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    rows = c.execute(text(f"""
        SELECT j.journey_id, split_part(d.dealer_name, ' ', 1) AS dealer
        FROM auditcore.journeys j
        JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
        WHERE j.tenant_id=:t AND {DELIVERY_STARTED_SQL.format(t=":t", j="j.journey_id")}
        ORDER BY (d.dealer_name ILIKE 'utkal%%') DESC, md5(CAST(j.journey_id AS text))"""), {"t": T}).mappings().all()
    out = {"Utkal, confirmed on Total Receipts": Counter(), "Utkal, on a net value": Counter()}
    started = time.monotonic()
    done = 0
    for row in rows:
        if row["dealer"] != "Utkal" or time.monotonic() - started > 540:
            continue
        try:
            with c.begin_nested():
                facts = rules._Facts(c, T, row["journey_id"])
                if not facts.receipts:
                    key, label = "Utkal, confirmed on Total Receipts", None
                    outcome = rules.receipts_equal_invoices(facts, judged_now=True)
                    side = "Utkal, on a net value" if (outcome.details or {}).get("comparedWith") == "NET_VALUE" else key
                    out[side]["no dealership receipt on file"] += 1
                    done += 1
                    continue
                outcome = rules.receipts_equal_invoices(facts, judged_now=True)
        except Exception as error:  # noqa: BLE001
            out["Utkal, confirmed on Total Receipts"][f"error {type(error).__name__}"] += 1
            continue
        done += 1
        side = "Utkal, on a net value" if (outcome.details or {}).get("comparedWith") == "NET_VALUE" else "Utkal, confirmed on Total Receipts"
        out[side][why(outcome)] += 1
    print("Diagnose Utkal deliveries judged:", done, "of", sum(1 for r in rows if r["dealer"] == "Utkal"))
    for key, counter in out.items():
        print(f"Diagnose {key} ({sum(counter.values())}):", j(dict(counter.most_common())))
