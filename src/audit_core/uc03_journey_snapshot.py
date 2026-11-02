"""JOURNEY_SNAPSHOT and JOURNEY_SNAPSHOT_DIFF (2026-11-02): what Journey 360
shows for every booking, kept per run, and what changed between two runs.

Why: the Deal, Payments, Add-ons, Trade-in and Audit Summary figures are
built by several modules from the same documents, and a change made for one
reported booking kept moving figures on others unseen. Before any structural
change, the screens of every booking are captured; after it, captured again;
the difference says exactly which figures moved, on how many bookings.

JOURNEY_SNAPSHOT (tag ``<tenant_id>:<label>``, reports ``["all"]`` or booking
references, optionally ``"section:<name>"`` entries to narrow the sections):

- Each section is built by the same function the Journey 360 endpoint serves
  (uc03_p2_journey360.BUILDERS), in its own READ ONLY transaction: taking a
  snapshot can never change a booking. A section that fails to build is kept
  with its error, never skipped silently.
- Rows go to auditcore.p2_journey_snapshots under the tag; taking the same tag
  again replaces it. The newest few tags of the Project are kept, older ones
  are dropped (snapshots are derived data, never a source of anything).
- Volatile keys (the time a summary was generated) are left out, so two runs
  over unchanged data are identical.

JOURNEY_SNAPSHOT_DIFF (tag ``<tenant_id>:<label>``, reports
``["<before label>", "<after label>"]``): per section, the bookings whose
JSON differs, and for each changed field path (list items aligned on their
``key`` where they have one) on how many bookings it changed, with a few
examples. Read only; the result is the job's detail.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

_JOB = "JOURNEY_SNAPSHOT"
# The money tabs (Journey 360): what a structural Deal change can move.
SECTIONS = ("deal", "invoices", "payments", "addons", "tradein", "audit-summary")
# Keys whose value is the time of building, not a fact of the booking.
_VOLATILE = frozenset({"generatedAtUtc"})
_PAUSE_S = float(os.environ.get("AUDIT_CORE_SNAPSHOT_PAUSE_S", "0.05"))
_KEEP_TAGS = int(os.environ.get("AUDIT_CORE_SNAPSHOT_KEEP_TAGS", "8"))
# The job's detail is capped (uc03_p2_worker._JOB_DETAIL_MAX): the fields that
# changed on the most bookings first, a few examples each.
_MAX_PATTERNS = 300
_REPORTED_PATTERNS = 60
_SAMPLES = 2


def parse_tag(tag: str) -> tuple[str, str]:
    tenant_id, sep, label = (tag or "").strip().partition(":")
    if not sep or not tenant_id.strip() or not label.strip():
        raise ValueError(f"{_JOB} needs the tag '<tenant_id>:<label>'")
    return tenant_id.strip(), label.strip()


def strip_volatile(value: Any) -> Any:
    """The JSON without the keys that only say when it was built."""
    if isinstance(value, dict):
        return {k: strip_volatile(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [strip_volatile(v) for v in value]
    return value


def _plain_json(value: Any) -> Any:
    """What the endpoint would send: JSON types only (Decimal, UUID, dates as text)."""
    return strip_volatile(json.loads(json.dumps(value, default=str)))


def _wanted(reports: list[str]) -> tuple[list[str], tuple[str, ...]]:
    references, sections = [], []
    for entry in reports or []:
        entry = str(entry or "").strip()
        if entry.lower().startswith("section:"):
            name = entry.split(":", 1)[1].strip()
            if name not in SECTIONS:
                raise ValueError(f"unknown section {name!r}; one of {', '.join(SECTIONS)}")
            sections.append(name)
        elif entry:
            references.append(entry)
    return references or ["all"], tuple(sections) or SECTIONS


def _journeys(engine: Engine, tenant_id: str, references: list[str]) -> list[dict[str, Any]]:
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        if references == ["all"]:
            rows = connection.execute(
                text("SELECT journey_id, journey_reference FROM auditcore.journeys WHERE tenant_id=:t "
                     "ORDER BY journey_reference, journey_id"),
                {"t": tenant_id},
            ).mappings().all()
        else:
            rows = connection.execute(
                text("SELECT journey_id, journey_reference FROM auditcore.journeys WHERE tenant_id=:t "
                     "AND journey_reference = ANY(:r) ORDER BY journey_reference"),
                {"t": tenant_id, "r": references},
            ).mappings().all()
    return [dict(r) for r in rows]


def build_section(engine: Engine, tenant_id: str, journey_id: UUID, section: str) -> tuple[Any, str | None]:
    """One Journey 360 section exactly as the endpoint builds it, in a READ
    ONLY transaction (a builder that tries to write fails here instead)."""
    from audit_core.uc03_p2_journey360 import BUILDERS

    try:
        with engine.begin() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            set_tenant_context(connection, tenant_id)
            payload = BUILDERS[section](connection, tenant_id=tenant_id, journey_id=journey_id)
        return _plain_json(payload), None
    except Exception as exc:  # noqa: BLE001 - kept on the row: a failing section is a finding
        return None, f"{type(exc).__name__}: {exc}"[:1000]


def _store(engine: Engine, tenant_id: str, tag: str, journey: dict[str, Any], rows: list[tuple[str, Any, str | None]]
           ) -> None:
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        for section, payload, error in rows:
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.p2_journey_snapshots
                      (tenant_id, tag, journey_id, journey_reference, section, payload, error)
                    VALUES (:t, :tag, :j, :ref, :s, CAST(:p AS jsonb), :e)
                    ON CONFLICT (tenant_id, tag, journey_id, section) DO UPDATE
                      SET payload=EXCLUDED.payload, error=EXCLUDED.error, taken_at_utc=now(),
                          journey_reference=EXCLUDED.journey_reference
                    """
                ),
                {"t": tenant_id, "tag": tag, "j": journey["journey_id"], "ref": journey["journey_reference"],
                 "s": section, "p": json.dumps(payload) if payload is not None else None, "e": error},
            )


