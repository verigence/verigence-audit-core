"""Bank statement upload against a real database (synthetic data only).

Every upload failed in DEV with HTTP 500 (2026-11-15): the Journey bank status upsert
used one bind parameter both as a varchar column value and in a text comparison, which
Postgres rejects ("inconsistent types deduced for parameter"). The pure tests never
reached SQL, so this runs the endpoint function end to end as the runtime role.
"""
from __future__ import annotations

import io
import json
import os
from datetime import date
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from sqlalchemy import create_engine, text
from starlette.datastructures import UploadFile

import audit_core.uc03_p2_bank_statements as bs

STATEMENT = b"""Account Number,123456789012
Account Name,TEST MOTORS PVT LTD
Txn Date,Value Date,Description,Ref No,Debit,Credit,Balance
03/11/2026,03/11/2026,RTGS-TESTR52026110300000001-ASHA KUMARI RATH-1111-TEST0000001,,,"6,74,000.00","6,74,000.00"
03/11/2026,03/11/2026,MMT/IMPS/630700000001/IMPS/BIKASHMOHA/Test Bank,,,"5,00,000.00","11,74,000.00"
03/11/2026,03/11/2026,BY CASH - TEST BRANCH,,,"2,00,000.00","13,74,000.00"
04/11/2026,04/11/2026,CHQ PAID OFFICE RENT,,"50,000.00",,"13,24,000.00"
04/11/2026,04/11/2026,NEFT/KKBK263087488998/KOTAK MAHINDRA BANK/Pyt Loan A c LCV 4137380 dt 0,,,"10,26,639.00","23,50,639.00"
"""


