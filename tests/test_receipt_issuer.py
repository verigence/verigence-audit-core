"""Only the dealership's own receipt is a payment (product owner 2026-11-11:
"this is the insurance payment receipt for self insurance case, but this
payment receipt is coming under payment receipt which is not correct.. payment
receipt only in the name of dealership or outlet should be there")."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_page,
    add_ready_document,
    add_receipt_payment,
    create_p2_journey,
    database_engine,
    set_minimum_booking_amount,
)
from sqlalchemy import text

from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.uc03_receipt_issuer import (
    issued_by_dealership,
    own_words,
    set_aside_receipts_not_from_the_dealership,
)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2ri")
    set_minimum_booking_amount(created, "21000")
    with engine.begin() as c:
        set_tenant_context(c, created.tenant_id)
        c.execute(text("UPDATE auditcore.dealers SET dealer_name='Shivnath Motors' WHERE tenant_id=:t AND dealer_id=:d"),
                  {"t": created.tenant_id, "d": created.dealer_id})
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


# ── whose receipt it is, from the names alone (the live issuers of Mahindra Orissa) ──
@pytest.mark.parametrize(("issuer", "dealership", "expected"), [
    ("Utkal Automobiles Pvt Ltd", "Utkal Mahindra", True),
    ("UTKAL AUTOMOBILES PRIVATE LIMITED", "Utkal Mahindra", True),
    ("Shivnath Group", "Shivnath Motors", True),
    ("Shivnath Motors (I) Pvt Ltd", "Shivnath Motors", True),
    ("Shivnath Group Bhubaneshwar Khurda Road S", "Shivnath Motors", True),
    ("Shivnth Motors", "Shivnath Motors", True),          # a letter misread
    ("Aditya Car Automotives Pvt Ltd", "Aditya Motors", True),
    ("National Insurance", "Shivnath Motors", False),
    ("National Insurance Company Ltd.", "Utkal Mahindra", False),
    ("Vishakhapatnam Area Depot", "Utkal Mahindra", False),
    ("Government of India Ministry of Defence", "Utkal Mahindra", False),
    ("CDA Mahindra", "Utkal Mahindra", False),            # the manufacturer's name is nobody's own
    ("Sai Motors Pvt Ltd", "Shivnath Motors", False),     # "Motors" is every dealer's
    ("", "Shivnath Motors", None),                         # no issuer read
    (None, "Shivnath Motors", None),
    ("National Insurance", "Mahindra Motors", None),       # the dealership has no word of its own
])
def test_whether_the_issuer_names_the_dealership(issuer, dealership, expected) -> None:
    assert issued_by_dealership(issuer, [dealership]) is expected


def test_a_legal_name_on_the_master_counts_as_the_dealership() -> None:
    assert own_words(["Utkal Mahindra", "Utkal Automobiles Pvt Ltd"]) == {"utkal"}
    assert issued_by_dealership("Kalinga Autocorp", ["Utkal Mahindra", "Kalinga Autocorp Pvt Ltd"]) is True


# ── on a booking ──
def _receipt(journey, *, issuer, amount, number, page):
    document_id = add_ready_document(journey, "dealer_receipt", amount_paid=amount, receipt_number=number,
                                     receipt_date="2026-10-09", dealer_name=issuer)
    add_receipt_payment(journey, amount=amount, receipt_number=number, receipt_date="2026-10-09",
                        di_document_id=document_id)
    _, queue_id, _ = add_page(journey, page_number=page, status="READY", di_document_id=document_id)
    return document_id, queue_id


def _set_aside(journey):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return set_aside_receipts_not_from_the_dealership(c, tenant_id=journey.tenant_id,
                                                          journey_id=journey.journey_id)


def _counted(journey):
    """What every reader of payments counts: receipts whose document is on the booking."""
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return [str(v) for v in c.execute(
            text("SELECT p.amount FROM auditcore.payments p JOIN auditcore.evidence e ON e.tenant_id=p.tenant_id "
                 "AND e.journey_id=p.journey_id AND e.di_document_id=p.source_di_document_id "
                 "WHERE p.tenant_id=:t AND p.journey_id=:j AND e.association_status='ACTIVE' ORDER BY p.amount"),
            {"t": journey.tenant_id, "j": journey.journey_id}).scalars().all()]


def _page(journey, queue_id):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return dict(c.execute(
            text("SELECT queue_status, template_key, display_name, status_reason FROM auditcore.p2_document_queue "
                 "WHERE tenant_id=:t AND queue_id=:q"), {"t": journey.tenant_id, "q": queue_id}).mappings().one())


def test_an_insurers_premium_receipt_is_not_a_payment_and_stays_on_file(journey) -> None:
    _receipt(journey, issuer="Shivnath Group", amount="500000", number="PB/26-27/0597", page=1)
    insurer_document, insurer_page = _receipt(journey, issuer="National Insurance", amount="50370",
                                              number="1637008126000000", page=2)
    assert _counted(journey) == ["50370.00", "500000.00"]

    aside = _set_aside(journey)

    assert [(a["diDocumentId"], a["name"]) for a in aside] == [(str(insurer_document), "Receipt from National Insurance")]
    assert _counted(journey) == ["500000.00"]
    page = _page(journey, insurer_page)
    assert (page["queue_status"], page["template_key"], page["display_name"]) == (
        "SUPPORTING", "supporting_document", "Receipt from National Insurance")
    assert "issued by National Insurance, not by Shivnath Motors" in page["status_reason"]
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        evidence = c.execute(text("SELECT association_status, voided_by_actor_id FROM auditcore.evidence "
                                  "WHERE tenant_id=:t AND di_document_id=:d"),
                             {"t": journey.tenant_id, "d": insurer_document}).mappings().one()
        activity = c.execute(text("SELECT details FROM auditcore.p2_activity_events WHERE tenant_id=:t AND journey_id=:j "
                                  "AND event_type='PAGE_KEPT_AS_OTHER'"),
                             {"t": journey.tenant_id, "j": journey.journey_id}).scalars().all()
    assert (evidence["association_status"], evidence["voided_by_actor_id"]) == ("SUPERSEDED", "SYSTEM:RECEIPT_ISSUER")
    assert len(activity) == 1 and activity[0]["issuer"] == "National Insurance"
    # Asked again, nothing more happens.
    assert _set_aside(journey) == []


def test_the_dealerships_own_receipts_and_one_with_no_issuer_read_stay_payments(journey) -> None:
    _receipt(journey, issuer="Shivnath Motors (I) Pvt Ltd", amount="21000", number="R1", page=1)
    _receipt(journey, issuer="SHIVNATH GROUP", amount="30000", number="R2", page=2)
    document_id = add_ready_document(journey, "dealer_receipt", amount_paid="40000", receipt_number="R3")
    add_receipt_payment(journey, amount="40000", receipt_number="R3", receipt_date="2026-10-09",
                        di_document_id=document_id)
    assert _set_aside(journey) == []
    assert _counted(journey) == ["21000.00", "30000.00", "40000.00"]


def test_a_page_a_person_typed_as_a_receipt_stays_a_receipt(journey) -> None:
    _, queue_id = _receipt(journey, issuer="National Insurance", amount="50370", number="N1", page=1)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_document_queue SET type_overridden_by_actor_id=:a "
                       "WHERE tenant_id=:t AND queue_id=:q"),
                  {"t": journey.tenant_id, "q": queue_id, "a": journey.actor_id})
    assert _set_aside(journey) == []
    assert _counted(journey) == ["50370.00"]


def _type_tasks(journey):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return c.execute(
            text("SELECT task_status FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j "
                 "AND source_code='DOCUMENT_UNCLASSIFIED'"),
            {"t": journey.tenant_id, "j": journey.journey_id}).scalars().all()


def test_the_pc_is_not_asked_to_set_the_type_of_a_receipt_set_aside(journey) -> None:
    """Live 11 Nov: each of the first receipts set aside raised "Set the document type"."""
    _, insurer_page = _receipt(journey, issuer="National Insurance", amount="50370", number="N1", page=1)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        worker.settle_journey(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert _type_tasks(journey) == []
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        assert c.execute(text("SELECT type_overridden_by_actor_id FROM auditcore.p2_document_queue "
                              "WHERE tenant_id=:t AND queue_id=:q"),
                         {"t": journey.tenant_id, "q": insurer_page}).scalar_one() == "SYSTEM:RECEIPT_ISSUER"


def test_a_page_set_aside_before_it_was_typed_is_typed_and_its_task_closes(journey) -> None:
    _, insurer_page = _receipt(journey, issuer="National Insurance", amount="50370", number="N1", page=1)
    _set_aside(journey)
    with journey.engine.begin() as c:   # as the first release left it
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_document_queue SET type_overridden_by_actor_id=NULL "
                       "WHERE tenant_id=:t AND queue_id=:q"), {"t": journey.tenant_id, "q": insurer_page})
        worker.sync_unclassified_page_tasks(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert _type_tasks(journey) == ["READY"]
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        worker.settle_journey(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert all(status in ("VERIFIED_COMPLETE", "CANCELLED") for status in _type_tasks(journey))
    assert _counted(journey) == []


def test_the_recompute_sets_it_aside_before_the_booking_is_rebuilt(journey) -> None:
    _receipt(journey, issuer="Shivnath Group", amount="500000", number="PB/26-27/0597", page=1)
    _, insurer_page = _receipt(journey, issuer="National Insurance Company Ltd.", amount="50370",
                               number="1637008126000000", page=2)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        worker.settle_journey(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert _counted(journey) == ["500000.00"]
    assert _page(journey, insurer_page)["queue_status"] == "SUPPORTING"
