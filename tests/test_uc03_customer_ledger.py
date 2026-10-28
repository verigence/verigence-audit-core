"""The NDC (account statement) and the customer ledger are two documents
(product owner 2026-10-28) though DI files both as customer_ledger."""
from __future__ import annotations

import json

import pytest
from conftest import delete_tenant_data
from p2_support import (
    AllowAllAuthorization,
    add_ready_document,
    create_p2_journey,
    database_engine,
    principal,
)

from audit_core.db import set_tenant_context
from audit_core.uc03_account_statement import sync_account_statement
from audit_core.uc03_customer_ledger import (
    PAYMENT_LEDGER_TEMPLATE,
    is_payment_ledger,
    payment_ledger_ids,
)
from audit_core.uc03_p2_api import list_documents
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_stage import (
    field_review_reasons,
    ready_document_counts,
    requirement_items,
)

# Pawan Gupta's "VEHICLE ACCOUNT STATEMENT" (UTK-SA-261027-002), as read.
STATEMENT = json.dumps([
    {"particulars_raw": "Vehicle cost", "debit_amount": 2428999},
    {"particulars_raw": "Insurance amount", "debit_amount": "OWN"},
    {"particulars_raw": "Registration", "debit_amount": 243639},
    {"particulars_raw": "Accessories", "debit_amount": 11355},
    {"particulars_raw": "Corporate Discount", "credit_amount": 9853},
    {"particulars_raw": "MR No.CDAM-1976", "credit_amount": 2698429},
])
TALLY = json.dumps([
    {"entry_date": "2026-09-09", "particulars_raw": "RAMAKRUSHNA PATTNAYAK- BJZPP8427M",
     "voucher_type": "JOURNAL_NONGST", "voucher_number": "24730", "credit_amount": 21000,
     "running_balance": "21,000.00 Cr"},
    {"entry_date": "2026-09-30", "particulars_raw": "Sales - Vehicle", "debit_amount": 1054828,
     "running_balance": "10,33,828.00 Dr"},
])
ERP = json.dumps([
    {"particulars_raw": "Ex-Showroom", "debit_amount": 912000},
    {"particulars_raw": "TOTAL VEHICLE COST", "debit_amount": 1076160},
])
ZERO_BALANCE = json.dumps([{"particulars_raw": "Vehicle cost", "debit_amount": 900000, "running_balance": 0}])


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2led")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def test_what_was_read_tells_a_ledger_from_the_statement():
    assert not is_payment_ledger(STATEMENT)
    assert is_payment_ledger(TALLY)
    assert is_payment_ledger(ERP)
    assert not is_payment_ledger(ZERO_BALANCE)
    assert not is_payment_ledger(None) and not is_payment_ledger("[]")


def test_the_sql_test_agrees_with_the_python_one(journey):
    cases = {"statement": STATEMENT, "tally": TALLY, "erp": ERP, "zero": ZERO_BALANCE, "blank": "[]",
             "as_array": json.loads(TALLY)}
    ids = {name: add_ready_document(journey, "customer_ledger", process_area="DELIVERY", line_items=value)
           for name, value in cases.items()}
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        ledgers = payment_ledger_ids(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    for name, value in cases.items():
        assert (str(ids[name]) in ledgers) == is_payment_ledger(value), name


def _checklist(connection, journey):
    registry = get_registry()
    counts = ready_document_counts(connection, registry, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    items = requirement_items(connection, registry, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                              stage="DELIVERY", reasons={}, ready_counts=counts)
    return counts, {item["key"]: item for item in items}


def test_a_ledger_alone_does_not_meet_the_ndc(journey):
    add_ready_document(journey, "customer_ledger", process_area="DELIVERY", line_items=TALLY)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        counts, items = _checklist(connection, journey)
    assert counts["customer_ledger"] == 0 and counts[PAYMENT_LEDGER_TEMPLATE] == 1
    assert items["customer_ledger"]["required"] and not items["customer_ledger"]["received"]
    assert items[PAYMENT_LEDGER_TEMPLATE]["received"] and not items[PAYMENT_LEDGER_TEMPLATE]["required"]


def test_both_on_file_each_meets_its_own(journey):
    """"If both documents come, our system should be able to extract and
    update data": the statement fills the Deal and the ledger stays a ledger."""
    statement = add_ready_document(journey, "customer_ledger", process_area="DELIVERY", line_items=STATEMENT)
    ledger = add_ready_document(journey, "customer_ledger", process_area="DELIVERY", line_items=TALLY)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        counts, _items = _checklist(connection, journey)
        assert counts["customer_ledger"] == 1 and counts[PAYMENT_LEDGER_TEMPLATE] == 1
        result = sync_account_statement(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        listing = list_documents(tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                 human_principal=principal(journey), authorization_client=AllowAllAuthorization(),
                                 connection=connection)
    assert result["statements"] == 1 and result["tallies"] == 1 and result["commercialLines"] >= 3
    by_id = {d["documentId"]: d for d in listing["documents"]}
    assert by_id[str(statement)]["templateKey"] == "customer_ledger"
    assert by_id[str(ledger)]["templateKey"] == PAYMENT_LEDGER_TEMPLATE
    assert by_id[str(ledger)]["displayName"] == "Customer Ledger"
    checklist = {c["templateKey"]: c for c in listing["checklist"] if c["stage"] == "DELIVERY"}
    assert checklist["customer_ledger"]["documentIds"] == [str(statement)]
    assert checklist[PAYMENT_LEDGER_TEMPLATE]["documentIds"] == [str(ledger)]


def test_a_ledger_is_not_held_to_the_statement_balance():
    registry = get_registry()
    template = registry.document("customer_ledger")
    values = {"line_items": TALLY, "total_cost": "1054828", "total_receipts": "21000", "balance_amount": "0"}
    assert "STATEMENT_DOES_NOT_BALANCE" not in field_review_reasons(
        registry, template, di_type="customer_ledger", field_key="balance_amount", value="0", confidence=99.0,
        document_values=values)


def test_a_customer_ledger_page_is_the_ndc_unless_it_reads_as_a_ledger():
    assert get_registry().template_for_di_type("customer_ledger", stage="DELIVERY").key == "customer_ledger"

