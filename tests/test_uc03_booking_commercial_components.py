from uuid import uuid4

from audit_core import uc03_attribute_mapping as attribute_mapping
from audit_core import uc03_v2_review_materialization as materialization
from audit_core.uc03_booking_commercial_components import (
    _BOOKING_COMMERCIAL_COMPONENT_FIELDS,
    install_uc03_booking_commercial_components,
)


def test_every_detailed_booking_commercial_field_has_existing_core_owner() -> None:
    install_uc03_booking_commercial_components()
    document_id = uuid4()

    for field_key in _BOOKING_COMMERCIAL_COMPONENT_FIELDS:
        assert field_key in materialization._BOOKING_FORM_FIELDS
        assert field_key in materialization._BOOKING_DECIMAL_FIELDS
        assert field_key in materialization._COMMERCIAL_LINE_FIELDS
        assert materialization.reviewed_field_core_owner(
            document_type_key="booking_form",
            field_key=field_key,
            document_id=document_id,
        ) == ("BOOKING_FORM_REVIEW_VALUE", str(document_id))


def test_every_detailed_booking_commercial_field_is_a_review_attribute() -> None:
    install_uc03_booking_commercial_components()

    for field_key in _BOOKING_COMMERCIAL_COMPONENT_FIELDS:
        spec = attribute_mapping.spec_for_field(field_key)
        assert spec is not None
        assert "BOOKING" in spec.stages
        assert field_key in spec.field_keys


def test_existing_identity_contract_remains_owned() -> None:
    install_uc03_booking_commercial_components()
    document_id = uuid4()

    for field_key in materialization._PAN_FIELDS:
        assert materialization.reviewed_field_core_owner(
            document_type_key="pan",
            field_key=field_key,
            document_id=document_id,
        ) is not None
    for field_key in materialization._AADHAAR_FIELDS:
        assert materialization.reviewed_field_core_owner(
            document_type_key="aadhaar",
            field_key=field_key,
            document_id=document_id,
        ) is not None
