"""NDC rows written FREE, misspelt rows, and rows left out told to the PC
(product owner 2026-11-22). Pure: no database. Rows as read off live Mahindra
Orissa account statements."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from audit_core.uc03_account_statement import (
    free_row_text,
    read_statement,
    spelled_right,
)
from audit_core.uc03_statement_rows_left_out import _FOR_A_PERSON, _POSTING, describe


def _read(*rows):
    lines = []
    for label, amount in rows:
        side = "credit_amount" if amount < 0 else "debit_amount"
        lines.append({"particulars_raw": label, side: abs(amount)})
    return read_statement(lines)


# ── misspelt words ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(("label", "right"), [
    ("ACCESORIES", "ACCESSORIES"),
    ("ACCESSOREIS", "ACCESSORIES"),
    ("EXCHANNGGE CLAIM", "EXCHANGE CLAIM"),
    ("EXCHANNGER CLAIM", "EXCHANGE CLAIM"),
    ("SCHME.", "SCHEME."),
    ("IF ANY OTHERS FITNERS", "IF ANY OTHERS FITNESS"),
    ("DEPOSITE INTO BANK", "DEPOSITED INTO BANK"),
])
def test_misspelt_words_are_put_right(label, right):
    assert spelled_right(label) == right


@pytest.mark.parametrize("label", [
    "FAST TAG STOCK IN 11.2 KW CHARGER",     # CHARGER is not CHARGES
    "EXCHANGE LOYALTY SCRAPPAGE BONDS",      # the certificate, not the bonus
    "SCORPIGE", "GRAPHINE", "OTHER TR", "RSA RECEIPTS",
])
def test_words_written_right_or_too_far_are_left_alone(label):
    assert spelled_right(label) == label


def test_a_misspelt_row_is_read_as_its_line_and_adds_to_another_row_of_it():
    # SHI-SS-261027-023: two accessory bills on the ledger, Rs 5,000 and Rs 8,380
    read = _read(("ACCESSORIES", 5000), ("Accesories", 8380))
    assert read.components == {"accessories_cost": Decimal(13380)}
    assert read.spelled == [{"label": "ACCESORIES", "as": "ACCESSORIES"}]
    assert not read.unmapped


def test_a_misspelt_exchange_claim_is_the_exchange_bonus():
    read = _read(("EXCHANNGGE CLAIM", -10000))
    assert read.discounts == {"EXCHANGE_BONUS": Decimal(10000)}


def test_a_row_still_unknown_once_corrected_is_left_under_its_own_words():
    read = _read(("IF ANY OTHERS FITNERS", 1000))
    assert [u["label"] for u in read.unmapped] == ["IF ANY OTHERS FITNERS"]


# ── rows written FREE ────────────────────────────────────────────────────────
@pytest.mark.parametrize(("label", "amount", "billed", "free"), [
    ("ACCESSORIES Free 5K", 140, 5140, 5000),             # ADI-RD-261111-009
    ("ACCESSORIES 5000 (FREE)", 5153, 10153, 5000),       # ADI-RD-261030-011
    ("ACCESSORIES FREE 30,000", 18724, 48724, 30000),     # ADI-RD-261104-007
    ("ACCESSORIES FREE 40.000", 3724, 43724, 40000),      # ADI-BM-261113-012
    ("ACCESSORIES 5000/ FOC", 4918, 9918, 5000),          # ADI-RD-261112-011
    ("ACCESSORIES Free 5000", 5000, 5000, 5000),          # the Offer Letter's "ACCESSORIES Free"
])
def test_accessories_written_free_are_billed_at_their_value_and_the_free_part_is_a_benefit(label, amount, billed, free):
    read = _read((label, amount))
    assert read.components == {"accessories_cost": Decimal(billed)}
    assert read.discounts == {"ACCESSORIES_KIT": Decimal(free)}
    assert read.free_rows[0]["free"] == Decimal(free)
    assert not read.unmapped


def test_accessories_written_free_with_no_figure_are_charged_as_written():
    read = _read(("ACCESSORIES FREE SALE", 5140))
    assert read.components == {"accessories_cost": Decimal(5140)} and not read.discounts
    assert read.free_rows[0]["free"] is None


def test_rsa_written_free_is_charged_beside_the_other_rsa_row():
    read = _read(("RSA Free", 5000), ("RSA", 140))  # ADI-RD-261111-014
    assert read.components == {"rsa_amount": Decimal(5140)} and not read.discounts


def test_a_product_written_free_as_a_minus_is_only_a_note():
    # SHI-YK-261103-002: "Extended Warranty Free" -60,000, left out of its own On Road Price
    read = read_statement([{"particulars_raw": "Extended Warranty Free", "debit_amount": -60000}])
    assert not read.components and not read.discounts and not read.unmapped
    assert read.free_rows == [{"key": "extended_warranty_amount", "label": "EXTENDED WARRANTY FREE",
                               "amount": Decimal(60000), "free": Decimal(60000), "side": "CR"}]


@pytest.mark.parametrize(("label", "amount"), [
    ("ACCESSORIES 5000 FREE", 156540),                    # another figure read on the row
    ("ACCESSORIES FREE - 50000 - 63977", 2021),           # figures that make no working
    ("ACCESSORIES-FREE-5000/- ACCESSORIES PENDING", 10000),  # not settled
    ("EXTENDED WARRANTY FREE - 17999/-", 140638),         # a product's figure not the amount
])
def test_free_rows_that_make_no_sense_stay_for_a_person(label, amount):
    read = _read((label, amount))
    assert not read.components and read.unmapped


def test_a_working_that_adds_up_is_still_read_as_before():
    read = _read(("ACCESSORIES 48398-18398 FREE", 30000))
    assert read.components == {"accessories_cost": Decimal(48398)}
    assert read.discounts == {"ACCESSORIES_KIT": Decimal(18398)}


def test_the_free_sentences():
    assert free_row_text({"key": "accessories_cost", "label": "ACCESSORIES FREE 5K", "amount": Decimal(140),
                          "free": Decimal(5000), "side": "DR"}) == (
        'Accessories Rs 5,000 given free on the NDC ("ACCESSORIES FREE 5K"): Rs 140 left to pay, billed at Rs 5,140.')
    assert "no free amount is counted" in free_row_text(
        {"key": "rsa_amount", "label": "RSA FREE", "amount": Decimal(5000), "free": None, "side": "DR"})
    assert "neither charged nor counted as a benefit" in free_row_text(
        {"key": "extended_warranty_amount", "label": "EXTENDED WARRANTY FREE", "amount": Decimal(60000),
         "free": Decimal(60000), "side": "CR"})


def test_the_free_remarks():
    from audit_core.uc03_system_remarks import free_on_ndc_remarks

    remarks = free_on_ndc_remarks([{"text": "Accessories Rs 5,000 given free on the NDC."}])
    assert remarks[0]["code"] == "FREE_ON_NDC" and remarks[0]["text"].startswith("Accessories")


# ── rows left out, for the PC ────────────────────────────────────────────────
def test_the_rows_listed_for_the_pc():
    read = _read(("IF ANY OTHERS TR", 2350), ("ACCESSORIES 1 DEC 5000", 1025),
                 ("2026-10-09, JOURNAL_NONGST, 25711, PRADIP KUMAR SAHOO", -255931))
    listed = [u for u in read.unmapped if u["why"] in _FOR_A_PERSON and not _POSTING.fullmatch(str(u["label"]))]
    assert [u["label"] for u in listed] == ["IF ANY OTHERS TR", "ACCESSORIES 1 DEC 5000"]


def test_the_task_text():
    text_ = describe([{"label": "IF ANY OTHERS TR", "amount": Decimal(2350), "why": "its wording names no Deal line"}])
    assert text_ == '"IF ANY OTHERS TR" Rs 2,350: its wording names no Deal line'


def test_a_final_payment_summary_row_and_an_exchange_vehicle_amount_are_not_left_out():
    read = _read(("FINAL PAYMENT (20+21+22+23)", -1640186), ("EXCHANGE VEHICLE AMT.", -260000))
    assert not read.unmapped and read.old_vehicle_value == Decimal(260000)


# ── on the database: the PC's task, the Deal note and the system remark ─────
@pytest.fixture
def journey():
    from conftest import delete_tenant_data
    from p2_support import create_p2_journey, database_engine

    engine = database_engine()
    created = create_p2_journey(engine, prefix="ndcfree")
    with engine.begin() as c:
        c.execute(text("UPDATE auditcore.projects SET deal_source_policy='ACCOUNT_STATEMENT' WHERE tenant_id=:t"),
                  {"t": created.tenant_id})
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _ndc(journey, rows):
    import json
    from uuid import uuid4

    from p2_support import add_evidence, add_extracted_field, add_page

    document_id = uuid4()
    add_page(journey, page_number=4, status="READY", di_document_id=document_id)
    add_evidence(journey, di_document_id=document_id, document_type_key="customer_ledger")
    add_extracted_field(journey, di_document_id=document_id, field_key="line_items", value=json.dumps(rows),
                        document_type="customer_ledger")
    return document_id


def _in(journey, fn):
    from audit_core.db import set_tenant_context

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return fn(c)


def _task(c, journey):
    from audit_core.uc03_statement_rows_left_out import TASK_TYPE

    row = c.execute(text("SELECT task_status, description, assigned_role_code FROM auditcore.p2_tasks "
                         "WHERE tenant_id=:t AND journey_id=:j AND task_type=:k"),
                    {"t": journey.tenant_id, "j": journey.journey_id, "k": TASK_TYPE}).mappings().one_or_none()
    return dict(row) if row else None


_ROWS = [
    {"particulars_raw": "EX-SHOW ROOM PRICE OF VEHICLE", "debit_amount": "891001"},
    {"particulars_raw": "ACCESSORIES Free 5K", "debit_amount": "140"},
    {"particulars_raw": "IF ANY OTHERS TR", "debit_amount": "2350"},
    {"particulars_raw": "BOOKING AMOUNT", "credit_amount": "5000"},
]


def test_a_row_left_out_raises_the_pcs_task_that_closes_once_it_is_read(journey):
    import json

    from audit_core.uc03_statement_rows_left_out import sync_rows_left_out

    document_id = _ndc(journey, _ROWS)
    assert _in(journey, lambda c: sync_rows_left_out(c, tenant_id=journey.tenant_id,
                                                     journey_id=journey.journey_id)) == {"rows": 1}
    task = _in(journey, lambda c: _task(c, journey))
    assert task["assigned_role_code"] == "PC" and task["task_status"] == "READY"
    assert '"IF ANY OTHERS TR" Rs 2,350: its wording names no Deal line' in task["description"]
    fixed = [dict(r) for r in _ROWS]
    fixed[2]["particulars_raw"] = "REGISTRATION COST TR"
    _in(journey, lambda c: c.execute(
        text("UPDATE auditcore.journey_document_extracted_fields SET effective_value=CAST(:v AS jsonb), "
             "is_modified=true WHERE tenant_id=:t AND di_document_id=:d AND field_key='line_items'"),
        {"v": json.dumps(json.dumps(fixed)), "t": journey.tenant_id, "d": document_id}))
    assert _in(journey, lambda c: sync_rows_left_out(c, tenant_id=journey.tenant_id,
                                                     journey_id=journey.journey_id)) == {"rows": 0}
    assert _in(journey, lambda c: _task(c, journey))["task_status"] == "VERIFIED_COMPLETE"


def test_no_task_for_a_project_whose_deal_is_priced_by_invoices(journey):
    from audit_core.uc03_statement_rows_left_out import sync_rows_left_out

    _ndc(journey, _ROWS)
    _in(journey, lambda c: c.execute(text("UPDATE auditcore.projects SET deal_source_policy='INVOICE' "
                                          "WHERE tenant_id=:t"), {"t": journey.tenant_id}))
    assert _in(journey, lambda c: sync_rows_left_out(c, tenant_id=journey.tenant_id,
                                                     journey_id=journey.journey_id)) == {"skipped": True}


def test_the_free_row_is_found_for_the_deal_and_the_remarks(journey):
    from audit_core.uc03_account_statement import ndc_free_rows

    _ndc(journey, _ROWS)
    rows = _in(journey, lambda c: ndc_free_rows(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id))
    assert [(r["key"], r["text"]) for r in rows] == [(
        "accessories_cost",
        'Accessories Rs 5,000 given free on the NDC ("ACCESSORIES FREE 5K"): Rs 140 left to pay, billed at Rs 5,140.')]
