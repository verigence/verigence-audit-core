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
        c.execute(text("DROP TABLE IF EXISTS auditcore.job_reads_probe"))
        c.execute(text("CREATE TABLE auditcore.job_reads_probe (k int PRIMARY KEY, v jsonb NOT NULL)"))
        c.execute(text("INSERT INTO auditcore.job_reads_probe VALUES (1, '{\"n\": [1]}')"))
    yield engine
    with engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS auditcore.job_reads_probe"))
    engine.dispose()


_READ = "SELECT k, v FROM auditcore.job_reads_probe WHERE k = ANY(:ks) ORDER BY k"


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
        _elsewhere(engine, "UPDATE auditcore.job_reads_probe SET v='{\"n\": [2]}' WHERE k=1")
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
    "UPDATE auditcore.job_reads_probe SET v='{\"n\": [5]}' WHERE k=1",
    "INSERT INTO auditcore.job_reads_probe VALUES (2, '{}') ON CONFLICT DO NOTHING",
    "DELETE FROM auditcore.job_reads_probe WHERE k=404",
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
        c.execute(text("SELECT count(*) FROM auditcore.job_reads_probe")).scalar_one()
        c.execute(text("SELECT k FROM auditcore.job_reads_probe WHERE k=1 FOR UPDATE")).all()
        with c.begin_nested():
            c.execute(text("SELECT 1"))
        job_reads.rows(c, _READ, {"ks": [1]})
    assert sum("job_reads_probe WHERE k = ANY" in s for s in seen) == 1


def test_a_rollback_to_a_savepoint_makes_it_read_again(engine):
    with job_reads.job(), engine.begin() as c:
        with pytest.raises(RuntimeError), c.begin_nested():
            c.execute(text("UPDATE auditcore.job_reads_probe SET v='{\"n\": [8]}' WHERE k=1"))
            assert job_reads.rows(c, _READ, {"ks": [1]}) == [{"k": 1, "v": {"n": [8]}}]
            raise RuntimeError("the step failed")
        assert job_reads.rows(c, _READ, {"ks": [1]}) == [{"k": 1, "v": {"n": [1]}}]


def test_nothing_is_carried_into_the_next_transaction(engine):
    with job_reads.job():
        with engine.begin() as c:
            assert job_reads.rows(c, _READ, {"ks": [1]}) == [{"k": 1, "v": {"n": [1]}}]
        _elsewhere(engine, "UPDATE auditcore.job_reads_probe SET v='{\"n\": [3]}' WHERE k=1")
        with engine.begin() as c:  # most likely the same pooled connection
            assert job_reads.rows(c, _READ, {"ks": [1]}) == [{"k": 1, "v": {"n": [3]}}]
        with engine.connect() as c:  # commit and carry on, on one Connection
            job_reads.rows(c, _READ, {"ks": [1]})
            c.commit()
            _elsewhere(engine, "UPDATE auditcore.job_reads_probe SET v='{\"n\": [4]}' WHERE k=1")
            assert job_reads.rows(c, _READ, {"ks": [1]}) == [{"k": 1, "v": {"n": [4]}}]


def test_within_one_transaction_the_job_keeps_what_it_first_read(engine):
    """What the layer is for, and its one visible difference: a commit made
    elsewhere between two reads of the same thing is not seen by the second."""
    with job_reads.job(), engine.begin() as c:
        first = job_reads.rows(c, _READ, {"ks": [1]})
        _elsewhere(engine, "UPDATE auditcore.job_reads_probe SET v='{\"n\": [6]}' WHERE k=1")
        assert job_reads.rows(c, _READ, {"ks": [1]}) == first
        c.execute(text("UPDATE auditcore.job_reads_probe SET v=v WHERE k=404"))  # the job writes
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
            _elsewhere(engine, "UPDATE auditcore.job_reads_probe SET v='{\"n\": [9]}' WHERE k=1")
            assert job_reads.rows(c, _READ, {"ks": [1]}) != first
    finally:
        other.dispose()


