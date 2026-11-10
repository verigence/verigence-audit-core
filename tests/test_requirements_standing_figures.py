"""A document is asked for only on a figure that still stands (2026-11-10).

UTK-SU-261020-005: the NDC's first reading had the struck-out accessories
Rs 7,000; the Reviewer cleared that line. The Deal dropped it, but the figure
recorded from the first reading kept "Upload the Accessory Invoice" open.
Real Postgres."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import add_extracted_field, create_p2_journey, database_engine
from sqlalchemy import text
from test_uc03_account_statement import UTKAL, _ledger, _set_policy, _sync

from audit_core.db import set_tenant_context
from audit_core.uc03_deal_source_policy import POLICY_ACCOUNT_STATEMENT
from audit_core.uc03_p2_stage import condition_reasons


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="stnd")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _reasons(journey) -> dict:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return condition_reasons(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def test_accessories_cleared_on_the_ndc_no_longer_ask_for_the_accessory_invoice(journey):
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    document_id, _ = _ledger(journey, UTKAL)
    _sync(journey)
    assert "accessoriesSold" in _reasons(journey)  # Accessories 30,000 on the NDC

    # A person clears the line: the reading changes, the recorded figure stays behind.
    cleared = [{**row, "debit_amount": None} if row["particulars_raw"] == "Accessories" else row for row in UTKAL]
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("DELETE FROM auditcore.journey_document_extracted_fields WHERE di_document_id=:d"),
                  {"d": document_id})
    add_extracted_field(journey, di_document_id=document_id, field_key="line_items", value=cleared,
                        document_type="customer_ledger")
    _sync(journey)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        recorded = c.execute(text("SELECT count(*) FROM auditcore.commercial_line_source_values WHERE journey_id=:j "
                                  "AND component_key LIKE 'accessor%'"), {"j": journey.journey_id}).scalar_one()
    assert recorded >= 1  # still there, as live
    assert "accessoriesSold" not in _reasons(journey)
    assert "rsaSold" in _reasons(journey)  # the other lines still stand
