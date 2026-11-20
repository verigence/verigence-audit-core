"""Per-Project task switches (uc03_task_switches).

Product owner 2026-11-19: "is there a way i can see the list of the tasks being produced and
tick them off, because some of the tasks are not relevant currently, but it may be required in
future ... at the project level and it should be under project administration only"; switched
off, the check stops, "and another is that it shouldnt be visible for the existing journey as
well".
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    add_ready_document,
    add_receipt_payment,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import uc03_p2_controls as controls
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_task_producer import apply_control_transitions, raise_or_refresh
from audit_core.uc03_task_switches import set_switch, switched_off

CODE = "PAYMENT_BEFORE_BOOKING"


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="switch")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql: str, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _evaluate(journey, unit: str = "NATIVE:BOOKING") -> None:
    started = datetime.now(UTC)
    transitions = controls.evaluate_unit(journey.engine, tenant_id=journey.tenant_id,
                                         journey_id=journey.journey_id, unit=unit, force=True)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        apply_control_transitions(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                  transitions=transitions, evaluation_started_at=started)


def _task(journey, code: str = CODE) -> dict | None:
    row = _sql(journey, "SELECT task_status, completion_result FROM auditcore.p2_tasks WHERE tenant_id=:t "
                        "AND dedupe_key=:k", k=f"control:{journey.journey_id}:{code}").mappings().one_or_none()
    return dict(row) if row else None


def _finding(journey, code: str = CODE) -> str | None:
    return _sql(journey, "SELECT finding_status FROM auditcore.audit_findings WHERE tenant_id=:t AND journey_id=:j "
                         "AND rule_key=:k ORDER BY created_at_utc DESC LIMIT 1", k=code).scalar_one_or_none()


def _state(journey, code: str = CODE) -> tuple | None:
    row = _sql(journey, "SELECT control_status, status_reason FROM auditcore.p2_control_state WHERE tenant_id=:t "
                        "AND journey_id=:j AND control_code=:c", c=code).one_or_none()
    return tuple(row) if row else None


def _switch(journey, enabled: bool, reason: str = "Not relevant for now") -> dict:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return set_switch(connection, tenant_id=journey.tenant_id, code=CODE, enabled=enabled, reason=reason,
                          actor_id="admin-1", actor_name="Amit Admin")


def _early_payment(journey) -> None:
    add_ready_document(journey, "booking_form", booking_date="2026-09-10")
    add_receipt_payment(journey, amount="21000", receipt_number="R-1", receipt_date=date(2026, 9, 1).isoformat())


def test_a_switched_off_check_stops_and_leaves_the_existing_journeys(journey):
    _early_payment(journey)
    _evaluate(journey)
    assert _task(journey)["task_status"] == "READY" and _finding(journey) == "OPEN"
    assert _state(journey)[0] == "FAIL"

    effect = _switch(journey, False)
    assert effect["tasksClosed"] == 1 and effect["findingsVoided"] == 1 and effect["checksSet"] == 1
    task = _task(journey)
    assert task["task_status"] == "CANCELLED"
    assert task["completion_result"]["outcome"] == "SWITCHED_OFF"
    assert task["completion_result"]["reason"].startswith("Switched off for this Project by Amit Admin on ")
    assert task["completion_result"]["reason"].endswith(": Not relevant for now")
    assert _finding(journey) == "VOIDED"
    assert _state(journey)[0] == "NOT_APPLICABLE"

    # The check does not run any more: no task, no finding, not applicable.
    _evaluate(journey)
    assert _task(journey)["task_status"] == "CANCELLED" and _finding(journey) == "VOIDED"
    assert _state(journey)[0] == "NOT_APPLICABLE"
    outcome = _sql(journey, "SELECT outcome, reason FROM auditcore.rule_executions WHERE tenant_id=:t AND journey_id=:j "
                            "AND rule_code=:c ORDER BY evaluated_at_utc DESC LIMIT 1", c=CODE).one()
    assert tuple(outcome) == ("SKIPPED", "Switched off for this Project.")

    # Journey 360 does not show it.
    from audit_core.uc03_p2_journey360 import compliance

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        listed = compliance(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert CODE not in {i["code"] for items in listed["stages"].values() for i in items}


def test_switched_on_again_the_check_runs_and_its_task_comes_back(journey):
    _early_payment(journey)
    _evaluate(journey)
    _switch(journey, False)
    effect = _switch(journey, True, "Needed again")
    assert effect["bookingsRechecked"] >= 1
    job = _sql(journey, "SELECT job_kind, tag FROM auditcore.p2_worker_jobs WHERE tag LIKE :tag",
               tag=f"{journey.tenant_id}:task-switch-on-payment_before_booking-%").one()
    assert job[0] == "RULES_RERUN" and job[1].endswith("-commit")
    _evaluate(journey)
    assert _task(journey)["task_status"] == "READY"
    assert _finding(journey) == "OPEN" and _state(journey)[0] == "FAIL"
    # Every change is kept.
    changes = _sql(journey, "SELECT enabled, reason, changed_by_name FROM auditcore.p2_task_switch_events "
                            "WHERE tenant_id=:t ORDER BY event_id").all()
    assert [tuple(c) for c in changes] == [(False, "Not relevant for now", "Amit Admin"),
                                           (True, "Needed again", "Amit Admin")]


def test_a_producer_task_of_a_switched_off_kind_is_not_raised(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        set_switch(connection, tenant_id=journey.tenant_id, code="DOCUMENT_MISSING", enabled=False,
                   reason="Not needed", actor_id="admin-1", actor_name="Amit Admin")
        task_id, outcome = raise_or_refresh(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            dedupe_key=f"missing:{journey.journey_id}:x", task_type=next(iter(get_registry().tasks)),
            source_type="DOCUMENT", source_code="DOCUMENT_MISSING", title="Upload the gate pass", description="d",
            reference={}, severity="MEDIUM", registry=get_registry())
    assert (task_id, outcome) == (None, "SUPPRESSED")


def test_the_two_product_owner_switches_are_off_until_a_project_turns_them_on(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert {"ACCESSORIES_FITTED_UNCONFIRMED", "DELIVERY_NOT_COMPLETED_IN_TIME"} <= switched_off(
            connection, journey.tenant_id)
        set_switch(connection, tenant_id=journey.tenant_id, code="DELIVERY_NOT_COMPLETED_IN_TIME", enabled=True,
                   reason="Needed now", actor_id="admin-1", actor_name="Amit Admin")
        assert "DELIVERY_NOT_COMPLETED_IN_TIME" not in switched_off(connection, journey.tenant_id)


# --------------------------------------------------------------------------- API

@pytest.fixture
def api(journey):
    acting = {"subject": journey.actor_id, "role": "PC"}
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=acting["subject"])
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization(acting["role"])
    try:
        yield SimpleNamespace(client=TestClient(app, raise_server_exceptions=False), acting=acting,
                              base=f"/p2/v1/tenants/{journey.tenant_id}/task-switches")
    finally:
        app.dependency_overrides.clear()


def test_only_project_administration_lists_and_changes_the_switches(journey, api):
    _early_payment(journey)
    _evaluate(journey)
    for role in ("PC", "TL", "PM", "TENANT_ADMIN"):
        api.acting["role"] = role
        assert api.client.get(api.base).status_code == 403
        assert api.client.put(f"{api.base}/{CODE}", json={"enabled": False, "reason": "no"}).status_code == 403
    api.acting["role"] = "SUPER_ADMIN"
    listed = api.client.get(api.base).json()
    items = {i["code"]: i for i in listed["items"]}
    assert items[CODE]["enabled"] is True and items[CODE]["open"] == 1 and items[CODE]["owner"] == "Team Lead"
    assert items["DELIVERY_NOT_COMPLETED_IN_TIME"]["enabled"] is False
    assert listed["items"][0]["enabled"] is False  # switched-off kinds first
    assert api.client.put(f"{api.base}/{CODE}", json={"enabled": False, "reason": "no"}).status_code == 422
    assert api.client.put(f"{api.base}/{CODE}", json={"enabled": "off", "reason": "Not now"}).status_code == 422
    assert api.client.put(f"{api.base}/NO_SUCH_TASK", json={"enabled": False, "reason": "Not now"}).status_code == 404
    done = api.client.put(f"{api.base}/{CODE.lower()}", json={"enabled": False, "reason": "Not relevant now"})
    assert done.status_code == 200 and done.json()["tasksClosed"] == 1
    after = {i["code"]: i for i in api.client.get(api.base).json()["items"]}[CODE]
    assert (after["enabled"], after["open"], after["reason"]) == (False, 0, "Not relevant now")


# ── inside a worker job the Project's switches are read once (2026-11-20, hosting cost) ──
def test_inside_a_worker_job_the_switches_are_read_once_and_say_the_same(journey):
    from sqlalchemy import event

    from audit_core import job_reads
    from audit_core.uc03_task_switches import off_result

    _switch(journey, False, reason="Not relevant for now")
    codes = [CODE, "ACCESSORIES_FITTED_UNCONFIRMED", "DUPLICATE_RECEIPT", None]

    def answers(connection):
        return [off_result(connection, journey.tenant_id, code) for code in codes]

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        plain = answers(connection)
    assert [bool(a) for a in plain] == [True, True, False, False]
    assert "Amit Admin" in plain[0]["reason"] and "Not relevant for now" in plain[0]["reason"]

    seen: list[str] = []
    event.listen(journey.engine, "before_cursor_execute", lambda c, cur, s, p, ctx, m: seen.append(s))
    job_reads.install(journey.engine)
    with job_reads.job(), journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert answers(connection) == plain
        assert answers(connection) == plain
        assert sum("FROM auditcore.p2_task_switches" in s for s in seen) == 1
        # Switched back on inside the job: the job's own write is seen at once.
        set_switch(connection, tenant_id=journey.tenant_id, code=CODE, enabled=True, reason="Back on",
                   actor_id="admin-1", actor_name="Amit Admin")
        assert off_result(connection, journey.tenant_id, CODE) is None
