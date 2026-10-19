"""READ-ONLY. Why do the SKU resolution runs show different counts for the same Project?"""
from __future__ import annotations

import os
import re
import traceback
from pathlib import Path

import psycopg

out = ["# SKU resolution runs compared (read-only, redacted)", ""]


def rd(s, n=140):
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", str(s))
    return re.sub(r"\s+", " ", s)[:n].replace("|", "/")


def table(title, cols, rows, limit=80):
    out.extend([f"### {title}", ""])
    if not rows:
        out.extend(["_no rows_", ""])
        return
    out.append("| " + " | ".join(cols) + " |")
    out.append("|" + "|".join(["---"] * len(cols)) + "|")
    for r in rows[:limit]:
        out.append("| " + " | ".join("" if v is None else rd(v) for v in r) + " |")
    out.append("")


Q = [
    ("Runs", ["started (UTC)", "finished", "status", "total", "processed", "newly matched", "already had SKU", "still unmatched", "failed", "with SKU before", "with SKU after", "tenant"],
     """SELECT started_at_utc::timestamp(0), finished_at_utc::timestamp(0), status, journeys_total, journeys_processed, newly_matched,
               already_had_sku, still_unmatched, failed, with_sku_before, with_sku_after, left(tenant_id,8)
        FROM auditcore.sku_resolution_runs ORDER BY started_at_utc DESC LIMIT 12""", ()),
    ("Outcome counts per run, from the per-journey rows", ["run started (UTC)", "outcome", "journeys"],
     """SELECT r.started_at_utc::timestamp(0), i.outcome, count(*) FROM auditcore.sku_resolution_runs r
        JOIN auditcore.sku_resolution_run_items i ON i.tenant_id=r.tenant_id AND i.run_id=r.run_id
        GROUP BY 1,2 ORDER BY 1 DESC, 2""", ()),
    ("Journeys whose outcome differs between runs (same journey, different result)", ["journey", "run started (UTC)", "outcome", "sku", "model"],
     """WITH x AS (SELECT i.journey_id, r.started_at_utc, i.outcome, i.sku_code, i.model_label
                  FROM auditcore.sku_resolution_runs r JOIN auditcore.sku_resolution_run_items i USING (tenant_id, run_id))
        SELECT left(journey_id::text,8), started_at_utc::timestamp(0), outcome, sku_code, model_label FROM x
        WHERE journey_id IN (SELECT journey_id FROM x GROUP BY 1 HAVING count(DISTINCT outcome) > 1)
        ORDER BY journey_id, started_at_utc""", ()),
    ("Latest run: unmatched journeys by model text and reason", ["model as read", "outcome", "journeys", "reason (detail)"],
     """SELECT coalesce(model_label,'(none)'), outcome, count(*), left(coalesce(detail,''),120)
        FROM auditcore.sku_resolution_run_items i
        WHERE run_id = (SELECT run_id FROM auditcore.sku_resolution_runs WHERE journeys_processed >= journeys_total AND journeys_total > 0 ORDER BY started_at_utc DESC LIMIT 1)
          AND outcome <> 'ALREADY_HAD_SKU'
        GROUP BY 1,2,4 ORDER BY 3 DESC""", ()),
    ("Failed items (any run)", ["run started (UTC)", "journey", "model", "detail"],
     """SELECT r.started_at_utc::timestamp(0), left(i.journey_id::text,8), i.model_label, i.detail
        FROM auditcore.sku_resolution_runs r JOIN auditcore.sku_resolution_run_items i USING (tenant_id, run_id)
        WHERE i.outcome='FAILED' ORDER BY 1 DESC""", ()),
    ("Journeys of the Project now: SKU set, booking form read, journey changed since the first run", ["journeys", "with SKU", "without SKU"],
     """SELECT count(*), count(*) FILTER (WHERE jp.product_sku_id IS NOT NULL), count(*) FILTER (WHERE jp.product_sku_id IS NULL)
        FROM auditcore.journeys j LEFT JOIN auditcore.journey_products jp ON jp.tenant_id=j.tenant_id AND jp.journey_id=j.journey_id
        WHERE j.tenant_id=(SELECT tenant_id FROM auditcore.sku_resolution_runs ORDER BY started_at_utc DESC LIMIT 1)""", ()),
    ("Open model findings on this Project by match stage", ["stage", "candidates", "journeys"],
     """SELECT coalesce(safe_payload->>'matchStage','?'), coalesce(safe_payload->>'candidateCount','?'), count(*)
        FROM auditcore.audit_findings
        WHERE tenant_id=(SELECT tenant_id FROM auditcore.sku_resolution_runs ORDER BY started_at_utc DESC LIMIT 1)
          AND finding_type='MODEL_NOT_IDENTIFIED' AND coalesce(status,'OPEN') NOT IN ('RESOLVED','CLOSED')
        GROUP BY 1,2 ORDER BY 3 DESC""", ()),
]
try:
    url = re.sub(r"^postgresql\+\w+://", "postgresql://", os.environ["DATABASE_URL"])
    with psycopg.connect(url, autocommit=True, options="-c default_transaction_read_only=on -c statement_timeout=20000") as conn:
        for title, cols, sql, params in Q:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    table(title, cols, cur.fetchall())
            except Exception:
                out += [f"### {title}", "```", rd(traceback.format_exc()[-400:], 400), "```", ""]
except Exception:
    out += ["```", rd(traceback.format_exc()[-700:], 700), "```"]

lines = Path("/tmp/audit.log").read_text(errors="replace").splitlines() if Path("/tmp/audit.log").exists() else []
hits = [l for l in lines if "sync_model_resolution failed" in l or "sku_resolution" in l.lower() and ("error" in l.lower() or "fail" in l.lower())]
out += [f"### Audit Core log: {len(hits)} SKU failure lines in the last {len(lines)} lines", "```"] + [rd(l, 420) for l in hits[:6]] + ["```", ""]
Path("/tmp/report.md").write_text("\n".join(out)[:60000])
print("\n".join(out)[:1500])
