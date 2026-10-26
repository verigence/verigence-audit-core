"""Mahindra Orissa: the dealer's account statement is the source of truth for
the Deal (2026-10-25).

The reader tests use the lines DI actually extracted from three real forms
(Aditya, Utkal, Shivnath); the amounts are those printed on the forms. The
database tests drive the whole path: policy, the withheld invoice, the rebuild,
the CRITICAL tasks and migration 0162's clean-up.
"""
from __future__ import annotations

import importlib.util
import json
import re
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_evidence,
    add_extracted_field,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import uc03_account_statement as module
from audit_core.db import set_tenant_context
from audit_core.uc03_account_statement import (
    KIND_FORM,
    KIND_TALLY,
    NO_EX_SHOWROOM_TASK,
    TALLY_DEVIATION_TASK,
    line_rows,
    read_statement,
    statement_kind,
    sync_account_statement,
    sync_if_source_of_truth,
    tally_deviations,
    tally_postings,
)
from audit_core.uc03_deal_source_history import (
    record_source_value,
    upsert_commercial_actual,
)
from audit_core.uc03_deal_source_policy import (
    POLICY_ACCOUNT_STATEMENT,
    POLICY_INVOICE,
    deal_source_policy,
    is_invoice_type,
)

# ── the real forms ───────────────────────────────────────────────────────────
UTKAL = [  # UTK-JI-261019-004
    {"particulars_raw": "vehicle cost", "debit_amount": 1039000}, {"particulars_raw": "TCS", "debit_amount": 10390},
    {"particulars_raw": "Insurance amount", "debit_amount": 38201},
    {"particulars_raw": "Registration", "debit_amount": 106139},
    {"particulars_raw": "Accessories", "debit_amount": 30000},
    {"particulars_raw": "Fastag charges", "debit_amount": 600},
    {"particulars_raw": "Shield warranty", "debit_amount": 9999}, {"particulars_raw": "RSA", "debit_amount": 2021},
    {"particulars_raw": "Finance D/O", "entry_date": "2024-06-08", "credit_amount": 1060000},
    {"particulars_raw": "MR No. CP M-01886", "entry_date": "2024-06-09", "credit_amount": 50000},
    {"particulars_raw": "MR No. PR M-4453", "entry_date": "2024-06-10", "credit_amount": 1000},
    {"particulars_raw": "MR No. CB M-4455", "entry_date": "2024-06-10", "credit_amount": 100000},
    {"particulars_raw": "MR No. DD M-4265", "entry_date": "2024-06-05", "credit_amount": 21000},
    {"particulars_raw": "Other Discount (if any)", "credit_amount": 5000},
]
SHIVNATH = [  # SHI-KE-261019-002
    {"particulars_raw": "Ex Showroom", "debit_amount": 1797001}, {"particulars_raw": "Registration", "debit_amount": 27912},
    {"particulars_raw": "Insurance", "debit_amount": 102472}, {"particulars_raw": "RSA", "debit_amount": 2021},
    {"particulars_raw": "Accessories", "debit_amount": 26361}, {"particulars_raw": "TCS", "debit_amount": 17970},
    {"particulars_raw": "Fast Tag", "debit_amount": 500}, {"particulars_raw": "Cash", "credit_amount": 200000},
    {"particulars_raw": "Online transfer", "credit_amount": 11000}, {"particulars_raw": "Bank DO", "credit_amount": 1700000},
    {"particulars_raw": "Trade in value", "credit_amount": 53155},
]
ADITYA = [  # ADI-BA-261019-004
    {"particulars_raw": "EX-SHOW ROOM PRICE OF VEHICLE", "debit_amount": 1030100},
    {"particulars_raw": "TCS ON INVOICE", "debit_amount": 10300},
    {"particulars_raw": "INSURANCE COST (NIL DEP)", "debit_amount": 40148},
    {"particulars_raw": "REGISTRATION COST", "debit_amount": 103739}, {"particulars_raw": "RSA", "debit_amount": 2021},
    {"particulars_raw": "BOOKING AMOUNT", "credit_amount": 1189789}, {"particulars_raw": "CASH DISCOUNT", "credit_amount": 3000},
]
ADITYA_NO_VEHICLE = [  # ADI-SJ-261023-012: receipts and a cash discount, no vehicle line
    {"entry_date": "2026-10-10", "particulars_raw": "BOOKING AMOUNT", "voucher_type": "QR", "voucher_number": "05675",
     "credit_amount": 1001},
    {"particulars_raw": "FINANCER AMOUNT IndusInd", "credit_amount": 930990},
    {"entry_date": "2026-10-12", "particulars_raw": "DEPOSITED INTO BANK", "voucher_type": "QR", "voucher_number": "05698",
     "credit_amount": 44000},
    {"particulars_raw": "**CASH DISCOUNT Edline", "credit_amount": 4000},
]
TALLY = [  # ADI-BA-261019-003, the Tally printout (vehicle debit = invoice total)
    {"entry_date": "2026-09-24", "particulars_raw": "Sales Accessories@18%", "voucher_type": "SALES WORKSHOP",
     "voucher_number": "C1R27A00002328", "debit_amount": 14000, "running_balance": "7,000.00 Cr"},
    {"entry_date": "2026-09-25", "particulars_raw": "Sales BOLERO NEO", "voucher_type": "Vehicle Sales_ Mahindra",
     "voucher_number": "INV27A002879", "debit_amount": 1040300, "running_balance": "10,34,300.00 Dr"},
    {"entry_date": "2026-09-25", "particulars_raw": "Canara Bank (Financer)", "voucher_type": "Bank Finance DO (BDO)",
     "voucher_number": "02178", "credit_amount": 1210036, "running_balance": "1,75,736.00 Cr"},
]
D = Decimal


