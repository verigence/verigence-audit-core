"""SuperAdmin System Health (product owner 2026-10-27): heartbeats tell restarts
from deploys and catch unclean stops; request counts are kept per minute; the
page reads every Project's queues through the narrow health functions (real
Postgres, as the runtime role)."""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import platform_health
from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_engine,
    require_super_admin_request,
)
from audit_core.main import app
from audit_core.security_integration import SecurityAdminContext
from audit_core.system_health_api import build_alerts, services_health


@pytest.fixture
def engine():
    engine = database_engine()
    yield engine
    engine.dispose()


def _boot(connection, service, *, deployment, started, last_seen, stopped=None, replica="r1"):
    connection.execute(
        text("INSERT INTO platform_ops.service_heartbeats (boot_id, service_name, replica_id, deployment_id, "
             "started_at_utc, last_seen_at_utc, stopped_at_utc) VALUES (:b, :s, :r, :d, :st, :ls, :sp)"),
        {"b": str(uuid4()), "s": service, "r": replica, "d": deployment, "st": started, "ls": last_seen, "sp": stopped},
    )


def test_heartbeat_writes_a_boot_beats_flushes_requests_and_stamps_the_stop(engine, monkeypatch):
    monkeypatch.setattr(platform_health, "_BOOT_ID", uuid4())
    monkeypatch.setattr(platform_health, "_minutes", {})  # requests counted by earlier tests in this process
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", f"svc-{uuid4().hex[:6]}")
    service = platform_health._identity("x")["service"]
    runtime = get_engine()  # the API's own engine: drops to audit_core_runtime
    platform_health.record_boot(runtime, "x")
    platform_health.record_request(200, "/p2/v1/x", 50, slow_ms=2000)
    platform_health.record_request(404, "/p2/v1/x", 50, slow_ms=2000)
    platform_health.record_request(500, "/p2/v1/y", 3000, slow_ms=2000)
    platform_health.record_stop(runtime, "x", "shutdown")
    with engine.begin() as connection:
        boot = connection.execute(
            text("SELECT stopped_at_utc, stop_reason FROM platform_ops.service_heartbeats WHERE boot_id=:b"),
            {"b": platform_health.boot_id()}).one()
        stats = connection.execute(
            text("SELECT sum(total), sum(client_errors), sum(server_errors), sum(slow) "
                 "FROM platform_ops.http_minute_stats WHERE service_name=:s"), {"s": service}).one()
        routes = connection.execute(
            text("SELECT failed_routes FROM platform_ops.http_minute_stats WHERE service_name=:s"), {"s": service},
        ).scalar_one()
    assert boot.stopped_at_utc is not None and boot.stop_reason == "shutdown"
    assert tuple(stats) == (3, 1, 1, 1)
    assert routes == {"/p2/v1/x": {"4xx": 1, "5xx": 0}, "/p2/v1/y": {"4xx": 0, "5xx": 1}}


def test_the_worker_heartbeat_thread_stops_cleanly(engine, monkeypatch):
    monkeypatch.setattr(platform_health, "_BOOT_ID", uuid4())
    stop = threading.Event()
    thread = platform_health.start_heartbeat_thread(engine, f"worker-{uuid4().hex[:6]}", stop, sample_seconds=0.05)
    stop.wait(0.2)
    stop.set()
    thread.join(timeout=5)
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT stopped_at_utc IS NOT NULL FROM platform_ops.service_heartbeats WHERE boot_id=:b"),
            {"b": platform_health.boot_id()}).scalar_one() is True


