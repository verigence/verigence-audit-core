"""System Health: Cloudflare (web Worker and R2) and the Neon database (product owner 2026-10-31).

"pls use Cloudflare connector and add the cloudflare web and R2 nodes in the
system health" and "i need NeonDB status health status.. spikes, CPU and all".

GET /v1/admin/system-health/infrastructure?hours=24 answers three nodes:

- web: the Cloudflare Worker that serves the web app. Requests, errors,
  subrequests, invocation status and CPU time (p50/p99) per hour from the
  Cloudflare GraphQL Analytics API (workersInvocationsAdaptive), and a live
  GET of the web app's URL (status and latency).
- r2: the bucket Audit Core keeps documents in. Operations per hour by kind
  and response status (r2OperationsAdaptiveGroups), objects and bytes stored
  (r2StorageAdaptiveGroups), and a live HEAD of the bucket with the same
  credentials the document storage uses.
- neon: the database itself, from the per-minute samples the API's heartbeat
  writes (platform_ops.db_minute_stats): connections by state against
  max_connections, waiting on locks, the longest query, transactions per
  second and its per-minute peak, buffer and local-file-cache hit ratios,
  deadlocks, temp bytes, size. With NEON_API_KEY set, also the compute
  endpoint's state and autoscaling range and the compute used per hour
  (CU-hours, from the consumption API), which is how far the compute scaled:
  the public API's answer to "CPU spikes". Per-minute CPU % is only in the
  Neon console (or its OpenTelemetry export).

Settings read: CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID (optional; found
with GET /accounts otherwise), CLOUDFLARE_WEB_WORKER (default
verigence-web-dev), CLOUDFLARE_R2_BUCKET (default: the document storage
bucket), VERIGENCE_WEB_URL (default: the first CORS origin, or the dev web
origin on dev), NEON_API_KEY, NEON_PROJECT_ID (default round-scene-14217399),
NEON_ORG_ID (optional; read from the project otherwise). Tokens are never
logged or returned.

Every outside call has a short timeout and is cached for a minute per window
(Neon's consumption figures for 15 minutes, as Neon asks); a failure is shown
as the node's state and never fails the page.
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx
import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection, text

from audit_core.config import DEV_WEB_ORIGIN
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_super_admin_request,
)

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["system-health"])

CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
NEON_API = "https://console.neon.tech/api/v2"
DEFAULT_WEB_WORKER = "verigence-web-dev"
DEFAULT_NEON_PROJECT = "round-scene-14217399"
TIMEOUT = httpx.Timeout(5.0, connect=3.0)
CACHE_SECONDS = 60.0
NEON_CONSUMPTION_CACHE_SECONDS = 900.0
OVERALL_SECONDS = 8.0

WEB_ERROR_RATE_WARNING = 0.01
WEB_ERROR_RATE_CRITICAL = 0.05
R2_ERROR_RATE_WARNING = 0.01
MIN_ERRORS_FOR_ALERT = 5
NEON_CONNECTIONS_RATIO = 0.8
LONG_QUERY_SECONDS = 300
CACHE_HIT_MIN = 0.95
MIN_READS_FOR_CACHE_ALERT = 1000
SAMPLE_STALE_SECONDS = 180

_lock = threading.Lock()
_cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
_cloudflare_account: dict[str, str] = {}
_neon_org: dict[str, str] = {}


class CloudUnavailable(RuntimeError):
    """An outside API could not be read; the message is safe to show a SuperAdmin."""


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _cached(key: tuple[Any, ...], ttl: float, fn: Callable[[], Any]) -> Any:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _lock:
        _cache[key] = (time.monotonic(), value)
    return value


def clear_cache() -> None:
    with _lock:
        _cache.clear()
        _cloudflare_account.clear()
        _neon_org.clear()


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _round(value: Any, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


def _ratio(part: float, whole: float) -> float | None:
    return round(part / whole, 4) if whole else None


# ---------------------------------------------------------------- Cloudflare

def _cf_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def cloudflare_account_id(client: httpx.Client, token: str) -> str:
    override = _env("CLOUDFLARE_ACCOUNT_ID")
    if override:
        return override
    with _lock:
        known = _cloudflare_account.get("id")
    if known:
        return known
    response = client.get(f"{CLOUDFLARE_API}/accounts", params={"per_page": 5}, headers=_cf_headers(token))
    if response.status_code != 200:
        raise CloudUnavailable(f"Cloudflare accounts answered HTTP {response.status_code}; "
                               "set CLOUDFLARE_ACCOUNT_ID if the token cannot list accounts.")
    accounts = [a for a in (response.json().get("result") or []) if isinstance(a, dict) and a.get("id")]
    if not accounts:
        raise CloudUnavailable("The Cloudflare token sees no account; set CLOUDFLARE_ACCOUNT_ID.")
    with _lock:
        _cloudflare_account["id"] = str(accounts[0]["id"])
    return str(accounts[0]["id"])


def _graphql(client: httpx.Client, token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    response = client.post(f"{CLOUDFLARE_API}/graphql", json={"query": query, "variables": variables},
                           headers=_cf_headers(token))
    if response.status_code != 200:
        raise CloudUnavailable(f"Cloudflare analytics answered HTTP {response.status_code}.")
    body = response.json()
    errors = body.get("errors") or []
    if errors:
        first = errors[0].get("message") if isinstance(errors[0], dict) else str(errors[0])
        raise CloudUnavailable(f"Cloudflare analytics refused the query: {str(first)[:200]}")
    accounts = ((body.get("data") or {}).get("viewer") or {}).get("accounts") or []
    return accounts[0] if accounts else {}


_WORKER_QUERY = """
query WorkerHealth($accountTag: string!, $script: string!, $start: Time!, $end: Time!) {
  viewer {
    accounts(filter: {accountTag: $accountTag}) {
      total: workersInvocationsAdaptive(limit: 1,
          filter: {scriptName: $script, datetime_geq: $start, datetime_leq: $end}) {
        sum { requests errors subrequests }
        quantiles { cpuTimeP50 cpuTimeP99 }
      }
      hourly: workersInvocationsAdaptive(limit: 10000,
          filter: {scriptName: $script, datetime_geq: $start, datetime_leq: $end}) {
        sum { requests errors subrequests }
        quantiles { cpuTimeP50 cpuTimeP99 }
        dimensions { datetimeHour status }
      }
    }
  }
}
"""

_R2_QUERY = """
query R2Health($accountTag: string!, $bucket: string!, $start: Time!, $end: Time!, $storageStart: Time!) {
  viewer {
    accounts(filter: {accountTag: $accountTag}) {
      operations: r2OperationsAdaptiveGroups(limit: 10000,
          filter: {bucketName: $bucket, datetime_geq: $start, datetime_leq: $end}) {
        sum { requests }
        dimensions { datetimeHour actionType responseStatusCode }
      }
      storage: r2StorageAdaptiveGroups(limit: 1,
          filter: {bucketName: $bucket, datetime_geq: $storageStart, datetime_leq: $end},
          orderBy: [datetime_DESC]) {
        max { objectCount payloadSize metadataSize uploadCount }
        dimensions { datetime }
      }
    }
  }
}
"""


def _ms(microseconds: Any) -> float | None:
    return None if microseconds is None else round(float(microseconds) / 1000, 2)


def summarize_worker(account: dict[str, Any]) -> dict[str, Any]:
    """The Worker's window totals, invocation statuses and hourly series from the GraphQL answer.
    CPU time comes in microseconds; the page shows milliseconds."""
    total = (account.get("total") or [{}])[0] if account.get("total") else {}
    tsum = total.get("sum") or {}
    tq = total.get("quantiles") or {}
    by_status: dict[str, int] = defaultdict(int)
    hours: dict[str, dict[str, Any]] = {}
    for g in account.get("hourly") or []:
        dims, s, q = g.get("dimensions") or {}, g.get("sum") or {}, g.get("quantiles") or {}
        requests = int(s.get("requests") or 0)
        by_status[str(dims.get("status") or "unknown")] += requests
        h = hours.setdefault(str(dims.get("datetimeHour")), {
            "hour": dims.get("datetimeHour"), "requests": 0, "errors": 0, "subrequests": 0,
            "cpuP50Ms": None, "cpuP99Ms": None, "_largest": -1})
        h["requests"] += requests
        h["errors"] += int(s.get("errors") or 0)
        h["subrequests"] += int(s.get("subrequests") or 0)
        # Quantiles cannot be added: the hour's p50 is the busiest status's, its p99 the worst.
        if requests > h["_largest"]:
            h["_largest"], h["cpuP50Ms"] = requests, _ms(q.get("cpuTimeP50"))
        p99 = _ms(q.get("cpuTimeP99"))
        if p99 is not None and (h["cpuP99Ms"] is None or p99 > h["cpuP99Ms"]):
            h["cpuP99Ms"] = p99
    hourly = sorted(hours.values(), key=lambda h: str(h["hour"]))
    for h in hourly:
        h.pop("_largest", None)
    requests = int(tsum.get("requests") or sum(h["requests"] for h in hourly))
    errors = int(tsum.get("errors") or sum(h["errors"] for h in hourly))
    return {
        "requests": requests,
        "errors": errors,
        "subrequests": int(tsum.get("subrequests") or sum(h["subrequests"] for h in hourly)),
        "errorRate": _ratio(errors, requests),
        "cpuTimeP50Ms": _ms(tq.get("cpuTimeP50")),
        "cpuTimeP99Ms": _ms(tq.get("cpuTimeP99")),
        "byStatus": [{"status": k, "requests": v} for k, v in sorted(by_status.items(), key=lambda kv: -kv[1])],
        "hourly": hourly,
    }


_R2_KINDS = {
    "reads": ("GetObject", "HeadObject", "HeadBucket"),
    "writes": ("PutObject", "CopyObject", "CreateMultipartUpload", "UploadPart", "UploadPartCopy",
               "CompleteMultipartUpload", "AbortMultipartUpload"),
    "lists": ("ListObjects", "ListObjectsV2", "ListBuckets", "ListMultipartUploads", "ListParts"),
    "deletes": ("DeleteObject", "DeleteObjects"),
}


def r2_kind(action: str) -> str:
    for kind, actions in _R2_KINDS.items():
        if action in actions:
            return kind
    return "other"


def summarize_r2(account: dict[str, Any]) -> dict[str, Any]:
    """R2 operations per hour by kind, errors by response status, and the latest storage reading.
    A 404 on HeadObject is how the app checks a key is free, so 4xx is shown, not alerted."""
    by_action: dict[str, dict[str, int]] = {}
    by_code: dict[int, int] = defaultdict(int)
    hours: dict[str, dict[str, Any]] = {}
    for g in account.get("operations") or []:
        dims = g.get("dimensions") or {}
        count = int((g.get("sum") or {}).get("requests") or 0)
        action = str(dims.get("actionType") or "unknown")
        try:
            code = int(dims.get("responseStatusCode") or 0)
        except (TypeError, ValueError):
            code = 0
        a = by_action.setdefault(action, {"requests": 0, "clientErrors": 0, "serverErrors": 0})
        a["requests"] += count
        if code >= 500:
            a["serverErrors"] += count
        elif code >= 400:
            a["clientErrors"] += count
        by_code[code] += count
        h = hours.setdefault(str(dims.get("datetimeHour")), {
            "hour": dims.get("datetimeHour"), "reads": 0, "writes": 0, "lists": 0, "deletes": 0, "other": 0,
            "clientErrors": 0, "serverErrors": 0})
        h[r2_kind(action)] += count
        if code >= 500:
            h["serverErrors"] += count
        elif code >= 400:
            h["clientErrors"] += count
    operations = sum(by_code.values())
    server = sum(v for k, v in by_code.items() if k >= 500)
    client = sum(v for k, v in by_code.items() if 400 <= k < 500)
    storage_rows = account.get("storage") or []
    latest = storage_rows[0] if storage_rows else None
    smax = (latest or {}).get("max") or {}
    return {
        "operations": operations,
        "clientErrors": client,
        "serverErrors": server,
        "serverErrorRate": _ratio(server, operations),
        "byAction": [{"actionType": k, **v} for k, v in sorted(by_action.items(), key=lambda kv: -kv[1]["requests"])],
        "byStatusCode": [{"status": k, "requests": v} for k, v in sorted(by_code.items())],
        "hourly": sorted(hours.values(), key=lambda h: str(h["hour"])),
        "storage": None if latest is None else {
            "objectCount": int(smax.get("objectCount") or 0),
            "payloadBytes": int(smax.get("payloadSize") or 0),
            "metadataBytes": int(smax.get("metadataSize") or 0),
            "pendingUploads": int(smax.get("uploadCount") or 0),
            "atUtc": (latest.get("dimensions") or {}).get("datetime"),
        },
    }


def _window(hours: int) -> tuple[str, str, str]:
    end = datetime.now(UTC).replace(microsecond=0)
    start = end - timedelta(hours=hours)
    storage_start = end - timedelta(hours=max(hours, 48))
    return (start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z"),
            storage_start.isoformat().replace("+00:00", "Z"))


def _unavailable(configured: bool, error: str) -> dict[str, Any]:
    return {"configured": configured, "available": False, "error": error}


def fetch_worker_analytics(hours: int) -> dict[str, Any]:
    token = _env("CLOUDFLARE_API_TOKEN")
    if not token:
        return _unavailable(False, "CLOUDFLARE_API_TOKEN is not set on Audit Core.")
    start, end, _ = _window(hours)
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            account = _graphql(client, token, _WORKER_QUERY, {
                "accountTag": cloudflare_account_id(client, token),
                "script": _env("CLOUDFLARE_WEB_WORKER", DEFAULT_WEB_WORKER), "start": start, "end": end})
    except CloudUnavailable as exc:
        logger.warning("cloudflare_worker_analytics_failed", error=str(exc)[:200])
        return _unavailable(True, str(exc))
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("cloudflare_worker_analytics_failed", error_type=type(exc).__name__)
        return _unavailable(True, f"Cloudflare did not answer ({type(exc).__name__}).")
    return {"configured": True, "available": True, "error": None, **summarize_worker(account)}


def r2_bucket() -> tuple[str | None, Any]:
    """The bucket to watch and the document storage (None when storage is not configured)."""
    from audit_core.uc03_p2_storage import get_p2_document_storage

    try:
        storage = get_p2_document_storage()
    except RuntimeError:
        storage = None
    return (_env("CLOUDFLARE_R2_BUCKET") or (storage.bucket if storage is not None else None)), storage


def fetch_r2_analytics(hours: int, bucket: str | None) -> dict[str, Any]:
    token = _env("CLOUDFLARE_API_TOKEN")
    if not token:
        return _unavailable(False, "CLOUDFLARE_API_TOKEN is not set on Audit Core.")
    if not bucket:
        return _unavailable(False, "No R2 bucket is configured (document storage or CLOUDFLARE_R2_BUCKET).")
    start, end, storage_start = _window(hours)
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            account = _graphql(client, token, _R2_QUERY, {
                "accountTag": cloudflare_account_id(client, token), "bucket": bucket,
                "start": start, "end": end, "storageStart": storage_start})
    except CloudUnavailable as exc:
        logger.warning("cloudflare_r2_analytics_failed", error=str(exc)[:200])
        return _unavailable(True, str(exc))
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("cloudflare_r2_analytics_failed", error_type=type(exc).__name__)
        return _unavailable(True, f"Cloudflare did not answer ({type(exc).__name__}).")
    return {"configured": True, "available": True, "error": None, **summarize_r2(account)}


def web_url() -> str | None:
    explicit = _env("VERIGENCE_WEB_URL")
    if explicit:
        return explicit
    origins = [o.strip() for o in _env("AUDIT_CORE_CORS_ALLOWED_ORIGINS").split(",") if o.strip()]
    if origins:
        return origins[0]
    return DEV_WEB_ORIGIN if _env("APP_ENV").lower() in ("dev", "development") else None


def probe_web(url: str | None) -> dict[str, Any]:
    checked = _now_iso()
    if not url:
        return {"configured": False, "ok": None, "url": None, "statusCode": None, "latencyMs": None,
                "error": "VERIGENCE_WEB_URL is not set.", "checkedAtUtc": checked}
    started = time.perf_counter()
    try:
        response = httpx.get(url, timeout=TIMEOUT, follow_redirects=True,
                             headers={"User-Agent": "verigence-audit-core-system-health"})
    except httpx.HTTPError as exc:
        return {"configured": True, "ok": False, "url": url, "statusCode": None,
                "latencyMs": round((time.perf_counter() - started) * 1000), "checkedAtUtc": checked,
                "error": f"No answer ({type(exc).__name__})."}
    return {"configured": True, "ok": response.status_code < 400, "url": url, "statusCode": response.status_code,
            "latencyMs": round((time.perf_counter() - started) * 1000), "checkedAtUtc": checked,
            "error": None if response.status_code < 400 else f"Answered HTTP {response.status_code}."}


def probe_r2(storage: Any) -> dict[str, Any]:
    checked = _now_iso()
    if storage is None:
        return {"configured": False, "ok": None, "latencyMs": None, "error": "Document storage is not configured.",
                "checkedAtUtc": checked}
    started = time.perf_counter()
    try:
        storage.probe_bucket(timeout_seconds=5.0)
    except Exception as exc:  # noqa: BLE001 - shown as the node's state
        return {"configured": True, "ok": False, "latencyMs": round((time.perf_counter() - started) * 1000),
                "error": str(exc)[:200] or type(exc).__name__, "checkedAtUtc": checked}
    return {"configured": True, "ok": True, "latencyMs": round((time.perf_counter() - started) * 1000),
            "error": None, "checkedAtUtc": checked}


# ---------------------------------------------------------------- Neon API

def _neon_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


def _neon_get(client: httpx.Client, key: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = client.get(f"{NEON_API}{path}", params=params, headers=_neon_headers(key))
    if response.status_code != 200:
        raise CloudUnavailable(f"Neon API answered HTTP {response.status_code} for {path.split('?')[0]}.")
    body = response.json()
    if not isinstance(body, dict):
        raise CloudUnavailable("Neon API answered in an unexpected shape.")
    return body


def neon_project_id() -> str:
    return _env("NEON_PROJECT_ID", DEFAULT_NEON_PROJECT)


def summarize_endpoints(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [{
        "id": e.get("id"), "type": e.get("type"), "branchId": e.get("branch_id"),
        "state": e.get("current_state"), "pendingState": e.get("pending_state"),
        "minCu": e.get("autoscaling_limit_min_cu"), "maxCu": e.get("autoscaling_limit_max_cu"),
        "suspendTimeoutSeconds": e.get("suspend_timeout_seconds"),
        "lastActiveAtUtc": e.get("last_active"), "regionId": e.get("region_id"),
    } for e in body.get("endpoints") or [] if isinstance(e, dict)]


def summarize_consumption(body: dict[str, Any], project_id: str) -> dict[str, Any]:
    """CU-seconds per hour into CU-hours per hour, which is the compute's average size that hour."""
    hourly: list[dict[str, Any]] = []
    for project in body.get("projects") or []:
        if project.get("project_id") not in (None, project_id):
            continue
        for period in project.get("periods") or []:
            for frame in period.get("consumption") or []:
                seconds = sum(float(m.get("value") or 0) for m in frame.get("metrics") or []
                              if m.get("metric_name") == "compute_unit_seconds")
                hourly.append({"hour": frame.get("timeframe_start"), "cuHours": round(seconds / 3600, 3)})
    hourly.sort(key=lambda h: str(h["hour"]))
    peak = max(hourly, key=lambda h: h["cuHours"], default=None)
    return {
        "hourly": hourly,
        "totalCuHours": round(sum(h["cuHours"] for h in hourly), 3),
        "peakCu": peak["cuHours"] if peak else None,
        "peakAtUtc": peak["hour"] if peak else None,
    }


