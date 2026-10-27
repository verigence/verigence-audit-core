"""Service heartbeat, request counts and container CPU/memory for the SuperAdmin
System Health page (product owner 2026-10-27).

Each process writes one row to platform_ops.service_heartbeats when it starts
(a "boot"), refreshes last_seen_at_utc every minute, and stamps stopped_at_utc
when it shuts down cleanly. A restart is therefore a new boot, and a boot that
went quiet without a stop stamp ended unclean (crash, kill, platform restart).

Every 5 seconds it also reads its container's CPU and memory from the cgroup
(falling back to the process's own figures) and keeps the average and the peak
per minute against the container's limits, so a short CPU spike is not
averaged away.

The API also counts its own requests per minute (total, 4xx, 5xx, slow, and
which routes failed). Counting is in memory and never blocks a request. Every
write here is best effort: a failure is logged and dropped, never retried into
a request and never allowed to stop a service.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import Engine, text

logger = structlog.get_logger(__name__)

SAMPLE_SECONDS = 5.0
BEAT_EVERY_SAMPLES = 12          # a beat a minute
DB_COUNTERS_EVERY_BEATS = 15     # API only: DB counters every 15 minutes
PRUNE_EVERY_BEATS = 60           # API only: retention once an hour
_MAX_ROUTES_PER_MINUTE = 20

_BOOT_ID = uuid4()
_lock = threading.Lock()
_minutes: dict[datetime, dict[str, Any]] = {}
_resources: dict[datetime, dict[str, Any]] = {}
_last_cpu: tuple[float, float] | None = None  # (cpu seconds used, wall seconds)
_CGROUP = Path("/sys/fs/cgroup")


def boot_id() -> str:
    return str(_BOOT_ID)


def _minute(now: float | None = None) -> datetime:
    stamp = int((now if now is not None else time.time()) // 60 * 60)
    return datetime.fromtimestamp(stamp, UTC)


# ---------------------------------------------------------------- requests

def record_request(status_code: int, route: str, duration_ms: float, *, slow_ms: float) -> None:
    """Count one answered request in the current minute (health probes excluded by the caller)."""
    with _lock:
        bucket = _minutes.setdefault(_minute(), {"total": 0, "client": 0, "server": 0, "slow": 0,
                                                 "routes": defaultdict(lambda: [0, 0])})
        bucket["total"] += 1
        if duration_ms > slow_ms:
            bucket["slow"] += 1
        if status_code >= 400:
            kind = 1 if status_code >= 500 else 0
            bucket["server" if kind else "client"] += 1
            routes = bucket["routes"]
            if route in routes or len(routes) < _MAX_ROUTES_PER_MINUTE:
                routes[route][kind] += 1


# ---------------------------------------------------------------- resources

def _read_int(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    return None if raw in ("", "max") else int(raw)


def read_container_usage() -> dict[str, float | int | None]:
    """CPU seconds used so far, memory in use, and the limits, from cgroup v2,
    then cgroup v1, then the process itself."""
    cpu_seconds: float | None = None
    memory = memory_limit = None
    cpu_limit: float | None = None
    try:
        for line in (_CGROUP / "cpu.stat").read_text().splitlines():
            if line.startswith("usage_usec "):
                cpu_seconds = int(line.split()[1]) / 1_000_000
        memory = _read_int(_CGROUP / "memory.current")
        memory_limit = _read_int(_CGROUP / "memory.max")
        quota, _, period = (_CGROUP / "cpu.max").read_text().strip().partition(" ")
        if quota != "max" and period:
            cpu_limit = int(quota) / int(period)
    except (OSError, ValueError):
        pass
    if cpu_seconds is None:
        usage = _read_int(_CGROUP / "cpuacct" / "cpuacct.usage")
        if usage is not None:
            cpu_seconds = usage / 1_000_000_000
            memory = _read_int(_CGROUP / "memory" / "memory.usage_in_bytes")
            memory_limit = _read_int(_CGROUP / "memory" / "memory.limit_in_bytes")
            quota_us = _read_int(_CGROUP / "cpu" / "cpu.cfs_quota_us")
            period_us = _read_int(_CGROUP / "cpu" / "cpu.cfs_period_us")
            if quota_us and quota_us > 0 and period_us:
                cpu_limit = quota_us / period_us
    if cpu_seconds is None:
        times = os.times()
        cpu_seconds = times.user + times.system
        try:
            memory = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            memory = None
    if memory_limit is not None and memory_limit >= 1 << 60:  # "unlimited" on cgroup v1
        memory_limit = None
    if cpu_limit is None:
        cpu_limit = float(os.cpu_count() or 0) or None
    return {"cpu_seconds": cpu_seconds, "memory": memory, "memory_limit": memory_limit, "cpu_limit": cpu_limit}


def sample_resources(now: float | None = None) -> None:
    """Take one CPU/memory reading into the current minute."""
    global _last_cpu
    wall = now if now is not None else time.time()
    usage = read_container_usage()
    cores = None
    if _last_cpu is not None and usage["cpu_seconds"] is not None and wall > _last_cpu[1]:
        cores = max(float(usage["cpu_seconds"]) - _last_cpu[0], 0.0) / (wall - _last_cpu[1])
    if usage["cpu_seconds"] is not None:
        _last_cpu = (float(usage["cpu_seconds"]), wall)
    with _lock:
        bucket = _resources.setdefault(_minute(wall), {"cores_sum": 0.0, "cores_n": 0, "cores_max": None,
                                                       "mem_max": None, "mem_limit": None, "cpu_limit": None})
        if cores is not None:
            bucket["cores_sum"] += cores
            bucket["cores_n"] += 1
            bucket["cores_max"] = max(cores, bucket["cores_max"] or 0.0)
        if usage["memory"] is not None:
            bucket["mem_max"] = max(int(usage["memory"]), bucket["mem_max"] or 0)
        bucket["mem_limit"] = usage["memory_limit"]
        bucket["cpu_limit"] = usage["cpu_limit"]


# ---------------------------------------------------------------- writes

def _take_closed(store: dict[datetime, dict[str, Any]], *, include_current: bool) -> dict[datetime, dict[str, Any]]:
    current = _minute()
    with _lock:
        keys = [m for m in store if include_current or m < current]
        return {m: store.pop(m) for m in keys}


def _identity(default_service: str) -> dict[str, str | None]:
    return {
        "service": (os.environ.get("RAILWAY_SERVICE_NAME") or default_service)[:80],
        "replica": (os.environ.get("RAILWAY_REPLICA_ID") or None),
        "deployment": (os.environ.get("RAILWAY_DEPLOYMENT_ID") or None),
        "version": (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("SERVICE_VERSION") or "")[:12] or None,
    }


def record_boot(engine: Engine, default_service: str) -> None:
    ident = _identity(default_service)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO platform_ops.service_heartbeats
                  (boot_id, service_name, replica_id, deployment_id, version)
                VALUES (:b, :s, :r, :d, :v)
                ON CONFLICT (boot_id) DO UPDATE SET last_seen_at_utc = now()
                """
            ),
            {"b": boot_id(), "s": ident["service"], "r": ident["replica"], "d": ident["deployment"],
             "v": ident["version"]},
        )


