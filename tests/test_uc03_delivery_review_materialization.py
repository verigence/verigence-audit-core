from __future__ import annotations

import inspect
from uuid import uuid4

from audit_core import uc03_delivery_review_materialization as materialization
from audit_core.uc03_document_review_v2 import ReviewV2Document, ReviewV2Field
from audit_core.uc03_review_effective_values import (
    confirm_delivery_review_v2_effective_values,
)


def _field(field_key: str, value, *, confidence: float = 99.0) -> ReviewV2Field:
    return ReviewV2Field(
        canonicalFieldId=str(uuid4()),
        fieldKey=field_key,
        value=value,
        confidenceScore=confidence,
        sourceFactVersion=1,
        reviewState="READY",
    )


def _document(document_type: str, fields: list[ReviewV2Field]) -> ReviewV2Document:
    document_id = uuid4()
    return ReviewV2Document(
        documentId=document_id,
        label=document_type,
        documentTypeKey=document_type,
        originalFilename=f"{document_id}.pdf",
        processingStatus="PROCESSED",
        extractionState="READY",
        fields=fields,
    )


def test_delivery_order_contract_materializes_exact_chassis_field_only() -> None:
    document = _document(
        "delivery_order_cover",
        [
            _field("chassis", "SHOULD-NOT-MATCH", confidence=100.0),
            _field("chassis_no", "MA1ABC123", confidence=95.0),
        ],
    )

    selected = materialization._best_field(
        [document],
        materialization._CHASSIS_FIELD_KEYS,
        source_priority=materialization._VEHICLE_SOURCE_PRIORITY,
    )

    assert selected is not None
    assert selected[1].fieldKey == "chassis_no"
    assert selected[1].value == "MA1ABC123"


def test_invoice_source_precedes_delivery_order_for_vehicle_identifier() -> None:
    delivery_order = _document(
        "delivery_order_cover",
        [_field("chassis_no", "DO-CHASSIS", confidence=99.0)],
    )
    invoice = _document(
        "customer_invoice_dms",
        [_field("chassis_number", "INV-CHASSIS", confidence=93.0)],
    )

    selected = materialization._best_field(
        [delivery_order, invoice],
        materialization._CHASSIS_FIELD_KEYS,
        source_priority=materialization._VEHICLE_SOURCE_PRIORITY,
    )

    assert selected is not None
    assert selected[0].documentTypeKey == "customer_invoice_dms"
    assert selected[1].value == "INV-CHASSIS"


def test_rto_challan_registration_fields_mapping() -> None:
    # Confirmed live gap: DI extracts registration_state/territory/district/
    # type from an RTO Challan fine (rto_challan.py), but only
    # registration_number was ever wired into registration_records, leaving
    # a PC to type State/District in by hand.
    assert materialization._RTO_CHALLAN_DOCUMENT_TYPE == "rto_challan"
    assert materialization._RTO_CHALLAN_FIELDS == {
        "registration_state": "registration_state",
        "registration_territory": "registration_territory",
        "registration_district": "registration_district",
        "registration_type": "registration_type_code",
    }


def test_rto_challan_fields_only_selected_from_an_actual_rto_challan() -> None:
    challan = _document(
        "rto_challan",
        [
            _field("registration_state", "Odisha"),
            _field("registration_district", "Cuttack"),
        ],
    )
    other = _document(
        "customer_invoice_dms",
        [_field("registration_state", "SHOULD-NOT-MATCH")],
    )

    state = materialization._best_field(
        [other, challan], ("registration_state",), document_types={"rto_challan"},
    )
    assert state is not None
    assert state[0].documentTypeKey == "rto_challan"
    assert state[1].value == "Odisha"

    district = materialization._best_field(
        [challan], ("registration_district",), document_types={"rto_challan"},
    )
    assert district is not None
    assert district[1].value == "Cuttack"


def test_delivery_insurance_mapping_matches_di_insurance_cover_contract() -> None:
    assert materialization._INSURANCE_DOCUMENT_TYPE == "insurance_cover"
    assert materialization._INSURANCE_FIELDS == {
        "insurer_name": "insurer_name",
        "policy_number": "policy_reference",
        "premium_amount": "actual_premium_amount",
        "agent_intermediary_name": "agent_intermediary_name",
        "agent_intermediary_code": "agent_intermediary_code",
        "misp_code": "misp_code",
    }
    assert materialization._REGISTRATION_FIELD_KEYS == (
        "registration_number",
        "insured_vehicle_reg",
    )


def test_insurance_addons_are_selected_from_the_di_add_ons_array_field() -> None:
    # add_ons is a DI array field (zero dep, engine protect, etc.) kept out
    # of the per-column text merge and handled as its own JSON column.
    document = _document(
        "insurance_cover",
        [_field("add_ons", ["zero_depreciation", "engine_protection"])],
    )
    selected = materialization._best_field(
        [document], ("add_ons",), document_types={"insurance_cover"},
    )
    assert selected is not None
    assert selected[1].value == ["zero_depreciation", "engine_protection"]


def test_delivery_business_materializer_calls_all_canonical_projections(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        materialization,
        "materialize_delivery_vehicle",
        lambda *args, **kwargs: calls.append("vehicle") or 2,
    )
    monkeypatch.setattr(
        materialization,
        "materialize_delivery_registration",
        lambda *args, **kwargs: calls.append("registration") or 1,
    )
    monkeypatch.setattr(
        materialization,
        "materialize_delivery_insurance",
        lambda *args, **kwargs: calls.append("insurance") or 3,
    )
    monkeypatch.setattr(
        materialization,
        "materialize_delivery_commercial_lines",
        lambda *args, **kwargs: calls.append("commercials") or 4,
    )
    monkeypatch.setattr(
        materialization,
        "materialize_delivery_receipts",
        lambda *args, **kwargs: calls.append("receipts")
        or {
            "reviewRowsWritten": 1,
            "created": 1,
            "updated": 0,
            "unchanged": 0,
            "skippedWithoutAmount": 0,
        },
    )

    result = materialization.materialize_reviewed_delivery_business_values(
        object(),
        tenant_id="tenant-a",
        journey_id=uuid4(),
        documents=[],
        actor_id="reviewer-a",
    )

    assert calls == ["vehicle", "registration", "insurance", "commercials", "receipts"]
    assert result["vehicleFields"] == 2
    assert result["registrationFields"] == 1
    assert result["insuranceFields"] == 3
    assert result["commercialLines"] == 4
    assert result["receiptPaymentsCreated"] == 1


def test_delivery_review_materializes_before_marking_stage_verified() -> None:
    # confirm_delivery_review_v2_effective_values is the live handler for
    # POST /delivery/review/confirm (confirm_delivery_review_v2, previously
    # tested here, was shadowed dead code -- see
    # test_uc03_delivery_review_confirm.py). Canonical materialization also
    # runs async per document as DI confirms it (durable-state-driven, safe
    # to call redundantly); this synchronous call is confirm's own safety
    # net for a correction applied at confirm time, matching Booking's.
    source = inspect.getsource(confirm_delivery_review_v2_effective_values)
    materialize_at = source.index("materialize_reviewed_delivery_business_values(")
    verified_at = source.index("pc_verification_status='VERIFIED'")

    assert materialize_at < verified_at
