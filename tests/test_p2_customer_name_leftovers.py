"""The customer's name after a booking held another customer's file, and with only a masked PAN (2026-11-01).

Product owner 2026-11-01:
- a masked PAN with no GSTIN and no Aadhaar naming the customer: the booking's
  own booking form, invoice, receipt or insurance names them, when that name fits
  the masked letters exactly (ADI-RD-261026-031, -011, -036, ADI-BM-261026-010);
- a name read from another customer's file does not stay once that file is taken
  out (ADI-RD-261024-005 kept Bhagabata Mohanty's masked PAN name on Gurupada
  Mandal's booking), and the entered name follows the booking's own customer
  ("CHINMAYA SENAPATI" on Bikash Kumar Martha's ADI-RD-261022-018)."""
from __future__ import annotations

import pytest
import test_p2_file_identity as fid
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2cnl")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _customer(journey) -> dict:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return dict(connection.execute(
            text("SELECT c.display_name, c.legal_name, c.legal_name_status FROM auditcore.journeys j "
                 "JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id "
                 "WHERE j.tenant_id=:t AND j.journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id}).mappings().one())


def _events(journey, event_type: str) -> list[dict]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [r[0] for r in connection.execute(
            text("SELECT details FROM auditcore.p2_activity_events WHERE tenant_id=:t AND event_type=:e "
                 "ORDER BY event_id"), {"t": journey.tenant_id, "e": event_type}).all()]


def _entered(journey, name: str) -> None:
    """The entered name as the first name read set it (0058), through the guard's door (0185)."""
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("SELECT set_config('auditcore.entered_name_follows_customer', 'on', true)"))
        connection.execute(text("UPDATE auditcore.customers SET display_name=:n WHERE tenant_id=:t AND customer_id=:c"),
                           {"t": journey.tenant_id, "c": journey.customer_id, "n": name})


@pytest.mark.parametrize(("document", "field", "printed", "named"), [
    ("booking_docket", "customer_name", "Sushanta Sahoo", "SUSHANTA SAHOO"),
    ("payment_receipt", "customer_name", "SUSHANTA SAHOO-RMXPS9145C", "SUSHANTA SAHOO"),
    ("booking_docket", "customer_name", "Mr. Sushanta Sahoo", "SUSHANTA SAHOO"),
    ("booking_docket", "customer_name", "Susanta Sahoo", "SXXXXXXA SXXXO"),  # one letter short: does not fit
    ("booking_docket", "customer_name", "Sushanta Sahu", "SXXXXXXA SXXXO"),  # another surname
])
def test_a_masked_pan_alone_is_named_from_the_bookings_own_documents_only_when_it_fits(
        journey, monkeypatch, document, field, printed, named):
    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    requirements = fid._requirements(journey)
    fid._upload(journey, requirements, "Sushanta Sahoo.pdf", 0, [
        ("pan_card", "pan_card", {"pan_number": "RMXPS9145C", "pan_name": "SXXXXXXA SXXXO"}),
        (document, "dealer_receipt" if document == "payment_receipt" else document, {field: printed}),
    ])
    fid._settle(journey)
    assert _customer(journey)["legal_name"] == named
    fid._settle(journey)  # and stays
    assert _customer(journey)["legal_name"] == named


def test_an_aadhaar_still_names_the_customer_before_any_other_document(journey, monkeypatch):
    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    requirements = fid._requirements(journey)
    fid._upload(journey, requirements, "Sushanta Sahoo.pdf", 0, [
        ("pan_card", "pan_card", {"pan_number": "RMXPS9145C", "pan_name": "SXXXXXXA SXXXO"}),
        ("aadhaar", "aadhaar", {"aadhaar_number": "7206 1197 5770", "aadhaar_name": "Sushanta Kumar Sahoo"}),
        ("booking_docket", "booking_docket", {"customer_name": "Sushanta Sahoo"}),
    ])
    fid._settle(journey)
    assert _customer(journey)["legal_name"] == "Sushanta Kumar Sahoo"


