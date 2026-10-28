"""The car sold's own RC is not the exchange RC (2026-10-28).

ADI-BM-261026-002: the Bolero Maxx pick-up's Temporary Certificate of
Registration ("T1026OR9323A") was typed as the Exchange Vehicle RC and made the
exchange scheme's old-vehicle condition true on the Deal."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import add_ready_document, create_p2_journey, database_engine

from audit_core.db import set_tenant_context
from audit_core.uc03_deal_reconciliation import _old_vehicle_facts
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_stage import ready_document_count
from audit_core.uc03_vehicle_rc import exchange_rc_on_file, new_vehicle_rcs


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="vrc")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _check(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        args = {"tenant_id": journey.tenant_id, "journey_id": journey.journey_id}
        template = get_registry().documents["vehicle_rc"]
        return (new_vehicle_rcs(connection, **args), exchange_rc_on_file(connection, **args),
                ready_document_count(connection, template=template, **args), _old_vehicle_facts(connection, **args))


def test_a_temporary_registration_is_the_car_solds(journey):
    rc = add_ready_document(journey, "vehicle_rc", process_area="DELIVERY", vehicle_registration_number="T1026OR9323A",
                            vehicle_model="BOL MAXX PUP HD 2.0L SXI CBC", registration_date="2026-10-05")
    sold, exchange, ready, old = _check(journey)
    assert sold == {str(rc)} and exchange is False and ready == 0 and old == []


def test_an_rc_with_the_invoiced_engine_number_is_the_car_solds(journey):
    add_ready_document(journey, "customer_invoice_dms", process_area="DELIVERY", engine_number="TTT1J19678")
    rc = add_ready_document(journey, "vehicle_rc", process_area="DELIVERY", vehicle_registration_number="OD05AB1234",
                            engine_number="TTT1J19678", registration_date="2019-02-01")
    sold, exchange, ready, _ = _check(journey)
    assert sold == {str(rc)} and exchange is False and ready == 0


def test_an_old_vehicles_rc_is_the_exchange_rc(journey):
    add_ready_document(journey, "customer_invoice_dms", process_area="DELIVERY", engine_number="TTT1J19678")
    add_ready_document(journey, "vehicle_rc", process_area="DELIVERY", vehicle_registration_number="OD02 AK 4455",
                       engine_number="G13BN123456", vehicle_model="SWIFT DZIRE VXI", registration_date="2016-05-11")
    sold, exchange, ready, old = _check(journey)
    assert sold == set() and exchange is True and ready == 1 and old == [{"model": "SWIFT DZIRE VXI"}]
