"""Business insights (product owner 2026-11-10): product penetration, insurance, scrappage,
finance, customer catchment and discount control. The reports are worked out from one fact
per live Journey: the arithmetic is tested on facts written here, the reading of the facts
and the endpoint on a real Postgres."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    add_evidence,
    add_extracted_field,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import uc03_p2_insights as insights
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_deal_source_history import record_source_value

# --------------------------------------------------------------------------- facts written by hand

def _fact(**over):
    base = {
        "journeyId": uuid4().hex, "reference": "J-1", "customer": "A Customer", "dealerId": "d1", "dealer": "Dealer A",
        "outletId": "o1", "outlet": "Outlet 1", "pc": "PC One", "bookingDate": date(2026, 10, 5),
        "deliveredOn": None, "model": "SUV", "variant": "Top", "category": "PV", "statement": True,
        "dealSheet": True, "charged": {}, "sheet": {}, "rto": {}, "exShowroom": 1_000_000.0, "policy": None,
        "channel": insights.NO_POLICY, "dealerCode": False, "invoicePremium": None, "finance": None,
        "certificates": [], "discounts": [], "verdicts": [], "pin": None, "districtRaw": None,
        "districtKey": None, "district": None, "sanctioned": None, "policyEnd": None, "loan": None,
        "bookingDateRead": True, "deliveryStarted": False, "receipts": [], "refunds": [],
    }
    return {**base, **over}


def _row(rows, label):
    return next(r for r in rows if r["label"] == label)


def test_spellings_of_one_district_are_one_district():
    key = insights.district_key
    assert key("KHORDA") == key("Khordha") == key("khurda")
    assert key("JAJPUR") == key("JAJAPUR")
    assert key("JAGATSINGHPUR") == key("Jagatsinghapur")
    assert key("BALESHWAR") == key("BALESWAR") and key("ANGUL") == key("ANUGUL")
    assert key("CUTTACK") == key("CATTACK")
    assert key("CUTTACK") != key("KHORDA") and key("PURI") != key("GANJAM")
    assert key("") == "" and key("ab") == ""

    facts = [
        _fact(pin="752001", districtRaw="KHORDA"), _fact(pin="752001", districtRaw="Khordha"),
        _fact(pin="752001", districtRaw="KHORDHA"),
        # No district read: the PIN's other customers give it.
        _fact(pin="752001", districtRaw=None),
        _fact(pin="753001", districtRaw="Cuttack"), _fact(pin=None, districtRaw=None),
    ]
    insights._resolve_districts(facts)
    assert [f["district"] for f in facts[:4]] == ["Khordha"] * 4  # the commonest spelling names the district
    assert facts[4]["district"] == "Cuttack" and facts[5]["district"] is None


def test_amounts_names_and_states_are_read_as_documents_write_them():
    assert insights.parse_amount("9,90,000.00") == 990000.0
    assert insights.parse_amount("Rs. 12,50,000/-") == 1250000.0
    assert insights.parse_amount(750000) == 750000.0
    assert insights.parse_amount("not a figure") is None and insights.parse_amount(None) is None
    assert insights.lender_group("MAHINDRA & MAHINDRA FINANCIAL SERVICES LTD") == "Mahindra Finance"
    assert insights.lender_group("sbi, cuttack main branch") == "State Bank of India"
    assert insights.lender_group("Utkal Grameen Bank") == "Regional rural bank"
    assert insights.lender_group("Some New Lender") == "Some New Lender" and insights.lender_group(" ") is None
    assert insights.insurer_group("TATA AIG GENERAL INSURANCE CO LTD") == "Tata AIG"
    assert insights.cover_group("Nil Depreciation Waiver cover") == "Zero depreciation"
    assert insights.cover_group("Return to Invoice - A Purchase Invoice value") == "Return to invoice"
    assert insights.cover_group("Something unusual") == "Other cover"
    assert insights.old_vehicle_state("DL 3C AB 1234") == "Delhi"
    assert insights.old_vehicle_state("OR-05-X-1") == insights.old_vehicle_state("OD05X1") == "Odisha"
    assert insights.old_vehicle_state("") is None


def test_in_house_insurance_follows_the_one_rule_in_its_order():
    codes = {"MIBLMMAAACU2665C000"}
    policy = {"insurer": "Tata AIG", "policyNumber": "P1", "premium": 40000.0, "confirmed": None,
              "agentCode": "PS000041087", "mispCode": None}
    channel = insights.insurance_channel
    # A policy with an outside agent's code and nothing on the statement: outside, not confirmed.
    assert channel(_fact(policy=policy), codes) == (insights.OUTSIDE_UNCONFIRMED, False)
    # The statement charges the premium: in-house, whatever code the policy carries.
    assert channel(_fact(policy=policy, sheet={"insurance_amount": 41000.0}), codes)[0] == insights.INHOUSE_STATEMENT
    # The dealer's own MISP code on the policy.
    own = {**policy, "mispCode": "MIBL/M&M/AAACU2665C/000"}
    assert channel(_fact(policy=own), codes) == (insights.INHOUSE_DEALER_CODE, True)
    # The PC's answer decides over the documents, either way.
    assert channel(_fact(policy={**own, "confirmed": "EXTERNAL"}, sheet={"insurance_amount": 1.0}), codes)[0] == \
        insights.EXTERNAL_CONFIRMED
    assert channel(_fact(policy={**policy, "confirmed": "INHOUSE"}), codes)[0] == insights.INHOUSE_CONFIRMED
    # Nothing read at all.
    assert channel(_fact(policy=None), codes) == (insights.NO_POLICY, False)
    # A premium on the statement with no policy uploaded yet is still the dealer's sale.
    assert channel(_fact(policy=None, sheet={"insurance_amount": 30000.0}), codes)[0] == insights.INHOUSE_STATEMENT


def test_penetration_is_of_statement_deals_and_compares_like_for_like():
    rsa = {"charged": {"rsa_amount": 2000.0}}
    facts = (
        [_fact(outletId="a", outlet="A", model="SUV", **rsa) for _ in range(4)]            # A: 4 SUVs, all with RSA
        + [_fact(outletId="b", outlet="B", model="SUV") for _ in range(4)]                 # B: 4 SUVs, none
        + [_fact(outletId="b", outlet="B", model="TRUCK", category="CV") for _ in range(4)]  # and 4 trucks, none
        + [_fact(outletId="c", outlet="C", model="TRUCK", category="CV") for _ in range(4)]  # C: 4 trucks, none
        # A booking with only a quote: no statement, so it is in no percentage.
        + [_fact(outletId="a", outlet="A", statement=False, charged={})]
    )
    report = insights.penetration_report(facts)
    assert report["basis"] == {"liveJourneys": 17, "statementDeals": 16, "withoutStatement": 1,
                               "thinBelow": insights.THIN_SAMPLE}
    assert report["network"]["products"]["rsa"] == {"count": 4, "percent": 25.0, "amount": 8000, "typical": 2000}
    a, b, c = (_row(report["outlets"], name) for name in "ABC")
    assert a["products"]["rsa"]["percent"] == 100.0 and b["products"]["rsa"]["percent"] == 0.0
    # The network sells RSA on half its SUVs and on no truck: B's mix would give 2, C's none.
    assert (a["products"]["rsa"]["expected"], a["products"]["rsa"]["gap"]) == (2.0, 2.0)
    assert (b["products"]["rsa"]["expected"], b["products"]["rsa"]["gap"]) == (2.0, -2.0)
    assert (c["products"]["rsa"]["expected"], c["products"]["rsa"]["gap"]) == (0.0, 0.0)
    assert a["thin"] and a["parent"] == "Dealer A"
    suv = _row(report["models"], "SUV")
    assert suv["deals"] == 8 and suv["products"]["rsa"]["percent"] == 50.0 and "expected" not in suv["products"]["rsa"]


def test_every_product_is_counted_from_its_own_evidence():
    fact = _fact(
        charged={"fastag_amount": 600.0, "additional_warranty_amount": 18000.0, "insurance_amount": 45000.0,
                 "accessories_cost": 25000.0},
        sheet={"insurance_amount": 45000.0}, channel=insights.INHOUSE_STATEMENT,
        finance={"lender": "State Bank of India", "lenderRaw": "SBI", "disbursed": None}, loan=800000.0,
        certificates=[{"number": "C1", "key": "C1", "kind": "TRANSFERRED", "oldRegistration": "DL1", "state": "Delhi",
                       "facility": None}],
    )
    cells = insights.penetration_report([fact, _fact()])["network"]["products"]
    assert {key: cell["count"] for key, cell in cells.items()} == {
        "finance": 1, "insurance": 1, "accessories": 1, "fastag": 1, "rsa": 0, "extendedWarranty": 1, "scrappage": 1}
    assert cells["finance"]["amount"] == 800000 and cells["insurance"]["amount"] == 45000
    assert cells["accessories"]["amount"] == 25000
    assert cells["extendedWarranty"]["typical"] == 18000 and cells["scrappage"]["amount"] is None


def test_insurance_report_names_the_agents_outside_policies_went_through():
    def policy(agent, code, premium, covers=()):
        return {"insurer": "ICICI Lombard GIC", "policyNumber": uuid4().hex, "premium": premium, "confirmed": None,
                "agentName": agent, "agentCode": code, "mispCode": None, "covers": list(covers)}

    facts = [
        _fact(channel=insights.INHOUSE_STATEMENT, sheet={"insurance_amount": 50000.0},
              policy=policy("Dealer", "D-1", 48000.0, ["Zero Depreciation", "Engine Protect"]),
              policyEnd=date(2027, 10, 4)),
        _fact(channel=insights.OUTSIDE_UNCONFIRMED, policy=policy("saswati dhar", "PS000041087", 30000.0, ["Nil Dep"]),
              policyEnd=date(2027, 10, 20)),
        _fact(channel=insights.OUTSIDE_UNCONFIRMED, policy=policy("SASWATI DHAR", "PS-000041087", 20000.0),
              outletId="o2", outlet="Outlet 2", policyEnd=date(2027, 11, 2)),
        _fact(channel=insights.EXTERNAL_CONFIRMED, policy=policy("Policy Bazaar", None, 25000.0)),
        # The dealer's own code, and no premium on the statement.
        _fact(channel=insights.INHOUSE_DEALER_CODE, dealerCode=True, policy=policy("Dealer", "D-1", 40000.0)),
        _fact(),                                # no policy
        _fact(statement=False, channel=insights.OUTSIDE_UNCONFIRMED, policy=policy("x", "y", 1.0)),  # no statement
    ]
    report = insights.insurance_report(facts, today=date(2026, 11, 10))
    network = report["network"]
    assert (network["deals"], network["inhouse"], network["outside"], network["noPolicy"]) == (6, 2, 3, 1)
    assert network["inhousePremium"] == 90000 and network["outsidePremium"] == 75000
    assert network["inhousePercent"] == 33.3 and network["channels"][insights.OUTSIDE_UNCONFIRMED] == 2
    # Two spellings of one agent code are one agent, across outlets.
    first = report["outsideAgents"][0]
    assert (first["agent"], first["policies"], first["premium"]) == ("Saswati Dhar", 2, 50000)
    assert {o["outlet"] for o in first["outlets"]} == {"Outlet 1", "Outlet 2"}
    assert report["outsideAgents"][1]["agent"] == "Policy Bazaar" and report["outsideAgents"][1]["confirmedOutside"] == 1
    assert report["dealerCodeOffStatement"]["policies"] == 1 and report["dealerCodeOffStatement"]["premium"] == 40000
    assert report["insurers"][0] == {"insurer": "ICICI Lombard", "policies": 5, "premium": 165000, "inhouse": 2,
                                     "outside": 3, "sharePercent": 100.0}
    assert {c["cover"]: c["policies"] for c in report["covers"]} == {"Zero depreciation": 2, "Engine protect": 1}
    assert [(r["month"], r["policies"]) for r in report["renewals"]] == [("2027-10", 2), ("2027-11", 1)]


def test_an_agent_whose_code_was_read_two_ways_is_one_agent():
    def outside(agent, code, premium, outlet="Outlet 1"):
        return _fact(channel=insights.OUTSIDE_UNCONFIRMED, outlet=outlet, policy={
            "insurer": "X", "policyNumber": uuid4().hex, "premium": premium, "confirmed": None,
            "agentName": agent, "agentCode": code, "mispCode": None, "covers": []})

    facts = [outside("Saswati Dhar", "PS0000041087", 30000.0), outside("Saswati Dhar", "PS0000041087", 30000.0),
             outside("SASWATI  DHAR", "PS000041087", 20000.0, outlet="Outlet 2"),     # a zero dropped by the reader
             outside("Saswati Dhar", None, 10000.0),                                  # the code not read at all
             outside("Mathura Bhoi", "NY0000001773", 15000.0),
             outside(None, "ZZ-1", 5000.0), outside(None, "ZZ-2", 5000.0),            # no name: never joined
             outside(None, None, 1000.0)]
    agents = insights.insurance_report(facts, today=date(2026, 11, 10))["outsideAgents"]
    assert [(a["agent"], a["policies"], a["premium"]) for a in agents] == [
        ("Saswati Dhar", 4, 90000), ("Mathura Bhoi", 1, 15000), ("Name not read", 1, 5000), ("Name not read", 1, 5000),
        ("No agent or code on the policy", 1, 1000)]
    assert agents[0]["code"] == "PS0000041087" and agents[0]["codes"] == ["PS0000041087", "PS000041087"]
    assert {o["outlet"]: o["policies"] for o in agents[0]["outlets"]} == {"Outlet 1": 3, "Outlet 2": 1}
    assert len(agents[0]["journeys"]) == 4


def test_scrappage_report_shows_where_certificates_come_from_and_what_they_save():
    def cert(number, state="Delhi", kind="TRANSFERRED"):
        return [{"number": number, "key": number, "kind": kind, "oldRegistration": "X", "state": state,
                 "facility": None}]

    facts = [
        # With a certificate the RTO took 7.5%; without, 10%: the same band, the same model.
        _fact(reference="S1", exShowroom=1_600_000.0, rto={"road_tax_amount": 120_000.0, "registration_charges": 2000.0},
              charged={"registration_charges": 122_000.0}, certificates=cert("C-1")),
        _fact(reference="S2", exShowroom=1_600_000.0, rto={"road_tax_amount": 120_000.0}, certificates=cert("C-2", "Haryana"),
              charged={"registration_charges": 180_000.0}),
        _fact(reference="S3", exShowroom=1_600_000.0, rto={"road_tax_amount": 160_000.0}),
        _fact(reference="S4", exShowroom=1_600_000.0, rto={"road_tax_amount": 160_000.0}),
        # The same certificate on a second Journey.
        _fact(reference="S5", outletId="o2", outlet="Outlet 2", certificates=cert("C-1"),
              discounts=[{"key": "SCRAPPAGE_BONUS_DEALER", "amount": 20000.0}]),
        # A local vehicle, the buyer's own.
        _fact(reference="S6", model="HATCH", certificates=cert("C-3", "Odisha", "ORIGINAL")),
        _fact(reference="S7", model="TRUCK", category="CV", rto={"road_tax_amount": 60_000.0}),
    ]
    report = insights.scrappage_report(facts, home_states={"Odisha"})
    summary = report["summary"]
    assert summary["journeysWithCertificate"] == 4 and summary["percentOfStatementDeals"] == 57.1
    assert (summary["transferred"], summary["original"]) == (3, 1)
    assert (summary["bonusGiven"], summary["certificateWithoutBonus"]) == (1, 3)
    assert (summary["oldVehiclesOutsideState"], summary["oldVehiclesStateRead"]) == (3, 4)
    assert summary["certificatesUsedTwice"] == 1
    assert [j["reference"] for j in report["usedTwice"][0]["journeys"]] == ["S1", "S5"]
    band = _row([{**b, "label": b["band"]} for b in report["roadTaxEffect"]], "₹10–20 lakh")
    assert (band["withCertificate"], band["withoutCertificate"]) == (2, 2)
    assert (band["taxPercentWith"], band["taxPercentWithout"]) == (7.5, 10.0)
    # 2.5% of Rs 16 lakh on each of the two certificate deals.
    assert band["savingPerDeal"] == 40000 and summary["estimatedRoadTaxSaved"] == 80000
    # S2's statement charges Rs 60,000 more than the RTO took; S1's agrees with it.
    assert summary["chargedAbovePaid"] == {"deals": 1, "amount": 60000}
    s1 = next(r for r in report["register"] if r["reference"] == "S1")
    assert (s1["paidAtRto"], s1["chargedAbovePaid"], s1["state"], s1["roadTaxPercent"]) == (122000, 0, "Delhi", 7.5)
    assert _row(report["states"] and [{**s, "label": s["state"]} for s in report["states"]], "Delhi")["certificates"] == 2
    # No home state known: nothing is called "outside".
    assert insights.scrappage_report(facts)["summary"]["oldVehiclesOutsideState"] is None


def test_finance_report_reads_the_loan_and_compares_sanction_with_disbursal():
    def financed(lender, *, sanctioned=None, disbursed=None, ex=1_000_000.0, **over):
        fact = _fact(finance={"lender": insights.lender_group(lender), "lenderRaw": lender, "disbursed": disbursed},
                     sanctioned=sanctioned, exShowroom=ex, **over)
        fact["loan"] = insights.loan_amount(fact)
        return fact

    facts = [
        financed("SBI", sanctioned=850_000.0, disbursed=850_000.0),
        financed("State Bank of India", sanctioned=900_000.0, disbursed=800_000.0, reference="GAP"),
        financed("HDFC BANK LTD", disbursed=1_050_000.0),                 # funded above ex-showroom
        financed("HDFC", sanctioned=500_000.0),                          # half the price
        # Rs 1,500 is the hypothecation fee, never a loan; a 9-digit figure is a misread.
        financed("Chola", sanctioned=1500.0, disbursed=150_000_000.0),
        _fact(), _fact(),
        financed("SBI", sanctioned=700_000.0, statement=False),           # no statement: outside the percentages
    ]
    report = insights.finance_report(facts)
    summary = report["summary"]
    assert (summary["deals"], summary["financed"], summary["percent"]) == (7, 5, 71.4)
    assert (summary["loanKnown"], summary["loanTotal"], summary["typicalLoan"]) == (4, 3_300_000, 875_000)
    assert (summary["loanAboveExShowroom"], summary["loanBelowSixtyPercent"]) == (1, 1)
    assert summary["sanctionAndDisbursalRead"] == 2 and summary["sanctionDiffersFromDisbursal"] == 1
    assert summary["shortDisbursed"] == {"deals": 1, "amount": 100000}
    assert report["sanctionGaps"][0]["reference"] == "GAP" and report["sanctionGaps"][0]["short"] == 100000
    sbi = next(r for r in report["lenders"] if r["lender"] == "State Bank of India")   # "SBI" is the same lender
    assert (sbi["deals"], sbi["sharePercent"], sbi["loanTotal"]) == (2, 40.0, 1_750_000)
    assert sbi["typicalLoanToValue"] == 87.5
    bands = {b["band"]: b["deals"] for b in report["loanToValueBands"]}
    assert bands["Above ex-showroom"] == 1 and bands["Under 60%"] == 1 and bands["85–95%"] == 2
    assert report["basis"]["financedAllLiveJourneys"] == 6


def test_catchment_report_places_customers_by_district_and_pin():
    facts = [
        _fact(pin="758001", districtRaw="KENDUJHAR", charged={"rsa_amount": 2000.0}),
        _fact(pin="758001", districtRaw="Kendujhar"),
        _fact(pin="758002", districtRaw="KENDUJHAR", statement=False),
        _fact(pin="753001", districtRaw="CUTTACK", outletId="o2", outlet="Outlet 2"),
        _fact(pin=None, districtRaw=None, outletId="o2", outlet="Outlet 2"),
    ]
    insights._resolve_districts(facts)
    report = insights.catchment_report(facts)
    assert report["basis"]["withPin"] == 4 and report["basis"]["withoutPlace"] == 1
    assert (report["basis"]["pins"], report["basis"]["districts"]) == (3, 2)
    kendujhar = _row(report["districts"], "Kendujhar")
    assert (kendujhar["journeys"], kendujhar["pins"], kendujhar["statementDeals"]) == (3, 2, 2)
    assert kendujhar["sharePercent"] == 60.0 and kendujhar["products"]["rsa"]["percent"] == 50.0
    pin = _row(report["pins"], "758001")
    assert (pin["journeys"], pin["parent"]) == (2, "Kendujhar")
    outlet = _row(report["outlets"], "Outlet 2")
    assert (outlet["journeys"], outlet["located"], outlet["homeDistrict"], outlet["homeSharePercent"]) == \
        (2, 1, "Cuttack", 100.0)


def _line(key, status, given, *, entitled=None, variance=None, kit=None, reason=None):
    return {"key": key, "label": key.title(), "status": status, "given": given, "entitled": entitled,
            "variance": variance, "kit": kit, "reason": reason}


def test_discount_report_carries_the_deals_own_verdicts_and_measures_like_for_like():
    def deal(outlet, model, ex, given, verdicts=(), **over):
        return _fact(outletId=outlet, outlet=outlet.upper(), model=model, exShowroom=ex,
                     discounts=[{"key": "CASH_DISCOUNT", "amount": given}] if given else [],
                     verdicts=list(verdicts), **over)

    facts = [
        # SUVs: the network gives 2% (A 1%, B 3%).
        deal("a", "SUV", 1_000_000.0, 10_000.0), deal("a", "SUV", 1_000_000.0, 10_000.0),
        deal("a", "SUV", 1_000_000.0, 10_000.0),
        deal("b", "SUV", 1_000_000.0, 30_000.0, [_line("CASH_DISCOUNT", insights.OVER, 30_000.0, entitled=20_000.0,
                                                    variance=10_000.0)]),
        deal("b", "SUV", 1_000_000.0, 30_000.0, [_line("CASH_DISCOUNT", insights.KIT_AS_CASH, 30_000.0,
                                                    entitled=10_000.0, variance=20_000.0, kit=15_000.0)]),
        deal("b", "SUV", 1_000_000.0, 30_000.0, [
            _line("CORPORATE_PRIVILEGE", insights.UNKNOWN, 30_000.0, reason="the corporate category is still to be confirmed"),
            _line("EXCHANGE_BONUS", insights.UNDER, 0.0, entitled=25_000.0, variance=-25_000.0)]),
        # One discount of 8% and one misread price: Rs 9 crore for an SUV.
        deal("b", "SUV", 1_000_000.0, 80_000.0, reference="BIG"),
        deal("b", "SUV", 90_000_000.0, 50_000.0, reference="MISREAD"),
        deal("a", "SUV", 1_000_000.0, 0.0, statement=False, dealSheet=False),   # nothing billed yet
    ]
    findings = [
        {"journey_id": facts[3]["journeyId"], "rule_key": "EXCESS_DISCOUNT", "finding_status": "OPEN",
         "disposition": None, "resolution_reason": None},
        {"journey_id": facts[3]["journeyId"], "rule_key": "EXCESS_DISCOUNT", "finding_status": "RESOLVED",
         "disposition": "FIXED", "resolution_reason": None},
        {"journey_id": facts[4]["journeyId"], "rule_key": "KIT_GIVEN_AS_CASH", "finding_status": "RESOLVED",
         "disposition": "CONFIRMED_BREACH", "resolution_reason": "Kit given as cash"},
        {"journey_id": facts[4]["journeyId"], "rule_key": "EXCESS_DISCOUNT", "finding_status": "VOIDED",
         "disposition": None, "resolution_reason": None},
        {"journey_id": uuid4().hex, "rule_key": "EXCESS_DISCOUNT", "finding_status": "OPEN", "disposition": None,
         "resolution_reason": None},                                       # a Journey outside the reader's scope
    ]
    report = insights.discount_report(facts, findings, not_yet_calculated=3)
    assert report["basis"]["billedDeals"] == 8 and report["basis"]["priceInDoubt"] == 1
    assert report["basis"]["notYetCalculated"] == 3
    network = report["network"]
    # Rupees count every billed deal; the percentage leaves the misread price out.
    assert network["given"] == 250_000 and network["exShowroom"] == 7_000_000 and network["givenPercent"] == 2.86
    assert network["verdicts"][insights.OVER] == {"lines": 1, "journeys": 1, "amount": 10000}
    assert network["verdicts"][insights.KIT_AS_CASH]["amount"] == 15000
    assert network["verdicts"][insights.UNKNOWN]["amount"] == 30000
    assert network["verdicts"][insights.UNDER]["amount"] == 25000
    a, b = _row(report["outlets"], "A"), _row(report["outlets"], "B")
    # At the network's SUV rate (Rs 2,00,000 on Rs 70 lakh) A would give 85,714 and gives 30,000.
    assert (a["givenPercent"], a["index"]) == (1.0, 0.35) and a["aboveExpected"] < 0
    assert b["givenPercent"] == 4.25 and b["index"] == 1.49 and b["aboveExpected"] > 0
    assert report["unknownByReason"] == [{
        "reason": "the corporate category is still to be confirmed", "key": "CORPORATE_PRIVILEGE",
        "label": "Corporate_Privilege", "lines": 1, "amount": 30000}]
    cash = _row(report["byDiscount"], "Consumer offer / scheme discount")
    assert (cash["deals"], cash["given"], cash["over"], cash["kitAsCash"]) == (8, 250000, 10000, 15000)
    assert [o["reference"] for o in report["outliers"]] == ["BIG"] and report["outliers"][0]["percent"] == 8.0
    assert report["concentration"] == {"discountedDeals": 7, "aboveMark": 1, "aboveMarkGiven": 80000,
                                       "aboveMarkSharePercent": 40.0}
    excess = next(r for r in report["decisions"]["rules"] if r["rule"] == "EXCESS_DISCOUNT")
    assert (excess["raised"], excess["open"], excess["closedOnRecheck"]) == (2, 1, 1)
    assert report["decisions"]["decidedByAPerson"] == 1 and report["decisions"]["remarks"][0]["remark"] == "Kit given as cash"


def test_a_large_discount_on_a_misread_price_is_still_an_outlier():
    facts = [_fact(exShowroom=1_000_000.0) for _ in range(3)] + [
        _fact(reference="LAKHS", exShowroom=90_000_000.0, discounts=[{"key": "EXCHANGE_BONUS", "amount": 730_000.0}])]
    outliers = insights.discount_report(facts, [])["outliers"]
    assert [(o["reference"], o["given"], o["percent"]) for o in outliers] == [("LAKHS", 730000, None)]


def test_sales_report_counts_what_was_booked_billed_and_delivered():
    facts = [
        _fact(bookingDate=date(2026, 10, 1), deliveredOn=date(2026, 10, 9), deliveryStarted=True),       # 8 days
        _fact(bookingDate=date(2026, 10, 20), deliveredOn=date(2026, 11, 3), deliveryStarted=True),      # 14 days
        _fact(bookingDate=date(2026, 10, 25), deliveryStarted=True, exShowroom=1_200_000.0),
        # Opened on 2 Nov with no booking date read: a November booking, and no days to deliver.
        _fact(bookingDate=date(2026, 11, 2), bookingDateRead=False, deliveredOn=date(2026, 11, 5),
              outletId="o2", outlet="Outlet 2", model="TRUCK", category="CV", exShowroom=800_000.0),
        _fact(bookingDate=date(2026, 11, 4), statement=False, dealSheet=False, exShowroom=None, outletId="o2",
              outlet="Outlet 2", model=None, category=None),
        _fact(bookingDate=date(2026, 11, 6), exShowroom=90_000_000.0, reference="MISREAD"),
    ]
    report = insights.sales_report(facts)
    network = report["network"]
    assert (network["journeys"], network["deliveryStarted"], network["delivered"]) == (6, 3, 3)
    assert network["statementDeals"] == 5 and network["billedDeals"] == 4      # the misread price is left out
    assert network["billedValue"] == 4_000_000 and network["typicalExShowroom"] == 1_000_000
    assert (network["typicalDaysToDeliver"], network["daysMeasuredOn"]) == (11.0, 2)
    assert report["basis"]["priceInDoubt"] == 1 and report["basis"]["modelNotResolved"] == 1
    assert report["months"] == [{"month": "2026-10", "booked": 3, "delivered": 1},
                                {"month": "2026-11", "booked": 3, "delivered": 2}]
    assert {b["band"]: b["deals"] for b in report["daysToDeliverBands"]}["8–15 days"] == 2
    second = _row(report["outlets"], "Outlet 2")
    assert (second["journeys"], second["sharePercent"], second["delivered"], second["billedValue"]) == \
        (2, 33.3, 1, 800_000)
    assert _row(report["models"], insights.MODEL_NOT_RESOLVED)["journeys"] == 1
    assert _row(report["categories"], "CV")["journeys"] == 1


def test_a_delivery_date_after_today_is_delivered_but_not_charted_or_measured():
    facts = [
        _fact(bookingDate=date(2026, 10, 1), deliveredOn=date(2026, 10, 9)),
        _fact(bookingDate=date(2026, 10, 3), deliveredOn=date(2027, 10, 9)),       # the year misread
    ]
    report = insights.sales_report(facts, today=date(2026, 11, 10))
    assert report["network"]["delivered"] == 2 and report["basis"]["deliveryDateInDoubt"] == 1
    assert report["months"] == [{"month": "2026-10", "booked": 2, "delivered": 1}]
    assert (report["network"]["typicalDaysToDeliver"], report["network"]["daysMeasuredOn"]) == (8.0, 1)
    assert sum(b["deals"] for b in report["daysToDeliverBands"]) == 1


def test_a_price_not_read_is_not_a_price_in_doubt():
    facts = [_fact() for _ in range(3)] + [_fact(exShowroom=None), _fact(exShowroom=90_000_000.0)]
    basis = insights.discount_report(facts, [])["basis"]
    assert (basis["billedDeals"], basis["pricedDeals"], basis["priceNotRead"], basis["priceInDoubt"]) == (5, 3, 1, 1)


def test_payments_report_measures_cash_against_the_limit():
    def receipt(amount, mode, label=None, day=date(2026, 10, 5), number="R-1"):
        return {"amount": amount, "mode": mode, "modeLabel": label or mode.title(), "date": day,
                "receiptNumber": number}

    facts = [
        _fact(reference="BANK", receipts=[receipt(900_000.0, "RTGS"), receipt(50_000.0, "CASH")]),
        # One cash receipt above the limit.
        _fact(reference="ONE-BIG", outletId="o2", outlet="Outlet 2", receipts=[receipt(250_000.0, "CASH", number="R-9")]),
        # No single receipt above it, Rs 3,00,000 in cash in all.
        _fact(reference="SPLIT", outletId="o2", outlet="Outlet 2",
              receipts=[receipt(150_000.0, "CASH"), receipt(150_000.0, "CASH"), receipt(100_000.0, "OTHERS")]),
        _fact(reference="REFUND", refunds=[receipt(-20_000.0, "REFUND")]),
    ]
    report = insights.payments_report(facts)
    network = report["network"]
    assert (network["receipts"], network["amount"], network["journeysWithReceipts"]) == (6, 1_600_000, 3)
    assert (network["cashReceipts"], network["cashAmount"], network["cashPercent"]) == (4, 600_000, 37.5)
    assert (network["cashAboveLimit"], network["cashAboveLimitAmount"]) == (1, 250_000)
    assert network["journeysCashAboveLimitInAll"] == 2 and network["journeysPayingCash"] == 3
    assert (network["modeNotRead"], network["modeNotReadPercent"]) == (1, 6.2)
    assert report["basis"]["cashLimit"] == 200_000 and report["basis"]["refundAmount"] == 20_000
    assert [(m["label"], m["receipts"], m["sharePercent"]) for m in report["modes"]] == [
        ("Rtgs", 1, 56.2), ("Cash", 4, 37.5), ("Mode not read", 1, 6.2)]
    assert [(r["reference"], r["amount"], r["receiptNumber"]) for r in report["cashAboveLimit"]] == [("ONE-BIG", 250_000, "R-9")]
    assert [(r["reference"], r["cashTotal"]) for r in report["cashAboveLimitInAll"]] == [("SPLIT", 300_000), ("ONE-BIG", 250_000)]
    second = _row(report["outlets"], "Outlet 2")
    assert (second["cashPercent"], second["cashAboveLimit"]) == (84.6, 1)


def test_cash_at_the_limit_and_just_under_it_is_counted_apart_from_cash_above_it():
    def cash(amount, number, day=date(2026, 10, 5)):
        return {"amount": amount, "mode": "CASH", "modeLabel": "Cash", "date": day, "receiptNumber": number}

    facts = [
        _fact(reference="AT", receipts=[cash(200_000.0, "R-1"), cash(200_000.0, "R-2", date(2026, 10, 6))]),
        _fact(reference="NEAR", outletId="o2", outlet="Outlet 2", receipts=[cash(199_000.0, "R-3"), cash(190_000.0, "R-4")]),
        _fact(reference="ABOVE", receipts=[cash(200_001.0, "R-5")]),
        _fact(reference="SMALL", receipts=[cash(189_999.0, "R-6"),
                                           {"amount": 200_000.0, "mode": "RTGS", "modeLabel": "RTGS", "date": None,
                                            "receiptNumber": "R-7"}]),
    ]
    report = insights.payments_report(facts)
    network = report["network"]
    assert (network["cashAboveLimit"], network["cashAtLimit"], network["cashNearLimit"]) == (1, 2, 2)
    assert (network["cashAtLimitAmount"], network["cashNearLimitAmount"]) == (400_000, 389_000)
    assert report["basis"]["nearLimitFrom"] == 190_000
    assert [(r["reference"], r["receiptNumber"], r["band"]) for r in report["cashAroundLimit"]] == [
        ("ABOVE", "R-5", "ABOVE"), ("AT", "R-1", "AT"), ("AT", "R-2", "AT"), ("NEAR", "R-3", "NEAR"), ("NEAR", "R-4", "NEAR")]
    second = _row(report["outlets"], "Outlet 2")
    assert (second["cashAtLimit"], second["cashNearLimit"]) == (0, 2)


# --------------------------------------------------------------------------- read from Postgres

@pytest.fixture
def setup():
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="insight")
    acting = {"subject": journey.actor_id, "role": "PC"}
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=acting["subject"])
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization(acting["role"])
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield SimpleNamespace(journey=journey, client=client, acting=acting,
                              base=f"/p2/v1/tenants/{journey.tenant_id}/insights")
    finally:
        app.dependency_overrides.clear()
        with engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            connection.execute(text("DELETE FROM auditcore.dealer_insurance_codes WHERE tenant_id=:t"),
                               {"t": journey.tenant_id})
        delete_tenant_data(engine, journey.tenant_id)
        engine.dispose()


def _exec(s, sql: str, **params):
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        return connection.execute(text(sql), {"t": s.journey.tenant_id, "j": s.journey.journey_id, **params})


def _source(s, journey_id, document_type: str, component: str, amount: int, *, evidence=None, document=None):
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        record_source_value(connection, tenant_id=s.journey.tenant_id, journey_id=journey_id, line_kind="COMMERCIAL",
                            component_key=component, source_document_type=document_type, amount=Decimal(amount),
                            evidence_id=evidence, document_id=document)


def _second_journey(s, *, outlet=None, reference="INS-2"):
    return _exec(s, "INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, journey_reference, "
                    "created_by_actor_id) VALUES (:t, :d, :o, :c, :r, :a) RETURNING journey_id",
                 d=s.journey.dealer_id, o=outlet or s.journey.outlet_id, c=s.journey.customer_id,
                 r=f"{reference}-{uuid4().hex[:6]}", a=s.journey.actor_id).scalar_one()


def _facts(s):
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        return {f["journeyId"]: f for f in insights.load_facts(connection, tenant_id=s.journey.tenant_id)}


def _seed_deal(s):
    """One delivered deal as the documents give it: the NDC, the RTO challan, the policy, the
    loan, a scrappage certificate, the Deal's discount lines and its verdict."""
    j = s.journey.journey_id
    _exec(s, "UPDATE auditcore.dealer_outlets SET state_region='Odisha' WHERE tenant_id=:t")
    _exec(s, "INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date) VALUES (:t, :j, CURRENT_DATE - 3)")
    for component, amount in (("ex_showroom_price", 1_600_000), ("insurance_amount", 52_000), ("rsa_amount", 2021),
                              ("fastag_amount", 600), ("additional_warranty_amount", 17_999),
                              ("registration_charges", 124_000)):
        _source(s, j, "Customer Ledger", component, amount)          # spelt as a reader may spell it
    _source(s, j, "rto_challan", "road_tax_amount", 120_000)
    _source(s, j, "rto_challan", "registration_charges", 2_000)
    _source(s, j, "booking_form", "rsa_amount", 9_999)               # a quote, never a sale
    _exec(s, "INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, actual_amount, "
             "source_reference) VALUES (:t, :j, 'ex_showroom_price', 1600000, 'customer_ledger:doc')")
    _exec(s, "INSERT INTO auditcore.insurance_records (tenant_id, journey_id, insurer_name, policy_reference, "
             "actual_premium_amount, agent_intermediary_name, agent_intermediary_code, source_kind, add_ons) "
             "VALUES (:t, :j, 'TATA AIG GENERAL INSURANCE', 'POL-1', 51000, 'Dealer Desk', 'AG-1', 'EVIDENCE', "
             "CAST(:a AS jsonb))", a=json.dumps(json.dumps(["Zero Depreciation", "Engine Protect"])))
    _exec(s, "INSERT INTO auditcore.finance_records (tenant_id, journey_id, finance_type_code, provider_name, "
             "financed_amount, loan_disbursement_amount, details) VALUES (:t, :j, 'LOAN', 'state bank of india', "
             "1500, 1300000, '{}'::jsonb)")
    certificate = uuid4()
    evidence = add_evidence(s.journey, di_document_id=certificate, document_type_key="scrappage_certificate_of_deposit",
                            process_area="DELIVERY")
    _exec(s, "INSERT INTO auditcore.scrappage_certificate_review_values (tenant_id, journey_id, source_di_document_id, "
             "source_evidence_id, document_type_key, certificate_variant, certificate_number, "
             "old_vehicle_registration_number, reviewed_by_actor_id) VALUES (:t, :j, :d, :e, "
             "'scrappage_certificate_of_deposit', 'TRANSFERRED', 'COD/2026/0001', 'DL 3C AB 1234', 'tester')",
          d=certificate, e=evidence)
    _exec(s, "INSERT INTO auditcore.discount_applications (tenant_id, journey_id, discount_key, "
             "standard_eligible_amount, actual_discount_amount, eligibility_result, actual_source_kind) "
             "VALUES (:t, :j, 'CASH_DISCOUNT', 20000, 32000, 'ELIGIBLE', 'EVIDENCE')")
    verdict = [{"key": "CASH_DISCOUNT", "label": "Consumer offer / scheme discount", "status": insights.OVER,
                "entitled": "20000", "invoice": "32000.00", "variance": "12000.00"}]
    _exec(s, "INSERT INTO auditcore.p2_journey_runtime (tenant_id, journey_id, current_stage, discount_variance) "
             "VALUES (:t, :j, 'DELIVERY_DOCUMENT_UPLOAD', CAST(:d AS jsonb))", d=json.dumps(verdict))
    _exec(s, "INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code, business_status) "
             "VALUES (:t, :j, 'DELIVERY', 'DELIVERY_STARTED')")
    # The same cash receipt uploaded twice, and a bank transfer.
    for number, amount, mode in (("R-77", 250_000, "CASH"), ("R-77", 250_000, "CASH"), ("R-78", 1_000_000, "RTGS")):
        receipt = uuid4()
        add_evidence(s.journey, di_document_id=receipt, document_type_key="payment_receipt")
        _exec(s, "INSERT INTO auditcore.payments (tenant_id, journey_id, amount, payment_method_code, payment_mode_code, "
                 "receipt_number, receipt_date, status_source, source_di_document_id) "
                 "VALUES (:t, :j, :a, :m, :m, :n, CURRENT_DATE - 2, 'EVIDENCE', :d)", a=amount, m=mode, n=number, d=receipt)
    form = uuid4()
    add_evidence(s.journey, di_document_id=form, document_type_key="booking_form")
    for key, value in (("address_pincode", "758001"), ("address_district", "KENDUJHAR"),
                       ("finance_sanctioned_amount", "13,50,000.00"), ("policy_end_date", "04/10/2027")):
        add_extracted_field(s.journey, di_document_id=form, field_key=key, value=value)


