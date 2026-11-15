"""The Rule Engine checks moved into Audit Core (product owner 2026-11-15, rule
review decision 1), corrected where the Rule Engine compared them wrongly; on a
real Postgres."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from conftest import delete_tenant_data
from p2_support import add_ready_document, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import uc03_document_consistency_rules as rules
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_audit_rules import RULE_CODES, _Facts
from audit_core.uc03_p2_registry import get_registry


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine)
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _check(journey, check):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return check(_Facts(connection, journey.tenant_id, journey.journey_id))


def _iso(day: date) -> str:
    return day.isoformat()


TODAY = datetime.now(UTC).date()


def test_each_moved_check_is_a_native_delivery_control():
    registry = get_registry()
    for code in rules.CODES:
        control = registry.controls[code]
        assert control.mode == "RERUN" and code in RULE_CODES["DELIVERY"], code
    assert "BOOKING_TO_DELIVERY_EXCESS" in registry.controls  # still catalogued as the Rule Engine's
    assert registry.controls["BOOKING_TO_DELIVERY_EXCESS"].mode == "EXTERNAL"


def test_insurance_starting_before_delivery_passes_and_after_fails(journey):
    gate = TODAY - timedelta(days=5)
    add_ready_document(journey, "gate_pass", delivery_date=_iso(gate))
    add_ready_document(journey, "insurance_cover", policy_start_date=_iso(gate - timedelta(days=10)))
    assert _check(journey, rules.insurance_start_after_delivery).outcome == "PASS"
    add_ready_document(journey, "insurance_cover", policy_start_date=_iso(gate + timedelta(days=2)))
    assert _check(journey, rules.insurance_start_after_delivery).outcome == "FAIL"


def test_an_accessory_invoice_before_delivery_passes_and_after_fails(journey):
    gate = TODAY - timedelta(days=5)
    add_ready_document(journey, "gate_pass", delivery_date=_iso(gate))
    add_ready_document(journey, "accessory_invoice_dms", invoice_date=_iso(gate - timedelta(days=3)))
    assert _check(journey, rules.accessory_date_vs_delivery).outcome == "PASS"
    add_ready_document(journey, "accessory_invoice_dms", invoice_date=_iso(gate + timedelta(days=1)),
                       invoice_number="ACC-9")
    result = _check(journey, rules.accessory_date_vs_delivery)
    assert result.outcome == "FAIL" and "ACC-9" in result.reason


def test_the_last_eight_characters_of_the_vin_are_this_vehicle(journey):
    add_ready_document(journey, "customer_invoice_dms", chassis_number="MA1XL2TVXT5J30535")
    add_ready_document(journey, "accessory_invoice_dms", chassis_number="T5J30535")
    assert _check(journey, rules.accessory_vin_vs_vehicle_vin).outcome == "PASS"


def test_another_vehicles_chassis_fails_and_one_character_off_is_a_misread(journey):
    add_ready_document(journey, "customer_invoice_dms", chassis_number="MA1XL2TVXT5J30535")
    add_ready_document(journey, "delivery_order_cover", chassis_number="T5J30536")
    assert _check(journey, rules.chassis_invoice_vs_do).outcome == "SKIPPED"
    add_ready_document(journey, "delivery_order_cover", chassis_number="T5K99999")
    assert _check(journey, rules.chassis_invoice_vs_do).outcome == "FAIL"


def test_a_gate_pass_with_a_misread_year_is_not_a_fact(journey):
    add_ready_document(journey, "customer_invoice_dms", invoice_date=_iso(TODAY - timedelta(days=4)))
    add_ready_document(journey, "gate_pass", delivery_date="2021-11-10")
    result = _check(journey, rules.gate_date_before_invoice)
    assert result.outcome == "SKIPPED" and "cannot be right" in result.reason


def test_a_vehicle_leaving_before_its_invoice_fails(journey):
    add_ready_document(journey, "customer_invoice_dms", invoice_date=_iso(TODAY - timedelta(days=2)))
    add_ready_document(journey, "gate_pass", delivery_date=_iso(TODAY - timedelta(days=4)))
    assert _check(journey, rules.gate_date_before_invoice).outcome == "FAIL"


def test_the_gst_certificate_name_is_read_from_its_own_field(journey):
    add_ready_document(journey, "gst_certificate", organisation_legal_name="SAHOO TRADERS PRIVATE LIMITED",
                       gstin="21ABCDE1234F1Z5")
    add_ready_document(journey, "customer_invoice_dms", buyer_name="Sahoo Traders Pvt Ltd",
                       buyer_gstin="21ABCDE1234F1Z5")
    assert _check(journey, rules.corporate_name_cert_vs_invoice).outcome == "PASS"
    assert _check(journey, rules.corporate_gstin_cert_vs_invoice).outcome == "PASS"


def test_a_debit_note_is_compared_only_when_it_gives_the_insurance(journey):
    add_ready_document(journey, "insurance_cover", premium_amount="42000")
    add_ready_document(journey, "debit_note", particulars="Accessories fitment", total_amount="9000")
    assert "No debit note for the insurance" in _check(journey, rules.debit_note_insurance_vs_cover_note).reason
    add_ready_document(journey, "debit_note", particulars="Insurance premium", total_amount="45000")
    assert _check(journey, rules.debit_note_insurance_vs_cover_note).outcome == "FAIL"


def test_the_exchange_valuation_is_optional(journey):
    assert _check(journey, rules.exchange_value_vs_invoice_credit).outcome == "PASS"


def test_the_po_is_optional_and_compared_per_vehicle(journey):
    assert _check(journey, rules.corporate_po_amount_vs_invoice).outcome == "PASS"
    add_ready_document(journey, "purchase_order", po_amount="2400000", quantity="2")
    add_ready_document(journey, "customer_invoice_dms", grand_total_amount="1200000")
    assert _check(journey, rules.corporate_po_amount_vs_invoice).outcome == "PASS"


def test_a_project_with_the_rule_engine_off_sends_nothing(journey):
    from audit_core.uc03_p2_controls import rule_engine_enabled

    assert rule_engine_enabled(journey.engine, tenant_id=journey.tenant_id)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text(
            "INSERT INTO auditcore.tenant_rule_config (tenant_id, rule_engine_enabled) VALUES (:t, false) "
            "ON CONFLICT (tenant_id) DO UPDATE SET rule_engine_enabled=false"), {"t": journey.tenant_id})
    assert not rule_engine_enabled(journey.engine, tenant_id=journey.tenant_id)
