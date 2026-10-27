"""Finance rule edge cases found in review (2026-10-27): values that name no
financier, receipts from the customer, HPA lines, person-entered records with
no source kind, the newest reading only, the last Delivery document removed,
and the disbursement review coming back when finance does."""
from __future__ import annotations

from uuid import uuid4

import pytest
from p2_support import add_evidence, add_extracted_field, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_finance_rule import (
    _amount,
    apply_finance_rule,
    hypothecation_line_amount,
    names_a_financier,
    receipt_financier,
    receipt_from_financier_for_customer,
)


@pytest.mark.parametrize("value", [
    "CASH", " cash ", "N.A.", "NA.", "N.A", "-NA-", "NOT HYPOTHECATED", "No", "None.", "NIL.", "By Cash",
    "CASH SALES", "Cash Customer", "Self Finance", "SELF FINANCED", "0.00", "Cash/Self", "Hypothecation: NIL",
    "Own", "", None,
])
def test_values_that_name_no_financier(value) -> None:
    assert not names_a_financier(value)


@pytest.mark.parametrize("value", ["HDFC Bank", "SBI", "MMFSL", "Cholamandalam Investment & Finance Co. Ltd."])
def test_values_that_name_a_financier(value) -> None:
    assert names_a_financier(value)


def test_receipts_from_the_customer_are_not_finance() -> None:
    customers = ("UCO BANK", "R K SAHOO")
    assert receipt_financier("UCO BANK", customer_names=customers) is None  # a bank buying its own vehicle
    assert receipt_financier("Prakash (Self) through HDFC Bank") is None
    assert receipt_financier("STATE BANK OF INDIA - NEFT") is None
    assert receipt_financier("R K SAHOO (DO)", customer_names=customers) is None
    assert receipt_from_financier_for_customer("R K SAHOO (DO)", customer_names=customers)  # finance, unnamed
    assert receipt_financier("BAJAJ FINSERV") == "BAJAJ FINSERV"
    assert receipt_financier("IDFC FIRST (FINANCE)") == "IDFC FIRST"


def test_hpa_lines_and_rupee_amounts() -> None:
    assert hypothecation_line_amount([{"description_raw": "HPA Charges", "total": 1500}]) == 1500
    assert hypothecation_line_amount([{"description_raw": "H.P.A.", "amount": "Rs. 1,500/-"}]) == 1500
    assert _amount("Rs. 1500") == 1500 and _amount("₹ 2,000.50") == 2000.50


@pytest.fixture
def journey():
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="finedge")
    yield journey
    engine.dispose()


def _apply(journey, **kw):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return apply_finance_rule(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, **kw)


def _rows(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT provider_name, source_kind FROM auditcore.finance_records WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id}).mappings().all()


def test_a_record_with_no_source_kind_is_a_persons_and_kept(journey) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("INSERT INTO auditcore.finance_records (tenant_id, journey_id, finance_type_code, "
                                "provider_name, details) VALUES (:t, :j, 'LOAN', 'SBI', '{}'::jsonb)"),
                           {"t": journey.tenant_id, "j": journey.journey_id})
    assert _apply(journey)["action"] == "KEPT_PERSON_ENTERED"
    assert [r["provider_name"] for r in _rows(journey)] == ["SBI"]


def test_only_the_newest_reading_of_a_field_counts(journey) -> None:
    document = uuid4()
    add_evidence(journey, di_document_id=document, document_type_key="rto_challan", process_area="DELIVERY")
    add_extracted_field(journey, di_document_id=document, field_key="financer_name", value="HDFC BANK",
                        document_type="rto_challan")
    with journey.engine.begin() as connection:  # a newer reading: no financier printed
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text(
            "INSERT INTO auditcore.journey_document_extracted_fields (tenant_id, journey_id, di_document_id, "
            "source_fact_version, field_key, stage_code, source_canonical_field_id, source_document_type_key, "
            "extracted_value, effective_value) VALUES (:t, :j, :d, 2, 'financer_name', 'BOOKING', 'cf-financer_name', "
            "'rto_challan', 'null'::jsonb, 'null'::jsonb)"),
            {"t": journey.tenant_id, "j": journey.journey_id, "d": document})
    for _ in range(3):  # stable across passes
        assert _apply(journey)["action"] == "NONE"


def test_removing_the_last_delivery_document_removes_the_loan(journey) -> None:
    from audit_core.uc03_delivery_post_extraction_materialization import (
        materialize_delivery_documents_from_durable_store,
    )

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("INSERT INTO auditcore.finance_records (tenant_id, journey_id, finance_type_code, "
                                "provider_name, source_kind, details) VALUES (:t, :j, 'LOAN', 'CASH', 'EVIDENCE', '{}')"),
                           {"t": journey.tenant_id, "j": journey.journey_id})
        result = materialize_delivery_documents_from_durable_store(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert result["reason"] == "no_documents" and result["finance"] == "REMOVED"
    assert _rows(journey) == []


def test_the_disbursement_review_comes_back_when_finance_does(journey) -> None:
    from audit_core.uc03_finance_disbursement_resolution import TASK_TYPE, resolve_finance_disbursement

    def open_tasks():
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            return connection.execute(text(
                "SELECT count(*) FROM auditcore.workflow_tasks WHERE tenant_id=:t AND journey_id=:j AND task_type=:k "
                "AND task_status IN ('PENDING','READY','CLAIMED','IN_PROGRESS','RETRY_WAIT')"),
                {"t": journey.tenant_id, "j": journey.journey_id, "k": TASK_TYPE}).scalar_one()

    def resolve():
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            resolve_finance_disbursement(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                         correlation_id="t")

    document = uuid4()
    add_evidence(journey, di_document_id=document, document_type_key="rto_challan", process_area="DELIVERY")
    add_extracted_field(journey, di_document_id=document, field_key="financer_name", value="CANARA BANK",
                        document_type="rto_challan")
    assert _apply(journey)["action"] == "CREATED"
    resolve()
    assert open_tasks() == 1
    with journey.engine.begin() as connection:  # the RTO challan is voided
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.evidence SET association_status='VOIDED' WHERE tenant_id=:t "
                                "AND di_document_id=:d"), {"t": journey.tenant_id, "d": document})
    assert _apply(journey)["action"] == "REMOVED" and open_tasks() == 0
    again = uuid4()  # re-uploaded
    add_evidence(journey, di_document_id=again, document_type_key="rto_challan", process_area="DELIVERY")
    add_extracted_field(journey, di_document_id=again, field_key="financer_name", value="CANARA BANK",
                        document_type="rto_challan")
    assert _apply(journey)["action"] == "CREATED"
    resolve()
    assert open_tasks() == 1
