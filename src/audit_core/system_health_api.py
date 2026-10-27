"""SuperAdmin System Health (product owner 2026-10-27).

GET /v1/admin/system-health?hours=24 answers, for the whole platform:

- services: each service's boots (restarts within a deployment vs new
  deployments), unclean ends, whether it is alive now (a beat in the last
  3 minutes), and its request counts (total, 4xx, 5xx, slow) with the routes
  that failed;
- resources: each service's CPU and memory, peak and average, against its
  container's limits, from the services' own 5-second readings;
- railway: deployment failures/crashes and monitor alerts Railway posted to
  the webhook;
- pipeline: per Project, documents and work items in flight, those past the
  30-minute rule, failures and their reasons, dead letters, outbox/inbox
  problems, Document Intelligence jobs;
- database: connections, idle-in-transaction sessions, deadlocks and rollbacks
  (with the change over the window, from 15-minute counter snapshots),
  slowest queries;
- Document Intelligence: DI keeps its own database, so its boots, CPU/memory,
  queues and database come from DI's GET /v1/platform/health and are merged
  into services, resources, pipeline.documentIntelligence and diDatabase;
- alerts: what needs attention now, most severe first, shown as a banner.

Counts and error codes only; no Journey or customer data leaves this route.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import Connection, text

from audit_core.db import set_platform_super_admin_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    get_engine,
    require_super_admin_request,
)
from audit_core.system_health_di import (
    DiHealthUnavailable,
    di_alerts,
    di_boot_rows,
    di_pipeline,
    di_unavailable,
    fetch_di_health,
)

router = APIRouter(tags=["system-health"])

ALIVE_SECONDS = 180
CPU_ALERT_RATIO = 0.8
MEMORY_ALERT_RATIO = 0.85


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


def services_health(connection: Connection, since: datetime, now: datetime,
                    extra_boots: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    boots = [dict(b) for b in connection.execute(
        text(
            """
            SELECT boot_id, service_name, replica_id, deployment_id, version, started_at_utc,
                   last_seen_at_utc, stopped_at_utc, stop_reason
            FROM platform_ops.service_heartbeats
            WHERE last_seen_at_utc >= :since OR stopped_at_utc IS NULL
            ORDER BY service_name, started_at_utc
            """
        ),
        {"since": since},
    ).mappings().all()]
    # Services on their own database (Document Intelligence) report their boots
    # through their API; the same rules count them.
    boots = sorted(boots + list(extra_boots or []), key=lambda b: (b["service_name"], b["started_at_utc"]))
    http = {r["service_name"]: dict(r) for r in connection.execute(
        text(
            """
            SELECT service_name, sum(total) AS total, sum(client_errors) AS client_errors,
                   sum(server_errors) AS server_errors, sum(slow) AS slow,
                   sum(total) FILTER (WHERE minute_utc >= now() - interval '1 hour') AS total_1h,
                   sum(server_errors) FILTER (WHERE minute_utc >= now() - interval '1 hour') AS server_errors_1h
            FROM platform_ops.http_minute_stats
            WHERE minute_utc >= :since
            GROUP BY 1
            """
        ),
        {"since": since},
    ).mappings().all()}
    routes: dict[str, dict[str, dict[str, int]]] = {}
    for r in connection.execute(
        text(
            """
            SELECT s.service_name, r.key AS route,
                   sum((r.value->>'4xx')::int) AS c4, sum((r.value->>'5xx')::int) AS c5
            FROM platform_ops.http_minute_stats s, jsonb_each(s.failed_routes) r
            WHERE s.minute_utc >= :since
            GROUP BY 1, 2
            """
        ),
        {"since": since},
    ).mappings().all():
        routes.setdefault(r["service_name"], {})[r["route"]] = {"4xx": int(r["c4"] or 0), "5xx": int(r["c5"] or 0)}
    hourly: dict[str, list[dict[str, Any]]] = {}
    for r in connection.execute(
        text(
            """
            SELECT service_name, date_trunc('hour', minute_utc) AS hour, sum(total) AS total,
                   sum(client_errors) AS c4, sum(server_errors) AS c5
            FROM platform_ops.http_minute_stats
            WHERE minute_utc >= :since
            GROUP BY 1, 2 ORDER BY 2
            """
        ),
        {"since": since},
    ).mappings().all():
        hourly.setdefault(r["service_name"], []).append(
            {"hour": _iso(r["hour"]), "total": int(r["total"]), "4xx": int(r["c4"]), "5xx": int(r["c5"])})

    alive_after = now - timedelta(seconds=ALIVE_SECONDS)
    by_service: dict[str, list[dict[str, Any]]] = {}
    for b in boots:
        by_service.setdefault(b["service_name"], []).append(dict(b))
    names = sorted(set(by_service) | set(http))
    out = []
    for name in names:
        rows = by_service.get(name, [])
        alive = [b for b in rows if b["stopped_at_utc"] is None and b["last_seen_at_utc"] >= alive_after]
        in_window = [b for b in rows if b["started_at_utc"] >= since]
        # A start in a deployment not seen before is a deploy; a second start of the
        # same replica within a deployment is a restart (crash, OOM, platform restart).
        restarts = deploys = 0
        seen_deployments: set[str | None] = set()
        seen_replicas: set[tuple[str | None, str | None]] = set()
        for b in rows:
            key = (b["deployment_id"], b["replica_id"])
            if b["started_at_utc"] >= since and seen_deployments:
                if b["deployment_id"] and key in seen_replicas:
                    restarts += 1
                elif b["deployment_id"] not in seen_deployments:
                    deploys += 1
            seen_deployments.add(b["deployment_id"])
            seen_replicas.add(key)

        def replaced_by_deploy(boot: dict[str, Any], rows: list[dict[str, Any]] = rows) -> bool:
            # Railway starts the new deployment, then stops the old one; the old
            # process often cannot stamp its own stop in time (uvicorn waits for
            # open streams), so a quiet boot whose newer deployment started before
            # it went quiet was replaced, not crashed (live 2026-10-27).
            return any(
                other["started_at_utc"] > boot["started_at_utc"]
                and other["deployment_id"] != boot["deployment_id"]
                and other["started_at_utc"] <= boot["last_seen_at_utc"] + timedelta(seconds=ALIVE_SECONDS)
                for other in rows
            )

        unclean = [b for b in rows
                   if b["stopped_at_utc"] is None and b["last_seen_at_utc"] < alive_after
                   and b["last_seen_at_utc"] >= since and not replaced_by_deploy(b)]
        last_seen = max((b["last_seen_at_utc"] for b in rows), default=None)
        h = http.get(name, {})
        failing = sorted(routes.get(name, {}).items(), key=lambda kv: -(kv[1]["5xx"] * 1000 + kv[1]["4xx"]))[:10]
        out.append({
            "service": name,
            "status": "UP" if alive else ("DOWN" if rows else "NO_HEARTBEAT"),
            "replicasAlive": len(alive),
            "lastSeenAtUtc": _iso(last_seen),
            "currentVersion": (max(alive, key=lambda b: b["started_at_utc"])["version"] if alive else None),
            "upSinceUtc": _iso(min((b["started_at_utc"] for b in alive), default=None)),
            "boots": len(in_window),
            "restarts": restarts,
            "deploys": deploys,
            "uncleanStops": len(unclean),
            "lastUncleanStopAtUtc": _iso(max((b["last_seen_at_utc"] for b in unclean), default=None)),
            "requests": int(h.get("total") or 0),
            "clientErrors": int(h.get("client_errors") or 0),
            "serverErrors": int(h.get("server_errors") or 0),
            "slowRequests": int(h.get("slow") or 0),
            "requestsLastHour": int(h.get("total_1h") or 0),
            "serverErrorsLastHour": int(h.get("server_errors_1h") or 0),
            "failingRoutes": [{"route": r, **c} for r, c in failing],
            "hourly": hourly.get(name, []),
        })
    return out


def resources_health(connection: Connection, since: datetime) -> list[dict[str, Any]]:
    """Per service: CPU (cores) and memory peaks and averages against the container's
    limits, from each process's own 5-second readings, plus an hourly series."""
    peaks = {r["service_name"]: dict(r) for r in connection.execute(
        text(
            """
            SELECT service_name, max(cpu_cores_max) AS cpu_max, avg(cpu_cores_avg) AS cpu_avg,
                   max(cpu_limit_cores) AS cpu_limit, max(memory_bytes_max) AS mem_max,
                   avg(memory_bytes_max) AS mem_avg, max(memory_limit_bytes) AS mem_limit,
                   (array_agg(minute_utc ORDER BY cpu_cores_max DESC NULLS LAST))[1] AS cpu_peak_at,
                   (array_agg(minute_utc ORDER BY memory_bytes_max DESC NULLS LAST))[1] AS mem_peak_at,
                   count(*) FILTER (WHERE cpu_limit_cores > 0 AND cpu_cores_max >= 0.8 * cpu_limit_cores) AS cpu_hot_minutes
            FROM platform_ops.service_resource_minutes
            WHERE minute_utc >= :since
            GROUP BY 1
            """
        ),
        {"since": since},
    ).mappings().all()}
    series: dict[str, list[dict[str, Any]]] = {}
    for r in connection.execute(
        text(
            """
            SELECT service_name, date_trunc('hour', minute_utc) AS hour,
                   max(cpu_cores_max) AS cpu_max, avg(cpu_cores_avg) AS cpu_avg, max(memory_bytes_max) AS mem_max
            FROM platform_ops.service_resource_minutes
            WHERE minute_utc >= :since
            GROUP BY 1, 2 ORDER BY 2
            """
        ),
        {"since": since},
    ).mappings().all():
        series.setdefault(r["service_name"], []).append({
            "hour": _iso(r["hour"]), "cpuMax": _num(r["cpu_max"]), "cpuAvg": _num(r["cpu_avg"]),
            "memoryMaxGb": _gb(r["mem_max"])})
    return [{
        "service": name,
        "cpuMax": _num(p["cpu_max"]), "cpuAvg": _num(p["cpu_avg"]), "cpuLimit": _num(p["cpu_limit"]),
        "cpuPeakAtUtc": _iso(p["cpu_peak_at"]), "cpuHotMinutes": int(p["cpu_hot_minutes"] or 0),
        "memoryMaxGb": _gb(p["mem_max"]), "memoryAvgGb": _gb(p["mem_avg"]), "memoryLimitGb": _gb(p["mem_limit"]),
        "memoryPeakAtUtc": _iso(p["mem_peak_at"]),
        "hourly": series.get(name, []),
    } for name, p in sorted(peaks.items())]


