from __future__ import annotations

from uuid import uuid4

from audit_core import uc03_confidence_review_policy as confidence_policy
from audit_core import uc03_post_extraction_materialization as post_extract


def test_successful_late_sync_triggers_canonical_materialization(monkeypatch) -> None:
    journey_id = uuid4()
    document_id = uuid4()
    connection = object()
    calls: list[tuple[object, str, object]] = []

    def original_sync(*args, **kwargs):
        return 7

    def materialize(connection_arg, *, tenant_id, journey_id):
        calls.append((connection_arg, tenant_id, journey_id))
        return {
            "operationalOwners": 2,
            "commercialLines": 3,
            "productFields": 1,
            "paymentsCreated": 1,
            "paymentsUpdated": 0,
            "paymentsUnchanged": 0,
            "typedValuesSkipped": 0,
        }

    monkeypatch.setattr(confidence_policy, "_sync_booking_document", original_sync)
    monkeypatch.setattr(
        confidence_policy,
        "_post_extraction_materialization_installed",
        False,
        raising=False,
    )
    monkeypatch.setattr(post_extract, "materialize_machine_booking_values", materialize)

    post_extract.install_uc03_post_extraction_materialization()
    result = confidence_policy._sync_booking_document(
        connection,
        tenant_id="tenant-1",
        journey_id=journey_id,
        document_id=document_id,
        service_id="di-service",
        security_client=object(),
        di_client=object(),
        bump_version=True,
    )

    assert result == 7
    assert calls == [(connection, "tenant-1", journey_id)]


def test_pending_sync_does_not_run_canonical_materialization(monkeypatch) -> None:
    journey_id = uuid4()
    materialized = False

    def original_sync(*args, **kwargs):
        return 0

    def materialize(*args, **kwargs):
        nonlocal materialized
        materialized = True
        return {}

    monkeypatch.setattr(confidence_policy, "_sync_booking_document", original_sync)
    monkeypatch.setattr(
        confidence_policy,
        "_post_extraction_materialization_installed",
        False,
        raising=False,
    )
    monkeypatch.setattr(post_extract, "materialize_machine_booking_values", materialize)

    post_extract.install_uc03_post_extraction_materialization()
    result = confidence_policy._sync_booking_document(
        object(),
        tenant_id="tenant-1",
        journey_id=journey_id,
        document_id=uuid4(),
        service_id="di-service",
        security_client=object(),
        di_client=object(),
        bump_version=True,
    )

    assert result == 0
    assert materialized is False


def test_low_confidence_does_not_remove_machine_effective_value() -> None:
    row = {
        "confidenceScore": 84.0,
        "confidenceScale": "PERCENT",
        "effectiveValue": "450000",
        "hasEffectiveValue": True,
    }
    assert post_extract._confidence_percent(row) == 84.0
    assert row["effectiveValue"] == "450000"
    assert row["hasEffectiveValue"] is True


def test_unit_interval_confidence_is_normalized_for_identity_gate() -> None:
    row = {"confidenceScore": 0.97, "confidenceScale": "UNIT_INTERVAL"}
    assert post_extract._confidence_percent(row) == 97.0
