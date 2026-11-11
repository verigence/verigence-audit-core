"""The Deal after the product owner's review of 2026-11-11.

1. Every accessory invoice on file adds up, shown beside the accessories line;
   the NDC / cost sheet's charge is the line's figure. The customer ledger
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
6. Every line charged below its standard is flagged, also when the deal as a
   whole is above the standard.
7. For Mahindra Orissa the NDC / cost sheet alone prices the deal ("NDC has to
   come and there is no escape route for it"); the invoices and the customer
   ledger tally the payments only.
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


# ── 1. accessory invoices, and what prices the deal ─────────────────────────
def test_every_accessory_invoice_adds_up_beside_the_line_and_the_ndc_alone_prices_it(journey) -> None:
    """VJ2610-000045: four accessory invoices of Rs 1,07,812; the Tally customer
    ledger posts two of them (Rs 96,537), which stood as the charge."""
    _statement(journey, ADITYA, tally=[  # the NDC prints no accessories row
        _posting("Sales Accessories@18%", "SALES WORKSHOP", debit=90789),
        _posting("Sales Accessories@18%", "SALES WORKSHOP", debit=5748)])
    for number, total, on in (("C1R27B00000717", "90789", "2026-09-30"), ("R1R27B002287", "5310", "2026-10-01"),
                              ("C1R27B00000749", "5748", "2026-10-07"), ("C1R27B00000782", "5965", "2026-10-12")):
        _accessory_invoice(journey, number, total, on)
    row = _row(_deal(journey), "accessories_cost")
    assert D(row["invoiced"]) == D(107812)          # every invoice, added up
    assert D(row["ledger"]) == D(96537)             # the ledger's posting, as a check
    # neither prices the line: the NDC does not charge the accessories
    assert row["billed"] is None and row["current"] is None
    assert row["notTaken"] is True and "NOT_ON_STATEMENT" in row["flags"]


def test_the_ndc_charge_is_the_accessories_figure_with_the_invoices_beside_it(journey) -> None:
    _statement(journey, UTKAL)  # Accessories 30,000 charged on the NDC
    _accessory_invoice(journey, "ACC-1", "20000")
    _accessory_invoice(journey, "ACC-2", "15000", "2026-10-09")
    row = _row(_deal(journey), "accessories_cost")
    assert D(row["billed"]) == D(30000) and row["billedFrom"] == "document"
    assert D(row["invoiced"]) == D(35000) and D(row["freeByInvoice"]) == D(5000)


def test_the_customer_ledger_never_prices_a_line(journey) -> None:
    _statement(journey, _without(UTKAL, "RSA", "Insurance amount"), tally=[
        _posting("RSA CHARGES", "Journal", debit=2021), _posting("INSURANCE PREMIUM", "Journal", debit=40000)])
    _source(journey, "insurance_amount", "insurance_cover", "38201")
    view = _deal(journey)
    rsa = _row(view, "rsa_amount")
    assert rsa["billed"] is None and rsa["current"] is None and D(rsa["ledger"]) == D(2021)
    insurance = _row(view, "insurance_amount")
    assert D(insurance["billed"]) == D(38201) and D(insurance["ledger"]) == D(40000)  # the policy; the ledger checks
    assert "BILLED_VS_LEDGER" in insurance["flags"]


def test_without_the_ndc_the_deal_waits_for_it_whatever_invoices_and_ledger_are_on_file(journey) -> None:
    """Product owner 2026-11-11: "NDC has to come and there is no escape route
    for it ... we will wait till NDC is not there ... Invoice and customer
    ledger will be used to tally the total payments only"."""
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    add_ready_document(journey, "customer_invoice_dms", process_area="DELIVERY", invoice_purpose="VEHICLE_SALE",
                       invoice_nature="TAX_INVOICE", invoice_number="INV-1", invoice_date="2026-10-05",
                       vin_number="MA1NE2XO3T4000001", taxable_amount="701017.32", cgst_rate="9",
                       cgst_amount="63091.56", sgst_rate="9", sgst_amount="63091.56", tcs_amount="8272.00",
                       grand_total_amount="835472.44")
    _accessory_invoice(journey, "ACC-1", "20000")
    _ledger(journey, [_posting("Sales Accessories@18%", "SALES WORKSHOP", debit=20000),
                      _posting("FAST TAG (SALES)", "Sales_Inv", debit=600)])
    _sync(journey)
    view = _deal(journey)
    for key in ("ex_showroom_price", "tcs_amount", "accessories_cost", "fastag_amount"):
        row = _row(view, key)
        assert row["billed"] is None and row["current"] is None, key
        assert row["awaiting"] == "NDC (Account Statement)", key
    assert _row(view, "ex_showroom_price")["invoiced"] is not None  # the invoice is shown beside, to tally
    assert D(_row(view, "accessories_cost")["invoiced"]) == D(20000)
    summary = view["summary"]
    assert summary["payable"] is None and summary["balanceDue"] is None
    assert summary["variance"]["currentVsStandard"] is None  # no deal to set against the standard yet
    assert summary["invoiceNetPayable"] is not None  # what the invoices add up to, for the payment tally


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


