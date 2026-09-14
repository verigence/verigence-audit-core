from __future__ import annotations

from uuid import uuid4

from audit_core.uc03_journey_reviewed_details import (
    annotate_and_resolve_reviewed_fields,
    business_category,
    semantic_key,
)


def _row(
    *,
    field_key: str,
    value,
    document_type: str,
    stage: str,
    has_effective: bool = True,
    version: int = 1,
):
    return {
        "reviewedFieldId": uuid4(),
        "documentId": uuid4(),
        "evidenceId": None,
        "stageCode": stage,
        "documentTypeKey": document_type,
        "requirementKey": document_type,
        "originalFilename": f"{document_type}.pdf",
        "canonicalFieldId": str(uuid4()),
        "fieldKey": field_key,
        "extractedValue": value,
        "modifiedValue": None,
        "effectiveValue": value if has_effective else None,
        "hasEffectiveValue": has_effective,
        "isModified": False,
        "confidenceScore": 98.0,
        "confidenceScale": "PERCENT",
        "sourceFactVersion": version,
        "reviewedByActorId": "reviewer-1",
        "reviewedAtUtc": None,
    }


def test_pan_beats_booking_for_customer_name_and_all_sources_remain_visible() -> None:
    rows = [
        _row(
            field_key="customer_name",
            value="Booking Customer",
            document_type="booking_form",
            stage="BOOKING",
        ),
        _row(
            field_key="pan_name",
            value="Legal Customer",
            document_type="pan_card",
            stage="BOOKING",
        ),
    ]

    annotated, resolved = annotate_and_resolve_reviewed_fields(rows)

    assert len(annotated) == 2
    assert resolved["customer_name"]["value"] == "Legal Customer"
    assert resolved["customer_name"]["precedenceReason"] == "CUSTOMER_KYC_SOURCE_OF_TRUTH"
    assert sum(1 for item in annotated if item["isPreferred"]) == 1


def test_pan_is_deterministic_tie_break_over_aadhaar_for_shared_legal_name() -> None:
    rows = [
        _row(
            field_key="aadhaar_name",
            value="Aadhaar Name",
            document_type="aadhaar",
            stage="DELIVERY",
        ),
        _row(
            field_key="pan_name",
            value="PAN Name",
            document_type="pan_card",
            stage="BOOKING",
        ),
    ]

    _, resolved = annotate_and_resolve_reviewed_fields(rows)

    assert resolved["customer_name"]["value"] == "PAN Name"
    assert resolved["customer_name"]["documentTypeKey"] == "pan_card"


def test_aadhaar_address_is_customer_truth() -> None:
    rows = [
        _row(
            field_key="customer_address",
            value="Booking Address",
            document_type="booking_form",
            stage="BOOKING",
        ),
        _row(
            field_key="aadhaar_address",
            value="KYC Address",
            document_type="aadhaar",
            stage="BOOKING",
        ),
    ]

    annotated, resolved = annotate_and_resolve_reviewed_fields(rows)

    assert resolved["customer_address"]["value"] == "KYC Address"
    assert next(item for item in annotated if item["fieldKey"] == "aadhaar_address")["businessCategory"] == "CUSTOMER"


def test_delivery_wins_over_booking_for_overlapping_vehicle_fact() -> None:
    rows = [
        _row(
            field_key="vehicle_model",
            value="Booking Model",
            document_type="booking_form",
            stage="BOOKING",
        ),
        _row(
            field_key="model_name_raw",
            value="Delivered Model",
            document_type="customer_invoice_dms",
            stage="DELIVERY",
        ),
    ]

    _, resolved = annotate_and_resolve_reviewed_fields(rows)

    assert resolved["model"]["value"] == "Delivered Model"
    assert resolved["model"]["stageCode"] == "DELIVERY"
    assert resolved["model"]["precedenceReason"] == "DELIVERY_OVER_BOOKING"


def test_pan_father_name_fills_relationship_when_generic_pan_fields_are_empty() -> None:
    """A real PAN card prints Father's Name as its own labelled field, with
    no S/O prefix -- unlike Aadhaar. Reported live: a PAN-only customer
    showed 'Relationship' and 'Relationship Name' as empty on Journey 360
    despite the father's name being extracted correctly, because DI never
    populates pan_relationship_type/_name from a PAN card."""
    rows = [
        _row(
            field_key="pan_father_name",
            value="Father Name",
            document_type="pan_card",
            stage="BOOKING",
        ),
    ]

    _, resolved = annotate_and_resolve_reviewed_fields(rows)

    assert resolved["customer_relationship_type"]["value"] == "S/O"
    assert resolved["customer_relationship_name"]["value"] == "Father Name"


