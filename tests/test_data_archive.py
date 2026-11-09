"""Data archive job (audit_core.data_archive) and its delete path (migration 0201).

Real Postgres: the job runs as the runtime role through job_engine, so the
missing DELETE grant, the SECURITY DEFINER function and row security are all
exercised. R2 is an in-memory double. Every run is limited to this test's own
Projects (tenant_ids) so other rows in the test database are never touched.
"""
from __future__ import annotations

import gzip
import json
import os
from datetime import UTC, datetime, timedelta
from typing import ClassVar
from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import add_evidence, create_p2_journey, database_engine
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from audit_core import data_archive as archive
from audit_core import data_archive_api as archive_api
from audit_core.db import set_platform_super_admin_context, set_tenant_context
from audit_core.dependencies import HumanAdminRequest, require_super_admin_request
from audit_core.main import app
from audit_core.security_integration import SecurityAdminContext
from audit_core.uc03_p2_controls import _latest_executions as controls_latest
from audit_core.uc03_p2_controls import _unit_fingerprint
from audit_core.uc03_p2_diagnostics import REPORTS
from audit_core.uc03_rule_orchestrator import has_already_run
from audit_core.uc03_rule_status_report import _latest_executions as status_latest

RULE = archive.RULES_BY_KEY["rule_executions"]
RECEIPTS = "RECEIPTS_EQUAL_INVOICES"
NOW = datetime.now(UTC).replace(microsecond=0)


class FakeStore:
    """R2 double. ``tamper`` corrupts what is read back for keys containing it;
    ``fail_put_from`` makes the n-th data upload (1-based) fail."""

    def __init__(self, *, tamper: str | None = None, fail_put_from: int | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.tamper = tamper
        self.fail_put_from = fail_put_from
        self.data_puts = 0

    def put_object(self, object_key: str, data: bytes, *, content_type: str) -> None:
        if object_key.endswith(".jsonl.gz"):
            self.data_puts += 1
            if self.fail_put_from is not None and self.data_puts >= self.fail_put_from:
                raise RuntimeError("R2 unavailable")
        self.objects[object_key] = data
        self.content_types[object_key] = content_type

    def get_object(self, object_key: str) -> bytes:
        data = self.objects[object_key]
        if self.tamper and self.tamper in object_key and object_key.endswith(".jsonl.gz"):
            raw = gzip.decompress(data).decode().splitlines()
            return gzip.compress(("\n".join(raw[:-1]) + "\n").encode(), mtime=0)
        return data


def _settings(**overrides) -> archive.ArchiveSettings:
    values = {
        "bucket": "verigence-archive-test",
        "batch_size": 5000,
        "enabled_tables": frozenset({"rule_executions"}),
        "age_days": {"rule_executions": 30},
    }
    values.update(overrides)
    return archive.ArchiveSettings(**values)


def _insert(engine, tenant_id: str, journey_id: UUID, rule_code: str, outcome: str, days_ago: float,
            *, reason: str | None = None, at: datetime | None = None) -> str:
    with engine.begin() as connection:
        return str(connection.execute(
            text(
                """
                INSERT INTO auditcore.rule_executions
                    (tenant_id, journey_id, rule_code, triggering_event, outcome, reason, evaluated_at_utc,
                     correlation_id)
                VALUES (:t, :j, :r, 'TEST', :o, :reason, :at, 'archive-test')
                RETURNING rule_execution_id
                """
            ),
            {"t": tenant_id, "j": journey_id, "r": rule_code, "o": outcome, "reason": reason,
             "at": at or NOW - timedelta(days=days_ago)},
        ).scalar_one())


def _seed(engine, tenant_id: str, journey_id: UUID) -> dict[str, set[str]]:
    """Each (journey, rule) history, and which rows are candidates at 30 and 5 days."""
    other_journey = uuid4()
    c30: set[str] = set()
    young: set[str] = set()
    keep: set[str] = set()

    # P1 on the real Journey: the receipts check, latest FAIL on the account statement.
    c30.add(_insert(engine, tenant_id, journey_id, RECEIPTS, "PASS", 90))
    c30.add(_insert(engine, tenant_id, journey_id, RECEIPTS, "FAIL", 80, reason="old"))
    c30.add(_insert(engine, tenant_id, journey_id, RECEIPTS, "ERROR", 70, reason="timeout"))
    keep.add(_insert(engine, tenant_id, journey_id, RECEIPTS, "FAIL", 5,
                     reason="receipts do not match the account statement"))
    # P2: latest is an ERROR, so the latest non-ERROR (a PASS) stays as well.
    keep.add(_insert(engine, tenant_id, journey_id, "RULE_P2", "PASS", 90))
    c30.add(_insert(engine, tenant_id, journey_id, "RULE_P2", "ERROR", 60))
    keep.add(_insert(engine, tenant_id, journey_id, "RULE_P2", "ERROR", 50))
    # P3: an old history and a younger row the age limit protects.
    c30.add(_insert(engine, tenant_id, journey_id, "RULE_P3", "SKIPPED", 90))
    c30.add(_insert(engine, tenant_id, journey_id, "RULE_P3", "SKIPPED", 85))
    young.add(_insert(engine, tenant_id, journey_id, "RULE_P3", "FAIL", 10))
    keep.add(_insert(engine, tenant_id, journey_id, "RULE_P3", "PASS", 2))
    # P4: two rows tied at the newest time: both stay.
    tied = NOW - timedelta(days=60)
    keep.add(_insert(engine, tenant_id, journey_id, "RULE_P4", "PASS", 0, at=tied))
    keep.add(_insert(engine, tenant_id, journey_id, "RULE_P4", "FAIL", 0, at=tied))
    # P5: one old row: the latest, stays.
    keep.add(_insert(engine, tenant_id, journey_id, "RULE_P5", "PASS", 100))
    # P6: only ERRORs: the older one goes, has_already_run stays False.
    c30.add(_insert(engine, tenant_id, journey_id, "RULE_P6", "ERROR", 90))
    keep.add(_insert(engine, tenant_id, journey_id, "RULE_P6", "ERROR", 80))
    # Same rule as P1 on another Journey: its own pair, one row, stays.
    keep.add(_insert(engine, tenant_id, other_journey, RECEIPTS, "PASS", 100))
    return {"c30": c30, "young": young, "keep": keep}


@pytest.fixture
def setup():
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="archive")
    other_tenant = f"tenant-archive-b-{uuid4().hex[:10]}"
    add_evidence(journey, di_document_id=uuid4(), document_type_key="customer_ledger")
    rows = _seed(engine, journey.tenant_id, journey.journey_id)
    other = _seed(engine, other_tenant, uuid4())
    job = archive.job_engine(os.environ["DATABASE_URL"])
    try:
        yield {"engine": engine, "job": job, "journey": journey, "tenant": journey.tenant_id,
               "other_tenant": other_tenant, "rows": rows, "other_rows": other}
    finally:
        app.dependency_overrides.clear()
        job.dispose()
        delete_tenant_data(engine, journey.tenant_id)
        delete_tenant_data(engine, other_tenant)
        engine.dispose()