def _addons(journey) -> dict:
    from audit_core.uc03_p2_journey360 import addons

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return addons(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)["taken"]


def test_an_add_on_the_ndc_does_not_charge_is_switched_off_also_with_its_invoice_on_file(journey) -> None:
    """Product owner 2026-11-11: "if EW, RSA, FASTag, accessories absent from NDC,
    just add a line to the observation or simply deselect what all is taken by
    customer.. no tasks for it"."""
    _statement(journey, _without(UTKAL, "Shield warranty"))
    _standard(journey, "additional_warranty_amount", "9999")
    add_ready_document(journey, "ew_invoice", process_area="DELIVERY", invoice_number="EW-1",
                       grand_total_amount="9999")
    warranty = _addons(journey)["warranty"]
    assert (warranty["taken"], warranty["amount"]) == (False, None)
    assert warranty["details"]["notOnStatement"].startswith("Not charged on the NDC / cost sheet")
    assert warranty["documentIds"]  # the invoice stays listed
    # Nothing asks for anything about it.
    _missing_sync(journey)
    assert not {k for k in _missing_tasks(journey) if k in {"ew_invoice", "rsa_invoice"}}


def test_an_add_on_the_ndc_charges_stays_switched_on(journey) -> None:
    _statement(journey, UTKAL)
    warranty = _addons(journey)["warranty"]
    assert warranty["taken"] is True and D(warranty["amount"]) == D("9999")
    assert "notOnStatement" not in warranty["details"]


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


def test_every_line_below_its_standard_is_flagged_also_when_the_deal_is_above_the_standard(journey) -> None:
    """Product owner 2026-11-11: "if anything is undercharged, like TCS, ex-showroom
    price, insurance, FASTag, RSA, EW, accessories, it should be flagged"."""
    _line(journey, "ex_showroom_price", "900000", "900000")
    _line(journey, "insurance_amount", "40000", "35000")       # Rs 5,000 below its standard
    _line(journey, "rsa_amount", "2021", "1500")               # Rs 521 below
    _line(journey, "accessories_cost", "20000", "60000")       # Rs 40,000 above
    under = _run(journey)["DEAL_UNDERCHARGED"]
    assert under.outcome == "FAIL"
    assert [c["key"] for c in under.details["components"]] == ["insurance_amount", "rsa_amount"]
    assert "Net deal" not in under.reason  # the net is above the standard: only the lines are short


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


def test_the_buffer_is_the_one_the_reconciliation_last_worked_out_not_an_earlier_month_s(journey) -> None:
    """A row the account statement owns keeps the reconciliation's figures under
    "reconciliation"; its top-level grid is what an earlier run left (live: the
    first September grid on an October deal)."""
    _statement(journey, _without(UTKAL, "Other Discount (if any)"))
    _standard(journey, "ex_showroom_price", "1039000")
    _sql(journey, "INSERT INTO auditcore.discount_applications (tenant_id, journey_id, discount_key, "
                  "standard_eligible_amount, eligibility_result, actual_source_kind, details) "
                  "VALUES (:t, :j, 'ADDITIONAL_DISCOUNT', NULL, 'ELIGIBLE_UNCLAIMED', 'EVIDENCE', CAST(:d AS jsonb))",
         d=json.dumps({"origin": "ACCOUNT_STATEMENT", "potentialAmount": "7000.00",
                       "grid": {"buffer": "7000.00", "gridVersionNo": 1},
                       "reconciliation": {"potentialAmount": "5000.00", "informational": True,
                                          "grid": {"buffer": "5000.00", "gridVersionNo": 5, "tier": "BASE"}}}))
    row = _benefit(_deal(journey), "ADDITIONAL_DISCOUNT")
    assert D(row["entitled"]) == D(5000) and D(row["potential"]) == D(5000)


