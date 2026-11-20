"""A job reads the same thing from the database once.

The P2 worker handles one Journey per job and inside one job asked Neon the same
questions over and over (product owner 2026-11-20, hosting cost: "definite plan
which doesnt impact the performance of the system but cut down unnecessary
expenses"). Measured on the live database, 16-20 Nov 2026: the Journey's document
facts were read about 950,000 times a day by two statements, the count of a
requirement's documents 325,000 times, a Project's task switch 207,000 times --
some 25 times each per job. Every one is a round trip to Neon and its SQL text is
network traffic Railway bills.

What is held, and for how long
------------------------------
Only reads made through ``rows`` / ``one`` / ``scalar`` below, only while the
calling thread is inside ``job()``, only on an engine ``install`` was called for,
and only until the transaction next changes anything:

* a statement that is not a plain read (INSERT, UPDATE, DELETE, a function of
  the ``auditcore`` schema, ``set_config`` -- it moves the row-level-security
  context -- or anything this module does not recognise) forgets everything held
  on that connection, before it runs;
* except a plain write to one of the tables in ``OWN_ROWS_ONLY`` -- tables whose
  rows nothing else follows (no trigger but the ``updated_at`` stamp, no foreign
  key that cascades, no view, no policy; tests/test_job_reads.py checks each
  against the schema, so a migration that changes that fails the build): it
  forgets only the reads that name that table. A job writes its check results,
  its Deal lines and its queue rows all the way through; those writes do not
  change the documents it read;
* the beginning and the end of a transaction and a rollback to a savepoint
  forget everything.

A read answered from here is therefore what the same statement returned earlier
in the same transaction with nothing written in between. Another transaction's
commit in that gap is not seen by the second read; it was seen by the first or
it bumps the Journey's fact version and requests its own recompute, as before.

Outside a job (the web API, the sweeps, tests) the functions run the statement
every time, exactly as the code they replaced did. Each call returns its own
rows: a caller that changes a value it was given changes nobody else's.

``P2_JOB_READS=off`` switches the holding off without a code change.
"""
from __future__ import annotations

import copy
import os
import re
import threading
import weakref
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Connection, Engine, event, text

_INFO_KEY = "audit_core.job_reads"
# One job reads one Journey: a few hundred distinct questions at most. Past this
# everything held is dropped, so a long transaction cannot grow without bound.
_MAX_HELD = 2000
_ENABLED = os.environ.get("P2_JOB_READS", "on").strip().lower() not in {"off", "0", "false", "no"}

_thread = threading.local()
_installed: weakref.WeakSet[Engine] = weakref.WeakSet()
_install_lock = threading.Lock()
# Counted per thread and read by tests and the figures check; never by a decision.
_counts = threading.local()

_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_]\w*)?\$")


def _code(statement: str) -> str | None:
    """The statement without its comments and with every quoted value emptied, so
    a word inside a comment or a string is never taken for SQL. None when a string
    or a comment never ends: such a statement is not understood, and what is not
    understood forgets everything."""
    out: list[str] = []
    i, n = 0, len(statement)
    while i < n:
        ch = statement[i]
        two = statement[i:i + 2]
        if two == "--":
            end = statement.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
        elif two == "/*":
            end = statement.find("*/", i + 2)
            if end < 0:
                return None
            i = end + 2
            out.append(" ")
        elif ch == "'":
            # E'...' strings escape with a backslash; all strings double the quote.
            escapes = i > 0 and statement[i - 1] in "eE" and not (i > 1 and (statement[i - 2].isalnum() or statement[i - 2] == "_"))
            i += 1
            while i < n:
                if escapes and statement[i] == "\\":
                    i += 2
                elif statement[i] != "'":
                    i += 1
                elif statement[i + 1:i + 2] == "'":
                    i += 2
                else:
                    break
            if i >= n:
                return None
            i += 1
            out.append("''")
        elif ch == '"':
            end = statement.find('"', i + 1)
            if end < 0:
                return None
            out.append(statement[i:end + 1])
            i = end + 1
        elif ch == "$" and (tag := _DOLLAR_TAG.match(statement, i)) is not None:
            end = statement.find(tag.group(0), tag.end())
            if end < 0:
                return None
            i = end + len(tag.group(0))
            out.append("''")
        else:
            out.append(ch)
            i += 1
    return "".join(out)