def _num(value: Any) -> float | None:
    return None if value is None else round(float(value), 3)


def _gb(value: Any) -> float | None:
    return None if value is None else round(float(value) / (1024 ** 3), 3)


def railway_health(connection: Connection, since: datetime) -> dict[str, Any]:
    """Deployment status changes and monitor alerts Railway posted to the webhook."""
    configured = connection.execute(
        text("SELECT count(*) FROM platform_ops.webhook_secrets WHERE name = 'railway'")
    ).scalar_one() > 0
    events = [dict(r) for r in connection.execute(
        text(
            """
            SELECT received_at_utc, occurred_at_utc, event_type, severity, status, environment_name,
                   service_name, deployment_id
            FROM platform_ops.railway_events
            WHERE received_at_utc >= :since
            ORDER BY COALESCE(occurred_at_utc, received_at_utc) DESC
            LIMIT 200
            """
        ),
        {"since": since},
    ).mappings().all()]
    # The latest status per deployment: a deployment that failed and was then
    # removed by the next one still counts as failed.
    latest: dict[str, dict[str, Any]] = {}
    for e in events:
        key = e["deployment_id"] or f"{e['event_type']}:{e['received_at_utc']}"
        latest.setdefault(key, e)
    failed = [e for e in events if (e["status"] or "").upper() in ("FAILED", "CRASHED")
              or (e["event_type"] or "").lower().endswith((".failed", ".crashed"))]
    monitors = [e for e in events if not (e["event_type"] or "").lower().startswith("deployment.")]
    return {
        "webhookConfigured": configured,
        "lastEventAtUtc": _iso(events[0]["received_at_utc"]) if events else None,
        "deployments": sum(1 for e in latest.values() if (e["event_type"] or "").lower().startswith("deployment.")),
        "failedOrCrashed": [{
            "service": e["service_name"], "status": e["status"] or e["event_type"], "atUtc":
            _iso(e["occurred_at_utc"] or e["received_at_utc"]), "deploymentId": e["deployment_id"]} for e in failed],
        "monitorAlerts": [{
            "service": e["service_name"], "type": e["event_type"], "severity": e["severity"],
            "atUtc": _iso(e["occurred_at_utc"] or e["received_at_utc"])} for e in monitors[:20]],
        "events": [{
            "service": e["service_name"], "type": e["event_type"], "status": e["status"], "severity": e["severity"],
            "environment": e["environment_name"], "atUtc": _iso(e["occurred_at_utc"] or e["received_at_utc"])}
            for e in events[:50]],
    }