def test_the_facts_are_read_from_the_documents_on_file(setup):
    s = setup
    _seed_deal(s)
    fact = _facts(s)[str(s.journey.journey_id)]
    assert fact["statement"] and fact["dealSheet"] and fact["exShowroom"] == 1_600_000.0
    # The statement's charge, not the booking form's quote.
    assert fact["charged"]["rsa_amount"] == 2021.0 and fact["rto"]["road_tax_amount"] == 120_000.0
    assert fact["channel"] == insights.INHOUSE_STATEMENT and fact["policy"]["covers"] == ["Zero Depreciation", "Engine Protect"]
    assert fact["finance"]["lender"] == "State Bank of India" and fact["sanctioned"] == 1_350_000.0
    assert fact["loan"] == 1_350_000.0                                  # the sanction, before the disbursal
    assert [c["state"] for c in fact["certificates"]] == ["Delhi"] and fact["certificates"][0]["kind"] == "TRANSFERRED"
    assert (fact["pin"], fact["district"]) == ("758001", "Kendujhar") and fact["policyEnd"] == date(2027, 10, 4)
    assert fact["discounts"] == [{"key": "CASH_DISCOUNT", "amount": 32000.0}]
    assert fact["verdicts"][0]["status"] == insights.OVER and fact["verdicts"][0]["variance"] == 12000.0
    assert fact["model"] is None and fact["bookingDateRead"] and fact["deliveryStarted"]
    # The receipt uploaded twice is one receipt.
    assert sorted((r["amount"], r["mode"]) for r in fact["receipts"]) == [(250_000.0, "CASH"), (1_000_000.0, "RTGS")]


