from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core import uc03_backfill_document_sync_producers as backfill


@pytest.fixture
def seeded_tenant():
    """Unlike the usual `journey` fixture (yields one open Connection), the
    backfill function manages its own per-Journey transactions against an
    Engine -- seed data with a Connection, then hand back the Engine."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for backfill integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-bf-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"BF-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"BF-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'BF', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"BF-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"BF-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"BF-O-{suffix}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"BF-J-{suffix}"},
        ).scalar_one()
    # The setup transaction above must commit before any other connection
    # (the backfill function opens its own) can see this journey row.
    yield {"engine": engine, "tenant_id": tenant_id, "journey_id": journey_id}
    engine.dispose()


def _seed_field(engine, *, tenant_id, journey_id, stage_code, document_type_key, field_key, value):
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.execute(
            text(
                """
                INSERT INTO auditcore.journey_document_extracted_fields (
                    tenant_id, journey_id, evidence_id, di_document_id,
                    source_fact_ref, source_fact_version, stage_code,
                    source_document_type_key, source_canonical_field_id, field_key,
                    extracted_value, effective_value, is_modified
                ) VALUES (
                    :t, :j, NULL, :doc,
                    NULL, 1, :stage,
                    :dtk, NULL, :fk,
                    CAST(:v AS jsonb), CAST(:v AS jsonb), false
                )
                """
            ),
            {"t": tenant_id, "j": journey_id, "doc": uuid4(), "stage": stage_code,
             "dtk": document_type_key, "fk": field_key, "v": json.dumps(value)},
        )


def test_backfill_finds_nothing_when_no_documents_seeded(seeded_tenant) -> None:
    result = backfill.backfill_document_sync_producers_for_tenant(
        seeded_tenant["engine"], tenant_id=seeded_tenant["tenant_id"],
    )
    assert result["journeysConsidered"] == 0
    assert result["journeysProcessed"] == 0
    assert result["journeysFailed"] == []


def test_backfill_raises_wrong_document_for_an_old_journey(seeded_tenant) -> None:
    # Simulates a Journey whose documents were confirmed before the
    # identity-consistency producer existed: a KYC document and a
    # mismatching Booking Form already sit in durable storage, but nothing
    # has ever re-triggered the sync pipeline for them.
    engine, tenant_id, journey_id = (
        seeded_tenant["engine"], seeded_tenant["tenant_id"], seeded_tenant["journey_id"],
    )
    _seed_field(engine, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING",
                document_type_key="aadhaar", field_key="aadhaar_name", value="Sanjaya Kumar Mohanty")
    _seed_field(engine, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING",
                document_type_key="booking_form", field_key="customer_name", value="Priya Nair")

    result = backfill.backfill_document_sync_producers_for_tenant(engine, tenant_id=tenant_id)

    assert result["journeysConsidered"] == 1
    assert result["journeysProcessed"] == 1
    assert result["journeysFailed"] == []
    assert result["identityFindingsRaised"] == 1

    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        count = c.execute(
            text("SELECT count(*) FROM auditcore.audit_findings "
                 "WHERE tenant_id=:t AND journey_id=:j AND finding_type_code='WRONG_DOCUMENT' "
                 "AND finding_status IN ('OPEN','ACKNOWLEDGED')"),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one()
    assert count == 1


def test_backfill_sets_delivery_date_for_an_old_journey(seeded_tenant) -> None:
    engine, tenant_id, journey_id = (
        seeded_tenant["engine"], seeded_tenant["tenant_id"], seeded_tenant["journey_id"],
    )
    _seed_field(engine, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
                document_type_key="gate_pass", field_key="delivery_date", value="2026-08-05")

    result = backfill.backfill_document_sync_producers_for_tenant(engine, tenant_id=tenant_id)

    assert result["journeysProcessed"] == 1
    assert result["deliveryMaterializations"] == 1

    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        row = c.execute(
            text("SELECT actual_delivered_at, status_source FROM auditcore.deliveries "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"t": tenant_id, "j": journey_id},
        ).mappings().one()
    assert row["status_source"] == "EVIDENCE"
    assert row["actual_delivered_at"].date().isoformat() == "2026-08-05"


def test_backfill_is_idempotent_on_rerun(seeded_tenant) -> None:
    engine, tenant_id, journey_id = (
        seeded_tenant["engine"], seeded_tenant["tenant_id"], seeded_tenant["journey_id"],
    )
    _seed_field(engine, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING",
                document_type_key="dealer_receipt", field_key="dealer_name", value="Some Other Motors")

    first = backfill.backfill_document_sync_producers_for_tenant(engine, tenant_id=tenant_id)
    assert first["identityFindingsRaised"] == 1

    second = backfill.backfill_document_sync_producers_for_tenant(engine, tenant_id=tenant_id)
    # "raised" reports the mismatch still being true on this run, same as
    # every other producer's own convention (see uc03_customer_identity_
    # consistency.py) -- it is not "raised" in the sense of "newly inserted".
    # The real idempotency guarantee is _machine_flag's own: re-running
    # never creates a second row for the same still-open rule_key.
    assert second["identityFindingsRaised"] == 1
    assert second["journeysFailed"] == []

    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        count = c.execute(
            text("SELECT count(*) FROM auditcore.audit_findings "
                 "WHERE tenant_id=:t AND journey_id=:j AND finding_type_code='WRONG_DOCUMENT' "
                 "AND finding_status IN ('OPEN','ACKNOWLEDGED')"),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one()
    assert count == 1


def test_one_journeys_failure_does_not_block_the_rest(seeded_tenant, monkeypatch) -> None:
    engine, tenant_id, journey_id = (
        seeded_tenant["engine"], seeded_tenant["tenant_id"], seeded_tenant["journey_id"],
    )
    _seed_field(engine, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING",
                document_type_key="dealer_receipt", field_key="dealer_name", value="Some Other Motors")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(backfill, "materialize_delivery_documents_from_durable_store", _boom)

    result = backfill.backfill_document_sync_producers_for_tenant(engine, tenant_id=tenant_id)

    assert result["journeysConsidered"] == 1
    assert result["journeysProcessed"] == 0
    assert result["journeysFailed"] == [str(journey_id)]
