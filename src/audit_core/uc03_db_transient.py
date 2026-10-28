"""A deadlock is retried, not skipped (2026-10-28).

The Deal reconciliation, the payment match, the model resolution, the customer
name check and the delivery materialisation each catch any error, log
"... failed" and carry on, so one failing never stops the rest of a work item.
A deadlock or a serialisation failure is not a fault in the step: the same
work run a moment later succeeds. Swallowed, it left that step's result out
until something else triggered the Journey again (the Deal or variance stayed
stale), while the work item reported success.

Inside a P2 worker item (``work_item_scope``) such an error is raised again,
so the item fails and the queue retries it on its ladder. Elsewhere -- an API
request rebuilding a page on read -- the step keeps its old behaviour.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# deadlock_detected, serialization_failure, lock_not_available
TRANSIENT_SQLSTATES = frozenset({"40P01", "40001", "55P03"})

_in_work_item: ContextVar[bool] = ContextVar("p2_in_work_item", default=False)


def is_transient_db_error(exc: BaseException | None) -> bool:
    """Whether ``exc`` (or anything it wraps) is a deadlock, serialisation
    failure or lock-not-available error."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        code = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
        if code in TRANSIENT_SQLSTATES:
            return True
        exc = getattr(exc, "orig", None) or exc.__cause__ or exc.__context__
    return False


@contextmanager
def work_item_scope() -> Iterator[None]:
    token = _in_work_item.set(True)
    try:
        yield
    finally:
        _in_work_item.reset(token)


def raise_if_worker_transient(exc: BaseException) -> None:
    """Raise ``exc`` again when it is transient and a worker item is running."""
    if _in_work_item.get() and is_transient_db_error(exc):
        raise exc


__all__ = ["TRANSIENT_SQLSTATES", "is_transient_db_error", "raise_if_worker_transient", "work_item_scope"]
