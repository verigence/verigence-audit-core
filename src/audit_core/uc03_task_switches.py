"""uc03_task_switches.py -- switch a kind of task off, or on again, for one Project.

Product owner 2026-11-19: "is there a way i can see the list of the tasks being produced
and tick them off, because some of the tasks are not relevant currently, but it may be
required in future, is something of this sort can be done at the project level and it
should be under project administration only". Asked how: the check stops for that
Project, "and another is that it shouldnt be visible for the existing journey as well".

A kind of task is its source code: the check (control) or the producer that raises it --
DEAL_UNDERCHARGED, MANUAL_VERIFICATION, DOCUMENT_MISSING, ... One row per Project and code
in ``auditcore.p2_task_switches``; no row means on.

Switched off:

* the check does not run for the Project: the deal checks skip it
  (``uc03_p2_audit_rules.run_p2_audit_rules``), and whatever check state is written for it
  is "not applicable -- switched off" with no finding (``uc03_p2_controls.write_control_state``);
* no task of that kind is raised (``uc03_p2_task_producer.raise_or_refresh`` and
  ``apply_control_transitions``);
* on the existing journeys, at once (``set_switch``): its open tasks are closed
  (CANCELLED, "Switched off for this Project by <name> on <date>: <reason>"), its open
  findings voided and its check state set to not applicable. Nothing is deleted; the
  Journey 360 check list leaves it out.

Switched on again: the tasks it closed may be raised again, and every booking's checks
are run again (a RULES_RERUN job and a stage recompute per booking), so what still fails
comes back with today's figures.

Only Project Administration (Super Admin) lists or changes the switches.
Tasks a person raises (manual tasks, a Reviewer's closure, the Team Lead's review) are not
in the list: they are not produced by a check.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import Connection, text

from audit_core import job_reads
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)

SWITCHED_OFF = "SWITCHED_OFF"
# Project Administration is the Super Admin's (verigence-web, ProjectAdministrationPage, 19 Nov:
# "all project specific tasks should be under project administration").
ADMIN_ROLES = ("SUPER_ADMIN",)
MIN_REASON = 5
# The tasks a check or a producer raises (p2_tasks.source_type); not the ones a person
# raises or the review steps (REVIEW, HUMAN_ACTION, SCHEDULE, GATE_PASS, TASK_MANAGER).
SYSTEM_SOURCE_TYPES = ("RULE", "RESOLVER", "DOCUMENT", "DOCUMENT_FIELD", "EVIDENCE")
# Off unless a Project switches them on: the product owner switched them off before this
# list existed ("not relevant as of now, may we introduce it later").
DEFAULT_OFF = {
    "ACCESSORIES_FITTED_UNCONFIRMED": "Not relevant for now; may be introduced later (product owner, 12 Nov 2026).",
    "DELIVERY_NOT_COMPLETED_IN_TIME": "Not relevant for now; may be introduced later (product owner, 19 Nov 2026).",
}
_OPEN = ("READY", "IN_PROGRESS", "RETURNED", "ACTION_COMPLETED", "VERIFYING", "AWAITING_REQUESTER_REVIEW")
_CATEGORY = {"RULE": "Checks", "RESOLVER": "Model and price", "DOCUMENT": "Documents",
             "DOCUMENT_FIELD": "Document readings", "EVIDENCE": "Documents"}
_OWNER = {"TL": "Team Lead", "PC": "PC", "PM": "Project Manager", "REVIEWER": "Reviewer"}


# --------------------------------------------------------------------------- reading

def _rows(connection: Connection, tenant_id: str) -> dict[str, dict[str, Any]]:
    return {str(r["source_code"]): dict(r) for r in connection.execute(
        text("SELECT source_code, enabled, reason, changed_by_name, changed_at_utc "
             "FROM auditcore.p2_task_switches WHERE tenant_id=:t"),
        {"t": tenant_id},
    ).mappings().all()}


def _is_on(code: str, row: dict[str, Any] | None) -> bool:
    return bool(row["enabled"]) if row is not None else code not in DEFAULT_OFF


def switched_off(connection: Connection, tenant_id: str) -> frozenset[str]:
    """The codes switched off for the Project."""
    rows = _rows(connection, tenant_id)
    return frozenset(c for c in set(rows) | set(DEFAULT_OFF) if not _is_on(c, rows.get(c)))


def _when(at: Any) -> str:
    if not isinstance(at, datetime):
        return ""
    from audit_core.uc03_p2_object_keys import IST

    return at.astimezone(IST).strftime("%d %b %Y")


def _note(row: dict[str, Any]) -> str:
    who = row.get("changed_by_name") or "Project Administration"
    reason = str(row.get("reason") or "").strip()
    return f"Switched off for this Project by {who} on {_when(row.get('changed_at_utc'))}" + (
        f": {reason}" if reason else "")


def off_result(connection: Connection, tenant_id: str, code: str | None) -> dict[str, Any] | None:
    """The completion result of a task closed because its kind is switched off, or None
    while the kind is on."""
    if not code:
        return None
    if job_reads.active(connection):
        # Asked for every check of every worker job: inside a job the Project's
        # switches are read once, all of them, and looked up here (audit_core.job_reads).
        row = next((dict(r) for r in job_reads.rows(
            connection,
            "SELECT source_code, enabled, reason, changed_by_name, changed_at_utc "
            "FROM auditcore.p2_task_switches WHERE tenant_id=:t",
            {"t": tenant_id},
        ) if r["source_code"] == code), None)
        if row is not None:
            del row["source_code"]
    else:
        row = job_reads.one(
            connection,
            "SELECT enabled, reason, changed_by_name, changed_at_utc FROM auditcore.p2_task_switches "
            "WHERE tenant_id=:t AND source_code=:c",
            {"t": tenant_id, "c": code},
        )
    if _is_on(code, row):
        return None
    if row is None:
        return {"outcome": SWITCHED_OFF, "reason": f"Switched off: {DEFAULT_OFF[code]}", "sourceCode": code}
    return {"outcome": SWITCHED_OFF, "reason": _note(row), "sourceCode": code}


def closed_by_switch(completion_result: Any) -> bool:
    """A task closed because its kind was switched off: raised again once it is back on."""
    return isinstance(completion_result, dict) and completion_result.get("outcome") == SWITCHED_OFF


def void_open_findings(connection: Connection, *, tenant_id: str, code: str, note: str,
                       journey_id: UUID | None = None) -> int:
    """Void the open findings of a check that is switched off (all journeys, or one)."""
    rows = connection.execute(
        text(
            """
            UPDATE auditcore.audit_findings
            SET finding_status='VOIDED', resolution_reason=:note, resolved_at_utc=now(), updated_at_utc=now()
            WHERE tenant_id=:t AND rule_key=:c AND finding_status IN ('OPEN','ACKNOWLEDGED')
              AND (CAST(:j AS uuid) IS NULL OR journey_id=CAST(:j AS uuid))
            RETURNING audit_finding_id, journey_id, stage_code
            """
        ),
        {"t": tenant_id, "c": code, "note": note[:1000], "j": str(journey_id) if journey_id else None},
    ).mappings().all()
    for row in rows:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_finding_events (
                    tenant_id, audit_finding_id, journey_id, stage_code, event_type, actor_id,
                    actor_role_snapshot, safe_payload, correlation_id
                ) VALUES (:t, :f, :j, :stage, 'VOIDED', NULL, 'SYSTEM', CAST(:payload AS jsonb), NULL)
                """
            ),
            {"t": tenant_id, "f": row["audit_finding_id"], "j": row["journey_id"], "stage": row["stage_code"],
             "payload": json.dumps({"reason": SWITCHED_OFF, "note": note})},
        )
    return len(rows)


