from __future__ import annotations

from uuid import uuid4

from audit_core.uc03_requirement_satisfaction import (
    RequirementSatisfaction,
    first_linked_document_ids,
    resolve_requirement_satisfaction,
    unresolved_completion_blockers,
)


def _requirement(
    key: str,
    *,
    level: str = "REQUIRED",
    status: str = "PENDING",
    doc_type: str | None = None,
    requirement_ref=None,
    is_extension: bool = False,
) -> dict:
    return {
        "requirement_ref": None if is_extension else (requirement_ref or uuid4()),
        "requirement_key": key,
        "requirement_level": level,
        "requirement_status": status,
        "display_label": key.replace("_", " ").title(),
        "document_type_key": doc_type or key,
        "condition_key": None,
    }


def _document(
    requirement_key: str | None,
    *,
    di_document_id=None,
    capture_status: str = "CLASSIFIED",
    created_at_utc: str = "2026-09-24T10:00:00Z",
) -> dict:
    return {
        "di_document_id": di_document_id or uuid4(),
        "client_upload_id": "client-1",
        "requirement_key": requirement_key,
        "classified_document_type_key": requirement_key,
        "capture_status": capture_status,
        "original_filename": "doc.pdf",
        "content_type": "application/pdf",
        "created_at_utc": created_at_utc,
    }


def test_satisfied_when_a_classified_document_is_linked() -> None:
    result = resolve_requirement_satisfaction(
        object(),
        tenant_id="tenant-a",
        journey_id=uuid4(),
        stage_code="BOOKING",
        requirements=[_requirement("booking_form")],
        documents=[_document("booking_form")],
    )
    assert result["booking_form"].satisfied is True
    assert result["booking_form"].reason == "SATISFIED"
    assert result["booking_form"].active_document is not None


def test_no_document_reason_when_nothing_uploaded() -> None:
    result = resolve_requirement_satisfaction(
        object(),
        tenant_id="tenant-a",
        journey_id=uuid4(),
        stage_code="BOOKING",
        requirements=[_requirement("customer_kyc")],
        documents=[],
    )
    assert result["customer_kyc"].satisfied is False
    assert result["customer_kyc"].reason == "NO_DOCUMENT"


def test_not_yet_classified_reason_when_document_uploaded_but_unclassified() -> None:
    result = resolve_requirement_satisfaction(
        object(),
        tenant_id="tenant-a",
        journey_id=uuid4(),
        stage_code="BOOKING",
        requirements=[_requirement("customer_kyc")],
        documents=[_document("customer_kyc", capture_status="RECEIVING")],
    )
    assert result["customer_kyc"].satisfied is False
    assert result["customer_kyc"].reason == "NOT_YET_CLASSIFIED"


def test_not_applicable_requirements_are_always_satisfied() -> None:
    result = resolve_requirement_satisfaction(
        object(),
        tenant_id="tenant-a",
        journey_id=uuid4(),
        stage_code="DELIVERY",
        requirements=[_requirement("gst_certificate", status="NOT_APPLICABLE")],
        documents=[],
    )
    assert result["gst_certificate"].satisfied is True
    assert result["gst_certificate"].reason == "NOT_APPLICABLE"


def test_first_classified_document_by_created_at_wins_the_slot() -> None:
    # Confirmed live: the exact rule already proven in
    # _build_capture_response/_build_delivery_capture_response (ordered
    # query + dict.setdefault) -- must not be re-derived differently here.
    first_id = uuid4()
    second_id = uuid4()
    result = resolve_requirement_satisfaction(
        object(),
        tenant_id="tenant-a",
        journey_id=uuid4(),
        stage_code="BOOKING",
        requirements=[_requirement("booking_form")],
        documents=[
            _document("booking_form", di_document_id=first_id, created_at_utc="2026-09-24T10:00:00Z"),
            _document("booking_form", di_document_id=second_id, created_at_utc="2026-09-24T10:05:00Z"),
        ],
    )
    assert result["booking_form"].active_document_id == first_id


def test_first_linked_document_wins_the_slot_even_if_still_unclassified() -> None:
    # Distinct from satisfaction's own "active document" rule: a single
    # not-yet-classified upload still owns its slot, it just isn't
    # satisfied yet -- must not be nulled just because it's not CLASSIFIED.
    doc_id = uuid4()
    result = first_linked_document_ids(
        [_document("booking_form", di_document_id=doc_id, capture_status="RECEIVING")],
        is_repeatable=lambda key: False,
    )
    assert result == {"booking_form": doc_id}


