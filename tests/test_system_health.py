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


@pytest.fixture(autouse=True)
def _no_outside_calls(monkeypatch):
    """The page also asks Cloudflare and Neon (system_health_cloud); never from these tests."""
    from audit_core import system_health_cloud

    for name in ("CLOUDFLARE_API_TOKEN", "NEON_API_KEY", "VERIGENCE_WEB_URL", "AUDIT_CORE_CORS_ALLOWED_ORIGINS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(system_health_cloud, "r2_bucket", lambda: (None, None))
    system_health_cloud.clear_cache()
    yield
    system_health_cloud.clear_cache()


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
    platform_health.record_request(404, "/p2/v1/x", 50, slow_ms=2000, error_code="VAC-NF-005")
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
    assert routes == {"/p2/v1/x": {"4xx": 1, "5xx": 0, "codes": {"404 VAC-NF-005": 1}},
                      "/p2/v1/y": {"4xx": 0, "5xx": 1, "codes": {"500": 1}}}


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
            text("INSERT INTO auditcore.p2_work_queue (tenant_id, journey_id, work_type, work_key, created_at_utc, "
                 "queued_at_utc) VALUES (:t, :j, 'JOURNEY_RECONCILE', :k, now() - interval '45 minutes', "
                 "now() - interval '45 minutes')"),
            {"t": journey.tenant_id, "j": journey.journey_id, "k": f"k-{uuid4().hex}"})
        # A Journey's reused recompute row: created on its first day, asked for
        # again a minute ago. It is not "waiting for 12 days" (2026-10-31).
        connection.execute(
            text("INSERT INTO auditcore.p2_work_queue (tenant_id, journey_id, work_type, work_key, created_at_utc, "
                 "queued_at_utc) VALUES (:t, :j, 'STAGE_RECOMPUTE', :k, now() - interval '12 days', "
                 "now() - interval '1 minute')"),
            {"t": journey.tenant_id, "j": journey.journey_id, "k": f"booking:{journey.journey_id}"})
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
    assert mine["work"]["open"] == 2 and mine["work"]["over30m"] == 1
    oldest = datetime.fromisoformat(mine["work"]["oldestOpenAtUtc"])
    assert timedelta(minutes=44) < datetime.now(UTC) - oldest < timedelta(minutes=46)
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


def test_a_process_replaced_by_a_new_deployment_is_not_a_crash(engine):
    """Live 2026-10-27: uvicorn waits for open streams on shutdown and Railway stops
    the old container first, so the old process cannot stamp its stop."""
    now = datetime.now(UTC)
    svc = f"api-{uuid4().hex[:6]}"
    with engine.begin() as connection:
        _boot(connection, svc, deployment="A", replica="ra", started=now - timedelta(hours=2),
              last_seen=now - timedelta(minutes=30))                       # replaced, no stop stamp
        _boot(connection, svc, deployment="B", replica="rb", started=now - timedelta(minutes=31),
              last_seen=now - timedelta(seconds=20))
        rows = {s["service"]: s for s in services_health(connection, now - timedelta(hours=24), now)}
    assert (rows[svc]["deploys"], rows[svc]["restarts"], rows[svc]["uncleanStops"]) == (1, 0, 0)



# --- Document Intelligence (its own database, read over its API) ----------

def _di_payload(tenant_id: str) -> dict:
    now = datetime.now(UTC)
    iso = lambda d: d.isoformat()
    return {
        "generatedAtUtc": iso(now), "windowHours": 24,
        "boots": [
            # Same deployment and replica started twice: one restart; the first went quiet uncleanly.
            {"bootId": str(uuid4()), "service": "di-worker-v2", "replicaId": "r1", "deploymentId": "d1",
             "version": "abc", "startedAtUtc": iso(now - timedelta(hours=2)),
             "lastSeenAtUtc": iso(now - timedelta(hours=1)), "stoppedAtUtc": None},
            {"bootId": str(uuid4()), "service": "di-worker-v2", "replicaId": "r1", "deploymentId": "d1",
             "version": "abc", "startedAtUtc": iso(now - timedelta(minutes=50)),
             "lastSeenAtUtc": iso(now - timedelta(seconds=30)), "stoppedAtUtc": None},
        ],
        "resources": [{"service": "di-api", "cpuMax": 0.5, "cpuAvg": 0.1, "cpuLimit": 8.0, "cpuPeakAtUtc": iso(now),
                       "cpuHotMinutes": 0, "memoryMaxGb": 7.5, "memoryAvgGb": 1.0, "memoryLimitGb": 8.0,
                       "memoryPeakAtUtc": iso(now), "hourly": []}],
        "queues": {
            "byStatus": [
                {"queue": "extraction", "tenantId": tenant_id, "status": "PENDING", "count": 3, "over30m": 2,
                 "oldestInFlightAtUtc": iso(now - timedelta(hours=1))},
                {"queue": "classification", "tenantId": tenant_id, "status": "FAILED", "count": 4, "over30m": 0,
                 "oldestInFlightAtUtc": None},
            ],
            "failures": [{"queue": "classification", "tenantId": tenant_id, "errorCode": "GEMINI_TIMEOUT", "count": 4}],
            "leaseReclaims": 1,
        },
        "schedulerProblems": [{"runName": "nightly_reprocess", "runDate": "2026-10-26", "startedAtUtc": iso(now),
                               "completedAtUtc": None, "errorCode": None}],
        "database": {"database": "railway", "sizeBytes": 1, "maxConnections": 100, "commits": 1, "rollbacks": 0,
                     "deadlocks": 0, "conflicts": 0, "tempFiles": 0, "statsResetAtUtc": None,
                     "connections": {"idle": 85}, "idleInTransactionOver5m": 1, "activeOver1m": 0,
                     "waitingOnLocks": 0, "slowQueries": None, "slowQueriesNote": "pg_stat_statements is not installed"},
    }


