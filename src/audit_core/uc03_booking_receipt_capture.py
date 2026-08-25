from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core import uc03_booking_capture
from audit_core.errors import AuditCoreError, ConflictError, NotFoundError
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id

_RECEIPT_DOCUMENT_TYPE = "dealer_receipt"
_RECEIPT_CAPTURE_MAP: dict[str, str] = {
    "dealer_name": "RECEIPT_DEALER_NAME",
    "dealer_gstin": "RECEIPT_DEALER_GSTIN",
    "customer_name": "RECEIPT_CUSTOMER_NAME",
    "customer_phone": "RECEIPT_CUSTOMER_PHONE",
    "receipt_number": "RECEIPT_NUMBER",
    "receipt_date": "RECEIPT_DATE",
    "amount_paid": "RECEIPT_AMOUNT",
    "payment_mode": "RECEIPT_PAYMENT_MODE",
    "payment_reference_no": "RECEIPT_PAYMENT_REFERENCE",
    "payment_reference_date": "RECEIPT_PAYMENT_REFERENCE_DATE",
    "bank_name": "RECEIPT_BANK_NAME",
    "bank_location": "RECEIPT_BANK_LOCATION",
    "booking_reference_number": "RECEIPT_BOOKING_REFERENCE",
    "remarks": "RECEIPT_REMARKS",
    "amount_in_words": "RECEIPT_AMOUNT_IN_WORDS",
}
_RECEIPT_DETAIL_KEYS: dict[str, str] = {
    "RECEIPT_DEALER_NAME": "dealer_name",
    "RECEIPT_DEALER_GSTIN": "dealer_gstin",
    "RECEIPT_CUSTOMER_NAME": "customer_name",
    "RECEIPT_CUSTOMER_PHONE": "customer_phone",
    "RECEIPT_PAYMENT_REFERENCE_DATE": "payment_reference_date",
    "RECEIPT_BANK_NAME": "bank_name",
    "RECEIPT_BANK_LOCATION": "bank_location",
    "RECEIPT_BOOKING_REFERENCE": "booking_reference_number",
    "RECEIPT_REMARKS": "remarks",
    "RECEIPT_AMOUNT_IN_WORDS": "amount_in_words",
}

_installed = False
_original_decide_proposal = None
_original_proposals = None
_original_completion_summary = None


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _receipt_proposal_row(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    proposal_id: UUID,
):
    row = connection.execute(
        text(
            """
            SELECT capture_proposal_id, field_key, source_evidence_id,
                   source_document_type_key, proposed_value, proposal_status,
                   accepted_value, version_no
            FROM auditcore.journey_capture_proposals
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING' AND capture_proposal_id=:proposal_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "proposal_id": proposal_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-014",
            title="Extraction proposal not found",
            detail="The Booking extraction proposal was not found.",
        )
    return row


def _write_receipt_capture(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    capture_key: str,
    value: Any,
    source_evidence_id: UUID,
) -> tuple[str, str]:
    uc03_booking_capture._validate_evidence_for_journey(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        evidence_id=source_evidence_id,
    )

    row = connection.execute(
        text(
            """
            SELECT payment_id, receipt_details
            FROM auditcore.payments
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND source_evidence_id=:evidence_id
            ORDER BY created_at_utc, payment_id
            LIMIT 1
            FOR UPDATE
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "evidence_id": source_evidence_id,
        },
    ).mappings().one_or_none()

    if row is None:
        payment_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.payments (
                    tenant_id, journey_id, amount, status_source,
                    source_evidence_id, receipt_details
                ) VALUES (
                    :tenant_id, :journey_id, 0, 'EVIDENCE',
                    :evidence_id, CAST(:details AS jsonb)
                )
                RETURNING payment_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "evidence_id": source_evidence_id,
                "details": json.dumps({"_part1AmountReviewed": False}),
            },
        ).scalar_one()
        details: dict[str, Any] = {"_part1AmountReviewed": False}
    else:
        payment_id = row["payment_id"]
        details = dict(row["receipt_details"] or {})

    params: dict[str, Any] = {
        "tenant_id": tenant_id,
        "payment_id": payment_id,
    }
    assignment: str

    if capture_key == "RECEIPT_NUMBER":
        assignment = "receipt_number=:value"
        params["value"] = _text_or_none(value)
    elif capture_key == "RECEIPT_DATE":
        assignment = "receipt_date=:value"
        params["value"] = (
            uc03_booking_capture._as_date(value, capture_key) if value is not None else None
        )
    elif capture_key == "RECEIPT_AMOUNT":
        if value is None:
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Business validation failed",
                detail="Receipt Amount cannot be approved without a value.",
            )
        assignment = "amount=:value"
        params["value"] = uc03_booking_capture._as_decimal(value, capture_key)
        details["_part1AmountReviewed"] = True
    elif capture_key == "RECEIPT_PAYMENT_MODE":
        assignment = "payment_method_code=:value"
        params["value"] = _text_or_none(value)
    elif capture_key == "RECEIPT_PAYMENT_REFERENCE":
        assignment = "payment_reference=:value"
        params["value"] = _text_or_none(value)
    elif capture_key in _RECEIPT_DETAIL_KEYS:
        details[_RECEIPT_DETAIL_KEYS[capture_key]] = value
        assignment = "receipt_details=CAST(:details AS jsonb)"
        params["details"] = json.dumps(details, default=str)
    else:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Unsupported receipt field",
            detail="This Dealer Receipt field is not configured for review.",
        )

    # Keep the review metadata while updating a core receipt column as well.
    if capture_key not in _RECEIPT_DETAIL_KEYS:
        params["details"] = json.dumps(details, default=str)
        assignment = f"{assignment}, receipt_details=CAST(:details AS jsonb)"

    connection.execute(
        text(
            f"""
            UPDATE auditcore.payments
            SET {assignment},
                status_source='EVIDENCE',
                source_evidence_id=(
                    SELECT source_evidence_id
                    FROM auditcore.payments
                    WHERE tenant_id=:tenant_id AND payment_id=:payment_id
                ),
                updated_at_utc=now(),
                version_no=version_no+1
            WHERE tenant_id=:tenant_id AND payment_id=:payment_id
            """
        ),
        params,
    )
    return "PAYMENT", str(payment_id)