def test_a_document_no_longer_on_file_gives_no_figure(setup):
    s = setup
    j = s.journey.journey_id
    voided = uuid4()
    evidence = add_evidence(s.journey, di_document_id=voided, document_type_key="customer_ledger",
                            process_area="DELIVERY", status="SUPERSEDED")
    _source(s, j, "customer_ledger", "rsa_amount", 2021, evidence=evidence, document=voided)
    fact = _facts(s)[str(j)]
    assert not fact["statement"] and fact["charged"] == {}
    # A reading on a superseded copy is not the customer's PIN either.
    add_extracted_field(s.journey, di_document_id=voided, field_key="address_pincode", value="753001",
                        document_type="customer_ledger")
    assert _facts(s)[str(j)]["pin"] is None
    # A cost sheet prices the deal, and prints no RSA row: billed, but no statement deal.
    _source(s, j, "cost_sheet", "ex_showroom_price", 900_000)
    fact = _facts(s)[str(j)]
    assert fact["dealSheet"] and not fact["statement"] and fact["exShowroom"] == 900_000.0


def test_a_cancelled_booking_is_no_deal_and_a_missing_booking_date_falls_to_the_opening_day(setup):
    s = setup
    other = _second_journey(s)
    _exec(s, "INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code, business_status) "
             "VALUES (:t, :o, 'BOOKING', 'BOOKING_CANCELLED')", o=other)
    facts = _facts(s)
    assert list(facts) == [str(s.journey.journey_id)]
    opened = _exec(s, "SELECT (created_at_utc AT TIME ZONE 'Asia/Kolkata')::date FROM auditcore.journeys "
                      "WHERE tenant_id=:t AND journey_id=:j").scalar_one()
    assert facts[str(s.journey.journey_id)]["bookingDate"] == opened


