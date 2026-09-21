from __future__ import annotations

import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.db import set_tenant_context
from audit_core.errors import ConflictError
from audit_core.uc03_finance_disbursement_resolution import (
    TASK_TYPE,
    TL_NOTICE_TASK_TYPE,
    _is_eligible_disbursement_mode,
    _name_matches,
    _normalize_name,
    confirm_loan_disbursement,
    eligible_loan_disbursement_candidates,
    resolve_finance_disbursement,
)


def test_disallowed_modes_excluded() -> None:
    assert _is_eligible_disbursement_mode("UPI", "OTHERS") is False
    assert _is_eligible_disbursement_mode("Cash", "CASH") is False
    assert _is_eligible_disbursement_mode("Card", "OTHERS") is False
    assert _is_eligible_disbursement_mode("QR", "OTHERS") is False
    assert _is_eligible_disbursement_mode("Google Pay UPI", "OTHERS") is False


def test_allowed_modes_kept() -> None:
    assert _is_eligible_disbursement_mode("RTGS", "RTGS") is True
    assert _is_eligible_disbursement_mode("NEFT", "NEFT") is True
    assert _is_eligible_disbursement_mode("Bank Transfer", "BANK_TRANSFER") is True
    assert _is_eligible_disbursement_mode("Cheque", "CHEQUE") is True


def test_name_matching_ignores_legal_suffixes() -> None:
    assert _normalize_name("UCO Bank") == _normalize_name("UCO Bank Ltd")
    candidate = {"receipt_bank_name": "UCO Bank", "receipt_remarks": None, "payment_reference": None}
    assert _name_matches(candidate, _normalize_name("UCO Bank Ltd")) is True


def test_name_matching_uses_lender_aliases() -> None:
    candidate = {"receipt_bank_name": None, "receipt_remarks": "Disbursed by MMFSL RTGS", "payment_reference": None}
    assert _name_matches(candidate, _normalize_name("Mahindra Financial Services Ltd")) is True


def test_name_matching_false_when_nothing_in_common() -> None:
    candidate = {"receipt_bank_name": "HDFC Bank", "receipt_remarks": None, "payment_reference": None}
    assert _name_matches(candidate, _normalize_name("UCO Bank")) is False


@pytest.fixture
def finance_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for finance disbursement integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-fin-disb-{suffix}"
    actor_id = f"pc-fin-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"FIN-CAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"FIN-OEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.projects (tenant_id, project_code, project_name, oem_id, "
                 "product_category_id, effective_start_date) "
                 "VALUES (:t, :c, 'Finance Disbursement Project', :o, :cat, CURRENT_DATE)"),
            {"t": tenant_id, "c": f"FIN-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = connection.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"FIN-D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"FIN-O-{suffix}"},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, "
                 "business_role_code, dealer_id, outlet_id) VALUES (:t, :a, 'PC', :d, :o)"),
            {"t": tenant_id, "a": actor_id, "d": dealer_id, "o": outlet_id},
        )
        customer_id = connection.execute(
            text("INSERT INTO auditcore.customers (tenant_id, dealer_id, outlet_id, customer_type_code, "
                 "display_name) VALUES (:t, :d, :o, 'INDIVIDUAL', 'Finance Customer') RETURNING customer_id"),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, "
                 "journey_reference) VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"FIN-J-{suffix}"},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.tenant_rule_config (tenant_id, minimum_booking_amount) "
                 "VALUES (:t, 25000)"),
            {"t": tenant_id},
        )
        finance_record_id = connection.execute(
            text("INSERT INTO auditcore.finance_records (tenant_id, journey_id, finance_type_code, "
                 "provider_name, financed_amount, source_kind) "
                 "VALUES (:t, :j, 'LOAN', 'UCO Bank', 1500, 'EVIDENCE') RETURNING finance_record_id"),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one()

    yield {
        "engine": engine,
        "tenant_id": tenant_id,
        "journey_id": journey_id,
        "actor_id": actor_id,
        "finance_record_id": finance_record_id,
    }
    engine.dispose()


def _seed_payment(
    connection,
    *,
    tenant_id: str,
    journey_id,
    amount: str,
    days_after_booking: int,
    method: str,
    bank_name: str | None = None,
    remarks: str | None = None,
):
    connection.execute(
        text(
            """
            INSERT INTO auditcore.payments (
                tenant_id, journey_id, payment_at_utc, amount, payment_method_code,
                payment_mode_code, receipt_bank_name, receipt_remarks,
                receipt_number, receipt_date, payment_stage
            ) VALUES (
                :t, :j, now() + (:days || ' days')::interval, :amount, :method,
                :mode, :bank, :remarks,
                :receipt_number, CURRENT_DATE + :days, :stage
            )
            """
        ),
        {
            "t": tenant_id, "j": journey_id, "days": days_after_booking, "amount": amount,
            "method": method, "mode": _classify(method), "bank": bank_name, "remarks": remarks,
            "receipt_number": f"R-{uuid4().hex[:8]}", "stage": "BOOKING" if days_after_booking <= 0 else "DELIVERY",
        },
    )


def _classify(method: str) -> str:
    from audit_core.uc03_payment_mode import classify_payment_mode

    return classify_payment_mode(method)


def test_zero_candidates_marks_unverified_and_raises_task(finance_setup) -> None:
    setup = finance_setup
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                      amount="30000", days_after_booking=0, method="Cash")

        result = resolve_finance_disbursement(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"], correlation_id="test",
        )
        assert result == {"resolved": False, "reason": "no_eligible_payment"}

        row = connection.execute(
            text("SELECT loan_disbursement_confidence FROM auditcore.finance_records "
                 "WHERE tenant_id=:t AND finance_record_id=:f"),
            {"t": setup["tenant_id"], "f": setup["finance_record_id"]},
        ).mappings().one()
        assert row["loan_disbursement_confidence"] == "UNVERIFIED"

        task = connection.execute(
            text("SELECT task_type, assigned_role_code FROM auditcore.workflow_tasks "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"t": setup["tenant_id"], "j": setup["journey_id"]},
        ).mappings().one()
        assert task["task_type"] == TASK_TYPE
        assert task["assigned_role_code"] == "PC"


