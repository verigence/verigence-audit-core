"""Deal Type, Customer Type and the stored insurance decision (product owner 2026-11-11).

Deal Type: Inscope when booked on or after 1 Sep 2026 and the vehicle (age from its VIN)
is under 90 days old; Outscope otherwise; not filled while there is no VIN to read; MR,
Lease, Bulk -- and any Deal Type the Team Lead sets -- win over the rule.
Customer Type: Corporate with a GSTIN, Company with a company ID card and a corporate
discount, else Individual."""
from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from p2_support import (
    add_evidence,
    add_ready_document,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import uc03_p2_journey_classification as rule
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_tasks import submit_action

TODAY = date(2026, 11, 11)


# ── the VIN ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("vin", "made"), [
    ("MA1NS2EW1T2J82494", date(2026, 9, 1)),   # T = 2026, J = September
    ("MA1NA2XZXT6K57790", date(2026, 10, 1)),  # K = October
    ("MA1FN2XUWT6H11728", date(2026, 8, 1)),   # H = August
    ("MA1NS2NP1S2F79014", date(2025, 6, 1)),   # S = 2025, F = June
    ("ma1 ns2ew1t2 a12221", date(2026, 1, 1)),  # as typed, with spaces
])
def test_mahindra_vin_names_the_month_of_manufacture(vin: str, made: date) -> None:
    assert rule.vin_manufacture_month(vin, today=TODAY) == made


@pytest.mark.parametrize("vin", [
    None, "", "T1J82078",       # a partial chassis number
    "MALRE2TTKT5J35402",        # not a Mahindra VIN
    "MA1RY3JNKI5J35425",        # I is not a year code
    "MA1TJ2YD6TK659341",        # 6 is not a month code
    "MA1NS2EW1T2M82494",        # December 2026: after today, a misread
])
def test_a_vin_that_cannot_be_read_gives_no_month(vin: str | None) -> None:
    assert rule.vin_manufacture_month(vin, today=TODAY) is None


def test_the_vin_is_the_one_the_sale_documents_agree_on() -> None:
    # One misread copy (the invoice) against two that agree (ADI-SJ-261027-006).
    assert rule.agreed_vin([
        ("customer_invoice_dms", "d1", "MA1NA2XZTK6K57070"),
        ("insurance_cover", "d2", "MA1NA2XZXT6K57070"),
        ("rto_challan", "d3", "MA1NA2XZXT6K57070"),
        ("accessory_invoice_dms", "d4", "T6K57070"),
    ], today=TODAY) == "MA1NA2XZXT6K57070"
    # The same document read twice counts once. Two papers, two VINs, nothing else: no VIN.
    assert rule.agreed_vin([
        ("rto_challan", "d1", "MA1NS2NP1T2F75014"), ("rto_challan", "d1", "MA1NS2NP1T2F75014"),
        ("customer_invoice_dms", "d2", "MA1NS2NP1T2F79014"),
    ], today=TODAY) is None
    # A short chassis number that is one VIN's own ending supports it (SHI-SW-261110-001).
    assert rule.agreed_vin([
        ("customer_invoice_dms", "d1", "MA1JU2ZUZN2K26945"),
        ("rto_challan", "d2", "MA1UJ2JJ2T2K26045"),
        ("customer_ledger", "d3", "T2K26045"),
    ], today=TODAY) == "MA1UJ2JJ2T2K26045"
    # Three papers, three readings (UTK-JI-261106-006): they do not agree.
    assert rule.agreed_vin([
        ("customer_invoice_dms", "d1", "MA1RY2JNKTJ535417"),
        ("customer_ledger", "d2", "MA1RY2NKTSJ35417"),
        ("insurance_cover", "d3", "MA1Y22NTSJ5J56417"),
    ], today=TODAY) is None
    # Equal support of two documents each: the VIN that names its month, then the invoice's.
    assert rule.agreed_vin([
        ("insurance_cover", "d1", "MA1RY3JNKI5J35425"), ("form_21", "d2", "MA1RY3JNKI5J35425"),
        ("rto_challan", "d3", "MA1RY3JNKT5J35425"), ("customer_invoice_dms", "d4", "MA1RY3JNKT5J35425"),
    ], today=TODAY) == "MA1RY3JNKT5J35425"
    # A single paper with the only VIN stands; a VIN printed inside a longer text is read.
    assert rule.agreed_vin([("rto_challan", "d1", "MA1NS2NP1T2F75014")], today=TODAY) == "MA1NS2NP1T2F75014"
    assert rule.agreed_vin([("accessory_invoice_dms", "d1", "T5J31078 (MA1RE2TTKT5J31078)")],
                           today=TODAY) == "MA1RE2TTKT5J31078"
    assert rule.agreed_vin([("accessory_invoice_dms", "d1", "T1J82078")], today=TODAY) is None
    assert rule.agreed_vin([], today=TODAY) is None