def test_pan_father_name_fills_relationship_even_when_generic_pan_fields_extracted_empty() -> None:
    """Live bug (journey 6c4d527f-...): DI can extract
    pan_relationship_type/_name and aadhaar_relationship_type/_name with
    hasEffectiveValue=True but an actual value of None -- has_effective can
    be set on a row DI never really filled in. A dict is always truthy, so
    checking mere key presence on `resolved` never let the pan_father_name
    fallback fire in this exact, real shape; the empty-valued candidate row
    already "occupied" customer_relationship_type/_name."""
    rows = [
        _row(
            field_key="pan_father_name",
            value="Father Name",
            document_type="pan_card",
            stage="BOOKING",
        ),
        _row(
            field_key="pan_relationship_type",
            value=None,
            document_type="pan_card",
            stage="BOOKING",
            has_effective=True,
        ),
        _row(
            field_key="pan_relationship_name",
            value=None,
            document_type="pan_card",
            stage="BOOKING",
            has_effective=True,
        ),
        _row(
            field_key="aadhaar_relationship_type",
            value=None,
            document_type="aadhaar",
            stage="BOOKING",
            has_effective=True,
        ),
        _row(
            field_key="aadhaar_relationship_name",
            value=None,
            document_type="aadhaar",
            stage="BOOKING",
            has_effective=True,
        ),
    ]

    _, resolved = annotate_and_resolve_reviewed_fields(rows)

    assert resolved["customer_relationship_type"]["value"] == "S/O"
    assert resolved["customer_relationship_name"]["value"] == "Father Name"


def test_pan_father_name_fallback_never_overrides_an_explicit_relationship() -> None:
    rows = [
        _row(
            field_key="pan_father_name",
            value="Father Name",
            document_type="pan_card",
            stage="BOOKING",
        ),
        _row(
            field_key="aadhaar_relationship_type",
            value="W/O",
            document_type="aadhaar",
            stage="BOOKING",
        ),
        _row(
            field_key="aadhaar_relationship_name",
            value="Husband Name",
            document_type="aadhaar",
            stage="BOOKING",
        ),
    ]

    _, resolved = annotate_and_resolve_reviewed_fields(rows)

    assert resolved["customer_relationship_type"]["value"] == "W/O"
    assert resolved["customer_relationship_name"]["value"] == "Husband Name"


def test_booking_is_current_source_when_delivery_has_no_same_semantic_fact() -> None:
    rows = [
        _row(
            field_key="vehicle_variant",
            value="Booking Variant",
            document_type="booking_form",
            stage="BOOKING",
        )
    ]

    _, resolved = annotate_and_resolve_reviewed_fields(rows)

    assert resolved["variant"]["value"] == "Booking Variant"
    assert resolved["variant"]["stageCode"] == "BOOKING"


def test_rejected_field_is_visible_but_never_becomes_preferred_value() -> None:
    rows = [
        _row(
            field_key="gstin",
            value="22AAAAA0000A1Z5",
            document_type="gst_certificate",
            stage="DELIVERY",
            has_effective=False,
        )
    ]

    annotated, resolved = annotate_and_resolve_reviewed_fields(rows)

    assert len(annotated) == 1
    assert annotated[0]["displayValue"] == "22AAAAA0000A1Z5"
    assert annotated[0]["isPreferred"] is False
    assert "gstin" not in resolved


def test_gst_corporate_warranty_and_accessory_sources_get_explicit_categories() -> None:
    assert business_category("gst_certificate", "gstin") == "GST"
    assert business_category("corporate_id", "employee_code") == "CORPORATE"
    assert business_category("ew_invoice", "plan_name") == "EXTENDED_WARRANTY"
    assert business_category("accessory_invoice_tally", "invoice_number") == "ACCESSORIES"


def test_semantic_aliases_are_exact_and_not_fuzzy() -> None:
    assert semantic_key("pan_name") == "customer_name"
    assert semantic_key("vin_number") == "vin"
    assert semantic_key("some_future_di_field") == "some_future_di_field"
