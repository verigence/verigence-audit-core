"""The SKU ladder end to end (2026-10-20 product owner rules R3 / R4 / R5):
per document, words first, the price only checks the word-identified SKU,
automatic pins are TENTATIVE and replaceable, PC / TL decisions are not."""
from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
import test_uc03_model_resolution as base
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text
from test_uc03_model_resolution import (
    _seed_price_list,
    _set_invoice_field,
    _set_invoice_price,
    _set_journey_product,
    _sku_task,
    _vehicle,
)

import audit_core.uc03_model_resolution as mr
from audit_core.errors import AuditCoreError

# The model-resolution tests' Journey fixtures (one transaction each).
journey = base.journey
mahindra_journey = base.mahindra_journey

# The September 2026 consolidated list's Maxx City rows (ex-showroom, Rs).
_MAXX_CITY_SEP = [
    ("MAXX CITY 1.5 LX BS6.2", "839501.15"),
    ("MAXX CITY 1.4 LX BS6.2", "839501.15"),
    ("MAXX CITY 1.3 VXi AC - BS6.2", "853499.97"),
    ("MAXX CITY 1.5 LX CBC BS6.2", "815000.00"),
]


def _seed_maxx_city(c):
    return dict(zip(
        [v for v, _ in _MAXX_CITY_SEP],
        _seed_price_list(c, [{"model": "MAXX CITY", "variant": v, "fuel": "DIESEL",
                              "components": {"EX_SHOWROOM": ex}} for v, ex in _MAXX_CITY_SEP]),
        strict=True,
    ))


def _booking_form(c, fields: dict, *, doc=None):
    """One booking form document's read fields."""
    doc = doc or uuid4()
    for key, value in fields.items():
        c.execute(
            text(
                """
                INSERT INTO auditcore.journey_document_extracted_fields (
                    tenant_id, journey_id, di_document_id, source_fact_version, stage_code,
                    source_document_type_key, field_key, extracted_value, effective_value, is_modified
                ) VALUES (:t, :j, :doc, 1, 'BOOKING', 'booking_form', :k, CAST(:v AS jsonb), CAST(:v AS jsonb), false)
                """
            ),
            {"t": c.tenant_id, "j": c.journey_id, "doc": doc, "k": key, "v": json.dumps(value)},
        )
    return doc


def _correct_field(c, doc, key, value):
    c.execute(
        text("UPDATE auditcore.journey_document_extracted_fields SET effective_value = CAST(:v AS jsonb), "
             "updated_at_utc = now() WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d AND field_key=:k"),
        {"t": c.tenant_id, "j": c.journey_id, "d": doc, "k": key, "v": json.dumps(value)},
    )


def test_the_maxx_city_booking_at_the_october_price_is_a_price_task_not_the_vxi(mahindra_journey) -> None:
    """Retro case P13: "MAXX PUP CITY 1.5 LXD" booked at 8,52,000 with the
    September list in force. The old resolver took the 1.3 VXi AC (8,53,499.97,
    within 0.5%). The words name the 1.5 LX (LXD = LX diesel); its September
    master price 8,39,501.15 differs from the booking, so the 1.5 LX is
    pinned as AUTO_PRICE_MISMATCH and the PC is asked to set the SKU."""
    c = mahindra_journey
    skus = _seed_maxx_city(c)
    doc = _booking_form(c, {"vehicle_model": "BOLERO MAXX PUP CITY", "vehicle_variant": "1.5 LXD",
                            "ex_showroom_price": "852000"})

    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    row = _vehicle(c)
    assert result["basis"] == "AUTO_PRICE_MISMATCH" and result["priceCheck"] == "DIFFERS"
    assert row["product_sku_id"] == skus["MAXX CITY 1.5 LX BS6.2"]
    assert row["selection_status"] == "TENTATIVE"
    evidence = row["match_evidence"]
    assert (evidence["documentId"], evidence["masterExShowroom"], evidence["documentExShowroom"]) == (
        str(doc), "839501.15", "852000")
    task = _sku_task(c, "SKU_PRICE_MISMATCH")
    assert task["assigned_role_code"] == "PC"
    assert task["title"] == (
        "SKU price differs from booking: master Rs 8,39,501.15 vs booking Rs 8,52,000.00 -- set the SKU")

    # The PC corrects the form's price to the list's: the match is clean and
    # the task closes (paise rounding within two rupees is the same price).
    _correct_field(c, doc, "ex_showroom_price", "839500")
    again = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert again["basis"] == "AUTO_MATCHED" and _vehicle(c)["match_basis"] == "AUTO_MATCHED"
    assert _sku_task(c, "SKU_PRICE_MISMATCH")["task_status"] == "VERIFIED_COMPLETE"