def _ids(engine, tenant_id: str) -> set[str]:
    with engine.begin() as connection:
        return {str(i) for i in connection.execute(
            text("SELECT rule_execution_id FROM auditcore.rule_executions WHERE tenant_id=:t"),
            {"t": tenant_id}).scalars()}


def _candidates(engine, tenant_id: str, age_days: int) -> set[str]:
    columns = ["rule_execution_id", "journey_id", "rule_code", "evaluated_at_utc"]
    with engine.begin() as connection:
        rows = connection.execute(
            text(archive.select_batch_sql(RULE, columns, resume=False)),
            {"tenant_id": tenant_id, "cutoff": NOW - timedelta(days=age_days), "limit": 10000},
        ).mappings().all()
    return {str(r["rule_execution_id"]) for r in rows}


def _runs(engine, *, run_id=None, tenant_id=None) -> list[dict]:
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT * FROM auditcore.archive_runs WHERE (CAST(:r AS uuid) IS NULL OR run_id = :r) "
                "AND (CAST(:t AS varchar) IS NULL OR tenant_id = :t) ORDER BY batch_no NULLS LAST"
            ),
            {"r": run_id, "t": tenant_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def _readers(engine, tenant_id: str, journey_id: UUID) -> dict:
    codes = [RECEIPTS, "RULE_P2", "RULE_P3", "RULE_P4", "RULE_P5", "RULE_P6"]
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        status = status_latest(connection, tenant_id=tenant_id, journey_id=journey_id)
        controls = controls_latest(connection, tenant_id=tenant_id, journey_id=journey_id, codes=codes)
        ran = {code: has_already_run(connection, tenant_id=tenant_id, journey_id=journey_id, rule_code=code)
               for code in codes}
        fingerprint = _unit_fingerprint(connection, tenant_id=tenant_id, journey_id=journey_id, unit="P2:DERIVED")
        max_at = connection.execute(
            text("SELECT MAX(evaluated_at_utc) FROM auditcore.rule_executions WHERE tenant_id=:t AND journey_id=:j"),
            {"t": tenant_id, "j": journey_id}).scalar_one()
        diagnostics = {
            name: [dict(r) for r in connection.execute(text(REPORTS[name]), {"t": tenant_id}).mappings()]
            for name in ("receipts_statement_outcomes", "receipts_failing_detail")
        }
    # Tied rows: DISTINCT ON picks either; compare what it can pick from.
    tie = {k: v for k, v in status.items() if k == "RULE_P4"}
    status = {k: v for k, v in status.items() if k != "RULE_P4"}
    controls = {k: v for k, v in controls.items() if k != "RULE_P4"}
    return {"status": status, "controls": controls, "ran": ran, "fingerprint": fingerprint, "max": max_at,
            "diagnostics": diagnostics, "tie_time": {k: v["evaluated_at_utc"] for k, v in tie.items()}}


# ── Candidate selection ───────────────────────────────────────────────────────
def test_candidates_keep_the_latest_and_latest_non_error_row_of_each_pair(setup):
    rows = setup["rows"]
    assert _candidates(setup["engine"], setup["tenant"], 30) == rows["c30"]
    assert not (rows["keep"] & _candidates(setup["engine"], setup["tenant"], 1))


def test_candidates_respect_the_age_limit(setup):
    rows = setup["rows"]
    assert _candidates(setup["engine"], setup["tenant"], 5) == rows["c30"] | rows["young"]
    assert _candidates(setup["engine"], setup["tenant"], 95) == set()
    with setup["engine"].begin() as connection:
        count = connection.execute(text(archive.count_candidates_sql(RULE)),
                                   {"tenant_id": setup["tenant"], "cutoff": NOW - timedelta(days=30)}).scalar_one()
    assert count == len(rows["c30"]) == 7


# ── Dry run ───────────────────────────────────────────────────────────────────
def test_dry_run_counts_and_records_but_writes_and_deletes_nothing(setup):
    before = _ids(setup["engine"], setup["tenant"]) | _ids(setup["engine"], setup["other_tenant"])

    def no_store(bucket):
        raise AssertionError("a dry run must not open the archive store")

    [result] = archive.run_archive(setup["job"], mode=archive.MODE_DRY_RUN, settings=_settings(),
                                   started_by="test", store_factory=no_store, now=NOW,
                                   tenant_ids=[setup["tenant"], setup["other_tenant"]])
    assert result.status == "DRY_RUN"
    assert {t.tenant_id: t.candidate_count for t in result.tenants} == {
        setup["tenant"]: 7, setup["other_tenant"]: 7}
    assert _ids(setup["engine"], setup["tenant"]) | _ids(setup["engine"], setup["other_tenant"]) == before
    [row] = _runs(setup["engine"], run_id=result.run_id, tenant_id=setup["tenant"])
    assert row["mode"] == "DRY_RUN" and row["status"] == "DRY_RUN" and row["candidate_count"] == 7
    assert row["archived_count"] == 0 and row["object_keys"] == [] and row["batch_no"] is None
    assert row["table_key"] == "auditcore.rule_executions" and row["started_by"] == "test"


def test_the_cli_runs_a_dry_run_by_default(setup, monkeypatch, capsys):
    monkeypatch.setenv("AUDIT_CORE_ARCHIVE_BUCKET", "verigence-archive-test")
    monkeypatch.setenv("AUDIT_CORE_ARCHIVE_RULE_EXECUTIONS_ENABLED", "true")
    before = _ids(setup["engine"], setup["tenant"])
    assert archive.main(["--tenant", setup["tenant"], "--started-by", "test-cli"]) == 0
    printed = capsys.readouterr().out  # log lines, then the JSON summary
    [out] = json.loads(printed[printed.index("[\n  {"):])
    assert out["mode"] == "DRY_RUN" and out["candidateCount"] == 7 and out["archivedCount"] == 0
    assert _ids(setup["engine"], setup["tenant"]) == before
    [row] = _runs(setup["engine"], tenant_id=setup["tenant"])
    assert row["status"] == "DRY_RUN" and row["started_by"] == "test-cli"


# ── LIVE ─────────────────────────────────────────────────────────────────────
def test_live_uploads_verifies_deletes_exactly_the_verified_rows_and_records_the_run(setup):
    engine, tenant = setup["engine"], setup["tenant"]
    before_readers = _readers(engine, tenant, setup["journey"].journey_id)
    before_ids = _ids(engine, tenant)
    other_before = _ids(engine, setup["other_tenant"])
    store = FakeStore()

    [result] = archive.run_archive(setup["job"], mode=archive.MODE_LIVE, settings=_settings(batch_size=3),
                                   started_by="test", store_factory=lambda b: store, now=NOW,
                                   tenant_ids=[tenant])

    assert result.status == "SUCCEEDED", result.as_dict()
    assert _ids(engine, tenant) == before_ids - setup["rows"]["c30"]
    assert _ids(engine, setup["other_tenant"]) == other_before  # another Project: untouched

    # Objects: 7 rows in batches of 3 -> 3 data objects + 3 manifests, at the agreed path.
    data_keys = sorted(k for k in store.objects if k.endswith(".jsonl.gz"))
    assert len(data_keys) == 3 and len(store.objects) == 6
    prefix = f"archive/auditcore.rule_executions/{tenant}/{NOW:%Y-%m}/{result.run_id}-"
    assert all(k.startswith(prefix) for k in store.objects)
    archived: list[dict] = []
    for key in data_keys:
        body = store.objects[key]
        manifest = json.loads(store.objects[key.replace(".jsonl.gz", ".manifest.json")])
        lines = [json.loads(line) for line in gzip.decompress(body).decode().splitlines()]
        assert manifest["sha256"] == archive.sha256_hex(body) and manifest["rowCount"] == len(lines)
        assert manifest["columns"] == list(lines[0]) and manifest["table"] == "auditcore.rule_executions"
        assert manifest["minDate"] <= manifest["maxDate"] and manifest["rule"] == RULE.description
        archived.extend(lines)
    assert {r["rule_execution_id"] for r in archived} == setup["rows"]["c30"]
    assert list(archived[0]) == ["rule_execution_id", "tenant_id", "journey_id", "rule_code", "triggering_event",
                                 "outcome", "reason", "audit_finding_id", "evaluated_at_utc", "correlation_id"]
    assert datetime.fromisoformat(archived[0]["evaluated_at_utc"]).tzinfo is not None

    # The run log: one row per batch (with its delete) and the Project's summary.
    runs = _runs(engine, run_id=result.run_id)
    batches = [r for r in runs if r["batch_no"] is not None]
    [summary] = [r for r in runs if r["batch_no"] is None]
    assert [b["archived_count"] for b in batches] == [3, 3, 1]
    assert all(b["status"] == "BATCH_ARCHIVED" and len(b["object_keys"]) == 2 for b in batches)
    assert summary["status"] == "SUCCEEDED" and summary["mode"] == "LIVE"
    assert summary["candidate_count"] == 7 and summary["archived_count"] == 7
    assert sorted(summary["object_keys"]) == sorted(store.objects)
    assert summary["checksums"] == {k: archive.sha256_hex(store.objects[k]) for k in data_keys}

    # Every reader of the execution log answers exactly as before.
    after_readers = _readers(engine, tenant, setup["journey"].journey_id)
    assert after_readers == before_readers
    assert before_readers["ran"]["RULE_P6"] is False and before_readers["ran"]["RULE_P2"] is True
    assert before_readers["diagnostics"]["receipts_failing_detail"]  # the report has rows to compare

    # A second run finds nothing left to archive.
    [again] = archive.run_archive(setup["job"], mode=archive.MODE_LIVE, settings=_settings(),
                                  started_by="test", store_factory=lambda b: store, now=NOW,
                                  tenant_ids=[tenant])
    assert again.status == "SUCCEEDED" and again.tenants[0].archived_count == 0


def test_a_verification_mismatch_deletes_nothing(setup):
    engine, tenant = setup["engine"], setup["tenant"]
    before = _ids(engine, tenant)
    store = FakeStore(tamper=".jsonl.gz")
    [result] = archive.run_archive(setup["job"], mode=archive.MODE_LIVE, settings=_settings(),
                                   started_by="test", store_factory=lambda b: store, now=NOW,
                                   tenant_ids=[tenant])
    assert result.status == "FAILED"
    assert _ids(engine, tenant) == before
    [summary] = _runs(engine, run_id=result.run_id)
    assert summary["status"] == "FAILED" and summary["archived_count"] == 0 and summary["object_keys"] == []
    assert "sha256" in summary["error"] and "its rows were kept" in summary["error"]


def test_a_failure_part_way_keeps_every_row_not_yet_verified(setup):
    engine = setup["engine"]
    # Projects run in the job's own order, which depends on their (random) ids in this test.
    befores = {t: _ids(engine, t) for t in (setup["tenant"], setup["other_tenant"])}
    store = FakeStore(fail_put_from=2)
    [result] = archive.run_archive(setup["job"], mode=archive.MODE_LIVE, settings=_settings(batch_size=3),
                                   started_by="test", store_factory=lambda b: store, now=NOW,
                                   tenant_ids=list(befores))
    assert result.status == "FAILED"
    tenant = result.tenants[0].tenant_id
    [untouched] = [t for t in befores if t != tenant]
    before = befores[tenant]
    [first_key] = [k for k in store.objects if k.endswith(".jsonl.gz")]
    first_batch = {json.loads(line)["rule_execution_id"]
                   for line in gzip.decompress(store.objects[first_key]).decode().splitlines()}
    assert len(first_batch) == 3
    assert _ids(engine, tenant) == before - first_batch
    runs = _runs(engine, run_id=result.run_id)
    assert [(r["batch_no"], r["status"], r["archived_count"]) for r in runs] == [
        (1, "BATCH_ARCHIVED", 3), (None, "FAILED", 3)]
    # The failure stops the table's run: the next Project is not started.
    assert len(result.tenants) == 1
    assert _ids(engine, untouched) == befores[untouched]


def test_live_is_refused_without_the_bucket_or_the_table_switch(setup):
    engine, tenant = setup["engine"], setup["tenant"]
    before = _ids(engine, tenant)
    store = FakeStore()
    for settings, reason in (
        (_settings(bucket=None), "AUDIT_CORE_ARCHIVE_BUCKET"),
        (_settings(enabled_tables=frozenset()), "AUDIT_CORE_ARCHIVE_RULE_EXECUTIONS_ENABLED"),
    ):
        [result] = archive.run_archive(setup["job"], mode=archive.MODE_LIVE, settings=settings,
                                       started_by="test", store_factory=lambda b: store, now=NOW,
                                       tenant_ids=[tenant])
        assert result.status == "REFUSED" and reason in result.refused
        [row] = _runs(engine, run_id=result.run_id)
        assert row["status"] == "REFUSED" and row["tenant_id"] is None and reason in row["error"]
    assert store.objects == {} and _ids(engine, tenant) == before
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE auditcore.archive_runs DISABLE TRIGGER USER"))
        connection.execute(text("DELETE FROM auditcore.archive_runs WHERE tenant_id IS NULL AND started_by='test'"))
        connection.execute(text("ALTER TABLE auditcore.archive_runs ENABLE TRIGGER USER"))


def test_settings_default_to_dry_run_safety():
    settings = archive.ArchiveSettings.from_env({})
    assert settings.bucket is None and settings.enabled_tables == frozenset()
    assert settings.batch_size == 5000 and settings.age_for(RULE) == 30
    custom = archive.ArchiveSettings.from_env({
        "AUDIT_CORE_ARCHIVE_BUCKET": "b", "AUDIT_CORE_ARCHIVE_RULE_EXECUTIONS_ENABLED": "true",
        "AUDIT_CORE_ARCHIVE_RULE_EXECUTIONS_AGE_DAYS": "45", "AUDIT_CORE_ARCHIVE_BATCH_SIZE": "1000"})
    assert custom.bucket == "b" and custom.enabled_tables == {"rule_executions"}
    assert custom.age_for(RULE) == 45 and custom.batch_size == 1000
    with pytest.raises(ValueError):
        archive.ArchiveSettings.from_env({"AUDIT_CORE_ARCHIVE_BATCH_SIZE": "20000"})
    with pytest.raises(ValueError):
        archive.ArchiveSettings.from_env({"AUDIT_CORE_ARCHIVE_RULE_EXECUTIONS_AGE_DAYS": "0"})


def test_encoding_is_stable_and_keeps_json():
    when = datetime(2026, 8, 1, 5, 30, tzinfo=UTC)
    rows = [{"b": 1, "a": when, "j": {"x": [1, "y"]}, "u": UUID(int=1), "n": None}]
    raw = archive.encode_rows(["u", "a", "b", "j", "n"], rows)
    assert raw == (b'{"u":"00000000-0000-0000-0000-000000000001","a":"2026-08-01T05:30:00+00:00",'
                   b'"b":1,"j":{"x":[1,"y"]},"n":null}\n')
    assert archive.gzip_bytes(raw) == archive.gzip_bytes(raw)


# ── The delete function and the runtime role ─────────────────────────────────
def _as_runtime(connection, tenant_id: str | None) -> None:
    connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
    if tenant_id:
        set_tenant_context(connection, tenant_id)


def _call_delete(engine, *, context: str | None, tenant_id: str, table: str, ids: list[str]) -> int:
    with engine.begin() as connection:
        _as_runtime(connection, context)
        return connection.execute(text(archive._DELETE_FUNCTION_SQL),
                                  {"tenant_id": tenant_id, "table": table, "ids": ids}).scalar_one()


def test_the_delete_function_refuses_tables_not_on_its_allow_list(setup):
    engine, tenant = setup["engine"], setup["tenant"]
    journey_id = str(setup["journey"].journey_id)
    for table in ("auditcore.journeys", "auditcore.archive_runs", "rule_executions",
                  "auditcore.rule_executions; DELETE FROM auditcore.journeys"):
        with pytest.raises(DBAPIError, match="ARCHIVE_TABLE_NOT_ALLOWED"):
            _call_delete(engine, context=tenant, tenant_id=tenant, table=table, ids=[journey_id])
    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM auditcore.journeys WHERE journey_id=:j"),
                                  {"j": journey_id}).scalar_one() == 1


