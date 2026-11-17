"""The statement re-read experiment's Audit Core side (2026-11-17): a reading
is judged as the Deal page judges it, the sample is fixed and in proportion,
and the report counts verdicts without carrying any value."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "statement_reread_experiment"))

import ac_side

# Utkal's Vehicle Account Statement (test_statement_totals' figures).
UTKAL = [
    {"particulars_raw": "Vehicle cost", "debit_amount": "852000"},
    {"particulars_raw": "Registration", "debit_amount": "3685"},
    {"particulars_raw": "Permit", "debit_amount": "6500"},
    {"particulars_raw": "Accessories", "debit_amount": "5140"},
    {"particulars_raw": "RSA", "debit_amount": "940"},
    {"particulars_raw": "Finance D/O KJR/2627/133", "credit_amount": "741090"},
    {"particulars_raw": "MR No KRM/2627/3592", "credit_amount": "11000"},
    {"particulars_raw": "MR No KRM/2627/3677", "credit_amount": "100000"},
    {"particulars_raw": "Scheme Discount", "credit_amount": "13000"},
    {"particulars_raw": "Other Discount (if any)", "credit_amount": "7000"},
]
PRINTED = {"total_cost": "868265", "total_receipts": "872090"}


def _fields(rows, **printed):
    return {"line_items": json.dumps(rows), **printed}


def _misread():
    rows = [dict(r) for r in UTKAL]
    rows[0]["debit_amount"] = "825000"
    return _fields(rows, **PRINTED)


def test_a_reading_is_judged_as_the_deal_page_judges_it():
    good = ac_side.judge(_fields(UTKAL, **PRINTED))
    assert (good["verdict"], good["error"], good["rows"], good["totals"]) == (ac_side.PASS, "0", 10, 2)
    bad = ac_side.judge(_misread())
    assert (bad["verdict"], bad["error"]) == (ac_side.FAIL, "27000")
    # empty values are the same as a field not read, as in load_ledger_documents
    assert ac_side.judge({**_fields(UTKAL, **PRINTED), "net_value": "null", "gross_value": ""})["verdict"] == ac_side.PASS


def test_a_thousands_comma_read_as_a_one_is_put_back_before_judging():
    rows = [dict(r) for r in UTKAL]
    rows[9]["credit_amount"] = "71000"  # "7,000" read with its comma as a 1
    judged = ac_side.judge(_fields(rows, **PRINTED))
    assert (judged["verdict"], judged["commaRepairs"]) == (ac_side.PASS, 1)


def test_the_parts_of_the_sample():
    assert ac_side.stratum(ac_side.judge(_fields(UTKAL, **PRINTED))) == ac_side.PASS
    assert ac_side.stratum(ac_side.judge(_misread())) == ac_side.FAIL
    totals_only = ac_side.judge(dict(PRINTED))
    assert (totals_only["verdict"], ac_side.stratum(totals_only)) == (ac_side.NOT_JUDGED, ac_side.TOTALS_ONLY)
    assert ac_side.stratum(ac_side.judge({"statement_title": "x"})) is None


def test_the_sample_is_fixed_and_in_proportion_to_each_dealer():
    population = [{"documentId": f"{dealer}-{n:03d}", "dealer": dealer, "stratum": ac_side.FAIL}
                  for dealer, count in (("aaa", 60), ("bbb", 30), ("ccc", 10)) for n in range(count)]
    population += [{"documentId": f"p-{n}", "dealer": "aaa", "stratum": ac_side.PASS} for n in range(5)]
    sizes = {ac_side.FAIL: 20, ac_side.PASS: 40, ac_side.TOTALS_ONLY: 30}
    sample = ac_side.pick(population, sizes)
    by_dealer = {dealer: sum(1 for i in sample if i["stratum"] == ac_side.FAIL and i["dealer"] == dealer)
                 for dealer in ("aaa", "bbb", "ccc")}
    assert by_dealer == {"aaa": 12, "bbb": 6, "ccc": 2}
    assert sum(1 for i in sample if i["stratum"] == ac_side.PASS) == 5  # no more than there are
    assert [i["documentId"] for i in ac_side.pick(list(reversed(population)), sizes)] == [i["documentId"] for i in sample]


def test_the_report_counts_verdicts_and_carries_no_value():
    sample = [
        {"documentId": "d1", "dealer": "aaa", "stratum": ac_side.FAIL, "stored": ac_side.judge(_misread())},
        {"documentId": "d2", "dealer": "aaa", "stratum": ac_side.FAIL, "stored": ac_side.judge(_misread())},
        {"documentId": "d3", "dealer": "bbb", "stratum": ac_side.PASS, "stored": ac_side.judge(_fields(UTKAL, **PRINTED))},
    ]
    usage = {"promptTokens": 1000, "billedOutputTokens": 500, "thoughtsTokens": 100, "costUsd": 0.002}

    def reading(document, variant, fields=None, **more):
        return {"documentId": document, "variant": variant, "ok": fields is not None, "fields": fields,
                "usage": usage, "latencyMs": 900.0, **more}

    good, bad = _fields(UTKAL, **PRINTED), _misread()
    readings = [
        reading("d1", ac_side.STORED, bad), reading("d2", ac_side.STORED, bad), reading("d3", ac_side.STORED, good),
        reading("d1", "rerun", bad), reading("d2", "rerun", good), reading("d3", "rerun", good),
        reading("d1", "res_high", good), reading("d2", "res_high", good), reading("d3", "res_high", bad),
        reading("d1", "pro", None, error="HTTP_503"), reading("unknown", "pro", good),
    ]
    result = ac_side.score(sample, readings)
    assert result["storedReadingCheck"] == {"documents": 3, "sameAsAuditCore": 3}
    fail = result["table"]["res_high"][ac_side.FAIL]
    assert (fail["documents"], fail[ac_side.PASS], fail["errorCloser"], fail["byDealer"]) == (
        2, 2, 2, {"aaa": {ac_side.PASS: 2}})
    assert result["table"]["res_high"][ac_side.PASS][ac_side.FAIL] == 1  # a good reading broken
    assert result["table"]["rerun"][ac_side.FAIL][ac_side.PASS] == 1
    assert result["againstRerun"]["res_high"] == {
        "addsUpHereOnly": 1, "addsUpRerunOnly": 1, "addsUpBoth": 1, "addsUpNeither": 0}
    assert result["table"]["pro"][ac_side.FAIL][ac_side.READ_FAILED] == 1
    assert result["spend"]["pro"]["errors"] == {"HTTP_503": 1}
    assert result["spend"]["rerun"]["costUsd"] == 0.006 and result["spend"]["rerun"]["calls"] == 3
    assert ac_side.STORED not in result["spend"]
    assert result["documents"]["d1"] == {
        "stratum": ac_side.FAIL, "dealer": "aaa", "pro": ac_side.READ_FAILED, "rerun": ac_side.FAIL,
        "res_high": ac_side.PASS, ac_side.STORED: ac_side.FAIL}
    printed = json.dumps(result)
    assert "852000" not in printed and "Vehicle cost" not in printed


def test_a_failure_is_reported_by_kind_and_place_only(tmp_path, capsys):
    secret = tmp_path / "sample.json"
    secret.write_text('{"documents": "not a list 852000"}')
    assert ac_side.main(["ac_side.py", "score", str(secret), str(tmp_path / "missing.jsonl")]) == 1
    out = capsys.readouterr().out
    assert out.startswith("::error::FileNotFoundError at ") and "852000" not in out and str(tmp_path) not in out