def fetch_neon_api(hours: int) -> dict[str, Any]:
    key = _env("NEON_API_KEY")
    project_id = neon_project_id()
    if not key:
        return {**_unavailable(False, "NEON_API_KEY is not set on Audit Core."), "projectId": project_id,
                "endpoints": [], "compute": None}
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            endpoints = summarize_endpoints(_neon_get(client, key, f"/projects/{project_id}/endpoints"))

            def consumption() -> dict[str, Any]:
                org_id = _env("NEON_ORG_ID")
                if not org_id:
                    with _lock:
                        org_id = _neon_org.get(project_id, "")
                if not org_id:
                    project = _neon_get(client, key, f"/projects/{project_id}").get("project") or {}
                    org_id = str(project.get("org_id") or "")
                    if not org_id:
                        raise CloudUnavailable("The Neon project carries no organization id; set NEON_ORG_ID.")
                    with _lock:
                        _neon_org[project_id] = org_id
                end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                # Hourly figures reach back 168 hours at most.
                start = end - timedelta(hours=min(hours + 1, 167))
                body = _neon_get(client, key, "/consumption_history/v2/projects", {
                    "from": start.isoformat().replace("+00:00", "Z"), "to": end.isoformat().replace("+00:00", "Z"),
                    "granularity": "hourly", "org_id": org_id, "metrics": "compute_unit_seconds",
                    "project_ids": project_id})
                return summarize_consumption(body, project_id)

            try:
                compute = _cached(("neon-consumption", project_id, hours), NEON_CONSUMPTION_CACHE_SECONDS, consumption)
                compute_error = None
            except CloudUnavailable as exc:
                compute, compute_error = None, str(exc)
    except CloudUnavailable as exc:
        logger.warning("neon_api_failed", error=str(exc)[:200])
        return {**_unavailable(True, str(exc)), "projectId": project_id, "endpoints": [], "compute": None}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("neon_api_failed", error_type=type(exc).__name__)
        return {**_unavailable(True, f"Neon API did not answer ({type(exc).__name__})."), "projectId": project_id,
                "endpoints": [], "compute": None}
    return {"configured": True, "available": True, "error": compute_error, "projectId": project_id,
            "endpoints": endpoints, "compute": compute}