def _db_counters_delta(connection: Connection, since: datetime, current: dict[str, Any]) -> dict[str, Any] | None:
    earliest = connection.execute(
        text(
            """
            SELECT taken_at_utc, counters
            FROM platform_ops.db_counter_snapshots
            WHERE taken_at_utc >= :since
            ORDER BY taken_at_utc LIMIT 1
            """
        ),
        {"since": since},
    ).mappings().one_or_none()
    if earliest is None:
        return None
    before = earliest["counters"] or {}
    if before.get("statsResetAtUtc") != (_iso(current.get("statsResetAtUtc")) or current.get("statsResetAtUtc")):
        return {"fromUtc": _iso(earliest["taken_at_utc"]), "note": "Database statistics were reset in this window."}
    out: dict[str, Any] = {"fromUtc": _iso(earliest["taken_at_utc"])}
    for key in ("deadlocks", "rollbacks", "commits", "tempFiles"):
        if before.get(key) is not None and current.get(key) is not None:
            out[key] = max(int(current[key]) - int(before[key]), 0)
    if out.get("commits") is not None and out.get("rollbacks") is not None and (out["commits"] + out["rollbacks"]):
        out["rollbackRatio"] = round(out["rollbacks"] / (out["commits"] + out["rollbacks"]), 4)
    return out