def test_a_premium_on_a_dealer_invoice_is_in_house(setup):
    s = setup
    _source(s, s.journey.journey_id, "customer_ledger", "ex_showroom_price", 1_000_000)
    _exec(s, "INSERT INTO auditcore.insurance_records (tenant_id, journey_id, insurer_name, policy_reference, "
             "actual_premium_amount, agent_intermediary_code, source_kind) "
             "VALUES (:t, :j, 'ICICI Lombard', 'POL-9', 30000, 'PS000041087', 'EVIDENCE')")
    assert _facts(s)[str(s.journey.journey_id)]["channel"] == insights.OUTSIDE_UNCONFIRMED
    invoice = uuid4()
    add_evidence(s.journey, di_document_id=invoice, document_type_key="tax_invoice_tally", process_area="DELIVERY")
    add_extracted_field(s.journey, di_document_id=invoice, field_key="insurance_amount", value="31500",
                        document_type="tax_invoice_tally")
    fact = _facts(s)[str(s.journey.journey_id)]
    assert (fact["channel"], fact["invoicePremium"]) == (insights.INHOUSE_INVOICE, 31500.0)
    # The dealer's own code, once confirmed, decides before the invoice is consulted.
    _exec(s, "INSERT INTO auditcore.dealer_insurance_codes (tenant_id, dealer_id, normalized_code, code_as_entered, "
             "confirmed_by_actor_id) VALUES (:t, :d, 'PS000041087', 'PS000041087', 'tester')", d=s.journey.dealer_id)
    assert _facts(s)[str(s.journey.journey_id)]["channel"] == insights.INHOUSE_DEALER_CODE