# ── the reader (no database) ─────────────────────────────────────────────────
def test_utkal_form_every_line_is_mapped_none_guessed() -> None:
    read = read_statement(UTKAL)
    assert read.kind == KIND_FORM
    assert read.components == {
        "ex_showroom_price": D(1039000), "tcs_amount": D(10390), "insurance_amount": D(38201),
        "registration_charges": D(106139), "accessories_cost": D(30000), "fastag_amount": D(600),
        "additional_warranty_amount": D(9999), "rsa_amount": D(2021),
    }
    assert read.discounts == {"ADDITIONAL_DISCOUNT": D(5000)}
    assert read.financer == D(1060000)
    assert read.unmapped == [] and read.conflicts == []


def test_shivnath_form() -> None:
    read = read_statement(SHIVNATH)
    assert read.kind == KIND_FORM
    assert read.components == {
        "ex_showroom_price": D(1797001), "registration_charges": D(27912), "insurance_amount": D(102472),
        "rsa_amount": D(2021), "accessories_cost": D(26361), "tcs_amount": D(17970), "fastag_amount": D(500),
    }
    assert read.discounts == {} and read.financer == D(1700000)


def test_aditya_form_with_cash_discount() -> None:
    read = read_statement(ADITYA)
    assert read.kind == KIND_FORM
    assert read.components["ex_showroom_price"] == D(1030100)
    assert read.components["tcs_amount"] == D(10300)
    assert read.components["insurance_amount"] == D(40148)
    assert read.discounts == {"ADDITIONAL_DISCOUNT": D(3000)}
    assert read.receipts == D(1189789)


def test_form_without_a_vehicle_line_gives_no_ex_showroom() -> None:
    read = read_statement(ADITYA_NO_VEHICLE)
    assert "ex_showroom_price" not in read.components and read.components == {}
    assert read.discounts == {"ADDITIONAL_DISCOUNT": D(4000)}  # "**CASH DISCOUNT Edline"
    assert read.financer == D(930990)


@pytest.mark.parametrize("label", ["SHIELD WARANTY TCS", "FASTAG CHARGESRSA", "HANDLING CHARGES TCS"])
def test_two_items_merged_by_the_handwriting_reading_are_left_unmapped(label: str) -> None:
    read = read_statement([{"particulars_raw": "Ex Showroom", "debit_amount": 1000000},
                           {"particulars_raw": label, "debit_amount": 12345}])
    assert read.components == {"ex_showroom_price": D(1000000)}
    assert [u["label"] for u in read.unmapped] == [label]


def test_scrappage_is_never_filed_under_dealer_or_certificate() -> None:
    read = read_statement([{"particulars_raw": "Ex Showroom", "debit_amount": 1000000},
                           {"particulars_raw": "Scrappage bonus", "credit_amount": 25000}])
    assert read.discounts == {}
    assert read.unmapped and "scrappage" in read.unmapped[0]["why"]