def pipeline_health(connection: Connection, since: datetime) -> dict[str, Any]:
    raw = connection.execute(text("SELECT platform_ops.pipeline_health(:since)"), {"since": since}).scalar_one()
    set_platform_super_admin_context(connection)
    projects = {r["tenant_id"]: {"name": r["project_name"], "status": r["project_status"]} for r in connection.execute(
        text("SELECT tenant_id, project_name, project_status FROM auditcore.projects")
    ).mappings().all()}
    per: dict[str, dict[str, Any]] = {}

    def project(tenant_id: str) -> dict[str, Any]:
        info = projects.get(tenant_id, {})
        return per.setdefault(tenant_id, {
            "tenantId": tenant_id, "projectName": info.get("name") or tenant_id,
            "projectStatus": info.get("status"),
            "documents": {"inFlight": 0, "over30m": 0, "failed": 0, "ready": 0, "oldestInFlightAtUtc": None,
                          "byStatus": {}},
            "work": {"open": 0, "over30m": 0, "deadLetter": 0, "completed": 0, "oldestOpenAtUtc": None,
                     "byStatus": {}},
            "deadLettersOpen": 0, "outboxProblems": 0, "inboxProblems": 0,
            "failureReasons": [],
        })

    in_flight_docs = {"QUEUED", "PREPARING_PAGE", "DI_UPLOAD_PREPARING", "DI_UPLOADING", "DI_FINALIZING",
                      "CLASSIFYING", "EXTRACTING", "SYNCING_TO_AUDIT_CORE", "RETRY_WAIT"}
    for row in raw.get("documentQueue", []):
        d = project(row["tenantId"])["documents"]
        d["byStatus"][row["status"]] = row["count"]
        d["over30m"] += row["over30m"]
        if row["status"] in in_flight_docs:
            d["inFlight"] += row["count"]
            if d["oldestInFlightAtUtc"] is None or row["oldestAtUtc"] < d["oldestInFlightAtUtc"]:
                d["oldestInFlightAtUtc"] = row["oldestAtUtc"]
        elif row["status"] in ("FAILED", "DEAD_LETTER"):
            d["failed"] += row["count"]
        elif row["status"] == "READY":
            d["ready"] += row["count"]
    for row in raw.get("workQueue", []):
        w = project(row["tenantId"])["work"]
        w["byStatus"][row["status"]] = row["count"]
        w["over30m"] += row["over30m"]
        if row["status"] in ("PENDING", "CLAIMED", "PROCESSING", "RETRY_WAIT"):
            w["open"] += row["count"]
            if w["oldestOpenAtUtc"] is None or row["oldestAtUtc"] < w["oldestOpenAtUtc"]:
                w["oldestOpenAtUtc"] = row["oldestAtUtc"]
        elif row["status"] == "DEAD_LETTER":
            w["deadLetter"] += row["count"]
        elif row["status"] == "COMPLETED":
            w["completed"] += row["count"]
    for row in raw.get("documentFailureReasons", []):
        project(row["tenantId"])["failureReasons"].append({"kind": "DOCUMENT", "reason": row["reason"],
                                                           "count": row["count"]})
    for row in raw.get("workFailureReasons", []):
        project(row["tenantId"])["failureReasons"].append({"kind": row["workType"], "reason": row["reason"],
                                                           "count": row["count"]})
    for row in raw.get("deadLetters", []):
        project(row["tenantId"])["deadLettersOpen"] += row["open"]
    for row in raw.get("outbox", []):
        project(row["tenantId"])["outboxProblems"] += row["count"]
    for row in raw.get("inbox", []):
        project(row["tenantId"])["inboxProblems"] += row["count"]
    ordered = sorted(per.values(), key=lambda p: (p["projectStatus"] != "ACTIVE", p["projectName"].lower()))
    return {"projects": ordered, "workerJobs": raw.get("workerJobs", []),
            "documentIntelligence": raw.get("documentIntelligence")}


