"""READ-ONLY diagnosis: why are documents still queued and journeys still unnamed?

Prints aggregated counts and redacted log lines only. No customer names, no emails,
no tokens, no database URLs. Every database session is READ ONLY.
"""
from __future__ import annotations

import collections
import json
import re
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

RW = Path("/tmp/rw")
SERVICES = ["di-api", "di-worker", "di-worker-v2", "verigence-audit-core", "verigence-p2-worker"]


def rd(s: str, n: int = 260) -> str:
    s = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]+", "Bearer <redacted>", s)
    s = re.sub(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*", "<jwt>", s)
    s = re.sub(r"postgres(ql)?(\+\w+)?://\S+", "<db-url>", s)
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", s)
    s = re.sub(r"(?i)(key|token|secret|password)=\S+", r"\1=<redacted>", s)
    s = re.sub(r"\b\d{10}\b", "<num>", s)
    return re.sub(r"\s+", " ", s)[:n]


def norm(s: str) -> str:
    """Collapse ids/numbers/timestamps so the same error groups together."""
    s = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", s)
    s = re.sub(r"\d{4}-\d{2}-\d{2}[T ][\d:.]+Z?", "<ts>", s)
    s = re.sub(r"\d+(\.\d+)?", "N", s)
    return rd(s, 200)


def vars_of(s: str) -> dict:
    try:
        return json.loads((RW / f"vars-{s}.json").read_text() or "{}")
    except Exception:
        return {}


print("\n########## 1. RAILWAY DEPLOYMENTS (latest 15 per service)")
for s in SERVICES:
    try:
        deps = json.loads((RW / f"dep-{s}.json").read_text() or "[]")
    except Exception:
        deps = []
    print(f"\n--- {s}")
    for d in deps if isinstance(deps, list) else []:
        meta = d.get("meta") or {}
        print(" ", d.get("createdAt"), d.get("status"), (meta.get("commitHash") or "")[:8], rd(str(meta.get("commitMessage") or ""), 90))

safe_keys = ["DI_WORKER_MODE", "DI_WORKER_ENABLED", "DI_V2_CLASSIFICATION_CONCURRENCY", "DI_V2_EXTRACTION_CONCURRENCY",
             "DI_DOCAI_MOCK", "DI_DOCAI_MODEL", "DI_GEMINI_MODEL", "DI_ENV", "WORKER_LEASE_TIMEOUT_MINUTES",
             "DI_WORKER_LEASE_TIMEOUT_MINUTES", "RAILWAY_DEPLOYMENT_ID"]
print("\n########## 2. NON-SECRET WORKER SETTINGS")
for s in ["di-worker", "di-worker-v2", "verigence-p2-worker"]:
    v = vars_of(s)
    print(s, json.dumps({k: v.get(k) for k in safe_keys if k in v}, sort_keys=True))
    print("  keys mentioning MODEL/GEMINI/QUOTA/RATE/CONCURRENCY/LANE:",
          sorted(k for k in v if re.search(r"MODEL|GEMINI|QUOTA|RATE|CONCURREN|LANE|WORKER", k) and not re.search(r"KEY|TOKEN|SECRET", k)))

print("\n########## 3. LOG SUMMARY PER SERVICE (since ~17h ago)")
ERR = re.compile(r"\[ERRO\]|\bERROR\b|\"level\":\s*\"error\"|level=error|Traceback|Exception|SIGTERM|SIGKILL|OOM|out of memory|Killed|"
                 r"Stopping Container|Starting Container|429|RESOURCE_EXHAUSTED|quota|rate.?limit|deadline|timed? ?out|OperationalError|"
                 r"lease|reclaim|deferr|stale", re.I)
TS = re.compile(r"(20\d\d-\d\d-\d\dT\d\d:\d\d)")
for s in SERVICES:
    p = RW / f"log-{s}.txt"
    lines = p.read_text(errors="replace").splitlines() if p.exists() else []
    stamps = [m.group(1) for l in lines if (m := TS.search(l))]
    print(f"\n--- {s}: {len(lines)} lines; first {stamps[0] if stamps else '?'}; last {stamps[-1] if stamps else '?'}")
    per_hour = collections.Counter(t[:13] for t in stamps)
    print("  lines per hour (UTC):", dict(sorted(per_hour.items())))
    events = collections.Counter()
    for l in lines:
        for key in re.findall(r"\"(?:event|msg|message)\":\s*\"([^\"]{3,80})\"", l)[:1]:
            events[key] += 1
    if events:
        print("  top events:", events.most_common(25))
    groups: dict[str, list] = {}
    for l in lines:
        if ERR.search(l):
            k = norm(l)
            g = groups.setdefault(k, [0, None, None, l])
            g[0] += 1
            t = TS.search(l)
            if t:
                g[1] = g[1] or t.group(1)
                g[2] = t.group(1)
    print(f"  error/warning-like groups: {len(groups)}")
    for k, (n, first, last, sample) in sorted(groups.items(), key=lambda kv: -kv[1][0])[:30]:
        print(f"   x{n} [{first} .. {last}] {rd(sample, 300)}")
    if lines:
        print("  last 15 lines:")
        for l in lines[-15:]:
            print("   ", rd(l, 240))


def dsn(raw: str) -> str:
    raw = (raw or "").strip()
    for pre in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgresql+psycopg2://", "postgres://"):
        if raw.startswith(pre):
            return "postgresql://" + raw[len(pre):]
    return raw


def q(cur, title, sql, params=None):
    print(f"\n=== {title}")
    try:
        cur.execute(sql, params or {})
        rows = cur.fetchall()
        for r in rows[:80]:
            print("  ", json.dumps({k: (str(v) if v is not None else None) for k, v in r.items()}, ensure_ascii=False))
        if len(rows) > 80:
            print(f"   … {len(rows) - 80} more")
    except Exception as exc:
        print("   query failed:", rd(f"{type(exc).__name__}: {exc}", 300))
        cur.connection.rollback()
        cur.execute("SET TRANSACTION READ ONLY")


di_v = vars_of("di-worker") or vars_of("di-api")
di_url = dsn(di_v.get("DI_DATABASE_URL") or vars_of("di-api").get("DI_DATABASE_URL") or "")
au_v = vars_of("verigence-audit-core")
au_url = dsn(au_v.get("DATABASE_PUBLIC_URL") or au_v.get("DATABASE_URL") or "")
print("\n########## 4. DI DATABASE (read-only)")
print("same database as Audit Core:", bool(di_url) and di_url.split("@")[-1] == au_url.split("@")[-1])
if di_url:
    with psycopg.connect(di_url, connect_timeout=20, row_factory=dict_row, options="-c statement_timeout=60000") as conn:
        cur = conn.cursor()
        cur.execute("SET TRANSACTION READ ONLY")
        q(cur, "server now()", "SELECT now() AS now_utc")
        q(cur, "classification jobs: status x (due now / due later), last 4 days",
          """SELECT job_status, (due_at_utc <= now()) AS due_now, count(*) AS n,
                    min(created_at_utc) AS oldest_created, min(due_at_utc) AS earliest_due, max(due_at_utc) AS latest_due,
                    max(updated_at_utc) AS last_update, max(attempt_no) AS max_attempt
             FROM docintel.document_capture_v2_classification_jobs
             WHERE created_at_utc > now() - interval '4 days'
             GROUP BY 1,2 ORDER BY 1,2""")
        q(cur, "extraction/processing jobs: type x status x due now, last 4 days",
          """SELECT job_type, job_status, (due_at_utc <= now()) AS due_now, count(*) AS n,
                    min(created_at_utc) AS oldest_created, min(due_at_utc) AS earliest_due, max(due_at_utc) AS latest_due,
                    max(completed_at_utc) AS last_completed, max(attempt_no) AS max_attempt
             FROM docintel.processing_jobs
             WHERE created_at_utc > now() - interval '4 days'
             GROUP BY 1,2,3 ORDER BY 1,2,3""")
        q(cur, "RUNNING jobs and how long they have been held",
          """SELECT 'classification' AS q, locked_by, count(*) AS n, min(locked_at_utc) AS oldest_lock, max(locked_at_utc) AS newest_lock
             FROM docintel.document_capture_v2_classification_jobs WHERE job_status='RUNNING' GROUP BY 1,2
             UNION ALL
             SELECT 'processing', locked_by, count(*), min(locked_at_utc), max(locked_at_utc)
             FROM docintel.processing_jobs WHERE job_status='RUNNING' GROUP BY 1,2""")
        q(cur, "jobs completed per hour today (UTC) — when did work stop?",
          """SELECT date_trunc('hour', completed_at_utc) AS hour, 'processing' AS q, job_status, count(*) AS n
             FROM docintel.processing_jobs WHERE completed_at_utc > now() - interval '20 hours' GROUP BY 1,2,3
             UNION ALL
             SELECT date_trunc('hour', updated_at_utc), 'classification', job_status, count(*)
             FROM docintel.document_capture_v2_classification_jobs
             WHERE updated_at_utc > now() - interval '20 hours' AND job_status <> 'PENDING' GROUP BY 1,2,3
             ORDER BY 1,2,3""")
        q(cur, "last error codes on jobs touched in the last 20h",
          """SELECT 'processing' AS q, job_status, left(coalesce(error_code, ''), 60) AS error_code, count(*) AS n, max(updated_at_utc) AS last
             FROM docintel.processing_jobs WHERE updated_at_utc > now() - interval '20 hours' AND error_code IS NOT NULL GROUP BY 1,2,3
             UNION ALL
             SELECT 'classification', job_status, left(coalesce(error_code, ''), 60), count(*), max(updated_at_utc)
             FROM docintel.document_capture_v2_classification_jobs WHERE updated_at_utc > now() - interval '20 hours' AND error_code IS NOT NULL GROUP BY 1,2,3
             ORDER BY 4 DESC""")
        q(cur, "documents uploaded in last 4 days: upload x processing x confirmation x audit link",
          """SELECT upload_status, processing_status, confirmation_status, audit_link_status, count(*) AS n,
                    min(created_at_utc) AS oldest, max(created_at_utc) AS newest
             FROM docintel.documents WHERE created_at_utc > now() - interval '4 days'
             GROUP BY 1,2,3,4 ORDER BY 5 DESC""")
        q(cur, "capture v2 uploads in last 4 days by status columns",
          """SELECT * FROM (
               SELECT to_jsonb(u) - 'tenant_id' AS r FROM docintel.document_capture_v2_uploads u
               WHERE u.created_at_utc > now() - interval '4 days' LIMIT 0) x""")
        q(cur, "status-like columns available on docintel tables",
          """SELECT table_name, string_agg(column_name, ', ' ORDER BY column_name) AS cols
             FROM information_schema.columns WHERE table_schema='docintel'
               AND (column_name LIKE '%status%' OR column_name LIKE '%error%' OR column_name LIKE '%reason%')
             GROUP BY 1 ORDER BY 1""")
        q(cur, "worker heartbeats (if any table records them)",
          """SELECT table_name FROM information_schema.tables WHERE table_schema='docintel' AND table_name ILIKE '%heart%'""")

print("\n########## 5. AUDIT CORE: journeys without a customer name, and their DI documents")
if au_url:
    with psycopg.connect(au_url, connect_timeout=20, row_factory=dict_row, options="-c statement_timeout=60000") as conn:
        cur = conn.cursor()
        cur.execute("SET TRANSACTION READ ONLY")
        q(cur, "journeys per tenant: total / no customer name / created last 4 days",
          """SELECT j.tenant_id, count(*) AS journeys,
                    count(*) FILTER (WHERE coalesce(nullif(c.display_name,''), '') = '') AS no_name,
                    count(*) FILTER (WHERE j.created_at_utc > now() - interval '4 days') AS recent,
                    count(*) FILTER (WHERE j.created_at_utc > now() - interval '4 days'
                                     AND coalesce(nullif(c.display_name,''), '') = '') AS recent_no_name
             FROM auditcore.journeys j LEFT JOIN auditcore.customers c
               ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
             GROUP BY 1 ORDER BY 2 DESC""")
        q(cur, "latest SKU resolution runs",
          """SELECT tenant_id, run_id, status, started_at_utc, finished_at_utc, journeys_total, journeys_processed,
                    newly_matched, already_had_sku, still_unmatched, failed
             FROM auditcore.sku_resolution_runs ORDER BY started_at_utc DESC LIMIT 5""")
        q(cur, "latest run: outcome x detail (journey labels omitted)",
          """SELECT outcome, left(coalesce(detail,''),140) AS detail,
                    count(*) AS n,
                    count(*) FILTER (WHERE journey_label ~ '^[A-Z0-9_-]+$' OR journey_label IS NULL) AS label_looks_like_reference
             FROM auditcore.sku_resolution_run_items
             WHERE run_id = (SELECT run_id FROM auditcore.sku_resolution_runs ORDER BY started_at_utc DESC LIMIT 1)
             GROUP BY 1,2 ORDER BY 3 DESC""")
        q(cur, "auditcore tables that link to DI documents",
          """SELECT table_name, string_agg(column_name, ', ') AS cols FROM information_schema.columns
             WHERE table_schema='auditcore' AND (column_name ILIKE '%di_document%' OR column_name ILIKE 'document_id')
             GROUP BY 1 ORDER BY 1""")
print("\nDONE")
