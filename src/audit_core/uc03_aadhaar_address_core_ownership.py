"""Give Aadhaar address components canonical Review mappings and typed Core owners.

DI Aadhaar v1.2 publishes ``address_pincode``, ``address_state`` and
``address_district`` only when they are explicitly identifiable on the supplied
document. These are reviewed business values, so Booking and Delivery Review must
recognise them as canonical Aadhaar attributes and Booking Review must materialize
the reviewed/effective values into ``customer_identity_review_values``.

No geography is derived here. The exact reviewed DI/effective text is preserved.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from audit_core import uc03_attribute_mapping as attribute_mapping
from audit_core import uc03_v2_review_materialization as materialization

_AADHAAR_ADDRESS_COLUMN_BY_FIELD = {
    "address_pincode": "aadhaar_address_pincode",
    "address_state": "aadhaar_address_state",
    "address_district": "aadhaar_address_district",
}

_AADHAAR_ADDRESS_ATTRIBUTE_SPECS = (
    attribute_mapping.AttributeSpec(
        attribute_key="aadhaar_address_pincode",
        excel_field_no=None,
        label="Address Pincode",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"address_pincode"}),
        source_priority=("aadhaar",),
    ),
    attribute_mapping.AttributeSpec(
        attribute_key="aadhaar_address_state",
        excel_field_no=None,
        label="Address State",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"address_state"}),
        source_priority=("aadhaar",),
    ),
    attribute_mapping.AttributeSpec(
        attribute_key="aadhaar_address_district",
        excel_field_no=None,
        label="Address District",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"address_district"}),
        source_priority=("aadhaar",),
    ),
)

_installed = False


def _install_attribute_specs() -> None:
    existing_field_keys = {
        field_key.casefold()
        for spec in attribute_mapping.ATTRIBUTE_SPECS
        for field_key in spec.field_keys
    }
    new_specs = tuple(
        spec
        for spec in _AADHAAR_ADDRESS_ATTRIBUTE_SPECS
        if not any(field.casefold() in existing_field_keys for field in spec.field_keys)
    )
    if not new_specs:
        return

    attribute_mapping.ATTRIBUTE_SPECS = (*attribute_mapping.ATTRIBUTE_SPECS, *new_specs)
    attribute_mapping._FIELD_INDEX = {
        field_key.casefold(): spec
        for spec in attribute_mapping.ATTRIBUTE_SPECS
        for field_key in spec.field_keys
    }


def install_uc03_aadhaar_address_core_ownership() -> None:
    """Install one coherent Aadhaar address Review + typed-persistence path."""

    global _installed
    if _installed:
        return

    _install_attribute_specs()
    materialization._AADHAAR_FIELDS = {
        *materialization._AADHAAR_FIELDS,
        *_AADHAAR_ADDRESS_COLUMN_BY_FIELD,
    }

    original: Callable[..., int] = materialization.materialize_reviewed_identity_values

    def materialize_reviewed_identity_values_with_address(
        connection,
        *,
        tenant_id: str,
        journey_id,
        documents: list[Any],
        rejected_review_keys: set[str],
        actor_id: str,
    ) -> int:
        # Preserve the established PAN path. Aadhaar is materialized below in one
        # atomic upsert so address components cannot depend on a second UPDATE.
        non_aadhaar_documents = [
            document
            for document in documents
            if str(document.documentTypeKey or "").strip().lower() != "aadhaar"
        ]
        written = 0
        if non_aadhaar_documents:
            written = original(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                documents=non_aadhaar_documents,
                rejected_review_keys=rejected_review_keys,
                actor_id=actor_id,
            )

        aadhaar_documents = [
            document
            for document in documents
            if str(document.documentTypeKey or "").strip().lower() == "aadhaar"
            and str(document.extractionState).upper() == "READY"
        ]
        if not aadhaar_documents:
            return written

        customer_id = materialization._journey_customer_id(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )
        columns = (
            "pan_number",
            "pan_name",
            "pan_father_name",
            "pan_relationship_type",
            "pan_relationship_name",
            "pan_date_of_birth",
            "aadhaar_number",
            "aadhaar_name",
            "aadhaar_date_of_birth",
            "aadhaar_gender",
            "aadhaar_address",
            "aadhaar_address_pincode",
            "aadhaar_address_state",
            "aadhaar_address_district",
            "aadhaar_relationship_type",
            "aadhaar_relationship_name",
        )

        for document in aadhaar_documents:
            source_values = materialization._normalized_document_values(
                document,
                allowed_fields=materialization._AADHAAR_FIELDS,
                rejected_review_keys=rejected_review_keys,
                date_fields={"date_of_birth"},
            )
            if not source_values:
                continue

            row_values = {
                "aadhaar_number": source_values.get("aadhaar_number"),
                "aadhaar_name": source_values.get("aadhaar_name"),
                "aadhaar_date_of_birth": source_values.get("date_of_birth"),
                "aadhaar_gender": source_values.get("gender"),
                "aadhaar_address": source_values.get("aadhaar_address"),
                "aadhaar_address_pincode": source_values.get("address_pincode"),
                "aadhaar_address_state": source_values.get("address_state"),
                "aadhaar_address_district": source_values.get("address_district"),
                "aadhaar_relationship_type": source_values.get(
                    "aadhaar_relationship_type"
                ),
                "aadhaar_relationship_name": source_values.get(
                    "aadhaar_relationship_name"
                ),
            }
            materialization._upsert_review_value_row(
                connection,
                table_name="customer_identity_review_values",
                id_column="customer_identity_review_value_id",
                tenant_id=tenant_id,
                journey_id=journey_id,
                document_id=document.documentId,
                evidence_id=document.evidenceId,
                actor_id=actor_id,
                columns=columns,
                values=row_values,
                extra_insert_columns={
                    "customer_id": customer_id,
                    "document_type_key": "AADHAAR",
                },
            )
            written += 1

        return written

    materialization.materialize_reviewed_identity_values = (
        materialize_reviewed_identity_values_with_address
    )
    _installed = True
