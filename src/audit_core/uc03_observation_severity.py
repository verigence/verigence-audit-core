"""Each journey's observation severity (product owner 2026-11-18).

"if NDC doesnt show fast tag and invoice has, then add to deal and audit
summary and make remarks in system that NDC doesnt has fasttag and mark this
as transaction with observation level as high. we need to do this for all
journey. lets add column observation severity high low medium, and lets give
entire row a color, and add filter in search... severity can be changed by
Reviewer, TL, PM".

A journey's severity is HIGH, MEDIUM, LOW or none. The system sets it from
the Deal each time the Deal is worked out for the list (refresh_price_variance,
so every journey whose facts move, and DISCOUNT_VARIANCE_RECOMPUTE for all):
today one reason, a FASTag an invoice charges that the NDC does not (HIGH).
A Reviewer, Team Lead or Project Manager may set it over the system's, with a
reason; their setting stands until one of them changes or clears it, and the
system's own keeps being worked out beside it. Stored in
p2_observation_severity (migration 0239); read by the journey list (column,
row colour, filter) and Journey 360.
"""
from __future__ import annotations

import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)

SEVERITIES = ("HIGH", "MEDIUM", "LOW")
CHANGE_ROLES = ("REVIEWER", "TL", "PM")
READ_ROLES = ("PC", "REVIEWER", "TL", "PM", "EXECUTIVE")
MIN_REASON = 5
_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-observation-severity"])


# ── what the system sets ─────────────────────────────────────────────────────
def system_findings(view: dict[str, Any]) -> list[dict[str, Any]]:
    """The reasons the Deal gives for a severity, each {code, severity, text}."""
    found: list[dict[str, Any]] = []
    fastag = view.get("fastagNotOnNdc")
    if fastag:
        found.append({"code": "FASTAG_NOT_ON_NDC", "severity": "HIGH",
                      "text": f"FASTag Rs {fastag['amount']} charged by the {fastag['source']}, not on the NDC"})
    return found


