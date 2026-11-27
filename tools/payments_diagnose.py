"""Read-only: what the closure rule of 27 Nov will close, and the debit note on open tasks.
Counts only; no customer details, no journey references."""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import date

from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_delivery_start import DELIVERY_STARTED_SQL
from audit_core.uc03_p2_dates import programme_date_floor
from audit_core.uc03_p2_journey_views import SCOPED_SQL, scope_params
from audit_core.uc03_p2_tasks import task_queue_tab

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
MONTH = "2026-11"
OPEN = ("VERIFIED_COMPLETE", "CANCELLED", "FAILED", "DEAD_LETTER")
engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=300000"})


def j(value):
    return json.dumps(value, default=str, sort_keys=True)


with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    tz = c.execute(text("SELECT timezone_name FROM auditcore.projects WHERE tenant_id=:t"), {"t": T}).scalar() or "Asia/Kolkata"
    actor = c.execute(text("""
        SELECT ba.security_actor_id FROM auditcore.business_assignments ba
        WHERE ba.tenant_id=:t AND ba.assignment_status='ACTIVE' AND ba.dealer_id IS NULL
          AND ba.effective_from <= now() AND (ba.effective_to IS NULL OR ba.effective_to >= now())
        LIMIT 1"""), {"t": T}).scalar_one()
    params = {"tenant_id": T, "actor_id": actor, **scope_params(search=""),
              "date_floor": programme_date_floor() or date(1900, 1, 1), "month": MONTH,
              "month_basis": "business", "timezone": tz, "kind": "delivery"}
    november = {r["id"]: bool(r["is_closed"]) for r in c.execute(text(
        "WITH " + SCOPED_SQL + " SELECT CAST(s.journey_id AS text) AS id, s.is_closed FROM scoped s"), params).mappings()}
    print(f"Diagnose November deliveries: {len(november)} | closed now: {sum(november.values())}")

    # Every open delivery of the Project (the job's own scope).
    deliveries = {r["id"]: r["booking"] for r in c.execute(text(f"""
        SELECT CAST(j.journey_id AS text) AS id, r.booking_completion_state AS booking
        FROM auditcore.journeys j
        LEFT JOIN auditcore.p2_journey_runtime r ON r.tenant_id=j.tenant_id AND r.journey_id=j.journey_id
        WHERE j.tenant_id=:t AND COALESCE(r.delivery_completion_state, '') <> 'COMPLETE'
          AND {DELIVERY_STARTED_SQL.format(t=":t", j="j.journey_id")}"""), {"t": T}).mappings()}
    ids = list(deliveries)
    print("Diagnose open deliveries, whole project:", len(ids))
    missing: dict[str, set[str]] = {}
    for r in c.execute(text("""
        SELECT CAST(journey_id AS text) AS id, gate_status, details FROM auditcore.p2_stage_gate_state
        WHERE tenant_id=:t AND stage_code='DELIVERY' AND gate_key='REQUIRED_DOCUMENTS'
          AND CAST(journey_id AS text) = ANY(:ids)"""), {"t": T, "ids": ids}).mappings():
        missing[r["id"]] = {str(m.get("key")) for m in (r["details"] or {}).get("missing") or []}
    manual = Counter()
    for r in c.execute(text("""
        SELECT CAST(journey_id AS text) AS id, task_type, category FROM auditcore.p2_tasks
        WHERE tenant_id=:t AND CAST(journey_id AS text) = ANY(:ids) AND assigned_role_code='PC'
          AND task_status <> ALL(:closed)"""), {"t": T, "ids": ids, "closed": list(OPEN)}).mappings():
        if task_queue_tab(str(r["task_type"]), str(r["category"] or "")) == "MANUAL_VERIFICATION":
            manual[r["id"]] += 1

    def verdict(i: str) -> str:
        if i not in missing:
            return "not evaluated yet"
        left = missing[i] - {"debit_note"}
        if left:
            return "mandatory document missing"
        if manual[i]:
            return "manual verification task open"
        if deliveries[i] != "COMPLETE":
            return "booking papers incomplete"
        return "WILL CLOSE"

    whole = Counter(verdict(i) for i in ids)
    nov = Counter(verdict(i) for i in ids if i in november)
    print("Diagnose by the rule of 27 Nov, whole project:", j(whole))
    print("Diagnose by the rule of 27 Nov, November deliveries:", j(nov))
    print("Diagnose open deliveries whose only missing mandatory document is the debit note:",
          sum(1 for i in ids if missing.get(i) == {"debit_note"}))
    print("Diagnose open deliveries listing the debit note missing:", sum(1 for i in ids if "debit_note" in missing.get(i, ())))

    print("Diagnose open tasks naming the debit note, by type:", j({r[0]: r[1] for r in c.execute(text("""
        SELECT task_type, count(*) FROM auditcore.p2_tasks
        WHERE tenant_id=:t AND task_status <> ALL(:closed)
          AND (title ILIKE '%debit note%' OR description ILIKE '%debit note%' OR reference::text ILIKE '%debit_note%')
        GROUP BY 1 ORDER BY 2 DESC"""), {"t": T, "closed": list(OPEN)})}))
    row = c.execute(text("""
        SELECT count(*) AS tasks,
               count(*) FILTER (WHERE reference->'requirementKeys' ? 'debit_note') AS with_debit_note,
               count(*) FILTER (WHERE reference->'requirementKeys' = '["debit_note"]'::jsonb) AS only_debit_note
        FROM auditcore.p2_tasks WHERE tenant_id=:t AND task_type='DOCUMENT_MISSING' AND task_status <> ALL(:closed)"""),
        {"t": T, "closed": list(OPEN)}).mappings().one()
    print("Diagnose open missing-document tasks:", j(dict(row)))