def test_the_runtime_role_still_cannot_delete_directly(setup):
    engine, tenant = setup["engine"], setup["tenant"]
    some_id = next(iter(setup["rows"]["c30"]))
    for statement in ("DELETE FROM auditcore.rule_executions WHERE rule_execution_id = :i",
                      "UPDATE auditcore.rule_executions SET reason = 'x' WHERE rule_execution_id = :i",
                      "DELETE FROM auditcore.archive_runs WHERE entry_id = :i",
                      "UPDATE auditcore.archive_runs SET status = 'FAILED' WHERE entry_id = :i"):
        with pytest.raises(ProgrammingError, match="permission denied"), engine.begin() as connection:
            _as_runtime(connection, tenant)
            connection.execute(text(statement), {"i": some_id})
    with engine.begin() as connection:
        assert connection.execute(text(
            "SELECT has_table_privilege('audit_core_runtime', 'auditcore.rule_executions', 'DELETE'), "
            "has_function_privilege('audit_core_runtime', "
            "'auditcore.archive_delete_rows(varchar, text, uuid[])', 'EXECUTE'), "
            "has_function_privilege('public', 'auditcore.archive_delete_rows(varchar, text, uuid[])', 'EXECUTE')"
        )).one() == (False, True, False)
    assert some_id in _ids(engine, tenant)


