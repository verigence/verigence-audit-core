"""uc03_rule_engine_findings.py — materialise rule-engine anomalies as audit findings.

Option C of the rule-engine integration: Audit Core stays the single writer of
``auditcore.audit_findings``. After a Booking Review is confirmed and after a
Delivery is completed, Audit Core calls the rule-engine's phase-audit API and
projects each returned anomaly into a MACHINE finding via ``_machine_flag`` — the
same path the in-process BK_* / DL_* checkpoint rules already use, so the PC
dashboard's open-flag count picks them up with no further wiring.

Everything here is best-effort:
  * ``RULE_ENGINE_BASE_URL`` unset  → the whole step is skipped (dormant).
  * no DI Subject for the journey   → skipped (nothing for the rule-engine to see).
  * rule-engine down / errors       → logged, Booking/Delivery unaffected.

The nightly reconciliation batch remains the safety net for anything missed here.
"""
from __future__ import annotations

import os
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.rule_engine_client import (
    RULE_ENGINE_AUDIENCE,
    RuleEngineAnomaly,
    build_rule_engine_client,
)
from audit_core.security_integration import SecurityOAuthClient
from audit_core.uc03_delivery_commands import _machine_flag

logger = structlog.get_logger(__name__)

_RULE_KEY_PREFIX = "RE_"

# rule-engine severity (CRITICAL|WARNING|INFO) → audit-core severity.
_SEVERITY_MAP = {
    "CRITICAL": "CRITICAL",
    "WARNING": "HIGH",
    "INFO": "MEDIUM",
}
_DEFAULT_SEVERITY = "MEDIUM"

# rule-engine anomaly category → audit-core finding_type_code.
_FINDING_TYPE_BY_CATEGORY = {
    "PRICE": "PRICING_ANOMALY",
    "DISCOUNT": "DISCOUNT_ANOMALY",
    "ACCESSORY": "ACCESSORY_ANOMALY",
    "INSURANCE": "INSURANCE_ANOMALY",
    "RTO": "RTO_ANOMALY",
    "VEHICLE": "VEHICLE_IDENTITY_ANOMALY",
    "KYC": "CUSTOMER_IDENTITY_CONCERN",
    "DATE": "PROCESS_NON_COMPLIANCE",
    "CROSS_CASE": "CROSS_CASE_DUPLICATE",
}
_DEFAULT_FINDING_TYPE = "RULE_ENGINE_ANOMALY"

_TITLE_MAX = 300


def _build_security_oauth_client() -> SecurityOAuthClient | None:
    base_url = os.environ.get("SECURITY_BASE_URL", "").strip()
    client_id = os.environ.get("SECURITY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SECURITY_CLIENT_SECRET", "")
    if not base_url or not client_id or not client_secret:
        return None
    return SecurityOAuthClient(
        base_url=base_url, client_id=client_id, client_secret=client_secret
    )


def _resolve_di_subject_id(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> UUID | None:
    return connection.execute(
        text(
            """
            SELECT m.di_subject_id
            FROM auditcore.journeys j
            JOIN auditcore.di_subject_mappings m
              ON m.tenant_id = j.tenant_id AND m.customer_id = j.customer_id
            WHERE j.tenant_id = :tenant_id
              AND j.journey_id = :journey_id
              AND m.mapping_status = 'ACTIVE'
            ORDER BY m.created_at_utc DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()


def _finding_title(anomaly: RuleEngineAnomaly) -> str:
    base = anomaly.detail or f"Rule {anomaly.rule_code} raised an anomaly"
    title = " ".join(base.split())
    if len(title) > _TITLE_MAX:
        title = title[: _TITLE_MAX - 1].rstrip() + "…"
    return title


def _materialize_anomalies(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    anomalies: tuple[RuleEngineAnomaly, ...],
    correlation_id: str,
    audit_run_id: str | None,
) -> list[str]:
    flagged: list[str] = []
    for anomaly in anomalies:
        severity = _SEVERITY_MAP.get(anomaly.severity, _DEFAULT_SEVERITY)
        finding_type = _FINDING_TYPE_BY_CATEGORY.get(
            (anomaly.category or "").upper(), _DEFAULT_FINDING_TYPE
        )
        rule_key = f"{_RULE_KEY_PREFIX}{anomaly.rule_code}"
        _machine_flag(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage_code,  # "BOOKING" | "DELIVERY"
            rule_key=rule_key,
            finding_type=finding_type,
            severity=severity,
            title=_finding_title(anomaly),
            description=anomaly.detail,
            correlation_id=correlation_id,
            safe_payload={
                "trigger": "RULE_ENGINE_PHASE_AUDIT",
                "ruleCode": anomaly.rule_code,
                "ruleEngineSeverity": anomaly.severity,
                "category": anomaly.category,
                "leftValue": anomaly.left_value,
                "rightValue": anomaly.right_value,
                "auditRunId": audit_run_id,
            },
            blocking_completion=False,
        )
        flagged.append(rule_key)
    return flagged


def run_rule_engine_phase(
    engine: Engine,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    phase: str,
    correlation_id: str,
) -> None:
    """Best-effort rule-engine phase audit + finding materialisation.

    Safe to hand straight to ``BackgroundTasks.add_task``; never raises.
    """
    client = build_rule_engine_client()
    if client is None:
        return  # integration dormant — RULE_ENGINE_BASE_URL not set

    try:
        security_client = _build_security_oauth_client()
        if security_client is None:
            logger.warning(
                "uc03_rule_engine_security_not_configured",
                tenant_id=tenant_id,
                journey_id=str(journey_id),
            )
            return
        try:
            token = security_client.get_service_token(audience=RULE_ENGINE_AUDIENCE)
        finally:
            security_client.close()

        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            subject_id = _resolve_di_subject_id(
                connection, tenant_id=tenant_id, journey_id=journey_id
            )
        if subject_id is None:
            logger.info(
                "uc03_rule_engine_no_di_subject",
                tenant_id=tenant_id,
                journey_id=str(journey_id),
                phase=phase,
            )
            return

        result = client.evaluate_phase(
            token=token,
            tenant_id=tenant_id,
            subject_id=str(subject_id),
            phase=phase,
        )

        if not result.anomalies:
            logger.info(
                "uc03_rule_engine_phase_clean",
                tenant_id=tenant_id,
                journey_id=str(journey_id),
                phase=phase,
                verdict=result.verdict,
            )
            return

        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            flagged = _materialize_anomalies(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                stage_code=stage_code,
                anomalies=result.anomalies,
                correlation_id=correlation_id,
                audit_run_id=result.audit_run_id,
            )

        logger.info(
            "uc03_rule_engine_phase_materialized",
            tenant_id=tenant_id,
            journey_id=str(journey_id),
            phase=phase,
            verdict=result.verdict,
            anomaly_count=len(result.anomalies),
            flagged_rule_keys=sorted(flagged),
        )
    except Exception:
        logger.warning(
            "uc03_rule_engine_phase_failed",
            tenant_id=tenant_id,
            journey_id=str(journey_id),
            phase=phase,
            exc_info=True,
        )
    finally:
        client.close()
