"""System Health: failed requests by error code, and the daily trend (product owner 2026-10-30).

Two SuperAdmin reads next to GET /v1/admin/system-health:

- GET /v1/admin/system-health/errors?hours=24 -- every failed Audit Core API
  request in the window broken down by HTTP status and Verigence error code
  (``401 VAC-AUTH-001``), with the routes each code came from, and per route
  the codes it failed with. Counted per minute by the request middleware
  (platform_health.record_request); requests answered before the codes were
  recorded are shown as "not classified", never guessed.
- GET /v1/admin/system-health/daily?days=14[&tenantId=] -- one row per IST
  day: Document Intelligence outcomes of the pages uploaded that day (read,
  refused for scan quality, not read in 30 minutes, failed otherwise -- the
  same classification as the DI dashboard), and the platform's failures:
  5xx, refused requests by kind, restarts, unclean stops, failed deployments
  and work that gave up after its retries.

Counts and codes only; no Journey, customer or document data leaves these routes.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_super_admin_request,
)
from audit_core.errors import error_category
from audit_core.system_health_api import ALIVE_SECONDS, boot_events, project_names
from audit_core.system_health_di import (
    DiHealthUnavailable,
    di_boot_rows,
    fetch_di_health,
)
from audit_core.uc03_p2_object_keys import IST

router = APIRouter(tags=["system-health"])

MAX_DAYS = 30
TOP_ROUTES_PER_CODE = 10

# What each code means to someone reading the page, in plain words.
_MEANINGS: dict[str, str] = {
    "VAC-AUTH-001": "Login token not accepted: an expired session, or a screen calling an endpoint that still "
                    "expects the old token format.",
    "VAC-AUTH-002": "Permission denied: the person's role does not carry the permission the action needs.",
    "VAC-NF-005": "Journey not found in the caller's scope: not mapped to that dealership or outlet, or deleted.",
    "VAC-NF-000": "Not found.",
    "VAC-VAL-001": "The request was malformed (missing or invalid fields).",
    "VAC-VAL-002": "A business rule refused the action.",
    "VAC-VAL-010": "Unsupported request (wrong method, file type or size).",
    "VAC-CONFLICT-000": "Conflict: already done, or changed by someone else meanwhile.",
    "VAC-SYS-001": "Unexpected Audit Core error.",
    "VAC-SYS-002": "A dependency (Document Intelligence, Security, storage) did not answer.",
    "VAC-SYS-003": "Too many requests.",
}
_BY_STATUS: dict[int, str] = {
    400: "Bad request.", 401: "Not signed in.", 403: "Forbidden.", 404: "Not found.", 405: "Method not allowed.",
    409: "Conflict.", 413: "Upload too large.", 415: "Unsupported file type.", 422: "Refused by a business rule.",
    429: "Too many requests.", 500: "Server error.", 502: "Dependency failed.", 503: "Temporarily unavailable.",
    504: "Dependency timed out.",
}


def split_key(key: str) -> tuple[int, str | None]:
    status, _, code = key.partition(" ")
    try:
        return int(status), (code or None)
    except ValueError:
        return 0, key or None


def meaning(status: int, code: str | None) -> str:
    if code and code in _MEANINGS:
        return _MEANINGS[code]
    if code and code.startswith("VAC-NF"):
        return "Not found."
    if code and code.startswith("VAC-VAL"):
        return "The request was refused as invalid."
    return _BY_STATUS.get(status, "Server error." if status >= 500 else "Refused.")


# --------------------------------------------------------------------------- errors

def build_error_breakdown(connection: Connection, *, since: datetime) -> dict[str, Any]:
    totals = connection.execute(
        text(
            """
            SELECT COALESCE(sum(total), 0) AS total, COALESCE(sum(client_errors), 0) AS c4,
                   COALESCE(sum(server_errors), 0) AS c5
            FROM platform_ops.http_minute_stats WHERE minute_utc >= :since
            """
        ),
        {"since": since},
    ).mappings().one()
    rows = connection.execute(
        text(
            """
            SELECT s.service_name, r.key AS route,
                   COALESCE((r.value->>'4xx')::int, 0) AS c4, COALESCE((r.value->>'5xx')::int, 0) AS c5,
                   COALESCE(r.value->'codes', '{}'::jsonb) AS codes
            FROM platform_ops.http_minute_stats s, jsonb_each(s.failed_routes) r
            WHERE s.minute_utc >= :since
            """
        ),
        {"since": since},
    ).mappings().all()
    routes: dict[tuple[str, str], dict[str, Any]] = {}
    codes: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = (str(r["service_name"]), str(r["route"]))
        route = routes.setdefault(key, {"service": key[0], "route": key[1], "clientErrors": 0, "serverErrors": 0,
                                        "codes": defaultdict(int)})
        route["clientErrors"] += int(r["c4"])
        route["serverErrors"] += int(r["c5"])
        for code_key, n in (r["codes"] or {}).items():
            n = int(n or 0)
            route["codes"][code_key] += n
            entry = codes.setdefault(code_key, {"count": 0, "routes": defaultdict(int)})
            entry["count"] += n
            entry["routes"][key] += n

    failed = int(totals["c4"]) + int(totals["c5"])
    classified = sum(c["count"] for c in codes.values())
    by_code = []
    for code_key, entry in codes.items():
        status, code = split_key(code_key)
        top = sorted(entry["routes"].items(), key=lambda kv: -kv[1])[:TOP_ROUTES_PER_CODE]
        by_code.append({
            "key": code_key, "status": status, "errorCode": code,
            "category": error_category(status, code or ""), "meaning": meaning(status, code),
            "count": entry["count"],
            "share": round(100.0 * entry["count"] / failed, 1) if failed else None,
            "routes": [{"service": s, "route": rt, "count": n} for (s, rt), n in top],
        })
    by_code.sort(key=lambda c: (c["status"] < 500, -c["count"]))
    by_route = []
    for route in routes.values():
        listed = sorted(route.pop("codes").items(), key=lambda kv: -kv[1])
        route["codes"] = [{"key": k, "status": split_key(k)[0], "errorCode": split_key(k)[1], "count": n}
                          for k, n in listed]
        route["notClassified"] = max(route["clientErrors"] + route["serverErrors"] - sum(n for _, n in listed), 0)
        by_route.append(route)
    by_route.sort(key=lambda r: (-r["serverErrors"], -r["clientErrors"]))
    return {
        "totals": {"requests": int(totals["total"]), "clientErrors": int(totals["c4"]),
                   "serverErrors": int(totals["c5"]), "classified": classified,
                   "notClassified": max(failed - classified, 0)},
        "byCode": by_code,
        "byRoute": by_route,
        "notes": [
            "Audit Core API requests only; Document Intelligence's own API is not counted here.",
            ("Not classified: failed requests answered before the error code was recorded with each one "
             "(from 30 Oct 2026), or on a route beyond the 20 counted in one minute."),
        ],
    }


@router.get("/v1/admin/system-health/errors")
def system_health_errors(
    _admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {"generatedAtUtc": now.isoformat(), "windowHours": hours,
            **build_error_breakdown(connection, since=now - timedelta(hours=hours))}


# --------------------------------------------------------------------------- daily

_DI_OUTCOME_SQL = """
    SELECT (b.created_at_utc AT TIME ZONE 'Asia/Kolkata')::date AS day,
           CASE
             WHEN q.last_error = 'DUPLICATE_PAGE' THEN 'DUPLICATE'
             WHEN q.last_error LIKE 'DI_QUALITY_%' THEN 'BAD_SCAN'
             WHEN q.last_error = 'NOT_READ_IN_30_MIN' THEN 'NOT_READ_IN_TIME'
             WHEN q.queue_status IN ('READY', 'NEEDS_REVIEW', 'SUPPORTING') THEN 'READ'
             WHEN q.queue_status IN ('FAILED', 'DEAD_LETTER') THEN 'FAILED_OTHER'
             WHEN q.queue_status = 'CANCELLED' AND q.last_error IS NOT NULL
                  AND q.last_error NOT IN ('PAGE_REMOVED', 'SUPERSEDED') THEN 'FAILED_OTHER'
             WHEN q.queue_status = 'CANCELLED' THEN 'REMOVED'
             ELSE 'IN_PROGRESS'
           END AS outcome,
           count(*) AS n
    FROM auditcore.p2_document_queue q
    JOIN auditcore.p2_upload_batches b ON b.tenant_id = q.tenant_id AND b.batch_id = q.batch_id
    WHERE q.tenant_id = :t AND b.created_at_utc >= :s AND b.created_at_utc < :e
      AND q.queue_status <> 'MERGED'
    GROUP BY 1, 2
