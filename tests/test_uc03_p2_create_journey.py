"""P2 New Booking: one idempotent way to start a Journey."""
from __future__ import annotations

import re
from uuid import UUID

from fastapi.testclient import TestClient
from p2_support import P2Journey, add_page, add_ready_document
from sqlalchemy import text
from test_uc03_create_booking import uc03_create_booking_setup  # noqa: F401  (fixture)

from audit_core.db import set_tenant_context
from audit_core.main import app
from audit_core.uc03_p2_api import _outlet_code, _pc_code
from audit_core.uc03_p2_customer import sync_customer_name


def test_new_booking_starts_a_journey_idempotently(uc03_create_booking_setup):  # noqa: F811
    setup = uc03_create_booking_setup
    client = TestClient(app, raise_server_exceptions=False)
    url = f"/p2/v1/tenants/{setup['tenant_id']}/journeys"
    body = {"outletId": str(setup["outlet_id"]), "customerName": "  New   Customer ", "createdByName": " Asha  Rao "}
    first = client.post(url, headers={"Idempotency-Key": "p2-new-journey-001"}, json=body)
    assert first.status_code == 201, first.text
    again = client.post(url, headers={"Idempotency-Key": "p2-new-journey-001"}, json=body)
    assert again.status_code == 201, again.text
    assert again.json()["journeyId"] == first.json()["journeyId"]

    # The PC's name, dealer and outlet are recorded on the journey, and the
    # Booking & Delivery list shows them to a Team Lead.
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        row = connection.execute(
            text("SELECT created_by_display_name, dealer_id, outlet_id FROM auditcore.journeys "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"t": setup["tenant_id"], "j": first.json()["journeyId"]},
        ).mappings().one()
    assert row["created_by_display_name"] == "Asha Rao"
    assert str(row["outlet_id"]) == str(setup["outlet_id"])
    listed = client.get(url, params={"state": "open"}).json()["items"]
    mine = next(item for item in listed if item["journey_id"] == first.json()["journeyId"])
    assert mine["pc_name"] == "Asha Rao" and mine["outlet_name"] and mine["dealer_name"]
    assert mine["price_variance"] == "0"  # nothing priced yet