def test_two_different_amounts_for_one_line_are_a_conflict_and_nothing_is_written() -> None:
    read = read_statement([{"particulars_raw": "Ex Showroom", "debit_amount": 1000000},
                           {"particulars_raw": "EX-SHOWROOM", "debit_amount": 1010000}])
    assert "ex_showroom_price" not in read.components
    assert read.conflicts == [{"kind": "COMMERCIAL", "key": "ex_showroom_price", "amounts": [D(1000000), D(1010000)]}]


def test_the_same_amount_twice_is_one_line_not_a_conflict() -> None:
    read = read_statement([{"particulars_raw": "TCS", "debit_amount": 100}, {"particulars_raw": "TCS", "debit_amount": 100}])
    assert read.components == {"tcs_amount": D(100)} and read.conflicts == []


def test_zero_and_empty_lines_are_not_figures() -> None:
    read = read_statement([{"particulars_raw": "Fast Tag", "debit_amount": 0}, {"particulars_raw": "", "debit_amount": 5},
                           {"particulars_raw": "RSA"}])
    assert read.components == {} and read.unmapped == []


def test_line_items_stored_as_python_repr_are_read() -> None:
    assert line_rows(repr(ADITYA))[0]["particulars_raw"] == "EX-SHOW ROOM PRICE OF VEHICLE"
    assert line_rows(json.dumps(ADITYA)) == ADITYA
    assert line_rows("not a list") == [] and line_rows(None) == []


def test_tally_printout_is_recognised_and_is_not_a_form() -> None:
    assert statement_kind(TALLY) == KIND_TALLY
    assert statement_kind(ADITYA) == KIND_FORM


# ── Tally validates the statement (no database) ──────────────────────────────
def test_tally_vehicle_posting_is_the_invoice_total_and_agrees_with_the_statement() -> None:
    statement = read_statement(ADITYA)  # 1,030,100 + TCS 10,300 = 1,040,400; less 3,000 discount = 1,037,400
    assert tally_postings(TALLY) == {"vehicle": D(1040300), "financer": D(1210036)}
    # 1,040,300 is within Rs 100 of ex-showroom + TCS (1,040,400): no deviation
    assert tally_deviations(statement, tally_postings(TALLY)) == []


def test_tally_that_disagrees_by_more_than_rs_100_is_a_deviation() -> None:
    statement = read_statement(ADITYA)
    deviations = tally_deviations(statement, {"vehicle": D(1050000)})
    assert [d["line"] for d in deviations] == ["vehicle"]


def test_tally_within_the_rs_100_tolerance_is_not_a_deviation() -> None:
    statement = read_statement(SHIVNATH)
    assert tally_deviations(statement, {"vehicle": D(1797001) + D(100)}) == []
    assert [d["line"] for d in tally_deviations(statement, {"vehicle": D(1797001) + D(101) + D(17970)})] == ["vehicle"]


def test_tally_financer_and_insurance_are_compared() -> None:
    statement = read_statement(UTKAL)
    deviations = tally_deviations(statement, {"financer": D(1070000), "insurance": D(38201)})
    assert [d["line"] for d in deviations] == ["financer"]


def test_every_pattern_is_matched_against_the_whole_label() -> None:
    # a label that only starts like a charge must not be taken for it
    read = read_statement([{"particulars_raw": "Insurance refund pending", "debit_amount": 500}])
    assert read.components == {} and len(read.unmapped) == 1


def test_no_string_formatting_in_the_new_sql() -> None:
    source = Path(module.__file__).read_text()
    assert not re.search(r"text\(\s*f[\"']", source), "f-string SQL in uc03_account_statement"


def test_invoice_types_are_recognised_and_the_statement_is_not_one() -> None:
    assert is_invoice_type("Tax-Invoice Tally") and is_invoice_type("customer_invoice_dms_v2")
    assert not is_invoice_type("customer_ledger") and not is_invoice_type("insurance_cover_note")


# ── the database ─────────────────────────────────────────────────────────────
@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="acst")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _set_policy(journey, policy: str) -> None:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.projects SET deal_source_policy=:p WHERE tenant_id=:t"),
                  {"p": policy, "t": journey.tenant_id})


def _ledger(journey, lines: list[dict]) -> tuple:
    document_id = uuid4()
    evidence_id = add_evidence(journey, di_document_id=document_id, document_type_key="customer_ledger",
                               process_area="DELIVERY")
    add_extracted_field(journey, di_document_id=document_id, field_key="line_items", value=lines,
                        document_type="customer_ledger")
    return document_id, evidence_id


