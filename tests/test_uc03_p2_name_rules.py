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
from audit_core.uc03_p2_customer import sync_customer_name
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
        # 2026-10-28, from open name tasks on Mahindra Orissa:
        ("RASMI RANJAN BISWAL", "RASHMI RANJAN BISWAL- R270908806", True),  # receipt number after the name
        ("Ganganarayan Nanda", "GANGA NARAYAN NANDA-AGKPN5802B", True),  # PAN after it; split differently
        ("RASMI RANJAN BISWAL", "RASMIRANJAN BISWAL", True),
        ("PRITI RANJAN PANDA", "PRITI RNAJAN PANDA", True),  # two letters swapped in one word
        ("MD EJAMUDDIN", "MD NEJAMUDDIN", True),  # one letter missed
        ("PROGRESSIVE ODISHA PRIVATE LIMITED", "PROGRESSIVE ODISHA PVT LTD-R270936802", True),
        ("MIHIR KUMAR DEHURY", "Mr. Anil Kumar Dehury", False),
        ("MANOJ KUMAR MOHAPATRA", "Minakhi Mohapatra", False),
        ("ASHADHAR SAHOO", "DEEPAK SAHOO", False),
        ("RAM DAS", "RAJ DAS", False),  # a short word one letter apart is another word
        ("BIJAY SAHOO", "AJAY SAHOO", False),
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
    # One Team Lead task per Journey (product owner 2026-10-29), not a PC task per document.
    assert task["task_type"] == "INVOICE_DISCREPANCY_REVIEW"
    assert task["severity"] == "HIGH" and task["priority"] == "HIGH"
    assert task["assigned_role_code"] == "TL"
    assert task["title"] == "Customer name differs on 1 document"
    assert "Insurance Cover Note: Anita Sahoo" in task["description"]
    assert "customer per the PAN Card is BISWABHANU BISWAL" in task["description"]
    assert task["reference"]["documentIds"] == [str(wrong)]
    assert task["reference"]["sourceCode"] == "WRONG_CUSTOMER_NAME"
    assert "ACCEPT_EXCEPTION" in task["allowed_actions"]
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


def test_a_receipt_in_another_name_raises_no_name_task(journey):
    """Product owner 2026-10-28: the payer is often a relative or a company; the
    third-party payment question covers it."""
    add_ready_document(journey, "aadhaar", aadhaar_number="1234", aadhaar_name="ANITA SAHOO")
    add_ready_document(journey, "dealer_receipt", amount_paid="21000", customer_name="RAJESH SAHOO")
    assert _sync(journey) == {}


def test_a_corrected_name_that_matches_closes_the_task(journey):
    add_ready_document(journey, "aadhaar", aadhaar_number="1234", aadhaar_name="ANITA SAHOO")
    receipt = add_ready_document(journey, "insurance_cover", premium_amount="21000")
    add_extracted_field(journey, di_document_id=receipt, field_key="insured_name", value="AMITA SAHU",
                        confidence=80.0, document_type="insurance_cover")
    assert _sync(journey) == {"RAISED": 1}
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("""UPDATE auditcore.journey_document_extracted_fields
                    SET effective_value='"ANITA SAHOO"'::jsonb, is_modified=true
                    WHERE tenant_id=:t AND di_document_id=:d AND field_key='insured_name'"""),
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


