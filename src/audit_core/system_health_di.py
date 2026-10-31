"""Document Intelligence's part of the SuperAdmin System Health page (product owner 2026-10-27).

DI runs on its own database (Railway Postgres), so Audit Core cannot read DI's
queues or heartbeats itself. It asks DI: GET /v1/platform/health with a
ServiceIntegration token for the "di" audience. DI answers counts, codes and
timings only. A failed call never fails the page: the page says DI could not
be reached and raises an alert.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from audit_core.security_integration import SecurityOAuthClient

logger = structlog.get_logger(__name__)

_DI_AUDIENCE = "di"
_TIMEOUT_SECONDS = 10.0
_IN_FLIGHT = ("PENDING", "RUNNING")


class DiHealthUnavailable(RuntimeError):
    """DI's health could not be read; the message is safe to show a SuperAdmin."""


def fetch_di_health(hours: int) -> dict[str, Any]:
    di_url = os.environ.get("DI_BASE_URL", "").strip().rstrip("/")
    security_url = os.environ.get("SECURITY_BASE_URL", "").strip()
    client_id = os.environ.get("SECURITY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SECURITY_CLIENT_SECRET", "")
    if not di_url or not security_url or not client_id or not client_secret:
        raise DiHealthUnavailable("DI or Security integration is not configured on Audit Core.")
    try:
        with SecurityOAuthClient(base_url=security_url, client_id=client_id, client_secret=client_secret) as oauth:
            token = oauth.get_service_token(audience=_DI_AUDIENCE)
        response = httpx.get(f"{di_url}/v1/platform/health", params={"hours": hours},
                             headers={"Authorization": f"Bearer {token}"}, timeout=_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        logger.warning("di_platform_health_failed", error_type=type(exc).__name__)
        raise DiHealthUnavailable(f"Document Intelligence did not answer ({type(exc).__name__}).") from exc
    except Exception as exc:
        logger.warning("di_platform_health_failed", error_type=type(exc).__name__)
        raise DiHealthUnavailable(f"No service token for Document Intelligence ({type(exc).__name__}).") from exc
    if response.status_code != 200:
        logger.warning("di_platform_health_failed", status_code=response.status_code)
        raise DiHealthUnavailable(f"Document Intelligence answered HTTP {response.status_code}.")
    payload = response.json()
    if isinstance(payload, dict) and "boots" not in payload and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if not isinstance(payload, dict):
        raise DiHealthUnavailable("Document Intelligence answered in an unexpected shape.")
    return payload


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def di_boot_rows(di: dict[str, Any]) -> list[dict[str, Any]]:
    """DI's boots in the shape of platform_ops.service_heartbeats rows, so the
    same restart / deploy / unclean-stop rules count them."""
    rows = []
    for b in di.get("boots") or []:
        started, last_seen = _when(b.get("startedAtUtc")), _when(b.get("lastSeenAtUtc"))
        if started is None or last_seen is None:
            continue
        rows.append({
            "boot_id": b.get("bootId"), "service_name": b.get("service") or "di", "replica_id": b.get("replicaId"),
            "deployment_id": b.get("deploymentId"), "version": b.get("version"), "started_at_utc": started,
            "last_seen_at_utc": last_seen, "stopped_at_utc": _when(b.get("stoppedAtUtc")), "stop_reason": None,
        })
    return rows


def di_pipeline(di: dict[str, Any], project_names: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Per Project: DI classification and extraction jobs in flight, past 30
    minutes and failed, with the failure codes; lease reclaims; scheduler problems."""
    per: dict[str, dict[str, Any]] = {}

    def project(tenant_id: str) -> dict[str, Any]:
        info = project_names.get(tenant_id, {})
        empty = {"inFlight": 0, "over30m": 0, "failed": 0, "completed": 0, "oldestInFlightAtUtc": None, "byStatus": {}}
        return per.setdefault(tenant_id, {
            "tenantId": tenant_id, "projectName": info.get("name") or tenant_id, "projectStatus": info.get("status"),
            "classification": dict(empty, byStatus={}), "extraction": dict(empty, byStatus={}), "failureReasons": [],
        })

    queues = di.get("queues") or {}
    for row in queues.get("byStatus") or []:
        queue = row.get("queue")
        if queue not in ("classification", "extraction"):
            continue
        q = project(str(row.get("tenantId")))[queue]
        status, count = str(row.get("status")), int(row.get("count") or 0)
        q["byStatus"][status] = q["byStatus"].get(status, 0) + count
        q["over30m"] += int(row.get("over30m") or 0)
        if status in _IN_FLIGHT:
            q["inFlight"] += count
            oldest = row.get("oldestInFlightAtUtc")
            if oldest and (q["oldestInFlightAtUtc"] is None or oldest < q["oldestInFlightAtUtc"]):
                q["oldestInFlightAtUtc"] = oldest
        elif status == "FAILED":
            q["failed"] += count
        elif status == "COMPLETED":
            q["completed"] += count
    for row in queues.get("failures") or []:
        project(str(row.get("tenantId")))["failureReasons"].append(
            {"queue": row.get("queue"), "errorCode": row.get("errorCode"), "count": int(row.get("count") or 0)})
    ordered = sorted(per.values(), key=lambda p: (p["projectStatus"] != "ACTIVE", str(p["projectName"]).lower()))
    return {
        "available": True, "error": None, "projects": ordered,
        "leaseReclaims": int(queues.get("leaseReclaims") or 0),
        "schedulerProblems": di.get("schedulerProblems") or [],
    }


def di_unavailable(message: str) -> dict[str, Any]:
    return {"available": False, "error": message, "projects": [], "leaseReclaims": 0, "schedulerProblems": []}


# Failure codes that are not a failure to read (live, 2026-10-31): a Phase 2
# page is classified and read, then DI's v1 step that links a document to a
# checklist slot finds no requirement ref -- Phase 2 links pages itself -- and
# marks the job FAILED. 501 such "failures" on 30-31 Oct were all pages that
# were classified, read and MERGED or READY in Audit Core. They are shown with
# their code, but not counted as failed jobs.
NOT_READING_FAILURE_CODES = frozenset({"REQUIREMENT_REF_MISSING"})


def not_reading_failures(reasons: list[dict[str, Any]]) -> int:
    return sum(int(r.get("count") or 0) for r in reasons or [] if r.get("errorCode") in NOT_READING_FAILURE_CODES)


def di_alerts(pipeline: dict[str, Any], database: dict[str, Any] | None) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []

    def add(severity: str, code: str, message: str, **ref: Any) -> None:
        alerts.append({"severity": severity, "code": code, "message": message, **ref})

    if not pipeline["available"]:
        add("WARNING", "DI_HEALTH_UNAVAILABLE", f"Document Intelligence health could not be read: {pipeline['error']}")
        return alerts
    for p in pipeline["projects"]:
        over = p["classification"]["over30m"] + p["extraction"]["over30m"]
        if over:
            add("WARNING", "DI_JOBS_OVER_30_MIN",
                f"{p['projectName']}: {over} Document Intelligence job(s) still waiting or running after 30 minutes.",
                tenantId=p["tenantId"])
        failed = p["classification"]["failed"] + p["extraction"]["failed"]
        not_reading = not_reading_failures(p["failureReasons"])
        real = max(failed - not_reading, 0)
        if real:
            add("WARNING", "DI_JOBS_FAILED",
                f"{p['projectName']}: {real} Document Intelligence job(s) failed in this window.",
                tenantId=p["tenantId"])
    if pipeline["leaseReclaims"]:
        add("WARNING", "DI_LEASE_RECLAIMED",
            f"{pipeline['leaseReclaims']} Document Intelligence job(s) were taken back from a worker that stopped "
            "while holding them.")
    for s in pipeline["schedulerProblems"]:
        add("WARNING", "DI_SCHEDULER_PROBLEM",
            f"Document Intelligence {s.get('runName')} for {s.get('runDate')} "
            f"{'failed (' + str(s['errorCode']) + ')' if s.get('errorCode') else 'did not finish'}.")
    if database:
        if database.get("idleInTransactionOver5m"):
            add("WARNING", "DI_DB_IDLE_IN_TRANSACTION",
                f"{database['idleInTransactionOver5m']} Document Intelligence database session(s) have held a "
                "transaction open for over 5 minutes.")
        conns = sum(int(v) for v in (database.get("connections") or {}).values())
        if database.get("maxConnections") and conns >= 0.8 * database["maxConnections"]:
            add("CRITICAL", "DI_DB_CONNECTIONS_HIGH",
                f"{conns} of {database['maxConnections']} Document Intelligence database connections are in use.")
    return alerts