def beat(engine: Engine, default_service: str, *, include_current_minute: bool = False) -> None:
    """Refresh last_seen and flush finished request and resource minutes."""
    minutes = _take_closed(_minutes, include_current=include_current_minute)
    resources = _take_closed(_resources, include_current=include_current_minute)
    service = _identity(default_service)["service"]
    with engine.begin() as connection:
        seen = connection.execute(
            text("UPDATE platform_ops.service_heartbeats SET last_seen_at_utc = now() WHERE boot_id = :b"),
            {"b": boot_id()},
        )
        if seen.rowcount == 0:
            # The boot row was never written (the database was unreachable, or the
            # tables did not exist yet, when the process started): write it now.
            ident = _identity(default_service)
            connection.execute(
                text(
                    "INSERT INTO platform_ops.service_heartbeats (boot_id, service_name, replica_id, deployment_id, "
                    "version) VALUES (:b, :s, :r, :d, :v) ON CONFLICT (boot_id) DO UPDATE SET last_seen_at_utc = now()"
                ),
                {"b": boot_id(), "s": ident["service"], "r": ident["replica"], "d": ident["deployment"],
                 "v": ident["version"]},
            )
        for minute, bucket in minutes.items():
            routes = {route: {"4xx": counts[0], "5xx": counts[1]} for route, counts in bucket["routes"].items()}
            connection.execute(
                text(
                    """
                    INSERT INTO platform_ops.http_minute_stats
                      (service_name, boot_id, minute_utc, total, client_errors, server_errors, slow, failed_routes)
                    VALUES (:s, :b, :m, :t, :c, :e, :w, CAST(:r AS jsonb))
                    ON CONFLICT (service_name, boot_id, minute_utc) DO UPDATE SET
                      total = platform_ops.http_minute_stats.total + EXCLUDED.total,
                      client_errors = platform_ops.http_minute_stats.client_errors + EXCLUDED.client_errors,
                      server_errors = platform_ops.http_minute_stats.server_errors + EXCLUDED.server_errors,
                      slow = platform_ops.http_minute_stats.slow + EXCLUDED.slow,
                      failed_routes = platform_ops.http_minute_stats.failed_routes || EXCLUDED.failed_routes
                    """
                ),
                {"s": service, "b": boot_id(), "m": minute, "t": bucket["total"], "c": bucket["client"],
                 "e": bucket["server"], "w": bucket["slow"], "r": json.dumps(routes, separators=(",", ":"))},
            )
        for minute, r in resources.items():
            connection.execute(
                text(
                    """
                    INSERT INTO platform_ops.service_resource_minutes
                      (boot_id, service_name, minute_utc, cpu_cores_avg, cpu_cores_max, cpu_limit_cores,
                       memory_bytes_max, memory_limit_bytes)
                    VALUES (:b, :s, :m, :avg, :max, :cl, :mm, :ml)
                    ON CONFLICT (boot_id, minute_utc) DO UPDATE SET
                      cpu_cores_max = GREATEST(platform_ops.service_resource_minutes.cpu_cores_max, EXCLUDED.cpu_cores_max),
                      memory_bytes_max = GREATEST(platform_ops.service_resource_minutes.memory_bytes_max,
                                                  EXCLUDED.memory_bytes_max)
                    """
                ),
                {"b": boot_id(), "s": service, "m": minute,
                 "avg": round(r["cores_sum"] / r["cores_n"], 3) if r["cores_n"] else None,
                 "max": round(r["cores_max"], 3) if r["cores_max"] is not None else None,
                 "cl": round(r["cpu_limit"], 3) if r["cpu_limit"] else None,
                 "mm": r["mem_max"], "ml": r["mem_limit"]},
            )


