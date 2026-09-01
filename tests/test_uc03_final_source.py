from __future__ import annotations

import inspect
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute

from audit_core.errors import ConflictError
from audit_core.uc03_final_source import (
    FinalSourceConfirmResponse,
    FinalSourceStatusResponse,
    _choose_candidate,
    _require_verified_reviews,
    confirm_final_source,
    router,
)
from audit_core.uc03_final_source_policy import ReviewedSourcePolicy


def _policy() -> ReviewedSourcePolicy:
    return ReviewedSourcePolicy(
        attribute_key="customer_name",
        report_field="Customer Name",
        business_source_label="Customer KYC (PAN, Aadhaar, address proof)",
        technical_pairs=(("pan_card", "pan_name"), ("aadhaar", "aadhaar_name")),
        resolution_rule="FINAL_REPORT_CUSTOMER_KYC_UNANIMOUS",
    )


def _candidate(*, value, stage="BOOKING", document_type="pan_card", field="pan_name"):
    return {
        "extracted_field_id": uuid4(),
        "stage_code": stage,
        "di_document_id": uuid4(),
        "source_document_type_key": document_type,
        "source_canonical_field_id": str(uuid4()),
        "field_key": field,
        "source_fact_version": 1,
        "effective_value": value,
    }


def test_final_source_routes_are_additive_get_and_post() -> None:
    status_routes = [
        route
        for route in router.routes
        if isinstance(route, APIRoute) and route.path.endswith("/audit/final-source")
    ]
    confirm_routes = [
        route
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.path.endswith("/audit/final-source/confirm")
    ]

    assert len(status_routes) == 1
    assert status_routes[0].methods == {"GET"}
    assert status_routes[0].response_model is FinalSourceStatusResponse
    assert len(confirm_routes) == 1
    assert confirm_routes[0].methods == {"POST"}
    assert confirm_routes[0].response_model is FinalSourceConfirmResponse


def test_final_source_requires_both_reviews_verified() -> None:
    complete = datetime.now(UTC)
    with pytest.raises(ConflictError):
        _require_verified_reviews(
            {
                "BOOKING": {
                    "capture_completed_at_utc": complete,
                    "pc_verification_status": "PENDING",
                    "version_no": 2,
                },
                "DELIVERY": {
                    "capture_completed_at_utc": complete,
                    "pc_verification_status": "VERIFIED",
                    "version_no": 4,
                },
            }
        )

    with pytest.raises(ConflictError):
        _require_verified_reviews(
            {
                "BOOKING": {
                    "capture_completed_at_utc": complete,
                    "pc_verification_status": "VERIFIED",
                    "version_no": 2,
                },
                "DELIVERY": {
                    "capture_completed_at_utc": complete,
                    "pc_verification_status": "PENDING",
                    "version_no": 4,
                },
            }
        )

    assert _require_verified_reviews(
        {
            "BOOKING": {
                "capture_completed_at_utc": complete,
                "pc_verification_status": "VERIFIED",
                "version_no": 2,
            },
            "DELIVERY": {
                "capture_completed_at_utc": complete,
                "pc_verification_status": "VERIFIED",
                "version_no": 4,
            },
        }
    ) == (2, 4)


def test_disagreeing_legitimate_sources_fail_closed() -> None:
    with pytest.raises(ConflictError):
        _choose_candidate(
            _policy(),
            [
                _candidate(value="Customer A", document_type="pan_card", field="pan_name"),
                _candidate(
                    value="Customer B",
                    stage="DELIVERY",
                    document_type="aadhaar",
                    field="aadhaar_name",
                ),
            ],
        )


def test_agreeing_sources_use_deterministic_provenance_not_business_precedence() -> None:
    first = _candidate(value="Customer A", stage="BOOKING")
    second = _candidate(
        value="Customer A",
        stage="DELIVERY",
        document_type="aadhaar",
        field="aadhaar_name",
    )

    chosen = _choose_candidate(_policy(), [second, first])

    assert chosen is first


def test_confirm_fails_mapping_closed_before_idempotent_or_resolution_writes() -> None:
    source = inspect.getsource(confirm_final_source)

    mapping_guard = source.index("if UNRESOLVED_TECHNICAL_POLICIES:")
    idempotent_call = source.index("execute_idempotent_json_command(")
    persistence_call = source.index("record_post_delivery_reviewed_resolution(")

    assert mapping_guard < idempotent_call
    assert mapping_guard < persistence_call
    assert "_mapping_blocked_error()" in source


def test_confirm_preflights_all_sources_before_first_resolution_insert() -> None:
    source = inspect.getsource(confirm_final_source)

    assert source.index("_preflight_final_sources(") < source.index(
        "record_post_delivery_reviewed_resolution("
    )
    assert "_aggregate_lock(" in source
    assert "_require_verified_reviews(states)" in source
    assert "expected_delivery_version = _parse_if_match(if_match)" in source
    assert "execute_idempotent_json_command(" in source
    assert "_existing_finalization(" in source


def test_final_source_command_uses_durable_core_only_not_di() -> None:
    source = inspect.getsource(confirm_final_source)
    module_source = inspect.getsource(inspect.getmodule(confirm_final_source))

    assert "get_di_client" not in module_source
    assert "DiClient" not in module_source
    assert "journey_document_extracted_fields" in module_source
    assert "effective_value IS NOT NULL" in module_source
    assert "source_fact_version DESC" in module_source
    assert "confidence" not in source.casefold()
