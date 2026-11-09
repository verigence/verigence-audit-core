"""People who have left, as HR reports them (product owner 2026-11-09).

People who quit, were suspended or were terminated kept showing in dashboards and reports (the
mobile dashboard, utilisation, task status, gate passes, location checks, attendance) until
someone remembered to end their role in Audit Core, and showed as idle, absent or overdue for
the days in between. HR owns employment: it reports everyone who is not active, with the day
they left (the effective date of the approved status change), whenever a status changes and
once a day (PUT /v1/service/hr/employment, hr_employment_api).

Audit Core then:
- keeps the list in ``auditcore.people_employment`` (replaced whole by each report, so a person
  made active again simply drops off it);
- ends any role still open for a leaver once their last day is over, effective the end of that
  day (what an administrator did by hand until now), so every screen that lists the people of a
  Project stops expecting work from them;
- keeps them in reports for the days they were employed and no later ("keep up to exit date"):
  a period that ends before they left still shows them, one that starts after does not.

Someone not active with no exit date on file is treated as gone from the start.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_platform_super_admin_context, set_tenant_context

logger = structlog.get_logger(__name__)

_STATEMENT_TIMEOUT = "15s"

# The Projects whose roles are checked. A module constant so tests can narrow it.
_PROJECTS_SQL = text("SELECT tenant_id FROM auditcore.projects ORDER BY tenant_id")

# The end of a person's last day (IST), as a timestamp.
_LAST_DAY_END = "((pe.exit_date + 1)::timestamp AT TIME ZONE 'Asia/Kolkata')"


# --------------------------------------------------------------------------- reading


def exit_dates(connection: Connection) -> dict[str, date | None]:
    """Everyone HR reports as not active: their last day, or None when HR has no date."""
    return {
        str(actor): exit_date
        for actor, exit_date in connection.execute(
            text("SELECT actor_id, exit_date FROM auditcore.people_employment")
        ).all()
    }


def gone_on(exits: dict[str, date | None], actor: Any, day: date) -> bool:
    """True when the person had left before ``day`` (their last day itself still counts)."""
    if actor is None or str(actor) not in exits:
        return False
    last = exits[str(actor)]
    return last is None or last < day


# --------------------------------------------------------------------------- writing


def store_leavers(engine: Engine, leavers: list[dict[str, Any]]) -> int:
    """Replace the list with HR's report."""
    with engine.begin() as connection:
        connection.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
        set_platform_super_admin_context(connection)
        connection.execute(text("DELETE FROM auditcore.people_employment"))
        for person in leavers:
            connection.execute(text("""
                INSERT INTO auditcore.people_employment
                    (actor_id, employee_code, employment_status, exit_date, reported_at_utc)
                VALUES (:a, :c, :s, :d, now())
                ON CONFLICT (actor_id) DO UPDATE SET employee_code = EXCLUDED.employee_code,
                    employment_status = EXCLUDED.employment_status, exit_date = EXCLUDED.exit_date,
                    reported_at_utc = now()
            """), {"a": person["actorId"], "c": person.get("employeeCode"),
                   "s": person["status"], "d": person.get("exitDate")})
    return len(leavers)


def end_leaver_roles(engine: Engine) -> int:
    """End every role still open for someone whose last day is over, effective the end of that
    day (never before the role began). Roles of people still employed are not touched. Returns
    how many roles were ended."""
    with engine.begin() as connection:
        set_platform_super_admin_context(connection)
        if not connection.execute(text("SELECT EXISTS (SELECT 1 FROM auditcore.people_employment)")).scalar():
            return 0
        tenants = [str(t) for t in connection.execute(_PROJECTS_SQL).scalars().all()]
    ended = 0
    for tenant_id in tenants:
        with engine.begin() as connection:
            connection.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
            set_tenant_context(connection, tenant_id)
            rows = connection.execute(text(f"""
                UPDATE auditcore.business_assignments ba
                SET assignment_status = 'INACTIVE',
                    effective_to = GREATEST(ba.effective_from,
                                            LEAST(now(), coalesce({_LAST_DAY_END}, now()))),
                    updated_at_utc = now()
                FROM auditcore.people_employment pe
                WHERE ba.tenant_id = :t AND ba.security_actor_id = pe.actor_id
                  AND ba.assignment_status = 'ACTIVE'
                  AND (ba.effective_to IS NULL OR ba.effective_to > now())
                  AND (pe.exit_date IS NULL OR {_LAST_DAY_END} <= now())
                RETURNING ba.security_actor_id, ba.business_role_code
            """), {"t": tenant_id}).all()
        for actor, role in rows:
            logger.info("leaver_role_ended", tenant_id=tenant_id, actor_id=str(actor), role=role)
        ended += len(rows)
    return ended


def end_leaver_roles_quietly(engine: Engine) -> int:
    """For the worker: a failure is logged and tried again on the next round."""
    try:
        return end_leaver_roles(engine)
    except Exception:
        logger.warning("leaver_roles_end_failed", exc_info=True)
        return 0
