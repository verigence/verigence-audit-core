"""RSA and extended warranty are sold only when the customer was charged for
them (product owner 2026-11-02): "The booking form shouldn't form the basis;
it should be the customer ledger, then it should be asked -- and fix all the
open tasks accordingly." The booking form's RSA / EW amount no longer makes the
RSA Invoice / Extended Warranty Invoice mandatory; the NDC / customer ledger,
cost sheet or a customer invoice does. Other conditions are unchanged."""
from __future__ import annotations

from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import add_ready_document, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import uc03_booking_rebuild as rebuild
from audit_core.db import set_tenant_context
from audit_core.uc03_delivery_documents import _resolve_condition
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_stage import condition_reasons, requirement_items
from audit_core.uc03_p2_task_producer import raise_or_refresh
from audit_core.uc03_p2_worker import settle_journey


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2rsa")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _reasons_and_items(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        reasons = condition_reasons(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        items = requirement_items(connection, get_registry(), tenant_id=journey.tenant_id,
                                  journey_id=journey.journey_id, stage="DELIVERY", reasons=reasons)
    return reasons, {item["key"]: item for item in items}


def _source_value(journey, component_key: str, source_document_type: str, amount: str) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("""INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind,
                    component_key, source_document_type, amount, source_document_id)
                    VALUES (:t, :j, 'COMMERCIAL', :k, :s, :a, :d)"""),
            {"t": journey.tenant_id, "j": journey.journey_id, "k": component_key, "s": source_document_type,
             "a": amount, "d": uuid4()},
        )


CASES = [("rsaSold", "rsa_invoice", "rsa_amount"),
         ("ewSold", "ew_invoice", "additional_warranty_amount"),
         ("ewSold", "ew_invoice", "extended_warranty_amount")]


@pytest.mark.parametrize(("condition", "template", "field"), CASES)
@pytest.mark.parametrize("booking_type", ["booking_docket", "booking_form"])
def test_a_booking_form_amount_alone_does_not_ask_for_the_invoice(journey, condition, template, field, booking_type):
    add_ready_document(journey, booking_type, customer_name="A", booking_date="2026-09-01", **{field: "1500"})
    reasons, items = _reasons_and_items(journey)
    assert condition not in reasons
    assert template not in items


@pytest.mark.parametrize(("condition", "template", "field"), CASES)
def test_the_customer_ledger_amount_asks_for_the_invoice(journey, condition, template, field):
    add_ready_document(journey, "booking_docket", customer_name="A", booking_date="2026-09-01", **{field: "1500"})
    add_ready_document(journey, "customer_ledger", process_area="DELIVERY", **{field: "1500"})
    reasons, items = _reasons_and_items(journey)
    assert "customer ledger" in reasons[condition]
    assert items[template]["required"] is True and items[template]["received"] is False


@pytest.mark.parametrize(("condition", "template", "field"), CASES)
def test_a_booking_form_source_value_alone_does_not_ask_for_the_invoice(journey, condition, template, field):
    _source_value(journey, field, "booking_form", "1500")
    _source_value(journey, field, "booking_docket", "1500")
    reasons, items = _reasons_and_items(journey)
    assert condition not in reasons and template not in items


@pytest.mark.parametrize("source", ["customer_ledger", "cost_sheet", "erp_settlement", "customer_invoice_dms",
                                    "tax_invoice_tally"])
@pytest.mark.parametrize(("condition", "template", "field"), CASES)
def test_a_charged_source_value_asks_for_the_invoice(journey, condition, template, field, source):
    _source_value(journey, field, source, "1500")
    reasons, items = _reasons_and_items(journey)
    assert condition in reasons and items[template]["required"] is True


@pytest.mark.parametrize(("condition", "field"), [("rsaSold", "rsa_amount"), ("ewSold", "additional_warranty_amount")])
def test_the_oem_wholesale_invoice_and_a_credit_note_do_not_sell_it(journey, condition, field):
    _source_value(journey, field, "wholesale_invoice", "1500")
    _source_value(journey, field, "credit_note", "1500")
    reasons, _ = _reasons_and_items(journey)
    assert condition not in reasons


def test_no_condition_reads_the_booking_docket(journey):
    """2026-11-03: the booking docket is information only (product owner
    2026-11-02); every condition, not only RSA and EW, is made true by a
    billing document's figure."""
    add_ready_document(journey, "booking_docket", customer_name="A", booking_date="2026-09-01",
                       accessories_cost="12500", rsa_amount="1500")
    reasons, _ = _reasons_and_items(journey)
    assert "accessoriesSold" not in reasons and "rsaSold" not in reasons
    _source_value(journey, "accessories_cost", "customer_ledger", "12500")
    reasons, _ = _reasons_and_items(journey)
    assert "accessoriesSold" in reasons


