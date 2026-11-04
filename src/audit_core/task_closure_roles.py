"""Who may close a task besides the role it is assigned to (product owner, 2026-11-02;
the PC's tasks added 2026-11-04: "reviewer to be able to close the PC tasks").

"The tasks marked for TL closure should now also be marked for Reviewers for the
given outlets." One task, not a copy: a task assigned to the Team Lead role is on
the Team Lead's queue as before and also on the queue of every Reviewer whose
assignment covers the task's outlet, and whoever closes it closes it for both.

This module is the one place that rule lives. Journey tasks (uc03_p2_tasks,
uc03_sku_tasks, the Task Queue list) and Task Manager tasks (task_manager) all ask
it, so the rule cannot drift between them. The outlet check is not here: each
caller already narrows to the outlets the person is assigned to (resolve_p2_scope
for a Journey, _caller_outlets for a Task Manager task), and a Reviewer is held to
the same check.
"""
from __future__ import annotations

from typing import Any

# Assigned role -> the other roles that may close the same task.
CO_CLOSERS: dict[str, frozenset[str]] = {
    "TL": frozenset({"REVIEWER"}),
    # A Reviewer closes the PC's tasks of the outlets they cover (2026-11-04).
    "PC": frozenset({"REVIEWER"}),
}

# Roles that hold the Team Lead's authority on any task (accepting an exception,
# approving another month's price list, enabling a Management Referral).
_TEAM_LEAD_AUTHORITY = frozenset({"TL", "PM"})


def _role(value: Any) -> str:
    return str(value or "").strip().upper()


def can_close_as(assigned_role: Any, actor_role: Any) -> bool:
    """True when ``actor_role`` may work and close a task assigned to ``assigned_role``."""
    assigned, actor = _role(assigned_role), _role(actor_role)
    if not assigned or not actor:
        return False
    return assigned == actor or actor in CO_CLOSERS.get(assigned, frozenset())


def roles_closable_by(actor_role: Any) -> list[str]:
    """Every assigned role whose tasks ``actor_role`` sees and closes (its own first)."""
    actor = _role(actor_role)
    if not actor:
        return []
    return [actor] + sorted(role for role, closers in CO_CLOSERS.items() if actor in closers and role != actor)


def has_team_lead_authority(actor_role: Any, assigned_role: Any) -> bool:
    """A Team Lead or Project Manager; or a Reviewer working a task assigned to the
    Team Lead, which they close in the Team Lead's place."""
    actor = _role(actor_role)
    return actor in _TEAM_LEAD_AUTHORITY or (_role(assigned_role) == "TL" and can_close_as("TL", actor))
