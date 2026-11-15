"""Registration and EW / RSA invoice closures, and the System remarks they leave
(product owner 2026-11-15, rule review decisions 3, 5 and 9); on a real Postgres."""
from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_audit_rules import run_p2_audit_rules
from audit_core.uc03_p2_tasks import TaskPermissionError, submit_action
from audit_core.uc03_registration_closures import (
    BH_CHOICE,
    DEALER_NOT_PROVIDED,
    NOT_BH_CHOICE,
    TEMPORARY_REGISTRATION,
    temporary_number,
)
from audit_core.uc03_system_remarks import system_remarks

# ── the temporary registration number ──────────────────────────────────────────


@pytest.mark.parametrize(("comment", "number"), [
    ("Temporary registration: OD02T1234 issued at delivery", "OD02T1234"),
    ("Temporary registration: temp no. T0925OD1234A, permanent awaited", "T0925OD1234A"),
    ("Temporary registration: OD 02 TR 1234", "OD02TR1234"),
    ("Temporary registration: customer will register himself", None),
    ("Temporary registration: delivered on 12 Nov 2026", None),
])
def test_the_temporary_registration_number_is_read_from_the_comment(comment, number):
    assert temporary_number(comment) == number


# ── on the journey ─────────────────────────────────────────────────────────────


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine)
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql: str, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _run(journey) -> dict:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return {r.code: r for r in run_p2_audit_rules(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, stage="BOOKING",
            correlation_id="test", triggering_event="TEST")}


def _remarks(journey) -> list[dict]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return system_remarks(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)["remarks"]


def _act(journey, task_id, *, role: str, choice: str, comment: str | None = None):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return submit_action(connection, tenant_id=journey.tenant_id, task_id=task_id, action="CLOSE_TASK",
                             actor_id=str(uuid4()), actor_role_code=role, comment=comment or choice,
                             details={"choice": choice, "requirementKey": _REQUIREMENT.get(task_id)})


_REQUIREMENT: dict = {}


def _line(journey, key: str, standard: str, actual: str) -> None:
    _sql(journey, "INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, standard_amount, "
                  "actual_amount) VALUES (:t, :j, :k, :s, :a)", k=key, s=standard, a=actual)
    _sql(journey, "INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind, "
                  "component_key, source_document_type, amount, source_document_id) "
                  "VALUES (:t, :j, 'COMMERCIAL', :k, 'tax_invoice_tally', :a, :doc)", k=key, a=actual, doc=uuid4())


def _bh_task(journey):
    return _sql(journey, "SELECT task_id, task_status, assigned_role_code, title, completion_result "
                         "FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k",
                k=f"registration-bh:{journey.journey_id}").mappings().one_or_none()


def _missing_document_task(journey, requirement: str) -> UUID:
    """The journey's ONE missing-documents task (2026-11-15), carrying this document as its line."""
    items = [{"key": requirement, "label": requirement, "reason": "x", "stage": "DELIVERY", "templates": [],
              "conditions": [], "notRequiredAllowed": True},
             {"key": "insurance_cover", "label": "Insurance", "reason": "x", "stage": "DELIVERY", "templates": [],
              "conditions": [], "notRequiredAllowed": True}]
    task_id = UUID(str(_sql(
        journey,
        "INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, task_type, category, origin_kind, source_type, "
        "source_code, dedupe_key, title, description, reference, severity, priority, assigned_role_code, "
        "allowed_actions, completion_protocol) VALUES (:t, :j, 'DOCUMENT_MISSING', 'DOCUMENT_COLLECTION', "
        "'SYSTEM', 'REQUIREMENT', 'DOCUMENT_MISSING', :k, 'Upload the documents', 'x', CAST(:ref AS jsonb), "
        "'HIGH', 'HIGH', 'PC', '[\"UPLOAD_DOCUMENT\", \"ADD_COMMENT\"]', 'MACHINE_VERIFIED') RETURNING task_id",
        k=f"document-missing-test:{journey.journey_id}:{requirement}",
        ref=json.dumps({"sourceCode": "DOCUMENT_MISSING", "items": items,
                        "requirementKeys": [i["key"] for i in items]}),
    ).scalar_one()))
    _REQUIREMENT[task_id] = requirement
    return task_id


def _answered(journey, requirement: str) -> str | None:
    return _sql(journey, "SELECT status FROM auditcore.p2_document_waivers WHERE tenant_id=:t AND journey_id=:j "
                         "AND requirement_key=:k", k=requirement).scalar_one_or_none()


def _status(journey, task_id: UUID) -> str:
    return _sql(journey, "SELECT task_status FROM auditcore.p2_tasks WHERE tenant_id=:t AND task_id=:id",
                id=task_id).scalar_one()


def test_a_registration_below_half_the_standard_asks_the_pc_whether_it_is_bh(journey):
    _line(journey, "ex_showroom_price", "1000000", "1000000")
    _line(journey, "registration_charges", "80000", "30000")

    under = _run(journey)["DEAL_UNDERCHARGED"]
    assert under.outcome == "FAIL"
    assert "whether it is a BH registration" in under.reason
    task = _bh_task(journey)
    assert task["task_status"] == "READY" and task["assigned_role_code"] == "PC"
    assert task["title"] == "Is this a BH registration?"

    # "BH registration": the price master's RTO figure is no longer compared.
    _act(journey, task["task_id"], role="PC", choice=BH_CHOICE)
    under = _run(journey)["DEAL_UNDERCHARGED"]
    assert "Registration" not in under.reason or under.outcome == "PASS", under.reason
    assert _bh_task(journey)["task_status"] == "VERIFIED_COMPLETE"
    [remark] = [r for r in _remarks(journey) if r["code"] == "BH_REGISTRATION"]
    assert "the PC confirmed it" in remark["text"]


