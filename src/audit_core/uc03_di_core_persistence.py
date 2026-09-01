from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.db import set_security_actor_context

StageCode = Literal["BOOKING", "DELIVERY"]
ConfidenceScale = Literal["UNIT_INTERVAL", "PERCENT"]


@dataclass(frozen=True)
class ReviewedDiField:
    """One reviewed DI field ready for durable Audit Core persistence.

    source_fact_ref/evidence_id are the legacy V1 identifiers. Current V2 callers
    use source_canonical_field_id plus DI document/fact version instead. The
    effective value is deliberately separate from the original extracted value so
    corrections never erase DI provenance.
    """

    document_id: UUID
    field_key: str
    source_fact_version: int
    extracted_value: Any
    effective_value: Any
    evidence_id: UUID | None = None
    source_fact_ref: UUID | None = None
    source_canonical_field_id: str | None = None
    source_document_type_key: str | None = None
    confidence_score: float | None = None
    confidence_scale: ConfidenceScale | None = None
    modified_value: Any = None
    is_modified: bool = False
    effective_value_is_set: bool = True


def has_persistable_value(value: Any) -> bool:
    """Return True for meaningful DI values without losing valid 0/False values."""

    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _json_payload(value: Any, *, is_set: bool) -> str | None:
    if not is_set:
        return None
    return json.dumps(value, default=str)


def _validate_field(field: ReviewedDiField) -> None:
    if not field.field_key.strip():
        raise ValueError("Reviewed DI field requires a non-empty field_key")
    if field.source_fact_version <= 0:
        raise ValueError("Reviewed DI field source_fact_version must be positive")
    if field.source_fact_ref is None and not str(
        field.source_canonical_field_id or ""
    ).strip():
        raise ValueError(
            "Reviewed DI field requires either legacy source_fact_ref or "
            "source_canonical_field_id"
        )
    if field.confidence_score is None:
        if field.confidence_scale is not None:
            raise ValueError("confidence_scale requires confidence_score")
        return
    if field.confidence_scale is None:
        raise ValueError("confidence_score requires an explicit confidence_scale")
    score = float(field.confidence_score)
    if field.confidence_scale == "UNIT_INTERVAL" and not 0 <= score <= 1:
        raise ValueError("UNIT_INTERVAL confidence must be between 0 and 1")
    if field.confidence_scale == "PERCENT" and not 0 <= score <= 100:
        raise ValueError("PERCENT confidence must be between 0 and 100")


def _row(
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: StageCode,
    actor_id: str,
    field: ReviewedDiField,
) -> dict[str, Any]:
    _validate_field(field)
    return {
        "tenant_id": tenant_id,
        "journey_id": journey_id,
        "stage_code": stage_code,
        "evidence_id": field.evidence_id,
        "document_id": field.document_id,
        "source_fact_ref": field.source_fact_ref,
        "source_fact_version": field.source_fact_version,
        "source_document_type_key": (
            str(field.source_document_type_key).strip()
            if field.source_document_type_key
            else None
        ),
        "source_canonical_field_id": (
            str(field.source_canonical_field_id).strip()
            if field.source_canonical_field_id
            else None
        ),
        "field_key": field.field_key.strip(),
        "extracted_value": _json_payload(
            field.extracted_value,
            is_set=field.extracted_value is not None,
        ),
        "modified_value": _json_payload(
            field.modified_value,
            is_set=field.is_modified,
        ),
        "effective_value": _json_payload(
            field.effective_value,
            is_set=field.effective_value_is_set,
        ),
        "confidence_score": field.confidence_score,
        "confidence_scale": field.confidence_scale,
        "is_modified": field.is_modified,
        "actor_id": actor_id,
    }


_LEGACY_UPSERT = text(
    """
    INSERT INTO auditcore.journey_document_extracted_fields (
        tenant_id, journey_id, evidence_id, di_document_id,
        source_fact_ref, source_fact_version, stage_code,
        source_document_type_key, source_canonical_field_id, field_key,
        extracted_value, modified_value, effective_value,
        confidence_score, confidence_scale, is_modified,
        modified_by_actor_id, modified_at_utc,
        reviewed_by_actor_id, reviewed_at_utc
    ) VALUES (
        :tenant_id, :journey_id, :evidence_id, :document_id,
        :source_fact_ref, :source_fact_version, :stage_code,
        :source_document_type_key, :source_canonical_field_id, :field_key,
        CAST(:extracted_value AS jsonb), CAST(:modified_value AS jsonb),
        CAST(:effective_value AS jsonb), :confidence_score, :confidence_scale,
        :is_modified,
        CASE WHEN :is_modified THEN :actor_id ELSE NULL END,
        CASE WHEN :is_modified THEN now() ELSE NULL END,
        :actor_id, now()
    )
    ON CONFLICT (
        tenant_id, journey_id, di_document_id,
        source_fact_ref, source_fact_version
    ) DO UPDATE SET
        evidence_id=EXCLUDED.evidence_id,
        stage_code=EXCLUDED.stage_code,
        source_document_type_key=EXCLUDED.source_document_type_key,
        source_canonical_field_id=EXCLUDED.source_canonical_field_id,
        field_key=EXCLUDED.field_key,
        extracted_value=EXCLUDED.extracted_value,
        modified_value=EXCLUDED.modified_value,
        effective_value=EXCLUDED.effective_value,
        confidence_score=EXCLUDED.confidence_score,
        confidence_scale=EXCLUDED.confidence_scale,
        is_modified=EXCLUDED.is_modified,
        modified_by_actor_id=EXCLUDED.modified_by_actor_id,
        modified_at_utc=EXCLUDED.modified_at_utc,
        reviewed_by_actor_id=EXCLUDED.reviewed_by_actor_id,
        reviewed_at_utc=EXCLUDED.reviewed_at_utc,
        updated_at_utc=now()
    """
)

