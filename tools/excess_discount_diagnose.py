"""Read-only: what the change to the "depends on in-house insurance" list moves on the live
Project. Every booking with such a row stored is rebuilt with this branch's code; nothing
is written. Prints references and figures only."""
from __future__ import annotations

import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_discount_variance import discount_variance_rows
from audit_core.uc03_p2_journey360 import deal
from audit_core.uc03_statement_totals import charges_in_doubt

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"  # Mahindra Orissa
engine = create_engine(os.environ["DATABASE_URL"], pool_size=8, max_overflow=2,
                       connect_args={"options": "-c statement_timeout=120000"})

with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    stored = c.execute(text("""
        SELECT j.journey_id, j.journey_reference, r.discount_variance,
               EXISTS (SELECT 1 FROM auditcore.journey_stage_states bs
                       WHERE bs.tenant_id = j.tenant_id AND bs.journey_id = j.journey_id AND bs.stage_code = 'BOOKING'
                         AND (bs.business_status IN ('BOOKING_CANCELLED', 'DUPLICATE_BOOKING')
                              OR bs.closure_disposition = 'NO_DELIVERY')) AS cancelled
        FROM auditcore.p2_journey_runtime r
        JOIN auditcore.journeys j ON j.tenant_id = r.tenant_id AND j.journey_id = r.journey_id
        WHERE r.tenant_id = :t AND r.discount_variance @> '[{"status": "OVER_UNLESS_INHOUSE"}]'
        ORDER BY j.journey_reference"""), {"t": T}).mappings().all()
    overall = c.execute(text("""
        SELECT e.item->>'status' AS status, count(*) AS rows, sum((e.item->>'variance')::numeric) AS variance
        FROM auditcore.p2_journey_runtime r
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(r.discount_variance, '[]'::jsonb)) AS e(item)
        WHERE r.tenant_id = :t GROUP BY 1 ORDER BY 1"""), {"t": T}).mappings().all()
print("stored rows by status:", json.dumps([dict(r) for r in overall], default=str))
stored = [dict(r) for r in stored if not r["cancelled"]]
print("bookings with a 'depends on in-house insurance' row:", len(stored))


def rebuild(row):
    jid = UUID(str(row["journey_id"]))
    try:
        with engine.connect() as c:
            c.execute(text("SET TRANSACTION READ ONLY"))
            set_tenant_context(c, T)
            view = deal(c, tenant_id=T, journey_id=jid)
            rows = discount_variance_rows(view, charges_in_doubt(c, tenant_id=T, journey_id=jid))
            return row, rows, view.get("insurance") or {}, None
    except Exception as exc:  # noqa: BLE001
        return row, None, {}, repr(exc)


moved = Counter()
amount = Counter()
with ThreadPoolExecutor(max_workers=8) as pool:
    for row, rows, insurance, error in pool.map(rebuild, stored):
        ref = row["journey_reference"]
        if error:
            moved["FAILED"] += 1
            print("FAILED", ref, error)
            continue
        before = next(r for r in row["discount_variance"] if r.get("status") == "OVER_UNLESS_INHOUSE")
        after = next((r for r in rows if r.get("key") == before.get("key")), None)
        status = after["status"] if after else "NO_ROW"
        moved[status] += 1
        amount[status] += float((after or before).get("variance") or 0)
        print(ref, "|", status, "| given", before.get("invoice"), "entitled", before.get("entitled"), "extra",
              before.get("variance"), "| tier", ((before.get("offer") or {}).get("tier")),
              "| insurance", insurance.get("source"), "by", insurance.get("decidedBy"),
              "|", (after or {}).get("reason"))
print("after this change:", dict(moved))
print("extra rupees by status after:", {k: round(v) for k, v in amount.items()})