# ---------------------------------------------------------------- outside calls together

def fetch_external(hours: int) -> dict[str, Any]:
    """Every outside call for one window, in parallel, cached for a minute."""

    def run() -> dict[str, Any]:
        bucket, storage = r2_bucket()
        url = web_url()
        jobs: dict[str, Callable[[], dict[str, Any]]] = {
            "webProbe": lambda: probe_web(url),
            "webAnalytics": lambda: fetch_worker_analytics(hours),
            "r2Probe": lambda: probe_r2(storage),
            "r2Analytics": lambda: fetch_r2_analytics(hours, bucket),
            "neonApi": lambda: fetch_neon_api(hours),
        }
        out: dict[str, Any] = {"bucket": bucket}
        pool = ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="system-health-cloud")
        try:
            futures = {name: pool.submit(fn) for name, fn in jobs.items()}
            deadline = time.monotonic() + OVERALL_SECONDS
            for name, future in futures.items():
                try:
                    out[name] = future.result(timeout=max(deadline - time.monotonic(), 0.1))
                except FutureTimeout:
                    out[name] = {**_unavailable(True, "No answer in time."), "ok": False}
                except Exception as exc:  # noqa: BLE001 - one node failing never fails the page
                    logger.warning("system_health_cloud_failed", job=name, error_type=type(exc).__name__)
                    out[name] = {**_unavailable(True, f"Could not be read ({type(exc).__name__})."), "ok": False}
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return out

    return _cached(("external", hours), CACHE_SECONDS, run)


