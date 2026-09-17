from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core import uc03_duplicate_booking_detection as dbd
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationDecision,
    get_security_authorization_client,
)


@dataclass
class AllowedAuthorization:
    def check_user_permission(self, *, user_id: str, tenant_id: str, permission_key: str):
        return SecurityAuthorizationDecision(
            allowed=True, reason_code="AUTHORIZED", user_id=user_id,
            tenant_id=tenant_id, permission_key=permission_key, role_key=None,
        )


@pytest.fixture
def two_journeys_setup():
    """Two journeys under one tenant, each at its own dealer/outlet -- the
    exact cross-dealer shape this report exists to surface -- plus
    business_assignments for every role, matching audit_setup's own pattern
    in test_uc03_audit_flags.py."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for duplicate-bookings report integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-dbr-{suffix}"
    actors = {role: f"dbr-{role.lower()}-{suffix}" for role in ("PC", "TL", "PM", "EXECUTIVE")}

    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"DBR-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"DBR-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'DBR', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"DBR-{suffix}", "o": oem_id, "cat": category_id},
        )

        def _dealer_outlet(tag: str):
            dealer_id = c.execute(
                text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                     "VALUES (:t, :c, :n) RETURNING dealer_id"),
                {"t": tenant_id, "c": f"DBR-D-{tag}-{suffix}", "n": f"Dealer {tag}"},
            ).scalar_one()
            outlet_id = c.execute(
                text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                     "VALUES (:t, :d, :c, :n) RETURNING outlet_id"),
                {"t": tenant_id, "d": dealer_id, "c": f"DBR-O-{tag}-{suffix}", "n": f"Outlet {tag}"},
            ).scalar_one()
            return dealer_id, outlet_id

        dealer_a, outlet_a = _dealer_outlet("A")
        dealer_b, outlet_b = _dealer_outlet("B")

        # PC is assigned only at Dealer A -- the exact case reported: a PC
        # should see a duplicate-booking pair touching THEIR OWN journey
        # even though the other side belongs to a dealer they have no
        # assignment to at all.
        for role, actor_id in actors.items():
            dealer_id, outlet_id = (None, None) if role != "PC" else (dealer_a, outlet_a)
            c.execute(
                text("""INSERT INTO auditcore.business_assignments
                    (tenant_id, security_actor_id, business_role_code, dealer_id, outlet_id)
                    VALUES (:t, :a, :r, :d, :o)"""),
                {"t": tenant_id, "a": actor_id, "r": role, "d": dealer_id, "o": outlet_id},
            )

        def _journey(*, dealer_id, outlet_id, ref_suffix, customer_name):
            customer_id = c.execute(
                text("""INSERT INTO auditcore.customers
                    (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                    VALUES (:t, :d, :o, 'INDIVIDUAL', :n) RETURNING customer_id"""),
                {"t": tenant_id, "d": dealer_id, "o": outlet_id, "n": customer_name},
            ).scalar_one()
            return c.execute(
                text("""INSERT INTO auditcore.journeys
                    (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                    VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""),
                {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"DBR-J-{ref_suffix}"},
            ).scalar_one()

        journey_a = _journey(dealer_id=dealer_a, outlet_id=outlet_a, ref_suffix=f"{suffix}-A", customer_name="Customer At A")
        journey_b = _journey(dealer_id=dealer_b, outlet_id=outlet_b, ref_suffix=f"{suffix}-B", customer_name="Customer At B")

    active_actor = {"id": actors["PC"]}
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=active_actor["id"])
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowedAuthorization()
    try:
        yield {
            "engine": engine, "tenant_id": tenant_id, "actors": actors,
            "active_actor": active_actor, "journey_a": journey_a, "journey_b": journey_b,
        }
    finally:
        app.dependency_overrides.pop(get_human_principal, None)
        app.dependency_overrides.pop(get_security_authorization_client, None)
        engine.dispose()


def _client() -> TestClient:
    return TestClient(app)


def _force_holder(setup, *, journey_id) -> None:
    """Both test journeys are created in the same transaction (tied
    created_at_utc), so which one the created_at/journey_id tiebreak picks
    as the "holder" is otherwise undetermined -- these tests need a fixed,
    known side (journey_b, at Dealer B) so the PC-visibility assertions
    (PC assigned only at Dealer A) test a real scope boundary rather than
    an accident of tiebreak direction."""
    with setup["engine"].begin() as c:
        c.execute(
            text("""INSERT INTO auditcore.journey_stage_states (
                tenant_id, journey_id, stage_code, business_status,
                audit_state, audit_status, first_started_at_utc,
                latest_activity_at_utc, version_no,
                booking_confirm_date, booking_confirmed_at_utc
            ) VALUES (
                :t, :j, 'BOOKING', 'BOOKING_IN_PROGRESS',
                'IN_PROGRESS', 'NOT_EVALUATED', now(), now(), 1,
                CURRENT_DATE, now()
            ) ON CONFLICT (tenant_id, journey_id, stage_code)
            DO UPDATE SET booking_confirm_date = EXCLUDED.booking_confirm_date"""),
            {"t": setup["tenant_id"], "j": journey_id},
        )


