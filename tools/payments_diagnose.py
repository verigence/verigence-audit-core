"""Read-only fact base for a redesign: where the PC's and TL's tasks come from, how the uploaded
dealership receipts line up with the NDC's own payment lines and the bank, and how the insurance
source gets decided. Counts, task types and field names only; no customer details, no references."""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from decimal import Decimal

from sqlalchemy import create_engine, text

from audit_core import uc03_insurance_source as insurance
from audit_core import uc03_p2_audit_rules as rules
from audit_core.db import set_tenant_context
from audit_core.uc03_account_statement import (
    KIND_FORM,
    combine_statements,
    line_rows,
    load_ledger_documents,
    statement_kind,
)
from audit_core.uc03_delivery_start import DELIVERY_STARTED_SQL

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
OPEN = ("VERIFIED_COMPLETE", "CANCELLED", "FAILED", "DEAD_LETTER")
engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=300000"})


def j(value):
    return json.dumps(value, default=str, sort_keys=True)


with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    P = {"t": T, "closed": list(OPEN)}
    print("Diagnose open tasks by role / type:", j({f"{r[0]} / {r[1]}": r[2] for r in c.execute(text("""
        SELECT upper(COALESCE(assigned_role_code, '?')), task_type, count(*) FROM auditcore.p2_tasks
        WHERE tenant_id=:t AND task_status <> ALL(:closed) GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 28"""), P)}))
    print("Diagnose open finding tasks by check:", j({r[0]: r[1] for r in c.execute(text("""
        SELECT COALESCE(source_code, '?'), count(*) FROM auditcore.p2_tasks
        WHERE tenant_id=:t AND task_status <> ALL(:closed)
          AND task_type IN ('FINDING_REVIEW', 'FINDING_REMEDIATION', 'RULE_DISCREPANCY_REVIEW', 'PC_CONFIRMATION')
        GROUP BY 1 ORDER BY 2 DESC LIMIT 30"""), P)}))
    print("Diagnose tasks raised in the last 14 days by role:", j({r[0]: r[1] for r in c.execute(text("""
        SELECT upper(COALESCE(assigned_role_code, '?')), count(*) FROM auditcore.p2_tasks
        WHERE tenant_id=:t AND created_at_utc > now() - interval '14 days' GROUP BY 1 ORDER BY 2 DESC"""), P)}))
    print("Diagnose how tasks closed in the last 14 days ended:", j({f"{r[0]} / {r[1]}": r[2] for r in c.execute(text("""
        SELECT upper(COALESCE(assigned_role_code, '?')), COALESCE(completion_result->>'outcome', task_status), count(*)
        FROM auditcore.p2_tasks WHERE tenant_id=:t AND verified_at_utc > now() - interval '14 days'
        GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 16"""), P)}))

    fields, reasons, keys = Counter(), Counter(), Counter()
    for (reference,) in c.execute(text("""
            SELECT reference FROM auditcore.p2_tasks WHERE tenant_id=:t AND task_type='MANUAL_VERIFICATION_REVIEW'
              AND task_status <> ALL(:closed)"""), P):
        reference = reference or {}
        for key in reference:
            keys[key] += 1
        doc = str(reference.get("documentType") or reference.get("documentTypeKey") or reference.get("documentName") or "?")
        for item in reference.get("fields") or reference.get("fieldKeys") or []:
            if isinstance(item, dict):
                fields[f"{doc} / {item.get('fieldKey')}"] += 1
                for reason in item.get("reasons") or ["LOW_CONFIDENCE"]:
                    reasons[str(reason)] += 1
            else:
                fields[f"{doc} / {item}"] += 1
    print("Diagnose manual verification task reference keys:", j(dict(keys.most_common(14))))
    print("Diagnose manual verification, fields waiting (document / field):", j(dict(fields.most_common(30))))
    print("Diagnose manual verification, why:", j(dict(reasons.most_common(10))))
    print("Diagnose how read values were settled, by document type (extracted fields):", j({r[0]: [r[1], r[2], r[3]] for r in c.execute(text("""
        SELECT source_document_type_key, count(*), count(*) FILTER (WHERE is_modified),
               count(*) FILTER (WHERE confidence_score < 85)
        FROM auditcore.journey_document_extracted_fields
        WHERE tenant_id=:t AND field_key IN ('amount', 'receipt_amount', 'amount_paid', 'total_amount', 'net_value',
              'total_receipts', 'total_cost', 'grand_total_amount')
        GROUP BY 1 ORDER BY 2 DESC LIMIT 12"""), {"t": T})}))

    rows = c.execute(text(f"""
        SELECT j.journey_id, split_part(d.dealer_name, ' ', 1) AS dealer
        FROM auditcore.journeys j
        JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
        WHERE j.tenant_id=:t AND {DELIVERY_STARTED_SQL.format(t=":t", j="j.journey_id")}
        ORDER BY md5(CAST(j.journey_id AS text))"""), {"t": T}).mappings().all()
    how_many, line_match, sums, bank, ins, ins_dealer = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
    receipts_total = matched_total = bank_total = 0
    started = time.monotonic()
    judged = 0
    for row in rows:
        if time.monotonic() - started > 420:
            break
        judged += 1
        facts = rules._Facts(c, T, row["journey_id"])
        receipts = facts.receipts
        n = len(receipts)
        how_many["0" if n == 0 else "1" if n == 1 else "2-3" if n <= 3 else "4-6" if n <= 6 else "7+"] += 1
        verdict = insurance.resolve(c, tenant_id=T, journey_id=row["journey_id"])
        ins[f"{verdict['decidedBy']} -> {verdict['source']}"] += 1
        if verdict["decidedBy"] in {"UNCONFIRMED", "DEFAULT"}:
            ins_dealer[row["dealer"]] += 1
        forms = []
        for document in load_ledger_documents(c, tenant_id=T, journey_id=row["journey_id"]):
            lines = line_rows(document["fields"].get("line_items"))
            if statement_kind(lines) == KIND_FORM:
                forms.append((document, lines))
        if not forms or not receipts:
            continue
        read, _copies = combine_statements(forms)
        if read is None:
            continue
        ndc_lines = [amount for _, amount in read.received]
        left = list(ndc_lines)
        hit = 0
        for receipt in receipts:
            match = next((a for a in left if abs(a - receipt["amount"]) <= 1), None)
            if match is not None:
                left.remove(match)
                hit += 1
            if receipt["counterparty"]:
                bank_total += 1
        receipts_total += n
        matched_total += hit
        line_match["every receipt is a payment line of the NDC" if hit == n and not left
                   else "every receipt is on the NDC, the NDC lists more" if hit == n
                   else "some receipts are not on the NDC" if hit else "no receipt matches an NDC line"] += 1
        gap = sum((r["amount"] for r in receipts), Decimal(0)) - sum(ndc_lines, Decimal(0))
        sums["receipts = NDC payment lines (Re 1)" if abs(gap) <= 1 else "within Rs 100" if abs(gap) <= 100
             else "receipts less than the NDC lines" if gap < 0 else "receipts more than the NDC lines"] += 1
    print("Diagnose deliveries judged:", judged, "of", len(rows))
    print("Diagnose dealership receipts counted per delivery:", j(how_many))
    print("Diagnose receipts against the NDC's own payment lines, per delivery:", j(line_match))
    print("Diagnose receipts on the NDC, one by one:", matched_total, "of", receipts_total, "| matched to the bank statement:", bank_total)
    print("Diagnose sum of receipts against the sum of the NDC's payment lines:", j(sums))
    print("Diagnose insurance source, who decided -> what:", j(dict(ins.most_common())))
    print("Diagnose insurance not decided by documents or PC, by dealership:", j(ins_dealer))
