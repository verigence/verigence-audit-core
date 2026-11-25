"""A quotation is not the cost sheet or the NDC (product owner 2026-11-25: "quotation
is not cost sheet or NDC though it look same.. we have to be only following NDC or
Cost sheet.. yes fix if the same document is feeding other deal lines")."""
from __future__ import annotations

from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_evidence,
    add_extracted_field,
    add_page,
    add_ready_document,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import uc03_insurance_source as insurance
from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.uc03_quotation import (
    bookings_holding_a_quotation,
    is_quotation_title,
    request_quotation_recomputes,
    set_aside_quotations,
)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2qt")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


# The cost-sheet titles read on Mahindra Orissa, 25 Nov.
@pytest.mark.parametrize(("title", "quotation"), [
    ("QUOTATION", True), ("Quotation", True), ("QUOTATION CUM PROFORMA INVOICE", True),
    ("QUOTATION / PROFORMA INVOICE", True), ("PROFORMA INVOICE", True), ("PRO-FORMA INVOICE", True),
    ("Performa Invoice", True), ('"QUOTATION"', True),
    ("COST SHEET", False), ("Cost Sheet cum Quotation", False), ("VEHICLE ACCOUNT STATEMENT", False),
    ("OFFER LETTER / PAYMENT DETAILS", False), ("APPROVAL SHEET", False),
    ("TRANSACTION REGULARIZATION FORM", False), ("", False), (None, False),
])
def test_which_titles_are_a_quotation(title, quotation) -> None:
    assert is_quotation_title(title) is quotation


def _cost_sheet(journey, *, title, insurance_amount="40210", page=1):
    document_id = add_ready_document(journey, "cost_sheet", process_area="DELIVERY", document_title=title,
                                     insurance_amount=insurance_amount)
    _, queue_id, _ = add_page(journey, page_number=page, status="READY", di_document_id=document_id)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind, "
                       "component_key, source_document_type, amount, source_document_id) "
                       "VALUES (:t, :j, 'COMMERCIAL', 'insurance_amount', 'cost_sheet', :a, :d)"),
                  {"t": journey.tenant_id, "j": journey.journey_id, "a": insurance_amount, "d": document_id})
    return document_id, queue_id


def _set_aside(journey):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return set_aside_quotations(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _premium(journey):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return insurance.premium_on_dealer_statement(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _page(journey, queue_id):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return dict(c.execute(
            text("SELECT queue_status, template_key, display_name, status_reason, type_overridden_by_actor_id "
                 "FROM auditcore.p2_document_queue WHERE tenant_id=:t AND queue_id=:q"),
            {"t": journey.tenant_id, "q": queue_id}).mappings().one())


def _evidence(journey, document_id):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return c.execute(text("SELECT association_status, voided_by_actor_id FROM auditcore.evidence "
                              "WHERE tenant_id=:t AND di_document_id=:d"),
                         {"t": journey.tenant_id, "d": document_id}).one()


def test_a_quotation_leaves_the_booking_and_stays_on_file(journey) -> None:
    document_id, queue_id = _cost_sheet(journey, title="QUOTATION")
    assert _premium(journey) == 40210.0  # as the NDC's premium, before

    aside = _set_aside(journey)

    assert [(a["diDocumentId"], a["name"]) for a in aside] == [(str(document_id), "Quotation")]
    assert tuple(_evidence(journey, document_id)) == ("SUPERSEDED", "SYSTEM:QUOTATION")
    page = _page(journey, queue_id)
    assert (page["queue_status"], page["template_key"], page["display_name"], page["type_overridden_by_actor_id"]) == (
        "SUPPORTING", "supporting_document", "Quotation", "SYSTEM:QUOTATION")
    assert "not the cost sheet or the NDC" in page["status_reason"]
    # no figure is taken from it any more: the insurance premium was the live case
    assert _premium(journey) is None
    assert _set_aside(journey) == []  # asked again, nothing more happens


def test_a_cost_sheet_and_an_untitled_one_stay(journey) -> None:
    _cost_sheet(journey, title="COST SHEET", insurance_amount="35000", page=1)
    document_id = add_ready_document(journey, "cost_sheet", process_area="DELIVERY", insurance_amount="1")
    assert _set_aside(journey) == []
    assert _evidence(journey, document_id)[0] == "ACTIVE"
    assert _premium(journey) == 35000.0


def test_a_page_a_person_typed_as_the_cost_sheet_stays(journey) -> None:
    document_id, queue_id = _cost_sheet(journey, title="QUOTATION CUM PROFORMA INVOICE")
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_document_queue SET type_overridden_by_actor_id=:a "
                       "WHERE tenant_id=:t AND queue_id=:q"),
                  {"t": journey.tenant_id, "q": queue_id, "a": journey.actor_id})
        assert bookings_holding_a_quotation(c, tenant_id=journey.tenant_id) == []
    assert _set_aside(journey) == []
    assert _evidence(journey, document_id)[0] == "ACTIVE"


