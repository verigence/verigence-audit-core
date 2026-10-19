"""READ-ONLY. Summarise errors in the Railway DEV Audit Core runtime log (last 6 hours)."""
from __future__ import annotations

import collections
import json
import re
from pathlib import Path

raw = Path("/tmp/audit.log").read_text(errors="replace") if Path("/tmp/audit.log").exists() else ""
http = Path("/tmp/audit-http.json").read_text(errors="replace") if Path("/tmp/audit-http.json").exists() else ""
out = ["# Audit Core DEV: the 500 errors on booking-document-links (read-only, redacted)", ""]


def redact(s: str) -> str:
    s = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]+", "Bearer <redacted>", s)
    s = re.sub(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*", "<jwt>", s)
    s = re.sub(r"postgres(ql)?(\+\w+)?://\S+", "<db-url>", s)
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", s)
    s = re.sub(r'(service|environment|version|process)="[^"]*"', "", s)
    return re.sub(r"\s+", " ", s)[:700]


lines = raw.splitlines()
ids = []
for l in lines:
    if "[ERRO]" in l and "booking-document-links" in l:
        m = re.search(r'correlation_id="([^"]+)"', l)
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
out.append(f"failed requests found: {len(ids)}")
for cid in ids[-3:]:
    out += ["", "```"]
    n = 0
    for l in lines:
        if cid in l and n < 8:
            out.append(redact(l))
            n += 1
    out.append("```")
Path("/tmp/report.md").write_text("\n".join(out)[:12000])
print("\n".join(out)[:3000])
