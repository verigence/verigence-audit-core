"""Read-only: why the NDC gives no net value on so many deliveries, by dealership, and what the
closure gate of 27 Nov says. Counts and printed row names only; no customer details, no references."""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, defaultdict
from decimal import Decimal

from sqlalchemy import create_engine, text

from audit_core import uc03_p2_audit_rules as rules
from audit_core.db import set_tenant_context
from audit_core.uc03_account_statement import (
    KIND_FORM,
    combine_statements,
    line_rows,
    load_ledger_documents,
    statement_kind,
    statement_total,
)
from audit_core.uc03_delivery_start import DELIVERY_STARTED_SQL

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=300000"})
TOTALISH = re.compile(r"TOTAL|NET|PRICE|GRAND|PAYABLE|RECEIVABLE|ON ?ROAD|FINAL|BALANCE")


def j(value):
    return json.dumps(value, default=str, sort_keys=True)


def label_of(row) -> str:
    raw = str(row.get("particulars_raw") or row.get("label") or row.get("particulars") or "")
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z+()/& ]", " ", raw)).strip().upper()[:40]


with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    print("Diagnose stage recomputes in the queue:", j({r[0]: r[1] for r in c.execute(text(
        "SELECT work_status, count(*) FROM auditcore.p2_work_queue WHERE tenant_id=:t AND work_type='STAGE_RECOMPUTE' "
        "AND updated_at_utc > now() - interval '3 hours' GROUP BY 1"), {"t": T})}))
    print("Diagnose delivery files closed, whole project:", c.execute(text(
        "SELECT count(*) FROM auditcore.journey_stage_states WHERE tenant_id=:t AND stage_code='DELIVERY' "
        "AND business_completed_at_utc IS NOT NULL"), {"t": T}).scalar_one())
    rows = c.execute(text(f"""
        SELECT j.journey_id, d.dealer_name, COALESCE(r.delivery_completion_state, '') = 'COMPLETE' AS closed
        FROM auditcore.journeys j
        LEFT JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
        LEFT JOIN auditcore.p2_journey_runtime r ON r.tenant_id=j.tenant_id AND r.journey_id=j.journey_id
        WHERE j.tenant_id=:t AND {DELIVERY_STARTED_SQL.format(t=":t", j="j.journey_id")}"""), {"t": T}).mappings().all()
    print("Diagnose deliveries:", len(rows))

    by_dealer = defaultdict(Counter)
    labels = defaultdict(Counter)
    net_field = defaultdict(Counter)
    fallback = defaultdict(Counter)
    started = time.monotonic()
    compared = 0
    for row in rows:
        dealer = str(row["dealer_name"] or "?").split()[0]
        forms = []
        for document in load_ledger_documents(c, tenant_id=T, journey_id=row["journey_id"]):
            lines = line_rows(document["fields"].get("line_items"))
            if statement_kind(lines) == KIND_FORM:
                forms.append((document, lines))
        if not forms:
            by_dealer[dealer]["no NDC read"] += 1
            continue
        read, copies = combine_statements(forms)
        document = copies.primary
        if read is None or document is None:
            by_dealer[dealer]["no NDC read"] += 1
            continue
        total = statement_total(document["fields"], read)
        if total is not None:
            by_dealer[dealer]["net value read"] += 1
            continue
        by_dealer[dealer]["NO NET VALUE"] += 1
        net_field[dealer]["net_value field present" if document["fields"].get("net_value") not in (None, "", "null")
                          else "no net_value field"] += 1
        seen = set()
        for _doc, lines in forms:
            for line in lines:
                label = label_of(line)
                if label and TOTALISH.search(label) and label not in seen:
                    seen.add(label)
                    labels[dealer][label] += 1
        if not seen:
            labels[dealer]["(no total-like row at all)"] += 1
        charges = sum(read.components.values(), Decimal(0))
        given = sum(read.discounts.values(), Decimal(0))
        if charges <= 0:
            fallback[dealer]["no charge lines read"] += 1
            continue
        if read.conflicts or read.unmapped:
            fallback[dealer]["lines in doubt"] += 1
            continue
        if time.monotonic() - started > 420 or compared >= 250:
            fallback[dealer]["not compared (time)"] += 1
            continue
        compared += 1
        try:
            with c.begin_nested():
                facts = rules._Facts(c, T, row["journey_id"])
                if facts.money.get("loanUnknown"):
                    fallback[dealer]["loan amount not on file"] += 1
                    continue
                gap = abs(rules._received_in_all(facts) - (charges - given))
        except Exception as error:  # noqa: BLE001
            fallback[dealer][f"error {type(error).__name__}"] += 1
            continue
        fallback[dealer]["sum of lines: equal (Re 1)" if gap <= 1 else "sum of lines: within Rs 100" if gap <= 100
                         else "sum of lines: within Rs 1,000" if gap <= 1000 else "sum of lines: differs more"] += 1

    for dealer in sorted(by_dealer):
        print(f"Diagnose {dealer}:", j(by_dealer[dealer]))
        if by_dealer[dealer]["NO NET VALUE"]:
            print(f"Diagnose {dealer} net_value field:", j(net_field[dealer]))
            print(f"Diagnose {dealer} total-like rows on those NDCs:", j(dict(labels[dealer].most_common(14))))
            print(f"Diagnose {dealer} money received against the sum of the NDC's lines:", j(fallback[dealer]))