def build_alerts(services: list[dict[str, Any]], railway: dict[str, Any], pipeline: dict[str, Any],
                 database: dict[str, Any], resources: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    resources = resources or []
    alerts: list[dict[str, Any]] = []

    def add(severity: str, code: str, message: str, **ref: Any) -> None:
        alerts.append({"severity": severity, "code": code, "message": message, **ref})

    for s in services:
        if s["status"] == "DOWN":
            add("CRITICAL", "SERVICE_SILENT", f"{s['service']} has sent no heartbeat since {s['lastSeenAtUtc']}.",
                service=s["service"])
        if s["restarts"]:
            add("WARNING", "SERVICE_RESTARTED",
                f"{s['service']} restarted {s['restarts']} time(s) within a deployment in this window.",
                service=s["service"])
        if s["uncleanStops"]:
            add("WARNING", "SERVICE_UNCLEAN_STOP",
                f"{s['service']} ended {s['uncleanStops']} time(s) without a clean shutdown (crash or kill).",
                service=s["service"])
        if s["serverErrorsLastHour"]:
            add("CRITICAL" if s["requestsLastHour"] and s["serverErrorsLastHour"] / s["requestsLastHour"] > 0.01
                else "WARNING", "HTTP_5XX",
                f"{s['service']} answered {s['serverErrorsLastHour']} request(s) with a server error in the last hour.",
                service=s["service"])
    if not railway["webhookConfigured"]:
        add("WARNING", "RAILWAY_WEBHOOK_NOT_SET", "Railway deployment alerts are not connected yet.")
    for d in railway["failedOrCrashed"]:
        crashed = "CRASH" in (d["status"] or "").upper()
        add("CRITICAL" if crashed else "WARNING", "DEPLOY_CRASHED" if crashed else "DEPLOY_FAILED",
            f"{d['service']}: deployment {'crashed' if crashed else 'failed'} at {d['atUtc']}.", service=d["service"])
    for m in railway["monitorAlerts"]:
        add("WARNING", "RAILWAY_MONITOR", f"{m['service']}: Railway alert {m['type']}.", service=m["service"])
    for r in resources:
        if r["cpuMax"] and r["cpuLimit"] and r["cpuMax"] >= CPU_ALERT_RATIO * r["cpuLimit"]:
            add("WARNING", "CPU_HIGH",
                f"{r['service']} peaked at {r['cpuMax']:.2f} of {r['cpuLimit']:g} vCPU at {r['cpuPeakAtUtc']}.",
                service=r["service"])
        if r["memoryMaxGb"] and r["memoryLimitGb"] and r["memoryMaxGb"] >= MEMORY_ALERT_RATIO * r["memoryLimitGb"]:
            add("CRITICAL", "MEMORY_HIGH",
                f"{r['service']} peaked at {r['memoryMaxGb']:.2f} of {r['memoryLimitGb']:g} GB memory at "
                f"{r['memoryPeakAtUtc']}.", service=r["service"])
    for p in pipeline["projects"]:
        if p["documents"]["over30m"]:
            add("WARNING", "DOCUMENTS_OVER_30_MIN",
                f"{p['projectName']}: {p['documents']['over30m']} page(s) still processing after 30 minutes.",
                tenantId=p["tenantId"])
        if p["work"]["over30m"]:
            add("WARNING", "WORK_OVER_30_MIN",
                f"{p['projectName']}: {p['work']['over30m']} work item(s) waiting for more than 30 minutes "
                f"(oldest since {p['work']['oldestOpenAtUtc']}).", tenantId=p["tenantId"])
        if p["work"]["deadLetter"] or p["deadLettersOpen"]:
            add("CRITICAL", "DEAD_LETTERS",
                f"{p['projectName']}: {p['work']['deadLetter'] + p['deadLettersOpen']} item(s) gave up after retries.",
                tenantId=p["tenantId"])
        if p["outboxProblems"]:
            add("WARNING", "OUTBOX_BACKLOG", f"{p['projectName']}: {p['outboxProblems']} event(s) not published.",
                tenantId=p["tenantId"])
    if database.get("idleInTransactionOver5m"):
        add("WARNING", "DB_IDLE_IN_TRANSACTION",
            f"{database['idleInTransactionOver5m']} database session(s) have held a transaction open for over 5 minutes.")
    total_conns = sum(int(v) for v in (database.get("connections") or {}).values())
    if database.get("maxConnections") and total_conns >= 0.8 * database["maxConnections"]:
        add("CRITICAL", "DB_CONNECTIONS_HIGH",
            f"{total_conns} of {database['maxConnections']} database connections are in use.")
    delta = database.get("window") or {}
    if delta.get("rollbackRatio") and delta["rollbackRatio"] > 0.05:
        add("WARNING", "DB_ROLLBACKS", f"{delta['rollbackRatio']:.1%} of database transactions rolled back in this window.")
    if delta.get("deadlocks"):
        add("WARNING", "DB_DEADLOCKS", f"{delta['deadlocks']} deadlock(s) in this window.")
    order = {"CRITICAL": 0, "WARNING": 1}
    alerts.sort(key=lambda a: order.get(a["severity"], 2))
    return alerts


@router.get("/v1/admin/system-health")
def system_health(
    _admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    since = now - timedelta(hours=hours)
    # Document Intelligence keeps its own database: asked over HTTP; a failure is
    # shown on the page and raised as an alert, never fatal.
    di: dict[str, Any] | None
    try:
        di = fetch_di_health(hours)
        di_error = None
    except DiHealthUnavailable as exc:
        di, di_error = None, str(exc)
    services = services_health(connection, since, now, extra_boots=di_boot_rows(di) if di else None)
    railway = railway_health(connection, since)
    resources = resources_health(connection, since)
    pipeline = pipeline_health(connection, since)
    database = connection.execute(text("SELECT platform_ops.db_health()")).scalar_one()
    database["window"] = _db_counters_delta(connection, since, database)
    di_database: dict[str, Any] | None = None
    if di is None:
        pipeline["documentIntelligence"] = di_unavailable(di_error or "unknown")
    else:
        resources = sorted(resources + [r for r in di.get("resources") or [] if isinstance(r, dict)],
                           key=lambda r: str(r.get("service")))
        pipeline["documentIntelligence"] = di_pipeline(di, project_names(connection))
        if isinstance(di.get("database"), dict):
            di_database = {**di["database"], "window": None}
    alerts = build_alerts(services, railway, pipeline, database, resources)
    alerts += di_alerts(pipeline["documentIntelligence"], di_database)
    order = {"CRITICAL": 0, "WARNING": 1}
    alerts.sort(key=lambda a: order.get(a["severity"], 2))
    return {
        "generatedAtUtc": now.isoformat(),
        "windowHours": hours,
        "alerts": alerts,
        "services": services,
        "railway": railway,
        "resources": resources,
        "pipeline": pipeline,
        "database": database,
        "diDatabase": di_database,
    }


def project_names(connection: Connection) -> dict[str, dict[str, Any]]:
    set_platform_super_admin_context(connection)
    return {str(r["tenant_id"]): {"name": r["project_name"], "status": r["project_status"]}
            for r in connection.execute(
                text("SELECT tenant_id, project_name, project_status FROM auditcore.projects")).mappings().all()}


WEBHOOK_SECRET_HEADER = "X-Verigence-Webhook-Secret"
_MAX_WEBHOOK_BYTES = 64 * 1024


def _webhook_secret_ok(connection: Connection, presented: str | None) -> bool | None:
    """None: no secret configured. Constant-time comparison of SHA-256 digests."""
    stored = connection.execute(
        text("SELECT secret_sha256 FROM platform_ops.webhook_secrets WHERE name = 'railway'")
    ).scalar_one_or_none()
    if stored is None:
        return None
    digest = hashlib.sha256((presented or "").encode()).hexdigest()
    return hmac.compare_digest(digest, stored.strip())


@router.post("/v1/platform/railway-webhook", status_code=204)
async def railway_webhook(request: Request) -> Response:
    """Railway posts deployment status changes and CPU/RAM monitor alerts here.

    Railway does not sign its payloads, so the webhook is configured with a secret
    header; only its SHA-256 is stored. Only a few known fields are kept."""
    body = await request.body()
    if len(body) > _MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large.")
    # The header is preferred; Railway's API cannot set custom headers on a webhook,
    # so the same secret is also accepted as a ?key= query parameter (Railway's own
    # documented fallback). Request logs here record the route template, not the query.
    presented = request.headers.get(WEBHOOK_SECRET_HEADER) or request.query_params.get("key")

    def _store() -> int:
        with get_engine().begin() as connection:
            ok = _webhook_secret_ok(connection, presented)
            if ok is None:
                return 503
            if not ok:
                return 401
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                return 400
            if not isinstance(payload, dict):
                return 400
            resource = payload.get("resource") if isinstance(payload.get("resource"), dict) else {}
            details = payload.get("details") if isinstance(payload.get("details"), dict) else {}

            def name_of(key: str) -> str | None:
                value = resource.get(key)
                return str(value.get("name"))[:120] if isinstance(value, dict) and value.get("name") else None

            deployment = resource.get("deployment") if isinstance(resource.get("deployment"), dict) else {}
            kept = {
                "type": payload.get("type"), "severity": payload.get("severity"),
                "timestamp": payload.get("timestamp"),
                "details": {k: details.get(k) for k in ("id", "status", "source", "branch", "commitHash")
                            if k in details},
                "resource": {k: name_of(k) for k in ("project", "environment", "service")},
            }
            connection.execute(
                text(
                    """
                    INSERT INTO platform_ops.railway_events
                      (occurred_at_utc, event_type, severity, status, environment_name, service_name,
                       deployment_id, payload)
                    VALUES (:at, :type, :sev, :status, :env, :svc, :dep, CAST(:payload AS jsonb))
                    """
                ),
                {"at": _parse(payload.get("timestamp")), "type": str(payload.get("type") or "unknown")[:120],
                 "sev": (str(payload["severity"])[:40] if payload.get("severity") else None),
                 "status": (str(details["status"])[:40] if details.get("status") else None),
                 "env": name_of("environment"), "svc": name_of("service"),
                 "dep": (str(deployment.get("id"))[:120] if deployment.get("id") else None),
                 "payload": json.dumps(kept, default=str)},
            )
            return 204

    status = await asyncio.to_thread(_store)
    if status == 503:
        raise HTTPException(status_code=503, detail="Railway webhook is not configured.")
    if status == 401:
        raise HTTPException(status_code=401, detail="Webhook secret missing or wrong.")
    if status == 400:
        raise HTTPException(status_code=400, detail="Expected a JSON object.")
    return Response(status_code=204)
