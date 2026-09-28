"""One customer and one dealership per Journey: a document in another name
is the wrong document and goes back to the PC as a High severity Document
Missing task."""

from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_extracted_field,
    add_ready_document,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_names import same_organisation, same_person
from audit_core.uc03_p2_task_producer import sync_name_consistency_tasks


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2nam")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ("Mr. Biswabhanu Biswal", "BISWABHANU BISWAL", True),
        ("BISWAL BISWABHANU", "Biswabhanu Biswal", True),
        ("B. Biswal", "Biswabhanu Biswal", True),
        ("A K Sahoo", "Anita Kumari Sahoo", True),
        ("Smt Anita Sahoo", "ANITA SAHOO", True),
        ("Biswabhanu Biswal S/O Kailash Biswal", "BISWABHANU BISWAL", True),
        ("BISWABHANU BISWAI", "BISWABHANU BISWAL", True),  # one misread letter
        ("Rakesh Kumar", "Rakesh Kumar Singh", True),
        ("Anita Sahoo", "Anita Mohanty", False),
        ("Anita Sahoo", "Biswabhanu Biswal", False),
        ("A. B.", "Anita Sahoo", False),
        ("", "Anita Sahoo", False),
    ],
)
def test_same_person(left, right, same):
    assert same_person(left, right) is same


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ("Sarthak Motors Pvt. Ltd.", "SARTHAK MOTORS", True),
        ("M/s Sarthak Motors Private Limited", "Sarthak Motors Ltd", True),
        ("Sarthak Motors", "Sarthak Motors Bhubaneswar", True),
        ("Sarthak Motor", "Sarthak Motors", True),
        ("Sarthak Motors", "Utkal Automobiles", False),
    ],
)
def test_same_organisation(left, right, same):
    assert same_organisation(left, right) is same


def _sync(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return sync_name_consistency_tasks(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _tasks(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [dict(r) for r in connection.execute(
            text("SELECT * FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key LIKE 'wrong-document:%' "
                 "ORDER BY created_at_utc"),
            {"t": journey.tenant_id},
        ).mappings().all()]


def test_documents_in_another_customers_name_raise_a_high_document_missing_task(journey):
    add_ready_document(journey, "pan_card", pan_number="ABCDE1234F", pan_name="BISWABHANU BISWAL")
    add_ready_document(journey, "booking_form", customer_name="Mr. Biswabhanu Biswal", dealer_name="Sarthak Motors")
    add_ready_document(journey, "dealer_receipt", customer_name="B. Biswal", dealer_name="Sarthak Motors Pvt Ltd",
                       amount_paid="21000")
    wrong = add_ready_document(journey, "insurance_cover", insured_name="Anita Sahoo", policy_number="P1")
    assert _sync(journey) == {"RAISED": 1}
    (task,) = _tasks(journey)
    assert task["task_type"] == "DOCUMENT_MISSING"
    assert task["severity"] == "HIGH" and task["priority"] == "HIGH"
    assert task["assigned_role_code"] == "PC"
    assert task["title"] == "Replace the Insurance Cover Note: it is not in the customer's name"
    assert "in the name of Anita Sahoo" in task["description"]
    assert "customer per the PAN Card is BISWABHANU BISWAL" in task["description"]
    assert "delete it and upload the Insurance Cover Note" in task["description"]
    assert task["reference"]["documentId"] == str(wrong)
    assert task["reference"]["sourceCode"] == "WRONG_CUSTOMER_NAME"
    assert "UPLOAD_DOCUMENT" in task["allowed_actions"]
    # the same evaluation again changes nothing
    assert _sync(journey) == {"UNCHANGED": 1}

    # the PC deletes the wrong cover note: the task closes itself
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.evidence SET association_status='VOIDED' WHERE tenant_id=:t AND di_document_id=:d"),
            {"t": journey.tenant_id, "d": wrong},
        )
    assert _sync(journey) == {"VERIFIED": 1}
    assert _tasks(journey)[0]["task_status"] == "VERIFIED_COMPLETE"


def test_a_corrected_name_that_matches_closes_the_task(journey):
    add_ready_document(journey, "aadhaar", aadhaar_number="1234", aadhaar_name="ANITA SAHOO")
    receipt = add_ready_document(journey, "dealer_receipt", amount_paid="21000")
    add_extracted_field(journey, di_document_id=receipt, field_key="customer_name", value="AMITA SAHU",
                        confidence=80.0, document_type="dealer_receipt")
    assert _sync(journey) == {"RAISED": 1}
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("""UPDATE auditcore.journey_document_extracted_fields
                    SET effective_value='"ANITA SAHOO"'::jsonb, is_modified=true
                    WHERE tenant_id=:t AND di_document_id=:d AND field_key='customer_name'"""),
            {"t": journey.tenant_id, "d": receipt},
        )
    assert _sync(journey) == {"VERIFIED": 1}


def test_without_kyc_nothing_is_compared(journey):
    add_ready_document(journey, "booking_form", customer_name="Anita Sahoo", dealer_name="Sarthak Motors")
    add_ready_document(journey, "dealer_receipt", customer_name="Someone Else", dealer_name="Sarthak Motors",
                       amount_paid="21000")
    assert _sync(journey) == {}


def test_dealer_documents_must_name_the_booking_forms_dealership(journey):
    add_ready_document(journey, "booking_form", customer_name="Anita Sahoo", dealer_name="Sarthak Motors Pvt Ltd")
    add_ready_document(journey, "dealer_receipt", customer_name="Anita Sahoo", dealer_name="SARTHAK MOTORS",
                       amount_paid="21000")
    other = add_ready_document(journey, "customer_invoice_dms", buyer_name="Anita Sahoo",
                               seller_name="Utkal Automobiles", invoice_number="I1")
    assert _sync(journey) == {"RAISED": 1}
    (task,) = _tasks(journey)
    assert task["title"] == "Replace the Customer Invoice (DMS): it names another dealership"
    assert "names the dealership Utkal Automobiles" in task["description"]
    assert "Booking Docket names Sarthak Motors Pvt Ltd" in task["description"]
    assert task["reference"]["documentId"] == str(other)
    assert task["reference"]["sourceCode"] == "WRONG_DEALER_NAME"
