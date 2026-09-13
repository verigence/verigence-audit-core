"""uc03_run_all_rules.py — "Run All Applicable Rules", the manual trigger
(Phase 5 of the rule-engine platform).

  POST /v1/tenants/{tenant_id}/journeys/{journey_id}/uc03/run-all-rules

The button-press trigger the platform design calls for, distinct from (and
narrower in one sense, wider in another than) the existing Resync buttons
(``resync_booking_capture_v2`` / ``resync_delivery_capture_v2``):

  - Resync's job is re-fetching from DI and re-copying facts -- it exists
    because a document's classification/extraction can change after the
    fact. Running it already re-triggers every DOCUMENT_SYNCED rule as a
    side effect of that DI round trip.
  - This endpoint's job is narrower and cheaper: re-evaluate every rule
    against data ALREADY durably stored, with no DI call at all -- for
    when nothing new has been uploaded but a rule should still be checked
    right now (a PC/TL/PM pressing a button, not waiting for the next
    document event). It calls the exact same producer functions
    DOCUMENT_SYNCED already uses, plus the rule-engine's own phase
    evaluation for every stage this journey actually has.

Every producer here is already idempotent/self-healing by its own
docstring -- calling this repeatedly is always safe. Each producer's
Execution Log row is tagged ``triggering_event='MANUAL_RUN_ALL_RULES'``, so
a manually-triggered PASS/FAIL/SKIPPED is distinguishable from an
automatic one in the same audit trail.
"""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import Connection, Engine, text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_engine, get_human_principal
from audit_core.errors import DependencyUnavailableError
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    SecurityAuthorizationError,
    get_security_authorization_client,
)
from audit_core.uc03_rule_execution_log import (
    record_execution,
    record_from_resolution,
    record_from_summary,
)

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}/uc03", tags=["uc03-run-all-rules"]
)

_PERMISSION_KEY = "audit.finding.create"
_TRIGGERING_EVENT = "MANUAL_RUN_ALL_RULES"


def _authorize(
    client: SecurityAuthorizationClient, *, human_principal: HumanPrincipal, tenant_id: str
) -> None:
    try:
        decision = client.check_user_permission(
            user_id=human_principal.subject, tenant_id=tenant_id, permission_key=_PERMISSION_KEY
        )
    except SecurityAuthorizationError as exc:
        raise DependencyUnavailableError(
            detail="Running rules is temporarily unavailable. Please try again."
        ) from exc
    if not decision.allowed:
        raise AuthorizationError(
            error_code="VAC-AUTH-002", status_code=403, title="Permission denied"
        )