def _get_health(monkeypatch, fake):
    from audit_core import system_health_api

    monkeypatch.setattr(system_health_api, "fetch_di_health", fake)
    client = TestClient(app, raise_server_exceptions=False)
    try:
        app.dependency_overrides[require_super_admin_request] = lambda: HumanAdminRequest(
            user_id="sa", bearer_token="t",
            admin_context=SecurityAdminContext(user_id="sa", is_super_admin=True, admin_scopes=()))
        response = client.get("/v1/admin/system-health", params={"hours": 24})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    return response.json()


def test_document_intelligence_health_is_merged_into_the_page(engine, monkeypatch):
    journey = create_p2_journey(engine, prefix="dihealth")
    body = _get_health(monkeypatch, lambda hours: _di_payload(str(journey.tenant_id)))
    [worker] = [s for s in body["services"] if s["service"] == "di-worker-v2"]
    assert (worker["status"], worker["restarts"], worker["uncleanStops"]) == ("UP", 1, 1)
    assert any(r["service"] == "di-api" for r in body["resources"])
    di = body["pipeline"]["documentIntelligence"]
    assert di["available"] is True and di["leaseReclaims"] == 1
    [mine] = [p for p in di["projects"] if p["tenantId"] == str(journey.tenant_id)]
    assert mine["projectName"] != str(journey.tenant_id)  # named, not a bare id
    assert (mine["extraction"]["inFlight"], mine["extraction"]["over30m"], mine["classification"]["failed"]) == (3, 2, 4)
    assert mine["failureReasons"] == [{"queue": "classification", "errorCode": "GEMINI_TIMEOUT", "count": 4}]
    codes = {a["code"] for a in body["alerts"]}
    assert {"DI_JOBS_OVER_30_MIN", "DI_JOBS_FAILED", "DI_LEASE_RECLAIMED", "DI_SCHEDULER_PROBLEM",
            "DI_DB_IDLE_IN_TRANSACTION", "DI_DB_CONNECTIONS_HIGH", "MEMORY_HIGH", "SERVICE_RESTARTED"} <= codes
    assert body["diDatabase"]["database"] == "railway" and body["diDatabase"]["window"] is None
    assert body["alerts"][0]["severity"] == "CRITICAL"


def test_an_unreachable_document_intelligence_is_an_alert_not_an_error(engine, monkeypatch):
    from audit_core.system_health_di import DiHealthUnavailable

    def down(hours):
        raise DiHealthUnavailable("Document Intelligence answered HTTP 503.")

    body = _get_health(monkeypatch, down)
    di = body["pipeline"]["documentIntelligence"]
    assert di["available"] is False and "503" in di["error"]
    assert body["diDatabase"] is None
    assert "DI_HEALTH_UNAVAILABLE" in {a["code"] for a in body["alerts"]}