def test_archive_runs_is_append_only_even_for_the_owner(setup):
    archive.run_archive(setup["job"], mode=archive.MODE_DRY_RUN, settings=_settings(), started_by="test",
                        now=NOW, tenant_ids=[setup["tenant"]])
    for statement in ("UPDATE auditcore.archive_runs SET status='FAILED' WHERE tenant_id=:t",
                      "DELETE FROM auditcore.archive_runs WHERE tenant_id=:t"):
        with pytest.raises(DBAPIError, match="append-only"), setup["engine"].begin() as connection:
            connection.execute(text(statement), {"t": setup["tenant"]})


def test_the_delete_function_never_removes_a_row_the_readers_need(setup):
    engine, tenant = setup["engine"], setup["tenant"]
    before = _ids(engine, tenant)
    candidate = next(iter(setup["rows"]["c30"]))
    for kept in sorted(setup["rows"]["keep"]):
        with pytest.raises(DBAPIError, match="ARCHIVE_SCOPE_MISMATCH"):
            _call_delete(engine, context=tenant, tenant_id=tenant, table=RULE.qualified_name,
                         ids=[candidate, kept])
    assert _ids(engine, tenant) == before  # all or nothing: the candidate stayed too


def test_the_delete_function_respects_the_tenant(setup):
    engine, tenant, other = setup["engine"], setup["tenant"], setup["other_tenant"]
    before, other_before = _ids(engine, tenant), _ids(engine, other)
    mine, theirs = next(iter(setup["rows"]["c30"])), next(iter(setup["other_rows"]["c30"]))
    # Another Project's key under my tenant: refused.
    with pytest.raises(DBAPIError, match="ARCHIVE_SCOPE_MISMATCH"):
        _call_delete(engine, context=tenant, tenant_id=tenant, table=RULE.qualified_name, ids=[mine, theirs])
    # Told to delete for another Project than the transaction's tenant context: refused.
    with pytest.raises(DBAPIError, match="ARCHIVE_TENANT_CONTEXT_MISMATCH"):
        _call_delete(engine, context=tenant, tenant_id=other, table=RULE.qualified_name, ids=[theirs])
    with pytest.raises(DBAPIError, match="ARCHIVE_TENANT_CONTEXT_MISMATCH"):
        _call_delete(engine, context=None, tenant_id=tenant, table=RULE.qualified_name, ids=[mine])
    assert _ids(engine, tenant) == before and _ids(engine, other) == other_before
    # The right tenant, a candidate: deleted.
    assert _call_delete(engine, context=tenant, tenant_id=tenant, table=RULE.qualified_name, ids=[mine]) == 1
    assert _ids(engine, tenant) == before - {mine}


