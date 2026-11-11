"""Read-only: the Deal of every delivered journey of one Project, as Journey 360
builds it, one compact JSON line per journey (prefix DEALROW). Never writes."""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_journey360 import deal

T = os.environ.get("DIAG_TENANT", "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572")
FULL = {j for j in os.environ.get("DIAG_FULL", "8d1ca477-bf04-40ca-b6ba-4f793e530fed").split(",") if j}
LIMIT = int(os.environ.get("DIAG_LIMIT", "2000"))
engine = create_engine(os.environ["DATABASE_URL"], pool_size=6, max_overflow=0,
                       connect_args={"options": "-c statement_timeout=60000"})

with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    rows = c.execute(text("""
        SELECT j.journey_id, j.journey_reference, f.customer_name, d.dealer_name
        FROM auditcore.journeys j
        LEFT JOIN auditcore.p2_journey_file_identity f ON f.tenant_id=j.tenant_id AND f.journey_id=j.journey_id
        LEFT JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
        WHERE j.tenant_id=:t AND (j.delivery_id IS NOT NULL OR CAST(j.journey_id AS text) = ANY(:full))
        ORDER BY j.updated_at_utc DESC LIMIT :n"""), {"t": T, "full": list(FULL), "n": LIMIT}).mappings().all()
print("journeys", len(rows), flush=True)

ROW = ("key", "standard", "booking", "billed", "billedFrom", "current", "currentSource", "variance", "flags",
       "opted", "excluded", "awaiting", "invoiced", "freeByInvoice", "partOf", "standardBasis", "ledger", "quote",
       "notTaken", "statementOnly")
DISC = ("key", "entitled", "booking", "billed", "billedFrom", "current", "variance", "flags", "inStandard",
        "takenAsGiven", "eligibility", "potential", "awaiting", "ledgerPosted")


def one(row):
    started = time.time()
    try:
        with engine.connect() as c:
            c.execute(text("SET TRANSACTION READ ONLY"))
            set_tenant_context(c, T)
            view = deal(c, tenant_id=T, journey_id=UUID(str(row["journey_id"])))
            rules = {}
            try:
                from audit_core import uc03_p2_audit_rules as ar
                facts = ar._Facts(c, T, UUID(str(row["journey_id"])))
                facts.__dict__["sheet"] = view
                for fn in (ar.deal_undercharged, ar.insurance_invoice_missing, ar.delivered_on_short_payment):
                    out = fn(facts)
                    rules[out.code] = out.outcome
            except Exception as exc:  # noqa: BLE001
                rules = {"error": repr(exc)[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"j": str(row["journey_id"]), "ref": row["journey_reference"], "error": repr(exc)[:300]}
    out = {
        "j": str(row["journey_id"]), "ref": row["journey_reference"], "name": row["customer_name"],
        "dealer": row["dealer_name"], "secs": round(time.time() - started, 2),
        "summary": {k: v for k, v in view["summary"].items() if k not in ("figuresNotRead", "statementNotRead")},
        "notRead": len(view["summary"].get("figuresNotRead") or []),
        "rows": [{k: r.get(k) for k in ROW if r.get(k) not in (None, [], False)}
                 for g in view["categories"] for r in g["components"]],
        "discounts": [{k: r.get(k) for k in DISC if r.get(k) not in (None, [], False)} for r in view["discounts"]],
        "insurance": view["insurance"],
        "invoices": [{k: i.get(k) for k in ("documentType", "number", "date", "counted", "family", "total",
                                             "netPayable", "copyOf", "status") if i.get(k) is not None}
                     for i in view["invoices"]],
        "declared": [{k: d.get(k) for k in ("key", "billed", "booking", "ledger") if d.get(k) is not None}
                     for d in view["declared"]],
        "sku": {k: view["sku"].get(k) for k in ("model", "variant", "resolution")},
        "rules": rules,
    }
    if str(row["journey_id"]) in FULL:
        out["full"] = view
    return out


with ThreadPoolExecutor(max_workers=6) as pool:
    for result in pool.map(one, rows):
        print("DEALROW " + json.dumps(result, default=str, separators=(",", ":")), flush=True)
print("done", flush=True)
sys.exit(0)
