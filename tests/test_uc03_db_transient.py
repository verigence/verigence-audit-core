"""A deadlock inside a worker item is retried, not skipped (2026-10-28)."""
from __future__ import annotations

import pytest

from audit_core.uc03_db_transient import (
    is_transient_db_error,
    raise_if_worker_transient,
    work_item_scope,
)


class _PgError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _Wrapped(Exception):
    def __init__(self, orig: Exception) -> None:
        super().__init__(str(orig))
        self.orig = orig


def test_deadlock_serialisation_and_lock_errors_are_transient():
    for code in ("40P01", "40001", "55P03"):
        assert is_transient_db_error(_Wrapped(_PgError(code)))
    assert not is_transient_db_error(_Wrapped(_PgError("23505")))
    assert not is_transient_db_error(ValueError("x"))


def test_only_a_worker_item_raises_it_again():
    deadlock = _Wrapped(_PgError("40P01"))
    raise_if_worker_transient(deadlock)  # an API request keeps going
    with work_item_scope(), pytest.raises(_Wrapped):
        raise_if_worker_transient(deadlock)
    with work_item_scope():
        raise_if_worker_transient(_Wrapped(_PgError("23505")))  # a real fault is logged as before
