"""P2 New Booking: one idempotent way to start a Journey."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text
from test_uc03_create_booking import uc03_create_booking_setup  # noqa: F401  (fixture)

from audit_core.db import set_tenant_context
from audit_core.main import app


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


def test_new_booking_requires_a_customer_name(uc03_create_booking_setup):  # noqa: F811
    setup = uc03_create_booking_setup
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        f"/p2/v1/tenants/{setup['tenant_id']}/journeys",
        headers={"Idempotency-Key": "p2-new-journey-002"},
        json={"outletId": str(setup["outlet_id"]), "customerName": "   "},
    )
    assert response.status_code == 422
