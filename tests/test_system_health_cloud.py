"""System Health: Cloudflare web Worker and R2, and the Neon database (product owner 2026-10-31).

The outside APIs are mocked (httpx.MockTransport); the per-minute database
samples run against real Postgres as the runtime role."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from p2_support import database_engine
from sqlalchemy import text

from audit_core import platform_health, system_health_cloud
from audit_core.dependencies import (
    HumanAdminRequest,
    get_engine,
    require_super_admin_request,
)
from audit_core.main import app
from audit_core.security_integration import SecurityAdminContext

TOKEN = "cf-secret-token-value"
_REAL_CLIENT = httpx.Client
NEON_KEY = "neon-secret-key-value"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    for name in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_WEB_WORKER", "CLOUDFLARE_R2_BUCKET",
                 "NEON_API_KEY", "NEON_PROJECT_ID", "NEON_ORG_ID", "VERIGENCE_WEB_URL",
                 "AUDIT_CORE_CORS_ALLOWED_ORIGINS"):
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


def _mock_http(monkeypatch, handler):
    """Every httpx.Client the module opens goes to ``handler``; returns the requests seen."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(system_health_cloud.httpx, "Client",
                        lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(record), **kw))
    return seen


def _worker_answer():
    return {"data": {"viewer": {"accounts": [{
        "total": [{"sum": {"requests": 1000, "errors": 30, "subrequests": 400},
                   "quantiles": {"cpuTimeP50": 1500, "cpuTimeP99": 24000}}],
        "hourly": [
            {"sum": {"requests": 600, "errors": 0, "subrequests": 200}, "quantiles": {"cpuTimeP50": 1200, "cpuTimeP99": 9000},
             "dimensions": {"datetimeHour": "2026-10-31T09:00:00Z", "status": "success"}},
            {"sum": {"requests": 30, "errors": 30, "subrequests": 0}, "quantiles": {"cpuTimeP50": 50000, "cpuTimeP99": 90000},
             "dimensions": {"datetimeHour": "2026-10-31T09:00:00Z", "status": "scriptThrewException"}},
            {"sum": {"requests": 370, "errors": 0, "subrequests": 200}, "quantiles": {"cpuTimeP50": 1600, "cpuTimeP99": 20000},
             "dimensions": {"datetimeHour": "2026-10-31T08:00:00Z", "status": "success"}},
        ]}]}}}


def test_the_worker_is_read_from_cloudflare_analytics_with_a_discovered_account(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", TOKEN)

    def handler(request):
        if request.url.path.endswith("/accounts"):
            return httpx.Response(200, json={"result": [{"id": "acc123", "name": "JBR"}]})
        body = json.loads(request.content)
        assert body["variables"]["accountTag"] == "acc123"
        assert body["variables"]["script"] == "verigence-web-dev"
        assert "workersInvocationsAdaptive" in body["query"]
        return httpx.Response(200, json=_worker_answer())

    seen = _mock_http(monkeypatch, handler)
    first = system_health_cloud.fetch_worker_analytics(24)
    system_health_cloud.fetch_worker_analytics(24)
    assert [r.url.path for r in seen].count("/client/v4/accounts") == 1  # the account id is kept
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in seen)
    assert first["available"] is True
    assert (first["requests"], first["errors"], first["subrequests"], first["errorRate"]) == (1000, 30, 400, 0.03)
    assert (first["cpuTimeP50Ms"], first["cpuTimeP99Ms"]) == (1.5, 24.0)
    assert first["byStatus"][0] == {"status": "success", "requests": 970}
    nine = next(h for h in first["hourly"] if h["hour"].startswith("2026-10-31T09"))
    # The hour's p50 is the busiest status's; its p99 the worst of them.
    assert (nine["requests"], nine["errors"], nine["cpuP50Ms"], nine["cpuP99Ms"]) == (630, 30, 1.2, 90.0)
    assert [h["hour"] for h in first["hourly"]] == ["2026-10-31T08:00:00Z", "2026-10-31T09:00:00Z"]
    assert TOKEN not in json.dumps(first)


