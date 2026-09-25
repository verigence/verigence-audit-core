from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.uc03_document_sync_recovery import (
    _find_stale_document_syncs,
    _on_demand_checked_until,
    dispatch_stale_document_sync_recovery_on_demand,
)


@pytest.fixture
def journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for document-sync recovery integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-sync-recovery-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"SR-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"SR-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'SR', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"SR-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"SR-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"SR-O-{suffix}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"SR-J-{suffix}"},
        ).scalar_one()
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_id = journey_id  # type: ignore[attr-defined]
        c.customer_id = customer_id  # type: ignore[attr-defined]
        yield c
    engine.dispose()


def _insert_evidence(
    connection,
    *,
    tenant_id: str,
    journey_id,
    customer_id,
    processing_status_cache: str | None,
    linked_minutes_ago: int,
    process_area: str = "DELIVERY",
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.evidence (
                tenant_id, journey_id, customer_id, di_subject_id, di_document_id,
                document_type_key, evidence_purpose, process_area, association_status,
                processing_status_cache, linked_at_utc
            ) VALUES (
                :t, :j, :cu, :subj, :doc, 'dealer_receipt', 'DELIVERY_CAPTURE', :area,
                'ACTIVE', :cache, now() - (:minutes || ' minutes')::interval
            )
            """
        ),
        {
            "t": tenant_id,
            "j": journey_id,
            "cu": customer_id,
            "subj": uuid4(),
            "doc": uuid4(),
            "area": process_area,
            "cache": processing_status_cache,
            "minutes": linked_minutes_ago,
        },
    )


def test_finds_stale_unsynced_evidence_older_than_the_threshold(journey) -> None:
    _insert_evidence(
        journey, tenant_id=journey.tenant_id, journey_id=journey.journey_id, customer_id=journey.customer_id,
        processing_status_cache=None, linked_minutes_ago=15,
    )
    rows = _find_stale_document_syncs(journey, tenant_id=journey.tenant_id)
    assert len(rows) == 1
    assert rows[0]["journey_id"] == journey.journey_id
    assert rows[0]["process_area"] == "DELIVERY"


def test_ignores_evidence_still_within_the_retry_budget_window(journey) -> None:
    _insert_evidence(
        journey, tenant_id=journey.tenant_id, journey_id=journey.journey_id, customer_id=journey.customer_id,
        processing_status_cache=None, linked_minutes_ago=2,
    )
    rows = _find_stale_document_syncs(journey, tenant_id=journey.tenant_id)
    assert rows == []


def test_ignores_evidence_that_already_has_a_processing_status(journey) -> None:
    _insert_evidence(
        journey, tenant_id=journey.tenant_id, journey_id=journey.journey_id, customer_id=journey.customer_id,
        processing_status_cache="PROCESSED", linked_minutes_ago=30,
    )
    rows = _find_stale_document_syncs(journey, tenant_id=journey.tenant_id)
    assert rows == []


def test_on_demand_cooldown_skips_the_db_check_within_the_window(monkeypatch) -> None:
    _on_demand_checked_until.clear()
    calls: list[str] = []

    def _fake_find(connection, *, tenant_id, journey_id=None, limit=200):
        calls.append(tenant_id)
        return []

    monkeypatch.setattr(
        "audit_core.uc03_document_sync_recovery._find_stale_document_syncs", _fake_find,
    )

    class _FakeBackgroundTasks:
        def add_task(self, *args, **kwargs):
            raise AssertionError("should not schedule anything when nothing is stale")

    journey_id = uuid4()
    dispatch_stale_document_sync_recovery_on_demand(
        object(), _FakeBackgroundTasks(), object(), tenant_id="tenant-a", journey_id=journey_id,
    )
    dispatch_stale_document_sync_recovery_on_demand(
        object(), _FakeBackgroundTasks(), object(), tenant_id="tenant-a", journey_id=journey_id,
    )
    assert calls == ["tenant-a"]  # second call was skipped by the cooldown