# ── Deal Type ────────────────────────────────────────────────────────────────

def _deal(**overrides):
    inputs = {"booking_date": date(2026, 10, 5), "vin": "MA1NS2EW1T2J82494",
              "manufacture_month": date(2026, 9, 1), "reference_date": date(2026, 10, 20),
              "reference_kind": "invoice"}
    return rule.decide_deal_type(**{**inputs, **overrides})


def test_inscope_booked_from_september_with_a_vehicle_under_90_days() -> None:
    decided = _deal()
    assert (decided["deal_type"], decided["deal_type_decided_by"], decided["vehicle_age_days"]) == \
        ("INSCOPE", "RULE", 49)
    assert _deal(booking_date=date(2026, 9, 1))["deal_type"] == "INSCOPE"


def test_outscope_booked_before_september_or_vehicle_90_days_old() -> None:
    assert _deal(booking_date=date(2026, 8, 31))["deal_type"] == "OUTSCOPE"
    # Made June 2026, invoiced 20 Oct 2026: 141 days.
    assert _deal(manufacture_month=date(2026, 6, 1))["deal_type"] == "OUTSCOPE"
    # 89 days is under 90; 90 days is not.
    assert _deal(reference_date=date(2026, 11, 29))["deal_type"] == "INSCOPE"
    assert _deal(reference_date=date(2026, 11, 30))["deal_type"] == "OUTSCOPE"


def test_deal_type_is_not_filled_without_a_vin() -> None:
    decided = _deal(vin=None, manufacture_month=None)
    assert decided["deal_type"] is None and decided["vin_pending"] is True
    unread = _deal(vin="MA1RY3JNKI5J35425", manufacture_month=None)
    assert unread["deal_type"] is None and unread["vin_pending"] is True
    # Booked before 1 Sep 2026: Outscope whatever the vehicle.
    assert _deal(booking_date=date(2026, 8, 6), vin=None, manufacture_month=None)["deal_type"] == "OUTSCOPE"


def test_the_team_lead_wins_over_the_rule() -> None:
    for chosen in ("MR", "LEASE", "BULK", "INSCOPE", "OUTSCOPE"):
        decided = _deal(tl_deal_type=chosen, tl_reason="fleet order", booking_date=date(2026, 8, 1))
        assert (decided["deal_type"], decided["deal_type_decided_by"]) == (chosen, "TL")
    assert _deal(mr_opted=True)["deal_type"] == "MR"
    assert _deal(vin=None, manufacture_month=None, tl_deal_type="LEASE")["deal_type"] == "LEASE"


# ── Customer Type ────────────────────────────────────────────────────────────

def _customer(**overrides):
    inputs = {"gst_certificate_on_file": False, "gstins": [], "company_id_on_file": False,
              "corporate_discount": None}
    return rule.decide_customer_type(**{**inputs, **overrides})["customer_type"]


def test_customer_type() -> None:
    assert _customer() == "INDIVIDUAL"
    assert _customer(gstins=["21AAACU2665C1ZV"]) == "CORPORATE"
    assert _customer(gst_certificate_on_file=True) == "CORPORATE"
    # "No", a bare number or a PAN is not a GSTIN.
    assert _customer(gstins=["NO", "18FBW3915Q", "12345"]) == "INDIVIDUAL"
    assert _customer(company_id_on_file=True, corporate_discount=12000) == "COMPANY"
    # A company ID card without the discount, or the discount without the card: Individual.
    assert _customer(company_id_on_file=True, corporate_discount=0) == "INDIVIDUAL"
    assert _customer(corporate_discount=12000) == "INDIVIDUAL"
    # A GSTIN wins over a company ID card.
    assert _customer(gstins=["21AAACU2665C1ZV"], company_id_on_file=True, corporate_discount=5000) == "CORPORATE"