def test_a_statement_that_is_not_a_plain_read_is_never_held(engine):
    seen = _statements(engine)
    locking = "SELECT k FROM auditcore.job_reads_probe WHERE k=:k FOR UPDATE"
    with job_reads.job(), engine.begin() as c:
        job_reads.rows(c, locking, {"k": 1})
        job_reads.rows(c, locking, {"k": 1})
    assert sum("FOR UPDATE" in s for s in seen) == 2


def test_one_more_than_one_row_is_refused(engine):
    with engine.begin() as c:
        c.execute(text("INSERT INTO auditcore.job_reads_probe VALUES (2, '{}')"))
        with pytest.raises(RuntimeError):
            job_reads.one(c, _READ, {"ks": [1, 2]})
        assert job_reads.one(c, _READ, {"ks": [404]}) is None
        assert job_reads.scalar(c, _READ, {"ks": [404]}) is None
        c.rollback()


def test_switched_off_it_holds_nothing(engine, monkeypatch):
    monkeypatch.setattr(job_reads, "_ENABLED", False)
    with job_reads.job(), engine.begin() as c:
        assert not job_reads.active(c)


# ── a write to a table nothing else follows forgets only the reads that name it ──
@pytest.mark.parametrize(("statement", "table"), [
    (("INSERT INTO auditcore.p2_control_state (a) VALUES (1) ON CONFLICT (a) DO UPDATE SET "
      "n=auditcore.p2_control_state.n + 1, t=now()"), "p2_control_state"),
    ("  update auditcore.commercial_lines AS c SET amount=:a FROM auditcore.evidence e WHERE e.id=c.id", "commercial_lines"),
    ("DELETE FROM auditcore.rule_executions WHERE tenant_id=:t", "rule_executions"),
    (("INSERT INTO auditcore.rule_executions (a) SELECT :a WHERE NOT EXISTS "
      "(SELECT 1 FROM auditcore.rule_executions r WHERE r.a=:a FOR UPDATE)"), "rule_executions"),
    ("-- why\nINSERT INTO auditcore.p2_work_queue (a) VALUES (1) RETURNING work_id", "p2_work_queue"),
])
def test_a_plain_write_to_such_a_table_is_recognised(statement, table):
    assert job_reads.only_table_written(statement) == table
    assert not job_reads.leaves_rows_alone(statement)


@pytest.mark.parametrize("statement", [
    "INSERT INTO auditcore.evidence (a) VALUES (1)",  # documents: everything is read from them
    "UPDATE auditcore.payments SET amount=1",  # triggers of its own
    "UPDATE auditcore.p2_tasks SET x=1",  # triggers and a cascade
    "WITH gone AS (DELETE FROM auditcore.rule_executions RETURNING *) SELECT * FROM gone",
    "INSERT INTO auditcore.rule_executions (a) SELECT auditcore.p2_request_stage_recompute(:t, :j)",
    "UPDATE auditcore.commercial_lines SET a=(SELECT set_config('app.tenant_id', 'x', true))",
    "INSERT INTO public.somewhere VALUES (1)",
    "INSERT INTO p2_control_state VALUES (1)",
    "TRUNCATE auditcore.rule_executions",
    "SELECT 1",
])
def test_anything_else_is_not(statement):
    assert job_reads.only_table_written(statement) is None


def test_the_tables_a_read_names():
    assert job_reads.tables_read(
        "SELECT 1 FROM auditcore.evidence e JOIN auditcore . \"journey_document_extracted_fields\" f ON true "
        "-- auditcore.not_this_one\n WHERE EXISTS (SELECT 1 FROM AUDITCORE.Payments)"
    ) == {"evidence", "journey_document_extracted_fields", "payments"}
    assert job_reads.tables_read("SELECT 1") == frozenset()


