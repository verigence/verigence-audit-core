"""A Vehicle Delivery Receipt carrying a delivery date is the gate pass (product
owner 2026-11-12: "can't we put an additional rule that if there is delivery
date mentioned, let's consider that as gate pass")."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import add_page, add_ready_document, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.uc03_delivery_receipt_gate_pass import (
    count_delivery_receipt_as_gate_pass,
)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2dr")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _receipt(journey, *, page=1, **fields):
    document_id = add_ready_document(journey, "delivery_receipt", process_area="DELIVERY",
                                     customer_name="PRIYANKA NAYAK", **fields)
    _, queue_id, _ = add_page(journey, page_number=page, status="READY", di_document_id=document_id)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_document_queue SET template_key='delivery_receipt' "
                       "WHERE tenant_id=:t AND queue_id=:q"), {"t": journey.tenant_id, "q": queue_id})
    return document_id, queue_id


def _apply(journey):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return count_delivery_receipt_as_gate_pass(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _settle(journey):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        worker.settle_journey(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _types(journey, document_id):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        evidence = c.execute(text("SELECT document_type_key FROM auditcore.evidence WHERE tenant_id=:t "
                                  "AND di_document_id=:d AND association_status='ACTIVE'"),
                             {"t": journey.tenant_id, "d": document_id}).scalar_one()
        fields = set(c.execute(text("SELECT source_document_type_key FROM auditcore.journey_document_extracted_fields "
                                    "WHERE tenant_id=:t AND di_document_id=:d"),
                               {"t": journey.tenant_id, "d": document_id}).scalars().all())
        page = c.execute(text("SELECT template_key, type_overridden_by_actor_id FROM auditcore.p2_document_queue "
                              "WHERE tenant_id=:t AND di_document_id=:d AND retired_at_utc IS NULL"),
                         {"t": journey.tenant_id, "d": document_id}).mappings().one()
    return evidence, fields, page["template_key"], page["type_overridden_by_actor_id"]


def test_a_delivery_receipt_with_a_delivery_date_is_typed_the_gate_pass(journey) -> None:
    document_id, _ = _receipt(journey, delivery_date="2026-10-30", receipt_number="143")
    assert _apply(journey) == [{"diDocumentId": str(document_id), "deliveryDate": "2026-10-30"}]
    assert _types(journey, document_id) == ("gate_pass", {"gate_pass"}, "gate_pass",
                                             "SYSTEM:DELIVERY_RECEIPT_GATE_PASS")
    # Once it is the gate pass, asked again nothing more happens.
    assert _apply(journey) == []


def test_the_recompute_types_it_and_the_gate_pass_readers_see_its_date(journey) -> None:
    """Live 11 Nov, UTK-SS-261107-001: no delivery date while the gate pass read as a receipt."""
    from audit_core.uc03_p2_journey360 import _document_facts

    document_id, _ = _receipt(journey, delivery_date="2026-10-30")
    _settle(journey)
    assert _types(journey, document_id)[0] == "gate_pass"
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        facts = _document_facts(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                di_types=("gate_pass",))
        # the delivery date as the journey lists and the Excel read it: from the gate pass
        read = c.execute(
            text("SELECT f.effective_value #>> '{}' FROM auditcore.journey_document_extracted_fields f "
                 "JOIN auditcore.evidence e ON e.tenant_id=f.tenant_id AND e.journey_id=f.journey_id "
                 "AND e.di_document_id=f.di_document_id AND e.association_status='ACTIVE' "
                 "WHERE f.tenant_id=:t AND f.journey_id=:j AND e.document_type_key='gate_pass' "
                 "AND f.field_key='delivery_date'"),
            {"t": journey.tenant_id, "j": journey.journey_id}).scalars().all()
    assert len(facts) == 1 and read == ["2026-10-30"]


def test_a_receipt_with_no_delivery_date_stays_a_receipt(journey) -> None:
    document_id, _ = _receipt(journey, receipt_number="143")
    blank, _ = _receipt(journey, page=2, delivery_date="")
    assert _apply(journey) == []
    assert _types(journey, document_id)[0] == "delivery_receipt" and _types(journey, blank)[0] == "delivery_receipt"


def test_a_booking_that_has_a_gate_pass_keeps_the_receipt_beside_it(journey) -> None:
    add_ready_document(journey, "gate_pass", process_area="DELIVERY", delivery_date="2026-10-29")
    document_id, _ = _receipt(journey, delivery_date="2026-10-30")
    assert _apply(journey) == []
    assert _types(journey, document_id)[0] == "delivery_receipt"


def test_only_one_of_two_receipts_becomes_the_gate_pass(journey) -> None:
    first, _ = _receipt(journey, delivery_date="2026-10-30")
    second, _ = _receipt(journey, page=2, delivery_date="2026-10-30")
    assert len(_apply(journey)) == 1
    assert sorted([_types(journey, first)[0], _types(journey, second)[0]]) == ["delivery_receipt", "gate_pass"]


def test_a_page_a_person_typed_a_delivery_receipt_stays_one(journey) -> None:
    document_id, queue_id = _receipt(journey, delivery_date="2026-10-30")
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_document_queue SET type_overridden_by_actor_id=:a "
                       "WHERE tenant_id=:t AND queue_id=:q"),
                  {"t": journey.tenant_id, "q": queue_id, "a": journey.actor_id})
    assert _apply(journey) == []
    assert _types(journey, document_id)[0] == "delivery_receipt"