def _journey(setup, journey_id: str, customer_id: str) -> P2Journey:
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        dealer_id = connection.execute(
            text("SELECT dealer_id FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
            {"t": setup["tenant_id"], "j": journey_id},
        ).scalar_one()
    return P2Journey(engine=setup["engine"], tenant_id=setup["tenant_id"], journey_id=UUID(journey_id),
                     actor_id=setup["actor_id"], dealer_id=UUID(str(dealer_id)), outlet_id=UUID(str(setup["outlet_id"])),
                     customer_id=UUID(customer_id))


def _customer(setup, customer_id: str) -> dict:
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        return dict(connection.execute(
            text("SELECT display_name, legal_name, legal_name_status FROM auditcore.customers "
                 "WHERE tenant_id=:t AND customer_id=:c"),
            {"t": setup["tenant_id"], "c": customer_id},
        ).mappings().one())


def _sync(journey: P2Journey) -> str:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return sync_customer_name(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def test_new_booking_needs_no_customer_name_the_documents_name_the_customer(uc03_create_booking_setup):  # noqa: F811
    """No name is typed: the Journey starts at once with its id as the
    placeholder name, and the PAN card names the customer."""
    setup = uc03_create_booking_setup
    client = TestClient(app, raise_server_exceptions=False)
    url = f"/p2/v1/tenants/{setup['tenant_id']}/journeys"
    response = client.post(url, headers={"Idempotency-Key": "p2-new-journey-002"},
                           json={"outletId": str(setup["outlet_id"])})
    assert response.status_code == 201, response.text
    journey_id, customer_id = response.json()["journeyId"], response.json()["customerId"]
    assert _customer(setup, customer_id)["display_name"] == journey_id  # the placeholder the database knows
    listed = client.get(url, params={"state": "open"}).json()["items"]
    assert next(i for i in listed if i["journey_id"] == journey_id)["customer_name"] == journey_id
    # Blank counts as no name too, and the same key replays the same Journey.
    again = client.post(url, headers={"Idempotency-Key": "p2-new-journey-002"},
                        json={"outletId": str(setup["outlet_id"]), "customerName": "   "})
    assert again.status_code == 201 and again.json()["journeyId"] == journey_id

    journey = _journey(setup, journey_id, customer_id)
    add_ready_document(journey, "booking_form", customer_name="Mr. Biswabhanu Biswal", dealer_name="Sarthak Motors")
    assert _sync(journey) == "NO_NAME"  # only the KYC names the customer

    add_ready_document(journey, "pan_card", pan_number="ABCDE1234F", pan_name="BISWABHANU BISWAL")
    assert _sync(journey) == "VERIFIED"
    verified = _customer(setup, customer_id)
    assert verified["legal_name"] == "BISWABHANU BISWAL" and verified["legal_name_status"] == "VERIFIED"
    assert verified["display_name"] == "BISWABHANU BISWAL"  # the placeholder gave way to the KYC name
    listed = client.get(url, params={"state": "open"}).json()["items"]
    assert next(i for i in listed if i["journey_id"] == journey_id)["customer_name"] == "BISWABHANU BISWAL"
    assert _sync(journey) == "UNCHANGED"
    # Another KYC reading naming a different person never overwrites a verified name.
    add_ready_document(journey, "aadhaar", aadhaar_number="1234", aadhaar_name="ANITA SAHOO")
    assert _sync(journey) == "UNCHANGED"
    assert _customer(setup, customer_id)["legal_name"] == "BISWABHANU BISWAL"


def test_a_pc_can_delete_a_booking_a_failed_upload_left_stuck(uc03_create_booking_setup):  # noqa: F811
    setup = uc03_create_booking_setup
    client = TestClient(app, raise_server_exceptions=False)
    url = f"/p2/v1/tenants/{setup['tenant_id']}/journeys"
    created = client.post(url, headers={"Idempotency-Key": "p2-new-journey-003"},
                          json={"outletId": str(setup["outlet_id"])}).json()
    journey = _journey(setup, created["journeyId"], created["customerId"])
    cancel = f"{url}/{created['journeyId']}:cancel"

    # Nothing has failed: the booking cannot be deleted.
    assert client.post(cancel, json={}).status_code == 409
    add_page(journey, status="FAILED")
    listed = next(i for i in client.get(url, params={"state": "open"}).json()["items"] if i["journey_id"] == created["journeyId"])
    assert listed["failed_pages"] == 1 and listed["retrying_pages"] == 0

    response = client.post(cancel, json={"reason": "Scanner output was corrupt"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "BOOKING_CANCELLED"
    closed = next(i for i in client.get(url, params={"state": "closed"}).json()["items"] if i["journey_id"] == created["journeyId"])
    assert closed["cancelled"] is True and closed["closed"] is True
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        notice = connection.execute(
            text("SELECT task_type, assigned_role_code, task_status, description FROM auditcore.p2_tasks "
                 "WHERE tenant_id=:t AND journey_id=:j AND task_type='JOURNEY_CANCELLED_NOTICE'"),
            {"t": setup["tenant_id"], "j": created["journeyId"]},
        ).mappings().one()
        stage = connection.execute(
            text("SELECT business_status, close_reason_code FROM auditcore.journey_stage_states "
                 "WHERE tenant_id=:t AND journey_id=:j AND stage_code='BOOKING'"),
            {"t": setup["tenant_id"], "j": created["journeyId"]},
        ).mappings().one()
    assert notice["assigned_role_code"] == "TL" and notice["task_status"] == "READY"
    assert "Scanner output was corrupt" in notice["description"]
    assert stage["business_status"] == "BOOKING_CANCELLED" and stage["close_reason_code"] == "DOCUMENT_UPLOAD_FAILED"
    assert client.post(cancel, json={}).status_code == 409  # already closed


def test_reference_letters_for_the_outlet_and_the_pc():
    assert _outlet_code("Utkal Mahindra - CDA") == "UTK"
    assert _outlet_code("  a-b ") == "ABX"
    assert _outlet_code(None) == "XXX"
    assert _pc_code("Asha Rao") == "AR"
    assert _pc_code(" akansh  kumar chopra ") == "AC"
    assert _pc_code("akanshchopra") == "AK"
    assert _pc_code("Z") == "ZX"
    assert _pc_code(None) == "XX" and _pc_code("123") == "XX"


def _reference(setup, journey_id: str) -> str:
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        return connection.execute(
            text("SELECT journey_reference FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
            {"t": setup["tenant_id"], "j": journey_id},
        ).scalar_one()


def test_a_new_booking_gets_a_readable_reference_numbered_per_outlet_pc_and_day(uc03_create_booking_setup):  # noqa: F811
    setup = uc03_create_booking_setup
    client = TestClient(app, raise_server_exceptions=False)
    url = f"/p2/v1/tenants/{setup['tenant_id']}/journeys"
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        outlet_name = connection.execute(
            text("SELECT outlet_name FROM auditcore.dealer_outlets WHERE tenant_id=:t AND outlet_id=:o"),
            {"t": setup["tenant_id"], "o": setup["outlet_id"]},
        ).scalar_one()
    outlet = _outlet_code(outlet_name)

    def start(key: str, name: str | None):
        body = {"outletId": str(setup["outlet_id"])}
        if name:
            body["createdByName"] = name
        response = client.post(url, headers={"Idempotency-Key": key}, json=body)
        assert response.status_code == 201, response.text
        return response.json()["journeyId"]

    first = start("p2-readable-ref-001", "Asha Rao")
    second = start("p2-readable-ref-002", "Asha Rao")
    other_pc = start("p2-readable-ref-003", "Bikash Das")
    no_name = start("p2-readable-ref-004", None)
    refs = [_reference(setup, j) for j in (first, second, other_pc, no_name)]
    day = re.match(rf"^{outlet}-AR-(\d{{6}})-001$", refs[0])
    assert day, refs
    assert refs[1] == f"{outlet}-AR-{day.group(1)}-002"  # next booking of the same PC, outlet and day
    assert refs[2] == f"{outlet}-BD-{day.group(1)}-001"  # another PC counts on its own
    assert refs[3] == f"{outlet}-XX-{day.group(1)}-001"  # PC name not known

    # The same request again returns the same Journey and leaves its reference alone.
    replay = client.post(url, headers={"Idempotency-Key": "p2-readable-ref-001"},
                         json={"outletId": str(setup["outlet_id"]), "createdByName": "Asha Rao"})
    assert replay.status_code == 201 and replay.json()["journeyId"] == first
    assert _reference(setup, first) == refs[0]
    listed = client.get(url, params={"state": "open"}).json()["items"]
    assert next(i for i in listed if i["journey_id"] == first)["journey_reference"] == refs[0]
