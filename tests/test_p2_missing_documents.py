"""Missing documents of delivered Journeys, day by day (product owner 2026-11-02)."""
from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    add_batch_pages,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_p2_missing_documents import delivery_day, document_name
from audit_core.uc03_p2_object_keys import ist_day


@pytest.fixture
def setup():
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="missdocs")
    acting = {"subject": journey.actor_id, "role": "PC"}
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=acting["subject"])
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization(acting["role"])
    try:
        yield SimpleNamespace(journey=journey, acting=acting, client=TestClient(app, raise_server_exceptions=False),
                              url=f"/p2/v1/tenants/{journey.tenant_id}/missing-documents")
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, journey.tenant_id)
        engine.dispose()


def _journey(s, *, outlet_id=None, customer="Asha Das", pc="Ravi PC"):
    j = s.journey
    with j.engine.begin() as c:
        set_tenant_context(c, j.tenant_id)
        customer_id = c.execute(
            text("INSERT INTO auditcore.customers (tenant_id, dealer_id, outlet_id, customer_type_code, display_name) "
                 "VALUES (:t, :d, :o, 'INDIVIDUAL', :n) RETURNING customer_id"),
            {"t": j.tenant_id, "d": j.dealer_id, "o": outlet_id or j.outlet_id, "n": customer}).scalar_one()
        new_id = c.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, journey_reference, "
                 "created_by_display_name) VALUES (:t, :d, :o, :c, :r, :pc) RETURNING journey_id"),
            {"t": j.tenant_id, "d": j.dealer_id, "o": outlet_id or j.outlet_id, "c": customer_id,
             "r": f"MD-{uuid4().hex[:8]}", "pc": pc}).scalar_one()
    return SimpleNamespace(**{**j.__dict__, "journey_id": UUID(str(new_id)), "customer_id": customer_id})


def _pages(s, jny, stage="DELIVERY", *, uploaded_days_ago=0):
    batch_id, _ = add_batch_pages(jny, [("delivery_order", "READY")])
    with s.journey.engine.begin() as c:
        set_tenant_context(c, s.journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_document_queue SET business_stage=:s WHERE tenant_id=:t AND batch_id=:b"),
                  {"s": stage, "t": s.journey.tenant_id, "b": batch_id})
        c.execute(text("UPDATE auditcore.p2_upload_batches SET created_at_utc = now() - (:n * interval '1 day') "
                       "WHERE tenant_id=:t AND batch_id=:b"),
                  {"n": uploaded_days_ago, "t": s.journey.tenant_id, "b": batch_id})


def _task(s, jny, title, key, *, status="READY", source="DOCUMENT_MISSING"):
    with s.journey.engine.begin() as c:
        set_tenant_context(c, s.journey.tenant_id)
        c.execute(
            text("INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, task_type, category, origin_kind, source_type, "
                 "source_code, title, description, dedupe_key, assigned_role_code, task_status, completion_protocol, "
                 "reference) VALUES (:t, :j, 'DOCUMENT_MISSING', 'MISSING_DOCUMENT', 'SYSTEM', 'DOCUMENT', :src, "
                 ":title, 'x', :k, 'PC', :st, 'MACHINE_VERIFIED', CAST(:ref AS jsonb))"),
            {"t": s.journey.tenant_id, "j": jny.journey_id, "src": source, "title": title,
             "k": f"document-missing:{jny.journey_id}:{key}", "st": status,
             "ref": json.dumps({"requirementKey": key})})


def _gate_pass(s, jny, delivery_date):
    document = uuid4()
    with s.journey.engine.begin() as c:
        set_tenant_context(c, s.journey.tenant_id)
        c.execute(
            text("INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id, di_document_id, "
                 "document_type_key, evidence_purpose, process_area, association_status) "
                 "VALUES (:t, :j, :c, :s, :d, 'gate_pass', 'JOURNEY_DOCUMENT', 'DELIVERY', 'ACTIVE')"),
            {"t": s.journey.tenant_id, "j": jny.journey_id, "c": jny.customer_id, "s": uuid4(), "d": document})
        c.execute(
            text("INSERT INTO auditcore.journey_document_extracted_fields (tenant_id, journey_id, di_document_id, "
                 "field_key, stage_code, source_document_type_key, extracted_value, effective_value) "
                 "VALUES (:t, :j, :d, 'delivery_date', 'DELIVERY', 'gate_pass', CAST(:v AS jsonb), CAST(:v AS jsonb))"),
            {"t": s.journey.tenant_id, "j": jny.journey_id, "d": document, "v": json.dumps(str(delivery_date))})


def _second_outlet(s):
    j = s.journey
    with j.engine.begin() as c:
        return c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'Second Outlet') RETURNING outlet_id"),
            {"t": j.tenant_id, "d": j.dealer_id, "c": f"MD-O2-{uuid4().hex[:6]}"}).scalar_one()


def _assign(s, actor, role, *, outlet_id=None):
    with s.journey.engine.begin() as c:
        c.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
                 "dealer_id, outlet_id) VALUES (:t, :a, :r, :d, :o)"),
            {"t": s.journey.tenant_id, "a": actor, "r": role,
             "d": s.journey.dealer_id if outlet_id else None, "o": outlet_id})


def _today() -> date:
    return ist_day().date()