def test_archive_runs_rows_are_row_secured_by_project(setup):
    engine, tenant, other = setup["engine"], setup["tenant"], setup["other_tenant"]
    archive.run_archive(setup["job"], mode=archive.MODE_DRY_RUN, settings=_settings(), started_by="test",
                        now=NOW, tenant_ids=[tenant, other])
    with engine.begin() as connection:
        _as_runtime(connection, other)
        seen = set(connection.execute(text("SELECT tenant_id FROM auditcore.archive_runs "
                                           "WHERE tenant_id IN (:a, :b)"), {"a": tenant, "b": other}).scalars())
    assert seen == {other}
    with engine.begin() as connection:
        _as_runtime(connection, None)
        set_platform_super_admin_context(connection)
        seen = set(connection.execute(text("SELECT tenant_id FROM auditcore.archive_runs "
                                           "WHERE tenant_id IN (:a, :b)"), {"a": tenant, "b": other}).scalars())
    assert seen == {tenant, other}
    with pytest.raises(ProgrammingError, match="row-level security"), engine.begin() as connection:
        _as_runtime(connection, other)
        connection.execute(text(
            "INSERT INTO auditcore.archive_runs (run_id, tenant_id, table_key, mode, status, rule_description, "
            "started_by, started_at_utc) VALUES (gen_random_uuid(), :t, 'x', 'DRY_RUN', 'DRY_RUN', 'x', 'x', now())"
        ), {"t": tenant})


