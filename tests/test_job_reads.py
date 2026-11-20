"""A job reads the same thing once (audit_core.job_reads, 2026-11-20): what may be
held, and everything that makes the job forget it."""
import os
import threading

import pytest
from sqlalchemy import create_engine, text

from audit_core import job_reads


# ── which statements may be held, and which leave what is held alone ──────────
@pytest.mark.parametrize("statement", [
    "SELECT 1",
    "  select a, b FROM auditcore.evidence e WHERE e.tenant_id=:t",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "SELECT updated_at_utc, deleted_flag FROM t WHERE work_type='SAME_FILE_DELETE'",
    "SELECT 1 -- a comment that says update, delete and set_config(\n FROM t",
    "SELECT /* INSERT INTO nothing */ 1",
])
def test_a_plain_read_may_be_held(statement):
    assert job_reads.is_plain_read(statement)
    assert job_reads.leaves_rows_alone(statement)


@pytest.mark.parametrize("statement", [
    "SELECT * FROM auditcore.p2_tasks WHERE tenant_id=:t FOR UPDATE",
    "SELECT * FROM t FOR NO KEY UPDATE SKIP LOCKED",
    "SELECT * FROM t FOR SHARE",
])
def test_a_locking_read_is_not_held_and_forgets_nothing(statement):
    assert not job_reads.is_plain_read(statement)
    assert job_reads.leaves_rows_alone(statement)


@pytest.mark.parametrize("statement", ["SAVEPOINT sa_savepoint_1", "RELEASE SAVEPOINT sa_savepoint_1"])
def test_a_savepoint_opened_or_released_forgets_nothing(statement):
    assert not job_reads.is_plain_read(statement)
    assert job_reads.leaves_rows_alone(statement)


@pytest.mark.parametrize("statement", [
    "INSERT INTO auditcore.evidence (a) VALUES (1)",
    "UPDATE auditcore.evidence SET a=1",
    "DELETE FROM auditcore.evidence",
    "WITH gone AS (DELETE FROM t RETURNING *) SELECT * FROM gone",
    "WITH put AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM put",
    "SELECT auditcore.p2_request_stage_recompute(:t, :j)",
    "SELECT * FROM auditcore . p2_requeue_verifying_controls (:t)",
    "SELECT set_config('app.tenant_id', :tenant_id, true)",
    "SELECT nextval('auditcore.some_seq')",
    "SELECT pg_advisory_xact_lock(1)",
    "SELECT pg_notify('p2', 'x')",
    "SELECT * INTO scratch FROM t",
    "ROLLBACK TO SAVEPOINT sa_savepoint_1",
    "CALL something()",
    "SHOW standard_conforming_strings",
    "TRUNCATE t",
    "",
])
def test_anything_that_could_change_a_row_forgets_everything(statement):
    assert not job_reads.is_plain_read(statement)
    assert not job_reads.leaves_rows_alone(statement)


# ── against a database ────────────────────────────────────────────────────────
@pytest.fixture()
def engine():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is required")
    engine = create_engine(url)
    job_reads.install(engine)
    job_reads.install(engine)  # twice is once
    with engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS public.job_reads_probe"))
        c.execute(text("CREATE TABLE public.job_reads_probe (k int PRIMARY KEY, v jsonb NOT NULL)"))
        c.execute(text("INSERT INTO public.job_reads_probe VALUES (1, '{\"n\": [1]}')"))
    yield engine
    with engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS public.job_reads_probe"))
    engine.dispose()


_READ = "SELECT k, v FROM public.job_reads_probe WHERE k = ANY(:ks) ORDER BY k"


def _statements(engine):
    seen: list[str] = []
    from sqlalchemy import event

    event.listen(engine, "before_cursor_execute", lambda c, cur, s, p, ctx, m: seen.append(s))
    return seen


def _elsewhere(engine, sql):
    """Another transaction commits a change."""
    with engine.begin() as other:
        other.execute(text(sql))


