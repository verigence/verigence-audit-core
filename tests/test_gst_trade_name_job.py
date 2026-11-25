"""GST_TRADE_NAME (2026-11-25): a sole proprietor's booking named before the rule
by the owner's own name is named by the business; a dry run changes nothing."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import add_ready_document, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_gst_trade_name import run_gst_trade_name
from audit_core.uc03_p2_customer import is_individual_gstin, proprietor_trade_name


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2gtn")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _name(journey) -> str:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT c.legal_name FROM auditcore.journeys j JOIN auditcore.customers c "
                 "ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id WHERE j.tenant_id=:t AND j.journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id}).scalar_one()


def _named_before_the_rule(journey, name: str) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text(
            "UPDATE auditcore.customers c SET legal_name=:n, legal_name_status='VERIFIED' FROM auditcore.journeys j "
            "WHERE j.tenant_id=c.tenant_id AND j.customer_id=c.customer_id AND j.tenant_id=:t AND j.journey_id=:j"),
            {"n": name, "t": journey.tenant_id, "j": journey.journey_id})


def test_individual_gstin():
    assert is_individual_gstin("21ARPPS0866A1ZX")
    assert not is_individual_gstin("21AAFCJ2048H1ZY")  # a company
    assert not is_individual_gstin("No")
    assert proprietor_trade_name(gstin="21ARPPS0866A1ZX", legal_name="TEST OWNER", trade_name="TEST STORES") == \
        "TEST STORES"
    assert proprietor_trade_name(gstin="21ARPPS0866A1ZX", legal_name="TEST OWNER", trade_name=" test  owner ") is None


def test_dry_run_then_commit(journey):
    add_ready_document(journey, "pan_card", pan_number="ARPPS0866A", pan_name="TEST OWNER NAME")
    add_ready_document(journey, "gst_certificate", gstin="21ARPPS0866A1ZX", organisation_legal_name="TEST OWNER NAME",
                       organisation_trade_name="TEST CATERERS AND DECORATORS")
    _named_before_the_rule(journey, "TEST OWNER NAME")
    dry = run_gst_trade_name(journey.engine, f"{journey.tenant_id}:trial", [])
    assert dry == {"commit": False, "proprietorBookings": 1, "renamed": 1, "namedByTradeName": 1,
                   "settleRequested": 0, "failed": 0}
    assert _name(journey) == "TEST OWNER NAME"
    done = run_gst_trade_name(journey.engine, f"{journey.tenant_id}:trial-commit", [])
    assert done["renamed"] == 1 and done["settleRequested"] == 1 and done["failed"] == 0
    assert _name(journey) == "TEST CATERERS AND DECORATORS"
    again = run_gst_trade_name(journey.engine, f"{journey.tenant_id}:again", [])
    assert again["renamed"] == 0 and again["namedByTradeName"] == 1
