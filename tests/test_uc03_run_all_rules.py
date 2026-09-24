from __future__ import annotations

import os
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from sqlalchemy import create_engine, text

from audit_core.uc03_run_all_rules import _run_audit_core_rules_for_stage


@pytest.fixture
def run_all_rules_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-rar-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"RAR-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"RAR-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'RAR', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"RAR-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"RAR-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"RAR-O-{suffix}"},
        ).scalar_one()
        customer_id = c.execute(
            text("""INSERT INTO auditcore.customers
                (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = c.execute(
            text("""INSERT INTO auditcore.journeys
                (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"RAR-J-{suffix}"},
        ).scalar_one()
    engine.dispose()
    engine = create_engine(database_url)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            yield c, tenant_id, journey_id
    finally:
        delete_tenant_data(engine, tenant_id)
        engine.dispose()


def test_run_all_rules_for_a_fresh_booking_journey_returns_all_expected_rule_codes(
    run_all_rules_setup,
) -> None:
    connection, tenant_id, journey_id = run_all_rules_setup

    results = _run_audit_core_rules_for_stage(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage="BOOKING", correlation_id="corr-1",
    )

    rule_codes = {r.ruleCode for r in results}
    assert rule_codes == {
        "WRONG_DOCUMENT", "DUPLICATE_RECEIPT", "DUPLICATE_BOOKING", "MODEL_NOT_IDENTIFIED",
        "DEAL_RECONCILIATION_REFRESH", "MANUAL_VERIFICATION", "PAYMENT_BANK_UNMATCHED",
        "AUTOMATED_SYNC_FAILURE",
    }
    # Nothing has been extracted yet -- every one of these is a clean SKIPPED,
    # except AUTOMATED_SYNC_FAILURE, which has no SKIPPED state of its own
    # (it only ever reports whether an internal error occurred -- PASS here
    # since reconcile_payments ran without one).
    by_code = {r.ruleCode: r.outcome for r in results}
    assert by_code["AUTOMATED_SYNC_FAILURE"] == "PASS"
    assert all(outcome == "SKIPPED" for code, outcome in by_code.items() if code != "AUTOMATED_SYNC_FAILURE")
    assert all(r.stage == "BOOKING" for r in results)


def test_run_all_rules_writes_execution_log_rows(run_all_rules_setup) -> None:
    connection, tenant_id, journey_id = run_all_rules_setup

    _run_audit_core_rules_for_stage(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage="BOOKING", correlation_id="corr-1",
    )

    rows = connection.execute(
        text(
            "SELECT rule_code, outcome, triggering_event FROM auditcore.rule_executions "
            "WHERE tenant_id=:t AND journey_id=:j"
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    assert len(rows) == 8
    assert all(row["triggering_event"] == "MANUAL_RUN_ALL_RULES" for row in rows)


def test_run_all_rules_for_delivery_stage_only_runs_delivery_scoped_rules(
    run_all_rules_setup,
) -> None:
    connection, tenant_id, journey_id = run_all_rules_setup

    results = _run_audit_core_rules_for_stage(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage="DELIVERY", correlation_id="corr-1",
    )

    # The four journey-wide checks (WRONG_DOCUMENT, DUPLICATE_RECEIPT,
    # DUPLICATE_BOOKING, and Booking's own SKU resolution) only run once,
    # on BOOKING -- DELIVERY gets MODEL_NOT_IDENTIFIED's invoice-fallback
    # instead, plus the two stage-scoped checks.
    rule_codes = {r.ruleCode for r in results}
    assert rule_codes == {
        "MODEL_NOT_IDENTIFIED", "MANUAL_VERIFICATION", "PAYMENT_BANK_UNMATCHED", "AUTOMATED_SYNC_FAILURE",
    }
    assert all(r.stage == "DELIVERY" for r in results)
