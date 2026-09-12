from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from audit_core.uc03_rule_execution_log import (
    record_execution,
    record_executions_bulk,
    record_from_summary,
)


@pytest.fixture
def execution_log_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-rel-{suffix}"
    # A real journey -- audit_findings.journey_id carries a composite FK to
    # auditcore.journeys(tenant_id, journey_id); only the bulk test below
    # actually needs it (to link a real finding), but seeding it here once
    # keeps every test in this file on a genuinely valid journey.
    with engine.begin() as connection:
        category_id = connection.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"REL-CAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"REL-OEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'REL', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"REL-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = connection.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"REL-D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"REL-O-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text("""INSERT INTO auditcore.customers
                (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text("""INSERT INTO auditcore.journeys
                (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"REL-J-{suffix}"},
        ).scalar_one()
    yield engine, tenant_id, journey_id
    engine.dispose()


def _rows(engine, tenant_id):
    with engine.begin() as connection:
        return connection.execute(
            text(
                "SELECT rule_code, outcome, reason, audit_finding_id, "
                "triggering_event, correlation_id "
                "FROM auditcore.rule_executions WHERE tenant_id=:t ORDER BY rule_code"
            ),
            {"t": tenant_id},
        ).mappings().all()


def test_record_execution_writes_a_pass_row(execution_log_setup) -> None:
    engine, tenant_id, journey_id = execution_log_setup
    with engine.begin() as connection:
        record_execution(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            rule_code="KYC_NAME_VS_BOOKING",
            triggering_event="BOOKING_REVIEW_CONFIRMED",
            outcome="PASS",
            correlation_id="corr-1",
        )
    rows = _rows(engine, tenant_id)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "PASS"
    assert rows[0]["audit_finding_id"] is None
    assert rows[0]["reason"] is None


def test_record_execution_rejects_unknown_outcome(execution_log_setup) -> None:
    engine, tenant_id, journey_id = execution_log_setup
    with pytest.raises(IntegrityError), engine.begin() as connection:
        record_execution(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            rule_code="X",
            triggering_event="MANUAL",
            outcome="MAYBE",  # type: ignore[arg-type]
        )


def test_record_executions_bulk_writes_pass_fail_skipped(execution_log_setup) -> None:
    engine, tenant_id, journey_id = execution_log_setup
    with engine.begin() as connection:
        # audit_finding_id carries a real FK to audit_findings -- seed one
        # minimal row (same shape _machine_flag itself inserts) rather than
        # a random UUID, which the FK would reject.
        finding_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_findings (
                    tenant_id, journey_id, finding_type_code, severity,
                    finding_status, title, stage_code, origin_kind,
                    origin_role_snapshot, rule_key, correlation_id
                ) VALUES (
                    :t, :j, 'RULE_ENGINE_ANOMALY', 'HIGH',
                    'OPEN', 'test finding', 'DELIVERY', 'MACHINE',
                    'SYSTEM', 'RE_RULE_FAIL', 'corr-2'
                ) RETURNING audit_finding_id
                """
            ),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one()

    with engine.begin() as connection:
        record_executions_bulk(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            triggering_event="DELIVERY_COMPLETED",
            correlation_id="corr-2",
            pass_rule_codes=("RULE_PASS_1", "RULE_PASS_2"),
            fail_rule_codes={"RULE_FAIL": finding_id},
            skipped_rule_codes={"RULE_SKIPPED": "no evidence document on file"},
        )

    rows = {row["rule_code"]: row for row in _rows(engine, tenant_id)}
    assert set(rows) == {"RULE_PASS_1", "RULE_PASS_2", "RULE_FAIL", "RULE_SKIPPED"}
    assert rows["RULE_PASS_1"]["outcome"] == "PASS"
    assert rows["RULE_FAIL"]["outcome"] == "FAIL"
    assert rows["RULE_FAIL"]["audit_finding_id"] == finding_id
    assert rows["RULE_SKIPPED"]["outcome"] == "SKIPPED"
    assert rows["RULE_SKIPPED"]["reason"] == "no evidence document on file"
    assert all(row["triggering_event"] == "DELIVERY_COMPLETED" for row in rows.values())


def test_record_from_summary_skipped_when_nothing_examined(execution_log_setup) -> None:
    engine, tenant_id, journey_id = execution_log_setup
    with engine.begin() as connection:
        record_from_summary(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            rule_code="SOME_RULE", triggering_event="DOCUMENT_SYNCED",
            result={"raised": 0, "examined": 0}, skipped_reason="nothing to check yet",
        )
    rows = _rows(engine, tenant_id)
    assert rows[0]["outcome"] == "SKIPPED"
    assert rows[0]["reason"] == "nothing to check yet"


def test_record_from_summary_pass_when_examined_and_clean(execution_log_setup) -> None:
    engine, tenant_id, journey_id = execution_log_setup
    with engine.begin() as connection:
        record_from_summary(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            rule_code="SOME_RULE", triggering_event="DOCUMENT_SYNCED",
            result={"raised": 0, "examined": 3}, skipped_reason="unused",
        )
    rows = _rows(engine, tenant_id)
    assert rows[0]["outcome"] == "PASS"
    assert rows[0]["reason"] is None


def test_record_from_summary_fail_when_raised(execution_log_setup) -> None:
    engine, tenant_id, journey_id = execution_log_setup
    with engine.begin() as connection:
        record_from_summary(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            rule_code="SOME_RULE", triggering_event="DOCUMENT_SYNCED",
            result={"raised": 2, "examined": 3}, skipped_reason="unused",
        )
    rows = _rows(engine, tenant_id)
    assert rows[0]["outcome"] == "FAIL"


def test_record_from_summary_error_when_producer_failed(execution_log_setup) -> None:
    engine, tenant_id, journey_id = execution_log_setup
    with engine.begin() as connection:
        record_from_summary(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            rule_code="SOME_RULE", triggering_event="DOCUMENT_SYNCED",
            result={"error": True}, skipped_reason="unused",
        )
    rows = _rows(engine, tenant_id)
    assert rows[0]["outcome"] == "ERROR"
    assert "SOME_RULE" in rows[0]["reason"]
