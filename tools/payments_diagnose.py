"""Read-only: what the documents read as the NDC with no net value are -- the form's own printed
title, by dealership, beside the titles of the ones with a net value. Form titles and counts only."""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict

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


def title_of(fields) -> str:
    raw = str(fields.get("statement_title") or "")
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z ]", " ", raw)).strip().upper()[:48] or "(no title read)"


with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    rows = c.execute(text(f"""
        SELECT j.journey_id, split_part(d.dealer_name, ' ', 1) AS dealer
        FROM auditcore.journeys j
        JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
        WHERE j.tenant_id=:t AND {DELIVERY_STARTED_SQL.format(t=":t", j="j.journey_id")}"""), {"t": T}).mappings().all()
    titles = defaultdict(Counter)
    di_types = defaultdict(Counter)
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
        side = "net value read" if statement_total(document["fields"], read) is not None else "NO net value"
        titles[(row["dealer"], side)][title_of(document["fields"])] += 1
        kind = c.execute(text(
            "SELECT COALESCE(source_document_type_key, '?') FROM auditcore.journey_document_extracted_fields "
            "WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d LIMIT 1"),
            {"t": T, "j": row["journey_id"], "d": document["documentId"]}).scalar()
        di_types[(row["dealer"], side)][str(kind)] += 1
    for key in sorted(titles):
        total = sum(titles[key].values())
        print(f"Diagnose {key[0]} / {key[1]} ({total}) form titles:",
              j({k: v for k, v in titles[key].most_common(8) if v >= 2 or total < 10}))
        print(f"Diagnose {key[0]} / {key[1]} read as document type:", j(dict(di_types[key].most_common(5))))
