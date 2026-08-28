from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from audit_core.di_client import DiClientError
from audit_core.di_lineage import _fact, get_document_facts_with_lineage


def test_same_chassis_canonical_can_carry_two_distinct_vehicle_roles() -> None:
    canonical_id = "5ee16285-22af-43fd-a997-0c9ca4ad9caf"
    profile_id = "62b3184d-a148-4c8e-b96e-c14643409e54"

    subject = _fact(
        {
            "canonicalFieldId": canonical_id,
            "fieldKey": "chassis_number",
            "currentValue": "SUBJECT-VIN-001",
            "valueSource": "MACHINE",
            "confidenceScore": 98.0,
            "versionNo": 1,
            "factRole": "SUBJECT_VEHICLE",
            "extractionKey": "chassis_number",
            "extractedFactId": "da54dadf-508e-451f-be14-bdf3308fcb09",
            "processingRunId": "d07f31c6-9709-4262-908b-fe076e64064d",
            "extractionProfileId": profile_id,
            "extractionProfileVersion": 1,
            "invocationId": "75d29c7a-916c-453d-a379-af051f4b54c1",
            "pipelineVersion": "2.2.0",
            "pageNo": 1,
            "evidenceRegion": None,
        }
    )
    exchange = _fact(
        {
            "canonicalFieldId": canonical_id,
            "fieldKey": "chassis_number",
            "currentValue": "EXCHANGE-VIN-009",
            "valueSource": "MACHINE",
            "confidenceScore": 96.0,
            "versionNo": 1,
            "factRole": "EXCHANGE_VEHICLE",
            "extractionKey": "chassis_number",
            "extractedFactId": "57484582-0e0a-464b-8c87-31048a60ddb5",
            "processingRunId": "5ec51c70-b947-4ae0-9565-459957d27f89",
            "extractionProfileId": profile_id,
            "extractionProfileVersion": 1,
            "invocationId": "5abb26e0-eabf-4c69-bb44-99ad6f1abdd3",
            "pipelineVersion": "2.2.0",
            "pageNo": 2,
            "evidenceRegion": {"type": "BOX_2D", "box": [10, 10, 20, 20]},
        }
    )

    assert subject.canonical_field_id == exchange.canonical_field_id
    assert subject.field_key == exchange.field_key == "chassis_number"
    assert subject.fact_role == "SUBJECT_VEHICLE"
    assert exchange.fact_role == "EXCHANGE_VEHICLE"
    assert subject.value != exchange.value
    assert subject.extracted_fact_id != exchange.extracted_fact_id
    assert subject.processing_run_id != exchange.processing_run_id
    assert subject.extraction_profile_id == exchange.extraction_profile_id == UUID(profile_id)
    assert subject.extraction_profile_version == exchange.extraction_profile_version == 1


def test_lineage_parser_preserves_exact_profile_and_invocation_version() -> None:
    fact = _fact(
        {
            "canonicalFieldId": "b68e2301-2477-42b1-a3db-e393550b8502",
            "fieldKey": "valuation_final_offer_value",
            "currentValue": 450000,
            "valueSource": "MACHINE",
            "confidenceScore": 92.0,
            "versionNo": 3,
            "factRole": "EXCHANGE_VEHICLE",
            "extractionKey": "final_offer_value",
            "extractedFactId": "e4054d7c-380d-456a-adb9-d8098085bc0f",
            "processingRunId": "b3372ad6-1945-499c-b05b-ce9fb7081b38",
            "extractionProfileId": "7c68c213-5b2c-499e-9c87-2d04224330ba",
            "extractionProfileVersion": 2,
            "invocationId": "7f608ab1-9ea9-4fe5-bd8f-aaea0fe188b2",
            "pipelineVersion": "2.2.0",
            "pageNo": 3,
            "evidenceRegion": None,
        }
    )

    assert fact.version_no == 3
    assert fact.fact_role == "EXCHANGE_VEHICLE"
    assert fact.extraction_key == "final_offer_value"
    assert fact.extraction_profile_version == 2
    assert fact.pipeline_version == "2.2.0"
    assert fact.invocation_id == UUID("7f608ab1-9ea9-4fe5-bd8f-aaea0fe188b2")


class _OlderDiClient:
    def __init__(self, lineage_status: int = 404) -> None:
        self.lineage_status = lineage_status
        self.legacy_called = False

    def _request_data(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        raise DiClientError(
            status_code=self.lineage_status,
            code=f"DI_HTTP_{self.lineage_status}",
            retryable=False,
        )

    def get_document_facts(self, **_kwargs: object) -> tuple[SimpleNamespace, ...]:
        self.legacy_called = True
        return (
            SimpleNamespace(
                canonical_field_id="canonical-1",
                field_key="legacy_field",
                value="legacy-value",
                value_source="MACHINE",
                confidence_score=88.0,
                version_no=2,
                page_no=1,
                evidence_region=None,
            ),
        )


def test_lineage_route_404_falls_back_to_legacy_fields_during_rolling_deploy() -> None:
    client = _OlderDiClient()

    facts = get_document_facts_with_lineage(
        client,  # type: ignore[arg-type]
        token="token",
        tenant_id="tenant-1",
        subject_id="subject-1",
        document_id="document-1",
    )

    assert client.legacy_called is True
    assert len(facts) == 1
    assert facts[0].field_key == "legacy_field"
    assert facts[0].fact_role == "UNSPECIFIED"
    assert facts[0].extracted_fact_id is None


def test_lineage_route_authorization_error_does_not_fall_back() -> None:
    client = _OlderDiClient(lineage_status=403)

    with pytest.raises(DiClientError) as exc_info:
        get_document_facts_with_lineage(
            client,  # type: ignore[arg-type]
            token="token",
            tenant_id="tenant-1",
            subject_id="subject-1",
            document_id="document-1",
        )

    assert exc_info.value.status_code == 403
    assert client.legacy_called is False
