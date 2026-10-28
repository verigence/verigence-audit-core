"""Insurance policies read before insurance_cover 1.3 are read again for the
financier they are hypothecated to (product owner 2026-10-27); once read, a
policy is not picked again, and an insurance financier proves the loan."""
from __future__ import annotations

from uuid import uuid4

import pytest
from p2_support import (
    add_evidence,
    add_extracted_field,
    create_p2_journey,
    database_engine,
)

from audit_core.db import set_tenant_context
from audit_core.uc03_finance_rule import apply_finance_rule
from audit_core.uc03_insurance_hypothecation_reread import (
    journeys_to_reread,
    run_insurance_hypothecation_reread,
)


@pytest.fixture
def journey():
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="hypreread")
    yield journey
    engine.dispose()


def _picked(journey) -> list[str]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [str(j["journey_id"]) for j in journeys_to_reread(connection, journey.tenant_id)]


def test_a_policy_without_a_hypothecation_reading_is_picked_once(journey) -> None:
    document = uuid4()
    add_evidence(journey, di_document_id=document, document_type_key="insurance_cover", process_area="DELIVERY")
    add_extracted_field(journey, di_document_id=document, field_key="insurer_name", value="SBI General",
                        document_type="insurance_cover")
    assert str(journey.journey_id) in _picked(journey)

    # Read again under 1.3: the policy now holds a hypothecated_to reading.
    add_extracted_field(journey, di_document_id=document, field_key="hypothecated_to",
                        value="STATE BANK OF INDIA,MOHALI", document_type="insurance_cover")
    assert str(journey.journey_id) not in _picked(journey)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        result = apply_finance_rule(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert result["action"] == "CREATED"
    assert result["provider"] == "STATE BANK OF INDIA,MOHALI"
    assert result["basis"] == ["Insurance policy is hypothecated to STATE BANK OF INDIA,MOHALI"]


def test_the_job_is_a_dry_run_unless_committed(journey) -> None:
    from audit_core.uc03_p2_worker import _one_shot_target

    document = uuid4()
    add_evidence(journey, di_document_id=document, document_type_key="insurance_cover", process_area="DELIVERY")
    add_extracted_field(journey, di_document_id=document, field_key="insurer_name", value="SBI General",
                        document_type="insurance_cover")
    run_insurance_hypothecation_reread(journey.engine, "t-dry", [journey.tenant_id])  # asks DI nothing
    assert str(journey.journey_id) in _picked(journey)
    target, _args = _one_shot_target("INSURANCE_HYPOTHECATION_REREAD", engine=journey.engine, tag="x", reports=[])
    assert target is run_insurance_hypothecation_reread


def test_only_a_live_policy_is_picked(journey) -> None:
    """A policy since replaced (SUPERSEDED) is not read again (2026-10-28)."""
    from audit_core.uc03_insurance_hypothecation_reread import _documents_to_reread

    old, new = uuid4(), uuid4()
    add_evidence(journey, di_document_id=old, document_type_key="insurance_cover", process_area="DELIVERY",
                 status="SUPERSEDED")
    add_evidence(journey, di_document_id=new, document_type_key="insurance_cover", process_area="DELIVERY")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        picked = _documents_to_reread(connection, journey.tenant_id)
        assert str(new) in picked and str(old) not in picked
