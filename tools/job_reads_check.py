"""What a change to how the P2 worker reads (audit_core.job_reads) moves on real
bookings, and how many statements it saves -- measured on a COPY of the live
database, never on the live one (product owner 2026-11-20, hosting cost: cut the
expense "which doesnt impact the performance of the system").

The figures check (tools/figures_check.py) is read only, so it cannot run what
the worker does to a Journey: rebuild it from its documents, write the Deal,
run every check, raise and close findings and tasks. This script does exactly
that, the way the worker's STAGE_RECOMPUTE and CONTROL_EVALUATE jobs do, on two
identical copies of the database (two Neon branches taken at the same point) --
once with the code of the base branch, once with the code of the change -- and
then compares every row the two runs left behind for those Journeys.

    python tools/job_reads_check.py pick --tenant T --out journeys.json [--n 40]
    python tools/job_reads_check.py run  --tenant T --journeys journeys.json --out base.json
    python tools/job_reads_check.py diff base.json head.json --out report.md

It writes, so it refuses any database that does not carry the marker table
``public.job_reads_check_copy`` (created by hand on the copies only). It uses
only what both code versions have; the job scope is entered when the code under
test has one.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from functools import partial
from typing import Any
from uuid import UUID, uuid4

# Not a Journey's state: the queue's own bookkeeping and hash chains over timestamps.
_SKIP_TABLES = {"p2_work_queue", "audit_chain_heads", "idempotency_records", "p2_journey_exports"}
# Identifiers that exist before the run and mean the same thing on both copies.
_STABLE_IDS = {
    "tenant_id", "journey_id", "di_document_id", "evidence_id", "source_document_id", "source_evidence_id",
    "source_di_document_id", "outlet_id", "customer_id", "booking_id", "delivery_id", "price_list_item_id",
    "product_sku_id", "batch_id", "queue_id",
}
_TIME_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
_TIME_KEY = re.compile(r"(?:_at(?:_utc)?|At(?:Utc)?|_time|timestamp)$")
_UUID_TEXT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _engine():
    from sqlalchemy import create_engine

    url = os.environ["JOBCHECK_DATABASE_URL"]
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix):]
    return create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=1)


def _require_copy(engine) -> None:
    from sqlalchemy import text

    with engine.connect() as c:
        if not c.execute(text("SELECT to_regclass('public.job_reads_check_copy')")).scalar_one():
            raise SystemExit("Refusing to run: this database does not carry public.job_reads_check_copy, "
                             "so it is not one of the copies made for this check.")


def pick(args) -> None:
    from sqlalchemy import text

    from audit_core.db import set_tenant_context

    engine = _engine()
    _require_copy(engine)
    half = args.n // 2
    with engine.begin() as c:
        set_tenant_context(c, args.tenant)
        delivered = list(c.execute(text(
            """
            SELECT j.journey_id::text FROM auditcore.journeys j
            WHERE j.tenant_id=:t
              AND EXISTS (SELECT 1 FROM auditcore.deliveries d WHERE d.tenant_id=j.tenant_id AND d.journey_id=j.journey_id)
              AND EXISTS (SELECT 1 FROM auditcore.evidence e WHERE e.tenant_id=j.tenant_id AND e.journey_id=j.journey_id
                          AND e.association_status='ACTIVE' AND e.document_type_key='customer_ledger')
            ORDER BY md5(j.journey_id::text) LIMIT :n
            """), {"t": args.tenant, "n": half}).scalars())
        # Bookings in hand: the ones the worker is actually busy with by day.
        recent = list(c.execute(text(
            """
            SELECT r.journey_id::text FROM auditcore.p2_journey_runtime r
            WHERE r.tenant_id=:t
              AND EXISTS (SELECT 1 FROM auditcore.evidence e WHERE e.tenant_id=r.tenant_id AND e.journey_id=r.journey_id
                          AND e.association_status='ACTIVE')
            ORDER BY r.updated_at_utc DESC, r.journey_id LIMIT :n
            """), {"t": args.tenant, "n": args.n}).scalars())
    journeys = list(dict.fromkeys([*delivered, *recent]))[: args.n]
    with open(args.out, "w") as handle:
        json.dump({"journeys": journeys}, handle)
    print(f"{len(journeys)} journeys ({len(delivered)} delivered with an NDC)")


# ── what the two runs left behind ─────────────────────────────────────────────
def _plain(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _plain(v, k) for k, v in sorted(value.items()) if not _TIME_KEY.search(k)}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, str) and _TIME_TEXT.match(value):
        return "<time>"
    return value


def _capture(engine, tenant: str, journeys: list[str]) -> dict[str, dict[str, list[str]]]:
    from sqlalchemy import text

    from audit_core.db import set_tenant_context

    out: dict[str, dict[str, list[str]]] = {}
    with engine.begin() as c:
        set_tenant_context(c, tenant)
        columns = c.execute(text(
            """
            SELECT table_name, column_name, data_type FROM information_schema.columns
            WHERE table_schema='auditcore' AND table_name IN (
                SELECT table_name FROM information_schema.columns
                WHERE table_schema='auditcore' AND column_name='journey_id'
                INTERSECT
                SELECT table_name FROM information_schema.columns
                WHERE table_schema='auditcore' AND column_name='tenant_id'
                INTERSECT
                SELECT table_name FROM information_schema.tables
                WHERE table_schema='auditcore' AND table_type='BASE TABLE')
            """)).all()
        by_table: dict[str, dict[str, str]] = {}
        for table, column, kind in columns:
            by_table.setdefault(str(table), {})[str(column)] = str(kind)
        for table in sorted(by_table):
            if table in _SKIP_TABLES:
                continue
            kinds = by_table[table]
            rows = c.execute(
                text(f'SELECT t.journey_id::text AS j, to_jsonb(t) AS row FROM auditcore."{table}" t '
                     "WHERE t.tenant_id=:t AND t.journey_id::text = ANY(:js)"),
                {"t": tenant, "js": journeys},
            ).all()
            per: dict[str, list[str]] = {}
            for journey, row in rows:
                kept: dict[str, Any] = {}
                for column, value in dict(row).items():
                    kind = kinds.get(column, "")
                    if kind.startswith("timestamp") or (table.startswith("audit_") and "hash" in column):
                        continue
                    if kind == "uuid" and column not in _STABLE_IDS:
                        value = None if value is None else "<id>"
                    kept[column] = _plain(value, column)
                per.setdefault(journey, []).append(json.dumps(kept, sort_keys=True, default=str))
            out[table] = {journey: sorted(lines) for journey, lines in per.items()}
    return out


# ── the worker's jobs ─────────────────────────────────────────────────────────
def run(args) -> None:
    from sqlalchemy import event, text

    from audit_core import uc03_p2_worker as worker
    from audit_core.db import set_tenant_context
    from audit_core.uc03_p2_controls import request_control_evaluation
    from audit_core.uc03_p2_price_variance import refresh_price_variance
    from audit_core.uc03_p2_runtime import fact_fingerprint

    try:
        from audit_core import job_reads
    except ImportError:  # the base branch
        job_reads = None

    engine = _engine()
    _require_copy(engine)
    with open(args.journeys) as handle:
        journeys = json.load(handle)["journeys"]

    sent = {"statements": 0, "bytes": 0}
    asked: Counter[str] = Counter()
    measuring = {"on": False}

    def count(connection, cursor, statement, parameters, context, executemany) -> None:
        if not measuring["on"]:
            return
        sent["statements"] += 1
        sent["bytes"] += len(statement) + len(repr(parameters))
        asked[" ".join(statement.split())[:140]] += 1

    event.listen(engine, "before_cursor_execute", count)

    def as_a_job(action) -> None:
        """One worker job: what _process_in_context wraps around process_work."""
        if job_reads is None:
            action()
            return
        job_reads.install(engine)
        with job_reads.job():
            action()

    def recompute(journey_id: UUID, units: list[str]) -> None:
        # uc03_p2_worker._stage_recompute, without the queue row it owns.
        with engine.begin() as c:
            set_tenant_context(c, args.tenant)
            version_before = c.execute(
                text("SELECT fact_version FROM auditcore.p2_journey_runtime WHERE tenant_id=:t AND journey_id=:j"),
                {"t": args.tenant, "j": journey_id}).scalar_one_or_none()
            worker.settle_journey(c, tenant_id=args.tenant, journey_id=journey_id)
            refresh_price_variance(c, tenant_id=args.tenant, journey_id=journey_id)
            units.extend(request_control_evaluation(
                c, tenant_id=args.tenant, journey_id=journey_id, correlation_id="job-reads-check",
                delay_seconds=0, force=True))
            c.execute(
                text("UPDATE auditcore.p2_journey_runtime SET fact_fingerprint=:fp "
                     "WHERE tenant_id=:t AND journey_id=:j AND fact_version IS NOT DISTINCT FROM :v"),
                {"t": args.tenant, "j": journey_id, "v": version_before,
                 "fp": fact_fingerprint(c, tenant_id=args.tenant, journey_id=journey_id)})

    def evaluate(journey_id: UUID, unit: str) -> None:
        worker._control_evaluate(engine, worker.WorkItem(
            tenant_id=args.tenant, work_id=uuid4(), journey_id=journey_id, work_type="CONTROL_EVALUATE",
            work_key=f"unit:{journey_id}:{unit}", payload={"unit": unit, "force": True}, attempt_count=0,
            requested_version=None, correlation_id="job-reads-check", lease_token=uuid4()))

    errors: dict[str, list[str]] = {}
    started = time.monotonic()
    measuring["on"] = True
    for index, journey in enumerate(journeys, 1):
        journey_id = UUID(journey)
        units: list[str] = []
        steps = [("STAGE_RECOMPUTE", partial(recompute, journey_id, units))]
        while steps:
            name, action = steps.pop(0)
            try:
                as_a_job(action)
            except Exception as exc:  # noqa: BLE001 -- recorded and compared: both sides must fail alike
                errors.setdefault(journey, []).append(f"{name}: {type(exc).__name__}: {str(exc)[:200]}")
            if name == "STAGE_RECOMPUTE":
                steps = [(unit, partial(evaluate, journey_id, unit)) for unit in units]
        if index % 5 == 0:
            print(f"{index}/{len(journeys)} journeys, {sent['statements']} statements", flush=True)
    measuring["on"] = False
    seconds = round(time.monotonic() - started, 1)
    out = {
        "at": datetime.now(UTC).isoformat(), "journeys": journeys, "seconds": seconds, "errors": errors,
        "sql": {**sent, "top": asked.most_common(40)},
        "job_reads": job_reads is not None,
        "rows": _capture(engine, args.tenant, journeys),
    }
    with open(args.out, "w") as handle:
        json.dump(out, handle)
    print(f"{len(journeys)} journeys in {seconds}s: {sent['statements']} statements, "
          f"{sent['bytes'] / 1e6:.1f} MB of SQL; {len(errors)} journeys with an error")


# ── the comparison ────────────────────────────────────────────────────────────
def diff(args) -> None:
    with open(args.base) as handle:
        base = json.load(handle)
    with open(args.head) as handle:
        head = json.load(handle)
    lines = ["## Worker jobs on a copy of the live database: base against this change", ""]
    b, h = base["sql"], head["sql"]
    n = max(len(base["journeys"]), 1)
    lines += [
        f"{len(base['journeys'])} bookings, each rebuilt (STAGE_RECOMPUTE) and every check run (CONTROL_EVALUATE, forced).",
        "",
        "| | base | this change | change |", "|---|---:|---:|---:|",
        f"| Statements sent | {b['statements']:,} | {h['statements']:,} | {(h['statements'] - b['statements']) / max(b['statements'], 1):+.1%} |",
        f"| SQL sent (MB) | {b['bytes'] / 1e6:.1f} | {h['bytes'] / 1e6:.1f} | {(h['bytes'] - b['bytes']) / max(b['bytes'], 1):+.1%} |",
        f"| Statements per booking | {b['statements'] / n:,.0f} | {h['statements'] / n:,.0f} | |",
        f"| Seconds | {base['seconds']} | {head['seconds']} | |",
        "",
    ]
    differing = 0
    detail: list[str] = []
    tables = sorted(set(base["rows"]) | set(head["rows"]))
    compared = 0
    for table in tables:
        left, right = base["rows"].get(table, {}), head["rows"].get(table, {})
        for journey in sorted(set(left) | set(right)):
            a, z = left.get(journey, []), right.get(journey, [])
            compared += max(len(a), len(z))
            if a == z:
                continue
            only_base = list((Counter(a) - Counter(z)).elements())
            only_head = list((Counter(z) - Counter(a)).elements())
            differing += len(only_base) + len(only_head)
            detail.append(f"- `{table}` / `{journey}`: {len(only_base)} rows only on base, {len(only_head)} only on this change")
            for label, rows in (("base", only_base[:2]), ("head", only_head[:2])):
                detail += [f"  - {label}: `{row[:600]}`" for row in rows]
    if base["errors"] != head["errors"]:
        differing += 1
        detail.append("- the jobs' errors differ:")
        for journey in sorted(set(base["errors"]) | set(head["errors"])):
            if base["errors"].get(journey) != head["errors"].get(journey):
                detail.append(f"  - `{journey}`: base {base['errors'].get(journey)} / head {head['errors'].get(journey)}")
    lines += [f"**Rows compared: {compared:,} in {len(tables)} tables. Rows that differ: {differing:,}.**", ""]
    lines += detail[:200]
    raised = (f"Jobs that raised: base {sum(len(v) for v in base['errors'].values())}, "
              f"this change {sum(len(v) for v in head['errors'].values())}.")
    lines += ["", raised, ""]
    for label, side in (("base", b), ("this change", h)):
        lines += [f"<details><summary>Most-sent statements, {label}</summary>", "", "| times | statement |", "|---:|---|"]
        lines += [f"| {times:,} | `{statement.replace('|', '¦')}` |" for statement, times in side["top"][:25]]
        lines += ["", "</details>", ""]
    with open(args.out, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    print("\n".join(lines[:14]))
    if args.fail_on_difference and differing:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pick")
    p.add_argument("--tenant", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--n", type=int, default=40)
    p.set_defaults(func=pick)
    r = sub.add_parser("run")
    r.add_argument("--tenant", required=True)
    r.add_argument("--journeys", required=True)
    r.add_argument("--out", required=True)
    r.set_defaults(func=run)
    d = sub.add_parser("diff")
    d.add_argument("base")
    d.add_argument("head")
    d.add_argument("--out", required=True)
    d.add_argument("--fail-on-difference", action="store_true")
    d.set_defaults(func=diff)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
