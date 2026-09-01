from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, text

MappingStatus = Literal["SUPPORTED", "PROVISIONAL"]

_FINAL_SOURCE_MAPPING_VERSION = "UC03-FINAL-SOURCE-2026-09-01-v1"


def _snapshot_payload(value: Any) -> str:
    """Serialize a final value without changing its business representation."""

    return json.dumps(value, default=str, separators=(",", ":"))


def record_post_delivery_reviewed_resolution(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    attribute_key: str,
    source_reviewed_field_id: UUID,
    actor_id: str,
    resolution_rule: str,
    excel_field_no: int | None = None,
    mapping_status: MappingStatus = "SUPPORTED",
    mapping_version: str = _FINAL_SOURCE_MAPPING_VERSION,
    owning_domain_key: str | None = None,
    owning_record_reference: str | None = None,
) -> UUID:
    """Persist one document-derived POST_DELIVERY winner from reviewed Core state.

    The selected value and DI provenance are loaded from the durable reviewed-field
    row, never from a fresh DI request. Selecting by tenant + journey + row id makes
    a cross-Journey source impossible even before the database FK is evaluated.
    """

    source = connection.execute(
        text(
            """
            SELECT extracted_field_id, evidence_id, di_document_id,
                   source_canonical_field_id, field_key, source_fact_version,
                   source_document_type_key, effective_value
            FROM auditcore.journey_document_extracted_fields
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND extracted_field_id=:source_reviewed_field_id
              AND stage_code IN ('BOOKING','DELIVERY')
              AND effective_value IS NOT NULL
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "source_reviewed_field_id": source_reviewed_field_id,
        },
    ).mappings().one_or_none()
    if source is None:
        raise ValueError(
            "Selected reviewed field is not an accepted Booking/Delivery value for this Journey"
        )

    resolution_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_attribute_resolutions (
                tenant_id, journey_id, stage_code, attribute_key, excel_field_no,
                mapping_status, source_di_document_id, source_evidence_id,
                source_canonical_field_id, source_field_key, source_fact_version,
                source_document_type_key, source_reviewed_field_id,
                resolved_value_snapshot, resolution_rule, mapping_version,
                owning_domain_key, owning_record_reference, resolved_by_actor_id
            ) VALUES (
                :tenant_id, :journey_id, 'POST_DELIVERY', :attribute_key, :excel_field_no,
                :mapping_status, :source_di_document_id, :source_evidence_id,
                :source_canonical_field_id, :source_field_key, :source_fact_version,
                :source_document_type_key, :source_reviewed_field_id,
                CAST(:resolved_value_snapshot AS jsonb), :resolution_rule, :mapping_version,
                :owning_domain_key, :owning_record_reference, :actor_id
            )
            RETURNING journey_attribute_resolution_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "attribute_key": attribute_key,
            "excel_field_no": excel_field_no,
            "mapping_status": mapping_status,
            "source_di_document_id": source["di_document_id"],
            "source_evidence_id": source["evidence_id"],
            "source_canonical_field_id": source["source_canonical_field_id"],
            "source_field_key": source["field_key"],
            "source_fact_version": source["source_fact_version"],
            "source_document_type_key": source["source_document_type_key"],
            "source_reviewed_field_id": source["extracted_field_id"],
            "resolved_value_snapshot": _snapshot_payload(source["effective_value"]),
            "resolution_rule": resolution_rule,
            "mapping_version": mapping_version,
            "owning_domain_key": owning_domain_key,
            "owning_record_reference": owning_record_reference,
            "actor_id": actor_id,
        },
    ).scalar_one()
    return UUID(str(resolution_id))


def record_post_delivery_typed_resolution(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    attribute_key: str,
    resolved_value: Any,
    actor_id: str,
    resolution_rule: str,
    owning_domain_key: str,
    owning_record_reference: str,
    excel_field_no: int | None = None,
    mapping_status: MappingStatus = "SUPPORTED",
    mapping_version: str = _FINAL_SOURCE_MAPPING_VERSION,
) -> UUID:
    """Persist one typed/source-system POST_DELIVERY final value.

    No DI identifiers are fabricated. The existing owning-domain/reference pair is
    the source identity and source_reviewed_field_id remains SQL NULL.
    """

    if resolved_value is None or resolved_value == "":
        raise ValueError("A typed final resolution requires a populated value")
    if not owning_domain_key.strip() or not owning_record_reference.strip():
        raise ValueError("A typed final resolution requires an explicit owning record")

    resolution_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_attribute_resolutions (
                tenant_id, journey_id, stage_code, attribute_key, excel_field_no,
                mapping_status, source_di_document_id, source_evidence_id,
                source_canonical_field_id, source_field_key, source_fact_version,
                source_document_type_key, source_reviewed_field_id,
                resolved_value_snapshot, resolution_rule, mapping_version,
                owning_domain_key, owning_record_reference, resolved_by_actor_id
            ) VALUES (
                :tenant_id, :journey_id, 'POST_DELIVERY', :attribute_key, :excel_field_no,
                :mapping_status, NULL, NULL,
                NULL, NULL, NULL,
                NULL, NULL,
                CAST(:resolved_value_snapshot AS jsonb), :resolution_rule, :mapping_version,
                :owning_domain_key, :owning_record_reference, :actor_id
            )
            RETURNING journey_attribute_resolution_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "attribute_key": attribute_key,
            "excel_field_no": excel_field_no,
            "mapping_status": mapping_status,
            "resolved_value_snapshot": _snapshot_payload(resolved_value),
            "resolution_rule": resolution_rule,
            "mapping_version": mapping_version,
            "owning_domain_key": owning_domain_key,
            "owning_record_reference": owning_record_reference,
            "actor_id": actor_id,
        },
    ).scalar_one()
    return UUID(str(resolution_id))