def _sync(journey):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return sync_account_statement(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _lines(journey) -> dict[str, tuple]:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        rows = c.execute(
            text("SELECT component_key, actual_amount, source_reference FROM auditcore.commercial_lines "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id}).all()
    return {r[0]: (r[1], r[2]) for r in rows}


def _discounts(journey) -> dict[str, tuple]:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        rows = c.execute(
            text("SELECT discount_key, actual_discount_amount, details->>'origin' FROM auditcore.discount_applications "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id}).all()
    return {r[0]: (r[1], r[2]) for r in rows}


def _open_tasks(journey) -> dict[str, str]:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        rows = c.execute(
            text("SELECT task_type, severity FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j "
                 "AND task_type = ANY(:types) AND task_status NOT IN ('VERIFIED_COMPLETE', 'CANCELLED', 'FAILED', 'DEAD_LETTER')"),
            {"t": journey.tenant_id, "j": journey.journey_id,
             "types": [NO_EX_SHOWROOM_TASK, TALLY_DEVIATION_TASK]}).all()
    return {r[0]: r[1] for r in rows}


def _invoice_sets_the_deal(journey, *, ex_showroom: str = "999999") -> None:
    """What an invoice did to the Deal before the rule: a canonical actual and a source row."""
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(
            text("INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, standard_amount, "
                 "actual_amount, actual_source_kind, source_reference) VALUES (:t, :j, 'ex_showroom_price', 1000000, "
                 ":a, 'EVIDENCE', :r)"),
            {"t": journey.tenant_id, "j": journey.journey_id, "a": ex_showroom, "r": f"tax_invoice_tally:{uuid4()}"})
        c.execute(
            text("INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind, component_key, "
                 "source_document_type, amount) VALUES (:t, :j, 'COMMERCIAL', 'ex_showroom_price', 'tax_invoice_tally', :a)"),
            {"t": journey.tenant_id, "j": journey.journey_id, "a": ex_showroom})
        c.execute(
            text("INSERT INTO auditcore.discount_applications (tenant_id, journey_id, discount_key, "
                 "actual_discount_amount, actual_source_kind, details) VALUES (:t, :j, 'CASH_DISCOUNT', 7000, "
                 "'EVIDENCE', CAST(:d AS jsonb))"),
            {"t": journey.tenant_id, "j": journey.journey_id, "d": json.dumps({"origin": "INVOICE_MATERIALIZATION"})})


def test_every_project_defaults_to_the_invoice(journey) -> None:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        assert deal_source_policy(c, tenant_id=journey.tenant_id) == POLICY_INVOICE
        assert sync_if_source_of_truth(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id) is None
        # unchanged behaviour: an invoice may write the Deal
        assert upsert_commercial_actual(
            c, tenant_id=journey.tenant_id, journey_id=journey.journey_id, component_key="tcs_amount",
            amount=D(100), document_type="tax_invoice_tally", document_id=uuid4(), evidence_id=None, record=False)


def test_policy_column_only_allows_the_two_values(journey) -> None:
    with pytest.raises(Exception, match="ck_projects_deal_source_policy"):
        _set_policy(journey, "TALLY")


def test_the_invoice_cannot_write_the_deal_when_the_statement_is_the_source(journey) -> None:
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        assert not upsert_commercial_actual(
            c, tenant_id=journey.tenant_id, journey_id=journey.journey_id, component_key="tcs_amount",
            amount=D(100), document_type="tax_invoice_tally", document_id=uuid4(), evidence_id=None, record=False)
        record_source_value(
            c, tenant_id=journey.tenant_id, journey_id=journey.journey_id, line_kind="COMMERCIAL",
            component_key="tcs_amount", source_document_type="tax_invoice_tally", amount=D(100))
        count = c.execute(text("SELECT count(*) FROM auditcore.commercial_line_source_values WHERE tenant_id=:t"),
                          {"t": journey.tenant_id}).scalar_one()
    assert count == 0 and _lines(journey) == {}


def test_statement_replaces_the_invoice_data_and_is_idempotent(journey) -> None:
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _invoice_sets_the_deal(journey)
    document_id, _ = _ledger(journey, UTKAL)
    first = _sync(journey)
    assert first["statements"] == 1 and first["commercialLines"] == 8 and first["discountApplications"] == 1
    lines = _lines(journey)
    assert lines["ex_showroom_price"] == (D(1039000), f"customer_ledger:{document_id}")
    assert lines["fastag_amount"][0] == D(600) and lines["additional_warranty_amount"][0] == D(9999)
    assert lines["ex_showroom_price"][0] != D(999999)  # the invoice's figure is gone
    # the invoice's discount (7,000) is gone; the statement's Other Discount stands
    assert _discounts(journey) == {"CASH_DISCOUNT": (None, "INVOICE_MATERIALIZATION"),
                                   "ADDITIONAL_DISCOUNT": (D(5000), "ACCOUNT_STATEMENT")}
    second = _sync(journey)
    assert _lines(journey) == lines
    assert second["commercialLines"] == 0 or second["commercialLines"] == first["commercialLines"]
    assert _open_tasks(journey) == {}


def test_no_statement_leaves_the_invoice_blank(journey) -> None:
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _invoice_sets_the_deal(journey)
    result = _sync(journey)
    assert result["statements"] == 0 and result["commercialLines"] == 0
    assert _lines(journey)["ex_showroom_price"][0] is None  # blank, the row (and its standard) stays
    assert _discounts(journey)["CASH_DISCOUNT"][0] is None
    assert _open_tasks(journey) == {}  # nothing on file is not a reading failure


def test_statement_with_no_vehicle_line_raises_a_critical_task_and_clears_when_fixed(journey) -> None:
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _ledger(journey, ADITYA_NO_VEHICLE)
    _sync(journey)
    assert _open_tasks(journey) == {NO_EX_SHOWROOM_TASK: "CRITICAL"}
    assert "ex_showroom_price" not in {k for k, v in _lines(journey).items() if v[0] is not None}
    # the PC corrects the reading: a vehicle line appears
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.journey_document_extracted_fields SET effective_value=CAST(:v AS jsonb) "
                       "WHERE tenant_id=:t AND journey_id=:j AND field_key='line_items'"),
                  {"v": json.dumps([{"particulars_raw": "Ex Showroom", "debit_amount": 1000000}] + ADITYA_NO_VEHICLE),
                   "t": journey.tenant_id, "j": journey.journey_id})
    _sync(journey)
    assert _open_tasks(journey) == {}
    assert _lines(journey)["ex_showroom_price"][0] == D(1000000)


