from __future__ import annotations

import os
from decimal import Decimal
from uuid import UUID, uuid4

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
    names_financier,
    plan_finance_disbursement,
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
    candidate = {"receipt_customer_name": "Uco Bank (Financer)", "receipt_remarks": None, "payment_reference": None}
    assert _name_matches(candidate, _normalize_name("UCO Bank Ltd")) is True


def test_name_matching_uses_lender_aliases() -> None:
    candidate = {"receipt_bank_name": None, "receipt_remarks": "Disbursed by MMFSL RTGS", "payment_reference": None}
    assert _name_matches(candidate, _normalize_name("Mahindra Financial Services Ltd")) is True


def test_name_matching_false_when_nothing_in_common() -> None:
    candidate = {"receipt_customer_name": "HDFC Bank (Financer)", "receipt_remarks": None, "payment_reference": None}
    assert _name_matches(candidate, _normalize_name("UCO Bank")) is False


def test_the_receipts_bank_name_is_the_dealers_account_not_the_financier() -> None:
    # Live receipts print the dealer's own deposit account there ("SBI Bank",
    # "Axis Bank", "SBI 493A/C"): a customer's receipt banked at SBI does not
    # name an SBI loan.
    candidate = {"receipt_customer_name": "BHIKARI SINGH", "receipt_bank_name": "SBI Bank",
                 "receipt_remarks": "BEING AMOUNT RECEIVED FROM BHIKARI SINGH FOR FINANCE AMOUNT", "payment_reference": None}
    assert names_financier(candidate, "STATE BANK OF INDIA") is False
    assert names_financier({**candidate, "receipt_customer_name": "STATE BANK OF INDIA (FINANCER)"},
                           "STATE BANK OF INDIA") is True
    # ... unless the receipt marks it as the financier's payment (CHITA RANJAN NAYAK on live).
    assert names_financier({**candidate, "receipt_bank_name": "STATE BANK OF INDIA (FINANCER)"},
                           "STATE BANK OF INDIA") is True
    # The remarks naming the remitting lender do (HDFC NEFT narration on live receipts).
    assert names_financier({**candidate, "receipt_bank_name": "SBI Bank",
                            "receipt_remarks": "BY TRANSFER-NEFT*HDFC0000240*HDFCH01331681215*HDFC Bank Ltd*BA--"},
                           "HDFC BANK LTD") is True


def test_a_customer_with_the_hypothecation_noted_is_the_customers_name() -> None:
    candidate = {"receipt_customer_name": "SIBARAM NAYAK ( HP TO UNION BANK OF INDIA )", "receipt_remarks": None,
                 "payment_reference": None}
    assert names_financier(candidate, "Union Bank Of India") is False
    assert names_financier({**candidate, "receipt_customer_name": "BANK CHARGES"}, "State Bank of India") is False


def test_the_financiers_branch_is_not_part_of_its_name() -> None:
    candidate = {"receipt_customer_name": "STATE BANK OF INDIA (FINANCER)", "receipt_remarks": None,
                 "payment_reference": None}
    assert names_financier(candidate, "STATE BANK OF INDIA, UTKAL UNIV CAMPUS - BHUBANESWAR") is True
    assert names_financier({**candidate, "receipt_customer_name": "ICICI Finance (D.O)"}, "ICICI BANK LTD") is True


