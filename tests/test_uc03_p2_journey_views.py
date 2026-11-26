"""Journey list tabs, paging and counts (product owner 2026-10-20: ~1,200
journeys a month are split into tabs and paged on the server)."""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text
from test_uc03_create_booking import uc03_create_booking_setup  # noqa: F401  (fixture)
from test_uc03_p2_hero_counts import _seed, _task

from audit_core.db import set_tenant_context


def test_tabs_page_and_count_the_journeys_in_scope(uc03_create_booking_setup):  # noqa: F811
    setup = uc03_create_booking_setup
    client, base, ids = _seed(setup)
    booking, booking_complete, delivery, closed, booking_complete_two = ids
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        # booking_complete_two: its PC task is done, a TL task waits.
        connection.execute(
            text("UPDATE auditcore.p2_tasks SET task_status='VERIFIED_COMPLETE' "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"t": setup["tenant_id"], "j": booking_complete_two},
        )
        _task(connection, setup["tenant_id"], booking_complete_two, "TL")
        # delivery: one task overdue, and an open Critical finding.
        connection.execute(
            text("UPDATE auditcore.p2_tasks SET due_at_utc=now() - interval '2 days' "
                 "WHERE tenant_id=:t AND journey_id=:j AND assigned_role_code='TL'"),
            {"t": setup["tenant_id"], "j": delivery},
        )
        connection.execute(
            text("INSERT INTO auditcore.audit_findings (tenant_id, journey_id, stage_code, finding_type_code, "
                 "rule_key, severity, title, finding_status) VALUES (:t, :j, 'BOOKING', 'DUPLICATE_BOOKING', "
                 ":k, 'CRITICAL', 'Possible duplicate', 'OPEN')"),
            {"t": setup["tenant_id"], "j": delivery, "k": f"DUPLICATE_BOOKING:{uuid4()}"},
        )

    def tab(view, **params):
        response = client.get(f"{base}/journeys", params={"view": view, **params})
        assert response.status_code == 200, response.text
        return response.json()

    def ids_of(body):
        return {item["journey_id"] for item in body["items"]}

    assert ids_of(tab("needs_action")) == {booking, booking_complete, delivery}
    assert ids_of(tab("with_tl")) == {booking_complete_two}
    assert ids_of(tab("needs_tl")) == {booking, delivery, booking_complete_two}
    assert ids_of(tab("overdue")) == {delivery}
    assert ids_of(tab("high_risk")) == {delivery}
    assert ids_of(tab("closed")) == {closed}
    assert len(tab("all")["items"]) == 5

    # Paging: two at a time, no journey twice, nextOffset until the end.
    seen, offset = [], 0
    while offset is not None:
        page = tab("all", limit=2, offset=offset)
        seen += [item["journey_id"] for item in page["items"]]
        offset = page["nextOffset"]
    assert sorted(seen) == sorted(ids)

    counts = client.get(f"{base}/journeys:counts", params={"breakdown": "true"}).json()
    assert counts["counts"]["needs_action"] == 3 and counts["counts"]["with_tl"] == 1
    assert counts["counts"]["overdue"] == 1 and counts["counts"]["high_risk"] == 1
    assert counts["counts"]["closed"] == 1 and counts["counts"]["all"] == 5
    # The headline numbers add up: open = bookings open + deliveries open,
    # the same split the Booking & Delivery summary shows.
    c = counts["counts"]
    assert c["booking_open"] + c["delivery_open"] == c["open"] == 4
    summary = client.get(f"{base}/journeys:summary").json()
    assert summary["open"]["bookings"] == c["booking_open"]
    assert summary["open"]["deliveries"] == c["delivery_open"]
    (outlet,) = counts["byOutlet"]
    assert outlet["total"] == 5 and outlet["booking_open"] + outlet["delivery_open"] == outlet["open"]
    assert outlet["open"] == 4 and outlet["overdue"] == 1 and outlet["closed"] == 1
    assert sum(pc["open"] for pc in counts["byPc"]) == 4

    # Filters narrow the list and the counts alike.
    other = str(uuid4())
    assert tab("all", outlet_id=other)["items"] == []
    assert client.get(f"{base}/journeys:counts", params={"outlet_id": other}).json()["counts"]["all"] == 0
    assert tab("all", month="2001-01")["items"] == []