# ---------------------------------------------------------------- Neon, from the database

_HOURLY_SQL = """
WITH s AS (
  SELECT minute_utc,
         connections_active, connections_idle, connections_idle_in_tx, connections_other,
         connections_active + connections_idle + connections_idle_in_tx + connections_other AS connections_total,
         max_connections, waiting_on_locks, longest_query_seconds, size_bytes,
         extract(epoch FROM minute_utc - lag(minute_utc) OVER w) AS dt,
         (xact_commit + xact_rollback) - lag(xact_commit + xact_rollback) OVER w AS dx,
         blks_hit - lag(blks_hit) OVER w AS dhit,
         blks_read - lag(blks_read) OVER w AS dread,
         deadlocks - lag(deadlocks) OVER w AS ddead,
         temp_bytes - lag(temp_bytes) OVER w AS dtemp,
         lfc_hits - lag(lfc_hits) OVER w AS dlh,
         lfc_misses - lag(lfc_misses) OVER w AS dlm,
         stats_reset_at_utc IS NOT DISTINCT FROM lag(stats_reset_at_utc) OVER w AS same_stats
  FROM platform_ops.db_minute_stats
  WHERE minute_utc >= :since - interval '10 minutes'
  WINDOW w AS (ORDER BY minute_utc)
), d AS (
  SELECT *, (same_stats AND dt > 0 AND dt <= 600 AND dx >= 0) AS ok,
         (dt > 0 AND dt <= 600 AND dlh >= 0 AND dlm >= 0) AS lfc_ok
  FROM s WHERE minute_utc >= :since
)
SELECT date_trunc('hour', minute_utc) AS hour, grouping(date_trunc('hour', minute_utc)) AS is_total,
       count(*) AS samples,
       max(connections_total) AS conn_max, avg(connections_total) AS conn_avg,
       max(connections_active) AS active_max, avg(connections_active) AS active_avg,
       max(connections_idle_in_tx) AS idle_tx_max, max(max_connections) AS max_connections,
       (array_agg(minute_utc ORDER BY connections_total DESC))[1] AS conn_peak_at,
       max(waiting_on_locks) AS locks_max, max(longest_query_seconds) AS longest_query_max,
       sum(dx) FILTER (WHERE ok) AS xacts, sum(dt) FILTER (WHERE ok) AS seconds,
       max(dx / dt) FILTER (WHERE ok) AS tps_peak,
       (array_agg(minute_utc ORDER BY CASE WHEN ok THEN dx / dt END DESC NULLS LAST))[1] AS tps_peak_at,
       sum(dhit) FILTER (WHERE ok AND dhit >= 0 AND dread >= 0) AS hits,
       sum(dread) FILTER (WHERE ok AND dhit >= 0 AND dread >= 0) AS reads,
       sum(ddead) FILTER (WHERE ok AND ddead >= 0) AS deadlocks,
       sum(dtemp) FILTER (WHERE ok AND dtemp >= 0) AS temp_bytes,
       sum(dlh) FILTER (WHERE lfc_ok) AS lfc_hits, sum(dlm) FILTER (WHERE lfc_ok) AS lfc_misses,
       max(size_bytes) AS size_max
FROM d
GROUP BY GROUPING SETS ((date_trunc('hour', minute_utc)), ())
ORDER BY is_total, hour
"""