def test_restarts_are_told_from_deploys_and_silent_services_are_down(engine):
    now = datetime.now(UTC)
    svc, gone = f"api-{uuid4().hex[:6]}", f"gone-{uuid4().hex[:6]}"
    with engine.begin() as connection:
        # Deployment A ran, crashed (no stop stamp) and restarted in A; then deployment B.
        _boot(connection, svc, deployment="A", started=now - timedelta(hours=5), last_seen=now - timedelta(hours=4))
        _boot(connection, svc, deployment="A", started=now - timedelta(hours=4), last_seen=now - timedelta(hours=2),
              stopped=now - timedelta(hours=2))
        _boot(connection, svc, deployment="B", started=now - timedelta(hours=2), last_seen=now - timedelta(seconds=30))
        _boot(connection, gone, deployment="C", started=now - timedelta(hours=3), last_seen=now - timedelta(minutes=20))
        rows = {s["service"]: s for s in services_health(connection, now - timedelta(hours=24), now)}
    assert (rows[svc]["status"], rows[svc]["restarts"], rows[svc]["deploys"], rows[svc]["uncleanStops"]) == \
        ("UP", 1, 1, 1)
    assert rows[gone]["status"] == "DOWN"
    railway = {"webhookConfigured": True, "failedOrCrashed": [], "monitorAlerts": []}
    codes = {a["code"] for a in build_alerts(list(rows.values()), railway, {"projects": []}, {})}
    assert {"SERVICE_SILENT", "SERVICE_RESTARTED", "SERVICE_UNCLEAN_STOP"} <= codes


def test_the_page_is_superadmin_only_and_reads_every_project(engine):
    journey = create_p2_journey(engine, prefix="health")
    with engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.p2_work_queue (tenant_id, journey_id, work_type, work_key, created_at_utc) "
                 "VALUES (:t, :j, 'JOURNEY_RECONCILE', :k, now() - interval '45 minutes')"),
            {"t": journey.tenant_id, "j": journey.journey_id, "k": f"k-{uuid4().hex}"})
        connection.execute(
            text("INSERT INTO platform_ops.service_resource_minutes (boot_id, service_name, minute_utc, cpu_cores_avg, "
                 "cpu_cores_max, cpu_limit_cores, memory_bytes_max, memory_limit_bytes) "
                 "VALUES (:b, 'di-worker-v2', date_trunc('minute', now()), 2, 11, 12, 1700000000, 17179869184)"),
            {"b": str(uuid4())})
        connection.execute(
            text("INSERT INTO platform_ops.railway_events (occurred_at_utc, event_type, status, service_name, "
                 "deployment_id, payload) VALUES (now(), 'Deployment.failed', 'FAILED', 'di-api', :d, '{}')"),
            {"d": str(uuid4())})
    client = TestClient(app, raise_server_exceptions=False)
    try:
        app.dependency_overrides[require_super_admin_request] = lambda: HumanAdminRequest(
            user_id="sa", bearer_token="t",
            admin_context=SecurityAdminContext(user_id="sa", is_super_admin=True, admin_scopes=()))
        body = client.get("/v1/admin/system-health", params={"hours": 24}).json()
    finally:
        app.dependency_overrides.clear()
    [mine] = [p for p in body["pipeline"]["projects"] if p["tenantId"] == journey.tenant_id]
    assert mine["work"]["open"] == 1 and mine["work"]["over30m"] == 1
    codes = {a["code"] for a in body["alerts"]}
    assert {"WORK_OVER_30_MIN", "DEPLOY_FAILED", "CPU_HIGH"} <= codes
    [worker] = [r for r in body["resources"] if r["service"] == "di-worker-v2"]
    assert (worker["cpuMax"], worker["cpuLimit"]) == (11.0, 12.0) and worker["memoryLimitGb"] == 16.0
    assert body["alerts"][0]["severity"] == "CRITICAL" or all(a["severity"] == "WARNING" for a in body["alerts"])
    assert "connections" in body["database"] and "slowQueriesNote" in body["database"]
    # Nothing but counts and codes: no customer or Journey rows on the page.
    assert str(journey.journey_id) not in json.dumps(body)


def test_without_superadmin_the_page_is_refused():
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/v1/admin/system-health").status_code in (401, 403)