# ── the stored copy ──────────────────────────────────────────────────────────

@pytest.fixture
def engine():
    engine = database_engine()
    yield engine
    engine.dispose()


def _run(journey, fn, **kwargs):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return fn(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, **kwargs)


def _stored(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT * FROM auditcore.journey_classifications WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id}).mappings().one()


def _set_vin(journey, vin: str) -> None:
    """The sale's invoice prints the VIN; the customer's old car's RC prints another."""
    add_ready_document(journey, "customer_invoice_dms", vin_number=vin)
    add_ready_document(journey, "vehicle_rc", chassis_number="MA1ZP2TAKE2C61179")


def _task(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        row = connection.execute(
            text("SELECT * FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j AND task_type=:k"),
            {"t": journey.tenant_id, "j": journey.journey_id, "k": rule.TASK_TYPE}).mappings().one_or_none()
        return dict(row) if row else None


def test_stored_copy_follows_the_vin_and_the_documents(engine) -> None:
    journey = create_p2_journey(engine, prefix="cls")
    first = _run(journey, rule.refresh)
    assert first["changed"] is True
    row = _stored(journey)
    # No VIN: the Deal Type is not filled. No GSTIN: Individual. No policy: by the dealer, by default.
    assert (row["deal_type"], row["vin_pending"], row["customer_type"]) == (None, True, "INDIVIDUAL")
    assert (row["insurance_type"], row["insurance_decided_by"]) == ("DEALERSHIP", "DEFAULT")
    assert _run(journey, rule.refresh)["changed"] is False

    # A VIN made this month and a GST certificate arrive.
    today = _run(journey, lambda c, **_: c.execute(
        text("SELECT CAST(now() AT TIME ZONE 'Asia/Kolkata' AS date)")).scalar_one())
    year_code = {v: k for k, v in rule._YEAR_CODES.items()}[today.year]
    month_code = {v: k for k, v in rule._MONTH_CODES.items()}[today.month]
    _set_vin(journey, f"MA1NS2EW1{year_code}2{month_code}82494")
    add_ready_document(journey, "gst_certificate", gstin="21AAACU2665C1ZV")
    assert _run(journey, rule.refresh)["changed"] is True
    row = _stored(journey)
    assert row["customer_type"] == "CORPORATE"
    assert row["vin_manufacture_month"] == date(today.year, today.month, 1)
    assert row["vin_pending"] is False
    # Booked today (the upload day stands in for a missing booking date): after 1 Sep 2026.
    assert row["deal_type"] == "INSCOPE"


def test_team_lead_is_asked_in_delivery_and_their_answer_wins(engine) -> None:
    journey = create_p2_journey(engine, prefix="clstl")
    # Not asked before delivery has started.
    _run(journey, rule.sync_classification)
    assert _task(journey) is None
    add_evidence(journey, di_document_id=uuid4(), document_type_key="supporting_document", process_area="DELIVERY")
    _run(journey, rule.sync_classification)
    task = _task(journey)
    assert task is not None and task["assigned_role_code"] == "TL"

    def answer(role: str):
        return _run(journey, lambda c, **kw: submit_action(
            c, tenant_id=kw["tenant_id"], task_id=task["task_id"], action="COMPLETE_ACTION",
            actor_id=journey.actor_id, actor_role_code=role, comment="lease deal for a fleet",
            details={"answer": "LEASE"}))

    with pytest.raises(ValueError):
        answer("PC")
    answer("TL")
    row = _stored(journey)
    assert (row["deal_type"], row["deal_type_decided_by"], row["tl_deal_type"]) == ("LEASE", "TL", "LEASE")
    assert _task(journey)["task_status"] == "VERIFIED_COMPLETE"

    # The Team Lead hands it back to the rule: not filled again (still no VIN).
    _run(journey, rule.set_tl_deal_type, deal_type=None, reason="set by mistake", actor_id=journey.actor_id,
         role="TL", correlation_id=None)
    assert _stored(journey)["deal_type"] is None


def test_one_deal_type_for_several_journeys_in_one_go(engine) -> None:
    journey = create_p2_journey(engine, prefix="clsmany")
    add_evidence(journey, di_document_id=uuid4(), document_type_key="supporting_document", process_area="DELIVERY")
    _run(journey, rule.sync_classification)
    assert _task(journey)["task_status"] not in ("VERIFIED_COMPLETE", "CANCELLED")
    outside = uuid4()  # not a Journey of this Project: refused, nothing changed

    def bulk(actor_id: str):
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            return rule.set_deal_type_for_many(
                connection, tenant_id=journey.tenant_id, journey_ids=[journey.journey_id, outside, journey.journey_id],
                deal_type="BULK", reason="fleet order of 12 vehicles", actor_id=actor_id, role="REVIEWER",
                correlation_id=None)

    # Someone with no Dealership / Outlet of the Journey: refused.
    assert bulk("someone-else")["updated"] == []
    result = bulk(journey.actor_id)
    assert [u["dealType"] for u in result["updated"]] == ["BULK"] and result["refused"] == [str(outside)]
    row = _stored(journey)
    assert (row["deal_type"], row["deal_type_decided_by"], row["tl_set_by_role"]) == ("BULK", "TL", "REVIEWER")
    # The system's own task for the Journey is answered with it.
    assert _task(journey)["task_status"] == "VERIFIED_COMPLETE"


def _record(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT vin, chassis_number FROM auditcore.vehicle_records WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id}).one()


