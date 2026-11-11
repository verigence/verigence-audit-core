"""The Deal after the product owner's review of 2026-11-11.

1. Every accessory invoice on file adds up to the accessories line; only the
   NDC / cost sheet's own charge stands before them. The customer ledger
   verifies, it does not price ("NDC / cost sheet figures should be used for
   computation ... customer ledger has to be used to verify the payments").
2. A charge the NDC / cost sheet shows need not be on an invoice: the row says
   so, nothing is asked.
3. FASTag, the extended warranty and RSA are not mandatory: one no bill charges
   is not taken, and a product not taken is not in the standard deal ("where
   customer has actually paid more than standard deal price is showing negative
   variance").
4. The dealer discount grid's buffer is in the standard deal whether or not the
   dealer gave a discount.
5. A discount the customer ledger posts and the NDC does not show comes off
   what is still to be received ("discounts need to be handled in customer
   ledger transactions"), never off the deal's net.
6. A deal is undercharged when its net is below the standard net, not when one
   line is below its own standard.
"""
from __future__ import annotations

import json
from decimal import Decimal as D
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import add_ready_document, create_p2_journey, database_engine
from sqlalchemy import text
from test_uc03_account_statement import ADITYA, UTKAL, _ledger, _set_policy, _sync

from audit_core.db import set_tenant_context
from audit_core.uc03_deal_source_policy import POLICY_ACCOUNT_STATEMENT
from audit_core.uc03_p2_audit_rules import run_p2_audit_rules
from audit_core.uc03_p2_journey_export import columns, deal_amounts, identity_row


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="i1111")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql: str, **params):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return c.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _deal(journey) -> dict:
    from audit_core.uc03_p2_journey360 import deal

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return deal(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _row(view: dict, key: str) -> dict:
    return next(r for g in view["categories"] for r in g["components"] if r["key"] == key)


def _benefit(view: dict, key: str) -> dict:
    return next(d for d in view["discounts"] if d["key"] == key)


def _source(journey, key: str, document_type: str, amount: str, kind: str = "COMMERCIAL") -> None:
    _sql(journey, "INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind, "
                  "component_key, source_document_type, amount, source_document_id) "
                  "VALUES (:t, :j, :k, :c, :d, :a, :doc)", k=kind, c=key, d=document_type, a=amount, doc=uuid4())


def _standard(journey, key: str, amount: str) -> None:
    _sql(journey, "INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, standard_amount) "
                  "VALUES (:t, :j, :k, :s) ON CONFLICT (tenant_id, journey_id, component_key) DO UPDATE "
                  "SET standard_amount = EXCLUDED.standard_amount", k=key, s=amount)


def _statement(journey, lines: list[dict], tally: list[dict] | None = None) -> None:
    """The NDC on file and, with ``tally``, the Tally customer ledger beside it."""
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _ledger(journey, lines)
    if tally:
        _ledger(journey, tally)
    _sync(journey)


def _posting(label: str, voucher: str, *, debit: int | None = None, credit: int | None = None) -> dict:
    """One voucher of a Tally customer ledger (it carries a running balance)."""
    return {"entry_date": "2026-10-05", "particulars_raw": label, "voucher_type": voucher,
            "voucher_number": str(uuid4())[:8], "running_balance": "1,000.00 Dr",
            **({"debit_amount": debit} if debit is not None else {"credit_amount": credit})}


def _without(lines: list[dict], *labels: str) -> list[dict]:
    return [line for line in lines if line["particulars_raw"] not in labels]


def _accessory_invoice(journey, number: str, total: str, on: str = "2026-10-05") -> None:
    add_ready_document(journey, "accessory_invoice_dms", process_area="DELIVERY", invoice_number=number,
                       invoice_date=on, grand_total_amount=total)


# ── 1. accessory invoices ────────────────────────────────────────────────────
def test_every_accessory_invoice_adds_up_and_the_customer_ledger_does_not_price_the_line(journey) -> None:
    """VJ2610-000045: four accessory invoices of Rs 1,07,812; the Tally customer
    ledger posts two of them (Rs 96,537), which stood as the charge."""
    _statement(journey, ADITYA, tally=[  # the NDC prints no accessories row
        _posting("Sales Accessories@18%", "SALES WORKSHOP", debit=90789),
        _posting("Sales Accessories@18%", "SALES WORKSHOP", debit=5748)])
    for number, total, on in (("C1R27B00000717", "90789", "2026-09-30"), ("R1R27B002287", "5310", "2026-10-01"),
                              ("C1R27B00000749", "5748", "2026-10-07"), ("C1R27B00000782", "5965", "2026-10-12")):
        _accessory_invoice(journey, number, total, on)
    row = _row(_deal(journey), "accessories_cost")
    assert D(row["billed"]) == D(107812) and row["billedFrom"] == "invoice"
    assert D(row["current"]) == D(107812)
    # the ledger's posting is still listed on the row, as a check
    assert any(s["documentType"] == "customer_ledger_tally" for s in row["sources"])


def test_the_ndc_charge_stands_before_the_accessory_invoices(journey) -> None:
    _statement(journey, UTKAL)  # Accessories 30,000 charged on the NDC
    _accessory_invoice(journey, "ACC-1", "20000")
    _accessory_invoice(journey, "ACC-2", "15000", "2026-10-09")
    row = _row(_deal(journey), "accessories_cost")
    assert D(row["billed"]) == D(30000) and row["billedFrom"] == "document"
    assert D(row["invoiced"]) == D(35000) and D(row["freeByInvoice"]) == D(5000)


def test_the_customer_ledger_prices_a_line_only_when_no_other_document_gives_it(journey) -> None:
    _statement(journey, _without(UTKAL, "RSA", "Insurance amount"), tally=[
        _posting("RSA CHARGES", "Journal", debit=2021), _posting("INSURANCE PREMIUM", "Journal", debit=40000)])
    _source(journey, "insurance_amount", "insurance_cover", "38201")
    view = _deal(journey)
    assert D(_row(view, "rsa_amount")["billed"]) == D(2021)  # nothing else gives it
    assert D(_row(view, "insurance_amount")["billed"]) == D(38201)  # the policy stands before the ledger


# ── 2. on the NDC, not on an invoice ─────────────────────────────────────────
def test_a_charge_only_the_ndc_shows_is_marked_and_nothing_else_changes(journey) -> None:
    _statement(journey, UTKAL)
    view = _deal(journey)
    assert _row(view, "rsa_amount")["statementOnly"] is True
    assert _row(view, "accessories_cost")["statementOnly"] is True
    assert _row(view, "ex_showroom_price")["statementOnly"] is False  # the vehicle's own bill
    _accessory_invoice(journey, "ACC-1", "30000")
    assert _row(_deal(journey), "accessories_cost")["statementOnly"] is False


def test_an_insurance_premium_the_ndc_charges_is_accounted_for(journey) -> None:
    """No "Insurance invoice missing" ask when the NDC itself charges the premium."""
    _statement(journey, UTKAL)  # Insurance amount 38,201
    assert _deal(journey)["insurance"]["invoiceOnFile"] is True


# ── 3. not mandatory, not taken, not in the standard ─────────────────────────
def test_a_product_not_taken_is_not_in_the_standard_deal(journey) -> None:
    _statement(journey, _without(UTKAL, "Fastag charges", "Shield warranty", "RSA"))
    _standard(journey, "ex_showroom_price", "1039000")
    before = _deal(journey)["summary"]
    for key, amount in (("additional_warranty_amount", "9999"), ("rsa_amount", "2021"), ("fastag_amount", "500")):
        _standard(journey, key, amount)
    view = _deal(journey)
    for key in ("additional_warranty_amount", "rsa_amount", "fastag_amount"):
        row = _row(view, key)
        assert row["notTaken"] is True and row["current"] is None and row["awaiting"] is None, key
        assert row["standard"] is not None and row["variance"] is None, key  # the master's price, for reference
        assert not {"NOT_INVOICED", "BELOW_STANDARD", "AWAITING_DOCUMENT"} & set(row["flags"]), key
    summary = view["summary"]
    assert {n["key"] for n in summary["notTaken"]} == {"additional_warranty_amount", "rsa_amount", "fastag_amount"}
    assert summary["gross"]["standard"] == before["gross"]["standard"]
    assert summary["net"]["standard"] == before["net"]["standard"]
    assert summary["variance"]["currentVsStandard"] == before["variance"]["currentVsStandard"]


def test_a_product_the_ndc_charges_is_taken_and_in_the_standard(journey) -> None:
    _statement(journey, UTKAL)  # Fastag 600, Shield warranty 9,999, RSA 2,021
    _standard(journey, "ex_showroom_price", "1039000")
    _standard(journey, "fastag_amount", "500")
    view = _deal(journey)
    fastag = _row(view, "fastag_amount")
    assert not fastag.get("notTaken") and D(fastag["current"]) == D(600) and D(fastag["variance"]) == D(100)
    assert view["summary"]["notTaken"] == []


# ── 4. the buffer ────────────────────────────────────────────────────────────
def _grid_buffer(journey, buffer: str) -> None:
    """The dealer's discount line as the reconciliation leaves it when it sees
    no dealer discount claimed: no entitlement, the grid's buffer as its potential."""
    details = json.dumps({"source": "DEALER_DISCOUNT_GRID", "informational": True, "potentialAmount": buffer,
                          "grid": {"buffer": buffer, "inScope": True, "tier": "BASE"}})
    updated = _sql(journey, "UPDATE auditcore.discount_applications SET standard_eligible_amount=NULL, "
                            "eligibility_result='ELIGIBLE_UNCLAIMED', details=COALESCE(details, '{}'::jsonb) || "
                            "CAST(:d AS jsonb) WHERE tenant_id=:t AND journey_id=:j "
                            "AND discount_key='ADDITIONAL_DISCOUNT'", d=details).rowcount
    if not updated:
        _sql(journey, "INSERT INTO auditcore.discount_applications (tenant_id, journey_id, discount_key, "
                      "standard_eligible_amount, eligibility_result, actual_source_kind, details) "
                      "VALUES (:t, :j, 'ADDITIONAL_DISCOUNT', NULL, 'ELIGIBLE_UNCLAIMED', 'CALCULATED', "
                      "CAST(:d AS jsonb))", d=details)


def test_the_grid_buffer_is_in_the_standard_deal_when_no_dealer_discount_is_given(journey) -> None:
    _statement(journey, _without(UTKAL, "Other Discount (if any)"))
    _standard(journey, "ex_showroom_price", "1039000")
    _grid_buffer(journey, "5000.00")
    view = _deal(journey)
    row = _benefit(view, "ADDITIONAL_DISCOUNT")
    assert D(row["entitled"]) == D(5000) and row["inStandard"] is True and row["current"] is None
    assert D(view["summary"]["discounts"]["standard"]) == D(5000)
    assert D(view["summary"]["net"]["standard"]) == D(view["summary"]["gross"]["standard"]) - D(5000)


def test_a_dealer_discount_within_the_buffer_is_not_over_the_entitlement(journey) -> None:
    _statement(journey, UTKAL)  # Other Discount (if any) 5,000
    _standard(journey, "ex_showroom_price", "1039000")
    _grid_buffer(journey, "5000.00")
    row = _benefit(_deal(journey), "ADDITIONAL_DISCOUNT")
    assert D(row["current"]) == D(5000) and D(row["entitled"]) == D(5000) and D(row["variance"]) == 0
    assert "OVER_ENTITLEMENT" not in row["flags"]


# ── 5. the customer ledger's discount ────────────────────────────────────────
def test_a_discount_the_customer_ledger_posts_comes_off_what_is_to_be_received(journey) -> None:
    _statement(journey, _without(UTKAL, "Other Discount (if any)"))
    before = _deal(journey)["summary"]
    _ledger(journey, [_posting("DISCOUNT ALLOWED (NO GST)", "Journal_NonGST", credit=4000)])
    _sync(journey)
    view = _deal(journey)
    row, summary = _benefit(view, "ADDITIONAL_DISCOUNT"), view["summary"]
    # still not a discount of the deal sheet: shown, not counted, never a variance
    assert row["current"] is None and row["variance"] is None and "NOT_ON_STATEMENT" in row["flags"]
    assert D(row["ledgerPosted"]) == D(4000)
    assert summary["net"]["current"] == before["net"]["current"]
    assert summary["discounts"]["current"] == before["discounts"]["current"]
    # but the ledger has credited the customer with it
    assert D(summary["ledgerDiscounts"]) == D(4000)
    assert D(summary["payable"]) == D(before["payable"]) - D(4000)
    assert D(summary["balanceDue"]) == D(before["balanceDue"]) - D(4000)


# ── 6. undercharged is judged on the net deal ────────────────────────────────
def _run(journey, stage: str = "BOOKING") -> dict:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return {r.code: r for r in run_p2_audit_rules(
            c, tenant_id=journey.tenant_id, journey_id=journey.journey_id, stage=stage,
            correlation_id="test", triggering_event="TEST")}


def _line(journey, key: str, standard: str, billed: str) -> None:
    _sql(journey, "INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, standard_amount, "
                  "actual_amount) VALUES (:t, :j, :k, :s, :a)", k=key, s=standard, a=billed)
    _source(journey, key, "tax_invoice_tally", billed)


def test_a_line_below_its_standard_is_no_undercharge_when_the_deal_is_above_the_standard(journey) -> None:
    _line(journey, "ex_showroom_price", "900000", "900000")
    _line(journey, "insurance_amount", "40000", "35000")       # Rs 5,000 below its standard
    _line(journey, "accessories_cost", "20000", "60000")       # Rs 40,000 above
    under = _run(journey)["DEAL_UNDERCHARGED"]
    assert under.outcome == "PASS"
    assert "as a whole is charged at or above the standard" in under.reason
    assert [c["key"] for c in under.details["componentsBelowStandard"]] == ["insurance_amount"]


def test_a_deal_below_the_standard_net_is_still_undercharged(journey) -> None:
    _line(journey, "ex_showroom_price", "900000", "900000")
    _line(journey, "insurance_amount", "40000", "35000")
    under = _run(journey)["DEAL_UNDERCHARGED"]
    assert under.outcome == "FAIL" and [c["key"] for c in under.details["components"]] == ["insurance_amount"]


# ── 7. the export ────────────────────────────────────────────────────────────
def _export_deal() -> dict:
    return {
        "categories": [{"components": [
            {"key": "ex_showroom_price", "standard": "1000000", "current": "1000000"},
            {"key": "insurance_amount", "standard": "40000", "current": "36000", "excluded": True},
            {"key": "additional_warranty_amount", "standard": "19999", "current": None, "notTaken": True},
            {"key": "rsa_amount", "standard": "2021", "current": None, "notTaken": True},
            {"key": "fastag_amount", "standard": "500", "current": None, "notTaken": True},
            {"key": "accessories_cost", "standard": "30000", "current": "107812"},
        ]}],
        "discounts": [{"key": "ADDITIONAL_DISCOUNT", "entitled": "5000", "current": None, "inStandard": True}],
        "summary": {
            "deliveryStarted": True, "standardTakenAsCharged": [],
            "gross": {"standard": "1030000", "current": "1107812"},
            "discounts": {"standard": "5000", "current": None},
            "net": {"standard": "1025000", "current": "1107812"},
            "variance": {"currentVsStandard": "82812"}, "payable": "1107812", "paid": {"total": "1107812"},
        },
        "insurance": {"source": "SELF", "inDeal": False},
    }


def test_the_export_shows_the_master_price_of_a_line_that_is_not_in_the_deal() -> None:
    row = deal_amounts(_export_deal())
    # the customer's own insurance and products not taken: the standard is shown,
    # with no actual and no variance, and it is in no total
    for head, standard in (("Insurance", 40000), ("EW", 19999), ("RSA", 2021), ("Fasttag Taken", 500)):
        assert row[f"Std: {head}"] == standard and row[f"Act: {head}"] == 0 and row[f"Var: {head}"] is None, head
    assert row["Total Standard Deal"] == 1030000
    assert row["Std Disc: Buffer Discount"] == 5000
    assert row["Std: Essential Kit"] == 30000 and row["Var: Essential Kit"] == 77812
    assert row["Accessories Add-on"] == 77812  # billed over the standard kit
    assert row["Net Variance"] == 82812


def test_the_export_carries_the_policy_number_and_the_add_on_covers() -> None:
    vehicle = {"addons": {"insurance": {"details": {
        "policyNumber": "36140031266107274023", "addOns": '["Zero Depreciation", "RSA"]'}}}}
    row = identity_row({}, deal=_export_deal(), vehicle=vehicle, customer={})
    assert row["Insurance Policy No"] == "36140031266107274023"
    assert row["Insurance Add-ons"] == "Zero Depreciation, RSA"
    assert identity_row({}, deal=_export_deal(), vehicle={}, customer={})["Insurance Policy No"] is None
    assert columns()[-3:] == ["Insurance Policy No", "Insurance Add-ons", "Accessories Add-on"]
