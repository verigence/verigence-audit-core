from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import audit_core.evidence_read as evidence_read
from audit_core.di_lineage import DiLineageFact


class _CaptureConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, statement, params=None):
        self.calls.append((str(statement), dict(params or {})))
        return SimpleNamespace()


class _CaptureEngine:
    def __init__(self, connection: _CaptureConnection) -> None:
        self.connection = connection

    @contextmanager
    def begin(self):
        yield self.connection


def test_persist_refresh_writes_exact_schema_v2_di_lineage(monkeypatch) -> None:
    tenant_id = "tenant-schema-v2"
    evidence_id = UUID("10a05b50-9cb0-4a8d-a076-aa68984125c4")
    journey_id = UUID("4531e74c-74b0-4b12-aefd-910567576084")
    extracted_fact_id = UUID("da54dadf-508e-451f-be14-bdf3308fcb09")
    processing_run_id = UUID("d07f31c6-9709-4262-908b-fe076e64064d")
    extraction_profile_id = UUID("62b3184d-a148-4c8e-b96e-c14643409e54")
    invocation_id = UUID("75d29c7a-916c-453d-a379-af051f4b54c1")

    connection = _CaptureConnection()
    engine = _CaptureEngine(connection)

    monkeypatch.setattr(evidence_read, "set_tenant_context", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        evidence_read,
        "_evidence_row",
        lambda *_args, **_kwargs: {
            "evidence_id": evidence_id,
            "journey_id": journey_id,
            "document_type_key": "valuation_report",
            "evidence_purpose": "EXCHANGE_VEHICLE_VALUATION",
            "processing_status_cache": "PROCESSING",
            "verification_status_cache": "PENDING",
            "linked_at_utc": datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
        },
    )
    monkeypatch.setattr(evidence_read, "_fact_rows", lambda *_args, **_kwargs: [])

    fact = DiLineageFact(
        canonical_field_id="5ee16285-22af-43fd-a997-0c9ca4ad9caf",
        field_key="chassis_number",
        value="EXCHANGE-VIN-009",
        value_source="MACHINE",
        confidence_score=96.0,
        version_no=3,
        fact_role="EXCHANGE_VEHICLE",
        extraction_key="chassis_number",
        extracted_fact_id=extracted_fact_id,
        processing_run_id=processing_run_id,
        extraction_profile_id=extraction_profile_id,
        extraction_profile_version=2,
        invocation_id=invocation_id,
        pipeline_version="2.2.0",
        page_no=2,
        evidence_region=None,
    )
    document = SimpleNamespace(
        processing_status="COMPLETED",
        verification_state="VERIFIED",
        confirmation_status="CONFIRMED",
    )

    evidence_read._persist_refresh(
        engine,  # type: ignore[arg-type]
        tenant_id=tenant_id,
        evidence_id=evidence_id,
        journey_id=journey_id,
        document=document,
        facts=(fact,),
    )

    insert_calls = [
        (sql, params)
        for sql, params in connection.calls
        if "INSERT INTO auditcore.evidence_facts" in sql
    ]
    assert len(insert_calls) == 1
    sql, params = insert_calls[0]

    for column in (
        "fact_role",
        "di_value_version_no",
        "di_extracted_fact_id",
        "di_processing_run_id",
        "di_extraction_profile_id",
        "di_extraction_profile_version",
        "di_invocation_id",
        "di_pipeline_version",
    ):
        assert column in sql

    assert params["field_key"] == "chassis_number"
    assert params["fact_role"] == "EXCHANGE_VEHICLE"
    assert params["di_value_version_no"] == 3
    assert params["di_extracted_fact_id"] == extracted_fact_id
    assert params["di_processing_run_id"] == processing_run_id
    assert params["di_extraction_profile_id"] == extraction_profile_id
    assert params["di_extraction_profile_version"] == 2
    assert params["di_invocation_id"] == invocation_id
    assert params["di_pipeline_version"] == "2.2.0"