def _gurupada_then_bhagabata(journey):
    requirements = fid._requirements(journey)
    gurupada = fid._upload(journey, requirements, "Gurupada Mandal Booking .pdf", 0, [
        ("booking_docket", "booking_docket", {"customer_name": "Mr. Gurupada Mandal", "dealer_name": "Aditya Motors"}),
        ("payment_receipt", "dealer_receipt", {"customer_name": "GURUPADA MANDAL-EPGPM0671J", "amount_paid": "21000",
                                               "receipt_number": "AMC-B/30001", "receipt_date": "2026-10-22"}),
        ("aadhaar", "aadhaar", {"aadhaar_number": "2587 4279 3416"}),  # the back: no name read
    ])
    bhagabata = fid._upload(journey, requirements, "Bhagabata Mohanty Booking .pdf", 4, [
        ("pan_card", "pan_card", {"pan_number": "AKXPM6612L", "pan_name": "BXXXXXXXA MXXXXXY"}),
        ("booking_docket", "booking_docket", {"customer_name": "Bhagabata Mohanty", "dealer_name": "Aditya Motors"}),
    ])
    with journey.engine.begin() as connection:  # Gurupada's file stays the larger one
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_upload_batches SET page_count=12 WHERE tenant_id=:t AND batch_id=:b"),
                           {"t": journey.tenant_id, "b": gurupada["booking_docket:1"]["batch_id"]})
    return gurupada, bhagabata


def test_a_name_from_another_customers_file_goes_and_the_entered_name_follows_the_booking(journey, monkeypatch):
    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    _gurupada_then_bhagabata(journey)
    fid._settle(journey)  # decided, not taken out: Bhagabata's PAN (and docket) name the booking, as live
    _entered(journey, "BHAGABATA MOHANTY")
    assert _customer(journey) == {"display_name": "BHAGABATA MOHANTY", "legal_name": "BHAGABATA MOHANTY",
                                  "legal_name_status": "VERIFIED"}

    fid._switch(journey, True)
    fid._settle(journey)
    # No KYC of Gurupada's names him: the name read from Bhagabata's file goes,
    # and the entered name is the customer his own files are about.
    assert _customer(journey) == {"display_name": "GURUPADA MANDAL", "legal_name": None,
                                  "legal_name_status": "PENDING"}
    [cleared] = _events(journey, "CUSTOMER_NAME_CLEARED")
    assert cleared["name"] == "BHAGABATA MOHANTY"
    [followed] = _events(journey, "ENTERED_NAME_FOLLOWS_CUSTOMER")
    assert (followed["from"], followed["to"]) == ("BHAGABATA MOHANTY", "GURUPADA MANDAL")
    fid._settle(journey)  # nothing more
    assert len(_events(journey, "ENTERED_NAME_FOLLOWS_CUSTOMER")) == len(_events(journey, "CUSTOMER_NAME_CLEARED")) == 1


def test_the_entered_name_follows_the_verified_name_and_only_away_from_a_taken_out_file(journey, monkeypatch):
    """ADI-RD-261022-018: once Bikash's Aadhaar names him, "CHINMAYA SENAPATI"
    (whose file was taken out) gives way to him. An entered name that is
    nobody's taken-out file -- a PC's own spelling, a firm -- stays."""
    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    requirements = fid._requirements(journey)
    bikash = fid._upload(journey, requirements, "Bikash Kumar Martha Booking .pdf", 0, [
        ("pan_card", "pan_card", {"pan_number": "FJBPM5078P", "pan_name": "BXXXXH KXXXR MXXXXA"}),
        ("aadhaar", "aadhaar", {"aadhaar_number": "4410 2290 8812", "aadhaar_name": "Bikash Kumar Martha"}),
        ("booking_docket", "booking_docket", {"customer_name": "Bikash Kumar Martha"}),
    ])
    fid._upload(journey, requirements, "Chinmaya Senapati Booking .pdf", 4, [
        ("aadhaar", "aadhaar", {"aadhaar_number": "9182 7730 2214", "aadhaar_name": "Chinmaya Senapati"}),
        ("booking_docket", "booking_docket", {"customer_name": "MR. CHINMAYA SENAPATI"}),
    ])
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_upload_batches SET page_count=12 WHERE tenant_id=:t AND batch_id=:b"),
                           {"t": journey.tenant_id, "b": bikash["pan_card:1"]["batch_id"]})
    _entered(journey, "CHINMAYA SENAPATI")
    fid._settle(journey)  # not taken out yet: the entered name stays
    assert _customer(journey)["display_name"] == "CHINMAYA SENAPATI"
    fid._switch(journey, True)
    fid._settle(journey)
    assert _customer(journey) == {"display_name": "Bikash Kumar Martha", "legal_name": "Bikash Kumar Martha",
                                  "legal_name_status": "VERIFIED"}

    _entered(journey, "Bikas Martha")  # a spelling of the PC's, nobody's file taken out
    fid._settle(journey)
    assert _customer(journey)["display_name"] == "Bikas Martha"


def test_the_entered_name_guard_still_holds_without_the_setting(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.customers SET display_name='Somebody Else' WHERE tenant_id=:t "
                                "AND customer_id=:c"), {"t": journey.tenant_id, "c": journey.customer_id})
    assert _customer(journey)["display_name"] == "P2 Customer"