_LOCKING = re.compile(r"\bFOR\s+(?:NO\s+KEY\s+UPDATE|UPDATE|KEY\s+SHARE|SHARE)\b", re.IGNORECASE)
_CHANGES = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|TRUNCATE|CALL|COPY|DO|CREATE|ALTER|DROP|GRANT|REVOKE|REFRESH|LOCK|"
    r"NOTIFY|LISTEN|UNLISTEN|VACUUM|ANALYZE|SET|RESET|EXECUTE|PREPARE|INTO)\b"
    r"|\bauditcore\s*\.\s*\w+\s*\("
    r"|\b(?:set_config|nextval|setval|pg_notify|pg_advisory\w*|pg_try_advisory\w*|lo_\w+|dblink\w*)\s*\("
    # Not the same answer twice: a read that calls one of these is never held.
    r"|\b(?:clock_timestamp|statement_timestamp|timeofday|random|gen_random_uuid|uuid_generate_v\w+|txid_current)\s*\(",
    re.IGNORECASE,
)
# Tables a job writes all the way through whose rows are nobody else's business:
# writing one changes its own rows and nothing more. Checked against the schema by
# tests/test_job_reads.py::test_tables_written_alone_touch_only_their_own_rows.
OWN_ROWS_ONLY = frozenset({
    "p2_control_state", "p2_control_units", "rule_executions", "p2_stage_gate_state", "p2_task_events",
    "commercial_lines", "commercial_line_source_values", "discount_applications", "payment_bank_matches",
    "invoice_review_values", "dealer_receipt_review_values", "finding_evidence", "p2_work_queue",
})
_NAMES = re.compile(r"\bauditcore\s*\.\s*\"?(\w+)\"?", re.IGNORECASE)
_PLAIN_WRITE = re.compile(
    r"\s*(?:INSERT\s+INTO|UPDATE(?:\s+ONLY)?|DELETE\s+FROM(?:\s+ONLY)?)\s+auditcore\s*\.\s*\"?(\w+)\"?", re.IGNORECASE)
# "ON CONFLICT ... DO UPDATE" and a locking subquery are part of the one write.
_PART_OF_THE_WRITE = re.compile(r"\b(?:DO|FOR(?:\s+NO\s+KEY)?)\s+UPDATE\b", re.IGNORECASE)
_SECOND_CHANGE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|TRUNCATE|CALL|COPY|EXECUTE)\b"
    r"|\bauditcore\s*\.\s*\w+\s*\("
    r"|\b(?:set_config|nextval|setval|pg_notify|pg_advisory\w*|pg_try_advisory\w*|lo_\w+|dblink\w*)\s*\(",
    re.IGNORECASE,
)
_READ_START = re.compile(r"\s*(?:SELECT|WITH)\b", re.IGNORECASE)
# Savepoints are opened and released around steps that may fail: neither changes a row.
_NEUTRAL = re.compile(r"\s*(?:SAVEPOINT|RELEASE(?:\s+SAVEPOINT)?)\b", re.IGNORECASE)


def is_plain_read(statement: str) -> bool:
    """A SELECT (or WITH ... SELECT) that takes no lock, changes no row and calls
    nothing that could: the only kind of statement that may be held."""
    sql = _code(statement)
    if sql is None or not _READ_START.match(sql):
        return False
    if _LOCKING.search(sql):
        return False
    return _CHANGES.search(sql) is None


def leaves_rows_alone(statement: str) -> bool:
    """What may run without forgetting what the job holds: a plain read, a read
    that locks the rows it reads, a savepoint opened or released."""
    sql = _code(statement)
    if sql is None:
        return False
    if _NEUTRAL.match(sql):
        return True
    if not _READ_START.match(sql):
        return False
    return _CHANGES.search(_LOCKING.sub(" ", sql)) is None


def only_table_written(statement: str) -> str | None:
    """The one table of ``OWN_ROWS_ONLY`` a plain INSERT / UPDATE / DELETE writes,
    or None when the statement is anything else (then everything is forgotten)."""
    sql = _code(statement)
    target = _PLAIN_WRITE.match(sql) if sql is not None else None
    if target is None:
        return None
    table = target.group(1).lower()
    if table not in OWN_ROWS_ONLY:
        return None
    rest = _PART_OF_THE_WRITE.sub(" ", sql[target.end():])
    return None if _SECOND_CHANGE.search(rest) else table