# ── the NDC / cost sheet is asked for by a task, with the other pending documents ──
def _missing_sync(journey) -> dict:
    from audit_core.uc03_p2_task_producer import sync_document_missing_tasks

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return sync_document_missing_tasks(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _missing_tasks(journey) -> dict[str, dict]:
    rows = _sql(journey, "SELECT task_id, dedupe_key, task_type, source_code, title, task_status, "
                         "assigned_role_code FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j "
                         "AND dedupe_key LIKE 'document-missing:%'").mappings().all()
    return {r["dedupe_key"].rsplit(":", 1)[1]: dict(r) for r in rows}


def test_a_delivery_without_the_ndc_or_cost_sheet_has_a_missing_document_task(journey) -> None:
    """Product owner 2026-11-11: "NDC or cost sheet should be uploaded and there
    should be a task but it should be combined with all the pending docs"."""
    from audit_core.uc03_p2_document_task_groups import DOCUMENT_CATEGORY_BY_TASK_TYPE

    # Still at booking: nothing of the delivery is asked for yet.
    _missing_sync(journey)
    assert "DEALER_STATEMENT" not in _missing_tasks(journey)
    add_ready_document(journey, "tax_invoice_dms", process_area="DELIVERY", invoice_number="INV-1",
                       grand_total_amount="1400000")
    _missing_sync(journey)
    task = _missing_tasks(journey)["DEALER_STATEMENT"]
    assert (task["task_type"], task["source_code"], task["task_status"], task["assigned_role_code"]) == (
        "DOCUMENT_MISSING", "DOCUMENT_MISSING", "READY", "PC")
    assert task["title"] == "Upload the Dealer statement (NDC / cost sheet)"
    # The same family as every other pending document: the Missing documents group of the Task Queue.
    assert DOCUMENT_CATEGORY_BY_TASK_TYPE[task["task_type"]] == "MISSING_DOCUMENT"
    # Asked for once, however often the producer runs.
    _missing_sync(journey)
    assert len([k for k in _missing_tasks(journey) if k == "DEALER_STATEMENT"]) == 1


@pytest.mark.parametrize("statement", ["customer_ledger", "cost_sheet"])
def test_the_ndc_task_closes_itself_once_the_ndc_or_the_cost_sheet_is_read(journey, statement) -> None:
    add_ready_document(journey, "tax_invoice_dms", process_area="DELIVERY", invoice_number="INV-1",
                       grand_total_amount="1400000")
    _missing_sync(journey)
    assert _missing_tasks(journey)["DEALER_STATEMENT"]["task_status"] == "READY"
    add_ready_document(journey, statement, process_area="DELIVERY", ex_showroom_price="1400000")
    _missing_sync(journey)
    assert _missing_tasks(journey)["DEALER_STATEMENT"]["task_status"] in {"VERIFIED_COMPLETE", "CANCELLED"}


def test_the_ndc_task_cannot_be_closed_as_not_required(journey) -> None:
    """Product owner 2026-11-11: "NDC has to come and there is no escape route for it"."""
    from audit_core.uc03_not_mandatory_claims import NOT_MANDATORY_CHOICE
    from audit_core.uc03_p2_tasks import submit_action

    add_ready_document(journey, "tax_invoice_dms", process_area="DELIVERY", invoice_number="INV-1",
                       grand_total_amount="1400000")
    _missing_sync(journey)
    task_id = _missing_tasks(journey)["DEALER_STATEMENT"]["task_id"]
    with pytest.raises(ValueError, match="cannot be marked as not required"), journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        submit_action(c, tenant_id=journey.tenant_id, task_id=task_id, action="CLOSE_TASK",
                      actor_id=journey.actor_id, actor_role_code="PC",
                      comment=f"{NOT_MANDATORY_CHOICE}: the dealer does not give one",
                      details={"choice": NOT_MANDATORY_CHOICE})
    assert _missing_tasks(journey)["DEALER_STATEMENT"]["task_status"] == "READY"
    assert _sql(journey, "SELECT count(*) FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j "
                         "AND dedupe_key LIKE 'not-mandatory-review:%'").scalar_one() == 0