# ── SuperAdmin endpoint ──────────────────────────────────────────────────────
def test_the_super_admin_lists_archive_runs(setup):
    [result] = archive.run_archive(setup["job"], mode=archive.MODE_LIVE, settings=_settings(batch_size=5),
                                   started_by="test", store_factory=lambda b: FakeStore(), now=NOW,
                                   tenant_ids=[setup["tenant"]])
    app.dependency_overrides[require_super_admin_request] = lambda: HumanAdminRequest(
        user_id="sa-1", bearer_token="t",
        admin_context=SecurityAdminContext(user_id="sa-1", is_super_admin=True, admin_scopes=()))
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/v1/admin/archive-runs", params={"runId": str(result.run_id)})
    assert response.status_code == 200, response.text
    [item] = response.json()["items"]
    assert item["status"] == "SUCCEEDED" and item["archivedCount"] == 7 and item["batchNo"] is None
    assert item["tenantId"] == setup["tenant"] and item["table"] == "auditcore.rule_executions"
    with_batches = client.get("/v1/admin/archive-runs",
                              params={"runId": str(result.run_id), "includeBatches": "true"}).json()["items"]
    assert sorted(i["batchNo"] or 0 for i in with_batches) == [0, 1, 2]


def test_only_a_super_admin_may_list_archive_runs(setup):
    app.dependency_overrides.pop(require_super_admin_request, None)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/v1/admin/archive-runs").status_code in (401, 403)


