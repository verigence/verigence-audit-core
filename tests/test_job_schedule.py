"""The SuperAdmin Jobs page (product owner 2026-11-13): every job that runs on a
clock in one list, the daily ones with a time he can change, and all of them
before 10:00 pm so the night's work is through by midnight."""
from __future__ import annotations

from datetime import UTC, datetime
from datetime import time as clock_time

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import job_schedule, platform_health, sleep_window
from audit_core import uc03_p2_runtime as runtime
from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.dependencies import HumanAdminRequest, require_super_admin_request
from audit_core.main import app
from audit_core.security_integration import SecurityAdminContext
from audit_core.uc03_p2_runtime import claim_scheduled_run

NIGHT = (clock_time(0, 30), clock_time(8, 0))


@pytest.fixture(autouse=True)
def _times_as_at_start(monkeypatch):
    """Each test starts from the defaults and leaves the table as it found it."""
    monkeypatch.setattr(job_schedule, "_times", {k: j.default_ist for k, j in job_schedule.DAILY_JOBS.items()})
    monkeypatch.setattr(job_schedule, "_off", set())
    monkeypatch.setattr(worker, "_NIGHTLY_REVIEW_UTC", worker._NIGHTLY_REVIEW_UTC)
    monkeypatch.setattr(worker, "_DATA_ARCHIVE_UTC", worker._DATA_ARCHIVE_UTC)
    monkeypatch.setattr(runtime, "DAYEND_SWEEP_UTC", runtime.DAYEND_SWEEP_UTC)
    job_schedule.apply({})
    yield


def _clear_table() -> None:
    engine = database_engine()
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM platform_ops.job_schedule_changes"))
        connection.execute(text("DELETE FROM platform_ops.job_schedules"))
    engine.dispose()


@pytest.fixture
def table():
    _clear_table()
    yield
    _clear_table()


@pytest.fixture
def client(table):
    app.dependency_overrides[require_super_admin_request] = lambda: HumanAdminRequest(
        user_id="sa-1", bearer_token="t",
        admin_context=SecurityAdminContext(user_id="sa-1", is_super_admin=True, admin_scopes=()))
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(require_super_admin_request, None)


# ── the times ───────────────────────────────────────────────────────────────

def test_every_daily_job_starts_before_ten_at_night_and_outside_the_sleep(monkeypatch):
    monkeypatch.setattr(sleep_window, "WINDOW", NIGHT)
    for job in job_schedule.DAILY_JOBS.values():
        assert "21:00" <= job.default_ist < "22:00", job.key
        assert not job_schedule.in_sleep_window(job.default_ist)
    assert job_schedule.in_sleep_window("02:00") and not job_schedule.in_sleep_window("00:29")
    # The clean-up runs first in the worker's pass, then the review, as when both were at 02:00.
    assert job_schedule.time_ist("DAYEND_CLEANUP") <= job_schedule.time_ist("NIGHTLY_REVIEW")
    assert job_schedule.time_ist("NIGHTLY_REVIEW") < job_schedule.time_ist("DATA_ARCHIVE")


def test_a_time_is_read_as_a_person_types_it():
    assert job_schedule.parse_hhmm("9:05") == "09:05"
    assert job_schedule.parse_hhmm(" 21:30 ") == "21:30"
    assert job_schedule.parse_hhmm(clock_time(21, 45)) == "21:45"
    assert job_schedule.parse_hhmm("21:45:00") == "21:45"
    for wrong in ("", "24:00", "21:60", "half nine", "2130", None):
        assert job_schedule.parse_hhmm(wrong) is None
    assert job_schedule.ist_to_utc("21:30") == "16:00" and job_schedule.ist_to_utc("02:00") == "20:30"
    assert job_schedule.utc_to_ist("20:30") == "02:00"


def test_a_changed_time_reaches_the_code_that_runs_the_job():
    job_schedule.apply({"NIGHTLY_REVIEW": "20:15", "DAYEND_CLEANUP": "20:00", "DATA_ARCHIVE": "20:45"})
    assert worker._NIGHTLY_REVIEW_UTC == "14:45" and worker._DATA_ARCHIVE_UTC == "15:15"
    assert runtime.DAYEND_SWEEP_UTC == "14:30"
    assert runtime.dayend_sweep_ist() == "20:00 IST"  # what the PC's message quotes
    day = datetime(2026, 11, 14, tzinfo=UTC)
    assert worker._nightly_review_due("tenant-x", day.replace(hour=14, minute=44)) is None
    assert worker._nightly_review_due("tenant-x", day.replace(hour=14, minute=45)) == "2026-11-14"
    assert worker._dayend_sweep_due("tenant-x", day.replace(hour=14, minute=29)) != "2026-11-14"
    assert worker._dayend_sweep_due("tenant-x", day.replace(hour=14, minute=30)) == "2026-11-14"
    assert worker._data_archive_due("tenant-x", day.replace(hour=15, minute=15)) == "2026-11-14"
    job_schedule.apply({"NIGHTLY_REVIEW": "not a time", "NO_SUCH_JOB": "10:00"})  # ignored
    assert job_schedule.time_ist("NIGHTLY_REVIEW") == "20:15"


