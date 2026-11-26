"""The Booking & Delivery Hero: an open journey is a booking until it is a
delivery by the one delivery rule (its own vehicle's VIN / chassis on file,
product owner 2026-11-21) -- whatever its stage (2026-11-26) -- and open tasks
are the open journeys' tasks counted as the rows count them, by role (real
Postgres)."""
from __future__ import annotations

import json
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text
from test_uc03_create_booking import uc03_create_booking_setup  # noqa: F401  (fixture)

from audit_core.db import set_tenant_context
from audit_core.main import app
from audit_core.uc03_p2_tasks import create_p2_task
from audit_core.uc03_p2_workflow import mark_booking_completed


def _task(connection, tenant_id, journey_id, role, status="READY"):
    task_id = create_p2_task(
        connection, tenant_id=tenant_id, journey_id=journey_id,
        task_type="WRONG_DOCUMENT_REVIEW", category="DOCUMENT_VERIFICATION",
        origin_kind="SYSTEM", source_type="RULE", source_code="WRONG_DOCUMENT",
        dedupe_key=f"hero:{uuid4()}", title="Review", description="Review it",
        reference={}, severity="HIGH", priority="HIGH", assigned_role_code=role,
        assigned_actor_id=None, raised_by_actor_id=None, raised_by_role_code=None,
        allowed_actions=["REVIEW_DOCUMENT", "ADD_COMMENT"], completion_protocol="MACHINE_VERIFIED",
    )
    if status != "READY":
        connection.execute(
            text("UPDATE auditcore.p2_tasks SET task_status=:s WHERE tenant_id=:t AND task_id=:i"),
            {"s": status, "t": tenant_id, "i": task_id},
        )



def _seed(setup):
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{setup['tenant_id']}"
    ids = []
    for n in range(5):
        response = client.post(f"{base}/journeys", headers={"Idempotency-Key": f"p2-hero-{n:03d}-key"},
                               json={"outletId": str(setup["outlet_id"])})
        assert response.status_code == 201, response.text
        ids.append(response.json()["journeyId"])
    booking, booking_complete, delivery, closed, booking_complete_two = ids

    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        for journey_id, stage in ((booking, "BOOKING_VERIFY_DOCUMENTS"), (booking_complete, "BOOKING_COMPLETE"),
                                  (delivery, "DELIVERY_DOCUMENT_UPLOAD"), (closed, "DELIVERY_COMPLETE"),
                                  (booking_complete_two, "BOOKING_COMPLETE")):
            connection.execute(
                text("INSERT INTO auditcore.p2_journey_runtime (tenant_id, journey_id, current_stage) "
                     "VALUES (:t, :j, :s) ON CONFLICT (tenant_id, journey_id) DO UPDATE SET current_stage=:s"),
                {"t": setup["tenant_id"], "j": journey_id, "s": stage},
            )
        # A delivery row exists for the booking still being completed: it is still a booking.
        connection.execute(
            text("INSERT INTO auditcore.deliveries (tenant_id, journey_id, status_source) VALUES (:t, :j, 'EVIDENCE')"),
            {"t": setup["tenant_id"], "j": booking},
        )
        # Booking completed (the stage engine writes this) on all but the first.
        for journey_id in (booking_complete, delivery, closed, booking_complete_two):
            assert mark_booking_completed(connection, tenant_id=setup["tenant_id"], journey_id=UUID(journey_id))
        # The closed journey: delivery completed.
        connection.execute(
            text("INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code, business_status, "
                 "audit_state, audit_status, first_started_at_utc, latest_activity_at_utc, business_completed_at_utc, "
                 "version_no) VALUES (:t, :j, 'DELIVERY', 'DELIVERY_CLOSED', 'IN_PROGRESS', 'NOT_EVALUATED', "
                 "now(), now(), now(), 1)"),
            {"t": setup["tenant_id"], "j": closed},
        )
        for journey_id, roles in ((booking, ["PC", "PC", "TL"]), (booking_complete, ["PC"]), (booking_complete_two, ["PC"]),
                                  (delivery, ["PC", "TL", "TL"]), (closed, ["PC", "TL"])):
            for role in roles:
                _task(connection, setup["tenant_id"], journey_id, role)
        _task(connection, setup["tenant_id"], delivery, "PC", status="VERIFIED_COMPLETE")  # not open
    return client, base, ids


