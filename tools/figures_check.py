"""What a change to Audit Core moves on real bookings, before it is merged
(product owner 2026-11-17: "why so many mistakes ... can we do introspection").

The pull-request workflow (.github/workflows/pr-figures-check.yml) runs this script
twice on the same bookings of the live Project -- once with the code of the base
branch, once with the code of the pull request -- and posts what differs: the Deal,
the Payments tab and the Audit Summary as Journey 360 serves them, and the outcome
of every audit check. It reports and never blocks (the product owner chose "go live
and report"); a figure that moves is for the author to explain in the pull request.

Read only: every transaction is SET TRANSACTION READ ONLY, so neither version can
write to the live database. The bookings are the verified ones (p2_verified_bookings,
when the table exists) and a fixed sample of delivered bookings with an NDC.

    python tools/figures_check.py pick    --tenant T --out bookings.json [--sample 40]
    python tools/figures_check.py capture --tenant T --bookings bookings.json --out before.json
    python tools/figures_check.py diff    before.json after.json --out report.md

The script uses only what both code versions have (uc03_p2_journey360.BUILDERS and the
audit rules), so the base branch can run it as well.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Any
from uuid import UUID

SECTIONS = ("deal", "payments", "audit-summary")


def _load(path: str) -> Any:
    with open(path) as handle:
        return json.load(handle)
_VOLATILE = {"generatedAtUtc"}


def _engine():
    from sqlalchemy import create_engine

    url = os.environ["FIGURES_DATABASE_URL"]
    url = url.replace("postgresql://", "postgresql+psycopg://", 1) if url.startswith("postgresql://") else url
    url = url.replace("postgres://", "postgresql+psycopg://", 1) if url.startswith("postgres://") else url
    return create_engine(url, pool_pre_ping=True)


def _read_only(engine, tenant_id: str):
    from sqlalchemy import text

    from audit_core.db import set_tenant_context

    class _Txn:
        def __enter__(self):
            self.ctx = engine.begin()
            connection = self.ctx.__enter__()
            connection.execute(text("SET TRANSACTION READ ONLY"))
            set_tenant_context(connection, tenant_id)
            return connection

        def __exit__(self, *exc):
            return self.ctx.__exit__(*exc)

    return _Txn()


def _plain(value: Any) -> Any:
    def strip(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items() if k not in _VOLATILE}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v

    return strip(json.loads(json.dumps(value, default=str)))


def pick(args) -> None:
    from sqlalchemy import text

    engine = _engine()
    with _read_only(engine, args.tenant) as c:
        verified: list[str] = []
        if c.execute(text("SELECT to_regclass('auditcore.p2_verified_bookings')")).scalar_one():
            verified = list(c.execute(text(
                "SELECT journey_reference FROM auditcore.p2_verified_bookings "
                "WHERE tenant_id=:t AND status='ACTIVE' ORDER BY journey_reference"), {"t": args.tenant}).scalars())
        sample = list(c.execute(text(
            """
            SELECT j.journey_reference FROM auditcore.journeys j
            WHERE j.tenant_id = :t
              AND EXISTS (SELECT 1 FROM auditcore.deliveries d WHERE d.tenant_id=j.tenant_id AND d.journey_id=j.journey_id)
              AND EXISTS (SELECT 1 FROM auditcore.evidence e WHERE e.tenant_id=j.tenant_id AND e.journey_id=j.journey_id
                          AND e.association_status='ACTIVE' AND e.document_type_key='customer_ledger')
            ORDER BY md5(j.journey_id::text) LIMIT :n
            """), {"t": args.tenant, "n": args.sample}).scalars())
    bookings = list(dict.fromkeys([*verified, *sample]))
    with open(args.out, "w") as handle:
        json.dump({"verified": verified, "bookings": bookings}, handle)
    print(f"{len(bookings)} bookings ({len(verified)} verified)")


def _papers(c, tenant_id: str, journey_id: UUID) -> str:
    from sqlalchemy import text

    row = c.execute(text(
        """
        SELECT string_agg(e.di_document_id::text || ':' || coalesce(f.h, ''), ',' ORDER BY e.di_document_id)
        FROM auditcore.evidence e
        LEFT JOIN (SELECT di_document_id, md5(string_agg(field_key || '=' || coalesce(effective_value::text, ''), '|'
                                                   ORDER BY field_key)) h
                   FROM auditcore.journey_document_extracted_fields WHERE tenant_id=:t AND journey_id=:j
                   GROUP BY di_document_id) f ON f.di_document_id = e.di_document_id
        WHERE e.tenant_id=:t AND e.journey_id=:j AND e.association_status='ACTIVE'
        """), {"t": tenant_id, "j": journey_id}).scalar_one()
    return hashlib.sha256((row or "").encode()).hexdigest()[:16]


def _checks(c, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    from audit_core import uc03_p2_audit_rules as rules

    facts = rules._Facts(c, tenant_id, journey_id)
    out: dict[str, Any] = {}
    for rule in (*rules._BOOKING_RULES, *rules._DELIVERY_RULES):
        code = rule.__name__.upper()
        try:
            with c.begin_nested():
                result = rule(facts)
            out[code] = {"outcome": result.outcome, "reason": str(result.reason)[:300]}
        except Exception as exc:  # noqa: BLE001 -- recorded: a check that breaks is a finding
            out[code] = {"outcome": "ERROR", "reason": f"{type(exc).__name__}: {exc}"[:300]}
    return out


def capture(args) -> None:
    from sqlalchemy import text

    from audit_core.uc03_p2_journey360 import BUILDERS

    engine = _engine()
    wanted = _load(args.bookings)["bookings"]
    with _read_only(engine, args.tenant) as c:
        rows = c.execute(text("SELECT journey_id, journey_reference FROM auditcore.journeys "
                              "WHERE tenant_id=:t AND journey_reference = ANY(:r)"),
                         {"t": args.tenant, "r": wanted}).mappings().all()
    started = time.monotonic()
    out: dict[str, Any] = {}
    for row in sorted(rows, key=lambda r: r["journey_reference"]):
        journey_id = UUID(str(row["journey_id"]))
        item: dict[str, Any] = {}
        with _read_only(engine, args.tenant) as c:
            item["papers"] = _papers(c, args.tenant, journey_id)
        for section in SECTIONS:
            try:
                with _read_only(engine, args.tenant) as c:
                    item[section] = _plain(BUILDERS[section](c, tenant_id=args.tenant, journey_id=journey_id))
            except Exception as exc:  # noqa: BLE001
                item[section] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        try:
            with _read_only(engine, args.tenant) as c:
                item["checks"] = _checks(c, args.tenant, journey_id)
        except Exception as exc:  # noqa: BLE001
            item["checks"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        out[str(row["journey_reference"])] = item
    with open(args.out, "w") as handle:
        json.dump(out, handle)
    import audit_core

    print(f"captured {len(out)} bookings in {time.monotonic() - started:.0f}s with the code at {audit_core.__file__}")


def _diff(before: Any, after: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    try:
        from audit_core.uc03_journey_snapshot import diff_paths
    except ImportError:  # pragma: no cover
        diff_paths = None
    if diff_paths is not None:
        return diff_paths(before, after, path)
    return [] if before == after else [(path, before, after)]


def _short(value: Any) -> str:
    text_ = json.dumps(value, default=str)
    return text_ if len(text_) <= 60 else text_[:57] + "..."


def diff(args) -> None:
    before, after = _load(args.before), _load(args.after)
    verified = set()
    if args.bookings and os.path.exists(args.bookings):
        verified = set(_load(args.bookings).get("verified") or [])
    fields: dict[str, dict[str, Any]] = {}
    checks: dict[str, dict[str, Any]] = {}
    moved_bookings: set[str] = set()
    papers_moved: list[str] = []
    for ref in sorted(set(before) & set(after)):
        b, a = before[ref], after[ref]
        if b.get("papers") != a.get("papers"):
            papers_moved.append(ref)
            continue
        for section in SECTIONS:
            for path, x, y in _diff(b.get(section), a.get(section), section):
                entry = fields.setdefault(path, {"bookings": [], "sample": None})
                entry["bookings"].append(ref)
                entry["sample"] = entry["sample"] or (ref, x, y)
                moved_bookings.add(ref)
        for code in sorted(set(b.get("checks") or {}) | set(a.get("checks") or {})):
            x = ((b.get("checks") or {}).get(code) or {}).get("outcome")
            y = ((a.get("checks") or {}).get(code) or {}).get("outcome")
            if x != y:
                entry = checks.setdefault(f"{code}: {x} → {y}", {"bookings": [], "reason": None})
                entry["bookings"].append(ref)
                entry["reason"] = entry["reason"] or ((a.get("checks") or {}).get(code) or {}).get("reason")
                moved_bookings.add(ref)
    lines = ["## Figures check: what this change moves on live bookings", ""]
    compared = len(set(before) & set(after)) - len(papers_moved)
    of_verified = f", of which **{len(moved_bookings & verified)} verified**" if verified else ""
    lines.append(f"Compared **{compared}** bookings of the live Project with the base branch's code and with this "
                 f"pull request's code, read only. **{len(moved_bookings)}** moved{of_verified}.")
    if papers_moved:
        lines.append(f"Left out: {len(papers_moved)} booking(s) whose papers changed between the two runs.")
    if verified & moved_bookings:
        lines += ["", "### Verified bookings that moved", ""]
        lines += [f"- {ref}" for ref in sorted(verified & moved_bookings)]
    if checks:
        lines += ["", "### Check outcomes that change", "", "| Check | Bookings | Example |", "|---|---|---|"]
        for key, entry in sorted(checks.items(), key=lambda kv: -len(kv[1]["bookings"]))[:30]:
            example = entry["bookings"][0] + (f": {entry['reason']}" if entry["reason"] else "")
            lines.append(f"| {key} | {len(entry['bookings'])} | {example[:160].replace('|', '/')} |")
    if fields:
        lines += ["", "### Figures that change", "", "| Field | Bookings | Example (before → after) |", "|---|---|---|"]
        for path, entry in sorted(fields.items(), key=lambda kv: -len(kv[1]["bookings"]))[:40]:
            ref, x, y = entry["sample"]
            example = f"{ref}: {_short(x)} → {_short(y)}".replace("|", "/")
            lines.append(f"| `{path}` | {len(entry['bookings'])} | {example} |")
        if len(fields) > 40:
            lines.append(f"\n…and {len(fields) - 40} more fields.")
    if not checks and not fields:
        lines += ["", "No figure and no check outcome moved."]
    lines += ["", ("_Reports only; it does not block the merge. A figure that moves should be the one the change "
                   "means to move: say so in the pull request._")]
    report = "\n".join(lines)
    with open(args.out, "w") as handle:
        handle.write(report)
    print(report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pick")
    p.add_argument("--tenant", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--sample", type=int, default=40)
    c = sub.add_parser("capture")
    c.add_argument("--tenant", required=True)
    c.add_argument("--bookings", required=True)
    c.add_argument("--out", required=True)
    d = sub.add_parser("diff")
    d.add_argument("before")
    d.add_argument("after")
    d.add_argument("--out", required=True)
    d.add_argument("--bookings")
    args = parser.parse_args(argv)
    {"pick": pick, "capture": capture, "diff": diff}[args.command](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
