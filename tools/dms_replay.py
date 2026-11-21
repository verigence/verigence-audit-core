"""What a change to Audit Core moves on the dealerships' real DMS dumps, before it is merged
(product owner 2026-11-21: three days of DMS issues found by the team on live data -- an
upload timing out, August bookings marked Inscope, bookings in Verigence shown missing).

The pull-request workflow (.github/workflows/pr-figures-check.yml) runs this script with the
code of the base branch and with the code of the pull request, on every dump in use of the
live Project, and posts which rows would come out differently: matched or not, the Journey,
the basis, Inscope / Outscope. Read only (SET TRANSACTION READ ONLY): nothing is stored.
It reports and does not block, like the figures check; a row that moves is for the author
to explain in the pull request.

The rows are named by dump day, dealership, kind and row number, and the Journey by its
reference: the repository is public, so no customer name or mobile is posted.

    python tools/dms_replay.py capture --tenant T --out side.json
    python tools/dms_replay.py diff base.json head.json --out report.md

A code version without uc03_p2_dms_dumps.plan_dump (before 2026-11-21) cannot replay; its
side is the outcome stored at the dump's last match, and the report says so.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

FIELDS = ("outcome", "basis", "journey", "dealType")


def _engine():
    from sqlalchemy import create_engine

    url = os.environ["FIGURES_DATABASE_URL"]
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix):]
    return create_engine(url, pool_pre_ping=True)


def _read_only(engine, tenant_id: str):
    from sqlalchemy import text

    from audit_core.db import set_tenant_context

    connection = engine.connect()
    transaction = connection.begin()
    connection.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(connection, tenant_id)
    return connection, transaction


_DUMPS_SQL = """
    SELECT CAST(u.dump_id AS text) AS dump_id, u.dump_kind, u.business_date, u.uploaded_at_utc,
           COALESCE(d.dealer_name, CAST(u.dealer_id AS text)) AS dealer
    FROM auditcore.p2_dms_dumps u
    LEFT JOIN auditcore.dealers d ON d.tenant_id = u.tenant_id AND d.dealer_id = u.dealer_id
    WHERE u.tenant_id = :t AND u.status = 'ACTIVE'
    ORDER BY u.business_date, dealer, u.dump_kind
"""


def capture(args) -> None:
    from sqlalchemy import text

    from audit_core import uc03_p2_dms_dumps as m

    engine = _engine()
    planned = hasattr(m, "plan_dump")
    out: dict[str, Any] = {"mode": "replayed" if planned else "stored", "dumps": {}}
    connection, transaction = _read_only(engine, args.tenant)
    try:
        dumps = connection.execute(text(_DUMPS_SQL), {"t": args.tenant}).mappings().all()
        refs = dict(connection.execute(text(
            "SELECT CAST(journey_id AS text), journey_reference FROM auditcore.journeys WHERE tenant_id = :t"),
            {"t": args.tenant}).all())
        for dump in dumps:
            started = time.monotonic()
            rows: dict[str, list[Any]] = {}
            if planned:
                plan = m.plan_dump(connection, tenant_id=args.tenant, dump_id=dump["dump_id"])
                for u in plan.updates:
                    rows[str(u["n"])] = [u["o"], u["b"], refs.get(u["j"] or "", u["j"]), u["dt"]]
            else:
                for r in connection.execute(text(
                        "SELECT row_no, outcome, match_basis, CAST(journey_id AS text) AS j, deal_type "
                        "FROM auditcore.p2_dms_dump_rows WHERE tenant_id = :t AND dump_id = :d"),
                        {"t": args.tenant, "d": dump["dump_id"]}).mappings():
                    rows[str(r["row_no"])] = [r["outcome"], r["match_basis"], refs.get(r["j"] or "", r["j"]),
                                              r["deal_type"]]
            out["dumps"][dump["dump_id"]] = {
                "label": f"{dump['business_date']:%d %b} · {dump['dealer']} · {dump['dump_kind'].title()}",
                "rows": rows, "seconds": round(time.monotonic() - started, 1)}
            print(f"{out['dumps'][dump['dump_id']]['label']}: {len(rows)} rows "
                  f"in {out['dumps'][dump['dump_id']]['seconds']} s", file=sys.stderr)
    finally:
        transaction.rollback()
        connection.close()
    with open(args.out, "w") as handle:
        json.dump(out, handle)


def _show(value: list[Any] | None) -> str:
    if value is None:
        return "—"
    outcome, basis, journey, deal = value
    return " · ".join(str(x) for x in (outcome, basis, journey, deal) if x)


def diff(args) -> None:
    with open(args.before) as handle:
        before = json.load(handle)
    with open(args.after) as handle:
        after = json.load(handle)
    lines = ["## DMS replay — every dump in use, base vs this change", ""]
    if before.get("mode") == "stored":
        lines += [("_The base code cannot replay a dump (no `plan_dump`): its side is the outcome stored at each "
                   "dump's last match, so Journeys uploaded since then also show as changes._"), ""]
    changed_rows: list[tuple[str, str, list[Any] | None, list[Any] | None]] = []
    summary = ["| Dump | Rows | Changed | Matched before → after | Seconds |", "|---|---:|---:|---|---:|"]
    for dump_id, a in after["dumps"].items():
        b = before["dumps"].get(dump_id, {"rows": {}})
        keys = sorted(set(a["rows"]) | set(b["rows"]), key=int)
        moved = [k for k in keys if a["rows"].get(k) != b["rows"].get(k)]
        matched_b = sum(1 for v in b["rows"].values() if v[0] == "MATCHED")
        matched_a = sum(1 for v in a["rows"].values() if v[0] == "MATCHED")
        summary.append(f"| {a['label']} | {len(keys)} | {len(moved)} | {matched_b} → {matched_a} | {a.get('seconds', '')} |")
        changed_rows += [(a["label"], k, b["rows"].get(k), a["rows"].get(k)) for k in moved]
    lines += summary + [""]
    if not changed_rows:
        lines.append("**No row of any dump comes out differently.**")
    else:
        lines += [f"**{len(changed_rows)} row(s) come out differently** (outcome · basis · Journey · deal type):", "",
                  "| Dump | Row | Base | This change |", "|---|---:|---|---|"]
        lines += [f"| {label} | {row} | {_show(b)} | {_show(a)} |" for label, row, b, a in changed_rows[:args.limit]]
        if len(changed_rows) > args.limit:
            lines.append(f"\n_… and {len(changed_rows) - args.limit} more in the workflow's artifact._")
    with open(args.out, "w") as handle:
        handle.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--tenant", required=True)
    c.add_argument("--out", required=True)
    d = sub.add_parser("diff")
    d.add_argument("before")
    d.add_argument("after")
    d.add_argument("--out", required=True)
    d.add_argument("--limit", type=int, default=150)
    args = parser.parse_args(argv)
    {"capture": capture, "diff": diff}[args.command](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
