"""Read-only: where each row of one booking's Payments tab comes from. Prints receipt
numbers, amounts, document types and what was read on each receipt; no customer details."""
from __future__ import annotations

import json
import os
import re
from uuid import UUID

from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_journey360 import payments

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
J = UUID("1e14ed84-2e7b-44ae-a8ce-c2c426f29dd6")
_PERSONAL = re.compile(r"customer|payer|received_from|address|mobile|phone|pan|aadhaar|email|father|narration|remark", re.I)


def j(value):
    return json.dumps(value, default=str, sort_keys=True)


def some(row, keys):
    return {k: row[k] for k in keys if k in row and row[k] is not None}


engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=120000"})
with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    p = {"t": T, "j": J}
    print("journey:", c.execute(text("SELECT journey_reference FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"), p).scalar())
    view = payments(c, tenant_id=T, journey_id=J)
    print("totals:", j({k: v for k, v in view.items() if k not in ("items", "bankLines") and not isinstance(v, (list, dict))}))
    for item in view["items"]:
        print("tab row:", j(some(item, ("paymentId", "receiptNumber", "amount", "receiptDate", "mode", "bank", "reference",
                                        "document", "documentId", "counted", "isLoanReceipt", "notCountedReason", "duplicateOf"))))
    print()
    rows = [dict(r) for r in c.execute(text("""
        SELECT to_jsonb(pm) AS payment, to_jsonb(e) AS evidence
        FROM auditcore.payments pm
        LEFT JOIN auditcore.evidence e ON e.tenant_id=pm.tenant_id AND e.journey_id=pm.journey_id
         AND e.di_document_id=pm.source_di_document_id
        WHERE pm.tenant_id=:t AND pm.journey_id=:j ORDER BY pm.created_at_utc"""), p).mappings()]
    if rows:
        print("payment columns:", sorted(rows[0]["payment"].keys()))
        print("evidence columns:", sorted((rows[0]["evidence"] or {}).keys()))
    documents = []
    for r in rows:
        pay, ev = r["payment"], r["evidence"] or {}
        print("payment:", j({k: v for k, v in pay.items() if v is not None and not _PERSONAL.search(k)
                             and k not in ("tenant_id", "journey_id")}))
        print("   evidence:", j({k: v for k, v in ev.items() if v is not None and not _PERSONAL.search(k)
                                 and k not in ("tenant_id", "journey_id") and not isinstance(v, (dict, list))}))
        if pay.get("source_di_document_id"):
            documents.append(pay["source_di_document_id"])
    print()
    for document in dict.fromkeys(documents):
        fields = c.execute(text("""
            SELECT field_key, effective_value, source_document_type_key, confidence_score
            FROM auditcore.journey_document_extracted_fields
            WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d ORDER BY field_key"""),
            {**p, "d": document}).mappings().all()
        shown = {f["field_key"]: f["effective_value"] for f in fields if not _PERSONAL.search(f["field_key"])
                 or f["field_key"] in ("dealer_name",)}
        print("document", document, "type", {f["source_document_type_key"] for f in fields}, "fields:", j(shown)[:1500])
    print()
    queue = [dict(r) for r in c.execute(text("""
        SELECT to_jsonb(q) AS q FROM auditcore.p2_document_queue q
        WHERE q.tenant_id=:t AND q.journey_id=:j ORDER BY q.created_at_utc"""), p).mappings()]
    if queue:
        print("queue columns:", sorted(queue[0]["q"].keys()))
    for q in queue:
        q = q["q"]
        if str(q.get("di_document_id")) in {str(d) for d in documents} or "receipt" in str(q.get("document_type_key") or q.get("di_document_type") or "").lower():
            print("queue:", j({k: v for k, v in q.items() if v is not None and not isinstance(v, (dict, list))
                               and not _PERSONAL.search(k) and k not in ("tenant_id", "journey_id")})[:900])
    print()
    for e in c.execute(text("""
        SELECT document_type_key, association_status, count(*) AS n FROM auditcore.evidence
        WHERE tenant_id=:t AND journey_id=:j GROUP BY 1, 2 ORDER BY 1, 2"""), p).mappings():
        print("evidence on booking:", dict(e))
