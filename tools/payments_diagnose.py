"""Read-only: receipts counted twice across the live Project because a second copy's number
was read differently or not at all. Prints booking references, receipt numbers and amounts."""
from __future__ import annotations

import os
import re
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_journey360 import _duplicate_receipts

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=120000"})


def digits(number):
    """The running part of a receipt number: its digit groups ("BRBM/2627/04806" -> "2627/04806")."""
    groups = re.findall(r"\d+", str(number or ""))
    return "/".join(groups) if groups else None


def family(mode):
    return "CASH" if "CASH" in str(mode or "").upper() else "BANK"


with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    rows = [dict(r) for r in c.execute(text("""
        SELECT p.journey_id, j.journey_reference, p.payment_id, p.amount, p.receipt_number, p.receipt_date,
               COALESCE(p.payment_mode_code, p.payment_method_code) AS mode, p.receipt_bank_name,
               p.payment_reference, p.status_source, p.created_at_utc
        FROM auditcore.payments p
        JOIN auditcore.journeys j ON j.tenant_id = p.tenant_id AND j.journey_id = p.journey_id
        JOIN auditcore.evidence e ON e.tenant_id = p.tenant_id AND e.journey_id = p.journey_id
         AND e.di_document_id = p.source_di_document_id AND e.association_status = 'ACTIVE'
        WHERE p.tenant_id = :t AND p.status_source = 'EVIDENCE' AND p.amount > 0
        ORDER BY j.journey_reference, COALESCE(p.receipt_date, p.payment_at_utc::date) NULLS LAST, p.created_at_utc, p.payment_id
        """), {"t": T}).mappings()]
by_journey = defaultdict(list)
for r in rows:
    by_journey[r["journey_reference"]].append(r)
print("bookings with receipts:", len(by_journey), "| receipts on file:", len(rows))
already = 0
a_pairs, b_pairs = [], []
for ref, items in by_journey.items():
    dup = _duplicate_receipts(items)
    already += len(dup)
    counted = [r for r in items if r["payment_id"] not in dup]
    used = set()
    for i, first in enumerate(counted):
        for second in counted[i + 1:]:
            if second["payment_id"] in used or first["payment_id"] in used:
                continue
            if Decimal(str(first["amount"])) != Decimal(str(second["amount"])) or first["receipt_date"] is None \
                    or first["receipt_date"] != second["receipt_date"]:
                continue
            n1, n2 = first["receipt_number"], second["receipt_number"]
            if n1 and n2 and n1 != n2 and digits(n1) and digits(n1) == digits(n2):
                a_pairs.append((ref, n1, n2, first["amount"], first["receipt_date"], first["mode"], second["mode"]))
                used.add(second["payment_id"])
            elif bool(n1) != bool(n2) and family(first["mode"]) == family(second["mode"]):
                others = [r for r in counted if r is not first and r is not second
                          and Decimal(str(r["amount"])) == Decimal(str(first["amount"]))
                          and r["receipt_date"] == first["receipt_date"]]
                b_pairs.append((ref, n1 or n2, first["amount"], first["receipt_date"], first["mode"], second["mode"],
                                first["receipt_bank_name"], second["receipt_bank_name"], len(others)))
                used.add(second["payment_id"])
print("copies already not counted (same number and amount, or same bank reference, amount and date):", already)
print()
print("A. same digits, amount and date; only the letters of the number differ:", len(a_pairs),
      "| rupees", sum(Decimal(str(p[3])) for p in a_pairs))
for p in a_pairs:
    print("  A", *p, sep=" | ")
print()
print("B. one copy with no number read; same amount, date and cash / bank as a numbered receipt:", len(b_pairs),
      "| rupees", sum(Decimal(str(p[2])) for p in b_pairs))
for p in b_pairs:
    print("  B", *p, sep=" | ")
numberless = [r for r in rows if not r["receipt_number"]]
print()
print("receipts on file with no number read:", len(numberless), "on", len({r["journey_reference"] for r in numberless}), "bookings")