@pytest.fixture
def dealer():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for bank statement integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-bs-{suffix}"
    payments: dict[str, str] = {}
    journeys: dict[str, str] = {}
    with engine.begin() as c:
        category = c.execute(text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                                  "VALUES (:c, 'V') RETURNING product_category_id"), {"c": f"BS-C-{suffix}"}).scalar_one()
        oem = c.execute(text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
                        {"c": f"BS-O-{suffix}"}).scalar_one()
        c.execute(text("""INSERT INTO auditcore.projects (tenant_id, project_code, project_name, oem_id,
                product_category_id, effective_start_date, timezone_name, project_status)
                VALUES (:t, :p, 'BS', :o, :c, '2026-01-01', 'Asia/Kolkata', 'ACTIVE')"""),
                  {"t": tenant_id, "p": f"BS-{suffix}", "o": oem, "c": category})
        dealer_id = c.execute(text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                                   "VALUES (:t, :c, 'D') RETURNING dealer_id"), {"t": tenant_id, "c": f"BS-D-{suffix}"}
                              ).scalar_one()
        outlet_id = c.execute(text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                                   "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
                              {"t": tenant_id, "d": dealer_id, "c": f"BS-OU-{suffix}"}).scalar_one()
        for name, amount, reference, mode in (("ASHA KUMARI RATH", 674000, "TESTR52026110300000001", "RTGS"),
                                              ("BIKASH MOHANTY", 500000, None, "IMPS"),
                                              ("CASH BUYER", 200000, None, "CASH"),
                                              ("NOT IN BANK", 77777, "NOPE0001", "NEFT")):
            customer = c.execute(text("""INSERT INTO auditcore.customers (tenant_id, dealer_id, outlet_id,
                    customer_type_code, display_name, legal_name) VALUES (:t, :d, :o, 'INDIVIDUAL', :n, :n)
                    RETURNING customer_id"""), {"t": tenant_id, "d": dealer_id, "o": outlet_id, "n": name}).scalar_one()
            journey = c.execute(text("""INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id,
                    journey_reference) VALUES (:t, :d, :o, :c, :r) RETURNING journey_id"""),
                                {"t": tenant_id, "d": dealer_id, "o": outlet_id, "c": customer,
                                 "r": f"BS-J-{suffix}-{name[:4]}"}).scalar_one()
            booking = c.execute(text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date) "
                                     "VALUES (:t, :j, '2026-11-01') RETURNING booking_id"),
                                {"t": tenant_id, "j": journey}).scalar_one()
            document = uuid4()
            c.execute(text("""INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id,
                    di_document_id, document_type_key, evidence_purpose, process_area, association_status)
                    VALUES (:t, :j, :c, :s, :d, 'dealer_receipt', 'JOURNEY_DOCUMENT', 'BOOKING', 'ACTIVE')"""),
                      {"t": tenant_id, "j": journey, "c": customer, "s": uuid4(), "d": document})
            payments[name] = str(c.execute(text("""INSERT INTO auditcore.payments (tenant_id, journey_id, amount,
                    booking_id, payment_stage, status_source, source_di_document_id, receipt_number, receipt_date,
                    payment_reference, payment_mode_code)
                    VALUES (:t, :j, :a, :b, 'BOOKING', 'EVIDENCE', :d, :n, :rd, :ref, :m) RETURNING payment_id"""),
                                           {"t": tenant_id, "j": journey, "a": amount, "b": booking, "d": document,
                                            "n": f"R-{name[:4]}", "rd": date(2026, 11, 3), "ref": reference,
                                            "m": mode}).scalar_one())
            journeys[name] = str(journey)
        # A financed Journey with no receipt for its loan: the D.O. cover gives the loan.
        customer = c.execute(text("""INSERT INTO auditcore.customers (tenant_id, dealer_id, outlet_id,
                customer_type_code, display_name, legal_name) VALUES (:t, :d, :o, 'INDIVIDUAL', 'LOAN BUYER',
                'LOAN BUYER') RETURNING customer_id"""), {"t": tenant_id, "d": dealer_id, "o": outlet_id}).scalar_one()
        journey = c.execute(text("""INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id,
                journey_reference) VALUES (:t, :d, :o, :c, :r) RETURNING journey_id"""),
                            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "c": customer,
                             "r": f"BS-J-{suffix}-LOAN"}).scalar_one()
        journeys["LOAN BUYER"] = str(journey)
        document = uuid4()
        c.execute(text("""INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id,
                di_document_id, document_type_key, evidence_purpose, process_area, association_status)
                VALUES (:t, :j, :c, :s, :d, 'delivery_order_cover', 'JOURNEY_DOCUMENT', 'DELIVERY', 'ACTIVE')"""),
                  {"t": tenant_id, "j": journey, "c": customer, "s": uuid4(), "d": document})
        for key, value in (("loan_amount", "1026639"), ("delivery_order_number", "4137380"),
                           ("disbursement_date", "2026-11-03"), ("financier_name", "Kotak Mahindra Bank")):
            c.execute(text("""INSERT INTO auditcore.journey_document_extracted_fields (tenant_id, journey_id,
                    di_document_id, source_fact_version, field_key, stage_code, source_document_type_key,
                    extracted_value, effective_value)
                    VALUES (:t, :j, :d, 1, :f, 'DELIVERY', 'delivery_order_cover', CAST(:v AS jsonb),
                            CAST(:v AS jsonb))"""),
                      {"t": tenant_id, "j": journey, "d": document, "f": key, "v": json.dumps(value)})
    try:
        yield SimpleNamespace(engine=engine, tenant_id=tenant_id, dealer_id=dealer_id, payments=payments,
                              journeys=journeys)
    finally:
        delete_tenant_data(engine, tenant_id)
        engine.dispose()


def _as_runtime(c, tenant_id):
    c.execute(text("SET LOCAL ROLE audit_core_runtime"))
    c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})


def _upload(d, data: bytes = STATEMENT, name: str = "statement.csv"):
    with d.engine.begin() as c, \
            mock.patch.object(bs, "_access", return_value={"ADMIN"}), \
            mock.patch.object(bs, "_dealers", return_value=[{"dealerId": str(d.dealer_id)}]), \
            mock.patch.object(bs, "_name_of", return_value="Tester"):
        _as_runtime(c, d.tenant_id)
        return bs.upload_bank_statement(d.tenant_id, SimpleNamespace(subject="user-1"), None, c,
                                        UploadFile(io.BytesIO(data), filename=name), d.dealer_id, None)


