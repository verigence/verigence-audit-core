from types import SimpleNamespace
from uuid import uuid4

from audit_core import uc03_v2_review_materialization as materialization
from audit_core.uc03_aadhaar_address_core_ownership import (
    _AADHAAR_ADDRESS_COLUMN_BY_FIELD,
    install_uc03_aadhaar_address_core_ownership,
)
from audit_core.uc03_attribute_mapping import spec_for_field


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


def test_aadhaar_address_fields_are_canonical_review_attributes_for_both_stages() -> None:
    install_uc03_aadhaar_address_core_ownership()

    expected = {
        "address_pincode": ("aadhaar_address_pincode", "Address Pincode"),
        "address_state": ("aadhaar_address_state", "Address State"),
        "address_district": ("aadhaar_address_district", "Address District"),
    }
    for field_key, (attribute_key, label) in expected.items():
        spec = spec_for_field(field_key)
        assert spec is not None
        assert spec.attribute_key == attribute_key
        assert spec.label == label
        assert spec.stages == ("BOOKING", "DELIVERY")
        assert spec.source_priority == ("aadhaar",)
        assert spec.operational_field is None


def test_aadhaar_address_columns_are_source_specific_core_fields() -> None:
    assert _AADHAAR_ADDRESS_COLUMN_BY_FIELD == {
        "address_pincode": "aadhaar_address_pincode",
        "address_state": "aadhaar_address_state",
        "address_district": "aadhaar_address_district",
    }


def test_aadhaar_address_values_use_one_atomic_identity_upsert(monkeypatch) -> None:
    install_uc03_aadhaar_address_core_ownership()
    customer_id = uuid4()
    journey_id = uuid4()
    document_id = uuid4()
    evidence_id = uuid4()
    captured: list[dict] = []

    monkeypatch.setattr(
        materialization,
        "_journey_customer_id",
        lambda connection, *, tenant_id, journey_id: customer_id,
    )

    def capture_upsert(connection, **kwargs):
        captured.append(kwargs)
        return str(uuid4())

    monkeypatch.setattr(materialization, "_upsert_review_value_row", capture_upsert)

    document = SimpleNamespace(
        documentTypeKey="aadhaar",
        extractionState="READY",
        documentId=document_id,
        evidenceId=evidence_id,
        fields=[
            SimpleNamespace(fieldKey="aadhaar_number", value="123412341234"),
            SimpleNamespace(fieldKey="aadhaar_name", value="Test Customer"),
            SimpleNamespace(fieldKey="address_pincode", value="754112"),
            SimpleNamespace(fieldKey="address_state", value="Odisha"),
            SimpleNamespace(fieldKey="address_district", value="Cuttack"),
        ],
    )

    written = materialization.materialize_reviewed_identity_values(
        object(),
        tenant_id="tenant-test",
        journey_id=journey_id,
        documents=[document],
        rejected_review_keys=set(),
        actor_id="pc-test",
    )

    assert written == 1
    assert len(captured) == 1
    upsert = captured[0]
    assert upsert["table_name"] == "customer_identity_review_values"
    assert upsert["document_id"] == document_id
    assert upsert["evidence_id"] == evidence_id
    assert upsert["extra_insert_columns"] == {
        "customer_id": customer_id,
        "document_type_key": "AADHAAR",
    }
    assert upsert["values"]["aadhaar_address_pincode"] == "754112"
    assert upsert["values"]["aadhaar_address_state"] == "Odisha"
    assert upsert["values"]["aadhaar_address_district"] == "Cuttack"
    assert "aadhaar_address_pincode" in upsert["columns"]
    assert "aadhaar_address_state" in upsert["columns"]
    assert "aadhaar_address_district" in upsert["columns"]