def test_outside_a_job_every_read_goes_to_the_database(engine):
    seen = _statements(engine)
    with engine.begin() as c:
        assert not job_reads.active(c)
        first = job_reads.rows(c, _READ, {"ks": [1]})
        _elsewhere(engine, "UPDATE public.job_reads_probe SET v='{\"n\": [2]}' WHERE k=1")
        second = job_reads.rows(c, _READ, {"ks": [1]})
    assert first == [{"k": 1, "v": {"n": [1]}}] and second == [{"k": 1, "v": {"n": [2]}}]
    assert sum("job_reads_probe WHERE k = ANY" in s for s in seen) == 2


def test_inside_a_job_the_same_read_is_asked_once(engine):
    seen = _statements(engine)
    job_reads.reset_counts()
    with job_reads.job(), engine.begin() as c:
        assert job_reads.active(c)
        first = job_reads.rows(c, _READ, {"ks": [1]})
        again = job_reads.rows(c, _READ, {"ks": [1]})
        assert job_reads.one(c, _READ, {"ks": [1]}) == first[0]
        assert job_reads.scalar(c, _READ, {"ks": [1]}) == 1
        other = job_reads.rows(c, _READ, {"ks": [1, 2]})  # other parameters: its own question
    assert first == again == other == [{"k": 1, "v": {"n": [1]}}]
    assert sum("job_reads_probe WHERE k = ANY" in s for s in seen) == 2
    assert job_reads.counts() == {"asked": 5, "answered_from_job": 3}


def test_each_caller_gets_its_own_rows(engine):
    with job_reads.job(), engine.begin() as c:
        first = job_reads.rows(c, _READ, {"ks": [1]})
        first[0]["v"]["n"].append(99)
        first[0]["k"] = 7
        assert job_reads.rows(c, _READ, {"ks": [1]}) == [{"k": 1, "v": {"n": [1]}}]


@pytest.mark.parametrize("write", [
    "UPDATE public.job_reads_probe SET v='{\"n\": [5]}' WHERE k=1",
    "INSERT INTO public.job_reads_probe VALUES (2, '{}') ON CONFLICT DO NOTHING",
    "DELETE FROM public.job_reads_probe WHERE k=404",
    "SELECT set_config('app.tenant_id', 'x', true)",
])
def test_the_jobs_own_write_makes_it_read_again(engine, write):
    seen = _statements(engine)
    with job_reads.job(), engine.begin() as c:
        job_reads.rows(c, _READ, {"ks": [1]})
        c.execute(text(write))
        after = job_reads.rows(c, _READ, {"ks": [1]})
        c.rollback()
    assert sum("job_reads_probe WHERE k = ANY" in s for s in seen) == 2
    if write.startswith("UPDATE"):
        assert after == [{"k": 1, "v": {"n": [5]}}]


def test_reads_locks_and_savepoints_do_not_make_it_read_again(engine):
    seen = _statements(engine)
    with job_reads.job(), engine.begin() as c:
        job_reads.rows(c, _READ, {"ks": [1]})
        c.execute(text("SELECT count(*) FROM public.job_reads_probe")).scalar_one()
        c.execute(text("SELECT k FROM public.job_reads_probe WHERE k=1 FOR UPDATE")).all()
        with c.begin_nested():
            c.execute(text("SELECT 1"))
        job_reads.rows(c, _READ, {"ks": [1]})
    assert sum("job_reads_probe WHERE k = ANY" in s for s in seen) == 1


def test_a_rollback_to_a_savepoint_makes_it_read_again(engine):
    with job_reads.job(), engine.begin() as c:
        with pytest.raises(RuntimeError), c.begin_nested():
            c.execute(text("UPDATE public.job_reads_probe SET v='{\"n\": [8]}' WHERE k=1"))
            assert job_reads.rows(c, _READ, {"ks": [1]}) == [{"k": 1, "v": {"n": [8]}}]
            raise RuntimeError("the step failed")
        assert job_reads.rows(c, _READ, {"ks": [1]}) == [{"k": 1, "v": {"n": [1]}}]