def test_calendar_month_by_booking_date_and_delivery_date(uc03_create_booking_setup):  # noqa: F811
    """The Team Lead dashboard's calendar month (product owner 2026-11-07): with
    month_basis=business a journey is in the month of its delivery date once it is a
    delivery, and of its booking date while it is still a booking (2026-11-26: "if booking
    has been converted to delivery then that count should get reduced.. it should no longer
    to be counted in booking"); a misread booking date (before the programme's floor) falls
    back to the day the journey was started."""
    setup = uc03_create_booking_setup
    client, base, ids = _seed(setup)
    booking, booking_complete, delivery, _closed, booking_complete_two = ids
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])

        def booked(journey_id, day):
            connection.execute(
                text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date) VALUES (:t, :j, :d) "
                     "ON CONFLICT (tenant_id, journey_id) DO UPDATE SET booking_date = EXCLUDED.booking_date"),
                {"t": setup["tenant_id"], "j": journey_id, "d": day})

        booked(booking, "2026-09-10")
        booked(booking_complete, "2026-09-28")
        booked(delivery, "2026-08-20")                    # booked in August, delivered in September
        booked(booking_complete_two, "2020-09-15")        # a misread year: not September 2020, nor 2026
        booking_id = connection.execute(
            text("SELECT booking_id FROM auditcore.bookings WHERE tenant_id=:t AND journey_id=:j"),
            {"t": setup["tenant_id"], "j": delivery}).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.deliveries (tenant_id, journey_id, booking_id, actual_delivered_at, "
                 "status_source) VALUES (:t, :j, :b, '2026-09-25T10:00:00+05:30', 'EVIDENCE')"),
            {"t": setup["tenant_id"], "j": delivery, "b": booking_id})

    def ids_of(**params):
        response = client.get(f"{base}/journeys", params={"view": "all", **params})
        assert response.status_code == 200, response.text
        return {item["journey_id"] for item in response.json()["items"]}

    september = {"month": "2026-09", "month_basis": "business"}
    assert ids_of(**september) == {booking, booking_complete, delivery}
    # Booked in August and since delivered: a delivery of September, no longer a booking of August.
    assert ids_of(month="2026-08", month_basis="business") == set()
    counts = client.get(f"{base}/journeys:counts", params={**september, "breakdown": "true"}).json()
    assert counts["counts"]["all"] == 3
    assert (counts["counts"]["bookings_in_month"], counts["counts"]["deliveries_in_month"]) == (2, 1)
    assert counts["byOutlet"][0]["bookings_in_month"] == 2
    # Without the basis the month is still the month the journey was started.
    assert ids_of(month="2026-09") == set()
    # A date range (2026-11-14): by booking date or delivery date, in place of the month.
    assert ids_of(**{"from": "2026-09-01", "to": "2026-09-15"}) == {booking}
    assert ids_of(**{"from": "2026-09-20", "to": "2026-09-30", "month": "2026-01"}) == {booking_complete, delivery}
    assert ids_of(**{"from": "2026-08-01", "to": "2026-09-30", "kind": "delivery"}) == {delivery}
    ranged = client.get(f"{base}/journeys:counts",
                        params={"from": "2026-08-15", "to": "2026-09-30", "breakdown": "true"}).json()
    assert ranged["counts"]["all"] == 3
    # The bookings and the deliveries add up to the journeys: the delivery is counted once.
    assert (ranged["counts"]["bookings_in_month"], ranged["counts"]["deliveries_in_month"]) == (2, 1)
    assert ids_of(**{"from": "2026-08-15", "to": "2026-09-30", "kind": "booking"}) == {booking, booking_complete}