def test_a_financier_with_only_generic_words_is_matched_word_for_word() -> None:
    # "Bank of India" has no distinguishing word; "State Bank of India" is another lender.
    own = {"receipt_customer_name": "Bank Of India(Financer)", "receipt_remarks": None, "payment_reference": None}
    assert names_financier(own, "Bank Of India") is True
    assert names_financier({**own, "receipt_customer_name": "STATE BANK OF INDIA (FINANCER)"}, "Bank Of India") is False


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
    payer: str | None = None,
):
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.payments (
                tenant_id, journey_id, payment_at_utc, amount, payment_method_code,
                payment_mode_code, receipt_bank_name, receipt_remarks, receipt_customer_name,
                receipt_number, receipt_date, payment_stage
            ) VALUES (
                :t, :j, now() + (:days || ' days')::interval, :amount, :method,
                :mode, :bank, :remarks, :payer,
                :receipt_number, CURRENT_DATE + :days, :stage
            ) RETURNING payment_id
            """
        ),
        {
            "t": tenant_id, "j": journey_id, "days": days_after_booking, "amount": amount,
            "method": method, "mode": _classify(method), "bank": bank_name, "remarks": remarks, "payer": payer,
            # payment_stage='DELIVERY' requires an actual linked delivery
            # row (auditcore.prepare_payment_stage_link's own trigger) --
            # irrelevant to this resolver, which never filters on stage at
            # all, so every seeded payment here stays 'BOOKING' rather than
            # standing up a full Delivery fixture this module doesn't need.
            "receipt_number": f"R-{uuid4().hex[:8]}", "stage": "BOOKING",
        },
    ).scalar_one()


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


def _record(connection, setup) -> dict:
    return dict(connection.execute(
        text("SELECT loan_disbursement_amount, loan_disbursement_confidence, loan_disbursement_payment_id, "
             "loan_disbursement_match_basis, updated_at_utc FROM auditcore.finance_records "
             "WHERE tenant_id=:t AND finance_record_id=:f"),
        {"t": setup["tenant_id"], "f": setup["finance_record_id"]},
    ).mappings().one())


def _tasks(connection, setup) -> list[dict]:
    return [dict(r) for r in connection.execute(
        text("SELECT task_type, task_status FROM auditcore.workflow_tasks WHERE tenant_id=:t AND journey_id=:j "
             "ORDER BY created_at_utc"),
        {"t": setup["tenant_id"], "j": setup["journey_id"]},
    ).mappings().all()]


def test_the_receipt_in_the_financiers_name_is_linked_high_without_a_task(finance_setup) -> None:
    setup = finance_setup
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                      amount="30000", days_after_booking=0, method="Cash")
        _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                      amount="40000", days_after_booking=1, method="NEFT", payer="Finance Customer")
        loan = _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                             amount="500000", days_after_booking=5, method="RTGS", payer="Uco Bank (Financer)",
                             bank_name="Axis Bank")

        result = resolve_finance_disbursement(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"], correlation_id="test",
        )
        assert result == {"resolved": True, "confidence": "HIGH"}
        row = _record(connection, setup)
        assert row["loan_disbursement_amount"] == Decimal("500000.00")
        assert row["loan_disbursement_payment_id"] == loan
        assert row["loan_disbursement_confidence"] == "HIGH"
        assert "in the financier's name" in row["loan_disbursement_match_basis"]
        assert _tasks(connection, setup) == []

    # Written once: a later pass that finds the same changes nothing.
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        resolve_finance_disbursement(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"], correlation_id="test",
        )
        assert _record(connection, setup)["updated_at_utc"] == row["updated_at_utc"]


def test_the_loan_is_found_wherever_it_falls_against_the_minimum_booking_amount(finance_setup) -> None:
    # Bhikari Singh (2026-11-02): the loan receipt was the FIRST receipt by date.
    setup = finance_setup
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        loan = _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                             amount="900000", days_after_booking=0, method="RTGS", payer="UCO BANK (FINANCER)")
        _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                      amount="250000", days_after_booking=2, method="NEFT", payer="Finance Customer")
        candidates = eligible_loan_disbursement_candidates(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"])
        assert loan in {c["payment_id"] for c in candidates}
        assert resolve_finance_disbursement(connection, tenant_id=setup["tenant_id"],
                                            journey_id=setup["journey_id"], correlation_id="test")["resolved"]
        assert _record(connection, setup)["loan_disbursement_payment_id"] == loan


def test_two_receipts_in_the_financiers_name_ask_the_pc(finance_setup) -> None:
    setup = finance_setup
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        for days, amount in ((5, "400000"), (6, "100000")):
            _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                          amount=amount, days_after_booking=days, method="RTGS", payer="UCO Bank (Financer)")
        result = resolve_finance_disbursement(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"], correlation_id="test",
        )
        assert result == {"resolved": False, "reason": "ambiguous_candidates"}
        row = _record(connection, setup)
        assert row["loan_disbursement_confidence"] == "UNVERIFIED" and row["loan_disbursement_payment_id"] is None
        assert "2 receipts are in the financier's name" in row["loan_disbursement_match_basis"]
        assert [t["task_type"] for t in _tasks(connection, setup)] == [TASK_TYPE]


def test_a_receipt_that_neither_names_the_financier_nor_equals_the_loan_is_never_guessed(finance_setup) -> None:
    # The old MEDIUM "only eligible post-booking payment" link: cleared, and the PC is asked.
    setup = finance_setup
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                      amount="30000", days_after_booking=0, method="Cash")
        guess = _seed_payment(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"],
                              amount="250000", days_after_booking=5, method="RTGS", payer="Finance Customer",
                              bank_name="UCO Bank")
        connection.execute(
            text("UPDATE auditcore.finance_records SET loan_disbursement_payment_id=:p, loan_disbursement_amount=250000, "
                 "loan_disbursement_confidence='MEDIUM', loan_disbursement_match_basis='Only eligible post-booking "
                 "payment; financer name not found on the receipt.' WHERE tenant_id=:t AND finance_record_id=:f"),
            {"p": guess, "t": setup["tenant_id"], "f": setup["finance_record_id"]})

        plan = plan_finance_disbursement(connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"])
        assert plan["before"]["confidence"] == "MEDIUM" and plan["resolved"] is False
        assert _record(connection, setup)["loan_disbursement_confidence"] == "MEDIUM"  # a plan writes nothing

        result = resolve_finance_disbursement(
            connection, tenant_id=setup["tenant_id"], journey_id=setup["journey_id"], correlation_id="test",
        )
        assert result == {"resolved": False, "reason": "no_match"}
        row = _record(connection, setup)
        # Neither auto-written nor left at the stale guess.
        assert row["loan_disbursement_confidence"] == "UNVERIFIED"
        assert row["loan_disbursement_payment_id"] is None and row["loan_disbursement_amount"] is None
        assert [t["task_type"] for t in _tasks(connection, setup)] == [TASK_TYPE]


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


# ── the loan amount the paper states (product owner, 2026-11-02) ─────────────
@pytest.fixture
def p2():
    from conftest import delete_tenant_data
    from p2_support import create_p2_journey, database_engine

    engine = database_engine()
    created = create_p2_journey(engine, prefix="loanrcpt")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _receipt(journey, *, amount: str, number: str, day: str, mode: str | None, payer: str):
    from p2_support import add_receipt_payment

    add_receipt_payment(journey, amount=amount, receipt_number=number, receipt_date=day)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return c.execute(
            text("UPDATE auditcore.payments SET payment_method_code=:m, payment_mode_code=:mc, "
                 "receipt_customer_name=:p, receipt_bank_name='ICICI Bank', payment_at_utc=CAST(:d AS date) "
                 "WHERE tenant_id=:t AND journey_id=:j AND receipt_number=:n RETURNING payment_id"),
            {"m": mode, "mc": _classify(mode) if mode else "OTHERS", "p": payer, "d": day,
             "t": journey.tenant_id, "j": journey.journey_id, "n": number},
        ).scalar_one()


def _financed(journey, provider: str = "STATE BANK OF INDIA") -> UUID:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return c.execute(
            text("INSERT INTO auditcore.finance_records (tenant_id, journey_id, finance_type_code, provider_name, "
                 "financed_amount, source_kind) VALUES (:t, :j, 'LOAN', :p, 1500, 'EVIDENCE') "
                 "RETURNING finance_record_id"),
            {"t": journey.tenant_id, "j": journey.journey_id, "p": provider},
        ).scalar_one()


def _letter(journey, amount: str) -> None:
    from p2_support import add_ready_document

    add_ready_document(journey, "bank_approval_letter", process_area="DELIVERY", finance_sanctioned_amount=amount,
                       financier_name="STATE BANK OF INDIA", finance_sanction_date="2026-10-10")


def _ndc(journey, financer: int) -> None:
    from p2_support import add_evidence, add_extracted_field

    document_id = uuid4()
    add_evidence(journey, di_document_id=document_id, document_type_key="customer_ledger", process_area="DELIVERY")
    add_extracted_field(journey, di_document_id=document_id, field_key="line_items", document_type="customer_ledger",
                        value=[{"particulars_raw": "EX-SHOW ROOM PRICE OF VEHICLE", "debit_amount": 2400000},
                               {"particulars_raw": "BOOKING AMOUNT", "credit_amount": 50000},
                               {"particulars_raw": "FINANCER AMOUNT", "credit_amount": financer}])


def _resolve(journey) -> tuple[dict, dict]:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        result = resolve_finance_disbursement(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                              correlation_id="t")
        row = c.execute(
            text("SELECT loan_disbursement_payment_id, loan_disbursement_amount, loan_disbursement_confidence, "
                 "loan_disbursement_match_basis FROM auditcore.finance_records WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).mappings().one()
    return result, dict(row)


def _bhikari(journey) -> dict:
    """SHI-SS-261031-005 as it stands on live: the loan (RTGS, in the
    customer's name) is the first receipt; the old resolver took the only
    eligible receipt after the minimum booking amount, Rs 2,50,000."""
    ids = {
        "loan": _receipt(journey, amount="2275000", number="R-1", day="2026-10-12", mode="RTGS",
                         payer="BHIKARI SINGH"),
        "cash": _receipt(journey, amount="200000", number="R-2", day="2026-10-13", mode="Cash", payer="BHIKARI SINGH"),
        "upi": _receipt(journey, amount="13000", number="R-3", day="2026-10-14", mode="UPI", payer="BHIKARI SINGH"),
        "guess": _receipt(journey, amount="250000", number="R-4", day="2026-10-14", mode=None, payer="BHIKARI SINGH"),
    }
    record = _financed(journey)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.finance_records SET loan_disbursement_payment_id=:p, "
                       "loan_disbursement_amount=250000, loan_disbursement_confidence='MEDIUM' "
                       "WHERE tenant_id=:t AND finance_record_id=:f"),
                  {"p": ids["guess"], "t": journey.tenant_id, "f": record})
    return ids


def test_a_customer_receipt_equal_to_the_sanctioned_loan_is_the_loan_receipt(p2) -> None:
    ids = _bhikari(p2)
    _letter(p2, "22,75,000")
    result, row = _resolve(p2)
    assert result == {"resolved": True, "confidence": "HIGH"}
    assert row["loan_disbursement_payment_id"] == ids["loan"]
    assert row["loan_disbursement_amount"] == Decimal("2275000.00")
    assert "Bank Approval Letter (sanctioned amount)" in row["loan_disbursement_match_basis"]


def test_the_ndc_financier_row_is_the_loan_amount_and_is_checked_against_the_letter(p2) -> None:
    ids = _bhikari(p2)
    _ndc(p2, 2275000)
    _letter(p2, "2275000")
    _result, row = _resolve(p2)
    assert row["loan_disbursement_payment_id"] == ids["loan"]
    basis = row["loan_disbursement_match_basis"]
    assert "NDC (account statement) financier row" in basis and "agree" in basis


def test_a_statement_and_a_letter_that_disagree_are_said_to_disagree(p2) -> None:
    ids = _bhikari(p2)
    _ndc(p2, 2275000)
    _letter(p2, "2300000")
    _result, row = _resolve(p2)
    assert row["loan_disbursement_payment_id"] == ids["loan"]
    assert "disagree" in row["loan_disbursement_match_basis"]


def test_without_a_stated_loan_amount_the_customer_receipt_is_not_guessed(p2) -> None:
    _bhikari(p2)
    result, row = _resolve(p2)
    assert result == {"resolved": False, "reason": "no_match"}
    assert row["loan_disbursement_confidence"] == "UNVERIFIED" and row["loan_disbursement_payment_id"] is None


def test_a_cash_receipt_equal_to_the_loan_is_never_the_loan(p2) -> None:
    _receipt(p2, amount="700000", number="C-1", day="2026-10-12", mode="Cash", payer="P2 Customer")
    _financed(p2)
    _letter(p2, "700000")
    result, _row = _resolve(p2)
    assert result == {"resolved": False, "reason": "no_eligible_payment"}


def test_two_receipts_equal_to_the_loan_amount_ask_the_pc(p2) -> None:
    _receipt(p2, amount="700000", number="E-1", day="2026-10-12", mode="NEFT", payer="P2 Customer")
    _receipt(p2, amount="700000", number="E-2", day="2026-10-14", mode="RTGS", payer="P2 Customer")
    _financed(p2)
    _letter(p2, "700000")
    result, row = _resolve(p2)
    assert result == {"resolved": False, "reason": "ambiguous_candidates"}
    assert "2 receipts equal the loan amount" in row["loan_disbursement_match_basis"]


# ── the job that re-resolves stored bookings ─────────────────────────────────
def test_the_job_is_a_dry_run_unless_committed_and_reports_old_to_new(p2) -> None:
    from audit_core.uc03_loan_receipt_resolve import run_loan_receipt_resolve

    ids = _bhikari(p2)
    _letter(p2, "2275000")
    dry = run_loan_receipt_resolve(p2.engine, "loan-1102", [p2.tenant_id])
    tenant = dry["tenants"][p2.tenant_id]
    assert dry["commit"] is False
    assert tenant["transitions"] == {"MEDIUM->HIGH": 1} and tenant["linkChanged"] == 1
    [sample] = tenant["samples"]["MEDIUM->HIGH"]
    assert sample["before"]["paymentId"] == str(ids["guess"]) and sample["after"]["paymentId"] == str(ids["loan"])
    with p2.engine.begin() as c:
        set_tenant_context(c, p2.tenant_id)
        assert c.execute(text("SELECT loan_disbursement_confidence FROM auditcore.finance_records "
                              "WHERE tenant_id=:t"), {"t": p2.tenant_id}).scalar_one() == "MEDIUM"

    done = run_loan_receipt_resolve(p2.engine, "loan-1102-commit", [p2.tenant_id])
    assert done["commit"] is True and done["tenants"][p2.tenant_id]["transitions"] == {"MEDIUM->HIGH": 1}
    with p2.engine.begin() as c:
        set_tenant_context(c, p2.tenant_id)
        assert c.execute(text("SELECT loan_disbursement_payment_id FROM auditcore.finance_records "
                              "WHERE tenant_id=:t"), {"t": p2.tenant_id}).scalar_one() == ids["loan"]
    again = run_loan_receipt_resolve(p2.engine, "loan-1102-commit", [p2.tenant_id])
    assert again["tenants"][p2.tenant_id]["transitions"] == {"HIGH->HIGH": 1}
    assert again["tenants"][p2.tenant_id]["linkChanged"] == 0


def test_the_job_kind_is_registered_with_its_references() -> None:
    from p2_support import database_engine

    from audit_core.uc03_loan_receipt_resolve import run_loan_receipt_resolve
    from audit_core.uc03_p2_worker import _one_shot_target

    engine = database_engine()
    try:
        target, args = _one_shot_target("LOAN_RECEIPT_RESOLVE", engine=engine, tag="x", reports=["REF-1"])
    finally:
        engine.dispose()
    assert target is run_loan_receipt_resolve and args[-1] == ["REF-1"]
