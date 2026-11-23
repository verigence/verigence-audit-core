"""A Journey actual held by a document that no longer gives it passes to the strongest
source still on file (2026-11-23, ADI-RD-261101-051: a voided cost sheet's ex-showroom
Rs 10,30,100 stood over the NDC's Rs 10,30,000 on 274 bookings)."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import add_evidence, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_deal_source_history import (
    reconcile_commercial_actuals,
    record_source_value,
    upsert_commercial_actual,
)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="holder")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _run(journey, fn, **kwargs):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return fn(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, **kwargs)


def _sql(journey, sql, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _line(journey):
    return _sql(journey, "SELECT actual_amount, source_reference FROM auditcore.commercial_lines "
                         "WHERE tenant_id=:t AND journey_id=:j AND component_key='ex_showroom_price'").mappings().one()


def _setup(journey):
    sheet, ndc = uuid4(), uuid4()
    add_evidence(journey, di_document_id=sheet, document_type_key="cost_sheet", process_area="DELIVERY", chassis=None)
    add_evidence(journey, di_document_id=ndc, document_type_key="customer_ledger", process_area="DELIVERY",
                 chassis=None)
    _run(journey, upsert_commercial_actual, component_key="ex_showroom_price", amount=Decimal(1030100),
         document_type="cost_sheet", document_id=sheet, evidence_id=None)
    # the cost sheet ranks ahead of the NDC: it keeps the actual while it gives the figure
    assert not _run(journey, upsert_commercial_actual, component_key="ex_showroom_price", amount=Decimal(1030000),
                    document_type="customer_ledger", document_id=ndc, evidence_id=None)
    assert _line(journey)["source_reference"] == f"cost_sheet:{sheet}"
    return sheet, ndc


def test_a_voided_holder_passes_the_actual_to_the_next_source_on_file(journey):
    sheet, ndc = _setup(journey)
    assert _run(journey, reconcile_commercial_actuals) == 0  # nothing stale yet
    _sql(journey, "UPDATE auditcore.evidence SET association_status='VOIDED' WHERE tenant_id=:t AND di_document_id=:d",
         d=sheet)
    assert _run(journey, reconcile_commercial_actuals) == 1
    line = _line(journey)
    assert line["actual_amount"] == Decimal(1030000) and line["source_reference"] == f"customer_ledger:{ndc}"
    assert _run(journey, reconcile_commercial_actuals) == 0  # idempotent


def test_a_withdrawn_holder_blocks_no_new_writer(journey):
    sheet, ndc = _setup(journey)
    _sql(journey, "UPDATE auditcore.commercial_line_source_values SET withdrawn_at_utc=now() "
                  "WHERE tenant_id=:t AND source_document_id=:d", d=sheet)
    assert _run(journey, upsert_commercial_actual, component_key="ex_showroom_price", amount=Decimal(1030000),
                document_type="customer_ledger", document_id=ndc, evidence_id=None)
    assert _line(journey)["source_reference"] == f"customer_ledger:{ndc}"


def test_with_no_source_left_the_actual_is_cleared(journey):
    sheet, ndc = _setup(journey)
    _sql(journey, "UPDATE auditcore.evidence SET association_status='VOIDED' WHERE tenant_id=:t", )
    assert _run(journey, reconcile_commercial_actuals) == 1
    line = _line(journey)
    assert line["actual_amount"] is None and line["source_reference"] is None


def test_a_holder_with_no_source_record_stands(journey):
    form = uuid4()
    add_evidence(journey, di_document_id=form, document_type_key="booking_form", chassis=None)
    _run(journey, upsert_commercial_actual, component_key="ex_showroom_price", amount=Decimal(900000),
         document_type="booking_form", document_id=form, evidence_id=None, record=False)
    ndc = uuid4()
    add_evidence(journey, di_document_id=ndc, document_type_key="customer_ledger", chassis=None)
    _run(journey, record_source_value, line_kind="COMMERCIAL", component_key="ex_showroom_price",
         source_document_type="customer_ledger", amount=Decimal(1030000), evidence_id=None, document_id=ndc)
    assert _run(journey, reconcile_commercial_actuals) == 0
    assert _line(journey)["source_reference"] == f"booking_form:{form}"