def test_tables_written_alone_touch_only_their_own_rows(engine):
    """Each table of OWN_ROWS_ONLY, against the schema as migrated: no trigger but
    the updated_at stamp, no foreign key that cascades from it, no view or policy
    built on it. A migration that changes this must take the table out of the set."""
    problems: list[str] = []
    with engine.begin() as c:
        for table in sorted(job_reads.OWN_ROWS_ONLY):
            if c.execute(text("SELECT to_regclass(:n)"), {"n": f"auditcore.{table}"}).scalar_one() is None:
                problems.append(f"{table}: no such table")
                continue
            triggers = c.execute(text(
                "SELECT t.tgname, p.proname FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid "
                "WHERE t.tgrelid=CAST(:n AS regclass) AND NOT t.tgisinternal"), {"n": f"auditcore.{table}"}).all()
            problems += [f"{table}: trigger {name} -> {function}" for name, function in triggers
                         if function != "set_updated_at"]
            cascades = c.execute(text(
                "SELECT CAST(conrelid AS regclass)::text FROM pg_constraint WHERE contype='f' "
                "AND confrelid=CAST(:n AS regclass) AND (confdeltype NOT IN ('a','r') OR confupdtype NOT IN ('a','r'))"),
                {"n": f"auditcore.{table}"}).scalars().all()
            problems += [f"{table}: a foreign key of {child} follows it" for child in cascades]
            views = c.execute(text(
                "SELECT DISTINCT view_schema || '.' || view_name FROM information_schema.view_table_usage "
                "WHERE table_schema='auditcore' AND table_name=:t"), {"t": table}).scalars().all()
            problems += [f"{table}: view {view} reads it" for view in views]
            rules = c.execute(text(
                "SELECT rulename FROM pg_rules WHERE schemaname='auditcore' AND tablename=:t"), {"t": table}).scalars().all()
            problems += [f"{table}: rule {rule}" for rule in rules]
            policies = c.execute(text(
                "SELECT tablename || '.' || policyname FROM pg_policies WHERE schemaname='auditcore' AND tablename<>:t "
                "AND (coalesce(qual, '') || coalesce(with_check, '')) ~ ('auditcore\\.' || :t || '\\M')"),
                {"t": table}).scalars().all()
            problems += [f"{table}: policy {policy} reads it" for policy in policies]
        # Reads are matched to writes by the schema-qualified name: a bare name must not resolve.
        assert "auditcore" not in c.execute(text("SHOW search_path")).scalar_one()
        stamp = c.execute(text(
            "SELECT prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='auditcore' AND p.proname='set_updated_at'")).scalar_one()
        assert not job_reads._NAMES.search(stamp) and "EXECUTE" not in stamp.upper()
    assert problems == []


_SWITCHES = "SELECT source_code FROM auditcore.p2_task_switches WHERE tenant_id=:t"
_VALUES = "SELECT amount FROM auditcore.commercial_line_source_values WHERE tenant_id=:t"


def _asked(seen, table) -> int:
    return sum(s.lstrip().startswith("SELECT") and f"FROM auditcore.{table}" in s for s in seen)


def test_writing_its_results_does_not_make_a_job_read_its_documents_again(engine):
    seen = _statements(engine)
    with job_reads.job(), engine.begin() as c:
        job_reads.rows(c, _SWITCHES, {"t": "job-reads-nobody"})
        job_reads.rows(c, _VALUES, {"t": "job-reads-nobody"})
        c.execute(text("UPDATE auditcore.p2_control_state SET updated_at_utc=updated_at_utc WHERE tenant_id='job-reads-nobody'"))
        c.execute(text("DELETE FROM auditcore.rule_executions WHERE tenant_id='job-reads-nobody'"))
        job_reads.rows(c, _SWITCHES, {"t": "job-reads-nobody"})
        job_reads.rows(c, _VALUES, {"t": "job-reads-nobody"})
        assert (_asked(seen, "p2_task_switches"), _asked(seen, "commercial_line_source_values")) == (1, 1)
        # Its own table written: that read is asked again, the other is not.
        c.execute(text("UPDATE auditcore.commercial_line_source_values SET amount=amount WHERE tenant_id='job-reads-nobody'"))
        job_reads.rows(c, _SWITCHES, {"t": "job-reads-nobody"})
        job_reads.rows(c, _VALUES, {"t": "job-reads-nobody"})
        assert (_asked(seen, "p2_task_switches"), _asked(seen, "commercial_line_source_values")) == (1, 2)
        # A table other rows follow: everything is asked again.
        c.execute(text("UPDATE auditcore.evidence SET linked_at_utc=linked_at_utc WHERE tenant_id='job-reads-nobody'"))
        job_reads.rows(c, _SWITCHES, {"t": "job-reads-nobody"})
        job_reads.rows(c, _VALUES, {"t": "job-reads-nobody"})
        assert (_asked(seen, "p2_task_switches"), _asked(seen, "commercial_line_source_values")) == (2, 3)


