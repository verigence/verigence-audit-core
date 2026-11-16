"""A KYC held as another person's never names the customer (2026-11-16).

ADI-RD-261111-013: the loan papers carried the financier's PAN ("BAJAJ FINANCE
LIMITED", AABCB1518L). As the newer upload it replaced Prasanna Kumar Das's own
PAN; the identity check held it as another person's, yet the customer was
named from it. A held (or PC-rejected) KYC names no one, and the customer's own
KYC it pushed aside names them again. A held document's PAN pairs no booking as
the same file either."""
from __future__ import annotations

import pytest
import test_p2_file_identity as fid
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_customer import sync_customer_name


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2hk")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _legal(journey) -> str | None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT c.legal_name FROM auditcore.journeys j JOIN auditcore.customers c "
                 "ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id WHERE j.tenant_id=:t AND j.journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id}).scalar_one()


def _status(journey, evidence_id, status: str) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.evidence SET identity_check_status=:s WHERE tenant_id=:t "
                                "AND evidence_id=:e"), {"t": journey.tenant_id, "e": evidence_id, "s": status})


def _sync(journey) -> str:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return sync_customer_name(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _prasanna_then_bajaj(journey, *, with_aadhaar: bool = False):
    requirements = fid._requirements(journey)
    docs = [("pan_card", "pan_card", {"pan_number": "BHZPP3831M", "pan_name": "PRASANNA KUMAR DAS"})]
    if with_aadhaar:
        docs.append(("aadhaar", "aadhaar", {"aadhaar_number": "4321 8765 1234", "aadhaar_name": "Prasanna Kumar Das"}))
    docs.append(("pan_card", "pan_card", {"pan_number": "AABCB1518L", "pan_name": "BAJAJ FINANCE LIMITED"}))
    return fid._upload(journey, requirements, "Prasanna Kumar Das.pdf", 0, docs)


@pytest.mark.parametrize("hold", ["HELD", "REJECTED"])
def test_the_customers_own_pan_names_them_when_a_held_pan_replaced_it(journey, hold):
    uploaded = _prasanna_then_bajaj(journey)
    _status(journey, uploaded["pan_card:2"]["evidence_id"], hold)
    _sync(journey)
    assert _legal(journey) == "PRASANNA KUMAR DAS"
    assert _sync(journey) == "UNCHANGED"


def test_a_name_already_taken_from_the_held_pan_is_put_right(journey):
    uploaded = _prasanna_then_bajaj(journey, with_aadhaar=True)
    _sync(journey)  # before the hold: the newest PAN, as it was
    assert _legal(journey) == "BAJAJ FINANCE LIMITED"
    _status(journey, uploaded["pan_card:3"]["evidence_id"], "HELD")
    assert _sync(journey) == "VERIFIED"
    assert _legal(journey) == "PRASANNA KUMAR DAS"


def test_a_newer_pan_that_passes_still_names_the_customer(journey):
    _prasanna_then_bajaj(journey)  # no hold: the newer PAN stands as before
    _sync(journey)
    assert _legal(journey) == "BAJAJ FINANCE LIMITED"


def test_a_held_pan_pairs_no_booking_as_the_same_file(journey):
    from audit_core import uc03_p2_same_file as same_file

    uploaded = _prasanna_then_bajaj(journey)
    _status(journey, uploaded["pan_card:2"]["evidence_id"], "HELD")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        rows = connection.execute(text(same_file._ACTIVE_TAX_IDS_SQL),
                                  {"tenant_id": journey.tenant_id, "journey_ids": [journey.journey_id]}).mappings().all()
    assert "AABCB1518L" not in {r["value"] for r in rows}