def test_not_a_bh_registration_keeps_the_shortfall_and_does_not_ask_again(journey):
    _line(journey, "ex_showroom_price", "1000000", "1000000")
    _line(journey, "registration_charges", "80000", "30000")
    _run(journey)
    _act(journey, _bh_task(journey)["task_id"], role="PC", choice=NOT_BH_CHOICE)

    under = _run(journey)["DEAL_UNDERCHARGED"]
    assert under.outcome == "FAIL"
    assert _bh_task(journey)["task_status"] == "VERIFIED_COMPLETE"
    assert not [r for r in _remarks(journey) if r["code"] == "BH_REGISTRATION"]


def test_a_registration_short_by_less_than_half_asks_nothing(journey):
    _line(journey, "ex_showroom_price", "1000000", "1000000")
    _line(journey, "registration_charges", "80000", "60000")
    _run(journey)
    assert _bh_task(journey) is None


def test_the_bh_question_takes_one_of_its_two_answers(journey):
    _line(journey, "ex_showroom_price", "1000000", "1000000")
    _line(journey, "registration_charges", "80000", "30000")
    _run(journey)
    with pytest.raises(ValueError, match="BH registration"):
        _act(journey, _bh_task(journey)["task_id"], role="PC", choice="Document uploaded")


def test_a_team_lead_closes_the_rto_challan_task_with_the_temporary_registration_number(journey):
    task_id = _missing_document_task(journey, "rto_challan")
    with pytest.raises(TaskPermissionError):
        _act(journey, task_id, role="PC", choice=TEMPORARY_REGISTRATION,
             comment=f"{TEMPORARY_REGISTRATION}: OD02T1234")
    with pytest.raises(ValueError, match="temporary registration number"):
        _act(journey, task_id, role="TL", choice=TEMPORARY_REGISTRATION,
             comment=f"{TEMPORARY_REGISTRATION}: permanent number awaited")
    assert _status(journey, task_id) == "READY" and _answered(journey, "rto_challan") is None

    _act(journey, task_id, role="TL", choice=TEMPORARY_REGISTRATION,
         comment=f"{TEMPORARY_REGISTRATION}: OD02T1234, permanent number awaited")
    assert _answered(journey, "rto_challan") == "ACCEPTED"
    [remark] = [r for r in _remarks(journey) if r["code"] == "TEMPORARY_REGISTRATION"]
    assert "temporary registration OD02T1234" in remark["text"] and "Team Lead" in remark["text"]


def test_a_reviewer_may_close_it_too(journey):
    task_id = _missing_document_task(journey, "rto_challan")
    _act(journey, task_id, role="REVIEWER", choice=TEMPORARY_REGISTRATION,
         comment=f"{TEMPORARY_REGISTRATION}: T0925OD1234A")
    assert _answered(journey, "rto_challan") == "ACCEPTED"


def test_the_pc_closes_the_rsa_invoice_task_as_dealer_not_provided(journey):
    task_id = _missing_document_task(journey, "rsa_invoice")
    _act(journey, task_id, role="PC", choice=DEALER_NOT_PROVIDED,
         comment=f"{DEALER_NOT_PROVIDED}: dealership issues no separate RSA invoice")
    assert _answered(journey, "rsa_invoice") == "ACCEPTED"
    [remark] = [r for r in _remarks(journey) if r["code"] == "DEALER_NOT_PROVIDED"]
    assert remark["title"] == "RSA invoice: dealer not provided"
    assert "does not issue the RSA invoice" in remark["text"]


def test_dealer_not_provided_is_only_for_the_ew_and_rsa_invoices(journey):
    task_id = _missing_document_task(journey, "rto_challan")
    with pytest.raises(ValueError, match="extended warranty or RSA"):
        _act(journey, task_id, role="PC", choice=DEALER_NOT_PROVIDED)
    assert _status(journey, task_id) == "READY" and _answered(journey, "rto_challan") is None


def test_products_not_taken_are_remarks(journey, monkeypatch):
    import audit_core.uc03_p2_journey360 as j360

    sheet = {"categories": [], "discounts": [],
             "summary": {"net": {"standard": None, "current": None},
                         "notTaken": [{"key": "fastag_amount", "label": "FASTag", "standard": "500"},
                                      {"key": "rsa_amount", "label": "RSA", "standard": None},
                                      {"key": "genuine_accessories_amount", "label": "Accessories", "standard": "9000"}]}}
    monkeypatch.setattr(j360, "deal", lambda connection, *, tenant_id, journey_id: sheet)
    remarks = [r for r in _remarks(journey) if r["code"] == "NOT_TAKEN"]
    assert [r["title"] for r in remarks] == ["FASTag not taken", "RSA not taken"]
    assert "price master ₹500" in remarks[0]["text"]