def test_archive_files_are_kept_twelve_months_by_a_bucket_lifecycle_rule():
    """Product owner, 7 Nov 2026: archives are kept for 12 months."""
    from audit_core import data_archive as da

    assert da.retention_days({}) == 365
    assert da.retention_days({"AUDIT_CORE_ARCHIVE_RETENTION_DAYS": "400"}) == 400
    rule = da.retention_lifecycle(365)["Rules"][0]
    assert rule["Expiration"] == {"Days": 365} and rule["Filter"] == {"Prefix": "archive/"}
    assert rule["Status"] == "Enabled" and rule["ID"] == da.RETENTION_RULE_ID

    calls = []

    class _Client:
        def put_bucket_lifecycle_configuration(self, **kwargs):
            calls.append(kwargs)

    class _Store:
        bucket = "deal-guard-archive"

        def _client(self):
            return _Client()

    da.apply_retention(_Store(), 365)
    assert calls == [{"Bucket": "deal-guard-archive", "LifecycleConfiguration": da.retention_lifecycle(365)}]

def test_the_nightly_pass_dry_runs_until_switched_on_then_archives(setup):
    """Product owner, 7 Nov 2026: the archive runs nightly from the worker; a table is archived for
    real only when its switch and the bucket are set, otherwise its counts are recorded."""
    engine, tenant = setup["engine"], setup["tenant"]
    before = _ids(engine, tenant)
    [dry] = archive.nightly_archive(setup["job"], tenant_id=tenant, started_by="test", environ={})
    assert dry.mode == archive.MODE_DRY_RUN and _ids(engine, tenant) == before

    class _Lifecycle(FakeStore):
        bucket = "deal-guard-archive"
        rules: ClassVar[list] = []

        def _client(self):
            store = self

            class _C:
                def put_bucket_lifecycle_configuration(self, **kwargs):
                    store.rules.append(kwargs)

            return _C()

    store = _Lifecycle()
    env = {"AUDIT_CORE_ARCHIVE_BUCKET": "deal-guard-archive", "AUDIT_CORE_ARCHIVE_RULE_EXECUTIONS_ENABLED": "true",
           "AUDIT_CORE_ARCHIVE_RULE_EXECUTIONS_AGE_DAYS": "7"}
    [live] = archive.nightly_archive(setup["job"], tenant_id=tenant, started_by="test", environ=env,
                                     store_factory=lambda b: store)
    assert live.mode == archive.MODE_LIVE and live.status == "SUCCEEDED"
    assert store.rules and store.rules[0]["LifecycleConfiguration"]["Rules"][0]["Expiration"] == {"Days": 365}


def test_the_worker_runs_the_archive_once_a_night(setup, monkeypatch):
    from datetime import UTC, datetime

    from audit_core import uc03_p2_worker as worker

    calls = []
    monkeypatch.setattr(archive, "nightly_archive", lambda engine, **kw: calls.append(kw) or [])
    monkeypatch.setattr(worker, "_DATA_ARCHIVE_UTC", "21:30")
    worker._last_data_archive.clear()
    tenant = setup["tenant"]
    assert worker.run_data_archive(setup["engine"], tenant, now=datetime(2026, 11, 7, 21, 0, tzinfo=UTC)) is None
    night = datetime(2026, 11, 7, 21, 45, tzinfo=UTC)
    assert worker.run_data_archive(setup["engine"], tenant, now=night) == []
    assert worker.run_data_archive(setup["engine"], tenant, now=night) is None  # this worker, same night
    worker._last_data_archive.clear()  # a restart: the durable claim still holds
    assert worker.run_data_archive(setup["engine"], tenant, now=night) is None
    assert len(calls) == 1 and calls[0]["tenant_id"] == tenant
    monkeypatch.setattr(worker, "_DATA_ARCHIVE_UTC", "off")
    worker._last_data_archive.clear()
    assert worker._data_archive_due(tenant, datetime(2026, 11, 8, 22, 0, tzinfo=UTC)) is None


def test_archive_statements_get_the_job_time_limit_on_a_short_timeout_engine():
    """8 Nov 2026: the worker's nightly pass ran on its 10-second engine and Mahindra Orissa's
    candidate count was cancelled. Every archive transaction sets its own limit."""
    from sqlalchemy import create_engine

    url = database_engine().url
    short = create_engine(url, connect_args={"options": "-c statement_timeout=10000"})
    try:
        with short.begin() as connection:
            assert connection.execute(text("SHOW statement_timeout")).scalar_one() == "10s"
            archive._allow_long(connection)
            assert connection.execute(text("SHOW statement_timeout")).scalar_one() == "10min"
        with short.begin() as connection:
            # SET LOCAL ends with the transaction: the API engine keeps its own limit.
            assert connection.execute(text("SHOW statement_timeout")).scalar_one() == "10s"
    finally:
        short.dispose()


