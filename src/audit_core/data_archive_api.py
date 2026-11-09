"""SuperAdmin read of the data-archive job's run log (auditcore.archive_runs).

GET /v1/admin/archive-runs lists the run rows newest first: one row per table,
Project and run (``batchNo`` null), and with ``includeBatches=true`` also the
row each archived batch wrote with its delete. Read-only; the job itself is
started from GitHub Actions (audit_core.data_archive).

GET /v1/admin/system-health/archive is the System health "Data archive" tab
(product owner, 9 Nov 2026: "in System health need to add the status of
archive"): the runs of the last nights per Project and table, the last
successful run, rows archived to date, the size of each archived table now, the
archive settings as this service sees them (never a key or secret) and a
red / amber / green status -- red when a run of the latest night failed.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection, text

from audit_core.data_archive import ARCHIVE_RULES, ArchiveSettings, retention_days
from audit_core.db import set_platform_super_admin_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_super_admin_request,
)

router = APIRouter(tags=["data-archive"])


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


@router.get("/v1/admin/archive-runs")
def list_archive_runs(
    _admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    table: Annotated[str | None, Query(max_length=160)] = None,
    run_id: Annotated[UUID | None, Query(alias="runId")] = None,
    include_batches: Annotated[bool, Query(alias="includeBatches")] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    set_platform_super_admin_context(connection)
    rows = connection.execute(
        text(
            """
            SELECT entry_id, run_id, tenant_id, table_key, mode, batch_no, status, rule_description,
                   cutoff_utc, candidate_count, archived_count, object_keys, checksums, error,
                   started_by, started_at_utc, finished_at_utc
            FROM auditcore.archive_runs
            WHERE (CAST(:table AS varchar) IS NULL OR table_key = CAST(:table AS varchar))
              AND (CAST(:run_id AS uuid) IS NULL OR run_id = CAST(:run_id AS uuid))
              AND (CAST(:batches AS boolean) OR batch_no IS NULL)
            ORDER BY started_at_utc DESC, run_id, tenant_id NULLS FIRST, batch_no NULLS FIRST
            LIMIT :limit
            """
        ),
        {"table": table, "run_id": run_id, "batches": include_batches, "limit": limit},
    ).mappings().all()
    return {
        "items": [
            {
                "entryId": str(row["entry_id"]),
                "runId": str(row["run_id"]),
                "tenantId": row["tenant_id"],
                "table": row["table_key"],
                "mode": row["mode"],
                "batchNo": row["batch_no"],
                "status": row["status"],
                "rule": row["rule_description"],
                "cutoffUtc": _iso(row["cutoff_utc"]),
                "candidateCount": int(row["candidate_count"]),
                "archivedCount": int(row["archived_count"]),
                "objectKeys": row["object_keys"],
                "checksums": row["checksums"],
                "error": row["error"],
                "startedBy": row["started_by"],
                "startedAtUtc": _iso(row["started_at_utc"]),
                "finishedAtUtc": _iso(row["finished_at_utc"]),
            }
            for row in rows
        ]
    }


# ── System health: Data archive ───────────────────────────────────────────────
_IST = ZoneInfo("Asia/Kolkata")
STALE_AFTER = timedelta(hours=30)  # the worker archives once a night (03:00 IST)
MAX_NIGHTS = 60
_ERROR_CHARS = 200
_SECRETISH = re.compile(r"(?i)\b(secret|password|token|access[_-]?key[_-]?id|credential)s?\s*[=:]\s*\S+")


def night_of(at: datetime) -> date:
    """The IST day a run started on: the night it belongs to (the worker runs at 03:00 IST)."""
    return at.astimezone(_IST).date()


def short_error(error: str | None) -> str | None:
    """The first line of an error, at most 200 characters, with anything that looks like a
    credential masked."""
    if not error:
        return None
    first = _SECRETISH.sub(lambda m: f"{m.group(1)}=***", str(error).strip().splitlines()[0])
    return first if len(first) <= _ERROR_CHARS else first[: _ERROR_CHARS - 1] + "…"


def archive_status(runs: list[dict[str, Any]], now: datetime) -> dict[str, str]:
    """RED: a run of the latest night failed. AMBER: no run yet, none for 30 hours, or a run of
    the latest night was refused. GREEN otherwise (a night of dry runs only is green, with a note).
    ``runs``: run rows (batch rows left out) with ``status``, ``mode``, ``startedAtUtc``
    (datetime) and ``error``."""
    if not runs:
        return {"tone": "AMBER", "reason": "No archive run is recorded yet."}
    latest = max(r["startedAtUtc"] for r in runs)
    night = night_of(latest)
    tonight = [r for r in runs if night_of(r["startedAtUtc"]) == night]
    failed = [r for r in tonight if r["status"] == "FAILED"]
    if failed:
        first = short_error(failed[0].get("error")) or "no error recorded"
        return {"tone": "RED", "reason": f"{len(failed)} of {len(tonight)} runs failed on the night of "
                                         f"{night.isoformat()}: {first}. Rows not archived were kept."}
    if now - latest > STALE_AFTER:
        return {"tone": "AMBER", "reason": f"No archive run in the last 30 hours (last on {night.isoformat()})."}
    refused = [r for r in tonight if r["status"] == "REFUSED"]
    if refused:
        return {"tone": "AMBER", "reason": f"{len(refused)} of {len(tonight)} runs were refused on the night of "
                                           f"{night.isoformat()}: {short_error(refused[0].get('error')) or ''}".rstrip(": ")}
    if all(r["mode"] != "LIVE" for r in tonight):
        return {"tone": "GREEN", "reason": "Last night's runs succeeded (dry runs only: nothing is archived until a "
                                           "table is switched on)."}
    return {"tone": "GREEN", "reason": f"Every run on the night of {night.isoformat()} succeeded."}


def archive_settings_view(environ: Any = None) -> dict[str, Any]:
    """The archive settings as this service's environment holds them: bucket name, retention,
    batch size, and per table its LIVE switch and age limit. Storage keys and secrets are never
    read here. The nightly job runs in the P2 worker; if its settings differ from this service's,
    the runs' mode shows what the worker did."""
    try:
        settings = ArchiveSettings.from_env(environ)
        retention: int | None = retention_days(environ)
        problem = None
    except ValueError as exc:
        settings, retention, problem = ArchiveSettings(), None, str(exc)
    return {
        "source": "API environment",
        "bucket": settings.bucket,
        "retentionDays": retention,
        "batchSize": settings.batch_size,
        "maxRowsPerTable": settings.max_rows_per_table,
        "tables": [{"table": rule.qualified_name, "key": rule.key, "enabled": rule.key in settings.enabled_tables,
                    "ageDays": settings.age_for(rule), "description": rule.description} for rule in ARCHIVE_RULES],
        "problem": problem,
    }


