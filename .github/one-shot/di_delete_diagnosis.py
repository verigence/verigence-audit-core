"""READ-ONLY. Which database does DI use, and which constraint makes DELETE document fail?"""
from __future__ import annotations

import collections
import json
import re
import traceback
from pathlib import Path
from urllib.parse import urlparse

import psycopg

out = ["# DI document delete 500 — diagnosis (read-only, redacted)", ""]


def rd(s, n=300):
    s = re.sub(r"postgres(ql)?(\+\w+)?://\S+", "<db-url>", s)
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", s)
    return re.sub(r"\s+", " ", s)[:n]


def kind(host: str) -> str:
    h = (host or "").lower()
    if "neon.tech" in h:
        return "NEON" + (" (pooler)" if "-pooler" in h else " (direct)")
    if "railway.internal" in h:
        return "RAILWAY internal Postgres"
    if "proxy.rlwy.net" in h or "railway.app" in h:
        return "RAILWAY public proxy Postgres"
    return "other"


urls = {}
for svc in ["di-api", "di-worker", "di-worker-v2", "verigence-audit-core", "Postgres"]:
    try:
        v = json.loads(Path(f"/tmp/vars-{svc}.json").read_text())
    except Exception:
        v = {}
    rows = []
    for k, val in sorted(v.items()):
        if isinstance(val, str) and re.match(r"postgres", val):
            p = urlparse(re.sub(r"\+\w+", "", val, count=1))
            hh = p.hostname or ""
            rows.append((k, kind(hh), hh[:60] if "neon.tech" in hh else "(railway host hidden)", (p.path or "").strip("/")))
            urls[(svc, k)] = val
    out += [f"## Database variables on `{svc}`", ""]
    out += ["| variable | provider | host | database |", "|---|---|---|---|"] + [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows] + [""]

for name, path in (("Postgres service log", "/tmp/pg.log"), ("di-api log", "/tmp/di-api.log")):
    lines = Path(path).read_text(errors="replace").splitlines() if Path(path).exists() else []
    hits = [l for l in lines if re.search(r"violates|constraint|duplicate key|foreign key|IntegrityError|deadlock|too many connections|terminating connection|could not|FATAL", l, re.I)]
    c = collections.Counter(re.sub(r"\d{4}-\d\d-\d\d[ T][\d:.]+Z?( UTC)?|\[\d+\]|\b[0-9a-f]{8}-[0-9a-f-]{27}\b|\d+", "#", rd(l, 400)) for l in hits)
    out += [f"## {name}: {len(lines)} lines, {len(hits)} matching lines", ""]
    for k, n in c.most_common(12):
        out.append(f"- {n} × `{k[:330]}`")
    out.append("")

target = None
for (svc, k), val in urls.items():
    if svc != "di-api":
        continue
    h = urlparse(re.sub(r"\+\w+", "", val, count=1)).hostname or ""
    if "railway.internal" not in h:
        target = val
        if k == "DATABASE_PUBLIC_URL":
            break
if target is None:
    out += ["## DI database", "DI's database is only reachable on Railway's private network from here; structure checks skipped.", ""]
else:
    url = re.sub(r"^postgres(ql)?(\+\w+)?://", "postgresql://", target)
    try:
        with psycopg.connect(url, autocommit=True, options="-c default_transaction_read_only=on -c statement_timeout=20000") as conn:
            def q(title, sql, cols):
                try:
                    with conn.cursor() as cur:
                        cur.execute(sql)
                        rows = cur.fetchall()
                except Exception:
                    out.extend([f"### {title}", "```", rd(traceback.format_exc()[-300:]), "```", ""])
                    return
                out.extend([f"### {title}", ""])
                out.append("| " + " | ".join(cols) + " |")
                out.append("|" + "|".join(["---"] * len(cols)) + "|")
                for r in rows[:80]:
                    out.append("| " + " | ".join(rd(str(x), 90).replace("|", "/") for x in r) + " |")
                out.append("")
            q("Server", "SELECT left(version(), 60), current_database()", ["version", "db"])
            q("Foreign keys that reference the tables a document delete touches (child -> parent)",
              """SELECT c.conrelid::regclass::text, c.conname, c.confrelid::regclass::text, c.confdeltype
                 FROM pg_constraint c
                 WHERE c.contype='f' AND c.confrelid::regclass::text IN (
                   'docintel.documents','docintel.processing_runs','docintel.processing_jobs','docintel.document_artifacts',
                   'docintel.document_capture_v2_uploads','docintel.document_capture_v2_classification_jobs')
                 ORDER BY 3,1""", ["child table", "constraint", "parent", "on delete (a=no action, c=cascade, n=set null)"])
            q("Tables with a document_id column that the delete does NOT clear (rows)",
              """SELECT relname, n_live_tup FROM pg_stat_user_tables WHERE schemaname='docintel' AND relname IN (
                   SELECT table_name FROM information_schema.columns WHERE table_schema='docintel' AND column_name='document_id')
                 AND relname NOT IN ('document_field_values','validation_results','extracted_facts','processor_invocations','processing_runs',
                   'processing_jobs','document_quality_results','document_artifacts','documents','document_capture_v2_uploads','document_capture_v2_classification_jobs')
                 ORDER BY 2 DESC""", ["table", "rows"])
            q("Sessions on the DI database", "SELECT coalesce(nullif(application_name,''),'(none)'), state, count(*) FROM pg_stat_activity WHERE datname=current_database() GROUP BY 1,2 ORDER BY 3 DESC", ["app", "state", "sessions"])
    except Exception:
        out += ["```", rd(traceback.format_exc()[-700:], 700), "```"]
Path("/tmp/report.md").write_text("\n".join(out)[:60000])
print("\n".join(out)[:1500])