def _prune(engine: Engine, tenant_id: str, keep: str) -> list[str]:
    """Drop the Project's oldest tags beyond the newest _KEEP_TAGS (never ``keep``)."""
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        tags = [r[0] for r in connection.execute(
            text("SELECT tag FROM auditcore.p2_journey_snapshots WHERE tenant_id=:t GROUP BY tag "
                 "ORDER BY max(taken_at_utc) DESC"),
            {"t": tenant_id},
        ).all()]
        old = [t for t in tags[max(_KEEP_TAGS, 1):] if t != keep]
        if old:
            connection.execute(
                text("DELETE FROM auditcore.p2_journey_snapshots WHERE tenant_id=:t AND tag = ANY(:tags)"),
                {"t": tenant_id, "tags": old},
            )
    return old


def run_journey_snapshot(engine: Engine, tag: str, reports: list[str]) -> dict[str, Any]:
    tenant_id, label = parse_tag(tag)
    references, sections = _wanted(reports)
    started = time.monotonic()
    journeys = _journeys(engine, tenant_id, references)
    built = failed = 0
    errors: list[dict[str, Any]] = []
    for number, journey in enumerate(journeys):
        rows = []
        for section in sections:
            payload, error = build_section(engine, tenant_id, journey["journey_id"], section)
            rows.append((section, payload, error))
            if error is None:
                built += 1
            else:
                failed += 1
                if len(errors) < 30:
                    errors.append({"booking": journey["journey_reference"], "section": section, "error": error})
        _store(engine, tenant_id, label, journey, rows)
        if number and number % 100 == 0:
            logger.info("journey_snapshot_progress", tenant_id=tenant_id, tag=label, done=number,
                        of=len(journeys))
        if _PAUSE_S:
            time.sleep(_PAUSE_S)
    pruned = _prune(engine, tenant_id, label)
    found = {str(j["journey_reference"]) for j in journeys}
    return {
        "tag": label, "bookings": len(journeys), "sections": list(sections), "built": built, "failed": failed,
        "errors": errors, "notFound": [r for r in references if r != "all" and r not in found],
        "seconds": round(time.monotonic() - started, 1), "prunedTags": pruned,
    }


# ── the difference between two tags ──────────────────────────────────────────
def _key_of(item: Any) -> str | None:
    if isinstance(item, dict):
        for name in ("key", "paymentId", "documentId", "code"):
            if item.get(name) not in (None, ""):
                return f"{name}={item[name]}"
    return None