def test_a_cloudflare_failure_is_a_node_state_never_an_exception(monkeypatch):
    assert system_health_cloud.fetch_worker_analytics(24)["configured"] is False  # no token
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", TOKEN)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc-override")
    seen = _mock_http(monkeypatch, lambda r: httpx.Response(
        200, json={"data": None, "errors": [{"message": "unknown field \"datetimeHour\""}]}))
    out = system_health_cloud.fetch_worker_analytics(24)
    assert (out["configured"], out["available"]) == (True, False) and "datetimeHour" in out["error"]
    assert not any(r.url.path.endswith("/accounts") for r in seen)  # the override is used as is

    _mock_http(monkeypatch, lambda r: httpx.Response(500))
    assert "HTTP 500" in system_health_cloud.fetch_worker_analytics(6)["error"]

    def down(request):
        raise httpx.ConnectTimeout("slow", request=request)

    _mock_http(monkeypatch, down)
    out = system_health_cloud.fetch_r2_analytics(6, "verigence-di-bucket-dev")
    assert out["available"] is False and "ConnectTimeout" in out["error"] and TOKEN not in json.dumps(out)


def test_r2_operations_are_grouped_by_kind_and_errors_by_status(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", TOKEN)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc1")
    answer = {"data": {"viewer": {"accounts": [{
        "operations": [
            {"sum": {"requests": 900}, "dimensions": {"datetimeHour": "2026-10-31T09:00:00Z", "actionType": "GetObject", "responseStatusCode": 200}},
            {"sum": {"requests": 40}, "dimensions": {"datetimeHour": "2026-10-31T09:00:00Z", "actionType": "HeadObject", "responseStatusCode": 404}},
            {"sum": {"requests": 50}, "dimensions": {"datetimeHour": "2026-10-31T09:00:00Z", "actionType": "PutObject", "responseStatusCode": 200}},
            {"sum": {"requests": 10}, "dimensions": {"datetimeHour": "2026-10-31T10:00:00Z", "actionType": "PutObject", "responseStatusCode": 503}},
        ],
        "storage": [{"max": {"objectCount": 1234, "payloadSize": 5_000_000, "metadataSize": 100, "uploadCount": 2},
                     "dimensions": {"datetime": "2026-10-31T09:30:00Z"}}],
    }]}}}

    def handler(request):
        body = json.loads(request.content)
        assert body["variables"]["bucket"] == "verigence-di-bucket-dev"
        return httpx.Response(200, json=answer)

    _mock_http(monkeypatch, handler)
    out = system_health_cloud.fetch_r2_analytics(24, "verigence-di-bucket-dev")
    assert (out["operations"], out["clientErrors"], out["serverErrors"], out["serverErrorRate"]) == (1000, 40, 10, 0.01)
    assert out["storage"] == {"objectCount": 1234, "payloadBytes": 5_000_000, "metadataBytes": 100,
                              "pendingUploads": 2, "atUtc": "2026-10-31T09:30:00Z"}
    nine, ten = out["hourly"]
    assert (nine["reads"], nine["writes"], nine["clientErrors"]) == (940, 50, 40)
    assert (ten["writes"], ten["serverErrors"]) == (10, 10)
    put = next(a for a in out["byAction"] if a["actionType"] == "PutObject")
    assert (put["requests"], put["serverErrors"]) == (60, 10)