def test_the_workers_old_variables_still_count_until_a_time_is_set(monkeypatch):
    monkeypatch.setenv("P2_NIGHTLY_REVIEW_UTC", "20:30")
    monkeypatch.setenv("P2_DAYEND_SWEEP_UTC", "25:99")  # a typing mistake: the default
    monkeypatch.setenv("P2_DATA_ARCHIVE_UTC", "off")
    times, off = job_schedule._boot_times()
    assert times["NIGHTLY_REVIEW"] == "02:00" and times["DAYEND_CLEANUP"] == "21:30"
    assert off == {"DATA_ARCHIVE"}
    monkeypatch.setattr(job_schedule, "_off", off)
    job_schedule.apply({"DATA_ARCHIVE": "21:50"})
    assert worker._DATA_ARCHIVE_UTC == "off"  # switched off stays off, whatever its time


# ── the table, the worker and the API ───────────────────────────────────────

def test_the_worker_follows_the_table_within_its_beat_and_never_reads_it_asleep(table, monkeypatch):
    engine = database_engine()
    try:
        with engine.begin() as connection:
            job_schedule.set_time(connection, job_key="DATA_ARCHIVE", time_ist_value="21:10", changed_by="sa-1")
        job_schedule.apply({"DATA_ARCHIVE": "21:50"})  # another process, not yet told
        monkeypatch.setattr(sleep_window, "WINDOW", (clock_time(0, 0), clock_time(23, 59, 59)))
        job_schedule.refresh(engine)
        assert job_schedule.time_ist("DATA_ARCHIVE") == "21:50"  # asleep: the database is left alone
        monkeypatch.setattr(sleep_window, "WINDOW", None)
        monkeypatch.setattr(platform_health, "sample_resources", lambda: None)
        monkeypatch.setattr(platform_health, "beat", lambda *_a, **_k: None)
        platform_health._tick(engine, "verigence-p2-worker", platform_health.BEAT_EVERY_SAMPLES, api=False)
        assert job_schedule.time_ist("DATA_ARCHIVE") == "21:10" and worker._DATA_ARCHIVE_UTC == "15:40"
    finally:
        engine.dispose()


def test_the_page_lists_every_job_and_a_super_admin_changes_a_time(client, monkeypatch):
    monkeypatch.setattr(sleep_window, "WINDOW", NIGHT)
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="jobs")
    try:
        with engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            assert claim_scheduled_run(connection, tenant_id=journey.tenant_id, run_name="P2_NIGHTLY_REVIEW",
                                       run_key="2026-11-12", claimed_by="test")
        body = client.get("/v1/admin/jobs")
        assert body.status_code == 200, body.text
        page = body.json()
        assert page["sleepWindow"] == {"enabled": True, "startIst": "00:30", "endIst": "08:00"}
        daily = {j["key"]: j for j in page["daily"]}
        assert {k: j["timeIst"] for k, j in daily.items()} == {
            "DAYEND_CLEANUP": "21:30", "NIGHTLY_REVIEW": "21:30", "DATA_ARCHIVE": "21:50"}
        assert all(j["editable"] and j["enabled"] and not j["insideSleepWindow"] for j in daily.values())
        assert daily["NIGHTLY_REVIEW"]["lastRunAtUtc"] and daily["NIGHTLY_REVIEW"]["projectsRunLast"] >= 1
        assert daily["DATA_ARCHIVE"]["changedBy"] is None
        assert len(page["recurring"]) >= 10 and all(j["pausedAsleep"] for j in page["recurring"])
        assert {j["service"] for j in page["external"]} >= {"SevaSutra", "Security", "Document Intelligence"}
        assert page["changes"] == []

        changed = client.put("/v1/admin/jobs/DATA_ARCHIVE", json={"timeIst": "2:15"})
        assert changed.status_code == 200, changed.text
        archive = next(j for j in changed.json()["daily"] if j["key"] == "DATA_ARCHIVE")
        assert archive["timeIst"] == "02:15" and archive["defaultTimeIst"] == "21:50"
        assert archive["insideSleepWindow"] is True  # allowed, and said: it wakes the system
        assert archive["changedBy"] == "sa-1" and archive["changedAtUtc"]
        assert worker._DATA_ARCHIVE_UTC == "20:45"
        [change] = changed.json()["changes"]
        assert (change["job"], change["oldTimeIst"], change["newTimeIst"], change["changedBy"]) == (
            "Data archive", "21:50", "02:15", "sa-1")

        again = client.put("/v1/admin/jobs/DATA_ARCHIVE", json={"timeIst": "21:40"}).json()
        assert [(c["oldTimeIst"], c["newTimeIst"]) for c in again["changes"]] == [("02:15", "21:40"), ("21:50", "02:15")]

        assert client.put("/v1/admin/jobs/DATA_ARCHIVE", json={"timeIst": "25:00"}).status_code == 422
        assert client.put("/v1/admin/jobs/NO_SUCH_JOB", json={"timeIst": "21:00"}).status_code == 404
        assert client.put(f"/v1/admin/jobs/drive-runs/{journey.tenant_id}",
                          json={"timeIst": "20:30"}).status_code == 404  # its Drive is not connected
    finally:
        delete_tenant_data(engine, journey.tenant_id)
        engine.dispose()


def test_only_a_super_admin_sees_or_changes_the_jobs(table):
    app.dependency_overrides.pop(require_super_admin_request, None)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/v1/admin/jobs").status_code in (401, 403)
    assert client.put("/v1/admin/jobs/DATA_ARCHIVE", json={"timeIst": "21:00"}).status_code in (401, 403)
    assert job_schedule.time_ist("DATA_ARCHIVE") == "21:50"
