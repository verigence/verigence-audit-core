from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

import audit_core.uc03_payment_reconciliation as pr


# ── unit ────────────────────────────────────────────────────────────────────
def test_normalize_ref() -> None:
    assert pr._normalize_ref("imps/1234-5678 ab") == "IMPS12345678AB"
    assert pr._normalize_ref(None) == ""


def test_payment_method_class() -> None:
    assert pr._payment_method_class("Cash") == "CASH"
    assert pr._payment_method_class("CASH DEPOSIT") == "CASH"
    assert pr._payment_method_class("UPI") == "NON_CASH"
    assert pr._payment_method_class("NEFT Transfer") == "NON_CASH"
    assert pr._payment_method_class("cheque") == "NON_CASH"
    assert pr._payment_method_class(None) == "UNKNOWN"
    assert pr._payment_method_class("adjustment") == "UNKNOWN"


def test_utr_suffix_match() -> None:
    assert pr._utr_suffix_match("123456789012", "N123456789012")
    assert not pr._utr_suffix_match("12345", "N12345")  # too short
    assert not pr._utr_suffix_match("999999999999", "N123456789012")


def _payment(amount, ref, day_offset=0, method="UPI"):
    return {
        "payment_id": uuid4(),
        "amount": Decimal(str(amount)),
        "payment_method_code": method,
        "payment_reference": ref,
        "receipt_date": date(2026, 9, 1) + timedelta(days=day_offset),
        "receipt_number": "R1",
    }


def _line(amount, ref, day_offset=0):
    return {
        "bank_statement_line_id": uuid4(),
        "transaction_date": date(2026, 9, 1) + timedelta(days=day_offset),
        "reference_no": ref,
        "credit_amount": Decimal(str(amount)),
        "counterparty_name": "CUST",
    }


def test_match_one_reference_exact() -> None:
    got, method = pr._match_one(_payment(50000, "UTR12345678"), [_line(50000, "UTR12345678")])
    assert method == "REFERENCE_EXACT"
    assert len(got) == 1


def test_match_one_amount_mismatch() -> None:
    got, _ = pr._match_one(_payment(50000, "UTR12345678"), [_line(40000, "UTR12345678")])
    assert got == []


def test_match_one_date_outside_window() -> None:
    got, _ = pr._match_one(_payment(50000, "UTR12345678", 0), [_line(50000, "UTR12345678", 10)])
    assert got == []


def test_match_one_ambiguous() -> None:
    got, _ = pr._match_one(
        _payment(50000, "UTR12345678"),
        [_line(50000, "UTR12345678"), _line(50000, "UTR12345678", 1)],
    )
    assert len(got) == 2


def test_match_one_utr_suffix() -> None:
    got, method = pr._match_one(_payment(50000, "123456789012"), [_line(50000, "N123456789012")])
    assert method == "UTR_SUFFIX"
    assert len(got) == 1


# ── integration ─────────────────────────────────────────────────────────────
def _bank_doc(fields: dict[str, object]):
    return SimpleNamespace(
        documentId=uuid4(),
        evidenceId=None,
        documentTypeKey="bank_statement_extract",
        extractionState="READY",
        fields=[SimpleNamespace(fieldKey=k, value=v) for k, v in fields.items()],
    )


