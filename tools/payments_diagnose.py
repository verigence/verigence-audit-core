"""Read-only: where the PC's insurance answer disagrees with the documents, and the shape of the
undercharge / model findings. Counts and detail key names only; no customer details, no references."""
from __future__ import annotations

import json
import os
import re
from collections import Counter

from sqlalchemy import create_engine, text

from audit_core import uc03_insurance_source as insurance
from audit_core.db import set_tenant_context

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=300000"})


def j(value):
    return json.dumps(value, default=str, sort_keys=True)


def band(value) -> str:
    try:
        amount = abs(float(re.sub(r"[^0-9.\\-]", "", str(value)) or 0))
    except ValueError:
        return "?"
    return ("up to Rs 100" if amount <= 100 else "Rs 100 to 1,000" if amount <= 1000 else "Rs 1,000 to 5,000" if amount <= 5000
            else "Rs 5,000 to 25,000" if amount <= 25000 else "Rs 25,000 to 1 lakh" if amount <= 100000 else "above Rs 1 lakh")


with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    cross = Counter()
    for row in c.execute(text("""
            SELECT journey_id, insurance_source FROM auditcore.insurance_policies
            WHERE tenant_id=:t AND insurance_source IS NOT NULL"""), {"t": T}).mappings().all():
        try:
            with c.begin_nested():
                verdict = insurance.document_verdict(c, tenant_id=T, journey_id=row["journey_id"])
        except Exception as error:  # noqa: BLE001
            cross[f"error {type(error).__name__}"] += 1
            continue
        said = str(row["insurance_source"])
        cross[f"PC said {said} / documents say {verdict['source'] if verdict else 'nothing'}"] += 1
    print("Diagnose the PC's insurance answer against the documents:", j(dict(cross.most_common())))

    for code in ("DEAL_UNDERCHARGED", "MODEL_NOT_IDENTIFIED", "EXCESS_DISCOUNT", "TCS_SHORT"):
        rows = c.execute(text("""
            SELECT split_part(d.dealer_name, ' ', 1) AS dealer, cs.control_status, cs.details
            FROM auditcore.p2_control_state cs
            JOIN auditcore.journeys j ON j.tenant_id=cs.tenant_id AND j.journey_id=cs.journey_id
            JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
            WHERE cs.tenant_id=:t AND cs.control_code=:c"""), {"t": T, "c": code}).mappings().all()
        status = Counter(str(r["control_status"]) for r in rows)
        failing = [r for r in rows if str(r["control_status"]) == "FAIL"]
        keys = Counter(k for r in failing for k in (r["details"] or {}))
        print(f"Diagnose {code}: status", j(status), "| failing by dealership", j(Counter(r["dealer"] for r in failing)))
        print(f"Diagnose {code}: detail keys", j(dict(keys.most_common(16))))
        for key in ("undercharge", "undercharged", "difference", "shortfall", "excess", "variance", "gap", "amount"):
            values = [(r["details"] or {}).get(key) for r in failing if (r["details"] or {}).get(key) not in (None, "")]
            if values:
                print(f"Diagnose {code}: size by '{key}'", j(Counter(band(v) for v in values)))
        for key in ("reason", "cause", "basis", "kind", "tier", "status", "source", "priceSource", "sku", "skuStatus"):
            values = [str((r["details"] or {}).get(key))[:60] for r in failing if isinstance((r["details"] or {}).get(key), (str, bool))]
            if values and len(set(values)) <= 12:
                print(f"Diagnose {code}: by '{key}'", j(dict(Counter(values).most_common(8))))
