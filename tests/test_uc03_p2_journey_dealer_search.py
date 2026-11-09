"""The Executive / Project Manager dashboard (product owner 2026-11-09: "search
and view the booking and deliveries filtered by PC, dealership, search by
customer name, mobile number"): every Dealership of the Project is in an
Executive's or PM's scope, the list and counts narrow by Dealership, the
breakdown has a Dealership-wise strip, and a mobile number is found however it
is typed."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from p2_support import P2Journey, add_evidence, add_extracted_field
from sqlalchemy import text
from test_uc03_create_booking import uc03_create_booking_setup  # noqa: F401  (fixture)
from test_uc03_p2_hero_counts import _seed

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.uc03_p2_journey_views import mobile_search_pattern


@pytest.mark.parametrize(
    ("typed", "pattern"),
    [
        ("98765 43210", "%9876543210%"),
        ("+91 98765 43210", "%9876543210%"),
        ("+91-98765-43210", "%9876543210%"),
        ("919876543210", "%9876543210%"),
        ("09876543210", "%9876543210%"),
        ("98765", "%98765%"),
        ("3210", "%3210"),
        ("0123", "%0123"),
        ("(987) 654", "%987654%"),
        ("321", None),
        ("Ravi", None),
        ("Ravi 3210", None),
        ("", None),
        (None, None),
    ],
)
def test_a_mobile_number_is_read_however_it_is_typed(typed, pattern):
    assert mobile_search_pattern(typed) == pattern


def _second_dealership(setup) -> tuple[str, str, str]:
    """A second Dealership with an Outlet and one booking there; returns
    (dealer_id, outlet_id, journey_id)."""
    tenant_id = setup["tenant_id"]
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, tenant_id)
        dealer_id = connection.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'Second Dealership') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"D2-{uuid4().hex[:8]}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'Second Outlet') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"O2-{uuid4().hex[:8]}"},
        ).scalar_one()
        # The PC of the fixture also works this Outlet, so they can start a booking there.
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
                 "dealer_id, outlet_id) VALUES (:t, :a, 'PC', :d, :o)"),
            {"t": tenant_id, "a": setup["actor_id"], "d": dealer_id, "o": outlet_id},
        )
    from fastapi.testclient import TestClient

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        f"/p2/v1/tenants/{tenant_id}/journeys", headers={"Idempotency-Key": f"dealer-two-{uuid4().hex}"},
        json={"outletId": str(outlet_id)},
    )
    assert response.status_code == 201, response.text
    return str(dealer_id), str(outlet_id), response.json()["journeyId"]


def _project_wide(setup, role: str) -> str:
    """An Executive / PM of the Project: a Project-wide assignment (no Dealership,
    no Outlet), as Role Mapping writes it. The requests are then made as them."""
    actor_id = f"{role.lower()}-{uuid4().hex[:8]}"
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
                 "dealer_id, outlet_id) VALUES (:t, :a, :r, NULL, NULL)"),
            {"t": setup["tenant_id"], "a": actor_id, "r": role},
        )
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=actor_id)
    return actor_id


@pytest.mark.parametrize("role", ["Executive", "PM"])
def test_executive_and_pm_see_every_dealership_and_filter_by_one(uc03_create_booking_setup, role):  # noqa: F811
    setup = uc03_create_booking_setup
    client, base, first_ids = _seed(setup)
    dealer_two, outlet_two, journey_two = _second_dealership(setup)
    # The fixture's PC works both Dealerships here; an Executive / PM is not
    # assigned to either and still sees both, because their scope is the Project.
    _project_wide(setup, role)

    def ids_of(**params):
        response = client.get(f"{base}/journeys", params={"view": "all", **params})
        assert response.status_code == 200, response.text
        return {item["journey_id"] for item in response.json()["items"]}

    assert ids_of() == {*first_ids, journey_two}
    assert ids_of(dealer_id=dealer_two) == {journey_two}
    assert ids_of(dealer_id=dealer_two, outlet_id=str(setup["outlet_id"])) == set()
    assert ids_of(dealer_id=str(uuid4())) == set()

    counts = client.get(f"{base}/journeys:counts", params={"breakdown": "true"}).json()
    assert counts["counts"]["all"] == 6
    by_dealer = {row["dealer_id"]: row for row in counts["byDealer"]}
    assert len(by_dealer) == 2
    assert by_dealer[dealer_two]["dealer_name"] == "Second Dealership"
    assert by_dealer[dealer_two]["total"] == 1 and by_dealer[dealer_two]["open"] == 1
    (first,) = [row for key, row in by_dealer.items() if key != dealer_two]
    # The same figures the Outlet strip carries: 5 journeys, 4 open, 1 closed.
    assert (first["total"], first["open"], first["closed"]) == (5, 4, 1)
    for key in ("needs_tl", "overdue", "high_risk", "stuck", "booking_open", "delivery_open"):
        assert key in first
    assert {row["outlet_id"]: row["dealer_id"] for row in counts["byOutlet"]}[outlet_two] == dealer_two
    assert {row["outlet_id"] for row in counts["byOutlet"]} == {str(setup["outlet_id"]), outlet_two}

    narrowed = client.get(f"{base}/journeys:counts",
                          params={"breakdown": "true", "dealer_id": dealer_two}).json()
    assert narrowed["counts"]["all"] == 1
    assert [row["outlet_id"] for row in narrowed["byOutlet"]] == [outlet_two]
    assert [row["dealer_id"] for row in narrowed["byDealer"]] == [dealer_two]


def test_a_dealership_tl_sees_only_their_dealership(uc03_create_booking_setup):  # noqa: F811
    """The TL's own scope is unchanged: a TL of the first Dealership never sees
    the second one's bookings, with or without the Dealership filter."""
    setup = uc03_create_booking_setup
    client, base, first_ids = _seed(setup)
    dealer_two, _outlet_two, _journey_two = _second_dealership(setup)
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        dealer_one = connection.execute(
            text("SELECT dealer_id FROM auditcore.dealer_outlets WHERE tenant_id=:t AND outlet_id=:o"),
            {"t": setup["tenant_id"], "o": setup["outlet_id"]},
        ).scalar_one()
        tl = f"tl-{uuid4().hex[:8]}"
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
                 "dealer_id, outlet_id) VALUES (:t, :a, 'TL', :d, NULL)"),
            {"t": setup["tenant_id"], "a": tl, "d": dealer_one},
        )
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=tl)
    items = client.get(f"{base}/journeys", params={"view": "all"}).json()["items"]
    assert {item["journey_id"] for item in items} == set(first_ids)
    assert client.get(f"{base}/journeys", params={"view": "all", "dealer_id": dealer_two}).json()["items"] == []
    counts = client.get(f"{base}/journeys:counts", params={"breakdown": "true"}).json()
    assert [row["dealer_id"] for row in counts["byDealer"]] == [str(dealer_one)]


