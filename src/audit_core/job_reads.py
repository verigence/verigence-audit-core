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

* any statement that is not a plain read (INSERT, UPDATE, DELETE, a function of
  the ``auditcore`` schema, ``set_config`` -- it moves the row-level-security
  context -- or anything this module does not recognise) forgets everything held
  on that connection, before it runs;
* so do the beginning and the end of a transaction and a rollback to a savepoint.

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

_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_LOCKING = re.compile(r"\bFOR\s+(?:NO\s+KEY\s+UPDATE|UPDATE|KEY\s+SHARE|SHARE)\b", re.IGNORECASE)
_CHANGES = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|TRUNCATE|CALL|COPY|DO|CREATE|ALTER|DROP|GRANT|REVOKE|REFRESH|LOCK|"
    r"NOTIFY|LISTEN|UNLISTEN|VACUUM|ANALYZE|SET|RESET|EXECUTE|PREPARE|INTO)\b"
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
    sql = _COMMENTS.sub(" ", statement)
    if not _READ_START.match(sql):
        return False
    if _LOCKING.search(sql):
        return False
    return _CHANGES.search(sql) is None


def leaves_rows_alone(statement: str) -> bool:
    """What may run without forgetting what the job holds: a plain read, a read
    that locks the rows it reads, a savepoint opened or released."""
    sql = _COMMENTS.sub(" ", statement)
    if _NEUTRAL.match(sql):
        return True
    if not _READ_START.match(sql):
        return False
    return _CHANGES.search(_LOCKING.sub(" ", sql)) is None


def _forget(connection: Connection) -> None:
    held = connection.info.get(_INFO_KEY)
    if held:
        held.clear()


def _before_execute(connection, cursor, statement, parameters, context, executemany) -> None:
    held = connection.info.get(_INFO_KEY)
    if held and not leaves_rows_alone(statement):
        held.clear()


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
    held = connection.info.setdefault(_INFO_KEY, {})
    found = held.get(key)
    if found is None:
        found = tuple(dict(row) for row in connection.execute(text(sql), params).mappings().all())
        if len(held) >= _MAX_HELD:
            held.clear()
        held[key] = found
    else:
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
