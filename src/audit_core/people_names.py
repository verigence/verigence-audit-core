"""People's display names from Security (product owner 2026-10-27).

Audit Core keeps no people directory: it showed ids for anyone who had not
used the app yet. Security answers display names by user id to Audit Core
(GET /security/v1/service/user-names, read-only, names only). The P2 worker
copies the names of everyone with a business assignment into
``auditcore.people_names`` every few hours; name lookups read that table first.
"""
from __future__ import annotations

import os
from typing import Any

import httpx
import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_platform_super_admin_context, set_tenant_context

logger = structlog.get_logger(__name__)

_BATCH = 150


def _assigned_actor_ids(engine: Engine) -> list[str]:
    """Everyone with an active assignment in any Project. Assignments are
    tenant-isolated (row-level security), so each Project is read in its own
    tenant context; the platform context alone sees none of them."""
    with engine.begin() as connection:
        set_platform_super_admin_context(connection)
        tenants = [str(t) for t in connection.execute(
            text("SELECT tenant_id FROM auditcore.projects")).scalars().all()]
    ids: set[str] = set()
    for tenant_id in tenants:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            ids.update(str(r) for r in connection.execute(text(
                "SELECT DISTINCT security_actor_id FROM auditcore.business_assignments "
                "WHERE tenant_id = :t AND assignment_status = 'ACTIVE' AND security_actor_id IS NOT NULL"
            ), {"t": tenant_id}).scalars().all())
    return sorted(ids)


def fetch_names(ids: list[str], *, base_url: str, token: str, transport: Any = None) -> dict[str, str]:
    names: dict[str, str] = {}
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=10.0, transport=transport) as client:
        for start in range(0, len(ids), _BATCH):
            chunk = ids[start:start + _BATCH]
            response = client.get("/security/v1/service/user-names", params={"ids": ",".join(chunk)},
                                  headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
            for row in response.json():
                name = str(row.get("displayName") or "").strip()
                if name:
                    names[str(row["userId"])] = name
    return names


def store_names(engine: Engine, names: dict[str, str]) -> int:
    if not names:
        return 0
    with engine.begin() as connection:
        set_platform_super_admin_context(connection)
        for actor, name in names.items():
            connection.execute(text("""
                INSERT INTO auditcore.people_names (actor_id, display_name, refreshed_at_utc)
                VALUES (:a, :n, now())
                ON CONFLICT (actor_id) DO UPDATE SET display_name = EXCLUDED.display_name, refreshed_at_utc = now()
            """), {"a": actor, "n": name})
    return len(names)


def refresh_people_names(engine: Engine) -> int:
    """Copy the names of everyone assigned in any Project from Security. Best
    effort: a failure is logged and the previous names stay."""
    base_url = os.environ.get("SECURITY_BASE_URL", "").strip()
    client_id = os.environ.get("SECURITY_CLIENT_ID", "").strip()
    secret = os.environ.get("SECURITY_CLIENT_SECRET", "")
    if not base_url or not client_id or not secret:
        logger.info("people_names_skipped", reason="security not configured")
        return 0
    try:
        from audit_core.security_integration import SecurityOAuthClient

        ids = _assigned_actor_ids(engine)
        if not ids:
            logger.info("people_names_refreshed", people=0, named=0)
            return 0
        with SecurityOAuthClient(base_url=base_url, client_id=client_id, client_secret=secret) as oauth:
            token = oauth.get_service_token(audience="security")
        stored = store_names(engine, fetch_names(ids, base_url=base_url, token=token))
        logger.info("people_names_refreshed", people=len(ids), named=stored)
        return stored
    except Exception:  # best effort: the names already stored stay
        logger.warning("people_names_refresh_failed", exc_info=True)
        return 0