def test_tally_that_disagrees_raises_a_critical_task_and_one_that_agrees_does_not(journey) -> None:
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _ledger(journey, ADITYA)
    _ledger(journey, TALLY)
    result = _sync(journey)
    assert result["tallies"] == 1 and result["tallyDeviations"] == 0 and _open_tasks(journey) == {}
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.journey_document_extracted_fields SET effective_value=CAST(:v AS jsonb) "
                       "WHERE tenant_id=:t AND journey_id=:j AND field_key='line_items' AND effective_value::text "
                       "LIKE '%1040300%'"),
                  {"v": json.dumps([{**TALLY[1], "debit_amount": 1090000}, TALLY[2]]),
                   "t": journey.tenant_id, "j": journey.journey_id})
    result = _sync(journey)
    assert result["tallyDeviations"] == 1
    assert _open_tasks(journey) == {TALLY_DEVIATION_TASK: "CRITICAL"}
    # the Tally never writes the Deal: the statement's figure stands
    assert _lines(journey)["ex_showroom_price"][0] == D(1030100)


def test_migration_0162_snapshots_then_clears_only_the_invoice_figures(journey) -> None:
    path = Path(__file__).resolve().parent.parent / "migrations" / "versions" / "0162_deal_source_policy.py"
    spec = importlib.util.spec_from_file_location("m0162", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    _invoice_sets_the_deal(journey)
    document_id, evidence_id = _ledger(journey, UTKAL)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(  # a statement figure and an insurance figure that must survive
            text("INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind, component_key, "
                 "source_document_type, amount, source_evidence_id, source_document_id) VALUES (:t, :j, 'COMMERCIAL', "
                 "'ex_showroom_price', 'customer_ledger', 1039000, :e, :d)"),
            {"t": journey.tenant_id, "j": journey.journey_id, "e": evidence_id, "d": document_id})
        migration._clear_invoice_values(c, journey.tenant_id)
        snapshot = dict(c.execute(
            text("SELECT table_name, count(*) FROM auditcore.deal_source_snapshots WHERE tenant_id=:t GROUP BY 1"),
            {"t": journey.tenant_id}).all())
        remaining = c.execute(
            text("SELECT source_document_type FROM auditcore.commercial_line_source_values WHERE tenant_id=:t"),
            {"t": journey.tenant_id}).scalars().all()
        again = migration._clear_invoice_values(c, journey.tenant_id)
        snapshot_after = c.execute(text("SELECT count(*) FROM auditcore.deal_source_snapshots WHERE tenant_id=:t"),
                                   {"t": journey.tenant_id}).scalar_one()
    assert snapshot == {"commercial_line_source_values": 1, "commercial_lines": 1, "discount_applications": 1}
    assert remaining == ["customer_ledger"]
    assert _lines(journey)["ex_showroom_price"] == (None, None)
    assert again is None
    # a second run finds nothing left to snapshot that the first did not already clear
    assert snapshot_after == 3 + 0


# ── what the Deal screen shows ───────────────────────────────────────────────
def _deal_rows(journey) -> dict[str, dict]:
    from audit_core.uc03_p2_journey360 import deal

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        view = deal(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    rows: dict[str, dict] = {}
    for group in view["categories"]:
        for item in group["components"]:
            rows[item["key"]] = item
    return rows


def test_deal_shows_the_statement_not_the_invoice(journey) -> None:
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _invoice_sets_the_deal(journey, ex_showroom="999999")
    _ledger(journey, UTKAL)
    _sync(journey)
    # an invoice row that slipped in some other way (a legacy write) is not shown either
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(
            text("INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind, component_key, "
                 "source_document_type, amount) VALUES (:t, :j, 'COMMERCIAL', 'tcs_amount', 'tax_invoice_tally', 55555)"),
            {"t": journey.tenant_id, "j": journey.journey_id})
    rows = _deal_rows(journey)
    assert D(str(rows["ex_showroom_price"]["billed"])) == D(1039000)
    assert D(str(rows["tcs_amount"]["billed"])) == D(10390)  # the statement's, not the invoice's 55,555
    assert D(str(rows["fastag_amount"]["billed"])) == D(600)


def test_deal_with_no_statement_has_a_blank_invoice_even_if_an_invoice_is_on_file(journey) -> None:
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _invoice_sets_the_deal(journey, ex_showroom="999999")
    _sync(journey)
    rows = _deal_rows(journey)
    assert rows["ex_showroom_price"]["billed"] is None


def test_deal_of_a_project_on_the_invoice_policy_is_unchanged(journey) -> None:
    _invoice_sets_the_deal(journey, ex_showroom="999999")
    rows = _deal_rows(journey)
    assert D(str(rows["ex_showroom_price"]["billed"])) == D(999999)


# ── the rebuild job ──────────────────────────────────────────────────────────
def test_rebuild_reports_a_statement_without_a_vehicle_line_as_no_ex_showroom(journey) -> None:
    """The job's summary counts these (no_ex_showroom); the figure used to read
    "None" as text and so never counted."""
    from audit_core.uc03_account_statement_rebuild import rebuild_journey

    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _ledger(journey, ADITYA_NO_VEHICLE)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        item = rebuild_journey(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id, tag="t", commit=False)
    assert item["statements"] == 1 and item["exShowroom"] is None


def test_rebuild_dry_run_writes_nothing_and_commit_sets_the_deal(journey) -> None:
    from audit_core.uc03_account_statement_rebuild import rebuild_journey

    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _invoice_sets_the_deal(journey)
    with journey.engine.begin() as c:  # migration 0162 had already removed the per-invoice source rows
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("DELETE FROM auditcore.commercial_line_source_values WHERE tenant_id=:t"), {"t": journey.tenant_id})
    _ledger(journey, SHIVNATH)

    def run(commit: bool):
        with journey.engine.begin() as c:
            set_tenant_context(c, journey.tenant_id)
            return rebuild_journey(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                   tag="t", commit=commit)

    dry = run(False)
    assert dry["exShowroom"] == "1797001" and dry["invoiceHeldBefore"] == 1
    assert _lines(journey)["ex_showroom_price"][0] == D(999999)  # untouched
    done = run(True)
    assert done["invoiceHeldAfter"] == 0
    assert _lines(journey)["ex_showroom_price"][0] == D(1797001)
    assert run(True)["invoiceHeldBefore"] == 0  # a second run has nothing left to clear