def _put_record(journey, vin, chassis) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.vehicle_records (tenant_id, journey_id, vin, chassis_number, source_kind) "
                 "VALUES (:t, :j, :v, :c, 'EVIDENCE')"),
            {"t": journey.tenant_id, "j": journey.journey_id, "v": vin, "c": chassis})


def test_vehicle_record_follows_the_vin_the_sale_documents_agree_on(engine) -> None:
    # A misread copy on the record; the policy and the RTO challan agree on the VIN.
    journey = create_p2_journey(engine, prefix="clsrec")
    _put_record(journey, "MA1NA2XZTK6K57070", "T6K57070")
    add_ready_document(journey, "customer_invoice_dms", vin_number="MA1NA2XZTK6K57070")
    add_ready_document(journey, "insurance_cover", chassis_number="MA1NA2XZXT6K57070")
    add_ready_document(journey, "rto_challan", chassis_number="MA1NA2XZXT6K57070")
    assert _run(journey, rule.correct_vehicle_record, apply=False) is not None
    assert tuple(_record(journey)) == ("MA1NA2XZTK6K57070", "T6K57070")  # a dry run writes nothing
    change = _run(journey, rule.correct_vehicle_record)
    assert change["vin"] == {"from": "MA1NA2XZTK6K57070", "to": "MA1NA2XZXT6K57070"}
    # The short chassis number is the VIN's own ending: kept.
    assert tuple(_record(journey)) == ("MA1NA2XZXT6K57070", "T6K57070")
    assert _run(journey, rule.correct_vehicle_record) is None


def test_vehicle_record_drops_the_old_vehicles_chassis(engine) -> None:
    # Only the customer's old car's RC names a chassis number: it is not this sale's vehicle.
    journey = create_p2_journey(engine, prefix="clsold")
    _put_record(journey, None, "MA1ZP2TAKE2C61179")
    add_ready_document(journey, "vehicle_rc", chassis_number="MA1ZP2TAKE2C61179")
    _run(journey, rule.correct_vehicle_record)
    assert tuple(_record(journey)) == (None, None)
    assert _run(journey, rule.refresh)["deal_type"] is None

    # The RC over the sale's own documents: the record takes the sale's VIN.
    other = create_p2_journey(engine, prefix="clsrc")
    _put_record(other, "73722535", "MA1XL2TVXS6H24533")
    add_ready_document(other, "vehicle_rc", chassis_number="MA1XL2TVXS6H24533")
    add_ready_document(other, "customer_invoice_dms", chassis_number="MA1XL2TVXT5J30535")
    add_ready_document(other, "insurance_cover", chassis_number="MA1XL2TVXT5J30535")
    _run(other, rule.correct_vehicle_record)
    assert tuple(_record(other)) == ("MA1XL2TVXT5J30535", "MA1XL2TVXT5J30535")