def test_search_by_full_or_partial_mobile_number(uc03_create_booking_setup):  # noqa: F811
    setup = uc03_create_booking_setup
    client, base, ids = _seed(setup)
    on_record, from_document, other = ids[0], ids[1], ids[2]
    tenant_id = setup["tenant_id"]
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, tenant_id)

        def customer_of(journey_id):
            return connection.execute(
                text("SELECT customer_id, dealer_id FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
                {"t": tenant_id, "j": journey_id},
            ).one()

        # A number on the customer record (as the booking form's capture writes it).
        connection.execute(
            text("UPDATE auditcore.customers SET mobile_number='+919876543210', mobile_last4='3210', "
                 "legal_name='Ravi Kumar' WHERE tenant_id=:t AND customer_id=:c"),
            {"t": tenant_id, "c": customer_of(on_record).customer_id},
        )
        # Another customer whose record holds only the last four.
        connection.execute(
            text("UPDATE auditcore.customers SET mobile_last4='7788' WHERE tenant_id=:t AND customer_id=:c"),
            {"t": tenant_id, "c": customer_of(other).customer_id},
        )
        doc_customer, doc_dealer = customer_of(from_document)

    # A number read from the second booking's own booking form only.
    journey = P2Journey(engine=setup["engine"], tenant_id=tenant_id, journey_id=UUID(from_document),
                        actor_id=setup["actor_id"], dealer_id=doc_dealer, outlet_id=setup["outlet_id"],
                        customer_id=doc_customer)
    document = uuid4()
    add_evidence(journey, di_document_id=document, document_type_key="booking_form")
    add_extracted_field(journey, di_document_id=document, field_key="customer_phone", value="91234 56780")
    # A number on a document taken out of this booking (another customer's) is not this booking's.
    foreign = uuid4()
    add_evidence(journey, di_document_id=foreign, document_type_key="booking_form", status="FOREIGN")
    add_extracted_field(journey, di_document_id=foreign, field_key="customer_phone", value="9000011111")

    def found(q):
        response = client.get(f"{base}/journeys", params={"view": "all", "q": q})
        assert response.status_code == 200, response.text
        listed = {item["journey_id"] for item in response.json()["items"]}
        counted = client.get(f"{base}/journeys:counts", params={"q": q}).json()["counts"]["all"]
        assert counted == len(listed)
        return listed

    for typed in ("9876543210", "98765 43210", "+91 98765 43210", "09876543210", "98765", "543210", "3210"):
        assert found(typed) == {on_record}, typed
    for typed in ("9123456780", "+91 91234 56780", "91234", "6780"):
        assert found(typed) == {from_document}, typed
    assert found("7788") == {other}          # last four only: still found, as before
    assert found("8765") == set()            # four digits are the last four, not any four
    assert found("9000011111") == set()      # a document that is not this booking's
    assert found("Ravi") == {on_record}      # names still match
    assert found("ravi kum") == {on_record}