def test_neon_is_not_configured_without_a_key_and_reads_endpoints_and_compute_with_one(monkeypatch):
    out = system_health_cloud.fetch_neon_api(24)
    assert (out["configured"], out["available"], out["projectId"]) == (False, False, "round-scene-14217399")
    monkeypatch.setenv("NEON_API_KEY", NEON_KEY)

    def handler(request):
        path = request.url.path
        if path.endswith("/endpoints"):
            return httpx.Response(200, json={"endpoints": [{
                "id": "ep-1", "type": "read_write", "branch_id": "br-1", "current_state": "active",
                "autoscaling_limit_min_cu": 0.25, "autoscaling_limit_max_cu": 8, "suspend_timeout_seconds": -1,
                "last_active": "2026-10-31T09:59:00Z", "region_id": "aws-ap-southeast-1", "host": "ep-1.neon.tech"}]})
        if path.endswith("/projects/round-scene-14217399"):
            return httpx.Response(200, json={"project": {"id": "round-scene-14217399", "org_id": "org-9"}})
        if path.endswith("/consumption_history/v2/projects"):
            params = request.url.params
            assert (params["org_id"], params["granularity"], params["metrics"], params["project_ids"]) == (
                "org-9", "hourly", "compute_unit_seconds", "round-scene-14217399")
            return httpx.Response(200, json={"projects": [{"project_id": "round-scene-14217399", "periods": [{
                "consumption": [
                    {"timeframe_start": "2026-10-31T08:00:00Z", "metrics": [{"metric_name": "compute_unit_seconds", "value": 900}]},
                    {"timeframe_start": "2026-10-31T09:00:00Z", "metrics": [{"metric_name": "compute_unit_seconds", "value": 14400}]},
                ]}]}]})
        return httpx.Response(404)

    seen = _mock_http(monkeypatch, handler)
    out = system_health_cloud.fetch_neon_api(24)
    assert out["available"] is True and out["error"] is None
    assert out["endpoints"][0] == {
        "id": "ep-1", "type": "read_write", "branchId": "br-1", "state": "active", "pendingState": None,
        "minCu": 0.25, "maxCu": 8, "suspendTimeoutSeconds": -1, "lastActiveAtUtc": "2026-10-31T09:59:00Z",
        "regionId": "aws-ap-southeast-1"}
    assert out["compute"]["hourly"] == [{"hour": "2026-10-31T08:00:00Z", "cuHours": 0.25},
                                        {"hour": "2026-10-31T09:00:00Z", "cuHours": 4.0}]
    assert (out["compute"]["peakCu"], out["compute"]["peakAtUtc"], out["compute"]["totalCuHours"]) == (
        4.0, "2026-10-31T09:00:00Z", 4.25)
    assert all(r.headers["Authorization"] == f"Bearer {NEON_KEY}" for r in seen)
    assert NEON_KEY not in json.dumps(out)

    # The consumption API refusing (plan, rate limit) keeps the endpoint state.
    system_health_cloud.clear_cache()
    _mock_http(monkeypatch, lambda r: handler(r) if not r.url.path.endswith("/v2/projects") else httpx.Response(429))
    out = system_health_cloud.fetch_neon_api(24)
    assert out["available"] is True and out["endpoints"] and out["compute"] is None and "429" in out["error"]


def test_the_live_probes(monkeypatch):
    assert system_health_cloud.probe_web(None)["configured"] is False
    monkeypatch.setattr(system_health_cloud.httpx, "get",
                        lambda url, **kw: httpx.Response(200, request=httpx.Request("GET", url)))
    ok = system_health_cloud.probe_web("https://web.example")
    assert (ok["ok"], ok["statusCode"]) == (True, 200) and ok["latencyMs"] >= 0

    def refused(url, **kw):
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(system_health_cloud.httpx, "get", refused)
    bad = system_health_cloud.probe_web("https://web.example")
    assert bad["ok"] is False and "ConnectError" in bad["error"]

    class Storage:
        def __init__(self, error=None):
            self.error = error

        def probe_bucket(self, *, timeout_seconds):
            assert timeout_seconds <= 5
            if self.error:
                raise self.error

    assert system_health_cloud.probe_r2(None)["configured"] is False
    assert system_health_cloud.probe_r2(Storage())["ok"] is True
    from audit_core.uc03_p2_storage import P2DocumentStorageError

    failed = system_health_cloud.probe_r2(Storage(P2DocumentStorageError("R2 answered 403")))
    assert failed["ok"] is False and failed["error"] == "R2 answered 403"