_V2_UPSERT = text(
    """
    INSERT INTO auditcore.journey_document_extracted_fields (
        tenant_id, journey_id, evidence_id, di_document_id,
        source_fact_ref, source_fact_version, stage_code,
        source_document_type_key, source_canonical_field_id, field_key,
        extracted_value, modified_value, effective_value,
        confidence_score, confidence_scale, is_modified,
        modified_by_actor_id, modified_at_utc,
        reviewed_by_actor_id, reviewed_at_utc
    ) VALUES (
        :tenant_id, :journey_id, :evidence_id, :document_id,
        :source_fact_ref, :source_fact_version, :stage_code,
        :source_document_type_key, :source_canonical_field_id, :field_key,
        CAST(:extracted_value AS jsonb), CAST(:modified_value AS jsonb),
        CAST(:effective_value AS jsonb), :confidence_score, :confidence_scale,
        :is_modified,
        CASE WHEN :is_modified THEN :actor_id ELSE NULL END,
        CASE WHEN :is_modified THEN now() ELSE NULL END,
        :actor_id, now()
    )
    ON CONFLICT (
        tenant_id, journey_id, stage_code, di_document_id,
        source_canonical_field_id, source_fact_version
    ) WHERE source_canonical_field_id IS NOT NULL
    DO UPDATE SET
        evidence_id=EXCLUDED.evidence_id,
        source_fact_ref=EXCLUDED.source_fact_ref,
        source_document_type_key=EXCLUDED.source_document_type_key,
        field_key=EXCLUDED.field_key,
        extracted_value=EXCLUDED.extracted_value,
        modified_value=EXCLUDED.modified_value,
        effective_value=EXCLUDED.effective_value,
        confidence_score=EXCLUDED.confidence_score,
        confidence_scale=EXCLUDED.confidence_scale,
        is_modified=EXCLUDED.is_modified,
        modified_by_actor_id=EXCLUDED.modified_by_actor_id,
        modified_at_utc=EXCLUDED.modified_at_utc,
        reviewed_by_actor_id=EXCLUDED.reviewed_by_actor_id,
        reviewed_at_utc=EXCLUDED.reviewed_at_utc,
        updated_at_utc=now()
    """
)


def persist_reviewed_di_fields(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: StageCode,
    actor_id: str,
    fields: list[ReviewedDiField],
) -> int:
    """Persist every populated/reviewed DI field without requiring a typed owner.

    Empty/null unchanged DI values are skipped. Valid false/zero values are kept.
    A correction is persisted even when the original value was empty. V1 and V2
    identifiers share one table but use their own truthful conflict keys.
    """

    if stage_code not in {"BOOKING", "DELIVERY"}:
        raise ValueError("stage_code must be BOOKING or DELIVERY")
    if not actor_id.strip():
        raise ValueError("reviewed DI persistence requires actor_id")

    legacy_rows: list[dict[str, Any]] = []
    v2_rows: list[dict[str, Any]] = []
    for field in fields:
        if not (
            has_persistable_value(field.extracted_value)
            or field.is_modified
            or (
                field.effective_value_is_set
                and has_persistable_value(field.effective_value)
            )
        ):
            continue
        row = _row(
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage_code,
            actor_id=actor_id,
            field=field,
        )
        if field.source_fact_ref is not None:
            legacy_rows.append(row)
        else:
            v2_rows.append(row)

    if legacy_rows:
        connection.execute(_LEGACY_UPSERT, legacy_rows)
    if v2_rows:
        connection.execute(_V2_UPSERT, v2_rows)
    return len(legacy_rows) + len(v2_rows)


_installed = False
_original_review_scope: Any | None = None


def _scope_with_actor_context(
    connection: Connection,
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Preserve the authenticated Review actor in transaction-local DB context."""

    if _original_review_scope is None:
        raise RuntimeError("UC03 Review persistence installer is not initialized")
    context = _original_review_scope(connection, *args, **kwargs)
    human_principal = kwargs.get("human_principal")
    if human_principal is None:
        raise RuntimeError("UC03 Review scope requires an authenticated human principal")
    set_security_actor_context(connection, human_principal.subject)
    return context


def install_uc03_di_core_persistence() -> None:
    """Install Review actor context; field persistence is invoked explicitly."""

    global _installed, _original_review_scope
    if _installed:
        return

    # Lazy import prevents a cycle when Booking Review imports the shared
    # persistence helper from this module.
    from audit_core import uc03_booking_review_decisions as review_decisions

    _original_review_scope = review_decisions._scope
    review_decisions._scope = _scope_with_actor_context
    _installed = True