def test_a_read_that_names_no_table_of_the_schema_is_never_held(engine):
    seen = _statements(engine)
    with job_reads.job(), engine.begin() as c:
        job_reads.rows(c, "SELECT count(*) AS n FROM pg_class WHERE relname=:r", {"r": "x"})
        job_reads.rows(c, "SELECT count(*) AS n FROM pg_class WHERE relname=:r", {"r": "x"})
    assert sum("FROM pg_class WHERE relname" in s for s in seen) == 2


# ── a word inside a comment or a quoted value is never taken for SQL ──────────
@pytest.mark.parametrize(("statement", "code"), [
    ("SELECT 'it''s -- not a comment' AS a -- the car sold's own RC\nFROM t", "SELECT '' AS a  \nFROM t"),
    ("SELECT /* DELETE\n FROM x */ 1", "SELECT   1"),
    ("SELECT E'a\\'b; DELETE FROM t' , \"col--umn\"", "SELECT E'' , \"col--umn\""),
    ("SELECT $$ UPDATE t SET a=1 $$, $fn$ x $fn$", "SELECT '', ''"),
    ("SELECT a #>> '{}' FROM t WHERE b ~ '^T[0-9]{4}'", "SELECT a #>> '' FROM t WHERE b ~ ''"),
])
def test_what_is_left_of_a_statement_is_its_sql(statement, code):
    assert job_reads._code(statement) == code


@pytest.mark.parametrize("statement", ["SELECT 'never ends", "SELECT 1 /* never ends", 'SELECT "never ends', "SELECT $a$ never ends"])
def test_a_statement_that_cannot_be_read_forgets_everything(statement):
    assert job_reads._code(statement) is None
    assert not job_reads.is_plain_read(statement)
    assert not job_reads.leaves_rows_alone(statement)
    assert job_reads.only_table_written("UPDATE auditcore.rule_executions SET a='never ends") is None


def test_quoted_words_do_not_decide():
    read = "SELECT 1 FROM auditcore.p2_work_queue WHERE work_type <> 'DELETE' AND note = 'SET; UPDATE; set_config('"
    assert job_reads.is_plain_read(read)
    hidden = "SELECT '--' AS dash, x FROM (WITH gone AS (DELETE FROM auditcore.evidence RETURNING 1) SELECT 1 x FROM gone) q"
    assert not job_reads.is_plain_read(hidden) and not job_reads.leaves_rows_alone(hidden)
    assert job_reads.tables_read("SELECT to_regclass('auditcore.in_a_string') FROM auditcore.evidence") == {"evidence"}
    assert job_reads.only_table_written(
        "UPDATE auditcore.rule_executions SET reason='see auditcore.fn(x); DELETE' WHERE a=1") == "rule_executions"


@pytest.mark.parametrize("statement", [
    "SELECT clock_timestamp()", "SELECT random() FROM auditcore.evidence", "SELECT gen_random_uuid()",
])
def test_a_read_that_is_not_the_same_twice_is_never_held(statement):
    assert not job_reads.is_plain_read(statement)
