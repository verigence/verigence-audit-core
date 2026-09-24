from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.uc03_delivery_commands import (
    _complete_open_vehicle_photos_task,
    _ensure_vehicle_photos_task,
)
from audit_core.vehicle_photo_storage import (
    VehiclePhotoStorage,
    VehiclePhotoStorageSettings,
)


@pytest.fixture
def journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for vehicle-photos task integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-vp-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"VP-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"VP-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'VP', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"VP-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"VP-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"VP-O-{suffix}"},
        ).scalar_one()
        customer_id = c.execute(
            text("""INSERT INTO auditcore.customers
                (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = c.execute(
            text("""INSERT INTO auditcore.journeys
                (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"VP-J-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.journey_stage_states
                (tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                 first_started_at_utc, latest_activity_at_utc, version_no)
                VALUES (:t, :j, 'DELIVERY', 'DELIVERY_STARTED', 'NOT_STARTED', 'NOT_EVALUATED',
                        now(), now(), 1)"""),
            {"t": tenant_id, "j": journey_id},
        )
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_id = journey_id  # type: ignore[attr-defined]
        yield c
    engine.dispose()


def _requirement(key: str, *, level: str = "REQUIRED", status: str = "PENDING") -> dict:
    return {"requirement_key": key, "requirement_level": level, "requirement_status": status}


def _doc(key: str, *, status: str = "CLASSIFIED") -> dict:
    return {"requirement_key": key, "capture_status": status}


def _open_task_count(connection, *, tenant_id: str, journey_id) -> int:
    return connection.execute(
        text(
            """
            SELECT count(*) FROM auditcore.workflow_tasks
            WHERE tenant_id=:t AND journey_id=:j AND task_type='DELIVERY_VEHICLE_PHOTOS_MISSING'
              AND task_status NOT IN ('COMPLETED', 'CANCELLED', 'FAILED', 'DEAD_LETTER')
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()


def test_raises_a_high_task_when_required_docs_done_and_no_photos(journey) -> None:
    _ensure_vehicle_photos_task(
        journey,
        tenant_id=journey.tenant_id,
        journey_id=journey.journey_id,
        requirements=[_requirement("gate_pass"), _requirement("customer_ledger")],
        audit_documents=[_doc("gate_pass"), _doc("customer_ledger")],
        correlation_id="",
    )
    assert _open_task_count(journey, tenant_id=journey.tenant_id, journey_id=journey.journey_id) == 1


def test_is_a_noop_when_a_required_document_is_still_missing(journey) -> None:
    _ensure_vehicle_photos_task(
        journey,
        tenant_id=journey.tenant_id,
        journey_id=journey.journey_id,
        requirements=[_requirement("gate_pass"), _requirement("customer_ledger")],
        audit_documents=[_doc("gate_pass")],
        correlation_id="",
    )
    assert _open_task_count(journey, tenant_id=journey.tenant_id, journey_id=journey.journey_id) == 0


def test_is_a_noop_when_vehicle_photos_already_exist(journey) -> None:
    journey.execute(
        text(
            """
            INSERT INTO auditcore.delivery_vehicle_photos
                (tenant_id, journey_id, object_key, original_filename, content_type,
                 size_bytes, uploaded_by_actor_id)
            VALUES (:t, :j, 'k', 'f.jpg', 'image/jpeg', 100, 'actor-1')
            """
        ),
        {"t": journey.tenant_id, "j": journey.journey_id},
    )
    _ensure_vehicle_photos_task(
        journey,
        tenant_id=journey.tenant_id,
        journey_id=journey.journey_id,
        requirements=[_requirement("gate_pass")],
        audit_documents=[_doc("gate_pass")],
        correlation_id="",
    )
    assert _open_task_count(journey, tenant_id=journey.tenant_id, journey_id=journey.journey_id) == 0


def test_never_raises_a_second_open_task(journey) -> None:
    for _ in range(3):
        _ensure_vehicle_photos_task(
            journey,
            tenant_id=journey.tenant_id,
            journey_id=journey.journey_id,
            requirements=[_requirement("gate_pass")],
            audit_documents=[_doc("gate_pass")],
            correlation_id="",
        )
    assert _open_task_count(journey, tenant_id=journey.tenant_id, journey_id=journey.journey_id) == 1


def test_complete_open_task_marks_it_completed_and_allows_a_fresh_one_later(journey) -> None:
    _ensure_vehicle_photos_task(
        journey,
        tenant_id=journey.tenant_id,
        journey_id=journey.journey_id,
        requirements=[_requirement("gate_pass")],
        audit_documents=[_doc("gate_pass")],
        correlation_id="",
    )
    assert _open_task_count(journey, tenant_id=journey.tenant_id, journey_id=journey.journey_id) == 1

    _complete_open_vehicle_photos_task(
        journey, tenant_id=journey.tenant_id, journey_id=journey.journey_id, actor_id="pc-1",
    )
    assert _open_task_count(journey, tenant_id=journey.tenant_id, journey_id=journey.journey_id) == 0

    status = journey.execute(
        text(
            """
            SELECT task_status FROM auditcore.workflow_tasks
            WHERE tenant_id=:t AND journey_id=:j AND task_type='DELIVERY_VEHICLE_PHOTOS_MISSING'
            """
        ),
        {"t": journey.tenant_id, "j": journey.journey_id},
    ).scalar_one()
    assert status == "COMPLETED"

    # Self-healing: the gap is still open (no photo was ever actually
    # uploaded, just manually resolved) -- re-running must raise a fresh
    # task, and its effect_key must not collide with the completed one's.
    _ensure_vehicle_photos_task(
        journey,
        tenant_id=journey.tenant_id,
        journey_id=journey.journey_id,
        requirements=[_requirement("gate_pass")],
        audit_documents=[_doc("gate_pass")],
        correlation_id="",
    )
    assert _open_task_count(journey, tenant_id=journey.tenant_id, journey_id=journey.journey_id) == 1


def test_not_applicable_requirements_are_excluded_from_the_gate(journey) -> None:
    _ensure_vehicle_photos_task(
        journey,
        tenant_id=journey.tenant_id,
        journey_id=journey.journey_id,
        requirements=[
            _requirement("gate_pass"),
            _requirement("gst_certificate", status="NOT_APPLICABLE"),
        ],
        audit_documents=[_doc("gate_pass")],
        correlation_id="",
    )
    assert _open_task_count(journey, tenant_id=journey.tenant_id, journey_id=journey.journey_id) == 1


def test_vehicle_photo_storage_put_and_presign_use_the_configured_bucket(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class _FakeClient:
        def put_object(self, **kwargs):
            calls["put_object"] = kwargs

        def generate_presigned_url(self, operation, **kwargs):
            calls["generate_presigned_url"] = (operation, kwargs)
            return "https://example.test/signed"

    storage = VehiclePhotoStorage(
        VehiclePhotoStorageSettings(
            endpoint_url="https://r2.example.test",
            access_key_id="key",
            secret_access_key="secret",
            bucket="vehicle-photos-bucket",
        )
    )
    monkeypatch.setattr(storage, "_client", lambda: _FakeClient())

    storage.put_object("tenant/photo.jpg", b"bytes", "image/jpeg")
    assert calls["put_object"]["Bucket"] == "vehicle-photos-bucket"
    assert calls["put_object"]["Key"] == "tenant/photo.jpg"
    assert calls["put_object"]["ContentType"] == "image/jpeg"

    url = storage.get_presigned_url("tenant/photo.jpg", expires_seconds=60)
    assert url == "https://example.test/signed"
    operation, kwargs = calls["generate_presigned_url"]
    assert operation == "get_object"
    assert kwargs["Params"] == {"Bucket": "vehicle-photos-bucket", "Key": "tenant/photo.jpg"}
    assert kwargs["ExpiresIn"] == 60
