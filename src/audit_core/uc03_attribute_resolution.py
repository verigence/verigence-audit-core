from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_attribute_mapping import AttributeSpec
from audit_core.uc03_booking_capture import _write_typed_capture
from audit_core.uc03_booking_field_owners import apply_booking_field_owner

_MAPPING_VERSION = "UC03-ATTR-2026-08-30-v2"
_RELATIONSHIP_TYPES = {"S/O", "W/O", "D/O"}


def _normalize_identity_name(value: Any) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise TypeError("Identity name must be a scalar text value")
    name = re.sub(r"\s+", " ", str(value).strip())
    if not name:
        raise ValueError("Identity name cannot be blank")
    return name


def _identity_equivalence(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def apply_legal_name_review(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    value: Any,
    actor_id: str,
    source_evidence_id: UUID | None,
) -> tuple[str, str, str]:
    """Apply an identity-authoritative reviewed name without touching Entered Name."""

    name = _normalize_identity_name(value)
    customer = connection.execute(
        text(
            """
            SELECT c.customer_id, c.legal_name
            FROM auditcore.journeys j
            JOIN auditcore.customers c
              ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
            WHERE j.tenant_id=:tenant_id AND j.journey_id=:journey_id
            FOR UPDATE OF c
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()
    existing = customer["legal_name"]
    equivalent = (
        existing is None
        or not str(existing).strip()
        or _identity_equivalence(str(existing)) == _identity_equivalence(name)
    )
    if equivalent:
        connection.execute(
            text(
                """
                UPDATE auditcore.customers
                SET legal_name=:legal_name,
                    legal_name_status='VERIFIED',
                    legal_name_source_evidence_id=:source_evidence_id,
                    legal_name_verified_by_actor_id=:actor_id,
                    legal_name_verified_at_utc=now(),
                    updated_by_actor_id=:actor_id,
                    updated_at_utc=now(),
                    version_no=version_no+1
                WHERE tenant_id=:tenant_id AND customer_id=:customer_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "customer_id": customer["customer_id"],
                "legal_name": name,
                "source_evidence_id": source_evidence_id,
                "actor_id": actor_id,
            },
        )
        return "CUSTOMER", str(customer["customer_id"]), "VERIFIED"

    connection.execute(
        text(
            """
            UPDATE auditcore.customers
            SET legal_name_status='CONFLICT',
                updated_by_actor_id=:actor_id,
                updated_at_utc=now(),
                version_no=version_no+1
            WHERE tenant_id=:tenant_id AND customer_id=:customer_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "customer_id": customer["customer_id"],
            "actor_id": actor_id,
        },
    )
    return "CUSTOMER", str(customer["customer_id"]), "CONFLICT"


def apply_customer_relationship_review(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    attribute_key: str,
    value: Any,
    actor_id: str,
) -> tuple[str, str, str]:
    """Apply only a reviewed explicit S/O/W/O/D/O relationship value.

    PAN/Aadhaar raw values remain source-specific in DI. A materially different
    already-reviewed value is retained and surfaced as CONFLICT rather than being
    silently overwritten by another source.
    """

    customer = connection.execute(
        text(
            """
            SELECT c.customer_id, c.relationship_type, c.relationship_name
            FROM auditcore.journeys j
            JOIN auditcore.customers c
              ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
            WHERE j.tenant_id=:tenant_id AND j.journey_id=:journey_id
            FOR UPDATE OF c
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()

    if attribute_key == "customer_relationship_type":
        normalized = re.sub(r"\s+", "", str(value).strip()).upper()
        if normalized not in _RELATIONSHIP_TYPES:
            raise ValueError("Relationship Type must be S/O, W/O or D/O")
        existing = customer["relationship_type"]
        if existing is not None and str(existing).strip() and str(existing).upper() != normalized:
            return "CUSTOMER", str(customer["customer_id"]), "CONFLICT"
        connection.execute(
            text(
                """
                UPDATE auditcore.customers
                SET relationship_type=:value,
                    updated_by_actor_id=:actor_id,
                    updated_at_utc=now(),
                    version_no=version_no+1
                WHERE tenant_id=:tenant_id AND customer_id=:customer_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "customer_id": customer["customer_id"],
                "value": normalized,
                "actor_id": actor_id,
            },
        )
        return "CUSTOMER", str(customer["customer_id"]), "APPLIED"

    if attribute_key == "customer_relationship_name":
        normalized = _normalize_identity_name(value)
        existing = customer["relationship_name"]
        if (
            existing is not None
            and str(existing).strip()
            and _identity_equivalence(str(existing)) != _identity_equivalence(normalized)
        ):
            return "CUSTOMER", str(customer["customer_id"]), "CONFLICT"
        connection.execute(
            text(
                """
                UPDATE auditcore.customers
                SET relationship_name=:value,
                    updated_by_actor_id=:actor_id,
                    updated_at_utc=now(),
                    version_no=version_no+1
                WHERE tenant_id=:tenant_id AND customer_id=:customer_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "customer_id": customer["customer_id"],
                "value": normalized,
                "actor_id": actor_id,
            },
        )
        return "CUSTOMER", str(customer["customer_id"]), "APPLIED"

    raise RuntimeError("Unsupported customer relationship attribute")


def apply_supported_operational_attribute(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    spec: AttributeSpec,
    value: Any,
    actor_id: str,
    source_document_type_key: str | None,
    source_field_key: str,
    source_evidence_id: UUID | None,
) -> tuple[str, str, str] | None:
    """Write only an explicitly approved typed-domain owner.

    Commercial machine facts can be SUPPORTED for review and source resolution
    while returning None here; DI remains their source of truth. Audit Core stores
    only reviewed business values with an approved typed owner.
    """

    if spec.mapping_status != "SUPPORTED":
        return None

    document_type = (source_document_type_key or "").strip().casefold()
    field_key = source_field_key.strip().casefold()
    if spec.attribute_key == "customer_name":
        identity_source = (
            (document_type in {"pan", "pan_card"} and field_key == "pan_name")
            or (document_type == "aadhaar" and field_key == "aadhaar_name")
        )
        if not identity_source:
            return None
        return apply_legal_name_review(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            value=value,
            actor_id=actor_id,
            source_evidence_id=source_evidence_id,
        )

    if spec.attribute_key in {"customer_relationship_type", "customer_relationship_name"}:
        return apply_customer_relationship_review(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            attribute_key=spec.attribute_key,
            value=value,
            actor_id=actor_id,
        )

    booking_owner = apply_booking_field_owner(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        attribute_key=spec.attribute_key,
        value=value,
        source_evidence_id=source_evidence_id,
    )
    if booking_owner is not None:
        return booking_owner

    if spec.operational_field is None:
        # Deliberate review/provenance-only fact (especially commercial values).
        return None

    domain, record = _write_typed_capture(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        field_key=spec.operational_field,
        value=value,
        source_evidence_id=source_evidence_id,
    )
    return domain, record, "APPLIED"


def record_attribute_resolution(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    spec: AttributeSpec,
    source_di_document_id: UUID,
    source_evidence_id: UUID | None,
    source_canonical_field_id: str | None,
    source_field_key: str,
    source_fact_version: int,
    source_document_type_key: str | None,
    actor_id: str,
    owning_domain_key: str | None,
    owning_record_reference: str | None,
) -> None:
    """Persist source references only; raw DI facts remain in DI."""

    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_attribute_resolutions (
                tenant_id, journey_id, stage_code, attribute_key, excel_field_no,
                mapping_status, source_di_document_id, source_evidence_id,
                source_canonical_field_id, source_field_key, source_fact_version,
                source_document_type_key, resolution_rule, mapping_version,
                owning_domain_key, owning_record_reference, resolved_by_actor_id
            ) VALUES (
                :tenant_id, :journey_id, :stage_code, :attribute_key, :excel_field_no,
                :mapping_status, :source_di_document_id, :source_evidence_id,
                :source_canonical_field_id, :source_field_key, :source_fact_version,
                :source_document_type_key, 'SOURCE_PRIORITY_THEN_CONFIDENCE', :mapping_version,
                :owning_domain_key, :owning_record_reference, :actor_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
            "attribute_key": spec.attribute_key,
            "excel_field_no": spec.excel_field_no,
            "mapping_status": spec.mapping_status,
            "source_di_document_id": source_di_document_id,
            "source_evidence_id": source_evidence_id,
            "source_canonical_field_id": source_canonical_field_id,
            "source_field_key": source_field_key,
            "source_fact_version": source_fact_version,
            "source_document_type_key": source_document_type_key,
            "mapping_version": _MAPPING_VERSION,
            "owning_domain_key": owning_domain_key,
            "owning_record_reference": owning_record_reference,
            "actor_id": actor_id,
        },
    )
