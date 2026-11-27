"""Read-only: the size and the lines of the undercharge, excess discount and TCS findings.
Counts and Deal line names only; no customer details, no references."""
from __future__ import annotations

import json
import os
import re
from collections import Counter

from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=300000"})


def j(value):
    return json.dumps(value, default=str, sort_keys=True)


def number(value) -> float | None:
    try:
        text_value = re.sub(r"[^0-9.\\-]", "", str(value))
        return abs(float(text_value)) if text_value not in ("", "-", ".") else None
    except ValueError:
        return None


def band(amount: float | None) -> str:
    if amount is None:
        return "?"
    return ("up to Rs 100" if amount <= 100 else "Rs 100 to 1,000" if amount <= 1000 else "Rs 1,000 to 5,000" if amount <= 5000
            else "Rs 5,000 to 25,000" if amount <= 25000 else "Rs 25,000 to 1 lakh" if amount <= 100000 else "above Rs 1 lakh")


def failing(code):
    return [r[0] or {} for r in c.execute(text(
        "SELECT details FROM auditcore.p2_control_state WHERE tenant_id=:t AND control_code=:c AND control_status='FAIL'"),
        {"t": T, "c": code})]


with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    rows = failing("DEAL_UNDERCHARGED")
    print("Diagnose undercharged:", len(rows), "| net short by:", j(Counter(band(number(r.get("netShort"))) for r in rows)))
    print("Diagnose undercharged: low registration", j(Counter(str(bool(r.get("lowRegistration"))) for r in rows)),
          "| scrappage concession", j(Counter(str(bool(r.get("scrappageConcession"))) for r in rows)))
    lines, how_many, shape = Counter(), Counter(), Counter()
    for r in rows:
        components = r.get("components")
        shape[type(components).__name__] += 1
        items = components if isinstance(components, list) else (
            [{"key": k, **(v if isinstance(v, dict) else {"value": v})} for k, v in components.items()]
            if isinstance(components, dict) else [])
        short = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("key") or item.get("line") or item.get("component") or item.get("label") or "?")[:40]
            gap = number(item.get("short") or item.get("shortfall") or item.get("difference") or item.get("gap"))
            if gap is None or gap > 0:
                lines[name] += 1
                short += 1
        how_many["1 line" if short == 1 else "2 lines" if short == 2 else "3+ lines" if short >= 3 else "0"] += 1
    print("Diagnose undercharged: components shape", j(shape), "| lines per finding", j(how_many))
    print("Diagnose undercharged: lines named", j(dict(lines.most_common(16))))
    sample = next((r.get("components") for r in rows if r.get("components")), None)
    if isinstance(sample, list) and sample and isinstance(sample[0], dict):
        print("Diagnose undercharged: component keys", sorted(sample[0]))
    elif isinstance(sample, dict):
        first = next(iter(sample.values()))
        print("Diagnose undercharged: component names", sorted(sample)[:20], "| each", sorted(first) if isinstance(first, dict) else type(first).__name__)

    rows = failing("EXCESS_DISCOUNT")
    print("Diagnose excess discount:", len(rows), "| extra given:", j(Counter(band(number(r.get("extra"))) for r in rows)))
    kinds = Counter()
    for r in rows:
        for item in (r.get("discounts") or []) if isinstance(r.get("discounts"), list) else []:
            if isinstance(item, dict):
                kinds[str(item.get("kind") or item.get("key") or item.get("type") or "?")[:30]] += 1
    print("Diagnose excess discount: discount kinds on those files", j(dict(kinds.most_common(10))))

    rows = failing("TCS_SHORT")
    gaps = Counter()
    for r in rows:
        observed, expected = number(r.get("observed")), number(r.get("expected"))
        gaps[band(abs(expected - observed)) if observed is not None and expected is not None else "?"] += 1
    print("Diagnose TCS short:", len(rows), "| by:", j(gaps))
