"""uc03_model_selection_corrections.py — correct an already-CONFIRMED SKU
selection (2026-09-16, real user-reported gap).

``uc03_model_resolution._pin_sku`` (used by both the automatic resolver and
the PC's own manual "confirm SKU" picker, ``confirm_model_resolution_sku``)
refuses to overwrite a ``journey_products`` row whose ``selection_status``
is already 'CONFIRMED' -- intentional, so a routine automatic re-run can
never silently flip a locked-in deal. That guard also means neither path
can correct a SKU that was confirmed wrong, whether by the resolver's own
matching bugs (now fixed -- see ``uc03_model_resolution.py``'s generation-
refresh bridge and attribute-decomposition-before-price ordering) or by a
document-reader misread of the model/variant text in the first place.

This is a Task Queue item, not a rule-classified audit finding -- direct
user correction of this module's first cut, which raised an
``audit_findings`` row adjudicated through the finding-verdict machinery
(mirroring ``uc03_document_field_corrections.py``). Audit Review stays
reserved for what a rule actually detected; a PC-proposed, TL-decided SKU
correction is a human review-and-decide workflow item, and the Task Queue
already gives PC/TL full visibility into it for a given Journey. A PC's
propose call creates an ordinary ``workflow_tasks`` row (``task_type``
``MODEL_SELECTION_CORRECTION_REVIEW``, assigned to TL); a TL decides it
through the SAME generic Complete/Cancel actions every other task uses
(``tasks_api.py``), which is where this module's own side effect (the
actual SKU reassignment, on Complete only) is hooked in -- see
``apply_confirmed_model_selection_correction``.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError, NotFoundError
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_capture import _scope
from audit_core.uc03_model_resolution import (
    _price_plan_for_journey,
    _resolution_inputs,
    _run_deal_reconciliation,
    _sku_rows_for_version,
    reassign_confirmed_sku,
)
from audit_core.workflow import create_workflow_task

router = APIRouter(
    prefix="/v2/tenants/{tenant_id}/journeys/{journey_id}/booking/model-resolution",
    tags=["uc03-model-selection-corrections"],
)

TASK_TYPE = "MODEL_SELECTION_CORRECTION_REVIEW"
_WORKFLOW_TYPE = "UC03_MODEL_SELECTION_CORRECTION"
_STAGE = "BOOKING"


class ProposeModelSelectionCorrectionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    productSkuId: UUID
    reason: str = Field(min_length=1, max_length=2000)


class ModelSelectionCorrectionTaskResponse(BaseModel):
    taskId: UUID
    journeyId: UUID
    previousProductSkuId: UUID
    proposedProductSkuId: UUID
    proposedSkuCode: str
    reason: str


def _current_effective_rows(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[dict[str, Any]]:
    effective_on = date.fromisoformat(
        connection.execute(
            text(
                """
                SELECT COALESCE(b.booking_date, CURRENT_DATE)
                FROM auditcore.journeys j
                LEFT JOIN auditcore.bookings b
                  ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
                WHERE j.tenant_id = :tenant_id AND j.journey_id = :journey_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one().isoformat()
    )
    try:
        plan = _price_plan_for_journey(
            connection, tenant_id=tenant_id, journey_id=journey_id, effective_on=effective_on
        )
    except Exception as exc:
        raise AuditCoreError(
            error_code="VAC-SKU-003",
            status_code=422,
            title="No effective price list",
            detail="There is no effective price list for this Journey to propose a SKU against.",
        ) from exc
    return _sku_rows_for_version(
        connection, tenant_id=tenant_id, price_list_version_id=plan["price_list_version_id"]
    )


def _insert_proposal(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    workflow_task_id: UUID,
    previous_product_sku_id: UUID,
    proposed_product_sku_id: UUID,
    reason: str,
    actor_id: str,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.model_selection_correction_proposals (
                tenant_id, workflow_task_id, journey_id,
                previous_product_sku_id, proposed_product_sku_id,
                reason, proposed_by_actor_id
            ) VALUES (
                :tenant_id, :workflow_task_id, :journey_id,
                :previous_product_sku_id, :proposed_product_sku_id,
                :reason, :actor_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "workflow_task_id": workflow_task_id,
            "journey_id": journey_id,
            "previous_product_sku_id": previous_product_sku_id,
            "proposed_product_sku_id": proposed_product_sku_id,
            "reason": reason,
            "actor_id": actor_id,
        },
    )


