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

Mirrors ``uc03_document_field_corrections.py``'s own PC-proposes / TL-
Confirm-Breach-applies / TL-Mark-False-Positive-rejects flow exactly, minus
its confidence-gated self-serve branch: a SKU reassignment on an already-
confirmed Journey always has commercial consequences (price, discounts),
so every proposal is adjudicated -- there is no <90%-equivalent immediate-
apply path here.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError, NotFoundError
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_audit_flags import (
    FlagMutationResponse,
    FlagView,
    _append_finding_event,
    _finding,
    _flag_view,
    _scope,
    _set_etag,
    _view_context,
)
from audit_core.uc03_finding_classification import resolve_classification
from audit_core.uc03_model_resolution import (
    _price_plan_for_journey,
    _resolution_inputs,
    _run_deal_reconciliation,
    _sku_rows_for_version,
    reassign_confirmed_sku,
)

router = APIRouter(
    prefix="/v2/tenants/{tenant_id}/journeys/{journey_id}/booking/model-resolution",
    tags=["uc03-model-selection-corrections"],
)

_FINDING_TYPE_CODE = "MODEL_SELECTION_CORRECTION_PROPOSED"


class ProposeModelSelectionCorrectionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    productSkuId: UUID
    reason: str = Field(min_length=1, max_length=2000)


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
    audit_finding_id: UUID,
    previous_product_sku_id: UUID,
    proposed_product_sku_id: UUID,
    reason: str,
    actor_id: str,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.model_selection_correction_proposals (
                tenant_id, audit_finding_id, journey_id,
                previous_product_sku_id, proposed_product_sku_id,
                reason, proposed_by_actor_id
            ) VALUES (
                :tenant_id, :audit_finding_id, :journey_id,
                :previous_product_sku_id, :proposed_product_sku_id,
                :reason, :actor_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "audit_finding_id": audit_finding_id,
            "journey_id": journey_id,
            "previous_product_sku_id": previous_product_sku_id,
            "proposed_product_sku_id": proposed_product_sku_id,
            "reason": reason,
            "actor_id": actor_id,
        },
    )


@router.post("/propose-correction", response_model=FlagMutationResponse)
def submit_model_selection_correction(
    tenant_id: str,
    journey_id: UUID,
    payload: ProposeModelSelectionCorrectionCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
    ],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> FlagMutationResponse:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        # Same operation key uc03_document_field_corrections.py's own
        # PC-proposes-a-correction endpoint uses -- already permits PC (and
        # TL/PM/EXECUTIVE); no new role-policy wiring needed for this to
        # match the requested "PC edits it, a review flag goes to TL" shape.
        operation="PROPOSE_CORRECTION",
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

    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        rule_key = f"{_FINDING_TYPE_CODE}:{journey_id}"
        routing = resolve_classification(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            rule_key=rule_key,
            finding_type_code=_FINDING_TYPE_CODE,
            severity="HIGH",
        )
        title = f"Proposed SKU correction: {proposed_row['sku_code']}"
        flag_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_findings (
                    tenant_id, journey_id, finding_type_code, severity,
                    finding_status, title, description, created_by_actor_id,
                    correlation_id, stage_code, origin_kind, origin_actor_id,
                    origin_role_snapshot, rule_key, blocking_completion,
                    finding_class, owner_role_code, sla_due_at_utc
                ) VALUES (
                    :tenant_id, :journey_id, :finding_type_code, :severity,
                    'OPEN', :title, :description, :actor_id,
                    :correlation_id, 'BOOKING', 'HUMAN', :actor_id,
                    :actor_role, :rule_key, false,
                    :finding_class, :owner_role_code, :sla_due_at_utc
                ) RETURNING audit_finding_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "finding_type_code": _FINDING_TYPE_CODE,
                "severity": "HIGH",
                "title": title,
                "description": payload.reason,
                "actor_id": human_principal.subject,
                "correlation_id": correlation_id,
                "actor_role": context["operating_role"],
                "rule_key": rule_key,
                **routing,
            },
        ).scalar_one()
        _insert_proposal(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            audit_finding_id=flag_id,
            previous_product_sku_id=previous_product_sku_id,
            proposed_product_sku_id=payload.productSkuId,
            reason=payload.reason,
            actor_id=human_principal.subject,
        )
        event_id = _append_finding_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            flag_id=flag_id,
            stage_code="BOOKING",
            event_type="RAISED",
            actor_id=human_principal.subject,
            actor_role=context["operating_role"],
            reason=payload.reason,
            correlation_id=correlation_id,
            safe_payload={
                "originKind": "HUMAN",
                "category": _FINDING_TYPE_CODE,
                "previousProductSkuId": str(previous_product_sku_id),
                "proposedProductSkuId": str(payload.productSkuId),
                "proposedSkuCode": proposed_row["sku_code"],
            },
        )
        row = _finding(connection, tenant_id=tenant_id, journey_id=journey_id, flag_id=flag_id)
        role, policy = _view_context(context)
        return {
            "flag": _flag_view(
                connection, tenant_id=tenant_id, row=row, role=role, policy=policy
            ).model_dump(mode="json"),
            "eventId": str(event_id),
        }

    body, replay = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=(
            f"uc03.model-selection-correction.submit:{journey_id}:{payload.productSkuId}"
        ),
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(mode="json"),
        execute=execute,
    )
    flag = FlagView.model_validate(body["flag"])
    _set_etag(response, flag.version)
    return FlagMutationResponse(flag=flag, eventId=UUID(body["eventId"]), idempotent=replay)


def apply_confirmed_model_selection_correction(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    audit_finding_id: UUID,
    correlation_id: str,
) -> None:
    """Reassign the SKU to the proposed one and re-run deal reconciliation.

    Called from ``uc03_audit_flags.py::act_on_flag``'s execute() on
    CONFIRM_BREACH for a MODEL_SELECTION_CORRECTION_PROPOSED finding,
    inside the same transaction as the finding's own status update.
    MARK_FALSE_POSITIVE needs no extra step -- the original SKU stands.
    """
    proposal = connection.execute(
        text(
            """
            SELECT proposed_product_sku_id
            FROM auditcore.model_selection_correction_proposals
            WHERE tenant_id=:tenant_id AND audit_finding_id=:audit_finding_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "audit_finding_id": audit_finding_id},
    ).mappings().one_or_none()
    if proposal is None:
        # Defensive only: every MODEL_SELECTION_CORRECTION_PROPOSED finding
        # is created with its proposal row in the same transaction
        # (submit_model_selection_correction above) -- unreachable in practice.
        raise NotFoundError(
            error_code="VAC-NF-014",
            status_code=404,
            title="Correction proposal not found",
            detail="No proposed SKU correction is recorded for this finding.",
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
            WHERE tenant_id=:tenant_id AND audit_finding_id=:audit_finding_id
            """
        ),
        {"tenant_id": tenant_id, "audit_finding_id": audit_finding_id},
    )
    _run_deal_reconciliation(
        connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
    )


__all__ = [
    "apply_confirmed_model_selection_correction",
    "submit_model_selection_correction",
]
