"""Read only: what migration 0259 would re-file (carried uploads -> the person who put the file in)."""
import os
import re
from sqlalchemy import create_engine, text

src = open("tools/m259.sql").read()
sql = re.search(r'op\.execute\(r"""(.*?)"""\)', src, re.S).group(1) if "op.execute" in src else src
cte = sql.split("UPDATE auditcore.p2_upload_batches ub")[0]
engine = create_engine(os.environ["DATABASE_URL"])
with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    rows = c.execute(text(cte + """
        SELECT w.tenant_id, w.was, pw.display_name AS was_name, w.uploader, pu.display_name AS to_name,
               count(*) AS uploads, min(ub.created_at_utc) AS first, max(ub.created_at_utc) AS last
        FROM who w JOIN auditcore.p2_upload_batches ub ON ub.tenant_id=w.tenant_id AND ub.batch_id=w.batch_id
        LEFT JOIN auditcore.people_names pw ON pw.actor_id=w.was
        LEFT JOIN auditcore.people_names pu ON pu.actor_id=w.uploader
        WHERE w.uploader IS NOT NULL AND w.uploader <> '' AND w.was IS DISTINCT FROM w.uploader
        GROUP BY 1,2,3,4,5 ORDER BY uploads DESC""")).mappings().all()
    print(f"would re-file {sum(r['uploads'] for r in rows)} uploads in {len(rows)} (from -> to) pairs")
    for r in rows:
        print("  ", dict(r))
    total = c.execute(text(cte + "SELECT count(*) AS carried, count(*) FILTER (WHERE uploader IS NULL) AS unknown FROM who")).mappings().one()
    print("carried uploads found:", dict(total))