def snapshot_db_counters(engine: Engine) -> None:
    """Deadlock / rollback / commit counters, so the page can show a window's change."""
    with engine.begin() as connection:
        db = connection.execute(text("SELECT platform_ops.db_health()")).scalar_one()
        counters = {k: db.get(k) for k in ("commits", "rollbacks", "deadlocks", "conflicts", "tempFiles",
                                           "statsResetAtUtc")}
        connection.execute(
            text("INSERT INTO platform_ops.db_counter_snapshots (counters) VALUES (CAST(:c AS jsonb)) "
                 "ON CONFLICT (taken_at_utc) DO NOTHING"),
            {"c": json.dumps(counters, default=str)},
        )


def prune(engine: Engine) -> None:
    with engine.begin() as connection:
        for table, column, days in (
            ("service_heartbeats", "last_seen_at_utc", 30),
            ("http_minute_stats", "minute_utc", 14),
            ("service_resource_minutes", "minute_utc", 14),
            ("railway_events", "received_at_utc", 30),
            ("db_counter_snapshots", "taken_at_utc", 30),
        ):
            connection.execute(text(
                f"DELETE FROM platform_ops.{table} WHERE {column} < now() - make_interval(days => :d)"
            ), {"d": days})


def record_stop(engine: Engine, default_service: str, reason: str) -> None:
    try:
        beat(engine, default_service, include_current_minute=True)
    except Exception as exc:  # noqa: BLE001 - shutting down; never block it
        logger.warning("platform_heartbeat_flush_failed", error=type(exc).__name__)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE platform_ops.service_heartbeats SET stopped_at_utc = now(), last_seen_at_utc = now(), "
                "stop_reason = :r WHERE boot_id = :b"
            ),
            {"b": boot_id(), "r": reason[:80]},
        )


def _safe(action: str, fn, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - health reporting must never take a service down
        logger.warning("platform_heartbeat_failed", action=action, error=type(exc).__name__,
                       detail=str(exc)[:200])


def _tick(engine: Engine, default_service: str, n: int, *, api: bool) -> None:
    _safe("sample", sample_resources)
    if n % BEAT_EVERY_SAMPLES:
        return
    beats = n // BEAT_EVERY_SAMPLES
    _safe("beat", beat, engine, default_service)
    if api and beats % DB_COUNTERS_EVERY_BEATS == 1:
        _safe("db_counters", snapshot_db_counters, engine)
    if api and beats % PRUNE_EVERY_BEATS == 1:
        _safe("prune", prune, engine)


async def run_heartbeat_loop(engine: Engine, default_service: str, *, sample_seconds: float = SAMPLE_SECONDS) -> None:
    """For the API (asyncio lifespan): also snapshots DB counters and prunes. Cancelled on shutdown."""
    await asyncio.to_thread(_safe, "boot", record_boot, engine, default_service)
    await asyncio.to_thread(_safe, "sample", sample_resources)
    n = 0
    try:
        while True:
            await asyncio.sleep(sample_seconds)
            n += 1
            await asyncio.to_thread(_tick, engine, default_service, n, api=True)
    except asyncio.CancelledError:
        await asyncio.to_thread(_safe, "stop", record_stop, engine, default_service, "shutdown")
        raise


def start_heartbeat_thread(
    engine: Engine, default_service: str, stop: threading.Event, *, sample_seconds: float = SAMPLE_SECONDS,
) -> threading.Thread:
    """For a threaded process (the P2 worker). Stops, and stamps the stop, when ``stop`` is set."""

    def _run() -> None:
        _safe("boot", record_boot, engine, default_service)
        _safe("sample", sample_resources)
        n = 0
        while not stop.wait(sample_seconds):
            n += 1
            _tick(engine, default_service, n, api=False)
        _safe("stop", record_stop, engine, default_service, "shutdown")

    thread = threading.Thread(target=_run, name="platform-heartbeat", daemon=True)
    thread.start()
    return thread