def test_the_legacy_requirement_resolver_follows_the_same_rule(journey):
    """uc03_delivery_documents._resolve_condition (rsaTaken /
    extendedWarrantyTaken) reads the same rule: a booking-form amount on the
    Deal is "No", a charged one "Yes", nothing at all still unknown."""
    def resolve(key):
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            return _resolve_condition(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                      condition_key=key)

    assert resolve("rsaTaken") is None and resolve("extendedWarrantyTaken") is None
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("""INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, actual_amount)
                    VALUES (:t, :j, 'rsa_amount', 1500), (:t, :j, 'additional_warranty_amount', 9999)"""),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
    _source_value(journey, "rsa_amount", "booking_form", "1500")
    _source_value(journey, "additional_warranty_amount", "booking_form", "9999")
    assert resolve("rsaTaken") is False and resolve("extendedWarrantyTaken") is False
    _source_value(journey, "rsa_amount", "customer_ledger", "1500")
    assert resolve("rsaTaken") is True and resolve("extendedWarrantyTaken") is False


def _open_missing_tasks(journey) -> list[str]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return sorted(connection.execute(
            text("SELECT reference->>'requirementKey' FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j "
                 "AND task_type='DOCUMENT_MISSING' AND task_status IN "
                 "('READY','IN_PROGRESS','RETURNED','ACTION_COMPLETED','VERIFYING')"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalars().all())


def _raise_old_task(journey, requirement_key: str, label: str) -> None:
    """The task as the old rule raised it from the booking form."""
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        raise_or_refresh(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            dedupe_key=f"document-missing:{journey.journey_id}:{requirement_key}",
            task_type="DOCUMENT_MISSING", source_type="DOCUMENT", source_code="DOCUMENT_MISSING",
            title=f"Upload the {label}", description="The booking docket shows an amount.",
            reference={"generatedBy": "SYSTEM", "sourceType": "DOCUMENT", "sourceCode": "DOCUMENT_MISSING",
                       "requirementKey": requirement_key, "templateKeys": [requirement_key],
                       "conditions": [], "stage": "DELIVERY"},
            severity="HIGH", registry=get_registry(),
        )


def _settle(journey) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        settle_journey(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def test_an_open_task_raised_from_the_booking_form_closes_on_settle(journey):
    add_ready_document(journey, "booking_docket", customer_name="A", booking_date="2026-09-01",
                       rsa_amount="1500", additional_warranty_amount="9999")
    add_ready_document(journey, "gate_pass", process_area="DELIVERY", gate_pass_number="GP-1")  # delivery started
    _raise_old_task(journey, "rsa_invoice", "RSA Invoice")
    _raise_old_task(journey, "ew_invoice", "Extended Warranty Invoice")
    assert _open_missing_tasks(journey) == ["ew_invoice", "rsa_invoice"]

    # The BOOKING_REBUILD dry run finds them closing and writes nothing.
    totals = rebuild.rebuild_tenant(journey.engine, tenant_id=journey.tenant_id, tag="t", commit=False)
    assert totals["closedByTask"] == {"DOCUMENT_MISSING: Upload the RSA Invoice": 1,
                                      "DOCUMENT_MISSING: Upload the Extended Warranty Invoice": 1}
    assert _open_missing_tasks(journey) == ["ew_invoice", "rsa_invoice"]

    _settle(journey)
    assert _open_missing_tasks(journey) == []


def test_a_task_charged_on_the_ledger_stays_open(journey):
    add_ready_document(journey, "booking_docket", customer_name="A", booking_date="2026-09-01", rsa_amount="1500")
    add_ready_document(journey, "customer_ledger", process_area="DELIVERY", rsa_amount="1500")
    _settle(journey)
    assert "rsa_invoice" in _open_missing_tasks(journey)
    _settle(journey)
    assert "rsa_invoice" in _open_missing_tasks(journey)


def test_the_rebuild_job_can_be_limited_to_one_project(journey):
    """BOOKING_REBUILD with the tag '<tenant_id>:<label>' runs over that
    Project only, and returns its totals for the job row."""
    add_ready_document(journey, "booking_docket", customer_name="A", booking_date="2026-09-01", rsa_amount="1500")
    add_ready_document(journey, "gate_pass", process_area="DELIVERY", gate_pass_number="GP-1")
    _raise_old_task(journey, "rsa_invoice", "RSA Invoice")
    result = rebuild.run_booking_rebuild(journey.engine, f"{journey.tenant_id}:rsa-ew-1102",
                                         ["some-other-tenant", journey.tenant_id])
    assert result["commit"] is False and list(result["projects"]) == [journey.tenant_id]
    assert result["projects"][journey.tenant_id]["closedByTask"] == {"DOCUMENT_MISSING: Upload the RSA Invoice": 1}
    assert _open_missing_tasks(journey) == ["rsa_invoice"]  # a dry run writes nothing
    with pytest.raises(ValueError):
        rebuild.run_booking_rebuild(journey.engine, "not-a-project:x", [journey.tenant_id])