def test_names_and_day_basis():
    assert document_name("Upload the RSA Invoice") == "RSA Invoice"
    assert document_name("Upload Delivery Order (Financier) or Bank Approval Letter") == \
        "Delivery Order (Financier) or Bank Approval Letter"
    assert document_name("Booking Docket") == "Booking Docket"
    upload = date(2026, 11, 2)
    assert delivery_day("2026-10-30", upload) == (date(2026, 10, 30), "GATE_PASS")
    assert delivery_day("2026-11-03", upload) == (date(2026, 11, 3), "GATE_PASS")
    assert delivery_day("2026-11-04", upload) == (upload, "UPLOAD")
    assert delivery_day("2025-10-30", upload) == (upload, "UPLOAD")  # a misread year
    assert delivery_day("30/10/2026", upload) == (upload, "UPLOAD")
    assert delivery_day(None, upload) == (upload, "UPLOAD")


def test_delivered_journeys_listed_by_day_with_names_and_counts(setup):
    s = setup
    today = _today()
    delivered = _journey(s, customer="Asha Das")
    _pages(s, delivered, "BOOKING")
    _pages(s, delivered, "DELIVERY")
    _gate_pass(s, delivered, today - timedelta(days=2))
    _task(s, delivered, "Upload the RSA Invoice", "rsa_invoice")
    _task(s, delivered, "Upload the Booking Docket", "booking_docket", source="BOOKING_FORM_MISSING")
    _task(s, delivered, "Upload the Insurance Policy", "insurance_policy", status="VERIFIED_COMPLETE")

    by_upload = _journey(s, customer="Bimal Sahu")
    _pages(s, by_upload, "DELIVERY", uploaded_days_ago=1)
    _task(s, by_upload, "Upload the RSA Invoice", "rsa_invoice")

    booking_only = _journey(s, customer="Chitra Nayak")
    _pages(s, booking_only, "BOOKING")
    _task(s, booking_only, "Upload the RSA Invoice", "rsa_invoice")

    closed_only = _journey(s, customer="Deepak Rout")
    _pages(s, closed_only, "DELIVERY")
    _task(s, closed_only, "Upload the RSA Invoice", "rsa_invoice", status="CANCELLED")

    response = s.client.get(s.url)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["totals"] == {"journeys": 2, "documents": 3}
    assert body["byDocument"] == [
        {"name": "RSA Invoice", "requirementKey": "rsa_invoice", "count": 2},
        {"name": "Booking Docket", "requirementKey": "booking_docket", "count": 1},
    ]
    assert [d["date"] for d in body["days"]] == [(today - timedelta(days=1)).isoformat(),
                                                 (today - timedelta(days=2)).isoformat()]
    [upload_row] = body["days"][0]["journeys"]
    assert upload_row["customer"] == "Bimal Sahu" and upload_row["basis"] == "UPLOAD"
    assert upload_row["documents"] == ["RSA Invoice"]
    [gate_row] = body["days"][1]["journeys"]
    assert gate_row["journeyId"] == str(delivered.journey_id)
    assert gate_row["basis"] == "GATE_PASS" and gate_row["pcName"] == "Ravi PC"
    assert sorted(gate_row["documents"]) == ["Booking Docket", "RSA Invoice"]
    assert body["byOutlet"][0]["journeys"] == 2 and body["byOutlet"][0]["documents"] == 3

    # A narrower period leaves the older day out.
    narrow = s.client.get(s.url, params={"from": (today - timedelta(days=1)).isoformat()}).json()
    assert [d["date"] for d in narrow["days"]] == [(today - timedelta(days=1)).isoformat()]
    assert s.client.get(s.url, params={"from": (today - timedelta(days=70)).isoformat()}).status_code == 422


def test_pc_sees_own_outlet_and_tl_the_project(setup):
    s = setup
    other_outlet = _second_outlet(s)
    mine = _journey(s, customer="Own Outlet Customer")
    _pages(s, mine)
    _task(s, mine, "Upload the RSA Invoice", "rsa_invoice")
    theirs = _journey(s, outlet_id=other_outlet, customer="Other Outlet Customer")
    _pages(s, theirs)
    _task(s, theirs, "Upload the Form 21", "form_21")

    pc_view = s.client.get(s.url).json()
    assert [r["customer"] for d in pc_view["days"] for r in d["journeys"]] == ["Own Outlet Customer"]
    assert [o["outletId"] for o in pc_view["outlets"]] == [str(s.journey.outlet_id)]
    assert s.client.get(s.url, params={"outletId": str(other_outlet)}).status_code == 404

    tl = f"tl-{uuid4().hex[:8]}"
    _assign(s, tl, "TL")
    s.acting.update(subject=tl, role="TL")
    tl_view = s.client.get(s.url).json()
    assert sorted(r["customer"] for d in tl_view["days"] for r in d["journeys"]) == [
        "Other Outlet Customer", "Own Outlet Customer"]
    assert {e["requirementKey"]: e["count"] for e in tl_view["byDocument"]} == {"rsa_invoice": 1, "form_21": 1}
    assert len(tl_view["byOutlet"]) == 2 and tl_view["byDealer"][0]["journeys"] == 2
    filtered = s.client.get(s.url, params={"outletId": str(other_outlet)}).json()
    assert [r["customer"] for d in filtered["days"] for r in d["journeys"]] == ["Other Outlet Customer"]
    assert filtered["scope"] == "OUTLET"

    # Someone with no assignment sees nothing.
    s.acting.update(subject=f"nobody-{uuid4().hex[:6]}", role="PC")
    assert s.client.get(s.url).json()["totals"] == {"journeys": 0, "documents": 0}
