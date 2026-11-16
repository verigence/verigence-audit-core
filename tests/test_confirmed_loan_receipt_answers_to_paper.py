"""A receipt confirmed as the loan answers to the loan the paper states (2026-11-16).

SHI-SS-261027-006: Prabodh Kumar Dash's Rs 11,50,000 down payment was picked as
HDFC's loan, which the NDC records as Rs 18,80,226; Journey 360 then showed the
loan as Rs 11,50,000 and left the receipt out of the customer's payments."""
from __future__ import annotations

from decimal import Decimal

from audit_core.uc03_finance_disbursement_resolution import contradicted_by_paper

NDC = [{"amount": Decimal(1880226), "source": "NDC (account statement) financier row"}]


def test_a_receipt_of_another_amount_is_not_the_loan():
    assert contradicted_by_paper("1150000", NDC) == NDC[0]


def test_the_loan_itself_or_net_of_the_lenders_charges_stands():
    assert contradicted_by_paper("1880226", NDC) is None
    assert contradicted_by_paper("1880225.50", NDC) is None   # within Rs 1
    assert contradicted_by_paper("1850000", NDC) is None      # 98.4%: the lender's charges taken


def test_nothing_stated_contradicts_nothing():
    assert contradicted_by_paper("1150000", []) is None
    assert contradicted_by_paper(None, NDC) is None


def test_any_stated_figure_that_fits_is_enough():
    stated = [*NDC, {"amount": Decimal(1150000), "source": "Bank Approval Letter (sanctioned amount)"}]
    assert contradicted_by_paper("1150000", stated) is None