# ── System health: Data archive tab ──────────────────────────────────────────
def _super_admin():
    app.dependency_overrides[require_super_admin_request] = lambda: HumanAdminRequest(
        user_id="sa-1", bearer_token="t",
        admin_context=SecurityAdminContext(user_id="sa-1", is_super_admin=True, admin_scopes=()))


def _run(status: str, at: datetime, *, mode: str = "LIVE", error: str | None = None) -> dict:
    return {"status": status, "mode": mode, "startedAtUtc": at, "error": error}


def test_archive_status_is_red_when_last_night_failed_amber_when_stale_or_refused():
    now = datetime(2026, 11, 9, 2, 0, tzinfo=UTC)               # 07:30 IST on 9 Nov
    night = datetime(2026, 11, 8, 21, 30, tzinfo=UTC)           # 03:00 IST on 9 Nov
    failed = archive_api.archive_status([
        _run("SUCCEEDED", night), _run("FAILED", night + timedelta(seconds=20),
                                       error="P2DocumentStorageError: Failed to store archive/x\nTraceback..."),
        _run("SUCCEEDED", night - timedelta(days=1)),
    ], now)
    assert failed["tone"] == "RED" and "1 of 2 runs failed on the night of 2026-11-09" in failed["reason"]
    assert "Failed to store archive/x" in failed["reason"] and "Traceback" not in failed["reason"]
    # An older night's failure does not make last night red.
    assert archive_api.archive_status([_run("SUCCEEDED", night), _run("FAILED", night - timedelta(days=1))],
                                      now)["tone"] == "GREEN"
    assert archive_api.archive_status([_run("SUCCEEDED", night - timedelta(days=2))], now)["tone"] == "AMBER"
    assert archive_api.archive_status([_run("REFUSED", night, error="AUDIT_CORE_ARCHIVE_BUCKET is not set")],
                                      now)["tone"] == "AMBER"
    assert archive_api.archive_status([], now)["tone"] == "AMBER"
    dry = archive_api.archive_status([_run("SUCCEEDED", night, mode="DRY_RUN")], now)
    assert dry["tone"] == "GREEN" and "dry runs only" in dry["reason"]


def test_errors_are_short_and_never_carry_a_secret():
    assert archive_api.short_error(None) is None
    assert archive_api.short_error("x" * 500).endswith("…") and len(archive_api.short_error("x" * 500)) == 200
    masked = archive_api.short_error("boto failed secret=abc123 access_key_id: AKIAXYZ password=p")
    assert "abc123" not in masked and "AKIAXYZ" not in masked and "secret=***" in masked


def test_archive_settings_view_shows_names_and_switches_never_keys():
    env = {"AUDIT_CORE_ARCHIVE_BUCKET": "verigence-archive", "AUDIT_CORE_ARCHIVE_RULE_EXECUTIONS_ENABLED": "on",
           "AUDIT_CORE_ARCHIVE_RETENTION_DAYS": "365", "AUDIT_CORE_ARCHIVE_STORAGE_SECRET_ACCESS_KEY": "s3cr3t",
           "AUDIT_CORE_ARCHIVE_STORAGE_ACCESS_KEY_ID": "AKIA123"}
    view = archive_api.archive_settings_view(env)
    assert view["bucket"] == "verigence-archive" and view["retentionDays"] == 365
    [table] = view["tables"]
    assert table == {**table, "table": "auditcore.rule_executions", "enabled": True, "ageDays": 30}
    assert "s3cr3t" not in json.dumps(view) and "AKIA123" not in json.dumps(view)
    bad = archive_api.archive_settings_view({"AUDIT_CORE_ARCHIVE_RETENTION_DAYS": "forever"})
    assert bad["problem"] and bad["retentionDays"] is None


def test_the_system_health_archive_tab(setup):
    archive.run_archive(setup["job"], mode=archive.MODE_LIVE, settings=_settings(batch_size=5), started_by="test",
                        store_factory=lambda b: FakeStore(), now=NOW, tenant_ids=[setup["tenant"]])
    _super_admin()
    client = TestClient(app, raise_server_exceptions=False)
    body = client.get("/v1/admin/system-health/archive", params={"nights": 2})
    assert body.status_code == 200, body.text
    data = body.json()
    assert data["status"]["tone"] in {"GREEN", "AMBER", "RED"} and data["status"]["reason"]
    mine = [r for n in data["nights"] for r in n["runs"] if r["tenantId"] == setup["tenant"]]
    [run] = [r for r in mine if r["mode"] == "LIVE"]
    assert (run["status"], run["archived"], run["table"]) == ("SUCCEEDED", 7, "auditcore.rule_executions")
    assert run["objects"] >= 2 and run["durationSeconds"] is not None and run["night"]
    assert data["archivedToDate"] >= 7 and data["lastSucceededAtUtc"]
    [table] = data["tables"]
    assert table["table"] == "auditcore.rule_executions" and table["totalBytes"] > 0
    assert data["settings"]["tables"][0]["key"] == "rule_executions"
    app.dependency_overrides.pop(require_super_admin_request, None)
    assert client.get("/v1/admin/system-health/archive").status_code in (401, 403)
