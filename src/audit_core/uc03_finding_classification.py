"""uc03_finding_classification.py — the one place a finding gets its class + SLA.

Every producer of ``auditcore.audit_findings`` calls ``resolve_classification`` to
get ``finding_class`` / ``owner_role_code`` / ``sla_due_at_utc`` for the INSERT.

Resolution order:
  1. an explicit override from the caller
  2. the rule that raised it (``uc03_finding_routing.classify_by_rule_key``)
  3. the ``auditcore.finding_types`` registry (row for this finding_type_code)
  4. the finding_type heuristic (``classify_by_type``)
  5. VIOLATION — and the type is recorded in the registry as UNCLASSIFIED so it
     surfaces for a human to classify, and a metric is emitted.

The registry is small reference data; it is read once per request and cached
briefly.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, text

from audit_core.telemetry import record_metric
from audit_core.uc03_finding_routing import (
    DEFAULT_CLASS,
    FindingClass,
    class_profile,
    classify_by_rule_key,
    classify_by_type,
    resolve_sla_policy,
    sla_due_at,
)

logger = structlog.get_logger(__name__)

_REGISTRY_TTL_SECONDS = 30.0
_registry_cache: tuple[float, dict[str, dict[str, str]]] | None = None


def _load_registry(connection: Connection) -> dict[str, dict[str, str]]:
    global _registry_cache
    now = time.monotonic()
    if _registry_cache is not None and now - _registry_cache[0] < _REGISTRY_TTL_SECONDS:
        return _registry_cache[1]
    try:
        rows = connection.execute(
            text(
                """
                SELECT finding_type_code, finding_class, default_owner_role, status
                FROM auditcore.finding_types
                """
            )
        ).mappings().all()
        registry = {
            str(r["finding_type_code"]).upper(): {
                "finding_class": str(r["finding_class"]),
                "default_owner_role": str(r["default_owner_role"]),
                "status": str(r["status"]),
            }
            for r in rows
        }
    except Exception:
        logger.warning("finding_type_registry_load_failed", exc_info=True)
        return _registry_cache[1] if _registry_cache is not None else {}
    _registry_cache = (now, registry)
    return registry


def _register_unclassified(
    connection: Connection, *, finding_type_code: str
) -> None:
    global _registry_cache
    try:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.finding_types
                    (finding_type_code, finding_class, default_owner_role,
                     resolution_mode, status, description)
                VALUES (:code, :cls, :owner, 'ADJUDICATED', 'UNCLASSIFIED',
                        'Auto-registered on first use; needs a class from a Super Admin.')
                ON CONFLICT (finding_type_code) DO NOTHING
                """
            ),
            {"code": finding_type_code, "cls": DEFAULT_CLASS, "owner": "TL"},
        )
    except Exception:
        logger.warning(
            "finding_type_auto_register_failed",
            finding_type_code=finding_type_code,
            exc_info=True,
        )
    _registry_cache = None  # force reload so the new row is seen
    record_metric(
        "audit_finding_unclassified_total",
        1,
        labels={"finding_type_code": finding_type_code},
    )
    logger.warning("audit_finding_unclassified", finding_type_code=finding_type_code)


def journey_policy_settings(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT ppv.policy_settings
            FROM auditcore.journeys j
            LEFT JOIN auditcore.project_policy_versions ppv
              ON ppv.tenant_id = j.tenant_id
             AND ppv.policy_version_id = j.policy_version_id
            WHERE j.tenant_id = :tenant_id AND j.journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()
    return row if isinstance(row, dict) else {}


def resolve_class(
    connection: Connection,
    *,
    rule_key: str | None,
    finding_type_code: str | None,
    class_override: str | None = None,
) -> tuple[FindingClass, str]:
    """Return (finding_class, owner_role_code). Records an unknown type."""
    if class_override in ("DATA_GAP", "DOCUMENT_GAP", "VIOLATION"):
        cls: FindingClass = class_override  # type: ignore[assignment]
        return cls, class_profile(cls).owner_role

    by_rule = classify_by_rule_key(rule_key)
    if by_rule is not None:
        return by_rule, class_profile(by_rule).owner_role

    code = (finding_type_code or "").strip().upper()
    entry = _load_registry(connection).get(code)
    if entry is not None and entry["status"] != "UNCLASSIFIED":
        cls = entry["finding_class"]  # type: ignore[assignment]
        return cls, entry["default_owner_role"]

    by_type = classify_by_type(finding_type_code)
    if by_type is not None:
        return by_type, class_profile(by_type).owner_role

    if code:
        _register_unclassified(connection, finding_type_code=code)
    return DEFAULT_CLASS, class_profile(DEFAULT_CLASS).owner_role


def resolve_classification(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    rule_key: str | None,
    finding_type_code: str | None,
    severity: str,
    class_override: str | None = None,
    created_at: Any = None,
) -> dict[str, Any]:
    """finding_class / owner_role_code / sla_due_at_utc for a new finding INSERT."""
    finding_class, owner_role = resolve_class(
        connection,
        rule_key=rule_key,
        finding_type_code=finding_type_code,
        class_override=class_override,
    )
    policy = resolve_sla_policy(
        journey_policy_settings(connection, tenant_id=tenant_id, journey_id=journey_id)
    )
    due_at = sla_due_at(
        created_at or datetime.now(UTC),
        finding_class=finding_class,
        severity=severity,
        policy=policy,
    )
    return {
        "finding_class": finding_class,
        "owner_role_code": owner_role,
        "sla_due_at_utc": due_at,
    }