def test_the_web_url_comes_from_settings(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    assert system_health_cloud.web_url() is None
    monkeypatch.setenv("APP_ENV", "dev")
    assert system_health_cloud.web_url() == "https://verigence-web-dev.jbrconsulting-it.workers.dev"
    monkeypatch.setenv("AUDIT_CORE_CORS_ALLOWED_ORIGINS", "https://a.example, https://b.example")
    assert system_health_cloud.web_url() == "https://a.example"
    monkeypatch.setenv("VERIGENCE_WEB_URL", "https://web.example")
    assert system_health_cloud.web_url() == "https://web.example"


def test_one_outside_call_failing_leaves_the_others(monkeypatch):
    def boom(hours):
        raise RuntimeError("bug")

    monkeypatch.setattr(system_health_cloud, "fetch_worker_analytics", boom)
    out = system_health_cloud.fetch_external(24)
    assert out["webAnalytics"]["available"] is False and "RuntimeError" in out["webAnalytics"]["error"]
    assert out["neonApi"]["configured"] is False and out["webProbe"]["configured"] is False


def _sample(minute, **values):
    row = {"connections_active": 2, "connections_idle": 5, "connections_idle_in_tx": 0, "connections_other": 0,
           "max_connections": 100, "waiting_on_locks": 0, "longest_query_seconds": 1, "longest_transaction_seconds": 1,
           "xact_commit": 0, "xact_rollback": 0, "blks_hit": 0, "blks_read": 0, "deadlocks": 0, "temp_bytes": 0,
           "size_bytes": 10_000_000, "stats_reset_at_utc": datetime(2026, 10, 1, tzinfo=UTC),
           "lfc_hits": None, "lfc_misses": None, "minute_utc": minute}
    row.update(values)
    return row


def _insert(engine, rows):
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM platform_ops.db_minute_stats"))
        for row in rows:
            columns = ", ".join(row)
            connection.execute(text(f"INSERT INTO platform_ops.db_minute_stats ({columns}) VALUES "
                                    f"({', '.join(':' + c for c in row)})"), row)


def test_the_sampler_writes_one_row_a_minute_as_the_runtime_role(engine):
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM platform_ops.db_minute_stats"))
    runtime = get_engine()  # drops to audit_core_runtime, like the API's heartbeat
    platform_health.sample_db_minute(runtime, now=1_800_000_000)
    platform_health.sample_db_minute(runtime, now=1_800_000_030)  # a second replica, same minute
    with engine.begin() as connection:
        rows = connection.execute(text("SELECT * FROM platform_ops.db_minute_stats")).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["minute_utc"] == datetime.fromtimestamp(1_800_000_000 // 60 * 60, UTC)
    assert row["max_connections"] > 0 and row["xact_commit"] > 0 and row["size_bytes"] > 0
    assert row["connections_active"] >= 1  # the sampler's own session is active


def test_neon_database_health_derives_tps_cache_hits_and_peaks_per_hour(engine):
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    base = now - timedelta(minutes=5)
    rows = []
    commits = hits = reads = 0
    for i in range(6):
        # Each minute: 600 transactions (10/s) except a 3000-transaction spike at minute 3.
        commits += 3000 if i == 3 else 600
        hits, reads = hits + 9000, reads + 1000
        rows.append(_sample(base + timedelta(minutes=i), xact_commit=commits, blks_hit=hits, blks_read=reads,
                            deadlocks=1 if i >= 4 else 0, temp_bytes=1024 * i,
                            connections_active=12 if i == 2 else 2, connections_idle=70 if i == 2 else 5,
                            lfc_hits=990 * i, lfc_misses=10 * i, longest_query_seconds=400 if i == 5 else 2))
    _insert(engine, rows)
    with engine.begin() as connection:
        db = system_health_cloud.neon_database(connection, now - timedelta(hours=1), now)
    window = db["window"]
    assert db["sampled"] is True and db["lfcAvailable"] is True and db["note"] is None
    assert (window["connectionsMax"], window["maxConnections"]) == (82, 100)
    assert window["connectionsPeakAtUtc"] == (base + timedelta(minutes=2)).isoformat()
    # 5 deltas: 600, 600, 3000, 600, 600 over 300 s.
    assert window["transactions"] == 5400 and window["tps"] == 18.0
    assert window["tpsPeak"] == 50.0 and window["tpsPeakAtUtc"] == (base + timedelta(minutes=3)).isoformat()
    assert window["cacheHitRatio"] == 0.9 and window["lfcHitRatio"] == 0.99
    assert (window["deadlocks"], window["tempBytes"], window["longestQuerySecondsMax"]) == (1, 5 * 1024, 400.0)
    assert sum(h["samples"] for h in db["hourly"]) == 6
    latest = db["latest"]
    assert latest["connectionsTotal"] == 7 and latest["longestQuerySeconds"] == 400.0 and latest["stale"] is False

    alerts = {a["code"]: a for a in system_health_cloud.neon_alerts(db, {"configured": False})}
    assert {"NEON_CONNECTIONS_PEAK", "NEON_LONG_QUERY"} <= set(alerts) and "NEON_CACHE_HIT_LOW" not in alerts
    assert alerts["NEON_CONNECTIONS_PEAK"]["severity"] == "WARNING"


def test_a_stats_reset_is_not_counted_as_negative_traffic(engine):
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    base = now - timedelta(minutes=10)
    reset = datetime(2026, 10, 31, tzinfo=UTC)
    _insert(engine, [
        _sample(base, xact_commit=1_000_000, blks_hit=100, blks_read=900),
        _sample(base + timedelta(minutes=1), xact_commit=1_000_600, blks_hit=200, blks_read=1800),
        _sample(base + timedelta(minutes=2), xact_commit=50, blks_hit=10, blks_read=10, stats_reset_at_utc=reset),
        _sample(base + timedelta(minutes=3), xact_commit=650, blks_hit=20, blks_read=1010, stats_reset_at_utc=reset),
    ])
    with engine.begin() as connection:
        db = system_health_cloud.neon_database(connection, now - timedelta(hours=1), now)
    assert db["window"]["transactions"] == 1200 and db["window"]["tps"] == 10.0
    assert db["lfcAvailable"] is False and "neon extension" in db["note"]
    # Buffer hits 110 of 2010 blocks: the window has enough reads to judge.
    alerts = {a["code"] for a in system_health_cloud.neon_alerts(db, {"configured": True, "available": False,
                                                                      "error": "HTTP 401"})}
    assert {"NEON_CACHE_HIT_LOW", "NEON_API_UNAVAILABLE"} <= alerts


def test_infrastructure_alerts_and_node_states():
    web = {"probe": {"configured": True, "ok": False, "url": "https://w", "error": "Answered HTTP 502."},
           "analytics": {"configured": True, "available": True, "requests": 1000, "errors": 80, "errorRate": 0.08}}
    r2 = {"bucket": "b", "probe": {"configured": True, "ok": True},
          "analytics": {"configured": True, "available": True, "operations": 500, "serverErrors": 20,
                        "serverErrorRate": 0.04}}
    neon = {"database": {"sampled": False}, "api": {"configured": False}}
    alerts = {a["code"]: a["severity"] for a in system_health_cloud.infrastructure_alerts(web, r2, neon)}
    assert alerts == {"WEB_PROBE_FAILED": "CRITICAL", "WEB_ERRORS": "CRITICAL", "R2_ERRORS": "WARNING"}
    assert system_health_cloud.web_status(web["probe"], web["analytics"]) == "CRITICAL"
    assert system_health_cloud.r2_status(r2["probe"], r2["analytics"]) == "WARNING"
    assert system_health_cloud.r2_status({"configured": False}, {"configured": False}) == "NOT_CONFIGURED"
    quiet = {"configured": True, "available": True, "requests": 100, "errors": 3, "errorRate": 0.03}
    assert system_health_cloud.web_status({"configured": True, "ok": True}, quiet) == "OK"  # too few to judge


def _admin_get(path, params):
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


def test_the_infrastructure_route_and_the_page_banner(engine, monkeypatch):
    from audit_core import system_health_api
    from audit_core.system_health_di import DiHealthUnavailable

    assert TestClient(app).get("/v1/admin/system-health/infrastructure").status_code in (401, 403)
    _insert(engine, [])
    monkeypatch.setenv("VERIGENCE_WEB_URL", "https://web.example")
    monkeypatch.setattr(system_health_cloud.httpx, "get",
                        lambda url, **kw: httpx.Response(503, request=httpx.Request("GET", url)))
    body = _admin_get("/v1/admin/system-health/infrastructure", {"hours": 6})
    assert body["windowHours"] == 6
    assert body["web"]["status"] == "CRITICAL" and body["web"]["probe"]["statusCode"] == 503
    assert body["web"]["analytics"]["configured"] is False and body["r2"]["status"] == "NOT_CONFIGURED"
    assert body["neon"]["api"]["configured"] is False and "CU-hours" in body["neon"]["cpuNote"]
    assert [a["code"] for a in body["alerts"]] == ["WEB_PROBE_FAILED"]

    def no_di(hours):
        raise DiHealthUnavailable("not in tests")

    monkeypatch.setattr(system_health_api, "fetch_di_health", no_di)
    page = _admin_get("/v1/admin/system-health", {"hours": 6})
    assert "WEB_PROBE_FAILED" in {a["code"] for a in page["alerts"]}


def test_the_page_answers_when_the_infrastructure_read_fails(engine, monkeypatch):
    from audit_core import system_health_api
    from audit_core.system_health_di import DiHealthUnavailable

    def broken(connection, hours):
        connection.execute(text("SELECT * FROM platform_ops.no_such_table"))

    def no_di(hours):
        raise DiHealthUnavailable("not in tests")

    monkeypatch.setattr(system_health_api, "infrastructure_health", broken)
    monkeypatch.setattr(system_health_api, "fetch_di_health", no_di)
    page = _admin_get("/v1/admin/system-health", {"hours": 1})
    assert "INFRASTRUCTURE_UNAVAILABLE" in {a["code"] for a in page["alerts"]}