def _hour_row(r: Any) -> dict[str, Any]:
    hits, reads = float(r["hits"] or 0), float(r["reads"] or 0)
    lfc_hits, lfc_misses = float(r["lfc_hits"] or 0), float(r["lfc_misses"] or 0)
    return {
        "hour": _iso(r["hour"]),
        "samples": int(r["samples"]),
        "connectionsMax": int(r["conn_max"] or 0),
        "connectionsAvg": _round(r["conn_avg"], 1),
        "connectionsPeakAtUtc": _iso(r["conn_peak_at"]),
        "activeMax": int(r["active_max"] or 0),
        "activeAvg": _round(r["active_avg"], 1),
        "idleInTransactionMax": int(r["idle_tx_max"] or 0),
        "maxConnections": int(r["max_connections"]) if r["max_connections"] is not None else None,
        "waitingOnLocksMax": int(r["locks_max"] or 0),
        "longestQuerySecondsMax": _round(r["longest_query_max"], 1),
        "transactions": int(r["xacts"] or 0),
        "tps": _round(float(r["xacts"]) / float(r["seconds"]), 2) if r["xacts"] is not None and r["seconds"] else None,
        "tpsPeak": _round(r["tps_peak"], 2),
        "tpsPeakAtUtc": _iso(r["tps_peak_at"]) if r["tps_peak"] is not None else None,
        "cacheHitRatio": _ratio(hits, hits + reads),
        "blocksRead": int(reads),
        "lfcHitRatio": _ratio(lfc_hits, lfc_hits + lfc_misses) if r["lfc_hits"] is not None else None,
        "lfcMisses": int(lfc_misses) if r["lfc_misses"] is not None else None,
        "deadlocks": int(r["deadlocks"] or 0),
        "tempBytes": int(r["temp_bytes"] or 0),
        "sizeBytes": int(r["size_max"]) if r["size_max"] is not None else None,
    }