def diff_paths(before: Any, after: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    """Every leaf that differs, as (path, before, after). Lists of objects
    that carry a key (a Deal line's ``key``, a receipt's ``paymentId``) are
    matched on it, so one line added does not shift every line after it."""
    if isinstance(before, dict) and isinstance(after, dict):
        out = []
        for k in sorted(set(before) | set(after), key=str):
            if k in _VOLATILE:
                continue
            out.extend(diff_paths(before.get(k), after.get(k), f"{path}.{k}" if path else str(k)))
        return out
    if isinstance(before, list) and isinstance(after, list):
        keyed_before = {_key_of(i): i for i in before}
        keyed_after = {_key_of(i): i for i in after}
        if before and after and None not in keyed_before and None not in keyed_after \
                and len(keyed_before) == len(before) and len(keyed_after) == len(after):
            out = []
            for k in sorted(set(keyed_before) | set(keyed_after), key=str):
                out.extend(diff_paths(keyed_before.get(k), keyed_after.get(k), f"{path}[{k}]"))
            return out
        if len(before) != len(after):
            return [(f"{path}[]", f"{len(before)} items", f"{len(after)} items")]
        out = []
        for i, (b, a) in enumerate(zip(before, after, strict=True)):
            out.extend(diff_paths(b, a, f"{path}[{i}]"))
        return out
    if before != after:
        return [(path, before, after)]
    return []


def _short(value: Any) -> Any:
    text_ = json.dumps(value, default=str)
    return value if len(text_) <= 120 else text_[:120] + "..."


def run_journey_snapshot_diff(engine: Engine, tag: str, reports: list[str]) -> dict[str, Any]:
    tenant_id, _ = parse_tag(tag)
    if len(reports or []) != 2:
        raise ValueError("JOURNEY_SNAPSHOT_DIFF needs reports ['<before label>', '<after label>']")
    before_tag, after_tag = (str(r).strip() for r in reports)
    sections: dict[str, Any] = {}
    with engine.begin() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        set_tenant_context(connection, tenant_id)
        counts = {r["tag"]: int(r["n"]) for r in connection.execute(
            text("SELECT tag, count(DISTINCT journey_id) n FROM auditcore.p2_journey_snapshots "
                 "WHERE tenant_id=:t AND tag = ANY(:tags) GROUP BY tag"),
            {"t": tenant_id, "tags": [before_tag, after_tag]},
        ).mappings()}
        for section in SECTIONS:
            result = connection.execution_options(stream_results=True).execute(
                text(
                    """
                    SELECT coalesce(a.journey_reference, b.journey_reference) AS ref,
                           a.payload AS before, b.payload AS after, a.error AS before_error, b.error AS after_error,
                           a.journey_id IS NULL AS only_after, b.journey_id IS NULL AS only_before
                    FROM (SELECT * FROM auditcore.p2_journey_snapshots WHERE tenant_id=:t AND tag=:a AND section=:s) a
                    FULL JOIN (SELECT * FROM auditcore.p2_journey_snapshots WHERE tenant_id=:t AND tag=:b
                               AND section=:s) b ON b.journey_id=a.journey_id
                    WHERE a.payload IS DISTINCT FROM b.payload OR a.error IS DISTINCT FROM b.error
                    """
                ),
                {"t": tenant_id, "a": before_tag, "b": after_tag, "s": section},
            )
            patterns: dict[str, dict[str, Any]] = {}
            changed = errors_now = only_one = 0
            for row in result.mappings():
                if row["only_after"] or row["only_before"]:
                    only_one += 1
                    continue
                if row["after_error"] and not row["before_error"]:
                    errors_now += 1
                changes = diff_paths(row["before"], row["after"])
                if not changes:
                    continue
                changed += 1
                seen: set[str] = set()
                for path, b, a in changes:
                    # One count per booking per pattern; indices kept, keys kept.
                    if path in seen:
                        continue
                    seen.add(path)
                    entry = patterns.get(path)
                    if entry is None:
                        if len(patterns) >= _MAX_PATTERNS:
                            continue
                        entry = patterns[path] = {"bookings": 0, "samples": []}
                    entry["bookings"] += 1
                    if len(entry["samples"]) < _SAMPLES:
                        entry["samples"].append({"booking": row["ref"], "before": _short(b), "after": _short(a)})
            sections[section] = {
                "changedBookings": changed, "newErrors": errors_now, "inOneTagOnly": only_one,
                "fields": dict(sorted(patterns.items(), key=lambda kv: (-kv[1]["bookings"], kv[0]))[
                    :_REPORTED_PATTERNS]),
                "moreFields": max(len(patterns) - _REPORTED_PATTERNS, 0),
            }
    return {"before": before_tag, "after": after_tag, "bookings": counts, "sections": sections}


__all__ = [
    "SECTIONS",
    "build_section",
    "diff_paths",
    "parse_tag",
    "run_journey_snapshot",
    "run_journey_snapshot_diff",
    "strip_volatile",
]