def record_system(connection: Connection, *, tenant_id: str, journey_id: UUID, view: dict[str, Any]) -> str | None:
    """Store the severity the system sets from the Deal ``view``; returns it."""
    found = system_findings(view)
    severity = max((f["severity"] for f in found), key=_RANK.__getitem__, default=None)
    if severity is None:
        # Nothing to say: clear a severity the system set before, never add a row.
        connection.execute(
            text("UPDATE auditcore.p2_observation_severity SET system_severity = NULL, "
                 "system_reasons = '[]'::jsonb, system_at_utc = now() "
                 "WHERE tenant_id = :t AND journey_id = :j AND system_severity IS NOT NULL"),
            {"t": tenant_id, "j": journey_id},
        )
        return None
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_observation_severity
              (tenant_id, journey_id, system_severity, system_reasons, system_at_utc)
            VALUES (:t, :j, :s, CAST(:r AS jsonb), now())
            ON CONFLICT (tenant_id, journey_id) DO UPDATE
              SET system_severity = EXCLUDED.system_severity, system_reasons = EXCLUDED.system_reasons,
                  system_at_utc = now()
              WHERE p2_observation_severity.system_severity IS DISTINCT FROM EXCLUDED.system_severity
                 OR p2_observation_severity.system_reasons IS DISTINCT FROM EXCLUDED.system_reasons
            """
        ),
        {"t": tenant_id, "j": journey_id, "s": severity, "r": json.dumps(found)},
    )
    return severity


def journey_severity(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT * FROM auditcore.p2_observation_severity WHERE tenant_id = :t AND journey_id = :j"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one_or_none()
    if row is None:
        return {"severity": None, "system": None, "reasons": [], "override": None}
    override = None if row["override_severity"] is None else {
        "severity": row["override_severity"], "reason": row["override_reason"],
        "by": row["override_by_name"] or row["override_by"], "role": row["override_role"],
        "at": row["override_at_utc"].isoformat() if row["override_at_utc"] else None}
    return {"severity": row["override_severity"] or row["system_severity"], "system": row["system_severity"],
            "reasons": list(row["system_reasons"] or []), "override": override}


def set_override(connection: Connection, *, tenant_id: str, journey_id: UUID, severity: str | None, reason: str,
                 actor_id: str, actor_name: str | None, role: str | None) -> dict[str, Any]:
    """A Reviewer / TL / PM sets the severity (``None`` clears theirs, back to the system's)."""
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_observation_severity
              (tenant_id, journey_id, override_severity, override_reason, override_by, override_by_name,
               override_role, override_at_utc)
            VALUES (:t, :j, :s, :r, :a, :n, :role, now())
            ON CONFLICT (tenant_id, journey_id) DO UPDATE
              SET override_severity = EXCLUDED.override_severity, override_reason = EXCLUDED.override_reason,
                  override_by = EXCLUDED.override_by, override_by_name = EXCLUDED.override_by_name,
                  override_role = EXCLUDED.override_role, override_at_utc = now()
            """
        ),
        {"t": tenant_id, "j": journey_id, "s": severity, "r": reason, "a": actor_id, "n": actor_name, "role": role},
    )
    return journey_severity(connection, tenant_id=tenant_id, journey_id=journey_id)


def severity_filter(value: str | None) -> str | None:
    """The list filter: HIGH / MEDIUM / LOW, or NONE (no severity); else None (all)."""
    text_ = (value or "").strip().upper()
    return text_ if text_ in (*SEVERITIES, "NONE") else None


# ── the API ───────────────────────────────────────────────────────────────────
def _who(connection: Connection, tenant_id: str, principal: HumanPrincipal, client: SecurityAuthorizationClient,
         roles: tuple[str, ...], journey_id: UUID) -> tuple[set[str], str | None]:
    from audit_core.uc03_p2_dms_dumps import _READ, _access, _dealers, _name_of

    held = _access(connection, tenant_id, principal, client, _READ, roles)
    dealer = connection.execute(
        text("SELECT dealer_id FROM auditcore.journeys WHERE tenant_id = :t AND journey_id = :j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one_or_none()
    if dealer is None:
        raise HTTPException(status_code=404, detail="Journey not found.")
    covered = {d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)}
    if str(dealer) not in covered:
        raise HTTPException(status_code=403, detail="That journey is not in a dealership you cover.")
    return held, _name_of(connection, principal.subject)


@router.get("/journeys/{journey_id}/observation-severity")
def get_severity(
    tenant_id: str, journey_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    from audit_core.uc03_p2_dms_dumps import _roles

    held, _ = _who(connection, tenant_id, principal, client, READ_ROLES, journey_id)
    can_change = "ADMIN" in held or bool(_roles(connection, tenant_id, principal.subject) & set(CHANGE_ROLES))
    return {**journey_severity(connection, tenant_id=tenant_id, journey_id=journey_id), "canChange": can_change}


@router.put("/journeys/{journey_id}/observation-severity")
def put_severity(
    tenant_id: str, journey_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Set the journey's severity (``severity``: HIGH / MEDIUM / LOW, or null to go back
    to the system's) with a ``reason``. Reviewer, Team Lead, Project Manager."""
    held, name = _who(connection, tenant_id, principal, client, CHANGE_ROLES, journey_id)
    raw = (body or {}).get("severity")
    severity = None if raw in (None, "", "SYSTEM") else str(raw).upper()
    if severity is not None and severity not in SEVERITIES:
        raise HTTPException(status_code=422, detail="Severity is High, Medium or Low.")
    reason = str((body or {}).get("reason") or "").strip()
    if len(reason) < MIN_REASON:
        raise HTTPException(status_code=422, detail="Say why (a reason).")
    role = "ADMIN" if "ADMIN" in held else next((r for r in CHANGE_ROLES if r in held), None)
    return set_override(connection, tenant_id=tenant_id, journey_id=journey_id, severity=severity, reason=reason,
                        actor_id=principal.subject, actor_name=name, role=role)


__all__ = ["CHANGE_ROLES", "SEVERITIES", "journey_severity", "record_system", "router", "set_override",
           "severity_filter", "system_findings"]