@router.post("/propose-correction", response_model=ModelSelectionCorrectionTaskResponse)
def submit_model_selection_correction(
    tenant_id: str,
    journey_id: UUID,
    payload: ProposeModelSelectionCorrectionCommand,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
    ],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ModelSelectionCorrectionTaskResponse:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    inputs = _resolution_inputs(connection, tenant_id=tenant_id, journey_id=journey_id)
    if inputs is None:
        raise NotFoundError(
            error_code="VAC-NF-010",
            title="No reviewed booking model",
            detail="The Booking Form has not been reviewed yet -- nothing to correct.",
        )
    previous_product_sku_id = inputs["product_sku_id"]
    if previous_product_sku_id is None or inputs["selection_status"] != "CONFIRMED":
        raise AuditCoreError(
            error_code="VAC-SKU-004",
            status_code=422,
            title="No confirmed SKU to correct",
            detail=(
                "This Journey has no confirmed SKU selection yet -- use the model-resolution "
                "candidates/confirm-sku flow instead of proposing a correction."
            ),
        )
    if str(previous_product_sku_id) == str(payload.productSkuId):
        raise AuditCoreError(
            error_code="VAC-SKU-005",
            status_code=422,
            title="Proposed SKU is unchanged",
            detail="The proposed SKU is the same one already confirmed on this Journey.",
        )

    rows = _current_effective_rows(connection, tenant_id=tenant_id, journey_id=journey_id)
    proposed_row = next(
        (r for r in rows if str(r["product_sku_id"]) == str(payload.productSkuId)), None
    )
    if proposed_row is None:
        raise AuditCoreError(
            error_code="VAC-SKU-002",
            status_code=422,
            title="Unknown or inactive SKU",
            detail="The proposed SKU is not in this Journey's current effective price list.",
        )

    def execute() -> dict[str, Any]:
        task_id = create_workflow_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            workflow_type=_WORKFLOW_TYPE,
            process_area=_STAGE,
            task_type=TASK_TYPE,
            assigned_role_code="TL",
            task_payload={
                "previousProductSkuId": str(previous_product_sku_id),
                "proposedProductSkuId": str(payload.productSkuId),
                "proposedSkuCode": proposed_row["sku_code"],
                "reason": payload.reason,
            },
        )
        _insert_proposal(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            workflow_task_id=task_id,
            previous_product_sku_id=previous_product_sku_id,
            proposed_product_sku_id=payload.productSkuId,
            reason=payload.reason,
            actor_id=human_principal.subject,
        )
        return ModelSelectionCorrectionTaskResponse(
            taskId=task_id,
            journeyId=journey_id,
            previousProductSkuId=previous_product_sku_id,
            proposedProductSkuId=payload.productSkuId,
            proposedSkuCode=proposed_row["sku_code"],
            reason=payload.reason,
        ).model_dump(mode="json")

    body, _replay = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=(
            f"uc03.model-selection-correction.submit:{journey_id}:{payload.productSkuId}"
        ),
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(mode="json"),
        execute=execute,
    )
    return ModelSelectionCorrectionTaskResponse.model_validate(body)


def apply_confirmed_model_selection_correction(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    workflow_task_id: UUID,
    correlation_id: str | None,
) -> None:
    """Reassign the SKU to the proposed one and re-run deal reconciliation.

    Called from ``tasks_api.py``'s generic Complete-task action when the
    task being completed is a ``MODEL_SELECTION_CORRECTION_REVIEW`` --
    the ordinary "TL completes a task" gesture is what actually applies
    the correction here. Cancelling the task (the ordinary "reject"
    gesture) needs no equivalent hook: the original SKU simply stands.
    """
    proposal = connection.execute(
        text(
            """
            SELECT proposed_product_sku_id
            FROM auditcore.model_selection_correction_proposals
            WHERE tenant_id=:tenant_id AND workflow_task_id=:workflow_task_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "workflow_task_id": workflow_task_id},
    ).mappings().one_or_none()
    if proposal is None:
        # Defensive only: every MODEL_SELECTION_CORRECTION_REVIEW task is
        # created with its proposal row in the same transaction
        # (submit_model_selection_correction above) -- unreachable in practice.
        raise NotFoundError(
            error_code="VAC-NF-014",
            status_code=404,
            title="Correction proposal not found",
            detail="No proposed SKU correction is recorded for this task.",
        )

    reassign_confirmed_sku(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        product_sku_id=proposal["proposed_product_sku_id"],
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.model_selection_correction_proposals
            SET applied_at_utc=now(), updated_at_utc=now(), version_no=version_no+1
            WHERE tenant_id=:tenant_id AND workflow_task_id=:workflow_task_id
            """
        ),
        {"tenant_id": tenant_id, "workflow_task_id": workflow_task_id},
    )
    _run_deal_reconciliation(
        connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id or ""
    )


__all__ = [
    "TASK_TYPE",
    "apply_confirmed_model_selection_correction",
    "submit_model_selection_correction",
]