def test_the_oems_name_on_the_booking_form_is_not_a_dealership(journey):
    """2026-09-30: the booking form is the OEM's form and names Mahindra; the
    receipts and invoice name the dealership (Aditya Motors). No task: the
    OEM's name is skipped and the dealer documents agree among themselves.
    One naming another dealership is still caught, against the receipt."""
    with journey.engine.begin() as connection:
        connection.execute(text("UPDATE auditcore.oems SET oem_name='Mahindra & Mahindra Ltd' WHERE oem_id="
                                "(SELECT oem_id FROM auditcore.projects WHERE tenant_id=:t)"), {"t": journey.tenant_id})
    add_ready_document(journey, "booking_form", customer_name="Ramakanta Sahoo", dealer_name="Mahindra")
    add_ready_document(journey, "dealer_receipt", customer_name="Ramakanta Sahoo", dealer_name="ADITYA MOTORS",
                       amount_paid="21000")
    add_ready_document(journey, "customer_invoice_dms", buyer_name="Ramakanta Sahoo",
                       seller_name="Aditya Motors Pvt Ltd", invoice_number="I1")
    assert _sync(journey) == {}
    other = add_ready_document(journey, "customer_invoice_dms", buyer_name="Ramakanta Sahoo",
                               seller_name="Utkal Automobiles", invoice_number="I2")
    assert _sync(journey) == {"RAISED": 1}
    (task,) = _tasks(journey)
    assert task["reference"]["documentId"] == str(other)
    assert "but the Booking Payment Receipt names ADITYA MOTORS" in task["description"]


def test_the_verified_kyc_name_stays_the_reference_when_another_persons_kyc_arrives(journey):
    """Customer A's PAN named the customer; a PAN of customer B uploaded later
    is the wrong document, and A's documents are not."""
    pan_a = add_ready_document(journey, "pan_card", pan_number="ABCDE1234F", pan_name="BISWABHANU BISWAL")
    add_ready_document(journey, "booking_form", customer_name="Biswabhanu Biswal", dealer_name="Sarthak Motors")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert sync_customer_name(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id) == "VERIFIED"
    assert _sync(journey) == {}
    pan_b = add_ready_document(journey, "pan_card", pan_number="FGHIJ5678K", pan_name="ANITA SAHOO")
    assert _sync(journey) == {"RAISED": 1}
    (task,) = _tasks(journey)
    assert task["reference"]["documentIds"] == [str(pan_b)]
    assert task["reference"]["referenceDocumentId"] == str(pan_a)
    assert task["title"] == "Customer name differs on 1 document"
    assert "customer per the PAN Card is BISWABHANU BISWAL" in task["description"]


def test_a_later_pan_does_not_switch_the_reference(journey):
    """Product owner 2026-10-29: the first KYC document stays the reference. An
    Aadhaar came first; a PAN in another name read later is the odd one out,
    not the Aadhaar and every document that agrees with it."""
    add_ready_document(journey, "aadhaar", aadhaar_name="ANITA SAHOO")
    add_ready_document(journey, "booking_form", customer_name="Anita Sahoo", dealer_name="Sarthak Motors")
    pan = add_ready_document(journey, "pan_card", pan_number="FGHIJ5678K", pan_name="RAJESH KUMAR")
    assert _sync(journey) == {"RAISED": 1}
    (task,) = _tasks(journey)
    assert task["reference"]["documentIds"] == [str(pan)]
    assert "customer per the Aadhaar is ANITA SAHOO" in task["description"]


def test_several_documents_in_another_name_are_one_task(journey):
    add_ready_document(journey, "pan_card", pan_number="ABCDE1234F", pan_name="BISWABHANU BISWAL")
    a = add_ready_document(journey, "insurance_cover", insured_name="Anita Sahoo", policy_number="P1")
    b = add_ready_document(journey, "booking_form", customer_name="Rajesh Kumar", dealer_name="Sarthak Motors")
    assert _sync(journey) == {"RAISED": 1}
    (task,) = _tasks(journey)
    assert set(task["reference"]["documentIds"]) == {str(a), str(b)}
    assert task["title"] == "Customer name differs on 2 documents"