def _decide_proposal(
    *,
    tenant_id: str,
    journey_id: UUID,
    proposal_id: UUID,
    payload,
    corrected: bool,
    request,
    response,
    idempotency_key: str,
    if_match: str,
    human_principal,
    authorization_client,
    connection: Connection,
) -> dict[str, Any]:
    context = uc03_booking_capture._scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_version = uc03_booking_capture._parse_if_match(if_match)
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        uc03_booking_capture._aggregate_lock(
            connection, tenant_id=tenant_id, journey_id=journey_id
        )
        state = uc03_booking_capture._stage_state(
            connection, tenant_id=tenant_id, journey_id=journey_id
        )
        uc03_booking_capture._require_expected_version(state, expected_version)
        uc03_booking_capture._require_active_booking(state)
        proposal = _receipt_proposal_row(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            proposal_id=proposal_id,
        )
        if proposal["proposal_status"] != "PENDING":
            raise ConflictError(
                error_code="VAC-CONFLICT-008",
                title="Extraction proposal already decided",
                detail="Refresh the Booking before deciding this extraction proposal.",
            )

        document_type = str(proposal["source_document_type_key"] or "").strip().lower()
        receipt_capture_key = (
            _RECEIPT_CAPTURE_MAP.get(proposal["field_key"])
            if document_type == _RECEIPT_DOCUMENT_TYPE
            else None
        )
        normal_capture_key = uc03_booking_capture._PROPOSAL_CAPTURE_MAP.get(
            proposal["field_key"]
        )
        capture_key = receipt_capture_key or normal_capture_key
        if capture_key is None:
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Proposal requires configured master resolution",
                detail=(
                    "This extracted field cannot be accepted until its "
                    "typed-domain/master mapping is configured."
                ),
            )

        machine_value = proposal["proposed_value"]
        if isinstance(machine_value, dict) and "value" in machine_value:
            machine_value = machine_value["value"]
        accepted_value = payload.acceptedValue if corrected else machine_value
        if corrected and payload.acceptedValue is None:
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Business validation failed",
                detail="A corrected proposal requires acceptedValue.",
            )

        if receipt_capture_key is not None:
            domain, record_reference = _write_receipt_capture(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                capture_key=receipt_capture_key,
                value=accepted_value,
                source_evidence_id=proposal["source_evidence_id"],
            )
        else:
            domain, record_reference = uc03_booking_capture._write_typed_capture(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                field_key=capture_key,
                value=accepted_value,
                source_evidence_id=proposal["source_evidence_id"],
            )

        applicability_changes = uc03_booking_capture._resolve_booking_applicability(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )
        status_value = "CORRECTED" if corrected else "ACCEPTED"
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_capture_proposals
                SET proposal_status=:status,
                    accepted_value=CAST(:accepted_value AS jsonb),
                    accepted_by_actor_id=:actor_id,
                    accepted_by_role=:actor_role,
                    accepted_at_utc=now(),
                    owning_domain_key=:domain,
                    owning_record_reference=:record_reference,
                    version_no=version_no+1,
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND capture_proposal_id=:proposal_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "proposal_id": proposal_id,
                "status": status_value,
                "accepted_value": json.dumps({"value": accepted_value}, default=str),
                "actor_id": human_principal.subject,
                "actor_role": context["operating_role"],
                "domain": domain,
                "record_reference": record_reference,
            },
        )

        next_version = int(state["version_no"]) + 1
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET business_status='BOOKING_IN_PROGRESS',
                    audit_state=CASE
                        WHEN audit_state='NOT_STARTED' THEN 'IN_PROGRESS'
                        ELSE audit_state
                    END,
                    latest_activity_at_utc=now(), updated_at_utc=now(),
                    version_no=:version
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "version": next_version},
        )
        event_id = uc03_booking_capture._append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type=(
                "EXTRACTION_PROPOSAL_CORRECTED"
                if corrected
                else "EXTRACTION_PROPOSAL_ACCEPTED"
            ),
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={
                "proposalId": str(proposal_id),
                "fieldKey": proposal["field_key"],
                "sourceEvidenceId": str(proposal["source_evidence_id"]),
                "owningDomainKey": domain,
                "machineValuePreserved": True,
                "applicabilityChanges": applicability_changes,
            },
            aggregate_version=next_version,
        )
        return {
            "journeyId": str(journey_id),
            "proposalId": str(proposal_id),
            "fieldKey": proposal["field_key"],
            "status": status_value,
            "proposedValue": machine_value,
            "acceptedValue": accepted_value,
            "owningDomainKey": domain,
            "owningRecordReference": record_reference,
            "aggregateVersion": next_version,
            "eventId": str(event_id),
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=(
            f"uc03.booking.proposal.{'correct' if corrected else 'accept'}:{proposal_id}"
        ),
        idempotency_key=idempotency_key,
        request_payload={
            "expectedVersion": expected_version,
            "proposalId": str(proposal_id),
            "payload": payload.model_dump(mode="json"),
        },
        execute=execute,
    )
    uc03_booking_capture._set_etag(response, body)
    return body