def _set_pan(setup, *, journey_id, pan: str) -> None:
    with setup["engine"].begin() as c:
        c.execute(
            text("""INSERT INTO auditcore.journey_document_extracted_fields (
                tenant_id, journey_id, evidence_id, di_document_id,
                source_fact_ref, source_fact_version, stage_code,
                source_document_type_key, source_canonical_field_id, field_key,
                extracted_value, effective_value, confidence_score, is_modified
            ) VALUES (
                :t, :j, NULL, :doc, NULL, 1, 'BOOKING', 'pan_card', NULL,
                'pan_number', CAST(:v AS jsonb), CAST(:v AS jsonb), 0.95, false
            )"""),
            {"t": setup["tenant_id"], "j": journey_id, "doc": uuid4(), "v": f'"{pan}"'},
        )


def test_pc_sees_a_pairing_touching_their_own_journey_even_across_dealers(two_journeys_setup) -> None:
    """The exact case reported: PC is assigned only at Dealer A, yet the
    duplicate's holder journey lives at Dealer B -- the endpoint must still
    surface the pairing (that's the entire point) with Dealer B's own
    dealer/outlet/customer visible for context, not hidden or 403'd."""
    setup = two_journeys_setup
    _set_pan(setup, journey_id=setup["journey_a"], pan="ABCDE1234F")
    _set_pan(setup, journey_id=setup["journey_b"], pan="ABCDE1234F")
    _force_holder(setup, journey_id=setup["journey_b"])
    with setup["engine"].begin() as c:
        dbd.sync_duplicate_booking_detection(c, tenant_id=setup["tenant_id"], journey_id=setup["journey_a"], correlation_id="")
        dbd.sync_duplicate_booking_detection(c, tenant_id=setup["tenant_id"], journey_id=setup["journey_b"], correlation_id="")

    response = _client().get(f"/v1/tenants/{setup['tenant_id']}/uc03/duplicate-bookings")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "PC" in body["roles"]
    assert len(body["pairs"]) == 1
    pair = body["pairs"][0]
    assert pair["matchBasis"] == "PAN"
    assert pair["matchBasisLabel"] == "PAN"
    assert pair["matchConfidencePercent"] == 99
    assert pair["matchConfidenceLabel"] == "HIGH"

    sides = {pair["duplicate"]["dealerName"], pair["holder"]["dealerName"]}
    assert sides == {"Dealer A", "Dealer B"}
    names = {pair["duplicate"]["customerName"], pair["holder"]["customerName"]}
    assert names == {"Customer At A", "Customer At B"}


def test_tl_with_no_assignment_scope_restriction_also_sees_it(two_journeys_setup) -> None:
    setup = two_journeys_setup
    _set_pan(setup, journey_id=setup["journey_a"], pan="FGHIJ5678K")
    _set_pan(setup, journey_id=setup["journey_b"], pan="FGHIJ5678K")
    with setup["engine"].begin() as c:
        dbd.sync_duplicate_booking_detection(c, tenant_id=setup["tenant_id"], journey_id=setup["journey_a"], correlation_id="")
        dbd.sync_duplicate_booking_detection(c, tenant_id=setup["tenant_id"], journey_id=setup["journey_b"], correlation_id="")

    setup["active_actor"]["id"] = setup["actors"]["TL"]
    response = _client().get(f"/v1/tenants/{setup['tenant_id']}/uc03/duplicate-bookings")
    assert response.status_code == 200, response.text
    assert len(response.json()["pairs"]) == 1


def test_no_open_duplicate_bookings_is_an_empty_list(two_journeys_setup) -> None:
    setup = two_journeys_setup
    response = _client().get(f"/v1/tenants/{setup['tenant_id']}/uc03/duplicate-bookings")
    assert response.status_code == 200, response.text
    assert response.json()["pairs"] == []


def test_resolved_pairing_is_excluded_unless_include_closed(two_journeys_setup) -> None:
    setup = two_journeys_setup
    _set_pan(setup, journey_id=setup["journey_a"], pan="LMNOP9012Q")
    _set_pan(setup, journey_id=setup["journey_b"], pan="LMNOP9012Q")
    _force_holder(setup, journey_id=setup["journey_b"])
    with setup["engine"].begin() as c:
        dbd.sync_duplicate_booking_detection(c, tenant_id=setup["tenant_id"], journey_id=setup["journey_a"], correlation_id="")
        dbd.sync_duplicate_booking_detection(c, tenant_id=setup["tenant_id"], journey_id=setup["journey_b"], correlation_id="")

    # A correction shows these are two different PANs after all -- self-heal
    # resolves the finding.
    _set_pan(setup, journey_id=setup["journey_a"], pan="ZZZZZ0000Z")
    with setup["engine"].begin() as c:
        dbd.sync_duplicate_booking_detection(c, tenant_id=setup["tenant_id"], journey_id=setup["journey_a"], correlation_id="")
        dbd.sync_duplicate_booking_detection(c, tenant_id=setup["tenant_id"], journey_id=setup["journey_b"], correlation_id="")

    default_response = _client().get(f"/v1/tenants/{setup['tenant_id']}/uc03/duplicate-bookings")
    assert default_response.json()["pairs"] == []

    with_closed = _client().get(f"/v1/tenants/{setup['tenant_id']}/uc03/duplicate-bookings?includeClosed=true")
    assert len(with_closed.json()["pairs"]) == 1
    assert with_closed.json()["pairs"][0]["status"] == "RESOLVED"