def test_di_health_is_asked_with_a_di_service_token(monkeypatch):
    import httpx

    from audit_core import system_health_di

    seen = {}

    class FakeOAuth:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def get_service_token(self, *, audience):
            seen["audience"] = audience
            return "svc-token"

    def fake_get(url, *, params, headers, timeout):
        seen.update(url=url, params=params, auth=headers["Authorization"])
        return httpx.Response(200, json={"boots": []}, request=httpx.Request("GET", url))

    for key, value in {"DI_BASE_URL": "https://di.example/", "SECURITY_BASE_URL": "https://sec.example",
                       "SECURITY_CLIENT_ID": "audit-core", "SECURITY_CLIENT_SECRET": "x"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(system_health_di, "SecurityOAuthClient", FakeOAuth)
    monkeypatch.setattr(system_health_di.httpx, "get", fake_get)
    assert system_health_di.fetch_di_health(6) == {"boots": []}
    assert seen == {"audience": "di", "url": "https://di.example/v1/platform/health", "params": {"hours": 6},
                    "auth": "Bearer svc-token"}

    monkeypatch.setattr(system_health_di.httpx, "get",
                        lambda url, **kw: httpx.Response(403, request=httpx.Request("GET", url)))
    with pytest.raises(system_health_di.DiHealthUnavailable, match="403"):
        system_health_di.fetch_di_health(6)


def _admin_get(monkeypatch, path, params):
    from audit_core import system_health_trends
    from audit_core.system_health_di import DiHealthUnavailable

    def unreachable(hours):
        raise DiHealthUnavailable("not in tests")

    monkeypatch.setattr(system_health_trends, "fetch_di_health", unreachable)
    client = TestClient(app, raise_server_exceptions=False)
    try:
        app.dependency_overrides[require_super_admin_request] = lambda: HumanAdminRequest(
            user_id="sa", bearer_token="t",
            admin_context=SecurityAdminContext(user_id="sa", is_super_admin=True, admin_scopes=()))
        response = client.get(path, params=params)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    return response.json()


def test_failed_requests_are_broken_down_by_status_and_error_code(engine, monkeypatch):
    svc = f"api-{uuid4().hex[:6]}"
    auth_route, scope_route, old_route = (f"/p2/v1/{uuid4().hex[:8]}/{x}" for x in ("auth", "scope", "old"))
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO platform_ops.http_minute_stats (service_name, boot_id, minute_utc, total, client_errors, "
                 "server_errors, failed_routes) VALUES (:s, :b, date_trunc('minute', now()) - interval '5 minutes', "
                 "100, 9, 1, CAST(:r AS jsonb))"),
            {"s": svc, "b": str(uuid4()), "r": json.dumps({
                auth_route: {"4xx": 4, "5xx": 1, "codes": {"401 VAC-AUTH-001": 4, "500 VAC-SYS-001": 1}},
                scope_route: {"4xx": 3, "5xx": 0, "codes": {"404 VAC-NF-005": 2, "403 VAC-AUTH-002": 1}},
                old_route: {"4xx": 2, "5xx": 0},  # recorded before the codes were
            })})
    body = _admin_get(monkeypatch, "/v1/admin/system-health/errors", {"hours": 1})
    routes = {r["route"]: r for r in body["byRoute"] if r["service"] == svc}
    assert [c["key"] for c in routes[auth_route]["codes"]] == ["401 VAC-AUTH-001", "500 VAC-SYS-001"]
    assert routes[scope_route]["notClassified"] == 0 and routes[old_route]["notClassified"] == 2
    by_key = {c["key"]: c for c in body["byCode"]}
    auth = by_key["401 VAC-AUTH-001"]
    assert auth["status"] == 401 and auth["category"] == "SECURITY" and "token" in auth["meaning"]
    assert {"service": svc, "route": auth_route, "count": 4} in auth["routes"]
    assert "scope" in by_key["404 VAC-NF-005"]["meaning"]
    # Server errors lead the list.
    assert body["byCode"][0]["status"] >= 500
    assert body["totals"]["notClassified"] >= 2


def test_the_daily_trend_counts_di_outcomes_and_failures_per_ist_day(engine, monkeypatch):
    from p2_support import add_page

    journey = create_p2_journey(engine, prefix="daily")
    add_page(journey, page_number=1, status="READY")
    add_page(journey, page_number=2, status="READY")
    _, blurred, _ = add_page(journey, page_number=3, status="FAILED")
    _, late, _ = add_page(journey, page_number=4, status="FAILED")
    with engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_document_queue SET last_error='DI_QUALITY_IMAGE_BLUR_SCORE' "
                                "WHERE queue_id=:q"), {"q": blurred})
        connection.execute(text("UPDATE auditcore.p2_document_queue SET last_error='NOT_READ_IN_30_MIN' "
                                "WHERE queue_id=:q"), {"q": late})
        connection.execute(
            text("INSERT INTO auditcore.p2_work_queue (tenant_id, journey_id, work_type, work_key, work_status) "
                 "VALUES (:t, :j, 'JOURNEY_RECONCILE', :k, 'DEAD_LETTER')"),
            {"t": journey.tenant_id, "j": journey.journey_id, "k": f"k-{uuid4().hex}"})
    body = _admin_get(monkeypatch, "/v1/admin/system-health/daily",
                      {"days": 7, "tenantId": journey.tenant_id})
    assert len(body["days"]) == 7 and body["tenantId"] == journey.tenant_id
    today = body["days"][-1]
    assert (today["documents"], today["read"], today["badScan"], today["notReadInTime"]) == (4, 2, 1, 1)
    assert today["diFailedPct"] == 50.0 and today["diSystemFailedPct"] == 25.0
    assert today["deadLetters"] == 1
    assert any(p["tenantId"] == journey.tenant_id for p in body["projects"])
    assert any("Document Intelligence's restarts are missing" in n for n in body["notes"])
    assert str(journey.journey_id) not in json.dumps(body)


def test_the_middleware_counts_a_refused_request_with_its_error_code(monkeypatch):
    monkeypatch.setattr(platform_health, "_minutes", {})
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/p2/v1/tenants/t-1/journeys")  # no token
    assert response.status_code == 401
    [bucket] = platform_health._minutes.values()
    codes = bucket["routes"]["/p2/v1/tenants/{tenant_id}/journeys"][2]
    assert dict(codes) == {"401 VAC-AUTH-001": 1}