def test_the_booking_form_document_is_read_not_the_journey_snapshot(mahindra_journey) -> None:
    """Per document (R2/R3): the booking form's own model, variant and
    price; a model snapshot written from some other document is not read."""
    c = mahindra_journey
    skus = _seed_maxx_city(c)
    _set_journey_product(c, "MAXX CITY", "1.3 VXi AC")  # e.g. copied from another document
    _booking_form(c, {"vehicle_model": "MAXX PUP CITY", "vehicle_variant": "1.5 LX",
                      "ex_showroom_price": "839501"})

    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result["basis"] == "AUTO_MATCHED"
    assert _vehicle(c)["product_sku_id"] == skus["MAXX CITY 1.5 LX BS6.2"]


def test_a_pc_pick_replaces_an_automatic_match_directly_but_not_a_confirmed_one(mahindra_journey) -> None:
    from audit_core.uc03_p2_deal_actions import pricing_summary

    c = mahindra_journey
    skus = _seed_maxx_city(c)
    _booking_form(c, {"vehicle_model": "MAXX PUP CITY", "vehicle_variant": "1.5 LX", "ex_showroom_price": "852000"})
    mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    summary = pricing_summary(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    assert summary["modelChange"] == "CONFIRM_SKU"  # an automatic match is not a decision
    assert summary["sku"]["matchBasis"] == "AUTO_PRICE_MISMATCH"
    assert "price differs" in summary["sku"]["matchLabel"]
    assert mr.get_model_catalog(c, tenant_id=c.tenant_id, journey_id=c.journey_id)["confirmedProductSkuId"] is None

    mr.confirm_model_resolution_sku(c, tenant_id=c.tenant_id, journey_id=c.journey_id,
                                    product_sku_id=skus["MAXX CITY 1.4 LX BS6.2"], actor_id="pc-1", correlation_id="")
    row = _vehicle(c)
    assert row["product_sku_id"] == skus["MAXX CITY 1.4 LX BS6.2"]
    assert (row["selection_status"], row["match_basis"]) == ("CONFIRMED", "PC_CONFIRMED")
    assert _sku_task(c, "SKU_PRICE_MISMATCH")["task_status"] == "VERIFIED_COMPLETE"
    assert pricing_summary(c, tenant_id=c.tenant_id, journey_id=c.journey_id)["modelChange"] == "PROPOSE_CORRECTION"

    # The resolver never moves a PC's decision; another PC pick needs the TL.
    mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _vehicle(c)["product_sku_id"] == skus["MAXX CITY 1.4 LX BS6.2"]
    with pytest.raises(AuditCoreError) as refused:
        mr.confirm_model_resolution_sku(c, tenant_id=c.tenant_id, journey_id=c.journey_id,
                                        product_sku_id=skus["MAXX CITY 1.5 LX BS6.2"], actor_id="pc-1",
                                        correlation_id="")
    assert refused.value.status_code == 409


def test_accepting_the_price_task_confirms_the_sku(mahindra_journey) -> None:
    from audit_core.uc03_sku_tasks import handle_sku_task_action

    c = mahindra_journey
    skus = _seed_maxx_city(c)
    _booking_form(c, {"vehicle_model": "MAXX PUP CITY", "vehicle_variant": "1.5 LX", "ex_showroom_price": "852000"})
    mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    task = dict(_sku_task(c, "SKU_PRICE_MISMATCH"))
    handle_sku_task_action(c, tenant_id=c.tenant_id, task=task, action="ACCEPT", actor_id="pc-1",
                           actor_role_code="PC", comment=None)
    row = _vehicle(c)
    assert row["product_sku_id"] == skus["MAXX CITY 1.5 LX BS6.2"]
    assert row["match_basis"] == "PC_CONFIRMED"


def test_the_wholesale_invoice_and_a_voided_invoice_never_name_the_vehicle(journey) -> None:
    c = journey
    thar_lx, thar_ax = _seed_price_list(c, [
        {"model": "THAR", "variant": "LX", "components": {"EX_SHOWROOM": "1400000"}},
        {"model": "THAR", "variant": "AX", "components": {"EX_SHOWROOM": "1300000"}},
    ])
    wholesale = uuid4()
    _set_invoice_field(c, "model_name_raw", "THAR", document_type_key="wholesale_invoice", doc=wholesale)
    _set_invoice_field(c, "variant_raw", "AX", document_type_key="wholesale_invoice", doc=wholesale)
    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result == {"skipped": True, "reason": "no_vehicle_model_read"}

    voided, active = uuid4(), uuid4()
    customer_id = c.execute(text("SELECT customer_id FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
                            {"t": c.tenant_id, "j": c.journey_id}).scalar_one()
    for doc, status in ((voided, "VOIDED"), (active, "ACTIVE")):
        c.execute(
            text("INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id, di_document_id, "
                 "document_type_key, evidence_purpose, process_area, association_status) "
                 "VALUES (:t, :j, :c, :s, :d, 'customer_invoice_dms', 'JOURNEY_DOCUMENT', 'DELIVERY', :st)"),
            {"t": c.tenant_id, "j": c.journey_id, "c": customer_id, "s": uuid4(), "d": doc, "st": status},
        )
    _set_invoice_field(c, "model_name_raw", "THAR", doc=active)
    _set_invoice_field(c, "variant_raw", "LX", doc=active)
    _set_invoice_field(c, "model_name_raw", "THAR", doc=voided)
    _set_invoice_field(c, "variant_raw", "AX", doc=voided)
    _set_invoice_price(c, "1300000", doc=voided)
    readings = mr._vehicle_invoice_readings(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    # Neither the wholesale invoice nor the voided one is read: only the active one.
    assert [(r["documentId"], r["variant"]) for r in readings] == [(str(active), "LX")]
    assert thar_lx != thar_ax


def test_two_vehicle_invoices_naming_different_vehicles_set_nothing(journey) -> None:
    c = journey
    lx, ax = _seed_price_list(c, [
        {"model": "THAR", "variant": "LX", "components": {"EX_SHOWROOM": "1400000"}},
        {"model": "THAR", "variant": "AX", "components": {"EX_SHOWROOM": "1300000"}},
    ])
    first, second = uuid4(), uuid4()
    for doc, variant, price in ((first, "LX", "1400000"), (second, "AX", "1300000")):
        _set_invoice_field(c, "model_name_raw", "THAR", doc=doc)
        _set_invoice_field(c, "variant_raw", variant, doc=doc)
        _set_invoice_field(c, "invoice_number", f"INV-{variant}", doc=doc)
        _set_invoice_price(c, price, doc=doc)

    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result.get("mismatchFlagged") is True
    assert _vehicle(c) is None or _vehicle(c)["product_sku_id"] is None
    task = _sku_task(c, "SKU_INVOICES_DISAGREE")
    assert task["assigned_role_code"] == "PC" and "INV-LX" in task["description"] and "INV-AX" in task["description"]
    assert {lx, ax}


def test_no_pricing_date_means_no_resolution_never_today(journey) -> None:
    c = journey
    _seed_price_list(c, [{"model": "THAR", "variant": "LX", "components": {"EX_SHOWROOM": "1400000"}}])
    c.execute(text("UPDATE auditcore.bookings SET booking_date = NULL WHERE tenant_id=:t AND journey_id=:j"),
              {"t": c.tenant_id, "j": c.journey_id})
    _set_journey_product(c, "THAR", "LX")
    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result == {"skipped": True, "reason": "no_pricing_date"}
    assert mr.journey_pricing_date(c, tenant_id=c.tenant_id, journey_id=c.journey_id) is None


# ── the "reresolve" run ───────────────────────────────────────────────────────
class _Conn:
    """A connection carrying the tenant and OEM the price-list seeder reads."""

    def __init__(self, connection, tenant_id, journey_id, oem_id):
        self._c = connection
        self.tenant_id, self.journey_id, self.oem_id = tenant_id, journey_id, oem_id

    def execute(self, *args, **kwargs):
        return self._c.execute(*args, **kwargs)


@pytest.fixture
def two_journeys():
    engine = database_engine()
    first = create_p2_journey(engine, prefix="rer")
    with engine.begin() as connection:
        second_id = connection.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, journey_reference) "
                 "VALUES (:t, :d, :o, :c, :r) RETURNING journey_id"),
            {"t": first.tenant_id, "d": first.dealer_id, "o": first.outlet_id, "c": first.customer_id,
             "r": f"RER-2-{uuid4().hex[:6]}"},
        ).scalar_one()
    try:
        yield engine, first, UUID(str(second_id))
    finally:
        with engine.begin() as connection:
            for table in ("sku_resolution_run_items", "sku_resolution_runs"):
                connection.execute(text(f"DELETE FROM auditcore.{table} WHERE tenant_id=:t"),
                                   {"t": first.tenant_id})
        delete_tenant_data(engine, first.tenant_id)
        engine.dispose()


