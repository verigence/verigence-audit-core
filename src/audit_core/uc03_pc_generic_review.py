from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection

from audit_core.errors import AuditCoreError
from audit_core.uc03_attribute_mapping import spec_for_field
from audit_core.uc03_attribute_resolution import apply_supported_operational_attribute
from audit_core.uc03_booking_receipt_capture import (
    _RECEIPT_CAPTURE_MAP,
    _write_receipt_capture,
)
from audit_core.uc03_di_core_persistence import (
    ReviewedDiField,
    persist_reviewed_di_fields,
)
from audit_core.uc03_document_registry import is_receipt_document_type

logger = logging.getLogger(__name__)


class DirectExtractedField(BaseModel):
    """One DI field as shown to the PC, with an optional PC modification."""

    model_config = ConfigDict(extra="forbid")

    fieldKey: str = Field(min_length=1, max_length=160)
    sourceFactRef: UUID
    sourceFactVersion: int = Field(gt=0)
    extractedValue: Any | None = None
    modifiedValue: Any | None = None
    confidenceScore: float | None = Field(default=None, ge=0, le=1)


def _validate_unique_fields(fields: list[DirectExtractedField]) -> None:
    seen: set[tuple[UUID, int]] = set()
    for field in fields:
        key = (field.sourceFactRef, field.sourceFactVersion)
        if key in seen:
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Duplicate extraction field",
                detail="A DI source fact version may appear only once in a document review.",
            )
        seen.add(key)


def _store_fields(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    evidence_id: UUID,
    document_id: UUID,
    document_type_key: str,
    actor_id: str,
    fields: list[DirectExtractedField],
) -> int:
    """Persist every populated direct-review DI field before typed projection."""

    reviewed_fields = [
        ReviewedDiField(
            document_id=document_id,
            field_key=field.fieldKey.strip().lower(),
            source_fact_version=field.sourceFactVersion,
            evidence_id=evidence_id,
            source_fact_ref=field.sourceFactRef,
            source_document_type_key=document_type_key,
            extracted_value=field.extractedValue,
            modified_value=field.modifiedValue,
            effective_value=(
                field.modifiedValue
                if field.modifiedValue is not None
                else field.extractedValue
            ),
            confidence_score=field.confidenceScore,
            confidence_scale=(
                "UNIT_INTERVAL" if field.confidenceScore is not None else None
            ),
            is_modified=field.modifiedValue is not None,
        )
        for field in fields
    ]
    return persist_reviewed_di_fields(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code="BOOKING",
        actor_id=actor_id,
        fields=reviewed_fields,
    )


def _project_known_field(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    evidence_id: UUID,
    document_id: UUID,
    document_type_key: str,
    actor_id: str,
    field: DirectExtractedField,
) -> tuple[str, str] | None:
    source_field_key = field.fieldKey.strip().lower()
    receipt_capture_key = (
        _RECEIPT_CAPTURE_MAP.get(source_field_key)
        if is_receipt_document_type(document_type_key)
        else None
    )
    value = field.modifiedValue if field.modifiedValue is not None else field.extractedValue
    if receipt_capture_key is not None:
        return _write_receipt_capture(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            capture_key=receipt_capture_key,
            value=value,
            source_evidence_id=evidence_id,
        )

    spec = spec_for_field(source_field_key)
    if spec is None:
        return None
    application = apply_supported_operational_attribute(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        spec=spec,
        value=value,
        actor_id=actor_id,
        source_document_type_key=document_type_key,
        source_field_key=source_field_key,
        source_evidence_id=evidence_id,
    )
    if application is None:
        return None
    return application[0], application[1]


