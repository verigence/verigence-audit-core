from uuid import uuid4

from audit_core import uc03_v2_review_materialization as materialization
from audit_core.uc03_aadhaar_address_core_ownership import (
    _AADHAAR_ADDRESS_COLUMN_BY_FIELD,
    install_uc03_aadhaar_address_core_ownership,
)


def test_aadhaar_v12_address_fields_have_typed_core_owner() -> None:
    install_uc03_aadhaar_address_core_ownership()
    document_id = uuid4()

    assert set(_AADHAAR_ADDRESS_COLUMN_BY_FIELD) == {
        "address_pincode",
        "address_state",
        "address_district",
    }
    for field_key in _AADHAAR_ADDRESS_COLUMN_BY_FIELD:
        assert field_key in materialization._AADHAAR_FIELDS
        assert materialization.reviewed_field_core_owner(
            document_type_key="aadhaar",
            field_key=field_key,
            document_id=document_id,
        ) == ("CUSTOMER_IDENTITY_REVIEW_VALUE", str(document_id))


def test_aadhaar_address_columns_are_source_specific_core_fields() -> None:
    assert _AADHAAR_ADDRESS_COLUMN_BY_FIELD == {
        "address_pincode": "aadhaar_address_pincode",
        "address_state": "aadhaar_address_state",
        "address_district": "aadhaar_address_district",
    }
