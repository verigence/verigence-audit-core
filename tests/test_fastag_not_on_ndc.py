"""FASTag the NDC does not charge but an invoice does (product owner 2026-11-18):
in the Deal and the Audit Summary from the invoice, a system remark, and the
journey's observation severity HIGH. Only with the NDC on file."""
from __future__ import annotations

from decimal import Decimal as D

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from test_deal_issues_1111 import _deal, _posting, _row, _standard, _statement, _without
from test_uc03_account_statement import UTKAL

from audit_core.db import set_tenant_context


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="ft1118")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _tally_fastag(amount: int = 600) -> list[dict]:
    return [_posting("FAST TAG (SALES)", "Sales_Inv", debit=amount),
            _posting("Sales BOLERO NEO", "Vehicle Sales_ Mahindra", debit=1049390)]


def test_a_fastag_on_the_tally_invoice_and_not_on_the_ndc_is_in_the_deal(journey) -> None:
    _statement(journey, _without(UTKAL, "Fastag charges"), tally=_tally_fastag())
    _standard(journey, "fastag_amount", "500")
    view = _deal(journey)
    row = _row(view, "fastag_amount")
    assert D(row["billed"]) == D(600) and D(row["current"]) == D(600)
    assert "FASTAG_NOT_ON_NDC" in row["flags"] and not row.get("notTaken")
    assert view["fastagNotOnNdc"] == {"amount": "600.00", "source": "Tally invoice (customer ledger)"}


def test_a_fastag_on_the_ndc_is_the_ndcs_and_no_observation(journey) -> None:
    _statement(journey, UTKAL, tally=_tally_fastag(700))  # the NDC charges 600
    view = _deal(journey)
    row = _row(view, "fastag_amount")
    assert D(row["current"]) == D(600) and "FASTAG_NOT_ON_NDC" not in row["flags"]
    assert view["fastagNotOnNdc"] is None


def test_no_fastag_anywhere_stays_not_taken(journey) -> None:
    _statement(journey, _without(UTKAL, "Fastag charges"))
    _standard(journey, "fastag_amount", "500")
    view = _deal(journey)
    assert _row(view, "fastag_amount")["notTaken"] is True and view["fastagNotOnNdc"] is None


def test_the_journey_is_a_high_observation_and_a_remark_says_why(journey) -> None:
    from audit_core.uc03_observation_severity import journey_severity
    from audit_core.uc03_p2_price_variance import refresh_price_variance
    from audit_core.uc03_system_remarks import system_remarks

    _statement(journey, _without(UTKAL, "Fastag charges"), tally=_tally_fastag())
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        refresh_price_variance(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        severity = journey_severity(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        remarks = system_remarks(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert severity["severity"] == "HIGH" and severity["system"] == "HIGH" and severity["override"] is None
    assert [r["code"] for r in severity["reasons"]] == ["FASTAG_NOT_ON_NDC"]
    [remark] = [r for r in remarks["remarks"] if r["code"] == "FASTAG_NOT_ON_NDC"]
    assert "NDC does not charge" in remark["text"] and "₹600" in remark["text"]
