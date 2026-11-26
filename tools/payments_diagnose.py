"""Read-only: how November's deliveries stand against the two "closed" rules, and which gate holds
the open ones. Counts only; no customer details, no journey references."""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import UTC, date, datetime

from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_delivery_start import DELIVERY_STARTED_SQL
from audit_core.uc03_p2_daily_report import delivery_files_closed
from audit_core.uc03_p2_dates import programme_date_floor
from audit_core.uc03_p2_journey_views import SCOPED_SQL, scope_params

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
MONTH = "2026-11"
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
              "month_basis": "business", "timezone": tz}
    dealer_names = {str(r[0]): r[1] for r in c.execute(text(
        "SELECT dealer_id, dealer_name FROM auditcore.dealers WHERE tenant_id=:t"), {"t": T})}

    scoped = {}
    for kind in ("booking", "delivery"):
        rows = c.execute(text("WITH " + SCOPED_SQL + " SELECT CAST(s.journey_id AS text) AS id, s.is_closed, "
                              "CAST(s.dealer_id AS text) AS dealer FROM scoped s"), {**params, "kind": kind}).mappings().all()
        scoped[kind] = {r["id"]: r for r in rows}
        print(f"Diagnose {kind}s in {MONTH}: {len(rows)} | closed on the screen: {sum(1 for r in rows if r['is_closed'])}")

    deliveries = scoped["delivery"]
    ids = list(deliveries)
    runtime = {r["id"]: r for r in c.execute(text("""
        SELECT CAST(journey_id AS text) AS id, current_stage, booking_completion_state, delivery_completion_state
        FROM auditcore.p2_journey_runtime WHERE tenant_id=:t AND CAST(journey_id AS text) = ANY(:ids)"""),
        {"t": T, "ids": ids}).mappings()}
    gates: dict[str, dict[str, bool]] = {}
    missing_docs = Counter()
    for r in c.execute(text("""
        SELECT CAST(journey_id AS text) AS id, stage_code, gate_key, gate_status, details
        FROM auditcore.p2_stage_gate_state WHERE tenant_id=:t AND CAST(journey_id AS text) = ANY(:ids)"""),
            {"t": T, "ids": ids}).mappings():
        gates.setdefault(r["id"], {})[f"{r['stage_code']}:{r['gate_key']}"] = r["gate_status"] == "PASS"
        if r["stage_code"] == "DELIVERY" and r["gate_key"] == "REQUIRED_DOCUMENTS" and r["gate_status"] != "PASS" \
                and not deliveries[r["id"]]["is_closed"]:
            for m in (r["details"] or {}).get("missing") or []:
                missing_docs[str(m.get("key") if isinstance(m, dict) else m)] += 1
    stage_status = {r["id"]: r["business_status"] for r in c.execute(text("""
        SELECT CAST(journey_id AS text) AS id, business_status FROM auditcore.journey_stage_states
        WHERE tenant_id=:t AND stage_code='DELIVERY' AND CAST(journey_id AS text) = ANY(:ids)"""),
        {"t": T, "ids": ids}).mappings()}
    by_reviewer = {r[0] for r in c.execute(text("""
        SELECT DISTINCT CAST(journey_id AS text) FROM auditcore.journey_workflow_events
        WHERE tenant_id=:t AND event_type='P2_DELIVERY_COMPLETED' AND source_kind='HUMAN'
          AND CAST(journey_id AS text) = ANY(:ids)"""), {"t": T, "ids": ids})}

    tracker_closed = {f["journeyId"] for f in delivery_files_closed(
        c, tenant_id=T, actor_id="", names={}, start=datetime(2000, 1, 1, tzinfo=UTC),
        end=datetime.now(UTC), whole_project=True)["files"]}
    print("Diagnose tracker rule (PC's delivery file closed), ever, whole project:", len(tracker_closed))

    cross = Counter()
    per_dealer = Counter()
    for i, row in deliveries.items():
        a, b = bool(row["is_closed"]), i in tracker_closed
        cross[("screen closed" if a else "screen open", "tracker closed" if b else "tracker open")] += 1
        per_dealer[(dealer_names.get(row["dealer"], "?"), "screen" if a else "-", "tracker" if b else "-")] += 1
    print("Diagnose November deliveries, screen rule x tracker rule:", j({" / ".join(k): v for k, v in cross.items()}))
    print("Diagnose per dealership:", j({" / ".join(k): v for k, v in sorted(per_dealer.items())}))
    print("Diagnose screen closed: completed by a Reviewer", sum(1 for i in ids if deliveries[i]["is_closed"] and i in by_reviewer),
          "| by the rules", sum(1 for i in ids if deliveries[i]["is_closed"] and i not in by_reviewer))
    print("Diagnose delivery stage business_status:", j(Counter(stage_status.get(i, "(no row)") for i in ids)))

    order = ("BOOKING:BOOKING_FORM_EXTRACTED", "BOOKING:KYC_EXTRACTED", "BOOKING:MINIMUM_BOOKING_PAYMENT",
             "DELIVERY:REQUIRED_DOCUMENTS", "DELIVERY:VEHICLE_PROOF", "DELIVERY:PC_TASKS_CLOSED")
    failing_each = Counter()
    combos = Counter()
    booking_only = 0
    for i in ids:
        if deliveries[i]["is_closed"]:
            continue
        g = gates.get(i, {})
        failed = [k for k in order if not g.get(k, False)]
        for k in failed:
            failing_each[k] += 1
        combos[" + ".join(k.split(":")[1] for k in failed) or "ALL GATES PASS"] += 1
        if failed and all(k.startswith("BOOKING:") for k in failed):
            booking_only += 1
    print("Diagnose open November deliveries, gate not passed (a file can fail several):", j(failing_each))
    print("Diagnose open November deliveries, by combination:", j(dict(combos.most_common(12))))
    print("Diagnose open only because the booking side is incomplete:", booking_only)
    print("Diagnose open November deliveries, booking state:", j(Counter(
        (runtime.get(i) or {}).get("booking_completion_state", "(none)") for i in ids if not deliveries[i]["is_closed"])))
    print("Diagnose mandatory delivery documents missing on open November deliveries:", j(dict(missing_docs.most_common(15))))

    open_ids = [i for i in ids if not deliveries[i]["is_closed"]]
    print("Diagnose open tasks on open November deliveries by role / type:", j({f"{r[0]} / {r[1]}": r[2] for r in c.execute(text("""
        SELECT upper(COALESCE(assigned_role_code, '?')), task_type, count(*) FROM auditcore.p2_tasks
        WHERE tenant_id=:t AND CAST(journey_id AS text) = ANY(:ids)
          AND task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')
        GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 20"""), {"t": T, "ids": open_ids})}))

    row = c.execute(text(f"""
        WITH up AS (SELECT DISTINCT q.journey_id FROM auditcore.p2_document_queue q
                    WHERE q.tenant_id=:t AND q.business_stage='DELIVERY' AND q.queue_status <> 'CANCELLED')
        SELECT count(*) AS with_delivery_page,
               count(*) FILTER (WHERE NOT {DELIVERY_STARTED_SQL.format(t=":t", j="up.journey_id")}) AS not_a_delivery
        FROM up"""), {"t": T}).mappings().one()
    print("Diagnose whole project: journeys with a page filed under delivery", row["with_delivery_page"],
          "| of them not a delivery by the VIN rule", row["not_a_delivery"])
    not_delivery = c.execute(text(f"""
        SELECT count(*) FROM auditcore.journeys j WHERE j.tenant_id=:t AND CAST(j.journey_id AS text) = ANY(:ids)
          AND NOT {DELIVERY_STARTED_SQL.format(t=":t", j="j.journey_id")}"""), {"t": T, "ids": list(tracker_closed)}).scalar_one()
    print("Diagnose tracker-closed files that are not a delivery by the VIN rule:", not_delivery)