def test_the_reports_are_for_a_team_lead_and_above_over_their_own_outlets(setup):
    s = setup
    _seed_deal(s)
    assert s.client.get(f"{s.base}/penetration").status_code == 403           # the PC
    lead = f"lead-{uuid4().hex[:8]}"
    _exec(s, "INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
             "dealer_id, outlet_id) VALUES (:t, :a, 'TL', :d, :o)", a=lead, d=s.journey.dealer_id,
          o=s.journey.outlet_id)
    s.acting["subject"] = lead
    assert s.client.get(f"{s.base}/turnover").status_code == 404              # no such report
    for report in insights.REPORTS:
        response = s.client.get(f"{s.base}/{report}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["report"] == report and body["notes"] and body["scope"]["projectWide"] is False
        assert [o["outletId"] for d in body["scope"]["dealers"] for o in d["outlets"]] == [str(s.journey.outlet_id)]

    penetration = s.client.get(f"{s.base}/penetration").json()
    assert penetration["basis"]["statementDeals"] == 1
    assert {key: cell["count"] for key, cell in penetration["network"]["products"].items()} == {
        "finance": 1, "insurance": 1, "accessories": 0, "fastag": 1, "rsa": 1, "extendedWarranty": 1, "scrappage": 1}
    assert penetration["network"]["products"]["rsa"]["amount"] == 2021
    assert penetration["districts"][0]["label"] == "Kendujhar"

    scrappage = s.client.get(f"{s.base}/scrappage").json()
    assert scrappage["summary"]["oldVehiclesOutsideState"] == 1 and scrappage["register"][0]["state"] == "Delhi"
    assert scrappage["register"][0]["roadTaxPercent"] == 7.5 and scrappage["register"][0]["chargedAbovePaid"] == 2000

    finance = s.client.get(f"{s.base}/finance").json()
    assert finance["summary"]["typicalLoan"] == 1_350_000 and finance["summary"]["typicalLoanToValue"] == 84.4
    assert finance["sanctionGaps"][0]["short"] == 50_000

    discounts = s.client.get(f"{s.base}/discounts").json()
    assert discounts["network"]["given"] == 32000 and discounts["network"]["givenPercent"] == 2.0
    assert discounts["network"]["verdicts"][insights.OVER]["amount"] == 12000

    payments = s.client.get(f"{s.base}/payments").json()
    assert payments["network"]["amount"] == 1_250_000 and payments["network"]["cashAboveLimit"] == 1
    assert payments["cashAboveLimit"][0]["receiptNumber"] == "R-77"

    sales = s.client.get(f"{s.base}/sales").json()
    assert (sales["network"]["journeys"], sales["network"]["deliveryStarted"], sales["network"]["billedValue"]) == \
        (1, 1, 1_600_000)

    insurance = s.client.get(f"{s.base}/insurance").json()
    assert insurance["network"]["inhouse"] == 1 and insurance["network"]["inhousePremium"] == 52000
    assert insurance["renewals"] == [{"month": "2027-10", "policies": 1, "premium": 52000, "inhouse": 1, "outside": 0}]

    # A second outlet the Team Lead is not mapped to: its Journey is not theirs to read.
    elsewhere = _exec(s, "INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                         "VALUES (:t, :d, :c, 'Elsewhere') RETURNING outlet_id",
                      d=s.journey.dealer_id, c=f"E-{uuid4().hex[:6]}").scalar_one()
    hidden = _second_journey(s, outlet=elsewhere)
    _source(s, hidden, "customer_ledger", "rsa_amount", 2021)
    assert s.client.get(f"{s.base}/penetration").json()["basis"]["statementDeals"] == 1
    assert s.client.get(f"{s.base}/penetration", params={"outlet_id": str(elsewhere)}).status_code == 404
    # An administrator reads the whole Project.
    s.acting["role"] = "TENANT_ADMIN"
    wide = s.client.get(f"{s.base}/penetration").json()
    assert wide["scope"]["projectWide"] is True and wide["basis"]["statementDeals"] == 2
    assert s.client.get(f"{s.base}/penetration", params={"outlet_id": str(elsewhere)}).json()["basis"]["statementDeals"] == 1

    # The booking date filters; a range that ends before it starts is refused.
    booked = date.fromisoformat(s.client.get(f"{s.base}/catchment").json()["period"]["first"])
    assert s.client.get(f"{s.base}/catchment", params={"to": "2020-01-01"}).json()["basis"]["liveJourneys"] == 0
    assert s.client.get(f"{s.base}/catchment", params={"from": booked.isoformat()}).json()["basis"]["liveJourneys"] == 2
    assert s.client.get(f"{s.base}/catchment", params={"from": "2026-12-01", "to": "2026-11-01"}).status_code == 422
