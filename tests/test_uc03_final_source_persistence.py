import json
from pathlib import Path
from uuid import uuid4

import pytest

from audit_core.uc03_final_source_persistence import (
    record_post_delivery_reviewed_resolution,
    record_post_delivery_typed_resolution,
)


class _MappingsResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self._row


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _Connection:
    def __init__(self, *results):
        self._results = list(results)
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((str(statement), dict(params or {})))
        return self._results.pop(0)


def test_reviewed_resolution_snapshots_persisted_effective_value_and_provenance() -> None:
    journey_id = uuid4()
    reviewed_field_id = uuid4()
    document_id = uuid4()
    evidence_id = uuid4()
    resolution_id = uuid4()
    source_row = {
        "extracted_field_id": reviewed_field_id,
        "evidence_id": evidence_id,
        "di_document_id": document_id,
        "source_canonical_field_id": "canonical.invoice.number",
        "field_key": "invoice_number",
        "source_fact_version": 3,
        "source_document_type_key": "customer_invoice_dms",
        "effective_value": "INV-123",
    }
    connection = _Connection(
        _MappingsResult(source_row),
        _ScalarResult(resolution_id),
    )

    result = record_post_delivery_reviewed_resolution(
        connection,
        tenant_id="tenant-a",
        journey_id=journey_id,
        attribute_key="dms_invoice_number",
        source_reviewed_field_id=reviewed_field_id,
        actor_id="pc-1",
        resolution_rule="FINAL_REPORT_SOURCE_TAX_INVOICE_DMS",
        owning_domain_key="VEHICLE",
        owning_record_reference="vehicle-1",
    )

    assert result == resolution_id
    source_sql, source_params = connection.calls[0]
    assert "journey_id=:journey_id" in source_sql
    assert "effective_value IS NOT NULL" in source_sql
    assert source_params["source_reviewed_field_id"] == reviewed_field_id

    insert_sql, insert_params = connection.calls[1]
    assert "'POST_DELIVERY'" in insert_sql
    assert "source_reviewed_field_id" in insert_sql
    assert insert_params["source_di_document_id"] == document_id
    assert insert_params["source_evidence_id"] == evidence_id
    assert insert_params["source_fact_version"] == 3
    assert json.loads(insert_params["resolved_value_snapshot"]) == "INV-123"


def test_reviewed_resolution_rejects_field_from_another_or_unaccepted_journey() -> None:
    connection = _Connection(_MappingsResult(None))

    with pytest.raises(ValueError, match="not an accepted Booking/Delivery value"):
        record_post_delivery_reviewed_resolution(
            connection,
            tenant_id="tenant-a",
            journey_id=uuid4(),
            attribute_key="delivery_date",
            source_reviewed_field_id=uuid4(),
            actor_id="pc-1",
            resolution_rule="FINAL_REPORT_SOURCE_GATE_PASS",
        )

    assert len(connection.calls) == 1


def test_typed_resolution_uses_owner_without_fake_di_identity_and_keeps_zero() -> None:
    resolution_id = uuid4()
    connection = _Connection(_ScalarResult(resolution_id))

    result = record_post_delivery_typed_resolution(
        connection,
        tenant_id="tenant-a",
        journey_id=uuid4(),
        attribute_key="customer_type",
        resolved_value=0,
        actor_id="pc-1",
        resolution_rule="FINAL_REPORT_SOURCE_BOOKING_RETAIL_DUMP",
        owning_domain_key="CUSTOMER",
        owning_record_reference="customer-1",
    )

    assert result == resolution_id
    sql, params = connection.calls[0]
    assert "NULL, NULL" in sql
    assert "source_reviewed_field_id" in sql
    assert json.loads(params["resolved_value_snapshot"]) == 0
    assert params["owning_domain_key"] == "CUSTOMER"
    assert params["owning_record_reference"] == "customer-1"


@pytest.mark.parametrize("value", [None, ""])
def test_typed_resolution_requires_populated_value(value) -> None:
    connection = _Connection()

    with pytest.raises(ValueError, match="populated value"):
        record_post_delivery_typed_resolution(
            connection,
            tenant_id="tenant-a",
            journey_id=uuid4(),
            attribute_key="customer_type",
            resolved_value=value,
            actor_id="pc-1",
            resolution_rule="FINAL_REPORT_SOURCE_BOOKING_RETAIL_DUMP",
            owning_domain_key="CUSTOMER",
            owning_record_reference="customer-1",
        )

    assert connection.calls == []


def test_0052_migration_enforces_same_journey_reviewed_field_reference() -> None:
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0052_uc03_final_source_resolution.py"
    ).read_text()

    assert "resolved_value_snapshot jsonb" in migration
    assert "source_reviewed_field_id uuid" in migration
    assert (
        "FOREIGN KEY (tenant_id, journey_id, source_reviewed_field_id)" in migration
    )
    assert "ALTER COLUMN source_di_document_id DROP NOT NULL" in migration
    assert "stage_code IN ('BOOKING','DELIVERY')" in migration
    assert "stage_code = 'POST_DELIVERY'" in migration
    assert "No fake DI identifiers are manufactured." in migration
