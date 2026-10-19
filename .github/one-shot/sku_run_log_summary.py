"""READ-ONLY. What happened to the SKU-resolution batch requests (status, duration), plus restarts/kills."""
from __future__ import annotations

import json
import re
from pathlib import Path

raw = Path("/tmp/audit.log").read_text(errors="replace") if Path("/tmp/audit.log").exists() else ""
http = Path("/tmp/audit-http.json").read_text(errors="replace") if Path("/tmp/audit-http.json").exists() else ""
dep = Path("/tmp/dep.json").read_text(errors="replace") if Path("/tmp/dep.json").exists() else ""
out = ["# SKU resolution run: Audit Core DEV logs (read-only, redacted)", ""]


def redact(s: str) -> str:
    s = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]+", "Bearer <redacted>", s)
    s = re.sub(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*", "<jwt>", s)
    s = re.sub(r"postgres(ql)?(\+\w+)?://\S+", "<db-url>", s)
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", s)
    return re.sub(r"\s+", " ", s)[:500]


out += ["## Deployments (latest)", "```", redact(dep)[:900], "```", ""]
rows = []
for line in http.splitlines():
    if "sku-resolution" in line:
        try:
            r = json.loads(line)
        except Exception:
            continue
        rows.append((str(r.get("timestamp", ""))[11:19], r.get("httpStatus") or r.get("status"), r.get("totalDuration") or r.get("duration"), str(r.get("path", ""))[-60:]))
out.append(f"## SKU resolution requests seen: {len(rows)}")
out += ["", "| time (UTC) | status | duration ms | path |", "|---|---|---|---|"]
out += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows[-60:]]
out += ["", "## Notable log lines (errors, kills, restarts, memory)", "```"]
n = 0
for l in raw.splitlines():
    if re.search(r"\[ERRO\]|Traceback|SIGTERM|SIGKILL|OOM|out of memory|Killed|Stopping Container|worker .* exited|timeout|OperationalError", l, re.I):
        out.append(redact(l))
        n += 1
        if n >= 40:
            break
out.append("```")
Path("/tmp/report.md").write_text("\n".join(out)[:12000])
print("\n".join(out)[:2500])