def _proposals(connection: Connection, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    assert _original_proposals is not None
    result = _original_proposals(connection, tenant_id, journey_id)
    for proposal in result:
        if (
            str(proposal.get("sourceDocumentTypeKey") or "").strip().lower()
            == _RECEIPT_DOCUMENT_TYPE
            and proposal.get("fieldKey") in _RECEIPT_CAPTURE_MAP
        ):
            proposal["canAccept"] = True
            if proposal.get("status") == "PENDING":
                proposal["owningDomainKey"] = "PAYMENT"
    return result


def _completion_summary(
    connection: Connection,
    tenant_id: str,
    journey_id: UUID,
) -> dict[str, Any]:
    assert _original_completion_summary is not None
    summary = _original_completion_summary(connection, tenant_id, journey_id)
    acceptable_normal = list(uc03_booking_capture._PROPOSAL_CAPTURE_MAP)
    acceptable_receipt = list(_RECEIPT_CAPTURE_MAP)
    pending = connection.execute(
        text(
            """
            SELECT count(*)
            FROM auditcore.journey_capture_proposals
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING' AND proposal_status='PENDING'
              AND (
                    (
                        lower(COALESCE(source_document_type_key,''))='dealer_receipt'
                        AND field_key = ANY(:receipt_fields)
                    )
                    OR
                    (
                        lower(COALESCE(source_document_type_key,''))<>'dealer_receipt'
                        AND field_key = ANY(:normal_fields)
                    )
              )
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "receipt_fields": acceptable_receipt,
            "normal_fields": acceptable_normal,
        },
    ).scalar_one()
    blockers = [
        blocker
        for blocker in summary["blockers"]
        if blocker.get("code") != "EXTRACTION_PROPOSALS_PENDING"
    ]
    if pending:
        blockers.append(
            {
                "code": "EXTRACTION_PROPOSALS_PENDING",
                "label": f"Review {pending} extraction proposal(s)",
            }
        )
    summary["blockers"] = blockers
    summary["pendingProposalCount"] = int(pending)
    summary["ready"] = not blockers
    return summary


def install_uc03_booking_receipt_capture() -> None:
    """Enable Dealer Receipt facts in the existing generic PC review flow."""
    global _installed, _original_decide_proposal, _original_proposals
    global _original_completion_summary
    if _installed:
        return

    _original_decide_proposal = uc03_booking_capture._decide_proposal
    _original_proposals = uc03_booking_capture._proposals
    _original_completion_summary = uc03_booking_capture._completion_summary

    uc03_booking_capture._SUPPORTED_PROPOSAL_FIELDS[_RECEIPT_DOCUMENT_TYPE] = set(
        _RECEIPT_CAPTURE_MAP
    )
    uc03_booking_capture._decide_proposal = _decide_proposal
    uc03_booking_capture._proposals = _proposals
    uc03_booking_capture._completion_summary = _completion_summary
    _installed = True
