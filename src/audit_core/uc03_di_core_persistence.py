from __future__ import annotations

from typing import Any

from sqlalchemy import Connection

from audit_core import uc03_booking_review_decisions as review_decisions
from audit_core.db import set_security_actor_context

_installed = False
_original_review_scope = review_decisions._scope


def _scope_with_actor_context(
    connection: Connection,
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Preserve the authenticated Review actor in transaction-local DB context."""

    context = _original_review_scope(connection, *args, **kwargs)
    human_principal = kwargs.get("human_principal")
    if human_principal is None:
        raise RuntimeError("UC03 Review scope requires an authenticated human principal")
    set_security_actor_context(connection, human_principal.subject)
    return context


def install_uc03_di_core_persistence() -> None:
    """Install only Review actor context; persistence is explicit in Review Confirm."""

    global _installed
    if _installed:
        return
    review_decisions._scope = _scope_with_actor_context
    _installed = True
