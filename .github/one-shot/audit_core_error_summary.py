"""READ-ONLY. Summarise errors in the Railway DEV Audit Core runtime log (last 6 hours)."""
from __future__ import annotations

import collections
import json
import re
from pathlib import Path

raw = Path("/tmp/audit.log").read_text(errors="replace") if Path("/tmp/audit.log").exists() else ""
http = Path("/tmp/audit-http.json").read_text(errors="replace") if Path("/tmp/audit-http.json").exists() else ""
out = ["# Audit Core DEV: errors in the last 6 hours (read-only)", ""]


def redact(s: str) -> str:
    s = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]+", "Bearer <redacted>", s)
    s = re.sub(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*", "<jwt>", s)
    s = re.sub(r"postgres(ql)?(\+\w+)?://\S+", "<db-url>", s)
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", s)
    return s[:400]


lines = raw.splitlines()
out.append(f"runtime log lines read: {len(lines)}")
err_text = Path("/tmp/audit.err").read_text(errors="replace")[:600] if Path("/tmp/audit.err").exists() else ""
if err_text.strip():
    out += ["cli stderr: `" + redact(err_text).replace("`", "") + "`"]
errs = [l for l in lines if re.search(r"\b(ERROR|CRITICAL|Traceback|Exception|Error:)\b|\"level\": ?\"(error|critical)\"", l)]
out.append(f"error-looking lines: {len(errs)}")
out.append("")

out += ["## Log format and levels", "", "```"]
out += [redact(l) for l in lines[:3]] + ["..."] + [redact(l) for l in lines[-3:]]
lv = collections.Counter()
for l in lines:
    m = re.search(r"\b(debug|info|warning|warn|error|critical|exception)\b", l, re.I)
    lv[m.group(1).lower() if m else "none"] += 1
out += ["```", "", "level word counts: " + ", ".join(f"{k}={v}" for k, v in lv.most_common()), ""]
ev = collections.Counter()
for l in lines:
    if re.search(r"warning|error|critical|exception|fail|refused|timeout|denied|\b5\d\d\b", l, re.I):
        ev[re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "{id}", redact(l))[:220]] += 1
out += ["## Warning / failure-looking lines (count, line)", ""]
out += [f"- {n} x `{k.replace(chr(96), '')}`" for k, n in ev.most_common(40)] or ["_none_"]
out.append("")
# exception types
types = collections.Counter()
for l in lines:
    m = re.match(r"^\s*([A-Za-z_][\w.]*(Error|Exception|Violation|Warning))\b: ?(.*)", l)
    if m:
        types[(m.group(1), redact(m.group(3))[:160])] += 1
out += ["## Exceptions (count, type, message)", ""]
for (t, msg), n in types.most_common(25):
    out.append(f"- {n} x `{t}`: {msg}")
if not types:
    out.append("_none found in the plain-text log_")
out.append("")

# structured error events
events = collections.Counter()
for l in errs:
    l = l.strip()
    if l.startswith("{"):
        try:
            d = json.loads(l)
            key = (d.get("level") or d.get("severity"), d.get("event") or d.get("message") or d.get("msg"), d.get("exc_type") or d.get("error_code") or "")
            events[tuple(str(x) for x in key)] += 1
            continue
        except Exception:
            pass
    events[("text", redact(l)[:200], "")] += 1
out += ["## Error events (count, level, event, detail)", ""]
for (lv, ev, dt), n in events.most_common(30):
    out.append(f"- {n} x {lv} | {ev} | {dt}")
out.append("")

# HTTP 5xx / 4xx by path
paths = collections.Counter()
for line in http.splitlines():
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    a = d.get("attributes") or d
    st = str(a.get("httpStatus"))
    if st.startswith(("4", "5")) and st not in ("401",):
        p = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "{id}", str(a.get("path")))
        paths[(st, a.get("method"), p)] += 1
out += ["## HTTP errors by endpoint (excluding 401)", ""]
for (st, m, p), n in paths.most_common(40):
    out.append(f"- {n} x {st} {m} {p}")
if not paths:
    out.append("_none, or the HTTP log could not be read_")
out.append("")

# last 3 tracebacks, redacted
out += ["## Last tracebacks (redacted)", "", "```"]
idx = [i for i, l in enumerate(lines) if "Traceback (most recent call last)" in l]
for i in idx[-3:]:
    out += [redact(x) for x in lines[i:i + 40]]
    out.append("-----")
out.append("```")
Path("/tmp/report.md").write_text("\n".join(out)[:60000])
print("\n".join(out)[:6000])
