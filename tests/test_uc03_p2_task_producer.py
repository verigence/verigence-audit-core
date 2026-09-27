"""Machine-raised P2 tasks: raised, deduplicated, verified, returned, reopened."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import delete_tenant_data
from p2_support import (
    AllowAllAuthorization,
    add_extracted_field,
    add_ready_document,
    create_p2_journey,
    database_engine,
    principal,
)
from sqlalchemy import text

from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_controls import write_control_state
from audit_core.uc03_p2_documents import (
    P2FieldConfirmCommand,
    confirm_p2_document_field,
)
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_task_producer import (
    apply_control_transitions,
    format_value,
    sync_field_review_tasks,
)
from audit_core.uc03_p2_tasks import submit_action


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2tsk")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _fail(journey, code, *, left="2450000", right="2510000", started=None):
    registry = get_registry()
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        transition = write_control_state(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            control=registry.controls[code], status="FAIL", stage="BOOKING",
            reason="Invoice total differs", details={"leftValue": left, "rightValue": right},
        )
        return apply_control_transitions(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            transitions=[transition], evaluation_started_at=started or datetime.now(UTC),
        )


def _pass(journey, code):
    registry = get_registry()
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        transition = write_control_state(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            control=registry.controls[code], status="PASS", stage="BOOKING", reason=None,
        )
        return apply_control_transitions(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            transitions=[transition], evaluation_started_at=datetime.now(UTC),
        )


def _task(journey, key_suffix):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return dict(connection.execute(
            text("SELECT * FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k"),
            {"t": journey.tenant_id, "k": f"{key_suffix.split(':')[0]}:{journey.journey_id}:{key_suffix.split(':', 1)[1]}"},
        ).mappings().one())


def _act(journey, task_id, action, *, role="PC", comment=None):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return submit_action(connection, tenant_id=journey.tenant_id, task_id=task_id, action=action,
                             actor_id=journey.actor_id if role == "PC" else "tl-1", actor_role_code=role,
                             comment=comment)


def test_indian_money_formatting():
    assert format_value("grand_total_amount", "2510000") == "₹25,10,000"
    assert format_value("amount_paid", "21000.5") == "₹21,000.50"
    assert format_value("customer_name", "A K") == "A K"


def test_failing_control_raises_one_self_contained_task(journey):
    add_ready_document(journey, "booking_form", total_price="2450000")
    assert _fail(journey, "PRICE_BOOKING_VS_INVOICE") == {"RAISED": 1}
    task = _task(journey, "control:PRICE_BOOKING_VS_INVOICE")
    assert task["title"] == "Review total price: Booking Docket vs Customer Invoice (DMS)"
    assert "₹24,50,000" in task["description"] and "₹25,10,000" in task["description"]
    assert task["reference"]["sourceCode"] == "PRICE_BOOKING_VS_INVOICE"
    assert len(task["reference"]["documentIds"]) == 1  # the booking docket on file
    assert task["priority"] == "NORMAL" and task["due_at_utc"] is not None
    assert "CORRECT_EXTRACTED_FIELD" in task["allowed_actions"]
    # retries and repeated evaluations never duplicate it
    assert _fail(journey, "PRICE_BOOKING_VS_INVOICE") == {"UNCHANGED": 1}


def test_passing_control_verifies_the_task_and_recurrence_reopens_it(journey):
    _fail(journey, "PRICE_BOOKING_VS_INVOICE")
    assert _pass(journey, "PRICE_BOOKING_VS_INVOICE") == {"VERIFIED": 1}
    assert _task(journey, "control:PRICE_BOOKING_VS_INVOICE")["task_status"] == "VERIFIED_COMPLETE"
    assert _fail(journey, "PRICE_BOOKING_VS_INVOICE", right="2600000") == {"REOPENED": 1}
    task = _task(journey, "control:PRICE_BOOKING_VS_INVOICE")
    assert (task["task_status"], task["round_number"]) == ("READY", 2)


def test_only_evaluations_after_the_action_can_return_a_task(journey):
    _fail(journey, "PRICE_BOOKING_VS_INVOICE")
    task_id = _task(journey, "control:PRICE_BOOKING_VS_INVOICE")["task_id"]
    stale_start = datetime.now(UTC) - timedelta(seconds=5)
    assert _act(journey, task_id, "REVIEW_DOCUMENT")["status"] == "VERIFYING"

    _fail(journey, "PRICE_BOOKING_VS_INVOICE", started=stale_start)
    assert _task(journey, "control:PRICE_BOOKING_VS_INVOICE")["task_status"] == "VERIFYING"
    _fail(journey, "PRICE_BOOKING_VS_INVOICE")
    assert _task(journey, "control:PRICE_BOOKING_VS_INVOICE")["task_status"] == "RETURNED"


def test_tl_can_accept_an_exception_for_the_same_values_only(journey):
    _fail(journey, "PRICE_BOOKING_VS_INVOICE")
    task_id = _task(journey, "control:PRICE_BOOKING_VS_INVOICE")["task_id"]
    with pytest.raises(ValueError):
        _act(journey, task_id, "ACCEPT_EXCEPTION", role="PC", comment="fine")
    with pytest.raises(ValueError):
        _act(journey, task_id, "ACCEPT_EXCEPTION", role="TL")  # comment required
    assert _act(journey, task_id, "ACCEPT_EXCEPTION", role="TL", comment="OEM price revision")["outcome"] == \
        "EXCEPTION_ACCEPTED"
    assert _fail(journey, "PRICE_BOOKING_VS_INVOICE") == {"ACCEPTED_EXCEPTION": 1}
    assert _fail(journey, "PRICE_BOOKING_VS_INVOICE", right="2700000") == {"REOPENED": 1}


def test_low_confidence_fields_raise_a_review_task_that_closes_after_confirmation(journey):
    di_document_id = add_ready_document(journey, "aadhaar", aadhaar_number="1234")
    canonical = add_extracted_field(journey, di_document_id=di_document_id, field_key="aadhaar_name",
                                    value="SMRUTI", confidence=71.0, document_type="aadhaar")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert sync_field_review_tasks(connection, tenant_id=journey.tenant_id,
                                       journey_id=journey.journey_id) == {"RAISED": 1}
    task = _task(journey, f"field-review:{di_document_id}")
    assert task["title"] == "Verify 1 field on Aadhaar"
    assert "Aadhaar name (71%)" in task["description"]

    # finishing early: machine verification returns it with the field still listed
    _act(journey, task["task_id"], "REVIEW_DOCUMENT")
    engine = journey.engine
    work = worker.WorkItem(journey.tenant_id, task["task_id"], journey.journey_id, "TASK_VERIFY",
                           str(task["task_id"]), {}, 0, None, None, task["task_id"])
    worker._task_verify(engine, work)
    assert _task(journey, f"field-review:{di_document_id}")["task_status"] == "RETURNED"

    with journey.engine.begin() as connection:
        confirm_p2_document_field(
            tenant_id=journey.tenant_id, journey_id=journey.journey_id, document_id=di_document_id,
            field_key="aadhaar_name",
            command=P2FieldConfirmCommand(canonicalFieldId=canonical, sourceFactVersion=1),
            human_principal=principal(journey), authorization_client=AllowAllAuthorization(),
            connection=connection,
        )
        set_tenant_context(connection, journey.tenant_id)
        assert sync_field_review_tasks(connection, tenant_id=journey.tenant_id,
                                       journey_id=journey.journey_id) == {"VERIFIED": 1}
    assert _task(journey, f"field-review:{di_document_id}")["task_status"] == "VERIFIED_COMPLETE"
