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

Q = [
    ("Projects: journeys and latest activity", ["code", "journeys", "first created (UTC)", "last created (UTC)"],
     """SELECT p.project_code, count(j.*), min(j.created_at_utc)::timestamp(0), max(j.created_at_utc)::timestamp(0)
        FROM auditcore.projects p LEFT JOIN auditcore.journeys j ON j.tenant_id=p.tenant_id
        GROUP BY p.project_code ORDER BY 4 DESC NULLS LAST""", ()),
    ("Page queue by status (last 48h)", ["project", "queue_status", "pages", "oldest created", "latest update", "avg attempts", "max attempts"],
     """SELECT CASE q.tenant_id WHEN %s THEN 'ORISSA' WHEN %s THEN 'JBR-01' ELSE 'other' END, q.queue_status, count(*),
               min(q.created_at_utc)::timestamp(0), max(q.updated_at_utc)::timestamp(0),
               round(avg(q.attempt_count),1), max(q.attempt_count)
        FROM auditcore.p2_document_queue q WHERE q.created_at_utc > now() - interval '48 hours'
        GROUP BY 1,2 ORDER BY 1,3 DESC""", P),
    ("Work queue by type and status (last 48h)", ["project", "work_type", "work_status", "items", "oldest created", "oldest next attempt", "max attempts"],
     """SELECT CASE w.tenant_id WHEN %s THEN 'ORISSA' WHEN %s THEN 'JBR-01' ELSE 'other' END, w.work_type, w.work_status, count(*),
               min(w.created_at_utc)::timestamp(0), min(w.next_attempt_at_utc)::timestamp(0), max(w.attempt_count)
        FROM auditcore.p2_work_queue w WHERE w.created_at_utc > now() - interval '48 hours'
        GROUP BY 1,2,3 ORDER BY 1,2,4 DESC""", P),
    ("Work items waiting right now (claimable vs leased)", ["work_type", "status", "claimable now", "leased (valid)", "lease expired", "oldest waiting (min)"],
     """SELECT work_type, work_status,
          count(*) FILTER (WHERE work_status IN ('PENDING','RETRY_WAIT') AND coalesce(next_attempt_at_utc, now()) <= now()),
          count(*) FILTER (WHERE work_status IN ('CLAIMED','PROCESSING') AND lease_expires_at_utc > now()),
          count(*) FILTER (WHERE work_status IN ('CLAIMED','PROCESSING') AND lease_expires_at_utc <= now()),
          round(extract(epoch FROM now() - min(created_at_utc) FILTER (WHERE work_status IN ('PENDING','RETRY_WAIT','CLAIMED','PROCESSING')))/60)
        FROM auditcore.p2_work_queue WHERE work_status IN ('PENDING','RETRY_WAIT','CLAIMED','PROCESSING')
        GROUP BY 1,2 ORDER BY 1,2""", ()),
    ("Most common errors on work items (last 48h)", ["work_type", "status", "count", "error (first 150 chars)"],
     """SELECT work_type, work_status, count(*), left(regexp_replace(coalesce(last_error,''), '[0-9a-f]{8}-[0-9a-f-]{27}', '<id>', 'g'), 150)
        FROM auditcore.p2_work_queue WHERE last_error IS NOT NULL AND updated_at_utc > now() - interval '48 hours'
        GROUP BY 1,2,4 ORDER BY 3 DESC LIMIT 15""", ()),
    ("Most common errors on pages (last 48h)", ["queue_status", "count", "error (first 150 chars)"],
     """SELECT queue_status, count(*), left(regexp_replace(coalesce(last_error,''), '[0-9a-f]{8}-[0-9a-f-]{27}', '<id>', 'g'), 150)
        FROM auditcore.p2_document_queue WHERE last_error IS NOT NULL AND updated_at_utc > now() - interval '48 hours'
        GROUP BY 1,3 ORDER BY 2 DESC LIMIT 15""", ()),
    ("Throughput: pages created vs finished, per 10 minutes (last 6h, UTC)", ["bucket", "pages created", "pages reached READY/NEEDS_REVIEW", "pages still not started (QUEUED)"],
     """SELECT to_char(b, 'HH24:MI'),
          (SELECT count(*) FROM auditcore.p2_document_queue q WHERE q.created_at_utc >= b AND q.created_at_utc < b + interval '10 minutes'),
          (SELECT count(*) FROM auditcore.p2_document_queue q WHERE q.queue_status IN ('READY','NEEDS_REVIEW') AND q.updated_at_utc >= b AND q.updated_at_utc < b + interval '10 minutes'),
          (SELECT count(*) FROM auditcore.p2_document_queue q WHERE q.queue_status='QUEUED' AND q.created_at_utc >= b AND q.created_at_utc < b + interval '10 minutes')
        FROM generate_series(date_trunc('hour', now() - interval '6 hours'), now(), interval '10 minutes') b
        ORDER BY b""", ()),
    ("How long a page spends in the pipeline (created -> last update) for finished pages, last 48h (seconds)", ["queue_status", "pages", "p50", "p90", "max"],
     """SELECT queue_status, count(*),
          round(percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM updated_at_utc - created_at_utc))::numeric),
          round(percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch FROM updated_at_utc - created_at_utc))::numeric),
          round(max(extract(epoch FROM updated_at_utc - created_at_utc))::numeric)
        FROM auditcore.p2_document_queue WHERE queue_status IN ('READY','NEEDS_REVIEW','FAILED','DEAD_LETTER') AND created_at_utc > now() - interval '48 hours'
        GROUP BY 1""", ()),
    ("Upload batches by status (last 48h)", ["project", "batch_status", "batches", "pages", "oldest created", "latest update"],
     """SELECT CASE tenant_id WHEN %s THEN 'ORISSA' WHEN %s THEN 'JBR-01' ELSE 'other' END, batch_status, count(*), sum(page_count),
               min(created_at_utc)::timestamp(0), max(updated_at_utc)::timestamp(0)
        FROM auditcore.p2_upload_batches WHERE created_at_utc > now() - interval '48 hours' GROUP BY 1,2 ORDER BY 1,3 DESC""", P),
    ("Orissa journeys with NO page past QUEUED (the 'extraction not started' ones)", ["journey", "created (UTC)", "batches", "batch status", "pages by queue status", "oldest page age (min)", "last error"],
     """SELECT j.journey_reference, j.created_at_utc::timestamp(0),
          (SELECT count(*) FROM auditcore.p2_upload_batches b WHERE b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id),
          (SELECT string_agg(DISTINCT b.batch_status, ',') FROM auditcore.p2_upload_batches b WHERE b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id),
          (SELECT string_agg(s.queue_status||'='||s.n, ', ') FROM (SELECT q.queue_status, count(*) n FROM auditcore.p2_document_queue q WHERE q.tenant_id=j.tenant_id AND q.journey_id=j.journey_id GROUP BY 1) s),
          (SELECT round(extract(epoch FROM now()-min(q.created_at_utc))/60) FROM auditcore.p2_document_queue q WHERE q.tenant_id=j.tenant_id AND q.journey_id=j.journey_id),
          (SELECT left(max(q.last_error),120) FROM auditcore.p2_document_queue q WHERE q.tenant_id=j.tenant_id AND q.journey_id=j.journey_id)
        FROM auditcore.journeys j
        WHERE j.tenant_id=%s AND NOT EXISTS (
          SELECT 1 FROM auditcore.p2_document_queue q WHERE q.tenant_id=j.tenant_id AND q.journey_id=j.journey_id
            AND q.queue_status NOT IN ('QUEUED','PREPARING_PAGE'))
        ORDER BY j.created_at_utc""", (ORISSA,)),
    ("Database sessions (all clients)", ["application_name", "state", "wait_event_type", "sessions", "oldest query age (s)", "oldest xact age (s)"],
     """SELECT coalesce(nullif(application_name,''),'(none)'), state, wait_event_type, count(*),
          round(max(extract(epoch FROM now()-query_start))::numeric), round(max(extract(epoch FROM now()-xact_start))::numeric)
        FROM pg_stat_activity WHERE datname=current_database() GROUP BY 1,2,3 ORDER BY 4 DESC""", ()),
    ("Longest-running queries right now", ["age (s)", "state", "wait", "query (start)"],
     """SELECT round(extract(epoch FROM now()-query_start)::numeric), state, wait_event_type||':'||coalesce(wait_event,''), left(query, 140)
        FROM pg_stat_activity WHERE datname=current_database() AND state<>'idle' AND pid<>pg_backend_pid()
        ORDER BY query_start LIMIT 8""", ()),
    ("Connection limit vs sessions", ["max_connections", "sessions now"],
     "SELECT current_setting('max_connections'), count(*) FROM pg_stat_activity", ()),
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
                out += [f"### {title}", "```", traceback.format_exc().strip()[-500:], "```"]
                conn.rollback() if not conn.autocommit else None
except Exception:
    out += ["```", traceback.format_exc().strip()[-1200:], "```"]

# ---- service logs
out += ["", "## Services found", "```", Path("/tmp/service-names.txt").read_text(errors="replace") if Path("/tmp/service-names.txt").exists() else "(none)", "```"]
PAT = re.compile(r"\[ERRO\]|\[WARN\]|Traceback|SIGTERM|SIGKILL|OOM|out of memory|Killed|429|Too Many|rate.?limit|timed? ?out|timeout|QueuePool|too many connections|lease expired|dead.?letter|RETRY|retry", re.I)
for path in sorted(glob.glob("/tmp/logs/*.log")):
    name = Path(path).stem
    lines = Path(path).read_text(errors="replace").splitlines()
    norm = collections.Counter()
    first = {}
    for l in lines:
        if PAT.search(l):
            k = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}|\d{4}-\d\d-\d\dT[\d:.]+Z|\b\d+(\.\d+)?\b", "#", redact(l, 220))
            norm[k] += 1
            first.setdefault(k, l)
    span = (lines[0][:30] if lines else "") + " … " + (lines[-1][:30] if lines else "")
    out += ["", f"## Logs: {name} ({len(lines)} lines, {span})", ""]
    if not norm:
        out.append("_no warnings or errors in the sampled window_")
        continue
    out += ["| count | pattern |", "|---|---|"]
    for k, n in norm.most_common(14):
        out.append(f"| {n} | {redact(k, 230).replace('|', '/')} |")

# ---- HTTP: slow / failing routes on the API
try:
    rows = collections.defaultdict(list)
    for line in Path("/tmp/audit-http.json").read_text(errors="replace").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        path = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}", "{id}", str(r.get("path", "")))
        rows[(r.get("method"), path[:90])].append((r.get("httpStatus") or r.get("status"), float(r.get("totalDuration") or r.get("duration") or 0)))
    table("API routes by volume (last ~6h sample)", ["method", "route", "calls", "non-2xx", "p50 ms", "p95 ms", "max ms"],
          [(m, p, len(v), sum(1 for s, _ in v if not str(s).startswith("2")),
            round(sorted(d for _, d in v)[len(v) // 2]), round(sorted(d for _, d in v)[int(len(v) * 0.95) - 1 if len(v) > 1 else 0]), round(max(d for _, d in v)))
           for (m, p), v in sorted(rows.items(), key=lambda kv: -len(kv[1]))], limit=25)
except Exception:
    out += ["", "```", traceback.format_exc().strip()[-500:], "```"]

body = "\n".join(out)
Path("/tmp/report.md").write_text(body[:60000])
print(body[:3000])