def test_nothing_is_carried_into_the_next_transaction(engine):
    with job_reads.job():
        with engine.begin() as c:
            assert job_reads.rows(c, _READ, {"ks": [1]}) == [{"k": 1, "v": {"n": [1]}}]
        _elsewhere(engine, "UPDATE public.job_reads_probe SET v='{\"n\": [3]}' WHERE k=1")
        with engine.begin() as c:  # most likely the same pooled connection
            assert job_reads.rows(c, _READ, {"ks": [1]}) == [{"k": 1, "v": {"n": [3]}}]
        with engine.connect() as c:  # commit and carry on, on one Connection
            job_reads.rows(c, _READ, {"ks": [1]})
            c.commit()
            _elsewhere(engine, "UPDATE public.job_reads_probe SET v='{\"n\": [4]}' WHERE k=1")
            assert job_reads.rows(c, _READ, {"ks": [1]}) == [{"k": 1, "v": {"n": [4]}}]


def test_within_one_transaction_the_job_keeps_what_it_first_read(engine):
    """What the layer is for, and its one visible difference: a commit made
    elsewhere between two reads of the same thing is not seen by the second."""
    with job_reads.job(), engine.begin() as c:
        first = job_reads.rows(c, _READ, {"ks": [1]})
        _elsewhere(engine, "UPDATE public.job_reads_probe SET v='{\"n\": [6]}' WHERE k=1")
        assert job_reads.rows(c, _READ, {"ks": [1]}) == first
        c.execute(text("UPDATE public.job_reads_probe SET v=v WHERE k=404"))  # the job writes
        assert job_reads.rows(c, _READ, {"ks": [1]}) == [{"k": 1, "v": {"n": [6]}}]


def test_a_job_is_the_thread_that_runs_it(engine):
    inside: list[bool] = []
    with job_reads.job(), engine.begin() as c:
        thread = threading.Thread(target=lambda: inside.append(job_reads.active(c)))
        thread.start()
        thread.join()
        assert job_reads.active(c)
    assert inside == [False]
    with engine.begin() as c:
        assert not job_reads.active(c)


def test_an_engine_never_installed_holds_nothing(engine):
    other = create_engine(os.environ["DATABASE_URL"])
    try:
        with job_reads.job(), other.begin() as c:
            assert not job_reads.active(c)
            first = job_reads.rows(c, _READ, {"ks": [1]})
            _elsewhere(engine, "UPDATE public.job_reads_probe SET v='{\"n\": [9]}' WHERE k=1")
            assert job_reads.rows(c, _READ, {"ks": [1]}) != first
    finally:
        other.dispose()


def test_a_statement_that_is_not_a_plain_read_is_never_held(engine):
    seen = _statements(engine)
    locking = "SELECT k FROM public.job_reads_probe WHERE k=:k FOR UPDATE"
    with job_reads.job(), engine.begin() as c:
        job_reads.rows(c, locking, {"k": 1})
        job_reads.rows(c, locking, {"k": 1})
    assert sum("FOR UPDATE" in s for s in seen) == 2


def test_one_more_than_one_row_is_refused(engine):
    with engine.begin() as c:
        c.execute(text("INSERT INTO public.job_reads_probe VALUES (2, '{}')"))
        with pytest.raises(RuntimeError):
            job_reads.one(c, _READ, {"ks": [1, 2]})
        assert job_reads.one(c, _READ, {"ks": [404]}) is None
        assert job_reads.scalar(c, _READ, {"ks": [404]}) is None
        c.rollback()


def test_switched_off_it_holds_nothing(engine, monkeypatch):
    monkeypatch.setattr(job_reads, "_ENABLED", False)
    with job_reads.job(), engine.begin() as c:
        assert not job_reads.active(c)