def test_hero_deliveries_and_tasks_agree_with_the_rows(uc03_create_booking_setup):  # noqa: F811
    setup = uc03_create_booking_setup
    client, base, ids = _seed(setup)
    delivery = ids[2]
    # The stage alone makes no delivery (2026-11-26: the Delivery stage opens when the booking's
    # papers are complete -- live, 663 of November's 1,330 "Deliveries open" had no delivery
    # paper): with no vehicle paper on file all four open journeys are bookings.
    assert client.get(f"{base}/journeys:summary").json()["open"] == {"bookings": 4, "deliveries": 0}
    # The vehicle's chassis on a delivery paper: that journey is a delivery, the others bookings.
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        customer = connection.execute(
            text("SELECT customer_id FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
            {"t": setup["tenant_id"], "j": delivery}).scalar_one()
        document = uuid4()
        connection.execute(text("""INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id,
                di_document_id, document_type_key, evidence_purpose, process_area, association_status)
                VALUES (:t, :j, :c, :s, :d, 'gate_pass', 'JOURNEY_DOCUMENT', 'DELIVERY', 'ACTIVE')"""),
                           {"t": setup["tenant_id"], "j": delivery, "c": customer, "s": uuid4(), "d": document})
        connection.execute(text("""INSERT INTO auditcore.journey_document_extracted_fields (tenant_id, journey_id,
                di_document_id, source_fact_version, field_key, stage_code, source_document_type_key,
                extracted_value, effective_value)
                VALUES (:t, :j, :d, 1, 'chassis_number', 'DELIVERY', 'gate_pass', CAST(:v AS jsonb), CAST(:v AS jsonb))"""),
                           {"t": setup["tenant_id"], "j": delivery, "d": document, "v": json.dumps("MA1TEST0000000009")})
    summary = client.get(f"{base}/journeys:summary").json()
    rows = client.get(f"{base}/journeys", params={"state": "open"}).json()["items"]
    assert summary["open"] == {"bookings": 3, "deliveries": 1} and len(rows) == 4

    # Open journeys only (the closed one's tasks are not counted), and the same
    # numbers the rows carry, by role.
    assert summary["tasks"]["open"] == sum(r["open_tasks"] for r in rows) == 3 + 1 + 1 + 3
    assert summary["tasks"]["pc"] == sum(r["pc_open_tasks"] for r in rows) == 2 + 1 + 1 + 1
    assert summary["tasks"]["tl"] == sum(r["tl_open_tasks"] for r in rows) == 1 + 0 + 0 + 2


def _gate(connection, tenant_id, journey_id, stage, key, status, **details):
    connection.execute(
        text("INSERT INTO auditcore.p2_stage_gate_state (tenant_id, journey_id, stage_code, gate_key, gate_status, details) "
             "VALUES (:t, :j, :s, :k, :st, CAST(:d AS jsonb))"),
        {"t": tenant_id, "j": journey_id, "s": stage, "k": key, "st": status, "d": json.dumps(details)},
    )


def test_rows_and_hero_say_what_the_journey_holds_and_lacks(uc03_create_booking_setup):  # noqa: F811
    """KYC read or missing, required documents in against
    required (Booking and Delivery together), read from the stage engine's gate records."""
    setup = uc03_create_booking_setup
    client, base, ids = _seed(setup)
    booking, booking_complete, delivery, closed, booking_complete_two = ids
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        t = setup["tenant_id"]
        _gate(connection, t, booking, "BOOKING", "KYC_EXTRACTED", "WAITING")
        _gate(connection, t, booking, "BOOKING", "REQUIRED_DOCUMENTS", "WAITING", requiredCount=3, receivedCount=1)
        _gate(connection, t, booking, "DELIVERY", "REQUIRED_DOCUMENTS", "WAITING", requiredCount=5, receivedCount=0)
        _gate(connection, t, booking_complete, "BOOKING", "KYC_EXTRACTED", "PASS")
        _gate(connection, t, booking_complete, "BOOKING", "REQUIRED_DOCUMENTS", "PASS", requiredCount=3, receivedCount=3)
        _gate(connection, t, booking_complete, "DELIVERY", "REQUIRED_DOCUMENTS", "WAITING", requiredCount=5, receivedCount=0)
        _gate(connection, t, delivery, "BOOKING", "KYC_EXTRACTED", "PASS")
        _gate(connection, t, delivery, "BOOKING", "REQUIRED_DOCUMENTS", "PASS", requiredCount=3, receivedCount=3)
        _gate(connection, t, delivery, "DELIVERY", "REQUIRED_DOCUMENTS", "PASS", requiredCount=5, receivedCount=5)
        # A closed journey that still lacks KYC is not an open journey's lack.
        _gate(connection, t, closed, "BOOKING", "KYC_EXTRACTED", "WAITING")
        _gate(connection, t, closed, "DELIVERY", "REQUIRED_DOCUMENTS", "WAITING", requiredCount=5, receivedCount=1)

    rows = {r["journey_id"]: r for r in client.get(f"{base}/journeys", params={"state": "open"}).json()["items"]}
    assert (rows[booking]["kyc_status"], rows[booking]["docs_required"],
            rows[booking]["docs_received"]) == ("WAITING", 8, 1)
    assert (rows[booking_complete]["kyc_status"], rows[booking_complete]["docs_required"],
            rows[booking_complete]["docs_received"]) == ("PASS", 8, 3)
    assert (rows[delivery]["docs_required"], rows[delivery]["docs_received"]) == (8, 8)
    # No gate record yet: unknown, never a guess.
    assert (rows[booking_complete_two]["kyc_status"], rows[booking_complete_two]["docs_required"]) == (None, None)
