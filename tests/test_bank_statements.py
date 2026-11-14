"""Dealership bank statements: reading any bank's file, and tying receipts to credits
(product owner 2026-11-14). Synthetic statements only -- no client data in the repo."""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pytest
from openpyxl import Workbook

from audit_core import bank_reconciliation as br
from audit_core.bank_statement_parsers import (
    CREDIT,
    DEBIT,
    StatementUnreadable,
    parse_money,
    read_statement,
    references_in,
)
from audit_core.uc03_p2_bank_statements import journey_rollup

# --------------------------------------------------------------------------- reading


def _xlsx(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


def test_reads_an_amount_with_cr_dr_column_layout() -> None:
    # ICICI Corporate Internet Banking's export: one amount and a Cr/Dr column.
    data = _xlsx([
        ["DETAILED STATEMENT"],
        [],
        ["Transactions List -   -SAMPLE MOTORS (INDIA) PRIVATE LIMITED (INR) - 123456789012"],
        ["No.", "Transaction ID", "Value Date", "Txn Posted Date", "ChequeNo.", "Description", "Cr/Dr",
         "Transaction Amount(INR)"],
        [1, "S1", "03/11/2026", "03/11/2026 06:25:56 AM", "-", "RTGS-CNRBR52026110351238568-A PAYER-1617", "CR",
         674000.0],
        [2, "S2", "03/11/2026", "03/11/2026 02:58:47 PM", "-", "CashDep Chgs 01-31OCT26+GST", "DR", 2832.0],
        [3, "S3", "04/11/2026", "04/11/2026 06:36:40 AM", "-", "UPI/111075115842/UPI Pay/XX.ibz@icici", "CR", 21000],
    ])
    statement = read_statement(data, "OpTransactionHistoryUX304-11-2026.xlsx")
    assert statement.bank_name == "ICICI Bank"
    assert statement.account_number == "123456789012"
    assert statement.account_holder == "SAMPLE MOTORS (INDIA) PRIVATE LIMITED"
    assert (statement.period_from, statement.period_to) == (date(2026, 11, 3), date(2026, 11, 4))
    assert [(t.direction, t.amount) for t in statement.transactions] == [
        (CREDIT, Decimal("674000.0")), (DEBIT, Decimal("2832.0")), (CREDIT, Decimal(21000))]
    assert statement.check() == "UNCHECKED"


def test_reads_debit_credit_balance_columns_and_checks_the_balances() -> None:
    data = _xlsx([
        ["Statement of account", "IFSC Code : HDFC0000122"],
        ["Account No : 01222000099999", "From : 01/11/2026 To : 02/11/2026"],
        ["Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"],
        ["01/11/26", "OPENING BALANCE", "", "", "", "", "1,000.00"],
        ["01/11/26", "NEFT CR-SBIN0002011-A PAYER-SAMPLE MOTORS", "SBIN526308699840", "01/11/26", "", "5,000.00",
         "6,000.00"],
        ["02/11/26", "NEFT DR-UTIB0001070-OWN ACCOUNT", "HDFCH01377355272", "02/11/26", "2,500.00", "", "3,500.00"],
    ])
    statement = read_statement(data, "statement.xlsx")
    assert statement.bank_name == "HDFC Bank"
    assert statement.account_number == "01222000099999"
    assert [t.reference for t in statement.transactions] == ["SBIN526308699840", "HDFCH01377355272"]
    assert statement.credit_total == Decimal("5000.00")
    assert statement.debit_total == Decimal("2500.00")


def test_html_disguised_as_xls_is_read() -> None:
    html = (b"<html><body><table><tr><td>Date</td><td>Particulars</td><td>Debit</td><td>Credit</td>"
            b"<td>Balance</td></tr><tr><td>05/11/2026</td><td>IMPS/P2A/630814067089/SOMEPAYE</td><td></td>"
            b"<td>1,00,000.00</td><td>2,00,000.00</td></tr></table></body></html>")
    statement = read_statement(html, "statement.xls")
    assert statement.file_format == "HTML_XLS"
    assert statement.transactions[0].amount == Decimal("100000.00")
    assert statement.transactions[0].direction == CREDIT


def test_a_file_with_no_transaction_table_is_refused() -> None:
    with pytest.raises(StatementUnreadable):
        read_statement(_xlsx([["Name", "Phone"], ["A", "1"]]), "contacts.xlsx")


def test_indian_and_western_digit_grouping_and_sides() -> None:
    assert parse_money("1,08,93,028.88") == (Decimal("10893028.88"), None)
    assert parse_money("547,986.67") == (Decimal("547986.67"), None)
    assert parse_money("1,234.00 Cr") == (Decimal("1234.00"), "CR")
    assert parse_money("(500.00)") == (Decimal("500.00"), "DR")


def test_references_in_a_narration() -> None:
    refs = references_in("RTGS/ICICR12026110413766646/A PAYER/ICICI BANK LIMITED")
    assert "ICICR12026110413766646" in refs
    assert "630814067089" in references_in("IMPS/P2A/630814067089/SOMEPAYE/ICICIBAN")


# --------------------------------------------------------------------------- classifying credits


@pytest.mark.parametrize(("narration", "kind"), [
    ("NEFT/HDFCH01377355272/SAMPLE MOTORS/HDFC BANK/0001", br.K_INTERNAL),
    ("NEFT CR-SBIN0002011-JYOTI PAYER-SAMPLE MOTORS-SBIN526308699840", br.K_CUSTOMER),
    ("INF/INFT/046538878531/Self-XXX453/2212", br.K_INTERNAL),
    ("SELF CASH DEP/BNA/DPRO106804/5027/041126/KE", br.K_CASH),
    ("BY CASH - BHUBANESWAR - KESURA", br.K_CASH),
    ("EDC/00000/00000//M037134000240547", br.K_CARD),
    ("EZY/ICICIPOS_SET_10XX498994_031126", br.K_CARD),
    ("NEFT-IN22630715161025-ICICI LOMBARD GENERAL INSURANCE CO LTD-ILGIC", br.K_INSURANCE),
    ("NEFT/CBINH26308757838/TERM LOAN DISBURSAL ACCOUNT/CENTRAL BANK", br.K_LOAN),
])
def test_classify_credit(narration: str, kind: str) -> None:
    assert br.classify_credit(narration, "SAMPLE MOTORS PRIVATE LIMITED") == kind


# --------------------------------------------------------------------------- matching

STATEMENT = br.Window(statement_id="S", outlet_id=None, period_from=date(2026, 11, 4), period_to=date(2026, 11, 4))


def _receipt(key: str, amount: str, day: int, *, journey: str = "J1", mode: str = "OTHERS", ref: str | None = None,
             names: tuple[str, ...] = ("RAMESH KUMAR SAHU",), bank: str | None = None, month: int = 11) -> br.Receipt:
    return br.Receipt(key=key, journey_id=journey, outlet_id="O1", amount=Decimal(amount),
                      receipt_date=date(2026, month, day), mode=mode, reference=ref, bank=bank,
                      customer_names=list(names))


def _credit(key: str, amount: str, narration: str, *, day: int = 4, kind: str | None = None,
            statement: str = "S", bank: str | None = "Axis Bank") -> br.Credit:
    return br.Credit(key=key, statement_id=statement, outlet_id=None, bank=bank, txn_date=date(2026, 11, day),
                     amount=Decimal(amount), narration=narration,
                     kind=kind or br.classify_credit(narration, "SAMPLE MOTORS"))


def _run(receipts: list[br.Receipt], credits: list[br.Credit], windows: list[br.Window] | None = None
         ) -> dict[str, br.ReceiptResult]:
    results, _ = br.reconcile(receipts, credits, windows or [STATEMENT])
    return {r.receipt.key: r for r in results}


def test_only_receipts_within_five_days_of_the_period_are_checked() -> None:
    results = _run([_receipt("in", "21000", 4), _receipt("margin", "21000", 9),
                    _receipt("early", "21000", 29, month=10), _receipt("out", "21000", 10)], [])
    assert set(results) == {"in", "margin"}  # 30 Oct .. 9 Nov for a 4 Nov statement
    assert results["in"].outcome == br.NOT_FOUND
    # in the margin only: the money may have come in on a day the statement does not show
    assert results["margin"].outcome == br.OUTSIDE_DATES


def test_a_utr_ties_a_receipt_paid_by_a_third_party() -> None:
    results = _run(
        [_receipt("r", "21000", 3, ref="HDFCH01376848963", names=("SOUMYA PANDA",))],
        [_credit("c1", "21000", "NEFT-HDFCH01376848963-PUSHPAK AGRO-0001NBLVF0J40", day=3),
         _credit("c2", "21000", "NEFT/SBIN526308595478/OTHER PERSON/STATE BANK", day=3)])
    assert (results["r"].outcome, results["r"].basis, results["r"].credit_keys) == (br.MATCHED, br.REFERENCE, ["c1"])


def test_the_customer_name_ties_a_receipt_with_no_reference_even_split_across_lines() -> None:
    results = _run(
        [_receipt("r", "317670", 4, names=("Saurabh Kumar Biswal", "SAURAV BISWAL"))],
        [_credit("c1", "317670", "RTGS/ICICR12026110413766646/SAUR AV BISWAL/ICICI BANK LIMITED//URGENT")])
    assert (results["r"].outcome, results["r"].basis) == (br.MATCHED, br.NAME)


def test_two_imps_of_one_lakh_for_two_receipts_of_the_same_customer() -> None:
    results = _run(
        [_receipt("a", "100000", 4, names=("LAXMINARAYAN SWAIN",)),
         _receipt("b", "100000", 4, names=("LAXMINARAYAN SWAIN",))],
        [_credit("c1", "100000", "IMPS/P2A/630814067089/LAXMINAR/ICICIBAN"),
         _credit("c2", "100000", "IMPS/P2A/630815110625/LAXMINAR/ICICIBAN")])
    assert {r.outcome for r in results.values()} == {br.MATCHED}
    assert sorted(k for r in results.values() for k in r.credit_keys) == ["c1", "c2"]


def test_one_receipt_paid_as_two_credits() -> None:
    results = _run(
        [_receipt("r", "200000", 4, names=("LAXMINARAYAN SWAIN",))],
        [_credit("c1", "100000", "IMPS/P2A/630814067089/LAXMINAR/ICICIBAN"),
         _credit("c2", "100000", "IMPS/P2A/630815110625/LAXMINAR/ICICIBAN")])
    assert (results["r"].outcome, results["r"].basis) == (br.MATCHED, br.NAME_SPLIT)
    assert sorted(results["r"].credit_keys) == ["c1", "c2"]


def test_one_credit_receipted_as_two_receipts_of_one_journey() -> None:
    results = _run(
        [_receipt("a", "21000", 4), _receipt("b", "479000", 4)],
        [_credit("c1", "500000", "RTGS/SBINR52026110449010449/RAMESH KUMAR SAHU/STATE BANK")])
    assert {r.basis for r in results.values()} == {br.COMBINED}


def test_amount_alone_ties_only_a_distinctive_amount_and_never_a_common_one() -> None:
    results = _run(
        [_receipt("big", "479231", 4, names=("NOBODY ON NARRATION",)),
         _receipt("small", "21000", 4, names=("NOBODY ON NARRATION",))],
        [_credit("c1", "479231", "NEFT/SBIN126308225637/hpfashions/STATE BANK OF INDIA"),
         _credit("c2", "21000", "NEFT/SBIN526308595478/SOMEONE ELSE/STATE BANK OF INDIA")])
    assert (results["big"].outcome, results["big"].basis) == (br.MATCHED, br.AMOUNT_DATE)
    assert results["small"].outcome == br.NOT_FOUND


def test_amount_alone_is_refused_when_the_receipt_prints_another_reference() -> None:
    results = _run([_receipt("r", "200000", 4, mode="CHEQUE", ref="033292", names=("NOBODY",))],
                   [_credit("c1", "200000", "NEFT-IDIBH30765376619-SAI MULTI PRODUCTS-URGENT")])
    assert results["r"].outcome == br.NOT_FOUND


def test_a_same_day_cash_deposit_beats_an_earlier_cash_receipt_of_the_same_amount() -> None:
    results = _run(
        [_receipt("same_day", "200000", 3, mode="CASH", names=("A B",), journey="J1"),
         _receipt("earlier", "200000", 30, month=10, mode="CASH", names=("C D",), journey="J2")],
        [_credit("c1", "200000", "BY CASH - BHUBANESWAR - KESURA", day=3)])
    assert results["same_day"].credit_keys == ["c1"]
    assert results["earlier"].outcome != br.MATCHED


def test_amount_alone_is_refused_when_the_receipt_names_another_bank() -> None:
    results = _run([_receipt("r", "479231", 4, names=("NOBODY",), bank="HDFC Bank")],
                   [_credit("c1", "479231", "NEFT/SBIN126308225637/hpfashions/STATE BANK", bank="Axis Bank")])
    assert results["r"].outcome == br.NOT_FOUND


def test_two_equal_credits_without_a_name_are_ambiguous_not_guessed() -> None:
    results = _run(
        [_receipt("r", "50000", 4, ref="RRN999", names=("RAMESH KUMAR SAHU",))],
        [_credit("c1", "50000", "IMPS/RAMESHKU/ONE", day=3), _credit("c2", "50000", "IMPS/RAMESHKU/TWO", day=5)])
    assert results["r"].outcome == br.AMBIGUOUS
    assert sorted(results["r"].candidates) == ["c1", "c2"]


def test_cash_is_tied_only_to_a_cash_deposit_and_otherwise_left_as_cash() -> None:
    results = _run(
        [_receipt("deposited", "200000", 3, mode="CASH", names=("ABHIJIT ROUT",)),
         _receipt("batched", "35000", 3, mode="CASH", names=("SOMEONE",))],
        [_credit("c1", "200000", "BY CASH - BHUBANESWAR - KESURA", day=3),
         _credit("c2", "200000", "NEFT-IDIBH30765376619-SAI MULTI PRODUCTS-URGENT", day=3)])
    assert (results["deposited"].outcome, results["deposited"].credit_keys) == (br.MATCHED, ["c1"])
    assert results["batched"].outcome == br.CASH


def test_an_outlet_statement_checks_only_that_outlets_receipts() -> None:
    window = br.Window(statement_id="S", outlet_id="O2", period_from=date(2026, 11, 4), period_to=date(2026, 11, 4))
    results = _run([_receipt("r", "21000", 4)], [], [window])
    assert results == {}


def test_own_transfers_are_never_tied_to_a_receipt() -> None:
    results = _run([_receipt("r", "547986.67", 4, names=("SAMPLE",))],
                   [_credit("c1", "547986.67", "NEFT/HDFCH01377355272/SAMPLE MOTORS/HDFC BANK/0001")])
    assert results["r"].outcome == br.NOT_FOUND


def test_a_name_must_sit_on_word_boundaries() -> None:
    assert br.name_hit(["RANJAN KUMAR SAHOO"], "RTGS/IOBAR52026110400543871/SATY ARANJAN SAHOO/INDIAN") is None
    assert br.name_hit(["SATYARANJAN SAHOO"], "RTGS/IOBAR52026110400543871/SATY ARANJAN SAHOO/INDIAN") == br.NAME


def test_amount_alone_needs_the_exact_amount() -> None:
    results = _run([_receipt("r", "21001", 4, names=("NOBODY",))],
                   [_credit("c1", "21000", "NEFT/SBIN526308595478/JYOTI PRAKASH JENA/STATE BANK")])
    assert results["r"].outcome == br.NOT_FOUND


def test_a_credit_naming_a_customer_with_no_receipt_is_traced_to_the_journey() -> None:
    credit = _credit("c1", "948000", "RTGS/INDBR62026110400573630/SABIT A ROUT/INDUSIND BANK")
    assert br.name_journeys(credit, {"J7": ["SABITA ROUT"], "J8": ["OTHER NAME"]}) == ["J7"]


def test_journey_rollup_fully_partly_and_left() -> None:
    results, _ = br.reconcile(
        [_receipt("a", "21000", 4, journey="J1", ref="SBIN526308595478"),
         _receipt("b", "300000", 4, journey="J1"),
         _receipt("c", "50000", 4, journey="J2", mode="CASH", names=("X Y",))],
        [_credit("c1", "21000", "NEFT/SBIN526308595478/SOMEBODY/STATE BANK")], [STATEMENT])
    rollup = journey_rollup(results)
    assert rollup["J1"]["status"] == "PARTIAL"
    assert (rollup["J1"]["verified"], rollup["J1"]["left"]) == (Decimal(21000), Decimal(300000))
    assert rollup["J2"]["status"] == "CASH_ONLY"