def test_upload_reconciles_and_updates_statuses(dealer):
    statement = _upload(dealer)
    counts = statement["counts"]
    assert counts["receipts"] == 4
    assert counts["matched"] == 3
    assert counts["notFound"] == 1
    assert counts["notFoundAmount"] == "77777.00"

    with dealer.engine.begin() as c:
        _as_runtime(c, dealer.tenant_id)
        status = dict(c.execute(text("SELECT CAST(journey_id AS text), status FROM auditcore.p2_journey_bank_status "
                                     "WHERE tenant_id = :t"), {"t": dealer.tenant_id}).all())
        verified = set(c.execute(text("SELECT CAST(payment_id AS text) FROM auditcore.payment_verification_events "
                                      "WHERE tenant_id = :t AND verification_result = 'VERIFIED'"),
                                 {"t": dealer.tenant_id}).scalars())
    assert status[dealer.journeys["ASHA KUMARI RATH"]] == "BANK_VERIFIED"
    assert status[dealer.journeys["BIKASH MOHANTY"]] == "BANK_VERIFIED"
    assert status[dealer.journeys["NOT IN BANK"]] == "NOT_VERIFIED"
    assert {dealer.payments["ASHA KUMARI RATH"], dealer.payments["BIKASH MOHANTY"]} <= verified
    assert dealer.payments["NOT IN BANK"] not in verified


def test_second_upload_rematches_detail_and_report(dealer):
    first = _upload(dealer)
    second = _upload(dealer)  # the same account and period again: the first is superseded, upserts update
    assert second["counts"]["matched"] == 3
    with dealer.engine.begin() as c:
        _as_runtime(c, dealer.tenant_id)
        states = dict(c.execute(text("SELECT CAST(statement_id AS text), status FROM auditcore.p2_bank_statements "
                                     "WHERE tenant_id = :t"), {"t": dealer.tenant_id}).all())
        assert states[first["statementId"]] == "SUPERSEDED"
        assert states[second["statementId"]] == "ACTIVE"
        bs.reconcile_dealer(c, tenant_id=dealer.tenant_id, dealer_id=str(dealer.dealer_id))
        detail = bs.statement_detail(c, dealer.tenant_id, second)
        assert len(detail["receipts"]) == 4
        report = bs.build_report(c, tenant_id=dealer.tenant_id, statements=[second])
        assert report[:2] == b"PK"
        # the matched transactions across statements, each with its Journey and bank credit (2026-11-17)
        matched = bs.matched_transactions(c, dealer.tenant_id, [second])
        assert {m["journeyId"] for m in matched} == {dealer.journeys[n] for n in
                                                     ("ASHA KUMARI RATH", "BIKASH MOHANTY", "CASH BUYER")}
        asha = next(m for m in matched if m["journeyId"] == dealer.journeys["ASHA KUMARI RATH"])
        assert asha["amount"] == "674000.00" and asha["basis"] == "REFERENCE"
        assert asha["credits"][0]["statementId"] == second["statementId"]
        assert "ASHA KUMARI RATH" in asha["credits"][0]["narration"]
        # a statement that is not in the view gives no rows: the superseded upload's credits are gone
        assert bs.matched_transactions(c, dealer.tenant_id, [first]) == []
        from openpyxl import load_workbook
        found = load_workbook(io.BytesIO(report))["Found in bank"]
        assert found.max_row == 4 and found["C2"].value is not None
        summary = bs.journey_bank_summary(c, tenant_id=dealer.tenant_id,
                                          journey_id=dealer.journeys["ASHA KUMARI RATH"])
        assert summary is not None


def test_a_loan_with_no_receipt_is_tied_to_its_journey(dealer):
    statement = _upload(dealer)
    assert statement["counts"]["creditsLoan"] == 1
    assert statement["counts"]["creditsLoanAmount"] == "1026639.00"
    # the window is the statement's own transaction dates
    assert (statement["checkFrom"], statement["checkTo"]) == ("2026-11-03", "2026-11-04")
    with dealer.engine.begin() as c:
        _as_runtime(c, dealer.tenant_id)
        row = c.execute(text("SELECT outcome, match_basis, CAST(journey_id AS text) FROM auditcore.p2_bank_statement_txns "
                             "WHERE tenant_id = :t AND amount = 1026639"), {"t": dealer.tenant_id}).one()
        assert tuple(row) == ("LOAN", "LOAN_REFERENCE", dealer.journeys["LOAN BUYER"])
        report = bs.build_report(c, tenant_id=dealer.tenant_id, statements=[statement])
        # Audit Summary AS-13: the loan amount confirmed received in the bank
        from decimal import Decimal

        from audit_core.uc03_p2_audit_summary import loan_in_bank

        line = loan_in_bank(c, tenant_id=dealer.tenant_id, journey_id=dealer.journeys["LOAN BUYER"],
                            loan=Decimal(1026639))
        assert (line["conclusion"], line["received"], line["rule"]) == ("OK", "1026639.00", "AS-13")
        assert loan_in_bank(c, tenant_id=dealer.tenant_id, journey_id=dealer.journeys["NOT IN BANK"],
                            loan=None) is None
    from openpyxl import load_workbook

    sheet = load_workbook(io.BytesIO(report))["Loans found in bank"]
    assert sheet.max_row == 2 and sheet.cell(2, 5).value == 1026639