def test_first_linked_document_ids_ignores_repeatable_requirements() -> None:
    result = first_linked_document_ids(
        [_document("minimum_booking_payment_proof"), _document("minimum_booking_payment_proof")],
        is_repeatable=lambda key: True,
    )
    assert result == {}


def test_first_linked_document_ids_keeps_the_earliest_by_iteration_order() -> None:
    first_id, second_id = uuid4(), uuid4()
    result = first_linked_document_ids(
        [_document("booking_form", di_document_id=first_id), _document("booking_form", di_document_id=second_id)],
        is_repeatable=lambda key: False,
    )
    assert result == {"booking_form": first_id}


def test_requirement_with_a_document_present_but_wrong_slot_does_not_satisfy_it() -> None:
    result = resolve_requirement_satisfaction(
        object(),
        tenant_id="tenant-a",
        journey_id=uuid4(),
        stage_code="BOOKING",
        requirements=[_requirement("customer_kyc"), _requirement("booking_form")],
        documents=[_document("booking_form")],
    )
    assert result["booking_form"].satisfied is True
    assert result["customer_kyc"].satisfied is False
    assert result["customer_kyc"].reason == "NO_DOCUMENT"


def test_extension_requirement_is_marked_is_extension() -> None:
    # requirement_ref is NULL for a document_capture_v2_requirement_policy
    # "extension" row (corporate_id today) -- never materialized into
    # journey_document_requirements, so applicability can never resolve
    # for it the way a catalog-backed CONDITIONAL requirement's does.
    result = resolve_requirement_satisfaction(
        object(),
        tenant_id="tenant-a",
        journey_id=uuid4(),
        stage_code="DELIVERY",
        requirements=[_requirement("corporate_id", level="CONDITIONAL", is_extension=True)],
        documents=[],
    )
    assert result["corporate_id"].is_extension is True
    assert result["corporate_id"].satisfied is False


def test_catalog_backed_requirement_is_not_marked_is_extension() -> None:
    result = resolve_requirement_satisfaction(
        object(),
        tenant_id="tenant-a",
        journey_id=uuid4(),
        stage_code="BOOKING",
        requirements=[_requirement("booking_form")],
        documents=[_document("booking_form")],
    )
    assert result["booking_form"].is_extension is False


def _satisfaction(**overrides) -> RequirementSatisfaction:
    defaults = {
        "requirement_key": "booking_form",
        "stage_code": "BOOKING",
        "requirement_level": "REQUIRED",
        "display_label": "Booking Form",
        "document_type_key": "booking_form",
        "condition_key": None,
        "satisfied": False,
        "reason": "NO_DOCUMENT",
        "active_document_id": None,
        "active_document": None,
        "is_extension": False,
    }
    defaults.update(overrides)
    return RequirementSatisfaction(**defaults)


def test_unresolved_completion_blockers_includes_unsatisfied_required() -> None:
    blockers = unresolved_completion_blockers({"booking_form": _satisfaction()})
    assert [b.requirement_key for b in blockers] == ["booking_form"]


def test_unresolved_completion_blockers_includes_unsatisfied_conditional() -> None:
    blockers = unresolved_completion_blockers(
        {"gst_certificate": _satisfaction(requirement_key="gst_certificate", requirement_level="CONDITIONAL")}
    )
    assert [b.requirement_key for b in blockers] == ["gst_certificate"]


def test_unresolved_completion_blockers_excludes_optional() -> None:
    blockers = unresolved_completion_blockers(
        {"pan_card": _satisfaction(requirement_key="pan_card", requirement_level="OPTIONAL")}
    )
    assert blockers == []


def test_unresolved_completion_blockers_excludes_satisfied() -> None:
    blockers = unresolved_completion_blockers(
        {"booking_form": _satisfaction(satisfied=True, reason="SATISFIED")}
    )
    assert blockers == []


def test_unresolved_completion_blockers_excludes_extension_even_if_conditional_and_unsatisfied() -> None:
    # The exact landmine this exists to avoid: corporate_id is CONDITIONAL
    # and, for the overwhelming majority of non-corporate journeys, will
    # never have a document -- without this exclusion it would block every
    # such Delivery's completion forever.
    blockers = unresolved_completion_blockers(
        {
            "corporate_id": _satisfaction(
                requirement_key="corporate_id", requirement_level="CONDITIONAL", is_extension=True,
            )
        }
    )
    assert blockers == []
