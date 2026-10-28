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
    sync_booking_date_task,
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


def test_a_date_before_the_floor_is_a_high_verification_whatever_its_confidence(journey):
    receipt = add_ready_document(journey, "dealer_receipt", confidence=99.0, amount_paid="21000",
                                 receipt_date="12/03/2019")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert sync_field_review_tasks(connection, tenant_id=journey.tenant_id,
                                       journey_id=journey.journey_id) == {"RAISED": 1}
    task = _task(journey, f"field-review:{receipt}")
    assert task["title"] == "Check 1 date on Booking Payment Receipt"
    assert task["severity"] == "HIGH" and task["priority"] == "HIGH"
    assert task["assigned_role_code"] == "PC"
    assert "Receipt date read as 12/03/2019, before July 2026" in task["description"]
    (field,) = task["reference"]["fields"]
    assert field["fieldKey"] == "receipt_date" and field["reasons"] == ["DATE_BEFORE_FLOOR"]
    assert task["reference"]["dateFloor"] == "2026-07-01"

    # the PC corrects the date on the document: the task closes itself
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("""UPDATE auditcore.journey_document_extracted_fields
                    SET effective_value='"12/03/2026"'::jsonb, is_modified=true
                    WHERE tenant_id=:t AND di_document_id=:d AND field_key='receipt_date'"""),
            {"t": journey.tenant_id, "d": receipt},
        )
        assert sync_field_review_tasks(connection, tenant_id=journey.tenant_id,
                                       journey_id=journey.journey_id) == {"VERIFIED": 1}
    assert _task(journey, f"field-review:{receipt}")["task_status"] == "VERIFIED_COMPLETE"


def test_a_birth_date_is_never_held_against_the_floor(journey):
    add_ready_document(journey, "aadhaar", confidence=99.0, aadhaar_number="1234", date_of_birth="15/08/1990")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert sync_field_review_tasks(connection, tenant_id=journey.tenant_id,
                                       journey_id=journey.journey_id) == {}


def test_a_misread_date_and_a_low_confidence_value_share_one_task(journey):
    receipt = add_ready_document(journey, "dealer_receipt", confidence=99.0, amount_paid="21000",
                                 receipt_date="not a date")
    add_extracted_field(journey, di_document_id=receipt, field_key="receipt_number", value="R-7",
                        confidence=60.0, document_type="dealer_receipt")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert sync_field_review_tasks(connection, tenant_id=journey.tenant_id,
                                       journey_id=journey.journey_id) == {"RAISED": 1}
    task = _task(journey, f"field-review:{receipt}")
    assert task["title"] == "Verify 2 fields on Booking Payment Receipt"
    assert task["severity"] == "HIGH"
    assert "Receipt date read as 'not a date', which is not a date" in task["description"]
    assert "Receipt number (60%)" in task["description"]


def test_a_booking_form_without_a_booking_date_raises_a_critical_task_that_opens_the_field(journey):
    """#38 (2026-09-30): the deal is priced on the booking date, so a form
    read without one gets a Medium task pointing the PC at that field; the
    task closes itself once a date is in."""
    def run():
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            return sync_booking_date_task(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)

    def task():
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            return connection.execute(
                text("SELECT task_type, severity, task_status, reference FROM auditcore.p2_tasks "
                     "WHERE tenant_id=:t AND dedupe_key=:k"),
                {"t": journey.tenant_id, "k": f"booking-date:{journey.journey_id}"},
            ).mappings().one_or_none()

    assert run() == {} and task() is None  # no booking form yet: nothing to ask
    document_id = add_ready_document(journey, "booking_form", customer_name="A")
    add_extracted_field(journey, di_document_id=document_id, field_key="booking_date", value=None)
    assert run() == {"RAISED": 1}
    raised = task()
    assert (raised["task_type"], raised["severity"], raised["task_status"]) == (
        "PC_BOOKING_DATE_MISSING", "CRITICAL", "READY")  # nothing on the deal is priced until it is in
    assert raised["reference"]["documentId"] == str(document_id) and raised["reference"]["fieldKey"] == "booking_date"
    run()
    assert task()["task_status"] == "READY"

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.journey_document_extracted_fields SET effective_value=to_jsonb('03/09/2026'::text), "
                 "modified_value=to_jsonb('03/09/2026'::text), is_modified=true, "
                 "modified_by_actor_id='pc-1', modified_at_utc=now(), reviewed_by_actor_id='pc-1', reviewed_at_utc=now() "
                 "WHERE di_document_id=:d AND field_key='booking_date'"),
            {"d": document_id},
        )
    assert run() == {"VERIFIED": 1}
    assert task()["task_status"] == "VERIFIED_COMPLETE"


def test_a_booking_form_filed_under_its_old_key_still_gets_its_dates_checked(journey):
    """DEV 2026-10-20: ten Booking Forms read as 2020-2024 had no review task.
    Their evidence carried the pre-canonical key "booking_docket", which
    resolved to the supporting template, so the date floor never ran."""
    form = add_ready_document(journey, "booking_docket", confidence=92.0, booking_date="2024-09-25")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert sync_field_review_tasks(connection, tenant_id=journey.tenant_id,
                                       journey_id=journey.journey_id) == {"RAISED": 1}
    task = _task(journey, f"field-review:{form}")
    assert task["severity"] == "HIGH" and task["assigned_role_code"] == "PC"
    (field,) = task["reference"]["fields"]
    assert field["fieldKey"] == "booking_date" and "DATE_BEFORE_FLOOR" in field["reasons"]