def test_reresolve_replaces_a_price_chosen_pin_and_leaves_the_pcs(two_journeys) -> None:
    """A Journey pinned before 2026-10-20 by price (AUTO_LEGACY: "MX3 PM AT"
    although the booking says "MX3 PRO PETROL AT") is resolved again by its
    words -- first as a dry run that writes nothing, then committed; a PC's
    pick on another Journey is not touched."""
    from audit_core.db import set_tenant_context
    from audit_core.uc03_backfill_document_sync_producers import reresolve_all_skus

    engine, first, second_id = two_journeys
    with engine.begin() as connection:
        set_tenant_context(connection, first.tenant_id)
        oem_id = connection.execute(text("SELECT oem_id FROM auditcore.projects WHERE tenant_id=:t"),
                                    {"t": first.tenant_id}).scalar_one()
        c = _Conn(connection, first.tenant_id, first.journey_id, oem_id)
        non_pro, pro = _seed_price_list(c, [
            {"model": "XUV3XO", "variant": "MX3 PM AT", "fuel": "PETROL", "transmission": "AT",
             "components": {"EX_SHOWROOM": "1099000"}},
            {"model": "XUV3XO", "variant": "MX3 PRO PM AT", "fuel": "PETROL", "transmission": "AT",
             "components": {"EX_SHOWROOM": "1149000"}},
        ])
        for journey_id in (first.journey_id, second_id):
            connection.execute(text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date) "
                                    "VALUES (:t, :j, CURRENT_DATE - 10)"), {"t": first.tenant_id, "j": journey_id})
            c.journey_id = journey_id
            _booking_form(c, {"vehicle_model": "XUV 3XO", "vehicle_variant": "MX3 PRO PETROL AT",
                              "ex_showroom_price": "1099000"})
        for journey_id, sku, status, basis, method in (
            (first.journey_id, non_pro, "TENTATIVE", "AUTO_LEGACY", "MODEL_RESOLUTION_SYNC_V1"),
            (second_id, non_pro, "CONFIRMED", "PC_CONFIRMED", "MODEL_RESOLUTION_PC_PICK_V1"),
        ):
            connection.execute(
                text("INSERT INTO auditcore.journey_products (tenant_id, journey_id, product_sku_id, "
                     "selection_source, selection_status, selection_method, match_basis) "
                     "VALUES (:t, :j, :s, 'EVIDENCE', :st, :m, :b)"),
                {"t": first.tenant_id, "j": journey_id, "s": sku, "st": status, "m": method, "b": basis},
            )

    def state():
        with engine.begin() as connection:
            set_tenant_context(connection, first.tenant_id)
            return {
                r["journey_id"]: r for r in connection.execute(
                    text("SELECT journey_id, product_sku_id, match_basis, selection_status "
                         "FROM auditcore.journey_products WHERE tenant_id=:t"), {"t": first.tenant_id},
                ).mappings()
            }

    def items(run_id):
        with engine.begin() as connection:
            set_tenant_context(connection, first.tenant_id)
            return {
                r["journey_id"]: r for r in connection.execute(
                    text("SELECT journey_id, outcome, detail, proposal FROM auditcore.sku_resolution_run_items "
                         "WHERE tenant_id=:t AND run_id=:r"), {"t": first.tenant_id, "r": run_id},
                ).mappings()
            }

    # The dry run (review 471 B2) changes nothing and says what would happen.
    dry = reresolve_all_skus(engine, first.tenant_id, actor_id="test-dry")
    assert dry["journeysFailed"] == []
    rows = state()
    assert rows[first.journey_id]["product_sku_id"] == non_pro
    assert rows[first.journey_id]["match_basis"] == "AUTO_LEGACY"
    proposal = items(dry["runId"])[first.journey_id]["proposal"]
    assert proposal["dryRun"] is True and proposal["action"] == "BOOKING_PIN"
    assert proposal["oldBasis"] == "AUTO_LEGACY" and proposal["proposedBasis"] == "AUTO_PRICE_MISMATCH"
    assert proposal["words"] == "XUV 3XO / MX3 PRO PETROL AT"
    assert proposal["masterExShowroom"] == "1149000.00" and proposal["documentExShowroom"] == "1099000"
    assert "SKU_PRICE_MISMATCH (booking)" in proposal["tasks"]
    assert "[dry run]" in items(dry["runId"])[first.journey_id]["detail"]

    result = reresolve_all_skus(engine, first.tenant_id, actor_id="test", commit=True)
    assert result["journeysFailed"] == []
    rows, run_items = state(), items(result["runId"])
    # Words name the PRO; the booking price is the non-PRO's -> a price task.
    assert rows[first.journey_id]["product_sku_id"] == pro
    assert rows[first.journey_id]["match_basis"] == "AUTO_PRICE_MISMATCH"
    assert "AUTO_LEGACY" in run_items[first.journey_id]["detail"]
    assert rows[second_id]["product_sku_id"] == non_pro
    assert rows[second_id]["match_basis"] == "PC_CONFIRMED"
    assert run_items[second_id]["outcome"] == "ALREADY_HAD_SKU"
    with engine.begin() as connection:
        set_tenant_context(connection, first.tenant_id)
        changed = connection.execute(
            text("SELECT details FROM auditcore.p2_activity_events WHERE tenant_id=:t AND journey_id=:j "
                 "AND event_type='SKU_RESOLUTION_CHANGED'"), {"t": first.tenant_id, "j": first.journey_id},
        ).scalars().all()
    assert len(changed) == 1 and changed[0]["fromBasis"] == "AUTO_LEGACY"


