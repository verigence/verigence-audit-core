"""Give Aadhaar address components explicit typed Audit Core owners.

DI Aadhaar v1.2 publishes ``address_pincode``, ``address_state`` and
``address_district`` only when they are explicitly identifiable on the supplied
document. These are reviewed business values, so Booking Review must persist them
into ``customer_identity_review_values`` just like the other Aadhaar fields.

No geography is derived here. The exact reviewed DI/effective text is preserved.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import text

from audit_core import uc03_v2_review_materialization as materialization

_AADHAAR_ADDRESS_COLUMN_BY_FIELD = {
    "address_pincode": "aadhaar_address_pincode",
    "address_state": "aadhaar_address_state",
    "address_district": "aadhaar_address_district",
}

_installed = False


def install_uc03_aadhaar_address_core_ownership() -> None:
    """Extend the existing Aadhaar typed owner with DI v1.2 address components."""

    global _installed
    if _installed:
        return

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
        written = original(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            documents=documents,
            rejected_review_keys=rejected_review_keys,
            actor_id=actor_id,
        )

        for document in documents:
            if (
                str(document.documentTypeKey or "").strip().lower() != "aadhaar"
                or str(document.extractionState).upper() != "READY"
            ):
                continue

            values = materialization._normalized_document_values(
                document,
                allowed_fields=set(_AADHAAR_ADDRESS_COLUMN_BY_FIELD),
                rejected_review_keys=rejected_review_keys,
            )
            if not values:
                continue

            params = {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "document_id": document.documentId,
                "aadhaar_address_pincode": values.get("address_pincode"),
                "aadhaar_address_state": values.get("address_state"),
                "aadhaar_address_district": values.get("address_district"),
            }
            connection.execute(
                text(
                    """
                    UPDATE auditcore.customer_identity_review_values
                    SET aadhaar_address_pincode=:aadhaar_address_pincode,
                        aadhaar_address_state=:aadhaar_address_state,
                        aadhaar_address_district=:aadhaar_address_district,
                        updated_at_utc=now()
                    WHERE tenant_id=:tenant_id
                      AND journey_id=:journey_id
                      AND source_di_document_id=:document_id
                      AND document_type_key='AADHAAR'
                    """
                ),
                params,
            )

        return written

    materialization.materialize_reviewed_identity_values = (
        materialize_reviewed_identity_values_with_address
    )
    _installed = True