# --------------------------------------------------------------------------- the list

def catalog(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    """Every kind of task the Project's checks and producers raise: the checks of the
    registry that raise a task, and every code that raised a task on the Project; with
    its name, who it goes to, how many are open and raised in the last 30 days, and its
    switch."""
    from audit_core.uc03_p2_journey360 import control_labels
    from audit_core.uc03_p2_registry import get_registry

    registry = get_registry()
    labels = control_labels(connection)
    seen = {
        str(r["source_code"]): dict(r) for r in connection.execute(
            text(
                """
                SELECT source_code,
                       mode() WITHIN GROUP (ORDER BY source_type) AS source_type,
                       mode() WITHIN GROUP (ORDER BY assigned_role_code) AS owner,
                       count(*) FILTER (WHERE task_status = ANY(:open)) AS open,
                       count(*) FILTER (WHERE created_at_utc > now() - interval '30 days') AS recent,
                       (array_agg(title ORDER BY created_at_utc DESC))[1] AS example,
                       max(created_at_utc) AS last_raised
                FROM auditcore.p2_tasks
                WHERE tenant_id=:t AND origin_kind='SYSTEM' AND source_type = ANY(:types)
                  AND source_code IS NOT NULL
                GROUP BY source_code
                """
            ),
            {"t": tenant_id, "open": list(_OPEN), "types": list(SYSTEM_SOURCE_TYPES)},
        ).mappings().all()
    }
    switches = _rows(connection, tenant_id)
    codes = set(seen) | {c.code for c in registry.controls.values() if c.task_type} | set(switches) | set(DEFAULT_OFF)
    out = []
    for code in sorted(codes):
        control = registry.controls.get(code)
        used = seen.get(code) or {}
        switch = switches.get(code)
        source = str(used.get("source_type") or ("RULE" if control else ""))
        owner = str((control.task_owner if control else None) or used.get("owner") or "")
        out.append({
            "code": code,
            "name": labels.get(code) or code.replace("_", " ").capitalize(),
            "example": used.get("example"),
            "category": (control.category if control and control.category else _CATEGORY.get(source, "Other")),
            "owner": _OWNER.get(owner, owner or None),
            "open": int(used.get("open") or 0),
            "raisedLast30Days": int(used.get("recent") or 0),
            "lastRaisedAtUtc": used["last_raised"].isoformat() if used.get("last_raised") else None,
            "enabled": _is_on(code, switch),
            "reason": (switch or {}).get("reason") or (DEFAULT_OFF.get(code) if switch is None else None),
            "changedByName": (switch or {}).get("changed_by_name"),
            "changedAtUtc": switch["changed_at_utc"].isoformat() if switch and switch.get("changed_at_utc") else None,
        })
    # Switched-off kinds first, then the most open.
    out.sort(key=lambda r: (r["enabled"], -r["open"], -r["raisedLast30Days"], r["name"]))
    return out


# --------------------------------------------------------------------------- changing

def _withdraw(connection: Connection, *, tenant_id: str, code: str, result: dict[str, Any]) -> dict[str, int]:
    """Take a switched-off kind off the existing journeys: its open tasks closed, its open
    findings voided, its check state not applicable."""
    from audit_core.uc03_p2_tasks import record_task_event

    tasks = connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks t
            SET task_status='CANCELLED',
                completion_result=COALESCE(t.completion_result, '{}'::jsonb) || CAST(:result AS jsonb),
                updated_at_utc=now()
            FROM (SELECT task_id, task_status FROM auditcore.p2_tasks
                  WHERE tenant_id=:t AND source_code=:c AND origin_kind='SYSTEM'
                    AND source_type = ANY(:types) AND task_status = ANY(:open)
                  FOR UPDATE) old
            WHERE t.tenant_id=:t AND t.task_id=old.task_id
            RETURNING t.task_id, t.journey_id, old.task_status AS previous
            """
        ),
        {"t": tenant_id, "c": code, "types": list(SYSTEM_SOURCE_TYPES), "open": list(_OPEN),
         "result": json.dumps(result)},
    ).mappings().all()
    for row in tasks:
        record_task_event(connection, tenant_id=tenant_id, journey_id=row["journey_id"], task_id=row["task_id"],
                          event_type="CANCELLED", actor_id="SYSTEM", actor_role_code="SYSTEM",
                          details={**result, "previousStatus": row["previous"]})
    findings = void_open_findings(connection, tenant_id=tenant_id, code=code, note=result["reason"])
    checks = connection.execute(
        text(
            """
            UPDATE auditcore.p2_control_state
            SET control_status='NOT_APPLICABLE', status_reason=:reason, finding_id=NULL, last_error=NULL,
                status_changed_at_utc=now(), updated_at_utc=now()
            WHERE tenant_id=:t AND control_code=:c AND control_status <> 'NOT_APPLICABLE'
            """
        ),
        {"t": tenant_id, "c": code, "reason": result["reason"]},
    ).rowcount
    return {"tasksClosed": len(tasks), "findingsVoided": findings, "checksSet": int(checks or 0)}


def _rerun(connection: Connection, *, tenant_id: str, code: str, actor_name: str | None) -> dict[str, int]:
    """Switched on again: every booking's checks run again (RULES_RERUN, all bookings) and
    every booking's stage is recomputed, so the documents' tasks are raised again too."""
    connection.execute(
        text("INSERT INTO auditcore.p2_worker_jobs (job_kind, tag, reports, requested_by) "
             "VALUES ('RULES_RERUN', :tag, ARRAY[]::text[], :by)"),
        {"tag": f"{tenant_id}:task-switch-on-{code.lower()}-{datetime.now(UTC):%Y%m%d%H%M%S}-commit",
         "by": f"task switch ({actor_name or 'Project Administration'})"},
    )
    queued = connection.execute(
        text("SELECT count(auditcore.p2_request_stage_recompute(j.tenant_id, j.journey_id)) "
             "FROM auditcore.journeys j WHERE j.tenant_id=:t"),
        {"t": tenant_id},
    ).scalar_one()
    return {"bookingsRechecked": int(queued or 0)}


def set_switch(connection: Connection, *, tenant_id: str, code: str, enabled: bool, reason: str,
               actor_id: str, actor_name: str | None) -> dict[str, Any]:
    """Switch a kind of task on or off for the Project, with who, when and why."""
    was_enabled = code not in switched_off(connection, tenant_id)
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_task_switches (tenant_id, source_code, enabled, reason, changed_by,
                                                    changed_by_name, changed_at_utc)
            VALUES (:t, :c, :e, :reason, :by, :name, now())
            ON CONFLICT (tenant_id, source_code) DO UPDATE SET
                enabled=EXCLUDED.enabled, reason=EXCLUDED.reason, changed_by=EXCLUDED.changed_by,
                changed_by_name=EXCLUDED.changed_by_name, changed_at_utc=now()
            """
        ),
        {"t": tenant_id, "c": code, "e": enabled, "reason": reason, "by": actor_id, "name": actor_name},
    )
    connection.execute(
        text("INSERT INTO auditcore.p2_task_switch_events (tenant_id, source_code, enabled, reason, changed_by, "
             "changed_by_name) VALUES (:t, :c, :e, :reason, :by, :name)"),
        {"t": tenant_id, "c": code, "e": enabled, "reason": reason, "by": actor_id, "name": actor_name},
    )
    effect: dict[str, int] = {}
    if not enabled:
        effect = _withdraw(connection, tenant_id=tenant_id, code=code,
                           result=off_result(connection, tenant_id, code) or {})
    elif not was_enabled:
        effect = _rerun(connection, tenant_id=tenant_id, code=code, actor_name=actor_name)
    return {"code": code, "enabled": enabled, **effect}


