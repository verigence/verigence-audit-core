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


def test_a_job_whose_worker_stopped_is_taken_again_or_closed():
    """2026-11-14: a deploy restarted the worker while a PC's drop was being placed; the row
    stayed RUNNING for ever. A stopped heartbeat now gives a resumable job (a drop, a settle, a
    Drive run) back to PENDING, up to three attempts; any other job is closed as interrupted."""
    engine = database_engine()
    try:
        _jobs(engine, ("BULK_UPLOAD", "t:drop"), ("BOOKING_REBUILD", "t:x-commit"), ("DRIVE_RUN", "t:alive"))
        with engine.begin() as c:
            set_platform_super_admin_context(c)
            c.execute(text("UPDATE auditcore.p2_worker_jobs SET status='RUNNING', attempts=1, worker_id='gone:1', "
                           "started_at_utc=now()-interval '20 minutes', heartbeat_at_utc=now()-interval '10 minutes' "
                           "WHERE tag IN ('t:drop','t:x-commit')"))
            c.execute(text("UPDATE auditcore.p2_worker_jobs SET status='RUNNING', attempts=1, worker_id='here:1', "
                           "started_at_utc=now()-interval '20 minutes', heartbeat_at_utc=now() WHERE tag='t:alive'"))
        assert worker.reclaim_stalled_jobs(engine) == {"resumed": 1, "closed": 1}
        assert _status(engine) == {"BULK_UPLOAD": "PENDING", "BOOKING_REBUILD": "FAILED", "DRIVE_RUN": "RUNNING"}
        # Out of attempts: closed too, never taken a fourth time.
        with engine.begin() as c:
            set_platform_super_admin_context(c)
            c.execute(text("UPDATE auditcore.p2_worker_jobs SET status='RUNNING', attempts=3, "
                           "started_at_utc=now()-interval '20 minutes', heartbeat_at_utc=now()-interval '10 minutes' "
                           "WHERE tag='t:drop'"))
        assert worker.reclaim_stalled_jobs(engine) == {"resumed": 0, "closed": 1}
        # Past its time limit with a live heartbeat: still running, left to finish -- two copies
        # side by side would have the second submit the bulk the first is filling (2026-11-20).
        with engine.begin() as c:
            set_platform_super_admin_context(c)
            c.execute(text("UPDATE auditcore.p2_worker_jobs SET started_at_utc=now()-interval '2 hours' "
                           "WHERE tag='t:alive'"))
        assert worker.reclaim_stalled_jobs(engine) == {"resumed": 0, "closed": 0}
        assert _status(engine)["DRIVE_RUN"] == "RUNNING"
        # Past the hard limit while still beating: hung, taken again.
        with engine.begin() as c:
            set_platform_super_admin_context(c)
            c.execute(text("UPDATE auditcore.p2_worker_jobs SET started_at_utc=now()-interval '7 hours' "
                           "WHERE tag='t:alive'"))
        assert worker.reclaim_stalled_jobs(engine) == {"resumed": 1, "closed": 0}
        claimed = worker.claim_worker_jobs(engine)
        assert [j["job_kind"] for j in claimed] == ["DRIVE_RUN"]
    finally:
        with engine.begin() as c:
            set_platform_super_admin_context(c)
            c.execute(text("DELETE FROM auditcore.p2_worker_jobs"))
        engine.dispose()