def test_the_account_register_keeps_each_transaction_once_across_uploads(dealer):
    """Statements loaded daily or twice a week overlap: the account's register carries each
    transaction once, loaded by the first upload that had it, and the days covered."""
    from datetime import date

    from audit_core.uc03_p2_bank_register import registers

    _upload(dealer, STATEMENT, "first.csv")  # 3-4 Nov
    later = STATEMENT + b'05/11/2026,05/11/2026,NEFT/SBIN526300000009/NEW PAYER/SBI,,,"25,000.00","23,75,639.00"\n'
    _upload(dealer, later, "second.csv")  # 3-5 Nov: the same rows again and the 5th
    with dealer.engine.begin() as c:
        _as_runtime(c, dealer.tenant_id)
        found = registers(c, tenant_id=dealer.tenant_id, dealer_ids=[str(dealer.dealer_id)],
                          first=date(2026, 11, 1), today=date(2026, 11, 8), with_rows=True)
    assert len(found) == 1
    account = found[0]
    assert (account["uploads"], account["transactions"], account["credits"], account["debits"]) == (2, 6, 5, 1)
    assert account["coverage"]["from"] == "2026-11-03" and account["coverage"]["to"] == "2026-11-05"
    assert account["coverage"]["notYetLoaded"] == {"from": "2026-11-06", "to": "2026-11-07"}
    loaded = {r["narration"][:12]: r["loadedFrom"] for r in account["rows"]}
    assert loaded["NEFT/SBIN526"] == "second.csv" and loaded["BY CASH - TE"] == "first.csv"
    # the reconciliation's verdict is on the row however many uploads printed it
    asha = next(r for r in account["rows"] if "ASHA" in r["narration"])
    assert asha["result"] == "Tied to receipt" and asha["journeyId"] == dealer.journeys["ASHA KUMARI RATH"]


def test_bank_rematch_job_dry_run_changes_nothing_and_commit_reconciles(dealer):
    """BANK_REMATCH: statements uploaded before a rule change reconciled again; the dry run
    reports the result inside a transaction that is rolled back."""
    from audit_core.uc03_bank_rematch import run_bank_rematch

    statement = _upload(dealer)
    with dealer.engine.begin() as c:  # as if reconciled under the old rule: the loan not tied
        c.execute(text("UPDATE auditcore.p2_bank_statement_txns SET outcome = 'UNIDENTIFIED', journey_id = NULL, "
                       "match_basis = NULL WHERE tenant_id = :t AND amount = 1026639"), {"t": dealer.tenant_id})

    def loan_outcome():
        with dealer.engine.begin() as c:
            return c.execute(text("SELECT outcome FROM auditcore.p2_bank_statement_txns "
                                  "WHERE tenant_id = :t AND amount = 1026639"), {"t": dealer.tenant_id}).scalar_one()

    dry = run_bank_rematch(dealer.engine, f"{dealer.tenant_id}:bank-rematch-test", [])
    assert (dry["commit"], dry["dealers"], dry["failed"]) == (False, 1, 0)
    assert dry["items"][0]["loans"][0]["basis"] == "LOAN_REFERENCE"
    assert loan_outcome() == "UNIDENTIFIED"  # rolled back
    done = run_bank_rematch(dealer.engine, f"{dealer.tenant_id}:bank-rematch-test-commit", [])
    assert done["commit"] is True and done["items"][0]["statements"][0]["after"]["creditsLoan"] == 1
    assert loan_outcome() == "LOAN"
    assert statement["statementId"]