# --------------------------------------------------------------------------- API

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-task-switches"])


def _admin(connection: Connection, tenant_id: str, principal: HumanPrincipal,
           client: SecurityAuthorizationClient) -> str | None:
    """Project Administration only: the Super Admin. Returns the caller's name."""
    from audit_core.uc03_p2_access import authorize_p2_any
    from audit_core.uc03_p2_dms_dumps import _name_of

    access = authorize_p2_any(connection, tenant_id=tenant_id, journey_id=None, human_principal=principal,
                              authorization_client=client, permission_keys=("audit.journey.read",))
    if (access.functional_role or "").upper() not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="This is for Project Administration only.")
    return _name_of(connection, principal.subject)


@router.get("/task-switches")
def list_switches(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    _admin(connection, tenant_id, principal, client)
    items = catalog(connection, tenant_id)
    return {"items": items, "switchedOff": sum(1 for i in items if not i["enabled"])}


@router.put("/task-switches/{code}")
def put_switch(
    tenant_id: str, code: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Switch a kind of task on or off (``enabled``) with a ``reason``."""
    name = _admin(connection, tenant_id, principal, client)
    code = str(code or "").strip().upper()
    if code not in {i["code"] for i in catalog(connection, tenant_id)}:
        raise HTTPException(status_code=404, detail="No such kind of task on this Project.")
    enabled = (body or {}).get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="Say whether it is on or off (enabled: true / false).")
    reason = str((body or {}).get("reason") or "").strip()
    if len(reason) < MIN_REASON:
        raise HTTPException(status_code=422, detail="Say why (a reason of at least 5 characters).")
    return set_switch(connection, tenant_id=tenant_id, code=code, enabled=enabled, reason=reason[:1000],
                      actor_id=principal.subject, actor_name=name)


__all__ = ["SWITCHED_OFF", "catalog", "closed_by_switch", "off_result", "router", "set_switch", "switched_off",
           "void_open_findings"]