def test_banita_sasmal_a_quoted_premium_no_longer_makes_the_insurance_the_dealers(journey, monkeypatch) -> None:
    """Live 25 Nov, UTK-JP-261031-001: the policy through a broker, the NDC's insurance
    row 0, the quotation quoting Rs 40,210 -- marked Dealership on the quotation."""
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("INSERT INTO auditcore.insurance_records (tenant_id, journey_id, insurer_name, policy_reference, "
                       "agent_intermediary_name, agent_intermediary_code, source_kind) VALUES (:t, :j, "
                       "'THE NEW INDIA ASSURANCE CO. LTD.', :p, 'Certigo Insurance Brokers Private Limited', "
                       "'BR00001212', 'EVIDENCE')"),
                  {"t": journey.tenant_id, "j": journey.journey_id, "p": uuid4().hex[:12]})
    ndc = uuid4()
    add_evidence(journey, di_document_id=ndc, document_type_key="customer_ledger", process_area="DELIVERY")
    add_extracted_field(journey, di_document_id=ndc, field_key="line_items", document_type="customer_ledger",
                        value=[{"particulars_raw": "Insurance amount", "debit_amount": 0}])
    _cost_sheet(journey, title="QUOTATION", insurance_amount="40210")
    # the NDC read as a statement (its full rows are not what this test is about)
    from audit_core import uc03_account_statement
    monkeypatch.setattr(uc03_account_statement, "statement_reading", lambda *a, **k: {"read": True, "unread": []})
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        before = insurance.document_decision(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert before is not None and before["basis"] == "The NDC charges the insurance premium (Rs 40,210)."

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        worker.settle_journey(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        assert insurance.document_decision(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id) is None
        decision = insurance.resolve(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert (decision["source"], decision["decidedBy"]) == ("SELF", "DOCUMENT")
    assert "does not carry the dealer's MISP / Agent code and the NDC charges no insurance premium" in decision["basis"]


def test_the_sweep_asks_a_recompute_of_each_booking_holding_a_quotation(journey) -> None:
    _cost_sheet(journey, title="Proforma Invoice")
    dry = request_quotation_recomputes(journey.engine, journey.tenant_id, commit=False)
    assert (dry["bookings"], dry["journeyIds"]) == (1, [str(journey.journey_id)])
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        before = c.execute(text("SELECT coalesce(max(fact_version), 0) FROM auditcore.p2_journey_runtime "
                                "WHERE tenant_id=:t AND journey_id=:j"),
                           {"t": journey.tenant_id, "j": journey.journey_id}).scalar()
    assert request_quotation_recomputes(journey.engine, journey.tenant_id)["bookings"] == 1
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        after = c.execute(text("SELECT coalesce(max(fact_version), 0) FROM auditcore.p2_journey_runtime "
                               "WHERE tenant_id=:t AND journey_id=:j"),
                          {"t": journey.tenant_id, "j": journey.journey_id}).scalar()
        assert after > before
    _set_aside(journey)
    assert request_quotation_recomputes(journey.engine, journey.tenant_id, commit=False)["bookings"] == 0