def test_resource_readings_keep_the_peak_and_the_average_of_each_minute(monkeypatch):
    readings = iter([
        {"cpu_seconds": 100.0, "memory": 1_000, "memory_limit": 8_000, "cpu_limit": 4.0},
        {"cpu_seconds": 110.0, "memory": 3_000, "memory_limit": 8_000, "cpu_limit": 4.0},  # 2 cores over 5 s
        {"cpu_seconds": 111.0, "memory": 2_000, "memory_limit": 8_000, "cpu_limit": 4.0},  # 0.2 cores
    ])
    monkeypatch.setattr(platform_health, "read_container_usage", lambda: next(readings))
    monkeypatch.setattr(platform_health, "_last_cpu", None)
    monkeypatch.setattr(platform_health, "_resources", {})
    base = 1_800_000_000.0 - (1_800_000_000.0 % 60)
    for offset in (0, 5, 10):
        platform_health.sample_resources(base + offset)
    [bucket] = platform_health._resources.values()
    assert bucket["cores_max"] == pytest.approx(2.0) and bucket["cores_sum"] / bucket["cores_n"] == pytest.approx(1.1)
    assert (bucket["mem_max"], bucket["mem_limit"], bucket["cpu_limit"]) == (3_000, 8_000, 4.0)


def test_container_usage_is_readable_here():
    usage = platform_health.read_container_usage()
    assert usage["cpu_seconds"] is not None and usage["cpu_seconds"] >= 0


def test_railway_webhook_needs_the_secret_and_keeps_only_known_fields(engine):
    client = TestClient(app, raise_server_exceptions=False)
    event = {"type": "Deployment.crashed", "severity": "CRITICAL", "timestamp": "2026-10-27T08:00:00.000Z",
             "details": {"id": "d1", "status": "CRASHED", "commitAuthor": "someone", "commitMessage": "secret plan"},
             "resource": {"project": {"id": "p", "name": "verigence-audit-core"},
                          "environment": {"id": "e", "name": "dev"},
                          "service": {"id": "s", "name": "di-worker"}, "deployment": {"id": f"dep-{uuid4().hex}"}}}
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM platform_ops.webhook_secrets WHERE name='railway'"))
    assert client.post("/v1/platform/railway-webhook", json=event).status_code == 503
    secret = uuid4().hex
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO platform_ops.webhook_secrets (name, secret_sha256) "
                                "VALUES ('railway', encode(sha256(convert_to(:s, 'UTF8')), 'hex'))"), {"s": secret})
    try:
        assert client.post("/v1/platform/railway-webhook", json=event).status_code == 401
        assert client.post("/v1/platform/railway-webhook", json=event,
                           headers={"X-Verigence-Webhook-Secret": "wrong"}).status_code == 401
        assert client.post("/v1/platform/railway-webhook", json=event,
                           headers={"X-Verigence-Webhook-Secret": secret}).status_code == 204
        assert client.post("/v1/platform/railway-webhook?key=wrong", json=event).status_code == 401
        assert client.post(f"/v1/platform/railway-webhook?key={secret}",
                           json={**event, "resource": {**event["resource"], "deployment": {"id": "other"}}}).status_code == 204
        with engine.begin() as connection:
            row = connection.execute(
                text("SELECT event_type, status, service_name, environment_name, payload FROM platform_ops.railway_events "
                     "WHERE deployment_id=:d"), {"d": event["resource"]["deployment"]["id"]}).one()
        assert (row.event_type, row.status, row.service_name, row.environment_name) == \
            ("Deployment.crashed", "CRASHED", "di-worker", "dev")
        assert "secret plan" not in json.dumps(row.payload) and "someone" not in json.dumps(row.payload)
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM platform_ops.webhook_secrets WHERE name='railway'"))


def test_a_beat_writes_the_boot_row_when_the_start_could_not(engine, monkeypatch):
    """Live 2026-10-27: services that started before the tables existed never showed."""
    monkeypatch.setattr(platform_health, "_BOOT_ID", uuid4())
    platform_health.beat(get_engine(), f"late-{uuid4().hex[:6]}")
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM platform_ops.service_heartbeats WHERE boot_id=:b"),
            {"b": platform_health.boot_id()}).scalar_one() == 1

