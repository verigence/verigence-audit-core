"""Read-only: what Utkal's NDC readings carry where no net value is read -- field names and
frequent printed row names only. No customer details, no references, no amounts."""
from __future__ import annotations

import json
import os
import re
from collections import Counter

from sqlalchemy import create_engine, text

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


def j(value):
    return json.dumps(value, default=str, sort_keys=True)


def label_of(row) -> str:
    raw = str(row.get("particulars_raw") or row.get("label") or row.get("particulars") or "")
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z+()/& ]", " ", raw)).strip().upper()[:44]


with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    rows = c.execute(text(f"""
        SELECT j.journey_id, d.dealer_name, to_char(j.created_at_utc, 'YYYY-MM') AS month
        FROM auditcore.journeys j
        JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
        WHERE j.tenant_id=:t AND d.dealer_name ILIKE 'utkal%%'
          AND {DELIVERY_STARTED_SQL.format(t=":t", j="j.journey_id")}"""), {"t": T}).mappings().all()
    with_net, without = Counter(), Counter()
    keys = {"with": Counter(), "without": Counter()}
    labels = {"with": Counter(), "without": Counter()}
    line_keys = {"with": Counter(), "without": Counter()}
    row_counts = {"with": [], "without": []}
    schema = {"with": Counter(), "without": Counter()}
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
        if read is None or document is None:
            continue
        side = "with" if statement_total(document["fields"], read) is not None else "without"
        (with_net if side == "with" else without)[row["month"]] += 1
        for key in document["fields"]:
            keys[side][key] += 1
        schema[side][str(document["fields"].get("schema_version") or document["fields"].get("document_subtype") or "?")[:30]] += 1
        lines = next(l for d, l in forms if d is document) if any(d is document for d, _ in forms) else forms[0][1]
        row_counts[side].append(len(lines))
        for line in lines:
            labels[side][label_of(line)] += 1
            for key in line:
                line_keys[side][key] += 1
    print("Diagnose Utkal NDCs with a net value read, by month uploaded:", j(with_net))
    print("Diagnose Utkal NDCs with no net value read, by month uploaded:", j(without))
    for side in ("with", "without"):
        n = len(row_counts[side]) or 1
        print(f"Diagnose Utkal [{side}] documents: {len(row_counts[side])} | rows per NDC, average: {sum(row_counts[side]) / n:.1f}")
        print(f"Diagnose Utkal [{side}] field names:", j(dict(keys[side].most_common(30))))
        print(f"Diagnose Utkal [{side}] line keys:", j(dict(line_keys[side].most_common(12))))
        print(f"Diagnose Utkal [{side}] row names printed on at least 1 in 10:",
              j({k: v for k, v in labels[side].most_common(45) if v >= max(3, n // 10)}))