def test_single_eligible_candidate_auto_resolves_without_a_task(finance_setup) -> None:
    setup = finance_setup
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                      amount="30000", days_after_booking=0, method="Cash")
        _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                      amount="500000", days_after_booking=5, method="RTGS", bank_name="UCO Bank")

        result = resolve_finance_disbursement(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"], correlation_id="test",
        )
        assert result == {"resolved": True, "confidence": "HIGH"}

        row = connection.execute(
            text("SELECT loan_disbursement_amount, loan_disbursement_confidence FROM auditcore.finance_records "
                 "WHERE tenant_id=:t AND finance_record_id=:f"),
            {"t": setup["tenant_id"], "f": setup["finance_record_id"]},
        ).mappings().one()
        assert row["loan_disbursement_amount"] == Decimal("500000.00")
        assert row["loan_disbursement_confidence"] == "HIGH"

        task_count = connection.execute(
            text("SELECT COUNT(*) FROM auditcore.workflow_tasks WHERE tenant_id=:t AND journey_id=:j"),
            {"t": setup["tenant_id"], "j": setup["journey_id"]},
        ).scalar_one()
        assert task_count == 0


def test_ambiguous_candidates_raise_a_task_and_do_not_guess(finance_setup) -> None:
    setup = finance_setup
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                      amount="30000", days_after_booking=0, method="Cash")
        _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                      amount="400000", days_after_booking=5, method="RTGS", bank_name=None)
        _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                      amount="100000", days_after_booking=6, method="NEFT", bank_name=None)

        result = resolve_finance_disbursement(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"], correlation_id="test",
        )
        assert result == {"resolved": False, "reason": "ambiguous_candidates"}

        row = connection.execute(
            text("SELECT loan_disbursement_confidence FROM auditcore.finance_records "
                 "WHERE tenant_id=:t AND finance_record_id=:f"),
            {"t": setup["tenant_id"], "f": setup["finance_record_id"]},
        ).mappings().one()
        # Ambiguous: neither auto-written nor left at a stale prior value.
        assert row["loan_disbursement_confidence"] is None

        task = connection.execute(
            text("SELECT task_type FROM auditcore.workflow_tasks WHERE tenant_id=:t AND journey_id=:j"),
            {"t": setup["tenant_id"], "j": setup["journey_id"]},
        ).mappings().one()
        assert task["task_type"] == TASK_TYPE