def test_the_worker_runs_reresolve_for_a_reresolve_tag(monkeypatch) -> None:
    from audit_core import uc03_backfill_document_sync_producers as backfill
    from audit_core import uc03_p2_worker as worker

    called: list[str] = []

    class _C:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *_args, **_kwargs):
            class _R:
                def first(self_inner):
                    return None
            return _R()

    class _E:
        def begin(self):
            return _C()

    monkeypatch.setattr(worker, "_active_tenants", lambda engine: ["t1"])
    monkeypatch.setattr(worker, "set_tenant_context", lambda connection, tenant_id: None)
    monkeypatch.setattr(backfill, "reresolve_all_skus",
                        lambda engine, tenant_id, actor_id=None, commit=False:
                        called.append((tenant_id, commit)) or {"runId": uuid4()})
    monkeypatch.setattr(backfill, "backfill_sku_resolution_for_tenant",
                        lambda *a, **k: pytest.fail("the plain run must not be used for a reresolve tag"))
    worker._sku_run_once(_E(), "reresolve-2026-10-21")
    worker._sku_run_once(_E(), "reresolve-2026-10-22-commit")
    # "reresolve-..." is a dry run; only "-commit" writes (review 471 B2).
    assert called == [("t1", False), ("t1", True)]


# ── renamed models: either spelling, either date (coordinator, 2026-10-20) ────
_SEPT = [
    ("SCORPIO N", "Z8 S D AT 2WD 7 STR BS6.2 - N", "1917499.58"),
    ("New SCORPIO N", "Z8 S D AT 2WD 7 STR BS6.2 - Refresh", "1949999.00"),
    ("SCORPIO CLASSIC", "Classic S - 9 STR BS6.2 - E", "1361000.00"),
    ("NEW THAR 2WD & 4WD", "AXT D MT 2WD 4S HT BS6.2", "1249999.00"),
    ("XUV3XO", "AX5L TGDI MT", "1202500.54"),
    ("XEV9S", "XEV 9S TWO ABOVE B79 R18 7STR C11", "2599000.00"),
]
_OCT = [
    ("SCORPIO-N", "Z8 S D AT 2WD 7 STR BS6.2 - N", "1949999.00"),
    ("SCORPIO", "Classic S - 9 STR BS6.2 - E", "1361000.00"),
    ("NEW THAR", "AXT D MT 2WD 4S HT BS6.2", "1249999.00"),
    ("XUV 3XO", "AX5L TGDI MT", "1202501.17"),
    ("XEV 9S", "XEV 9S TWO ABOVE B79 R18 7STR C11", "2599000.00"),
]