def _table_sizes(connection: Connection) -> list[dict[str, Any]]:
    """Rows (the statistics' live-row estimate: no full count on a million-row table) and size on
    disk of every table the job archives."""
    out = []
    for rule in ARCHIVE_RULES:
        row = connection.execute(
            text("""
                SELECT COALESCE(s.n_live_tup, GREATEST(c.reltuples, 0))::bigint AS rows,
                       pg_total_relation_size(c.oid) AS total_bytes, pg_relation_size(c.oid) AS table_bytes
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
                WHERE n.nspname = :schema AND c.relname = :table
            """),
            {"schema": rule.schema, "table": rule.table},
        ).mappings().one_or_none()
        out.append({"table": rule.qualified_name, "rowsEstimate": int(row["rows"]) if row else None,
                    "totalBytes": int(row["total_bytes"]) if row else None,
                    "tableBytes": int(row["table_bytes"]) if row else None})
    return out


@router.get("/v1/admin/system-health/archive")
def archive_health(
    _admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    nights: Annotated[int, Query(ge=1, le=MAX_NIGHTS)] = 14,
) -> dict[str, Any]:
    from audit_core.system_health_api import project_names

    now = datetime.now(UTC)
    set_platform_super_admin_context(connection)
    names = project_names(connection)
    since = now - timedelta(days=nights)
    rows = connection.execute(
        text("""
            SELECT run_id, tenant_id, table_key, mode, status, cutoff_utc, candidate_count, archived_count,
                   COALESCE(jsonb_array_length(CASE WHEN jsonb_typeof(object_keys) = 'array'
                                                    THEN object_keys END), 0) AS objects,
                   error, started_by, started_at_utc, finished_at_utc
            FROM auditcore.archive_runs
            WHERE batch_no IS NULL AND started_at_utc >= :since
            ORDER BY started_at_utc DESC
            LIMIT 2000
        """),
        {"since": since},
    ).mappings().all()
    # Objects are written per batch: count them from the batch rows of these runs.
    batch_objects = {str(r["run_id"]) + ":" + str(r["tenant_id"]): int(r["objects"]) for r in connection.execute(
        text("""
            SELECT run_id, tenant_id,
                   SUM(COALESCE(jsonb_array_length(CASE WHEN jsonb_typeof(object_keys) = 'array'
                                                        THEN object_keys END), 0)) AS objects
            FROM auditcore.archive_runs
            WHERE batch_no IS NOT NULL AND started_at_utc >= :since
            GROUP BY run_id, tenant_id
        """),
        {"since": since - timedelta(days=1)},
    ).mappings()}
    totals = connection.execute(
        text("""
            SELECT COALESCE(SUM(archived_count) FILTER (WHERE status = 'BATCH_ARCHIVED'), 0) AS archived,
                   COUNT(*) FILTER (WHERE status = 'BATCH_ARCHIVED') AS batches,
                   MAX(finished_at_utc) FILTER (WHERE status = 'SUCCEEDED' AND batch_no IS NULL) AS last_succeeded,
                   MAX(started_at_utc) FILTER (WHERE status = 'BATCH_ARCHIVED') AS last_archived,
                   MIN(started_at_utc) AS first_run
            FROM auditcore.archive_runs
        """),
    ).mappings().one()
    last_live_success = connection.execute(
        text("""
            SELECT tenant_id, table_key, archived_count, finished_at_utc FROM auditcore.archive_runs
            WHERE batch_no IS NULL AND status = 'SUCCEEDED' AND mode = 'LIVE'
            ORDER BY finished_at_utc DESC NULLS LAST LIMIT 1
        """),
    ).mappings().one_or_none()

    runs = []
    for r in rows:
        tenant = str(r["tenant_id"]) if r["tenant_id"] else None
        objects = int(r["objects"]) or batch_objects.get(f"{r['run_id']}:{r['tenant_id']}", 0)
        finished = r["finished_at_utc"]
        runs.append({
            "runId": str(r["run_id"]), "night": night_of(r["started_at_utc"]).isoformat(),
            "tenantId": tenant, "projectName": (names.get(tenant) or {}).get("name") if tenant else None,
            "table": r["table_key"], "mode": r["mode"], "status": r["status"],
            "candidates": int(r["candidate_count"] or 0), "archived": int(r["archived_count"] or 0),
            "objects": objects, "error": short_error(r["error"]), "startedBy": r["started_by"],
            "startedAtUtc": r["started_at_utc"], "finishedAtUtc": finished,
            "durationSeconds": round((finished - r["started_at_utc"]).total_seconds(), 1) if finished else None,
            "cutoffUtc": _iso(r["cutoff_utc"]),
        })
    status = archive_status(runs, now)
    by_night: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        by_night.setdefault(run["night"], []).append(run)
    out_nights = []
    for night, items in sorted(by_night.items(), reverse=True):
        out_nights.append({
            "night": night,
            "runs": [{**i, "startedAtUtc": _iso(i["startedAtUtc"]), "finishedAtUtc": _iso(i["finishedAtUtc"])}
                     for i in sorted(items, key=lambda i: ((i["projectName"] or i["tenantId"] or "").lower(),
                                                            i["table"]))],
            "failed": sum(1 for i in items if i["status"] == "FAILED"),
            "succeeded": sum(1 for i in items if i["status"] == "SUCCEEDED"),
            "archived": sum(i["archived"] for i in items),
        })
    return {
        "generatedAtUtc": now.isoformat(),
        "status": status,
        "nights": out_nights,
        "lastSucceededAtUtc": _iso(totals["last_succeeded"]),
        "lastArchivedAtUtc": _iso(totals["last_archived"]),
        "lastLiveSuccess": ({"tenantId": last_live_success["tenant_id"],
                             "projectName": (names.get(str(last_live_success["tenant_id"])) or {}).get("name"),
                             "table": last_live_success["table_key"],
                             "archived": int(last_live_success["archived_count"] or 0),
                             "finishedAtUtc": _iso(last_live_success["finished_at_utc"])}
                            if last_live_success else None),
        "archivedToDate": int(totals["archived"]),
        "batchesToDate": int(totals["batches"]),
        "firstRunAtUtc": _iso(totals["first_run"]),
        "tables": _table_sizes(connection),
        "settings": archive_settings_view(),
    }
