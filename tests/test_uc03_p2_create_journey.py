"""P2 New Booking: one idempotent way to start a Journey."""
from __future__ import annotations

from fastapi.testclient import TestClient
from test_uc03_create_booking import uc03_create_booking_setup  # noqa: F401  (fixture)

from audit_core.main import app


def test_new_booking_starts_a_journey_idempotently(uc03_create_booking_setup):  # noqa: F811
    setup = uc03_create_booking_setup
    client = TestClient(app, raise_server_exceptions=False)
    url = f"/p2/v1/tenants/{setup['tenant_id']}/journeys"
    body = {"outletId": str(setup["outlet_id"]), "customerName": "  New   Customer "}
    first = client.post(url, headers={"Idempotency-Key": "p2-new-journey-001"}, json=body)
    assert first.status_code == 201, first.text
    again = client.post(url, headers={"Idempotency-Key": "p2-new-journey-001"}, json=body)
    assert again.status_code == 201, again.text
    assert again.json()["journeyId"] == first.json()["journeyId"]


def test_new_booking_requires_a_customer_name(uc03_create_booking_setup):  # noqa: F811
    setup = uc03_create_booking_setup
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        f"/p2/v1/tenants/{setup['tenant_id']}/journeys",
        headers={"Idempotency-Key": "p2-new-journey-002"},
        json={"outletId": str(setup["outlet_id"]), "customerName": "   "},
    )
    assert response.status_code == 422