def neon_database(connection: Connection, since: datetime, now: datetime) -> dict[str, Any]:
    if connection.execute(text("SELECT to_regclass('platform_ops.db_minute_stats')")).scalar_one() is None:
        return {"sampled": False, "latest": None, "window": None, "hourly": [], "lfcAvailable": False,
                "note": "The per-minute database samples start once migration 0179 has run."}
    latest = connection.execute(
        text("SELECT * FROM platform_ops.db_minute_stats ORDER BY minute_utc DESC LIMIT 1")
    ).mappings().one_or_none()
    if latest is None:
        return {"sampled": False, "latest": None, "window": None, "hourly": [], "lfcAvailable": False,
                "note": "The database is sampled every minute once this release is running; nothing recorded yet."}
    rows = connection.execute(text(_HOURLY_SQL), {"since": since}).mappings().all()
    hourly = [_hour_row(r) for r in rows if not r["is_total"]]
    totals = next((_hour_row(r) for r in rows if r["is_total"]), None)
    if totals is not None:
        totals.pop("hour", None)
    total_conns = sum(int(latest[c] or 0) for c in ("connections_active", "connections_idle",
                                                     "connections_idle_in_tx", "connections_other"))
    age = (now - latest["minute_utc"]).total_seconds()
    return {
        "sampled": True,
        "latest": {
            "minuteUtc": _iso(latest["minute_utc"]),
            "stale": age > SAMPLE_STALE_SECONDS + 60,
            "connections": {"active": int(latest["connections_active"]), "idle": int(latest["connections_idle"]),
                            "idleInTransaction": int(latest["connections_idle_in_tx"]),
                            "other": int(latest["connections_other"])},
            "connectionsTotal": total_conns,
            "maxConnections": latest["max_connections"],
            "waitingOnLocks": int(latest["waiting_on_locks"]),
            "longestQuerySeconds": _round(latest["longest_query_seconds"], 1),
            "longestTransactionSeconds": _round(latest["longest_transaction_seconds"], 1),
            "sizeBytes": latest["size_bytes"],
        },
        "window": totals if totals and totals["samples"] else None,
        "hourly": hourly,
        "lfcAvailable": latest["lfc_hits"] is not None,
        "note": None if latest["lfc_hits"] is not None else
        "Neon's local file cache figures need the neon extension; the buffer hit ratio is shown instead.",
    }


