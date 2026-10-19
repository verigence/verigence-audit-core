"""READ-ONLY. Shows what masters JBR-01 has and which other Mahindra projects could be a copy source."""
from __future__ import annotations

import os
import re
import traceback
from pathlib import Path

import psycopg

DST = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
out = ["# JBR-01 masters (read-only)", ""]


def table(title, cols, rows):
    out.extend([f"### {title}", ""])
    if not rows:
        out.extend(["_no rows_", ""])
        return
    out.append("| " + " | ".join(cols) + " |")
    out.append("|" + "|".join(["---"] * len(cols)) + "|")
    for r in rows:
        out.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
    out.append("")


Q = [
    ("database", ["db", "user"], "SELECT current_database(), current_user", ()),
    ("JBR-01", ["tenant", "code", "name", "status"],
     "SELECT tenant_id, project_code, project_name, project_status FROM auditcore.projects WHERE tenant_id=%s", (DST,)),
    ("JBR-01 segments", ["segment"],
     "SELECT s.segment_name FROM auditcore.project_segments ps JOIN auditcore.segments s ON s.segment_id=ps.segment_id WHERE ps.tenant_id=%s", (DST,)),
    ("JBR-01 published masters", ["product versions", "price versions", "discount versions"],
     """SELECT (SELECT count(*) FROM auditcore.project_product_master_versions WHERE tenant_id=%s AND lifecycle_status='PUBLISHED'),
               (SELECT count(*) FROM auditcore.price_list_versions WHERE tenant_id=%s AND lifecycle_status='PUBLISHED'),
               (SELECT count(*) FROM auditcore.discount_policy_versions WHERE tenant_id=%s AND lifecycle_status='PUBLISHED')""",
     (DST, DST, DST)),
    ("other projects: published masters and segments", ["tenant_id", "code", "name", "status", "product versions", "price versions", "discount versions", "segments"],
     """SELECT p.tenant_id, p.project_code, p.project_name, p.project_status,
          (SELECT count(*) FROM auditcore.project_product_master_versions v WHERE v.tenant_id=p.tenant_id AND v.lifecycle_status='PUBLISHED'),
          (SELECT count(*) FROM auditcore.price_list_versions v WHERE v.tenant_id=p.tenant_id AND v.lifecycle_status='PUBLISHED'),
          (SELECT count(*) FROM auditcore.discount_policy_versions v WHERE v.tenant_id=p.tenant_id AND v.lifecycle_status='PUBLISHED'),
          (SELECT string_agg(s.segment_name, ', ' ORDER BY s.segment_name) FROM auditcore.project_segments ps JOIN auditcore.segments s ON s.segment_id=ps.segment_id WHERE ps.tenant_id=p.tenant_id)
        FROM auditcore.projects p WHERE p.tenant_id<>%s ORDER BY p.project_name""", (DST,)),
]
try:
    url = os.environ["DATABASE_URL"]
    url = re.sub(r"^postgresql\+\w+://", "postgresql://", url)
    with psycopg.connect(url, autocommit=True, options="-c default_transaction_read_only=on") as conn:
        for title, cols, sql, params in Q:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    table(title, cols, cur.fetchall())
            except Exception:
                out += [f"### {title}", "```", traceback.format_exc().strip()[-800:], "```"]
except Exception:
    out += ["```", traceback.format_exc().strip()[-1500:], "```"]
body = "\n".join(out)
Path("/tmp/report.md").write_text(body)
print(body)
with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a") as fh:
    fh.write(body)
