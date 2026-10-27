"""Finance rule repair (2026-10-27): a dry run lists and changes nothing; a
commit removes a loan from "CASH", creates the loan the RTO proves, leaves a
person's entry alone and queues a reconcile for each changed Journey."""
from __future__ import annotations

from uuid import uuid4

import pytest
from p2_support import (
    add_evidence,
    add_extracted_field,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_finance_rule_repair import run_finance_rule_repair


@pytest.fixture
def engine():
    engine = database_engine()
    yield engine
    engine.dispose()


def _loan(journey, provider, kind="EVIDENCE"):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.finance_records (tenant_id, journey_id, finance_type_code, provider_name, "
                 "source_kind, details) VALUES (:t, :j, 'LOAN', :p, :k, '{}'::jsonb)"),
            {"t": journey.tenant_id, "j": journey.journey_id, "p": provider, "k": kind})


def _doc(journey, document_type, **fields):
    document = uuid4()
    add_evidence(journey, di_document_id=document, document_type_key=document_type, process_area="DELIVERY")
    for key, value in fields.items():
        add_extracted_field(journey, di_document_id=document, field_key=key, value=value,
                            document_type=document_type, confidence=92.0)


def _providers(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT provider_name FROM auditcore.finance_records WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id}).scalars().all()


def _reconciles(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT count(*) FROM auditcore.p2_work_queue WHERE tenant_id=:t AND journey_id=:j "
                 "AND work_type='JOURNEY_RECONCILE' AND payload->>'reason' = 'FINANCE_RULE'"),
            {"t": journey.tenant_id, "j": journey.journey_id}).scalar_one()


def test_dry_run_changes_nothing_and_commit_follows_the_rule(engine) -> None:
    cash = create_p2_journey(engine, prefix="finrepair")
    _loan(cash, "CASH")
    _doc(cash, "customer_invoice_dms", financed_by="CASH")

    run_finance_rule_repair(engine, "t-dry", [cash.tenant_id])
    assert _providers(cash) == ["CASH"] and _reconciles(cash) == 0

    run_finance_rule_repair(engine, "t-commit", [cash.tenant_id])
    assert _providers(cash) == [] and _reconciles(cash) == 1


def test_commit_creates_a_proven_loan_and_keeps_a_persons_entry(engine) -> None:
    financed = create_p2_journey(engine, prefix="finrepair")
    _doc(financed, "rto_challan", financer_name="CANARA BANK", hp_charges_amount="1500")
    entered = create_p2_journey(engine, prefix="finrepair")
    _loan(entered, "SBI", kind="OPERATIONAL_INPUT")

    run_finance_rule_repair(engine, "t-commit", [financed.tenant_id, entered.tenant_id])
    assert _providers(financed) == ["CANARA BANK"]
    assert _providers(entered) == ["SBI"]
    assert _reconciles(entered) == 0


def test_the_job_kind_is_registered() -> None:
    from audit_core.uc03_p2_worker import _one_shot_target

    engine = database_engine()
    try:
        target, _args = _one_shot_target("FINANCE_RULE_REPAIR", engine=engine, tag="x", reports=[])
    finally:
        engine.dispose()
    assert target is run_finance_rule_repair
