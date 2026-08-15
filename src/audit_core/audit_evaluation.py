from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.errors import AuditCoreError, NotFoundError


def evaluate_control(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    audit_control_version_id: UUID,
    observed_snapshot: dict[str, Any],
    correlation_id: str | None = None,
) -> UUID:
    control = connection.execute(
        text(
            """
            SELECT acv.audit_control_version_id, acv.evaluator_key,
                   acv.rule_config, ac.process_area
            FROM auditcore.audit_control_versions acv
            JOIN auditcore.audit_controls ac
              ON ac.tenant_id = acv.tenant_id
             AND ac.audit_control_id = acv.audit_control_id
            WHERE acv.tenant_id = :tenant_id
              AND acv.audit_control_version_id = :version_id
              AND acv.lifecycle_status = 'PUBLISHED'
            """
        ),
        {"tenant_id": tenant_id, "version_id": audit_control_version_id},
    ).mappings().one_or_none()
    if control is None:
        raise NotFoundError(
            error_code="VAC-MASTER-002",
            title="Published audit control version not found",
            detail="Evaluation requires an exact published Audit Control version.",
        )

    evaluator_key = control["evaluator_key"]
    if evaluator_key != "EXACT_SNAPSHOT_MATCH":
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Unsupported audit evaluator",
            detail="The published Audit Control references an evaluator not implemented by this service.",
        )

    expected_snapshot = control["rule_config"].get("expectedSnapshot")
    if not isinstance(expected_snapshot, dict):
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Invalid audit control configuration",
            detail="EXACT_SNAPSHOT_MATCH requires rule_config.expectedSnapshot.",
        )

    result = "PASS" if observed_snapshot == expected_snapshot else "FAIL"
    evaluation_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_evaluations (
                tenant_id, journey_id, audit_control_version_id, process_area,
                evaluation_result, expected_snapshot, observed_snapshot,
                explanation, evaluator_key_snapshot, correlation_id
            ) VALUES (
                :tenant_id, :journey_id, :version_id, :process_area,
                :result, CAST(:expected AS jsonb), CAST(:observed AS jsonb),
                :explanation, :evaluator_key, :correlation_id
            ) RETURNING audit_evaluation_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "version_id": audit_control_version_id,
            "process_area": control["process_area"],
            "result": result,
            "expected": json.dumps(expected_snapshot),
            "observed": json.dumps(observed_snapshot),
            "explanation": "Observed snapshot matched configured expected snapshot."
            if result == "PASS"
            else "Observed snapshot differed from configured expected snapshot.",
            "evaluator_key": evaluator_key,
            "correlation_id": correlation_id,
        },
    ).scalar_one()
    return evaluation_id


def get_evaluation(connection: Connection, *, tenant_id: str, evaluation_id: UUID):
    row = connection.execute(
        text(
            """
            SELECT audit_evaluation_id, journey_id, audit_control_version_id,
                   process_area, evaluation_result, expected_snapshot,
                   observed_snapshot, explanation, evaluator_key_snapshot,
                   correlation_id, evaluated_at_utc
            FROM auditcore.audit_evaluations
            WHERE tenant_id = :tenant_id AND audit_evaluation_id = :evaluation_id
            """
        ),
        {"tenant_id": tenant_id, "evaluation_id": evaluation_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-013",
            title="Audit evaluation not found",
            detail="Audit evaluation not found for the requested tenant.",
        )
    return row
