"""One-off worker jobs as rows, so starting one never restarts the worker
(restart review 2026-10-27). Real Postgres."""
from __future__ import annotations

from p2_support import database_engine
from sqlalchemy import text

from audit_core import uc03_p2_worker as worker
from audit_core.db import set_platform_super_admin_context


def _jobs(engine, *rows):
    with engine.begin() as c:
        set_platform_super_admin_context(c)
        c.execute(text("DELETE FROM auditcore.p2_worker_jobs"))
        for kind, tag in rows:
            c.execute(text("INSERT INTO auditcore.p2_worker_jobs (job_kind, tag, reports) "
                           "VALUES (:k, :t, ARRAY['receipts_failing_detail'])"), {"k": kind, "t": tag})


def _status(engine):
    with engine.begin() as c:
        set_platform_super_admin_context(c)
        return dict(c.execute(text("SELECT job_kind, status FROM auditcore.p2_worker_jobs")).all())


def test_pending_jobs_are_claimed_once_and_run(monkeypatch):
    engine = database_engine()
    ran = []
    real = worker._one_shot_target

    def fake(kind, engine_, tag, reports):
        if kind == "STATEMENT_REREAD":
            return (lambda *a: ran.append((kind, tag, reports))), ()
        return real(kind, engine_, tag, reports)

    monkeypatch.setattr(worker, "_one_shot_target", fake)
    try:
        _jobs(engine, ("STATEMENT_REREAD", "statement-1027-dry"), ("NO_SUCH_JOB", "x"))
        claimed = worker.claim_worker_jobs(engine)
        assert {j["job_kind"] for j in claimed} == {"STATEMENT_REREAD", "NO_SUCH_JOB"}
        assert worker.claim_worker_jobs(engine) == []  # a job is taken once
        for job in claimed:
            worker._run_job(engine, job)
        assert ran == [("STATEMENT_REREAD", "statement-1027-dry", ["receipts_failing_detail"])]
        assert _status(engine) == {"STATEMENT_REREAD": "DONE", "NO_SUCH_JOB": "FAILED"}
    finally:
        with engine.begin() as c:
            set_platform_super_admin_context(c)
            c.execute(text("DELETE FROM auditcore.p2_worker_jobs"))
        engine.dispose()