def tables_read(statement: str) -> frozenset[str]:
    """Every table of the schema a read names."""
    return frozenset(name.lower() for name in _NAMES.findall(_code(statement) or ""))


def _forget(connection: Connection) -> None:
    held = connection.info.get(_INFO_KEY)
    if held:
        held.clear()


def _before_execute(connection, cursor, statement, parameters, context, executemany) -> None:
    held = connection.info.get(_INFO_KEY)
    if not held or leaves_rows_alone(statement):
        return
    table = only_table_written(statement)
    if table is None:
        held.clear()
        return
    for key in [key for key, (tables, _rows) in held.items() if table in tables]:
        del held[key]


def _on_transaction_edge(connection, *_: Any) -> None:
    _forget(connection)


def install(engine: Engine) -> None:
    """Make ``engine`` forget what a job holds whenever its transaction writes,
    begins, ends or rolls back to a savepoint. Without this nothing is ever held
    on the engine's connections. Safe to call more than once."""
    with _install_lock:
        if engine in _installed:
            return
        event.listen(engine, "before_cursor_execute", _before_execute)
        for name in ("begin", "commit", "rollback", "rollback_savepoint"):
            event.listen(engine, name, _on_transaction_edge)
        _installed.add(engine)


@contextmanager
def job() -> Iterator[None]:
    """The calling thread is handling one job until this exits."""
    _thread.depth = getattr(_thread, "depth", 0) + 1
    try:
        yield
    finally:
        _thread.depth -= 1


def active(connection: Connection) -> bool:
    """Whether a read on this connection is held right now."""
    return bool(_ENABLED and getattr(_thread, "depth", 0) > 0 and connection.engine in _installed)


def counts() -> dict[str, int]:
    """This thread's reads through this module since ``reset_counts``."""
    return {"asked": getattr(_counts, "asked", 0), "answered_from_job": getattr(_counts, "held", 0)}


def reset_counts() -> None:
    _counts.asked = 0
    _counts.held = 0


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(sorted((str(k), _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple, set, frozenset)):
        frozen = [_freeze(v) for v in value]
        return tuple(sorted(frozen, key=repr)) if isinstance(value, (set, frozenset)) else tuple(frozen)
    return value


def _own(row: Mapping[str, Any]) -> dict[str, Any]:
    """The caller's own copy: a JSON value changed by one caller is not changed
    for the next."""
    return {key: copy.deepcopy(value) if isinstance(value, (dict, list)) else value for key, value in row.items()}


def rows(connection: Connection, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every row of a read, as plain dicts keyed by column name."""
    params = dict(params or {})
    _counts.asked = getattr(_counts, "asked", 0) + 1
    if not active(connection) or not is_plain_read(sql):
        return [_own(row) for row in connection.execute(text(sql), params).mappings().all()]
    try:
        key = (sql, _freeze(params))
        hash(key)
    except TypeError:
        return [_own(row) for row in connection.execute(text(sql), params).mappings().all()]
    tables = tables_read(sql)
    if not tables:  # nothing a write could be matched against: never held
        return [_own(row) for row in connection.execute(text(sql), params).mappings().all()]
    held = connection.info.setdefault(_INFO_KEY, {})
    entry = held.get(key)
    if entry is None:
        found = tuple(dict(row) for row in connection.execute(text(sql), params).mappings().all())
        if len(held) >= _MAX_HELD:
            held.clear()
        held[key] = (tables, found)
    else:
        found = entry[1]
        _counts.held = getattr(_counts, "held", 0) + 1
    return [_own(row) for row in found]


def one(connection: Connection, sql: str, params: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """The single row of a read, or None when there is none."""
    found = rows(connection, sql, params)
    if len(found) > 1:
        raise RuntimeError("job_reads.one: the read returned more than one row")
    return found[0] if found else None


def scalar(connection: Connection, sql: str, params: Mapping[str, Any] | None = None) -> Any:
    """The first column of the single row of a read (None when there is no row)."""
    row = one(connection, sql, params)
    return None if row is None else next(iter(row.values()))