def test_a_booking_with_its_papers_complete_is_a_booking_until_the_vehicle_is_on_file(uc03_create_booking_setup):  # noqa: F811
    """Product owner 2026-11-26 (the Team Lead dashboard against the DMS report): the open
    journeys split into bookings and deliveries by the one delivery rule (its own vehicle's
    VIN / chassis on file, 2026-11-21), not by the stage record -- the Delivery stage opens when
    the booking's papers are complete. Live that day 663 of November's 1,330 "Deliveries open"
    were bookings with no delivery paper."""
    import json

    setup = uc03_create_booking_setup
    client, base, ids = _seed(setup)
    _booking, booking_complete, delivery, _closed, _two = ids

    def counted():
        answer = client.get(f"{base}/journeys:counts", params={"breakdown": "true"}).json()
        return answer["counts"], answer["byOutlet"][0]

    counts, outlet = counted()
    # Stages BOOKING_COMPLETE (twice) and DELIVERY_DOCUMENT_UPLOAD, no vehicle paper: all bookings.
    assert (counts["booking_open"], counts["delivery_open"], counts["closed"]) == (4, 0, 1)
    assert (outlet["booking_open"], outlet["delivery_open"]) == (4, 0)
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        for journey_id, chassis in ((delivery, "MA1TEST0000000011"), (booking_complete, "MA1TEST0000000012")):
            customer = connection.execute(
                text("SELECT customer_id FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
                {"t": setup["tenant_id"], "j": journey_id}).scalar_one()
            document = uuid4()
            connection.execute(text("""INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id,
                    di_document_id, document_type_key, evidence_purpose, process_area, association_status)
                    VALUES (:t, :j, :c, :s, :d, 'tax_invoice_dms', 'JOURNEY_DOCUMENT', 'DELIVERY', 'ACTIVE')"""),
                               {"t": setup["tenant_id"], "j": journey_id, "c": customer, "s": uuid4(), "d": document})
            connection.execute(text("""INSERT INTO auditcore.journey_document_extracted_fields (tenant_id, journey_id,
                    di_document_id, source_fact_version, field_key, stage_code, source_document_type_key,
                    extracted_value, effective_value)
                    VALUES (:t, :j, :d, 1, 'chassis_number', 'DELIVERY', 'tax_invoice_dms', CAST(:v AS jsonb),
                            CAST(:v AS jsonb))"""),
                               {"t": setup["tenant_id"], "j": journey_id, "d": document, "v": json.dumps(chassis)})
    counts, outlet = counted()
    assert (counts["booking_open"], counts["delivery_open"], counts["closed"]) == (2, 2, 1)
    assert counts["booking_open"] + counts["delivery_open"] + counts["closed"] == counts["all"] == 5


def test_the_list_carries_the_observation_severity_and_filters_by_it(uc03_create_booking_setup):  # noqa: F811
    """Product owner 2026-11-18: a column observation severity (High / Medium / Low), a filter
    for it; a Reviewer / TL / PM's setting stands over the system's."""
    from audit_core.uc03_observation_severity import journey_severity, set_override

    setup = uc03_create_booking_setup
    client, base, ids = _seed(setup)
    high, medium = ids[0], ids[1]
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        connection.execute(
            text("INSERT INTO auditcore.p2_observation_severity (tenant_id, journey_id, system_severity, "
                 "system_reasons) VALUES (:t, :j, 'HIGH', '[]'), (:t, :m, 'HIGH', '[]')"),
            {"t": setup["tenant_id"], "j": high, "m": medium},
        )
        after = set_override(connection, tenant_id=setup["tenant_id"], journey_id=medium, severity="MEDIUM",
                             reason="FASTag confirmed with the dealer", actor_id="tl-1", actor_name="TL One",
                             role="TL")
        assert (after["severity"], after["system"], after["override"]["by"]) == ("MEDIUM", "HIGH", "TL One")
        back = set_override(connection, tenant_id=setup["tenant_id"], journey_id=medium, severity=None,
                            reason="back to the system", actor_id="tl-1", actor_name="TL One", role="TL")
        assert back["severity"] == "HIGH" and back["override"] is None
        set_override(connection, tenant_id=setup["tenant_id"], journey_id=medium, severity="MEDIUM",
                     reason="FASTag confirmed with the dealer", actor_id="tl-1", actor_name="TL One", role="TL")
        assert journey_severity(connection, tenant_id=setup["tenant_id"], journey_id=ids[2])["severity"] is None

    def listed(**params):
        response = client.get(f"{base}/journeys", params={"view": "all", **params})
        assert response.status_code == 200, response.text
        return {item["journey_id"]: item for item in response.json()["items"]}

    every = listed()
    assert every[high]["observation_severity"] == "HIGH" and not every[high]["observation_severity_set_by_person"]
    assert every[medium]["observation_severity"] == "MEDIUM" and every[medium]["observation_severity_set_by_person"]
    assert every[ids[2]]["observation_severity"] is None
    assert set(listed(severity="high")) == {high}
    assert set(listed(severity="MEDIUM")) == {medium}
    assert set(listed(severity="NONE")) == set(ids) - {high, medium}
    counts = client.get(f"{base}/journeys:counts", params={"severity": "HIGH"})
    assert counts.status_code == 200, counts.text
