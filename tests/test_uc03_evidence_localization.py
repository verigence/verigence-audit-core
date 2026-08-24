from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx

from audit_core.di_client import DiClient
from audit_core.uc03_booking_integrations import (
    _enrich_workspace_localization,
    _proposal_payload,
)


def test_proposal_payload_carries_optional_source_localization() -> None:
    fact = SimpleNamespace(
        value="RAJESH KUMAR",
        page_no=2,
        evidence_region={
            "type": "BOX_2D",
            "coordinateSystem": "NORMALIZED_1000",
            "box": [120, 85, 176, 438],
        },
    )

    assert _proposal_payload(fact) == {
        "value": "RAJESH KUMAR",
        "sourceLocalization": {
            "pageNo": 2,
            "evidenceRegion": {
                "type": "BOX_2D",
                "coordinateSystem": "NORMALIZED_1000",
                "box": [120, 85, 176, 438],
            },
        },
    }


def test_proposal_payload_does_not_invent_missing_localization() -> None:
    fact = SimpleNamespace(value="RAJESH KUMAR", page_no=None, evidence_region=None)
    assert _proposal_payload(fact) == {"value": "RAJESH KUMAR"}


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> "_Rows":
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _Connection:
    def execute(self, statement: Any, parameters: Any) -> _Rows:
        del statement, parameters
        return _Rows(
            [
                {
                    "capture_proposal_id": "proposal-1",
                    "proposed_value": {
                        "value": "RAJESH KUMAR",
                        "sourceLocalization": {
                            "pageNo": 1,
                            "evidenceRegion": {
                                "type": "BOX_2D",
                                "coordinateSystem": "NORMALIZED_1000",
                                "box": [100, 200, 160, 500],
                            },
                        },
                    },
                },
                {
                    "capture_proposal_id": "proposal-2",
                    "proposed_value": {"value": "WHITE"},
                },
            ]
        )


def test_workspace_localization_enrichment_is_optional_and_additive() -> None:
    body: dict[str, Any] = {
        "proposals": [
            {"proposalId": "proposal-1", "proposedValue": "RAJESH KUMAR"},
            {"proposalId": "proposal-2", "proposedValue": "WHITE"},
        ]
    }

    _enrich_workspace_localization(
        _Connection(),  # type: ignore[arg-type]
        tenant_id="tenant-1",
        journey_id="journey-1",  # type: ignore[arg-type]
        body=body,
    )

    assert body["proposals"][0]["pageNo"] == 1
    assert body["proposals"][0]["evidenceRegion"]["box"] == [100, 200, 160, 500]
    assert body["proposals"][1]["pageNo"] is None
    assert body["proposals"][1]["evidenceRegion"] is None


def test_di_client_reads_field_localization_and_original_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/fields"):
            return httpx.Response(
                200,
                json={
                    "errorCode": "000",
                    "errorMessage": "Success",
                    "data": {
                        "documentId": "doc-1",
                        "fields": [
                            {
                                "canonicalFieldId": "field-1",
                                "fieldKey": "customer_name",
                                "currentValue": "RAJESH KUMAR",
                                "valueSource": "MACHINE",
                                "confidenceScore": 92,
                                "versionNo": 1,
                                "pageNo": 3,
                                "evidenceRegion": {
                                    "type": "BOX_2D",
                                    "coordinateSystem": "NORMALIZED_1000",
                                    "box": [120, 85, 176, 438],
                                },
                            }
                        ],
                    },
                },
            )
        if request.url.path.endswith("/content"):
            return httpx.Response(
                200,
                content=b"sample-document",
                headers={
                    "content-type": "image/jpeg",
                    "content-disposition": 'attachment; filename="booking.jpg"',
                },
            )
        return httpx.Response(404)

    client = DiClient(
        base_url="https://di.example.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        facts = client.get_document_facts(
            token="service-token",
            tenant_id="tenant-1",
            subject_id="subject-1",
            document_id="doc-1",
        )
        assert facts[0].page_no == 3
        assert facts[0].evidence_region is not None
        assert facts[0].evidence_region["box"] == [120, 85, 176, 438]

        content, mime_type, disposition = client.get_document_content(
            token="service-token",
            tenant_id="tenant-1",
            subject_id="subject-1",
            document_id="doc-1",
        )
        assert content == b"sample-document"
        assert mime_type == "image/jpeg"
        assert disposition == 'attachment; filename="booking.jpg"'
    finally:
        client.close()