# ---------------------------------------------------------------- status and alerts

def _probe_tone(probe: dict[str, Any]) -> str | None:
    if not probe.get("configured"):
        return None
    return "OK" if probe.get("ok") else "CRITICAL"


def _worst(*states: str | None) -> str:
    order = {"CRITICAL": 0, "WARNING": 1, "OK": 2}
    known = [s for s in states if s in order]
    return min(known, key=lambda s: order[s]) if known else "NOT_CONFIGURED"


def web_status(probe: dict[str, Any], analytics: dict[str, Any]) -> str:
    rate_state = None
    if analytics.get("available"):
        rate, errors = analytics.get("errorRate") or 0, analytics.get("errors") or 0
        rate_state = ("CRITICAL" if rate > WEB_ERROR_RATE_CRITICAL and errors >= MIN_ERRORS_FOR_ALERT else
                      "WARNING" if rate > WEB_ERROR_RATE_WARNING and errors >= MIN_ERRORS_FOR_ALERT else "OK")
    elif analytics.get("configured"):
        rate_state = "WARNING"
    return _worst(_probe_tone(probe), rate_state)


def r2_status(probe: dict[str, Any], analytics: dict[str, Any]) -> str:
    rate_state = None
    if analytics.get("available"):
        rate, errors = analytics.get("serverErrorRate") or 0, analytics.get("serverErrors") or 0
        rate_state = "WARNING" if rate > R2_ERROR_RATE_WARNING and errors >= MIN_ERRORS_FOR_ALERT else "OK"
    elif analytics.get("configured"):
        rate_state = "WARNING"
    return _worst(_probe_tone(probe), rate_state)


def _cache_ratio(db: dict[str, Any]) -> tuple[str, float | None]:
    window = db.get("window") or {}
    if db.get("lfcAvailable") and window.get("lfcHitRatio") is not None:
        return "local file cache", window["lfcHitRatio"]
    if (window.get("blocksRead") or 0) >= MIN_READS_FOR_CACHE_ALERT:
        return "buffer cache", window.get("cacheHitRatio")
    return "buffer cache", None