def test_resolve_is_idempotent_and_never_duplicates_the_task(finance_setup) -> None:
    setup = finance_setup
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                      amount="30000", days_after_booking=0, method="Cash")

        resolve_finance_disbursement(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"], correlation_id="test",
        )
        resolve_finance_disbursement(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"], correlation_id="test",
        )
        task_count = connection.execute(
            text("SELECT COUNT(*) FROM auditcore.workflow_tasks WHERE tenant_id=:t AND journey_id=:j"),
            {"t": setup["tenant_id"], "j": setup["journey_id"]},
        ).scalar_one()
        assert task_count == 1


def test_confirm_loan_disbursement_writes_pc_confirmed_and_notifies_tl(finance_setup) -> None:
    setup = finance_setup
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                      amount="30000", days_after_booking=0, method="Cash")
        _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                      amount="400000", days_after_booking=5, method="RTGS", bank_name=None)
        _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                      amount="100000", days_after_booking=6, method="NEFT", bank_name=None)

        resolve_finance_disbursement(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"], correlation_id="test",
        )
        candidates = eligible_loan_disbursement_candidates(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
        )
        assert len(candidates) == 2
        chosen = candidates[0]

        confirm_loan_disbursement(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            payment_id=chosen["payment_id"],
            actor_id=setup["actor_id"],
            correlation_id="test",
        )

        row = connection.execute(
            text("SELECT loan_disbursement_amount, loan_disbursement_confidence, loan_disbursement_payment_id "
                 "FROM auditcore.finance_records WHERE tenant_id=:t AND finance_record_id=:f"),
            {"t": setup["tenant_id"], "f": setup["finance_record_id"]},
        ).mappings().one()
        assert row["loan_disbursement_confidence"] == "PC_CONFIRMED"
        assert row["loan_disbursement_payment_id"] == chosen["payment_id"]

        tasks = connection.execute(
            text("SELECT task_type, task_status, assigned_role_code FROM auditcore.workflow_tasks "
                 "WHERE tenant_id=:t AND journey_id=:j ORDER BY created_at_utc"),
            {"t": setup["tenant_id"], "j": setup["journey_id"]},
        ).mappings().all()
        assert tasks[0]["task_type"] == TASK_TYPE
        assert tasks[0]["task_status"] == "CANCELLED"
        assert tasks[1]["task_type"] == TL_NOTICE_TASK_TYPE
        assert tasks[1]["assigned_role_code"] == "TL"


def test_confirm_rejects_a_payment_that_is_not_an_eligible_candidate(finance_setup) -> None:
    setup = finance_setup
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                      amount="30000", days_after_booking=0, method="Cash")

        cash_payment_id = connection.execute(
            text("SELECT payment_id FROM auditcore.payments WHERE tenant_id=:t AND journey_id=:j"),
            {"t": setup["tenant_id"], "j": setup["journey_id"]},
        ).scalar_one()

        with pytest.raises(ConflictError):
            confirm_loan_disbursement(
                connection,
                tenant_id=setup["tenant_id"],
                journey_id=setup["journey_id"],
                payment_id=cash_payment_id,
                actor_id=setup["actor_id"],
                correlation_id="test",
            )


def test_pc_confirmed_is_sticky_across_later_resolve_calls(finance_setup) -> None:
    setup = finance_setup
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                      amount="30000", days_after_booking=0, method="Cash")
        _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                      amount="400000", days_after_booking=5, method="RTGS", bank_name=None)
        _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                      amount="100000", days_after_booking=6, method="NEFT", bank_name=None)

        candidates = eligible_loan_disbursement_candidates(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
        )
        confirm_loan_disbursement(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
            payment_id=candidates[1]["payment_id"], actor_id=setup["actor_id"], correlation_id="test",
        )

        # A later automatic pass (e.g. a Resync) must never override a human's
        # own confirmed pick, even though there are still two eligible
        # candidates and no name match to break the tie automatically.
        result = resolve_finance_disbursement(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"], correlation_id="test",
        )
        assert result == {"skipped": True, "reason": "pc_confirmed"}

        row = connection.execute(
            text("SELECT loan_disbursement_payment_id, loan_disbursement_confidence "
                 "FROM auditcore.finance_records WHERE tenant_id=:t AND finance_record_id=:f"),
            {"t": setup["tenant_id"], "f": setup["finance_record_id"]},
        ).mappings().one()
        assert row["loan_disbursement_confidence"] == "PC_CONFIRMED"
        assert row["loan_disbursement_payment_id"] == candidates[1]["payment_id"]