def test_a_booking_date_after_today_is_a_high_verification(journey):
    """DEV 2026-10-21 (UTK-SU-261020-008): a booking form read with a booking
    date after today is a misread, so it goes to the PC as a High date check."""
    from datetime import timedelta

    from audit_core.uc03_p2_dates import programme_today

    ahead = (programme_today() + timedelta(days=30)).isoformat()
    form = add_ready_document(journey, "booking_form", confidence=99.0, booking_date=ahead)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert sync_field_review_tasks(connection, tenant_id=journey.tenant_id,
                                       journey_id=journey.journey_id) == {"RAISED": 1}
    task = _task(journey, f"field-review:{form}")
    assert task["severity"] == "HIGH" and task["assigned_role_code"] == "PC"
    assert "which is after today" in task["description"]
    (field,) = task["reference"]["fields"]
    assert field["fieldKey"] == "booking_date" and field["reasons"] == ["DATE_IN_FUTURE"]


def test_a_gst_split_that_no_longer_exists_is_a_high_verification(journey):
    """2026-10-28: an invoice printing CGST 20% / SGST 20% (40%, no cess) was read
    as CGST 14% / SGST 14% plus a cess -- the pre-GST-2.0 split from the model's
    memory. Every tax figure on it goes to the PC, whatever the confidence."""
    invoice = add_ready_document(journey, "customer_invoice_dms", confidence=99.0, taxable_amount="1207071.71",
                                 cgst_rate="14.0", cgst_amount="168990.0", sgst_rate="14.0", sgst_amount="168990.0",
                                 cess_amount="144848.68", grand_total_amount="1706799.0")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert sync_field_review_tasks(connection, tenant_id=journey.tenant_id,
                                       journey_id=journey.journey_id) == {"RAISED": 1}
    task = _task(journey, f"field-review:{invoice}")
    assert task["severity"] == "HIGH" and task["title"].startswith("Check the GST on")
    assert {f["fieldKey"] for f in task["reference"]["fields"]} == {
        "cgst_rate", "cgst_amount", "sgst_rate", "sgst_amount", "cess_amount"}
    assert "no longer exists" in task["description"]


def test_a_gst_split_as_printed_after_gst_2_0_stands(journey):
    add_ready_document(journey, "customer_invoice_dms", confidence=99.0, taxable_amount="1207071.71",
                       cgst_rate="20", cgst_amount="241414.34", sgst_rate="20", sgst_amount="241414.34")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert sync_field_review_tasks(connection, tenant_id=journey.tenant_id,
                                       journey_id=journey.journey_id) == {}


_PANKAJ_LINES = (
    '[{"particulars_raw": "EX-SHOW ROOM PRICE OF VEHICLE", "debit_amount": 1709900},'
    ' {"particulars_raw": "TCS ON INVOICE", "debit_amount": 20000},'
    ' {"particulars_raw": "INSURANCE COST (NORMAL)", "debit_amount": 168991},'
    ' {"particulars_raw": "REGISTRATION COST", "debit_amount": 80998},'
    ' {"particulars_raw": "EXTENDED WARRANTY", "debit_amount": 132132},'
    ' {"particulars_raw": "RSA", "debit_amount": 2021}, {"particulars_raw": "FAST TAG", "debit_amount": 500},'
    ' {"particulars_raw": "ACCESSORIES", "debit_amount": 50000},'
    ' {"particulars_raw": "BOOKING AMOUNT", "credit_amount": 21000},'
    ' {"particulars_raw": "DEPOSITED INTO BANK", "credit_amount": 1200000},'
    ' {"particulars_raw": "DEPOSITED INTO BANK", "credit_amount": 50000},'
    ' {"particulars_raw": "DEPOSITED INTO BANK", "credit_amount": 650000},'
    ' {"particulars_raw": "CASH", "credit_amount": 51143}]'
)


def test_a_statement_that_does_not_add_up_goes_to_the_pc(journey):
    """2026-10-28: Pankaj Kumar Nayak's statement read with debits Rs 21,64,542,
    credits Rs 19,72,143 and a balance of 0; the Deal showed an EW never taken."""
    ndc = add_ready_document(journey, "customer_ledger", confidence=99.0, line_items=_PANKAJ_LINES,
                             balance_amount="0", net_value="1976143")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert sync_field_review_tasks(connection, tenant_id=journey.tenant_id,
                                       journey_id=journey.journey_id) == {"RAISED": 1}
    task = _task(journey, f"field-review:{ndc}")
    assert task["severity"] == "HIGH" and "do not add up" in task["title"]
    assert "debits Rs 2,164,542, credits Rs 1,972,143, balance Rs 0" in task["description"]


def test_a_statement_that_balances_stands(journey):
    lines = ('[{"particulars_raw": "VEHICLE", "debit_amount": 1101745},'
             ' {"particulars_raw": "DEPOSITED", "credit_amount": 1117161}]')
    add_ready_document(journey, "customer_ledger", confidence=99.0, line_items=lines, balance_amount="15416")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert sync_field_review_tasks(connection, tenant_id=journey.tenant_id,
                                       journey_id=journey.journey_id) == {}