"""

_DEAD_LETTER_SQL = """
    SELECT (updated_at_utc AT TIME ZONE 'Asia/Kolkata')::date AS day, count(*) AS n
    FROM auditcore.p2_work_queue
    WHERE tenant_id = :t AND work_status = 'DEAD_LETTER' AND updated_at_utc >= :s AND updated_at_utc < :e
    GROUP BY 1
"""


def _ist_day(value: datetime) -> date:
    return value.astimezone(IST).date()


def _empty_day(day: date) -> dict[str, Any]:
    return {
        "date": day.isoformat(),
        "documents": 0, "read": 0, "badScan": 0, "notReadInTime": 0, "failedOther": 0, "inProgress": 0,
        "diFailedPct": None, "diSystemFailedPct": None,
        "requests": 0, "clientErrors": 0, "serverErrors": 0,
        "unauthenticated": 0, "forbidden": 0, "notFound": 0, "otherClientErrors": 0, "codes": {},
        "restarts": 0, "uncleanStops": 0, "deploys": 0, "deploysFailed": 0, "deadLetters": 0,
    }


def build_daily(connection: Connection, *, days: int, tenant_id: str | None, now: datetime,
                extra_boots: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    last = _ist_day(now)
    first = last - timedelta(days=days - 1)
    start = datetime.combine(first, time(0), tzinfo=IST).astimezone(UTC)
    end = datetime.combine(last + timedelta(days=1), time(0), tzinfo=IST).astimezone(UTC)
    series = {(first + timedelta(days=i)).isoformat(): _empty_day(first + timedelta(days=i)) for i in range(days)}

    def row_for(day: date) -> dict[str, Any] | None:
        return series.get(day.isoformat())

    # Platform-wide: requests, by status and code.
    for r in connection.execute(
        text(
            """
            SELECT (minute_utc AT TIME ZONE 'Asia/Kolkata')::date AS day, sum(total) AS total,
                   sum(client_errors) AS c4, sum(server_errors) AS c5
            FROM platform_ops.http_minute_stats WHERE minute_utc >= :s AND minute_utc < :e
            GROUP BY 1
            """
        ),
        {"s": start, "e": end},
    ).mappings().all():
        day = row_for(r["day"])
        if day is not None:
            day.update(requests=int(r["total"] or 0), clientErrors=int(r["c4"] or 0), serverErrors=int(r["c5"] or 0))
    for r in connection.execute(
        text(
            """
            SELECT (s.minute_utc AT TIME ZONE 'Asia/Kolkata')::date AS day, c.key AS code, sum(c.value::int) AS n
            FROM platform_ops.http_minute_stats s, jsonb_each(s.failed_routes) r,
                 jsonb_each_text(COALESCE(r.value->'codes', '{}'::jsonb)) c
            WHERE s.minute_utc >= :s AND s.minute_utc < :e
            GROUP BY 1, 2
            """
        ),
        {"s": start, "e": end},
    ).mappings().all():
        day = row_for(r["day"])
        if day is None:
            continue
        n = int(r["n"] or 0)
        day["codes"][r["code"]] = n
        status, _code = split_key(r["code"])
        if status == 401:
            day["unauthenticated"] += n
        elif status == 403:
            day["forbidden"] += n
        elif status == 404:
            day["notFound"] += n
        elif 400 <= status < 500:
            day["otherClientErrors"] += n

    # Restarts, unclean stops and deploys, by the same rules as the services table.
    boots = [dict(b) for b in connection.execute(
        text(
            """
            SELECT service_name, replica_id, deployment_id, started_at_utc, last_seen_at_utc, stopped_at_utc
            FROM platform_ops.service_heartbeats ORDER BY service_name, started_at_utc
            """
        )
    ).mappings().all()]
    # Document Intelligence's services keep their boots in DI's database (asked over HTTP).
    boots += list(extra_boots or [])
    by_service: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for b in sorted(boots, key=lambda b: (b["service_name"], b["started_at_utc"])):
        by_service[b["service_name"]].append(b)
    alive_after = now - timedelta(seconds=ALIVE_SECONDS)
    for rows in by_service.values():
        for kind, at in boot_events(rows, alive_after):
            day = row_for(_ist_day(at))
            if day is not None:
                day[{"RESTART": "restarts", "DEPLOY": "deploys", "UNCLEAN": "uncleanStops"}[kind]] += 1

    for r in connection.execute(
        text(
            """
            SELECT COALESCE(occurred_at_utc, received_at_utc) AS at, event_type, status
            FROM platform_ops.railway_events
            WHERE COALESCE(occurred_at_utc, received_at_utc) >= :s AND COALESCE(occurred_at_utc, received_at_utc) < :e
            """
        ),
        {"s": start, "e": end},
    ).mappings().all():
        if (r["status"] or "").upper() in ("FAILED", "CRASHED") or \
                (r["event_type"] or "").lower().endswith((".failed", ".crashed")):
            day = row_for(_ist_day(r["at"]))
            if day is not None:
                day["deploysFailed"] += 1

    # Per Project (each read under its own tenant): DI outcomes and dead letters.
    projects = project_names(connection)
    tenants = [tenant_id] if tenant_id else sorted(projects)
    for tenant in tenants:
        set_tenant_context(connection, tenant)
        params = {"t": tenant, "s": start, "e": end}
        for r in connection.execute(text(_DI_OUTCOME_SQL), params).mappings().all():
            day = row_for(r["day"])
            if day is None:
                continue
            n = int(r["n"])
            outcome = r["outcome"]
            if outcome in ("DUPLICATE", "REMOVED"):
                continue
            day["documents"] += n
            key = {"READ": "read", "BAD_SCAN": "badScan", "NOT_READ_IN_TIME": "notReadInTime",
                   "FAILED_OTHER": "failedOther", "IN_PROGRESS": "inProgress"}[outcome]
            day[key] += n
        for r in connection.execute(text(_DEAD_LETTER_SQL), params).mappings().all():
            day = row_for(r["day"])
            if day is not None:
                day["deadLetters"] += int(r["n"])

    for day in series.values():
        settled = day["read"] + day["badScan"] + day["notReadInTime"] + day["failedOther"]
        if settled:
            day["diFailedPct"] = round(100.0 * (settled - day["read"]) / settled, 1)
            day["diSystemFailedPct"] = round(100.0 * (day["notReadInTime"] + day["failedOther"]) / settled, 1)

    first_request = connection.execute(
        text("SELECT min(minute_utc) FROM platform_ops.http_minute_stats")
    ).scalar_one_or_none()
    first_code = connection.execute(
        text(
            """
            SELECT min(s.minute_utc) FROM platform_ops.http_minute_stats s, jsonb_each(s.failed_routes) r
            WHERE r.value ? 'codes'
            """
        )
    ).scalar_one_or_none()
    return {
        "from": first.isoformat(), "to": last.isoformat(), "timezone": "Asia/Kolkata",
        "tenantId": tenant_id,
        "projects": sorted(({"tenantId": t, "projectName": p["name"], "projectStatus": p["status"]}
                            for t, p in projects.items()), key=lambda p: (p["projectStatus"] != "ACTIVE",
                                                                         str(p["projectName"]).lower())),
        "days": list(series.values()),
        "requestsRecordedFrom": _ist_day(first_request).isoformat() if first_request else None,
        "codesRecordedFrom": _ist_day(first_code).isoformat() if first_code else None,
        "notes": [
            ("Document Intelligence: the pages uploaded that day and how each ended, as the DI dashboard counts "
             "them. Scan quality is the photo's (the PC's side); not read in 30 minutes and other failures are "
             "the system's."),
            ("Requests, restarts and deployments are platform-wide (Audit Core API and the services' own "
             "heartbeats); the Project filter applies to Document Intelligence and dead letters."),
            "Request counts are kept 14 days; restarts and deployments 30 days.",
        ],
    }


@router.get("/v1/admin/system-health/daily")
def system_health_daily(
    _admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = 14,
    tenant_id: Annotated[str | None, Query(alias="tenantId", max_length=128)] = None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    # DI answers for at most a week; its restarts older than that are not shown.
    try:
        di_boots = di_boot_rows(fetch_di_health(min(days * 24, 168)))
        di_note = None
    except DiHealthUnavailable as exc:
        di_boots, di_note = [], f"Document Intelligence's restarts are missing: {exc}"
    body = build_daily(connection, days=days, tenant_id=tenant_id, now=now, extra_boots=di_boots)
    if di_note:
        body["notes"].append(di_note)
    elif days > 7:
        body["notes"].append("Document Intelligence's own restarts are included for the last 7 days only.")
    return {"generatedAtUtc": now.isoformat(), **body}
