"""Read-only: on Utkal's NDCs that print Total Cost and no net value, is "Total Cost less the
discounts" what the NDC itself treats as due, and how does the money received compare with it.
Counts only; no customer details, no references, no amounts."""
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
    shape, own, charges_vs, doubt = Counter(), Counter(), Counter(), Counter()
    money_less, money_plain, direction = Counter(), Counter(), Counter()
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
        cost, receipts, balance = (_amount(fields.get(k)) for k in ("total_cost", "total_receipts", "balance_amount"))
        dealer = row["dealer"]
        if cost is None or cost <= 0:
            shape[f"{dealer}: no Total Cost either"] += 1
            continue
        shape[f"{dealer}: Total Cost printed, no net value"] += 1
        given = sum(read.discounts.values(), Decimal(0))
        charges = sum(read.components.values(), Decimal(0))
        charges_vs["Total Cost = the charge rows (Rs 10)" if abs(cost - charges) <= TEN
                   else "Total Cost = charge rows less discounts" if abs(cost - (charges - given)) <= TEN
                   else "Total Cost matches neither"] += 1
        if receipts is not None and balance is not None:
            less, plain = cost - given - receipts, cost - receipts
            own["balance = Total Cost - discounts - Total Receipts" if abs(abs(less) - abs(balance)) <= TEN and given > 0
                else "balance = Total Cost - Total Receipts (no discounts on it)" if abs(abs(plain) - abs(balance)) <= TEN and given == 0
                else "balance = Total Cost - Total Receipts (discounts NOT taken off)" if abs(abs(plain) - abs(balance)) <= TEN
                else "balance agrees with neither"] += 1
        else:
            own["Total Receipts or balance not read"] += 1
        if read.conflicts or read.unmapped:
            doubt["lines in doubt (not compared by the rule)"] += 1
            continue
        doubt["lines clear"] += 1
        if time.monotonic() - started > 400 or compared >= 260:
            money_less["not compared (time)"] += 1
            continue
        compared += 1
        try:
            with c.begin_nested():
                facts = rules._Facts(c, T, row["journey_id"])
                if facts.money.get("loanUnknown"):
                    money_less["loan amount not on file"] += 1
                    continue
                received = rules._received_in_all(facts)
        except Exception as error:  # noqa: BLE001
            money_less[f"error {type(error).__name__}"] += 1
            continue
        money_less[band(received - (cost - given))] += 1
        money_plain[band(received - cost)] += 1
        if abs(received - (cost - given)) > 1:
            direction["short" if received < cost - given else "more"] += 1
    print("Diagnose NDCs with no net value read:", j(shape))
    print("Diagnose Total Cost against the rows read:", j(charges_vs))
    print("Diagnose the NDC's own balance:", j(own))
    print("Diagnose lines:", j(doubt))
    print("Diagnose money received against Total Cost less discounts:", j(money_less))
    print("Diagnose money received against Total Cost as printed:", j(money_plain))
    print("Diagnose where it differs from Total Cost less discounts:", j(direction))
