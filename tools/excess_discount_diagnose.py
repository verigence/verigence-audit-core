"""Read-only: bookings whose stored dealer-grid tier differs from the tier the insurance
decision gives now (this branch's code). Nothing is written. References and figures only."""
from __future__ import annotations

import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import UUID

from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_deal_reconciliation import _grid_tier

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
engine = create_engine(os.environ["DATABASE_URL"], pool_size=8, max_overflow=2,
                       connect_args={"options": "-c statement_timeout=120000"})
with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    rows = [dict(r) for r in c.execute(text("""
        SELECT j.journey_id, j.journey_reference, da.actual_discount_amount AS given,
               g.grid->>'tier' AS tier, g.grid->'tiers' AS tiers, j.review_completed_at_utc IS NULL AS under_review,
               EXISTS (SELECT 1 FROM auditcore.journey_stage_states bs
                       WHERE bs.tenant_id = j.tenant_id AND bs.journey_id = j.journey_id AND bs.stage_code = 'BOOKING'
                         AND (bs.business_status IN ('BOOKING_CANCELLED', 'DUPLICATE_BOOKING')
                              OR bs.closure_disposition = 'NO_DELIVERY')) AS cancelled
        FROM auditcore.discount_applications da
        JOIN auditcore.journeys j ON j.tenant_id = da.tenant_id AND j.journey_id = da.journey_id
        CROSS JOIN LATERAL (SELECT COALESCE(da.details->'reconciliation'->'grid', da.details->'grid') AS grid) g
        WHERE da.tenant_id = :t AND da.discount_key = 'ADDITIONAL_DISCOUNT' AND g.grid->>'tier' IS NOT NULL
          AND da.actual_discount_amount > 0
        ORDER BY j.journey_reference"""), {"t": T}).mappings()]
rows = [r for r in rows if not r["cancelled"]]
print("bookings with a dealer discount given and a tiered grid line:", len(rows),
      "| stored tiers:", dict(Counter(r["tier"] for r in rows)))


def tier_now(row):
    try:
        with engine.connect() as c:
            c.execute(text("SET TRANSACTION READ ONLY"))
            set_tenant_context(c, T)
            return row, _grid_tier(c, tenant_id=T, journey_id=UUID(str(row["journey_id"]))), None
    except Exception as exc:  # noqa: BLE001
        return row, None, repr(exc)


moves = Counter()
newly_over = 0
newly_over_rupees = Decimal(0)
with ThreadPoolExecutor(max_workers=8) as pool:
    for row, now, error in pool.map(tier_now, rows):
        if error:
            moves["FAILED"] += 1
            print("FAILED", row["journey_reference"], error)
            continue
        if now == row["tier"]:
            moves["same"] += 1
            continue
        moves[f"{row['tier']} -> {now}"] += 1
        tiers = row["tiers"] if isinstance(row["tiers"], dict) else json.loads(row["tiers"] or "{}")
        before, after = Decimal(str(tiers.get(row["tier"]) or 0)), Decimal(str(tiers.get(now) or 0))
        given = Decimal(str(row["given"]))
        was_over, is_over = given - before > 100, given - after > 100
        if is_over and not was_over:
            newly_over += 1
            newly_over_rupees += given - after
        print(row["journey_reference"], "|", row["tier"], "->", now, "| given", given, "| allowed", before, "->", after,
              "| over before", was_over, "now", is_over, "| under review", row["under_review"])
print("tier moves:", dict(moves))
print("dealer discounts that become above their tier:", newly_over, "extra rupees", newly_over_rupees)
