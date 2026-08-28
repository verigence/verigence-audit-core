"""Schema V2 DI field-lineage contract used by Audit Core evidence refresh.

The richer contract is intentionally isolated from the legacy DiFact parser while
Schema V2 is proving compatibility. Once every DI consumer has adopted the
additive lineage fields, this can be folded into DiClient proper without changing
persistence semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from audit_core.di_client import DiClient, DiClientError


@dataclass(frozen=True)
class DiLineageFact:
    canonical_field_id: str
    field_key: str
    value: Any
    value_source: str
    confidence_score: float | None
    version_no: int
    fact_role: str
    extraction_key: str | None = None
    extracted_fact_id: UUID | None = None
    processing_run_id: UUID | None = None
    extraction_profile_id: UUID | None = None
    extraction_profile_version: int | None = None
    invocation_id: UUID | None = None
    pipeline_version: str | None = None
    page_no: int | None = None
    evidence_region: dict[str, Any] | None = None


def get_document_facts_with_lineage(
    client: DiClient,
    *,
    token: str,
    tenant_id: str,
    subject_id: str,
    document_id: str,
) -> tuple[DiLineageFact, ...]:
    """Read the additive Schema V2 field-lineage endpoint.

    Real DiClient instances use the new endpoint. Lightweight test/downgrade
    adapters that do not expose the internal D8 request helper fall back to the
    legacy fact call with role=UNSPECIFIED and null lineage. That keeps a rolling
    deployment safe while DI and Audit Core versions overlap.
    """
    request_data = getattr(client, "_request_data", None)
    if not callable(request_data):
        legacy = client.get_document_facts(
            token=token,
            tenant_id=tenant_id,
            subject_id=subject_id,
            document_id=document_id,
        )
        return tuple(
            DiLineageFact(
                canonical_field_id=fact.canonical_field_id,
                field_key=fact.field_key,
                value=fact.value,
                value_source=fact.value_source,
                confidence_score=fact.confidence_score,
                version_no=fact.version_no,
                fact_role="UNSPECIFIED",
                page_no=fact.page_no,
                evidence_region=fact.evidence_region,
            )
            for fact in legacy
        )

    payload = request_data(
        "GET",
        f"/v1/tenants/{tenant_id}/subjects/{subject_id}/documents/{document_id}/fields/lineage",
        operation="get_document_facts_with_lineage",
        token=token,
    )
    fields = payload.get("fields")
    if not isinstance(fields, list):
        raise _contract_error()
    return tuple(_fact(item) for item in fields)


def _fact(payload: Any) -> DiLineageFact:
    if not isinstance(payload, dict):
        raise _contract_error()
    region = payload.get("evidenceRegion")
    if region is not None and not isinstance(region, dict):
        raise _contract_error()
    return DiLineageFact(
        canonical_field_id=_required_str(payload, "canonicalFieldId"),
        field_key=_required_str(payload, "fieldKey"),
        value=payload.get("currentValue"),
        value_source=_required_str(payload, "valueSource"),
        confidence_score=_optional_float(payload, "confidenceScore"),
        version_no=_required_int(payload, "versionNo"),
        fact_role=_required_str(payload, "factRole"),
        extraction_key=_optional_str(payload, "extractionKey"),
        extracted_fact_id=_optional_uuid(payload, "extractedFactId"),
        processing_run_id=_optional_uuid(payload, "processingRunId"),
        extraction_profile_id=_optional_uuid(payload, "extractionProfileId"),
        extraction_profile_version=_optional_int(payload, "extractionProfileVersion"),
        invocation_id=_optional_uuid(payload, "invocationId"),
        pipeline_version=_optional_str(payload, "pipelineVersion"),
        page_no=_optional_int(payload, "pageNo"),
        evidence_region=dict(region) if region is not None else None,
    )


def _contract_error() -> DiClientError:
    return DiClientError(status_code=502, code="DI_CONTRACT_ERROR", retryable=False)


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise _contract_error()
    return value


def _optional_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _contract_error()
    return value


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _contract_error()
    return value


def _optional_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise _contract_error()
    return value


def _optional_float(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise _contract_error()
    return float(value)


def _optional_uuid(payload: dict[str, Any], key: str) -> UUID | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _contract_error()
    try:
        return UUID(value)
    except ValueError as exc:
        raise _contract_error() from exc