def _existing_stages(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[str]:
    # BOOKING/DELIVERY only -- every producer called below is written for
    # exactly those two; a POST_DELIVERY row (a real, separate stage some
    # journeys carry) has no rules instrumented against it yet.
    rows = connection.execute(
        text(
            "SELECT stage_code FROM auditcore.journey_stage_states "
            "WHERE tenant_id=:t AND journey_id=:j AND stage_code IN ('BOOKING','DELIVERY') "
            "ORDER BY stage_code"
        ),
        {"t": tenant_id, "j": journey_id},
    ).scalars()
    return list(rows)


class RunAllRulesRuleResult(BaseModel):
    ruleCode: str
    stage: str
    outcome: str  # PASS | FAIL | SKIPPED | ERROR


class RunAllRulesResponse(BaseModel):
    journeyId: UUID
    stagesEvaluated: list[str]
    results: list[RunAllRulesRuleResult]


def _run_audit_core_rules_for_stage(
    connection: Connection, *, tenant_id: str, journey_id: UUID, stage: str, correlation_id: str
) -> list[RunAllRulesRuleResult]:
    from audit_core.uc03_async_sync_tasks import (
        reconcile_payments_with_escalation,
        sync_model_resolution_from_invoice_with_escalation,
        sync_model_resolution_with_escalation,
    )
    from audit_core.uc03_customer_identity_consistency import (
        sync_customer_identity_consistency,
    )
    from audit_core.uc03_duplicate_booking_detection import (
        sync_duplicate_booking_detection,
    )
    from audit_core.uc03_duplicate_receipt_detection import (
        sync_duplicate_receipt_detection,
    )
    from audit_core.uc03_manual_verification import sync_manual_verification_findings

    results: list[RunAllRulesRuleResult] = []

    def _record(rule_code: str, outcome: str) -> None:
        results.append(RunAllRulesRuleResult(ruleCode=rule_code, stage=stage, outcome=outcome))

    # These four are journey-wide (not scoped to one stage's documents), so
    # only run them once, on the first stage evaluated -- calling them a
    # second time for a second stage would just be redundant, identical work.
    if stage == "BOOKING":
        identity_result = sync_customer_identity_consistency(
            connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
        )
        record_from_summary(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            rule_code="WRONG_DOCUMENT", triggering_event=_TRIGGERING_EVENT,
            result=identity_result, skipped_reason="no other named document to compare against a KYC reference yet",
        )
        _record("WRONG_DOCUMENT", "ERROR" if identity_result.get("error") else ("SKIPPED" if identity_result.get("examined", 0) == 0 else ("FAIL" if identity_result.get("raised", 0) else "PASS")))

        duplicate_receipt_result = sync_duplicate_receipt_detection(
            connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
        )
        record_from_summary(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            rule_code="DUPLICATE_RECEIPT", triggering_event=_TRIGGERING_EVENT,
            result=duplicate_receipt_result, skipped_reason="no receipt documents on this Journey yet",
        )
        _record("DUPLICATE_RECEIPT", "ERROR" if duplicate_receipt_result.get("error") else ("SKIPPED" if duplicate_receipt_result.get("examined", 0) == 0 else ("FAIL" if duplicate_receipt_result.get("raised", 0) else "PASS")))

        duplicate_booking_result = sync_duplicate_booking_detection(
            connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
        )
        record_from_summary(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            rule_code="DUPLICATE_BOOKING", triggering_event=_TRIGGERING_EVENT,
            result=duplicate_booking_result,
            skipped_reason="no PAN/Aadhaar/name identified for this customer yet, or no other journey to compare against",
        )
        _record("DUPLICATE_BOOKING", "ERROR" if duplicate_booking_result.get("error") else ("SKIPPED" if duplicate_booking_result.get("examined", 0) == 0 else ("FAIL" if duplicate_booking_result.get("raised", 0) else "PASS")))

        model_result = sync_model_resolution_with_escalation(
            connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
        )
        record_from_resolution(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            rule_code="MODEL_NOT_IDENTIFIED", triggering_event=_TRIGGERING_EVENT, result=model_result,
        )
        _record("MODEL_NOT_IDENTIFIED", "ERROR" if model_result.get("error") else ("SKIPPED" if model_result.get("skipped") else ("FAIL" if model_result.get("raised") else "PASS")))
    else:
        invoice_model_result = sync_model_resolution_from_invoice_with_escalation(
            connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
        )
        record_from_resolution(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            rule_code="MODEL_NOT_IDENTIFIED", triggering_event=_TRIGGERING_EVENT, result=invoice_model_result,
        )
        _record("MODEL_NOT_IDENTIFIED", "ERROR" if invoice_model_result.get("error") else ("SKIPPED" if invoice_model_result.get("skipped") else ("FAIL" if invoice_model_result.get("raised") else "PASS")))

    manual_verification_result = sync_manual_verification_findings(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage, correlation_id=correlation_id
    )
    record_from_summary(
        connection, tenant_id=tenant_id, journey_id=journey_id,
        rule_code="MANUAL_VERIFICATION", triggering_event=_TRIGGERING_EVENT,
        result=manual_verification_result, skipped_reason=f"no extracted fields for {stage} yet",
    )
    _record("MANUAL_VERIFICATION", "ERROR" if manual_verification_result.get("error") else ("SKIPPED" if manual_verification_result.get("examined", 0) == 0 else ("FAIL" if manual_verification_result.get("raised", 0) else "PASS")))

    reconciliation_result = reconcile_payments_with_escalation(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage, correlation_id=correlation_id
    )
    if reconciliation_result.get("error"):
        record_execution(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            rule_code="PAYMENT_BANK_UNMATCHED", triggering_event=_TRIGGERING_EVENT,
            outcome="ERROR", reason="reconcile_payments raised",
        )
        record_execution(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            rule_code="AUTOMATED_SYNC_FAILURE", triggering_event=_TRIGGERING_EVENT,
            outcome="FAIL", reason="payment reconciliation raised an unexpected internal error",
        )
        _record("PAYMENT_BANK_UNMATCHED", "ERROR")
        _record("AUTOMATED_SYNC_FAILURE", "FAIL")
    else:
        record_execution(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            rule_code="AUTOMATED_SYNC_FAILURE", triggering_event=_TRIGGERING_EVENT, outcome="PASS",
        )
        _record("AUTOMATED_SYNC_FAILURE", "PASS")
        if reconciliation_result.get("skipped"):
            record_execution(
                connection, tenant_id=tenant_id, journey_id=journey_id,
                rule_code="PAYMENT_BANK_UNMATCHED", triggering_event=_TRIGGERING_EVENT,
                outcome="SKIPPED", reason=str(reconciliation_result.get("reason") or "no payments to reconcile yet"),
            )
            _record("PAYMENT_BANK_UNMATCHED", "SKIPPED")
        else:
            outcome = "FAIL" if reconciliation_result.get("unmatched", 0) > 0 else "PASS"
            record_execution(
                connection, tenant_id=tenant_id, journey_id=journey_id,
                rule_code="PAYMENT_BANK_UNMATCHED", triggering_event=_TRIGGERING_EVENT, outcome=outcome,
            )
            _record("PAYMENT_BANK_UNMATCHED", outcome)

    return results


@router.post("/run-all-rules", response_model=RunAllRulesResponse)
def run_all_applicable_rules(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> RunAllRulesResponse:
    _authorize(authorization_client, human_principal=human_principal, tenant_id=tenant_id)
    set_tenant_context(connection, tenant_id)
    correlation_id = get_correlation_id(request)

    stages = _existing_stages(connection, tenant_id=tenant_id, journey_id=journey_id)
    results: list[RunAllRulesRuleResult] = []
    for stage in stages:
        results.extend(
            _run_audit_core_rules_for_stage(
                connection, tenant_id=tenant_id, journey_id=journey_id,
                stage=stage, correlation_id=correlation_id,
            )
        )

    # run_rule_engine_phase opens its own connection from the pool (the
    # same pattern its other callers already use) -- it doesn't need to see
    # this request's own writes to do its job (its anomaly detection reads
    # DI Subject data, not audit-core's own just-written findings), so no
    # manual commit of the injected `connection` is needed here; FastAPI's
    # own dependency teardown commits it once this route returns.
    from audit_core.uc03_rule_engine_findings import run_rule_engine_phase

    for stage in stages:
        run_rule_engine_phase(
            engine, tenant_id, journey_id, stage, stage, correlation_id,
        )

    return RunAllRulesResponse(journeyId=journey_id, stagesEvaluated=stages, results=results)
