"""READ-ONLY. Which DI build is deployed, did Postgres restart, and when did each constraint error occur?"""
from __future__ import annotations

import collections
import json
import re
from pathlib import Path

out = ["# DI deployments and Postgres history (read-only)", ""]


def walk(x, key):
    if isinstance(x, dict):
        if key in x and isinstance(x[key], (str, int)):
            yield x[key]
        for v in x.values():
            yield from walk(v, key)
    elif isinstance(x, list):
        for v in x:
            yield from walk(v, key)


for svc in ["di-api", "di-worker", "di-worker-v2", "Postgres"]:
    try:
        d = json.loads(Path(f"/tmp/dep-{svc}.json").read_text())
    except Exception:
        d = []
    items = d if isinstance(d, list) else d.get("deployments", [d])
    out += [f"## Deployments: {svc}", "", "| created | status | commit | branch | message |", "|---|---|---|---|---|"]
    for it in items[:8]:
        meta = it.get("meta") or {}
        out.append("| {} | {} | {} | {} | {} |".format(
            str(it.get("createdAt", ""))[:19], it.get("status", ""),
            str(meta.get("commitHash") or next(iter(walk(it, "commitHash")), ""))[:10],
            meta.get("branch") or next(iter(walk(it, "branch")), ""),
            re.sub(r"\s+", " ", str(meta.get("commitMessage") or next(iter(walk(it, "commitMessage")), "")))[:90].replace("|", "/")))
    out.append("")

pg = Path("/tmp/pg.log").read_text(errors="replace").splitlines() if Path("/tmp/pg.log").exists() else []
out += [f"## Postgres log: {len(pg)} lines; first `{pg[0][:40] if pg else ''}` last `{pg[-1][:40] if pg else ''}`", ""]
events = [l for l in pg if re.search(r"database system is ready|shutdown|shut down|starting PostgreSQL|received (fast|smart|immediate) shutdown|terminated by signal|server process .* was terminated|out of memory|No space left|PANIC|recovery|automatic recovery|could not write|could not extend", l, re.I)]
out += ["### Restart / crash / disk / memory lines (all)", "```"] + [re.sub(r"\s+", " ", l)[:230] for l in events[:60]] + ["```", ""]
if not events:
    out += ["_none in this window_", ""]

hours = collections.defaultdict(collections.Counter)
for l in pg:
    m = re.match(r"(\d{4}-\d\d-\d\d \d\d)", l.strip()) or re.search(r"(\d{4}-\d\d-\d\d \d\d):\d\d:\d\d(\.\d+)? UTC", l)
    h = m.group(1) if m else None
    if not h:
        continue
    if "fk_documents_current_processing_run" in l and "ERROR" in l:
        hours[h]["pointer FK (fk_documents_current_processing_run)"] += 1
    elif "document_classifications_tenant_id_processing_run_id_fkey" in l and "ERROR" in l:
        hours[h]["document_classifications FK"] += 1
    elif "Connection reset by peer" in l:
        hours[h]["connection reset by peer"] += 1
out += ["### Constraint errors per hour (UTC)", "", "| hour | pointer FK | classifications FK | connection reset |", "|---|---|---|---|"]
for h in sorted(hours):
    c = hours[h]
    out.append(f"| {h}:00 | {c['pointer FK (fk_documents_current_processing_run)']} | {c['document_classifications FK']} | {c['connection reset by peer']} |")
out.append("")

# the statement that goes with a pointer-FK error
for i, l in enumerate(pg):
    if "fk_documents_current_processing_run" in l and "ERROR" in l:
        out += ["### One pointer-FK error with its detail and statement", "```"] + [re.sub(r"\s+", " ", x)[:300] for x in pg[i:i + 3]] + ["```", ""]
        break

di = Path("/tmp/di-api.log").read_text(errors="replace").splitlines() if Path("/tmp/di-api.log").exists() else []
dh = collections.Counter()
for l in di:
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d)", l)
    if m and 'method="DELETE"' in l:
        dh[(m.group(1), "500" if "api_request_failed" in l else ("204" if 'status=204' in l else "other"))] += 1
out += [f"## di-api: {len(di)} lines; DELETE requests per hour", "", "| hour | outcome | count |", "|---|---|---|"]
for (h, o), n in sorted(dh.items()):
    out.append(f"| {h} | {o} | {n} |")
out.append("")
ok = [l for l in di if "capture_v2_document_deleted" in l]
out += [f"Successful deletes logged (`capture_v2_document_deleted`): {len(ok)}", ""]
Path("/tmp/report.md").write_text("\n".join(out)[:60000])
print("\n".join(out)[:1500])