@pytest.fixture
def journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for payment-reconciliation integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-pay-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"PAY-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"PAY-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'PAY', :o, :cat, CURRENT_DATE - 60, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"PAY-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"PAY-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"PAY-O-{suffix}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"PAY-J-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.journey_stage_states
                (tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                 first_started_at_utc, latest_activity_at_utc, version_no)
                VALUES (:t, :j, 'BOOKING', 'BOOKING_IN_PROGRESS', 'IN_PROGRESS', 'NOT_EVALUATED',
                        now(), now(), 1)"""),
            {"t": tenant_id, "j": journey_id},
        )
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_id = journey_id  # type: ignore[attr-defined]
        yield c
    engine.dispose()


def _add_payment(c, *, amount, ref, method="UPI", day=1, stage="BOOKING"):
    return c.execute(
        text("""INSERT INTO auditcore.payments
            (tenant_id, journey_id, amount, payment_method_code, payment_reference,
             receipt_number, receipt_date, payment_stage, status_source)
            VALUES (:t, :j, :a, :m, :r, 'RC-1', DATE '2026-09-01' + :day, :stage, 'EVIDENCE')
            RETURNING payment_id"""),
        {"t": c.tenant_id, "j": c.journey_id, "a": amount, "m": method, "r": ref, "day": day, "stage": stage},
    ).scalar_one()


def _match(c, payment_id):
    return c.execute(
        text("SELECT match_status, match_method, bank_statement_line_id "
             "FROM auditcore.payment_bank_matches "
             "WHERE tenant_id=:t AND payment_id=:p"),
        {"t": c.tenant_id, "p": payment_id},
    ).mappings().one_or_none()


def _open_flags(c, payment_id):
    return c.execute(
        text("SELECT count(*) FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j "
             "AND rule_key=:rk AND finding_status IN ('OPEN','ACKNOWLEDGED')"),
        {"t": c.tenant_id, "j": c.journey_id, "rk": f"PAYMENT_BANK_UNMATCHED:{payment_id}"},
    ).scalar_one()


def _verified(c, payment_id):
    return c.execute(
        text("SELECT count(*) FROM auditcore.payment_verification_events "
             "WHERE tenant_id=:t AND payment_id=:p AND verification_result='VERIFIED'"),
        {"t": c.tenant_id, "p": payment_id},
    ).scalar_one()


def test_bank_statement_line_persisted(journey) -> None:
    c = journey
    n = pr.materialize_reviewed_bank_statements(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_bank_doc({
            "bank_name": "HDFC Bank", "account_number": "XXXXXX1234",
            "transaction_date": "2026-09-02", "transaction_description": "UPI/CR/xyz",
            "reference_no": "UTR99887766", "credit_amount": "50000",
        })],
    )
    assert n == 1
    row = c.execute(
        text("SELECT bank_name, reference_no, credit_amount FROM auditcore.bank_statement_lines "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert row["reference_no"] == "UTR99887766"
    assert row["credit_amount"] == Decimal(50000)


def test_matched_payment_is_verified(journey) -> None:
    c = journey
    pid = _add_payment(c, amount=50000, ref="UTR99887766")
    pr.materialize_reviewed_bank_statements(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_bank_doc({
            "transaction_date": "2026-09-02", "transaction_description": "UPI",
            "reference_no": "UTR99887766", "credit_amount": "50000",
        })],
    )
    result = pr.reconcile_payments(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result["matched"] == 1

    m = _match(c, pid)
    assert m["match_status"] == "MATCHED"
    assert m["match_method"] == "REFERENCE_EXACT"
    assert _verified(c, pid) == 1
    assert _open_flags(c, pid) == 0


def test_unmatched_raises_flag_then_resolves(journey) -> None:
    c = journey
    pid = _add_payment(c, amount=50000, ref="UTRAAA111")
    # a non-matching bank line
    pr.materialize_reviewed_bank_statements(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_bank_doc({
            "transaction_date": "2026-09-02", "reference_no": "OTHERREF", "credit_amount": "40000",
        })],
    )
    r1 = pr.reconcile_payments(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert r1["unmatched"] == 1
    assert _open_flags(c, pid) == 1

    # idempotent: still one open flag, still one match row
    pr.reconcile_payments(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _open_flags(c, pid) == 1

    # now the matching credit arrives
    pr.materialize_reviewed_bank_statements(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_bank_doc({
            "transaction_date": "2026-09-02", "reference_no": "UTRAAA111", "credit_amount": "50000",
        })],
    )
    r3 = pr.reconcile_payments(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert r3["matched"] == 1
    assert _match(c, pid)["match_status"] == "MATCHED"
    assert _open_flags(c, pid) == 0
    assert _verified(c, pid) == 1


def test_cash_payment_not_applicable(journey) -> None:
    c = journey
    pid = _add_payment(c, amount=25000, ref="", method="Cash")
    result = pr.reconcile_payments(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result["notApplicable"] == 1
    assert _match(c, pid)["match_status"] == "NOT_APPLICABLE"
    assert _open_flags(c, pid) == 0


def test_utr_suffix_match_integration(journey) -> None:
    c = journey
    pid = _add_payment(c, amount=75000, ref="123456789012", method="NEFT")
    pr.materialize_reviewed_bank_statements(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_bank_doc({
            "transaction_date": "2026-09-02", "reference_no": "N123456789012", "credit_amount": "75000",
        })],
    )
    result = pr.reconcile_payments(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result["matched"] == 1
    assert _match(c, pid)["match_method"] == "UTR_SUFFIX"


def test_idempotent(journey) -> None:
    c = journey
    pid = _add_payment(c, amount=50000, ref="UTR555")
    pr.materialize_reviewed_bank_statements(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_bank_doc({
            "transaction_date": "2026-09-02", "reference_no": "UTR555", "credit_amount": "50000",
        })],
    )
    for _ in range(3):
        pr.reconcile_payments(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert c.execute(
        text("SELECT count(*) FROM auditcore.payment_bank_matches WHERE tenant_id=:t AND payment_id=:p"),
        {"t": c.tenant_id, "p": pid},
    ).scalar_one() == 1
    assert _verified(c, pid) == 1


def test_unmatched_delivery_payment_flag_stamped_with_delivery_stage(journey) -> None:
    """Regression: _raise_flag used to hardcode stage_code=_STAGE ('BOOKING')
    for every payment regardless of its own payment_stage -- a Delivery
    payment's PAYMENT_BANK_UNMATCHED finding was silently mislabeled under
    the Booking stage. Fixed so a Delivery-stage payment's finding actually
    carries stage_code='DELIVERY', which is also what lets
    _delivery_audit_gaps's own unverified-payment check dedupe against this
    rule instead of raising a second, separate finding for the same fact."""
    c = journey
    c.execute(
        text("INSERT INTO auditcore.deliveries (tenant_id, journey_id) VALUES (:t, :j)"),
        {"t": c.tenant_id, "j": c.journey_id},
    )
    pid = _add_payment(c, amount=60000, ref="UTR-DELIVERY-1", stage="DELIVERY")
    pr.reconcile_payments(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    stage = c.execute(
        text("SELECT stage_code FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j AND rule_key=:rk"),
        {"t": c.tenant_id, "j": c.journey_id, "rk": f"PAYMENT_BANK_UNMATCHED:{pid}"},
    ).scalar_one()
    assert stage == "DELIVERY"
