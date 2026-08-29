from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from audit_core import evidence_read
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


def test_persist_refresh_preserves_role_collision_and_exact_di_lineage(monkeypatch) -> None:
    tenant_id = "tenant-schema-v2"
    evidence_id = UUID("10a05b50-9cb0-4a8d-a076-aa68984125c4")
    journey_id = UUID("4531e74c-74b0-4b12-aefd-910567576084")
    canonical_field_id = "5ee16285-22af-43fd-a997-0c9ca4ad9caf"
    extraction_profile_id = UUID("62b3184d-a148-4c8e-b96e-c14643409e54")

    subject_extracted_fact_id = UUID("da54dadf-508e-451f-be14-bdf3308fcb09")
    subject_processing_run_id = UUID("d07f31c6-9709-4262-908b-fe076e64064d")
    subject_invocation_id = UUID("75d29c7a-916c-453d-a379-af051f4b54c1")
    exchange_extracted_fact_id = UUID("57484582-0e0a-464b-8c87-31048a60ddb5")
    exchange_processing_run_id = UUID("5ec51c70-b947-4ae0-9565-459957d27f89")
    exchange_invocation_id = UUID("5abb26e0-eabf-4c69-bb44-99ad6f1abdd3")

    connection = _CaptureConnection()
    engine = _CaptureEngine(connection)

    monkeypatch.setattr(evidence_read, "set_tenant_context", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        evidence_read,
        "_evidence_row",
        lambda *_args, **_kwargs: {
            "evidence_id": evidence_id,
            "journey_id": journey_id,
            "document_type_key": "cost_sheet",
            "evidence_purpose": "BOOKING_AUDIT",
            "processing_status_cache": "PROCESSING",
            "verification_status_cache": "PENDING",
            "linked_at_utc": datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
        },
    )
    monkeypatch.setattr(evidence_read, "_fact_rows", lambda *_args, **_kwargs: [])

    subject = DiLineageFact(
        canonical_field_id=canonical_field_id,
        field_key="chassis_number",
        value="SUBJECT-VIN-001",
        value_source="MACHINE",
        confidence_score=98.0,
        version_no=2,
        fact_role="SUBJECT_VEHICLE",
        extraction_key="subject_chassis_number",
        extracted_fact_id=subject_extracted_fact_id,
        processing_run_id=subject_processing_run_id,
        extraction_profile_id=extraction_profile_id,
        extraction_profile_version=2,
        invocation_id=subject_invocation_id,
        pipeline_version="2.2.0",
        page_no=1,
        evidence_region=None,
    )
    exchange = DiLineageFact(
        canonical_field_id=canonical_field_id,
        field_key="chassis_number",
        value="EXCHANGE-VIN-009",
        value_source="MACHINE",
        confidence_score=96.0,
        version_no=3,
        fact_role="EXCHANGE_VEHICLE",
        extraction_key="exchange_chassis_number",
        extracted_fact_id=exchange_extracted_fact_id,
        processing_run_id=exchange_processing_run_id,
        extraction_profile_id=extraction_profile_id,
        extraction_profile_version=2,
        invocation_id=exchange_invocation_id,
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
        facts=(subject, exchange),
    )

    insert_calls = [
        (sql, params)
        for sql, params in connection.calls
        if "INSERT INTO auditcore.evidence_facts" in sql
    ]
    assert len(insert_calls) == 2

    for sql, _params in insert_calls:
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

    by_role = {params["fact_role"]: params for _sql, params in insert_calls}
    assert set(by_role) == {"SUBJECT_VEHICLE", "EXCHANGE_VEHICLE"}

    subject_params = by_role["SUBJECT_VEHICLE"]
    exchange_params = by_role["EXCHANGE_VEHICLE"]
    assert subject_params["field_key"] == exchange_params["field_key"] == "chassis_number"
    assert subject_params["di_field_reference"] == exchange_params["di_field_reference"]
    assert subject_params["value_json"] != exchange_params["value_json"]

    assert subject_params["di_value_version_no"] == 2
    assert subject_params["di_extracted_fact_id"] == subject_extracted_fact_id
    assert subject_params["di_processing_run_id"] == subject_processing_run_id
    assert subject_params["di_extraction_profile_id"] == extraction_profile_id
    assert subject_params["di_extraction_profile_version"] == 2
    assert subject_params["di_invocation_id"] == subject_invocation_id
    assert subject_params["di_pipeline_version"] == "2.2.0"

    assert exchange_params["di_value_version_no"] == 3
    assert exchange_params["di_extracted_fact_id"] == exchange_extracted_fact_id
    assert exchange_params["di_processing_run_id"] == exchange_processing_run_id
    assert exchange_params["di_extraction_profile_id"] == extraction_profile_id
    assert exchange_params["di_extraction_profile_version"] == 2
    assert exchange_params["di_invocation_id"] == exchange_invocation_id
    assert exchange_params["di_pipeline_version"] == "2.2.0"
