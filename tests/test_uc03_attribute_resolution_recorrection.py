"""A field corrected a second time (2026-11-06).

journey_attribute_resolutions holds one row per (journey, stage, attribute).
The second correction of the same attribute -- the booking date corrected
twice, or corrected on a second booking document -- used to insert a second
row, hit the unique key, and every edit after the first failed with
VAC-CONFLICT-000. The latest correction is now the resolution.
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_attribute_mapping import spec_for_field
from audit_core.uc03_attribute_resolution import record_attribute_resolution


def _journey(connection, tenant_id: str, suffix: str):
    category_id = connection.execute(
        text("INSERT INTO auditcore.product_categories (category_code, category_name) "
             "VALUES (:c, 'Vehicle') RETURNING product_category_id"), {"c": f"RC-PCAT-{suffix}"},
    ).scalar_one()
    oem_id = connection.execute(
        text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'RC OEM') RETURNING oem_id"),
        {"c": f"RC-OEM-{suffix}"},
    ).scalar_one()
    connection.execute(
        text("INSERT INTO auditcore.projects (tenant_id, project_code, project_name, oem_id, product_category_id, "
             "effective_start_date) VALUES (:t, :c, 'RC Project', :o, :p, CURRENT_DATE)"),
        {"t": tenant_id, "c": f"RC-P-{suffix}", "o": oem_id, "p": category_id},
    )
    dealer_id = connection.execute(
        text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) VALUES (:t, :c, 'RC Dealer') "
             "RETURNING dealer_id"), {"t": tenant_id, "c": f"RC-D-{suffix}"},
    ).scalar_one()
    outlet_id = connection.execute(
        text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
             "VALUES (:t, :d, :c, 'RC Outlet') RETURNING outlet_id"), {"t": tenant_id, "d": dealer_id, "c": f"RC-O-{suffix}"},
    ).scalar_one()
    customer_id = connection.execute(
        text("INSERT INTO auditcore.customers (tenant_id, dealer_id, outlet_id, customer_type_code, display_name) "
             "VALUES (:t, :d, :o, 'PENDING', 'RC Customer') RETURNING customer_id"),
        {"t": tenant_id, "d": dealer_id, "o": outlet_id},
    ).scalar_one()
    return connection.execute(
        text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, journey_reference) "
             "VALUES (:t, :d, :o, :c, 'RC-JOURNEY') RETURNING journey_id"),
        {"t": tenant_id, "d": dealer_id, "o": outlet_id, "c": customer_id},
    ).scalar_one()


def test_corrections_record_their_resolution_as_the_runtime_role_and_never_fail() -> None:
    """2026-11-06: ON CONFLICT DO UPDATE needs UPDATE, which the runtime role
    does not have on this insert-only ledger (migration 0037), so every field
    correction failed with VAC-SYS-001. Run as the runtime role: the first and
    a second correction both go through; the first resolution is kept."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    spec = spec_for_field("booking_date")
    assert spec is not None
    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-recorrect-{suffix}"
    with engine.begin() as connection:
        journey_id = _journey(connection, tenant_id, suffix)
    first_document, second_document = uuid4(), uuid4()
    for document_id, actor in ((first_document, "pc-1"), (second_document, "reviewer-1")):
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
            set_tenant_context(connection, tenant_id)
            record_attribute_resolution(
                connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING", spec=spec,
                source_di_document_id=document_id, source_evidence_id=None,
                source_canonical_field_id="booking_date", source_field_key="booking_date",
                source_fact_version=1, source_document_type_key="booking_form", actor_id=actor,
                owning_domain_key=None, owning_record_reference=None,
            )
    with engine.begin() as connection:
        rows = connection.execute(
            text("SELECT source_di_document_id, resolved_by_actor_id FROM auditcore.journey_attribute_resolutions "
                 "WHERE tenant_id=:t AND journey_id=:j AND attribute_key=:a"),
            {"t": tenant_id, "j": journey_id, "a": spec.attribute_key},
        ).all()
    assert len(rows) == 1
    assert rows[0].source_di_document_id == first_document
