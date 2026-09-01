from pathlib import Path
from uuid import uuid4

import pytest

from audit_core.uc03_di_core_persistence import (
    ReviewedDiField,
    has_persistable_value,
    persist_reviewed_di_fields,
)


class _RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return object()


def _v2_field(*, value, field_key: str = "future_field") -> ReviewedDiField:
    return ReviewedDiField(
        document_id=uuid4(),
        field_key=field_key,
        source_fact_version=1,
        source_canonical_field_id=str(uuid4()),
        source_document_type_key="future_document",
        extracted_value=value,
        effective_value=value,
        confidence_score=98.5,
        confidence_scale="PERCENT",
    )


def test_populated_rule_keeps_zero_and_false_but_skips_empty_values() -> None:
    assert has_persistable_value(0) is True
    assert has_persistable_value(False) is True
    assert has_persistable_value("value") is True
    assert has_persistable_value([0]) is True

    assert has_persistable_value(None) is False
    assert has_persistable_value("") is False
    assert has_persistable_value("   ") is False
    assert has_persistable_value([]) is False
    assert has_persistable_value({}) is False


def test_v2_persistence_keeps_every_populated_field_without_typed_owner() -> None:
    connection = _RecordingConnection()
    journey_id = uuid4()
    fields = [
        _v2_field(value="unmapped-value", field_key="future_unmapped"),
        _v2_field(value=0, field_key="zero_value"),
        _v2_field(value=False, field_key="false_value"),
        _v2_field(value="", field_key="empty_value"),
        _v2_field(value=None, field_key="null_value"),
    ]

    stored = persist_reviewed_di_fields(
        connection,
        tenant_id="tenant-a",
        journey_id=journey_id,
        stage_code="BOOKING",
        actor_id="pc-user",
        fields=fields,
    )

    assert stored == 3
    assert len(connection.calls) == 1
    sql, params = connection.calls[0]
    assert "source_canonical_field_id" in sql
    assert "WHERE source_canonical_field_id IS NOT NULL" in sql
    assert isinstance(params, list)
    assert {row["field_key"] for row in params} == {
        "future_unmapped",
        "zero_value",
        "false_value",
    }
    assert all(row["stage_code"] == "BOOKING" for row in params)
    assert all(row["confidence_scale"] == "PERCENT" for row in params)


def test_modified_field_persists_original_and_confirmed_effective_value() -> None:
    connection = _RecordingConnection()
    original = "wrong"
    corrected = "correct"
    field = ReviewedDiField(
        document_id=uuid4(),
        field_key="customer_name",
        source_fact_version=7,
        source_fact_ref=uuid4(),
        evidence_id=uuid4(),
        extracted_value=original,
        modified_value=corrected,
        effective_value=corrected,
        confidence_score=0.81,
        confidence_scale="UNIT_INTERVAL",
        is_modified=True,
    )

    stored = persist_reviewed_di_fields(
        connection,
        tenant_id="tenant-a",
        journey_id=uuid4(),
        stage_code="BOOKING",
        actor_id="pc-user",
        fields=[field],
    )

    assert stored == 1
    sql, params = connection.calls[0]
    assert "source_fact_ref" in sql
    assert "source_canonical_field_id IS NOT NULL" not in sql
    assert isinstance(params, list) and len(params) == 1
    row = params[0]
    assert row["extracted_value"] == '"wrong"'
    assert row["modified_value"] == '"correct"'
    assert row["effective_value"] == '"correct"'
    assert row["is_modified"] is True
    assert row["confidence_scale"] == "UNIT_INTERVAL"


def test_correction_from_empty_original_is_not_dropped() -> None:
    connection = _RecordingConnection()
    field = ReviewedDiField(
        document_id=uuid4(),
        field_key="corrected_from_empty",
        source_fact_version=1,
        source_fact_ref=uuid4(),
        evidence_id=uuid4(),
        extracted_value="",
        modified_value="now populated",
        effective_value="now populated",
        confidence_score=None,
        confidence_scale=None,
        is_modified=True,
    )

    assert (
        persist_reviewed_di_fields(
            connection,
            tenant_id="tenant-a",
            journey_id=uuid4(),
            stage_code="BOOKING",
            actor_id="pc-user",
            fields=[field],
        )
        == 1
    )


def test_rejected_field_can_keep_original_provenance_without_effective_value() -> None:
    connection = _RecordingConnection()
    field = ReviewedDiField(
        document_id=uuid4(),
        field_key="rejected_unknown",
        source_fact_version=3,
        source_canonical_field_id=str(uuid4()),
        extracted_value="machine-value",
        effective_value=None,
        effective_value_is_set=False,
        confidence_score=55.0,
        confidence_scale="PERCENT",
    )

    assert (
        persist_reviewed_di_fields(
            connection,
            tenant_id="tenant-a",
            journey_id=uuid4(),
            stage_code="DELIVERY",
            actor_id="pc-user",
            fields=[field],
        )
        == 1
    )
    _, params = connection.calls[0]
    assert isinstance(params, list)
    assert params[0]["extracted_value"] == '"machine-value"'
    assert params[0]["effective_value"] is None
    assert params[0]["stage_code"] == "DELIVERY"


def test_persistence_refuses_to_guess_missing_field_identity_or_confidence_scale() -> None:
    connection = _RecordingConnection()
    with pytest.raises(ValueError, match="requires either legacy source_fact_ref"):
        persist_reviewed_di_fields(
            connection,
            tenant_id="tenant-a",
            journey_id=uuid4(),
            stage_code="BOOKING",
            actor_id="pc-user",
            fields=[
                ReviewedDiField(
                    document_id=uuid4(),
                    field_key="unknown",
                    source_fact_version=1,
                    extracted_value="value",
                    effective_value="value",
                )
            ],
        )

    with pytest.raises(ValueError, match="explicit confidence_scale"):
        persist_reviewed_di_fields(
            connection,
            tenant_id="tenant-a",
            journey_id=uuid4(),
            stage_code="BOOKING",
            actor_id="pc-user",
            fields=[
                ReviewedDiField(
                    document_id=uuid4(),
                    field_key="unknown",
                    source_fact_version=1,
                    source_canonical_field_id=str(uuid4()),
                    extracted_value="value",
                    effective_value="value",
                    confidence_score=92.0,
                )
            ],
        )


def test_0051_migration_reuses_generic_table_and_preserves_v1_compatibility() -> None:
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0051_uc03_lossless_review_fields.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision = "0050_uc03_commercial_components"' in migration
    assert "ALTER COLUMN evidence_id DROP NOT NULL" in migration
    assert "ALTER COLUMN source_fact_ref DROP NOT NULL" in migration
    assert "ADD COLUMN stage_code" in migration
    assert "ADD COLUMN source_canonical_field_id" in migration
    assert "ADD COLUMN effective_value jsonb" in migration
    assert "ADD COLUMN confidence_scale" in migration
    assert "uq_journey_document_extracted_fields_v2_fact" in migration
    assert "CREATE TABLE" not in migration
    assert "No fake legacy UUIDs are manufactured" in migration
