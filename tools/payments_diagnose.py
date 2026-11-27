"""Read-only: on the NDC forms that print Total Cost and Total Receipts and no net value (Utkal's),
what Total Receipts is made of and how the money received compares with it. Counts only."""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from decimal import Decimal

from sqlalchemy import create_engine, text

from audit_core import uc03_p2_audit_rules as rules
from audit_core.db import set_tenant_context
from audit_core.uc03_account_statement import (
    KIND_FORM,
    _amount,
    combine_statements,
    line_rows,
    load_ledger_documents,
    statement_kind,
    statement_total,
)
from audit_core.uc03_delivery_start import DELIVERY_STARTED_SQL

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=300000"})
TEN = Decimal(10)


def j(value):
    return json.dumps(value, default=str, sort_keys=True)


def band(gap: Decimal) -> str:
    gap = abs(gap)
    return ("equal (Re 1)" if gap <= 1 else "within Rs 100" if gap <= 100 else "within Rs 1,000" if gap <= 1000
            else "within Rs 10,000" if gap <= 10000 else "above Rs 10,000")


with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    rows = c.execute(text(f"""
        SELECT j.journey_id, split_part(d.dealer_name, ' ', 1) AS dealer
        FROM auditcore.journeys j
        JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
        WHERE j.tenant_id=:t AND {DELIVERY_STARTED_SQL.format(t=":t", j="j.journey_id")}
        ORDER BY md5(CAST(j.journey_id AS text))"""), {"t": T}).mappings().all()
    shape, made_of, doubt, money, direction, parts = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
    started, compared = time.monotonic(), 0
    for row in rows:
        forms = []
        for document in load_ledger_documents(c, tenant_id=T, journey_id=row["journey_id"]):
            lines = line_rows(document["fields"].get("line_items"))
            if statement_kind(lines) == KIND_FORM:
                forms.append((document, lines))
        if not forms:
            continue
        read, copies = combine_statements(forms)
        document = copies.primary
        if read is None or document is None or statement_total(document["fields"], read) is not None:
            continue
        fields = document["fields"]
        cost, printed = _amount(fields.get("total_cost")), _amount(fields.get("total_receipts"))
        dealer = row["dealer"]
        if cost is None or cost <= 0:
            shape[f"{dealer}: no Total Cost"] += 1
            continue
        if printed is None or printed <= 0:
            shape[f"{dealer}: Total Cost, no Total Receipts read"] += 1
            continue
        shape[f"{dealer}: Total Cost and Total Receipts, no net value"] += 1
        given = sum(read.discounts.values(), Decimal(0))
        received = sum((a for _, a in read.received), Decimal(0))
        loan = read.financer or Decimal(0)
        old = read.old_vehicle_value or Decimal(0)
        listed = bool(loan) and any(abs(a - loan) <= 1 for _, a in read.received)
        base = received if listed else received + loan
        made_of["Total Receipts = money lines (incl. the loan)" if abs(printed - base) <= TEN
                else "Total Receipts = money lines + discounts" if given > 0 and abs(printed - base - given) <= TEN
                else "Total Receipts = money lines + old vehicle" if old > 0 and abs(printed - base - old) <= TEN
                else "Total Receipts = money lines + discounts + old vehicle" if abs(printed - base - given - old) <= TEN and (given > 0 or old > 0)
                else "Total Receipts matches none of these"] += 1
        parts["has discounts" if given > 0 else "no discounts"] += 1
        if read.conflicts or read.unmapped:
            doubt["lines in doubt (the rule does not compare)"] += 1
            continue
        doubt["lines clear"] += 1
        if time.monotonic() - started > 420 or compared >= 280:
            money["not compared (time)"] += 1
            continue
        compared += 1
        try:
            with c.begin_nested():
                facts = rules._Facts(c, T, row["journey_id"])
                if facts.money.get("loanUnknown"):
                    money["loan amount not on file"] += 1
                    continue
                got = rules._received_in_all(facts)
        except Exception as error:  # noqa: BLE001
            money[f"error {type(error).__name__}"] += 1
            continue
        money[band(got - printed)] += 1
        if abs(got - printed) > 1:
            direction["money received is short of Total Receipts" if got < printed else "money received is more"] += 1
    print("Diagnose NDCs with no net value read:", j(shape))
    print("Diagnose what Total Receipts is made of:", j(made_of))
    print("Diagnose discounts on these NDCs:", j(parts))
    print("Diagnose lines:", j(doubt))
    print("Diagnose money received (as the Deal counts it) against the NDC's Total Receipts:", j(money))
    print("Diagnose where it differs:", j(direction))