def neon_alerts(db: dict[str, Any], api: dict[str, Any]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []

    def add(severity: str, code: str, message: str) -> None:
        alerts.append({"severity": severity, "code": code, "message": message, "service": "neon"})

    latest, window = db.get("latest") or {}, db.get("window") or {}
    if db.get("sampled") and latest.get("stale"):
        add("WARNING", "NEON_SAMPLES_STALE", f"The database has not been sampled since {latest['minuteUtc']}.")
    peak, limit = window.get("connectionsMax") or 0, window.get("maxConnections") or latest.get("maxConnections")
    if limit and peak >= NEON_CONNECTIONS_RATIO * limit:
        add("CRITICAL" if peak >= 0.95 * limit else "WARNING", "NEON_CONNECTIONS_PEAK",
            f"Database connections peaked at {peak} of {limit} at {window.get('connectionsPeakAtUtc')}.")
    longest = latest.get("longestQuerySeconds") or 0
    if not latest.get("stale") and longest > LONG_QUERY_SECONDS:
        add("WARNING", "NEON_LONG_QUERY", f"A database query has been running for {longest / 60:.0f} minutes.")
    elif (window.get("longestQuerySecondsMax") or 0) > LONG_QUERY_SECONDS:
        add("WARNING", "NEON_LONG_QUERY",
            f"A database query ran for {window['longestQuerySecondsMax'] / 60:.0f} minutes in this window.")
    kind, ratio = _cache_ratio(db)
    if ratio is not None and ratio < CACHE_HIT_MIN:
        add("WARNING", "NEON_CACHE_HIT_LOW",
            f"The database's {kind} hit ratio was {ratio:.1%} in this window (below {CACHE_HIT_MIN:.0%}): "
            "reads are going to storage.")
    if api.get("configured") and not api.get("available"):
        add("WARNING", "NEON_API_UNAVAILABLE", f"Neon's API could not be read: {api.get('error')}")
    for e in api.get("endpoints") or []:
        if e.get("type") == "read_write" and e.get("state") not in (None, "active", "idle"):
            add("WARNING", "NEON_ENDPOINT_STATE", f"The Neon compute {e.get('id')} is {e.get('state')}.")
    return alerts


def infrastructure_alerts(web: dict[str, Any], r2: dict[str, Any], neon: dict[str, Any]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []

    def add(severity: str, code: str, message: str, service: str) -> None:
        alerts.append({"severity": severity, "code": code, "message": message, "service": service})

    probe, analytics = web["probe"], web["analytics"]
    if probe.get("configured") and not probe.get("ok"):
        add("CRITICAL", "WEB_PROBE_FAILED", f"The web app ({probe.get('url')}) did not load: {probe.get('error')}",
            "cloudflare-web")
    if analytics.get("available"):
        rate, errors = analytics.get("errorRate") or 0, analytics.get("errors") or 0
        if rate > WEB_ERROR_RATE_WARNING and errors >= MIN_ERRORS_FOR_ALERT:
            add("CRITICAL" if rate > WEB_ERROR_RATE_CRITICAL else "WARNING", "WEB_ERRORS",
                f"The web Worker failed {errors} of {analytics['requests']} requests ({rate:.1%}) in this window.",
                "cloudflare-web")
    elif analytics.get("configured"):
        add("WARNING", "CLOUDFLARE_ANALYTICS_UNAVAILABLE", f"Cloudflare analytics could not be read: {analytics.get('error')}",
            "cloudflare-web")
    probe, analytics = r2["probe"], r2["analytics"]
    if probe.get("configured") and not probe.get("ok"):
        add("CRITICAL", "R2_PROBE_FAILED", f"The R2 bucket {r2.get('bucket')} did not answer: {probe.get('error')}",
            "cloudflare-r2")
    if analytics.get("available"):
        rate, errors = analytics.get("serverErrorRate") or 0, analytics.get("serverErrors") or 0
        if rate > R2_ERROR_RATE_WARNING and errors >= MIN_ERRORS_FOR_ALERT:
            add("WARNING", "R2_ERRORS", f"R2 answered {errors} of {analytics['operations']} operations with a server "
                f"error ({rate:.1%}) in this window.", "cloudflare-r2")
    return alerts + neon_alerts(neon["database"], neon["api"])


def neon_status(db: dict[str, Any], api: dict[str, Any], alerts: list[dict[str, Any]]) -> str:
    mine = [a["severity"] for a in alerts if a.get("service") == "neon"]
    if "CRITICAL" in mine:
        return "CRITICAL"
    if "WARNING" in mine:
        return "WARNING"
    return "OK" if db.get("sampled") or api.get("available") else "NOT_CONFIGURED"


def infrastructure_health(connection: Connection, hours: int) -> dict[str, Any]:
    now = datetime.now(UTC)
    since = now - timedelta(hours=hours)
    external = fetch_external(hours)
    web = {
        "worker": _env("CLOUDFLARE_WEB_WORKER", DEFAULT_WEB_WORKER),
        "probe": external["webProbe"],
        "analytics": external["webAnalytics"],
    }
    web["status"] = web_status(web["probe"], web["analytics"])
    r2 = {"bucket": external.get("bucket"), "probe": external["r2Probe"], "analytics": external["r2Analytics"]}
    r2["status"] = r2_status(r2["probe"], r2["analytics"])
    neon = {
        "projectId": neon_project_id(),
        "database": neon_database(connection, since, now),
        "api": external["neonApi"],
        "cpuNote": "Neon's public API reports compute used per hour (CU-hours: the compute's average size that "
                   "hour, between its autoscaling minimum and maximum). Per-minute CPU % is only in the Neon "
                   "console's Monitoring page or its OpenTelemetry export.",
    }
    alerts = infrastructure_alerts(web, r2, neon)
    neon["status"] = neon_status(neon["database"], neon["api"], alerts)
    order = {"CRITICAL": 0, "WARNING": 1}
    alerts.sort(key=lambda a: order.get(a["severity"], 2))
    return {"generatedAtUtc": now.isoformat(), "windowHours": hours, "web": web, "r2": r2, "neon": neon,
            "alerts": alerts}


@router.get("/v1/admin/system-health/infrastructure")
def system_health_infrastructure(
    _admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
) -> dict[str, Any]:
    return infrastructure_health(connection, hours)