@pytest.mark.parametrize(("model", "variant", "september", "october"), [
    ("SCORPIO-N", "Z8 S D AT 2WD 7 STR", "New SCORPIO N", "SCORPIO-N"),
    ("New SCORPIO N", "Z8 S D AT 2WD 7 STR", "New SCORPIO N", "SCORPIO-N"),
    # "SCORPIO N" is the old model while it is on sale, its successor after.
    ("SCORPIO N", "Z8 S D AT 2WD 7 STR", "SCORPIO N", "SCORPIO-N"),
    ("SCORPIO", "Classic S 9 STR", "SCORPIO CLASSIC", "SCORPIO"),
    ("SCORPIO CLASSIC", "S 9 STR", "SCORPIO CLASSIC", "SCORPIO"),
    ("NEW THAR", "AXT D MT 2WD", "NEW THAR 2WD & 4WD", "NEW THAR"),
    ("NEW THAR 2WD & 4WD", "AXT D MT 2WD", "NEW THAR 2WD & 4WD", "NEW THAR"),
    ("THAR", "AXT D MT 2WD", "NEW THAR 2WD & 4WD", "NEW THAR"),
    ("XUV3XO", "AX5L TGDI MT", "XUV3XO", "XUV 3XO"),
    ("XUV 3XO", "AX5L TGDI MT", "XUV3XO", "XUV 3XO"),
    ("XEV9S", "TWO ABOVE B79 R18 7STR C11", "XEV9S", "XEV 9S"),
    ("XEV 9S", "TWO ABOVE B79 R18 7STR C11", "XEV9S", "XEV 9S"),
])
def test_a_renamed_model_is_found_under_either_spelling_on_either_date(
    mahindra_journey, model, variant, september, october,
) -> None:
    c = mahindra_journey
    sept = dict(zip([m for m, _, _ in _SEPT], _seed_price_list(
        c, [{"model": m, "variant": v, "components": {"EX_SHOWROOM": ex}} for m, v, ex in _SEPT],
        effective_from="DATE '2026-09-01'"), strict=True))
    octo = dict(zip([m for m, _, _ in _OCT], _seed_price_list(
        c, [{"model": m, "variant": v, "components": {"EX_SHOWROOM": ex}} for m, v, ex in _OCT],
        effective_from="DATE '2026-10-03'"), strict=True))
    _set_journey_product(c, model, variant)
    for on, expected in (("2026-09-15", sept[september]), ("2026-10-15", octo[october])):
        c.execute(text("UPDATE auditcore.bookings SET booking_date = CAST(:on AS date), pricing_effective_on = NULL "
                       "WHERE tenant_id=:t AND journey_id=:j"), {"on": on, "t": c.tenant_id, "j": c.journey_id})
        ev = mr._evaluate(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
        result = ev["bookingResult"]
        assert result.identified, (on, result.status, result.detail)
        assert result.row["product_sku_id"] == expected, (on, result.row["model_name"])


def test_the_bolero_family_is_three_models_on_either_book(mahindra_journey) -> None:
    """September's combined "THE BOSS- BOLERO+NEO" and October's three
    models: the series letter says which (migration 0151)."""
    c = mahindra_journey
    b6, n10, p10 = _seed_price_list(c, [
        {"model": "THE BOSS- BOLERO+NEO", "variant": v, "components": {"EX_SHOWROOM": "1000000"}}
        for v in ("B6 OPT BS6.2 - Refresh", "N10 OPT BS6.2 - Refresh", "P10 BS6.2")
    ])
    for model, variant, expected in (("BOLERO", "B6 OPT", b6), ("BOLERO NEO", "N10 OPT", n10),
                                     ("BOL NEO", "N10 OPT", n10), ("BOLERO NEO PLUS", "P10", p10)):
        c.execute(text("DELETE FROM auditcore.journey_products WHERE tenant_id=:t AND journey_id=:j"),
                  {"t": c.tenant_id, "j": c.journey_id})
        _set_journey_product(c, model, variant)
        result = mr._evaluate(c, tenant_id=c.tenant_id, journey_id=c.journey_id)["bookingResult"]
        assert result.identified and result.row["product_sku_id"] == expected, (model, result.status)