def _named(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        outcome = sync_customer_name(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        name = connection.execute(
            text("SELECT c.legal_name FROM auditcore.journeys j JOIN auditcore.customers c "
                 "ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id WHERE j.tenant_id=:t AND j.journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id}).scalar_one()
    return outcome, name


def test_a_gst_certificate_with_a_gstin_names_the_customer(journey):
    """Product owner 2026-10-27: with a GSTIN uploaded the customer is named as
    per the GSTIN (the PAN is often masked, Aadhaar is optional)."""
    add_ready_document(journey, "pan_card", pan_number="AXXXX1234F", pan_name="AXXXA KXXXR SXXXO")
    assert _named(journey) == ("VERIFIED", "AXXXA KXXXR SXXXO")
    add_ready_document(journey, "gst_certificate", gstin="21ARPPS0866A1ZX", organisation_legal_name="ANAYA KUMAR SAHOO",
                       organisation_trade_name="M/S SHREE KRISHNA TRADERS")
    assert _named(journey) == ("VERIFIED", "ANAYA KUMAR SAHOO")
    assert _named(journey) == ("UNCHANGED", "ANAYA KUMAR SAHOO")


def test_a_gst_certificate_without_a_gstin_does_not_name_the_customer(journey):
    add_ready_document(journey, "pan_card", pan_number="ABCDE1234F", pan_name="BISWABHANU BISWAL")
    add_ready_document(journey, "gst_certificate", gstin="No", organisation_legal_name="SOME TRADERS")
    assert _named(journey) == ("VERIFIED", "BISWABHANU BISWAL")


def test_a_company_customer_keeps_its_directors_kyc_and_its_trade_name_documents(journey):
    """The company is the customer; the director's PAN and Aadhaar are not the
    wrong documents, and a receipt in the director's or the trade name is fine.
    A document in a third name is still the wrong document."""
    add_ready_document(journey, "pan_card", pan_number="ABCDE1234F", pan_name="PRASANA KUMAR SENAPATI")
    add_ready_document(journey, "aadhaar", aadhaar_name="Prasana Kumar Senapati")
    add_ready_document(journey, "gst_certificate", gstin="21AAFCJ2048H1ZY",
                       organisation_legal_name="JAGANNATH POWER AND INFRA PRIVATE LIMITED",
                       organisation_trade_name="JAGANNATH POWER")
    assert _named(journey) == ("VERIFIED", "JAGANNATH POWER AND INFRA PRIVATE LIMITED")
    add_ready_document(journey, "customer_invoice_dms", buyer_name="Jagannath Power and Infra Pvt Ltd",
                       seller_name="Sarthak Motors", invoice_number="I1")
    add_ready_document(journey, "dealer_receipt", customer_name="JAGANNATH POWER-R270909255",
                       dealer_name="Sarthak Motors", amount_paid="21000")
    add_ready_document(journey, "booking_form", customer_name="Prasana Kumar Senapati", dealer_name="Sarthak Motors")
    assert _sync(journey) == {}
    other = add_ready_document(journey, "insurance_cover", insured_name="RAMAKANTA SAHOO")
    assert _sync(journey) == {"RAISED": 1}
    (task,) = _tasks(journey)
    assert task["reference"]["documentIds"] == [str(other)]
    assert "as per the GST certificate" in task["description"]


def test_a_masked_pan_gives_way_to_the_aadhaar_name(journey):
    """Product owner 2026-10-28: a masked PAN does not name the customer when the Aadhaar does."""
    add_ready_document(journey, "pan_card", pan_number="AXXXX1234F", pan_name="PXXXXJ KXXXR NXXXX")
    assert _named(journey) == ("VERIFIED", "PXXXXJ KXXXR NXXXX")  # nothing better on file yet
    add_ready_document(journey, "aadhaar", aadhaar_name="Pankaj Kumar Nayak")
    assert _named(journey) == ("VERIFIED", "Pankaj Kumar Nayak")
    assert _named(journey) == ("UNCHANGED", "Pankaj Kumar Nayak")
    # The masked PAN is the customer's own: not the wrong document.
    add_ready_document(journey, "booking_form", customer_name="Pankaj Kumar Nayak", dealer_name="Sarthak Motors")
    assert _sync(journey) == {}


def test_a_gstin_still_names_the_customer_over_a_masked_pan_and_aadhaar(journey):
    add_ready_document(journey, "pan_card", pan_number="AXXXX1234F", pan_name="AXXXA KXXXR SXXXO")
    add_ready_document(journey, "aadhaar", aadhaar_name="Anaya Kumar Sahoo")
    add_ready_document(journey, "gst_certificate", gstin="21ARPPS0866A1ZX", organisation_legal_name="ANAYA KUMAR SAHOO")
    assert _named(journey)[1] == "ANAYA KUMAR SAHOO"


def test_a_full_name_fits_a_masked_reference():
    from audit_core.uc03_p2_names import is_masked, masked_matches

    assert is_masked("MXXXXI JXXa") and not is_masked("Minati Jena")
    assert masked_matches("MXXXXI JXXa", "Minati Jena")
    assert masked_matches("PXXXXJ KXXXR NXXXX", "PANKAJ KUMAR NAYAK")
    assert not masked_matches("PXXXXJ KXXXR NXXXX", "PANKAJ KUMAR SAHOO")
    assert not masked_matches("SXXXXT KXXXR SXXXO", "Subrat Sahoo")


def test_a_proprietors_kyc_in_the_gst_legal_name_is_the_customers(journey):
    """2026-10-28: a proprietorship's GST certificate prints the proprietor as
    the legal name ("BISWAJIT DAS", trade name "M/S DAS CONSTRUCTION"). His
    Aadhaar was flagged as another customer's because it was compared with the
    masked PAN only."""
    add_ready_document(journey, "pan_card", pan_number="AXXXX1234F", pan_name="BXXXXXXT DXS")
    add_ready_document(journey, "aadhaar", aadhaar_name="BISWAJIT DAS")
    add_ready_document(journey, "gst_certificate", gstin="21ARPPS0866A1ZX", organisation_legal_name="BISWAJIT DAS",
                       organisation_trade_name="M/S DAS CONSTRUCTION", organisation_constitution="Proprietorship")
    _named(journey)
    assert _sync(journey) == {}


def test_a_firm_name_is_the_customers_when_the_kyc_is_the_proprietors(journey):
    """Product owner 2026-10-28: "firm name should be acceptable". A proprietor's
    PAN is on file; the booking form and invoice name the firm."""
    add_ready_document(journey, "pan_card", pan_number="ABCDE1234F", pan_name="PAWAN GUPTA")
    _named(journey)
    add_ready_document(journey, "booking_form", customer_name="M/S SHREE SHYAM ENTERPRISES", dealer_name="Sarthak Motors")
    add_ready_document(journey, "customer_invoice_dms", buyer_name="Shree Shyam Enterprises",
                       seller_name="Sarthak Motors", invoice_number="I1")
    assert _sync(journey) == {}


def test_a_second_firm_or_the_dealership_is_still_the_wrong_document(journey):
    add_ready_document(journey, "pan_card", pan_number="ABCDE1234F", pan_name="PAWAN GUPTA")
    _named(journey)
    add_ready_document(journey, "booking_form", customer_name="M/S SHREE SHYAM ENTERPRISES", dealer_name="Sarthak Motors")
    add_ready_document(journey, "customer_invoice_dms", buyer_name="Shree Shyam Enterprises",
                       seller_name="Sarthak Motors", invoice_number="I1")
    other = add_ready_document(journey, "insurance_cover", insured_name="MAA MANGALA TRADERS")
    dealer = add_ready_document(journey, "tax_invoice_tally", buyer_name="Sarthak Motors", seller_name="Sarthak Motors")
    assert _sync(journey) == {"RAISED": 1}
    (task,) = _tasks(journey)
    assert set(task["reference"]["documentIds"]) == {str(other), str(dealer)}


@pytest.mark.parametrize(("name", "firm"), [
    ("M/S SHREE SHYAM ENTERPRISES", True), ("MAA MANGALA TRADERS", True), ("TARA BAKERY", True),
    ("Messrs Jagannath Agencies", True), ("PAWAN GUPTA", False), ("Mr. Anil Kumar Dehury", False),
])
def test_is_firm_name(name, firm):
    from audit_core.uc03_p2_names import is_firm_name

    assert is_firm_name(name) is firm
