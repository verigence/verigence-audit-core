"""READ-ONLY. Where does upload -> extraction stall? Queue state, throughput, DB load, and service logs."""
from __future__ import annotations

import collections
import glob
import json
import os
import re
import traceback
from pathlib import Path

import psycopg

out = ["# Upload → extraction pipeline diagnosis (read-only, redacted)", ""]


def redact(s: str, n: int = 260) -> str:
    s = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]+", "Bearer <redacted>", s)
    s = re.sub(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*", "<jwt>", s)
    s = re.sub(r"postgres(ql)?(\+\w+)?://\S+", "<db-url>", s)
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", s)
    s = re.sub(r"\s+", " ", s)
    return s[:n]


def table(title, cols, rows, limit=60):
    out.extend([f"### {title}", ""])
    if not rows:
        out.extend(["_no rows_", ""])
        return
    out.append("| " + " | ".join(cols) + " |")
    out.append("|" + "|".join(["---"] * len(cols)) + "|")
    for r in rows[:limit]:
        out.append("| " + " | ".join("" if v is None else redact(str(v), 140).replace("|", "/") for v in r) + " |")
    out.append("")


ORISSA = "5fbe26ab-6ed7-444a-8488-5bfb81208588"
JBR01 = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
P = (ORISSA, JBR01)


import statistics
def pct(v,q):
    v=sorted(v); return round(v[min(len(v)-1,int(len(v)*q))]) if v else None

# ---- p2 worker log analysis
wl = Path("/tmp/logs/verigence-p2-worker.log").read_text(errors="replace").splitlines() if Path("/tmp/logs/verigence-p2-worker.log").exists() else []
def field(l,k):
    m=re.search(k+r'="?([^"\s]+)"?',l); return m.group(1) if m else None
dur=collections.defaultdict(list)
for l in wl:
    if "p2_work_completed" in l or "p2_work_retry_scheduled" in l or "p2_work_dead_lettered" in l or "p2_work_rescheduled" in l:
        t=field(l,"work_type"); d=field(l,"duration_ms")
        ev=field(l,"event")
        if t and d:
            try: dur[(t,ev)].append(float(d))
            except ValueError: pass
table("Worker job durations from the log (ms)", ["work_type","event","jobs","p50","p90","max","total minutes"],
      [(t,e,len(v),pct(v,.5),pct(v,.9),round(max(v)),round(sum(v)/60000,1)) for (t,e),v in sorted(dur.items(), key=lambda kv:-sum(kv[1]))])
hb=[l for l in wl if "p2_worker_heartbeat" in l]
out += ["### Worker heartbeat lines (last 12)", "```"] + [redact(l,300) for l in hb[-12:]] + ["```", ""]
dd=[l for l in wl if "p2_fragment_di_delete_failed" in l]
out += ["### One sample DI delete failure", "```"] + [redact(l,500) for l in dd[:2]] + ["```", ""]
dh=[l for l in wl if 'method="DELETE"' in l and "di-api.railway.internal" in l]
dv=[float(field(l,"duration_ms")) for l in dh if field(l,"duration_ms")]
sc=collections.Counter(field(l,"status_code") for l in dh)
out += [f"### DI DELETE calls from the worker: {len(dh)} calls, status codes {dict(sc)}, p50 {pct(dv,.5)} ms, p90 {pct(dv,.9)} ms, max {round(max(dv)) if dv else None} ms", ""]
ms=[l for l in wl if "uc03_machine_materialization_skipped" in l]
out += ["### Sample materialization-skipped line", "```"] + [redact(l,600) for l in ms[:2]] + ["```", ""]
ev=collections.Counter(field(l,"event") for l in wl)
out += ["### Worker event counts", "```"] + [f"{n:5d} {e}" for e,n in ev.most_common(25)] + ["```", ""]
# gaps between consecutive claims/heartbeats
ts=[l[:30] for l in wl if "p2_worker_heartbeat" in l]
out += ["### First/last worker lines", "```", redact(wl[0],200) if wl else "", redact(wl[-1],200) if wl else "", "```", ""]

# ---- DI api errors
dl = Path("/tmp/logs/di-api.log").read_text(errors="replace").splitlines() if Path("/tmp/logs/di-api.log").exists() else []
errs=[l for l in dl if "DATABASE_UNAVAILABLE" in l and "stack_summa" in l]
out += ["### DI API database error sample", "```"] + [redact(l,900) for l in errs[:2]] + ["```", ""]

# ---- DB: JBR-01 detail
JBR01 = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
Q = [
 ("Tenant id to name", ["tenant", "name", "status", "journeys"],
  "SELECT p.tenant_id, p.project_code, p.project_status, (SELECT count(*) FROM auditcore.journeys j WHERE j.tenant_id=p.tenant_id) FROM auditcore.projects p ORDER BY 4 DESC", ()),
 ("Pending work by journey (JBR-01): how many journeys are waiting and how many items each", ["pending items", "journeys"],
  "SELECT n, count(*) FROM (SELECT journey_id, count(*) n FROM auditcore.p2_work_queue WHERE tenant_id=%s AND work_status IN ('PENDING','RETRY_WAIT') GROUP BY 1) s GROUP BY 1 ORDER BY 1 DESC", (JBR01,)),
 ("Work now: what each in-flight lease is (JBR-01)", ["work_type", "status", "age since created (min)", "lease left (s)", "attempts", "journey"],
  "SELECT work_type, work_status, round(extract(epoch FROM now()-created_at_utc)/60), round(extract(epoch FROM lease_expires_at_utc-now())), attempt_count, left(journey_id::text,8) FROM auditcore.p2_work_queue WHERE tenant_id=%s AND work_status IN ('CLAIMED','PROCESSING') ORDER BY created_at_utc", (JBR01,)),
 ("Claim order: oldest claimable items (JBR-01) — what the worker would pick next", ["work_type", "created (UTC)", "journey", "next attempt", "status"],
  "SELECT work_type, created_at_utc::timestamp(0), left(journey_id::text,8), next_attempt_at_utc::timestamp(0), work_status FROM auditcore.p2_work_queue WHERE tenant_id=%s AND work_status IN ('PENDING','RETRY_WAIT') AND (next_attempt_at_utc IS NULL OR next_attempt_at_utc<=now()) ORDER BY created_at_utc LIMIT 12", (JBR01,)),
 ("Journeys with pages but nothing past QUEUED (JBR-01)", ["journey", "created (UTC)", "batches", "batch status", "pages by status", "oldest page (min)"],
  """SELECT left(j.journey_id::text,8), j.created_at_utc::timestamp(0),
      (SELECT count(*) FROM auditcore.p2_upload_batches b WHERE b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id),
      (SELECT string_agg(DISTINCT b.batch_status, ',') FROM auditcore.p2_upload_batches b WHERE b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id),
      (SELECT string_agg(s.queue_status||'='||s.n, ', ') FROM (SELECT q.queue_status, count(*) n FROM auditcore.p2_document_queue q WHERE q.tenant_id=j.tenant_id AND q.journey_id=j.journey_id GROUP BY 1) s),
      (SELECT round(extract(epoch FROM now()-min(q.created_at_utc))/60) FROM auditcore.p2_document_queue q WHERE q.tenant_id=j.tenant_id AND q.journey_id=j.journey_id)
     FROM auditcore.journeys j WHERE j.tenant_id=%s AND EXISTS (SELECT 1 FROM auditcore.p2_upload_batches b WHERE b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id)
      AND NOT EXISTS (SELECT 1 FROM auditcore.p2_document_queue q WHERE q.tenant_id=j.tenant_id AND q.journey_id=j.journey_id AND q.queue_status NOT IN ('QUEUED'))
     ORDER BY j.created_at_utc""", (JBR01,)),
 ("Journeys by overall extraction state (JBR-01)", ["state", "journeys"],
  """SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM auditcore.p2_document_queue q WHERE q.tenant_id=j.tenant_id AND q.journey_id=j.journey_id) THEN 'no pages uploaded'
        WHEN NOT EXISTS (SELECT 1 FROM auditcore.p2_document_queue q WHERE q.tenant_id=j.tenant_id AND q.journey_id=j.journey_id AND q.queue_status<>'QUEUED') THEN 'nothing started'
        WHEN EXISTS (SELECT 1 FROM auditcore.p2_document_queue q WHERE q.tenant_id=j.tenant_id AND q.journey_id=j.journey_id AND q.queue_status='QUEUED') THEN 'partly started'
        ELSE 'all pages started' END, count(*)
     FROM auditcore.journeys j WHERE j.tenant_id=%s GROUP BY 1 ORDER BY 2 DESC""", (JBR01,)),
 ("Worker-side DB timeouts (statement_timeout) and role setting", ["setting", "value"],
  "SELECT 'statement_timeout', current_setting('statement_timeout') UNION ALL SELECT 'idle_in_transaction_session_timeout', current_setting('idle_in_transaction_session_timeout')", ()),
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
                out += [f"### {title}", "```", traceback.format_exc().strip()[-400:], "```"]
except Exception:
    out += ["```", traceback.format_exc().strip()[-800:], "```"]
body = "\n".join(out)
Path("/tmp/report.md").write_text(body[:60000])
print(body[:2000])
