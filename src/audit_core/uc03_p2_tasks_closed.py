"""Tasks closed: one rule for every report (product owner 2026-11-23: the Super Admin reports
and the Project Tracker "don't tally").

A task counts as closed by a person on the day they acted on it with a closing event
(completed the action, closed it, accepted it, accepted an exception, confirmed or dismissed a
breach, approved or rejected a correction, gave a verdict) -- Journey tasks and Task Manager
tasks alike, each task once a day per person, never the system's own events. The Project
Tracker's "Tasks closed" and the engagement report's "Tasks closed" both read this.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

_SQL = """
    SELECT te.actor_id AS actor, (te.created_at_utc AT TIME ZONE :tz)::date AS day,
           COALESCE(tk.outlet_id, j.outlet_id) AS outlet, count(DISTINCT te.task_id) AS n
    FROM auditcore.p2_task_events te
    JOIN auditcore.p2_tasks tk ON tk.tenant_id = te.tenant_id AND tk.task_id = te.task_id
    LEFT JOIN auditcore.journeys j ON j.tenant_id = tk.tenant_id AND j.journey_id = tk.journey_id
    WHERE te.tenant_id = :t AND te.created_at_utc >= :s AND te.created_at_utc < :e
      AND te.actor_id IS NOT NULL AND upper(te.actor_id) <> 'SYSTEM' AND te.event_type = ANY(:closing)
    GROUP BY 1, 2, 3
"""


def tasks_closed_rows(connection: Connection, *, tenant_id: str, start: datetime, end: datetime,
                      tz: str) -> list[dict[str, Any]]:
    """{actor, day, outlet, n}: the tasks each person closed each day (in ``tz``) at each outlet,
    in [start, end)."""
    from audit_core.uc03_p2_project_tracker import _CLOSING_EVENTS

    return [dict(r) for r in connection.execute(
        text(_SQL), {"t": tenant_id, "s": start, "e": end, "tz": tz, "closing": sorted(_CLOSING_EVENTS)},
    ).mappings()]
