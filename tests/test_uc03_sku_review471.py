"""Review 471 (2026-10-21): the reviewer's probes S1-S9, now asserting the
fixed behaviour, and the blocking items B1-B4.

* B1 -- migration 0152 keeps a PC's decision saved under the resolver's own
  method (before #441) as PC_CONFIRMED.
* B2 -- the reresolve run decides first and never clears: no pricing date, a
  manual capture, no price list in force or an error leave the SKU where it
  is (marked AUTO_UNCONFIRMED with a PC task, or untouched on an error).
* B3 -- "Keep this vehicle" confirms the SKU the task names.
* B4 -- no SKU task reopens after a PC decision; disagreement tasks have
  real actions.
"""
from __future__ import annotations

import importlib.util
import pathlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_uc03_model_resolution as base
from sqlalchemy import text
from test_uc03_model_resolution import (
    _seed_price_list,
    _set_invoice_field,
    _set_invoice_price,
    _set_journey_product,
    _sku_task,
    _vehicle,
)
from test_uc03_sku_ladder import _booking_form, _correct_field

import audit_core.uc03_model_resolution as mr
from audit_core.uc03_sku_tasks import handle_sku_task_action

journey = base.journey

_THAR = [
    {"model": "THAR", "variant": "LX", "components": {"EX_SHOWROOM": "1400000"}},
    {"model": "THAR", "variant": "AX", "components": {"EX_SHOWROOM": "1300000"}},
]


def _legacy_pin(c, sku):
    c.execute(text("UPDATE auditcore.journey_products SET product_sku_id=:s, selection_status='TENTATIVE', "
                   "selection_method='MODEL_RESOLUTION_SYNC_V1', match_basis='AUTO_LEGACY' "
                   "WHERE tenant_id=:t AND journey_id=:j"),
              {"s": sku, "t": c.tenant_id, "j": c.journey_id})


def _open_tasks(c):
    return sorted(r[0] for r in c.execute(
        text("SELECT task_type FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j "
             "AND task_status IN ('READY','IN_PROGRESS','RETURNED','ACTION_COMPLETED','VERIFYING')"),
        {"t": c.tenant_id, "j": c.journey_id}).all())


def _tasks(c, task_type):
    return [dict(r) for r in c.execute(
        text("SELECT * FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j AND task_type=:k "
             "ORDER BY created_at_utc"),
        {"t": c.tenant_id, "j": c.journey_id, "k": task_type}).mappings().all()]


def _recheck(c, *, commit=True):
    return mr.recheck_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
                                       commit=commit)


def _sync(c):
    return mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")


# ── B2: reresolve never clears ────────────────────────────────────────────────
def test_s1_no_pricing_date_keeps_the_sku_and_asks_the_pc(journey):
    c = journey
    (lx,) = _seed_price_list(c, _THAR[:1])
    _booking_form(c, {"vehicle_model": "THAR", "vehicle_variant": "LX", "ex_showroom_price": "1400000"})
    _set_journey_product(c, "THAR", "LX")
    _legacy_pin(c, lx)
    c.execute(text("UPDATE auditcore.bookings SET booking_date=NULL WHERE tenant_id=:t AND journey_id=:j"),
              {"t": c.tenant_id, "j": c.journey_id})
    dry = _recheck(c, commit=False)
    assert dry["proposal"]["action"] == "UNCONFIRMED" and dry["proposal"]["tasks"] == ["SKU_NOT_CONFIRMED"]
    assert _vehicle(c)["match_basis"] == "AUTO_LEGACY"  # the dry run wrote nothing

    result = _recheck(c)
    v = _vehicle(c)
    assert v["product_sku_id"] == lx and v["match_basis"] == "AUTO_UNCONFIRMED", result
    task = _sku_task(c, "SKU_NOT_CONFIRMED")
    assert task["title"] == "SKU not clearly identified from the documents — confirm or set it"
    assert task["reference"]["skuId"] == str(lx)
    assert "no pricing date" in task["description"]


def test_s2_a_manual_capture_journey_keeps_its_sku(journey):
    c = journey
    (lx,) = _seed_price_list(c, _THAR[:1])
    _set_journey_product(c, "THAR", "LX")
    _legacy_pin(c, lx)
    _recheck(c)
    assert _vehicle(c)["product_sku_id"] == lx
    assert _open_tasks(c) == ["SKU_NOT_CONFIRMED"]


def test_a_manual_capture_pin_made_by_the_ladder_is_rechecked_from_its_own_words(journey):
    c = journey
    (lx,) = _seed_price_list(c, _THAR[:1])
    _set_journey_product(c, "THAR", "LX")
    assert _sync(c)["basis"] == "AUTO_PRICE_MISMATCH"  # no quoted price on a manual capture
    proposal = _recheck(c, commit=False)["proposal"]
    assert (proposal["action"], proposal["changes"], proposal["documentId"]) == ("BOOKING_PIN", False, None)
    _recheck(c)
    v = _vehicle(c)
    assert v["product_sku_id"] == lx and v["match_basis"] == "AUTO_PRICE_MISMATCH"
    assert "SKU_NOT_CONFIRMED" not in _open_tasks(c)


def test_s3_no_price_list_in_force_keeps_the_sku(journey):
    c = journey
    (lx,) = _seed_price_list(c, _THAR[:1], effective_from="CURRENT_DATE - 5")
    c.execute(text("UPDATE auditcore.bookings SET booking_date=CURRENT_DATE - 30 WHERE tenant_id=:t AND journey_id=:j"),
              {"t": c.tenant_id, "j": c.journey_id})
    _booking_form(c, {"vehicle_model": "THAR", "vehicle_variant": "LX", "ex_showroom_price": "1400000"})
    _set_journey_product(c, "THAR", "LX")
    _legacy_pin(c, lx)
    _recheck(c)
    v = _vehicle(c)
    assert v["product_sku_id"] == lx and v["match_basis"] == "AUTO_UNCONFIRMED"
    assert "price list" in _sku_task(c, "SKU_NOT_CONFIRMED")["description"]


def test_s7_an_error_leaves_the_journey_untouched(journey, monkeypatch):
    c = journey
    (lx,) = _seed_price_list(c, _THAR[:1])
    _booking_form(c, {"vehicle_model": "THAR", "vehicle_variant": "LX", "ex_showroom_price": "1400000"})
    _set_journey_product(c, "THAR", "LX")
    _legacy_pin(c, lx)
    calls = {"n": 0}
    real = mr._apply_booking

    def boom(*args, **kwargs):
        calls["n"] += 1
        real(*args, **kwargs)  # writes, then fails: the savepoint undoes it
        raise RuntimeError("statement timeout")

    monkeypatch.setattr(mr, "_apply_booking", boom)
    result = _recheck(c)
    assert result.get("error") and calls["n"] == 1
    v = _vehicle(c)
    assert v["product_sku_id"] == lx and v["match_basis"] == "AUTO_LEGACY"
    assert _open_tasks(c) == []


def test_reresolve_replaces_only_when_the_documents_name_a_different_sku(journey):
    c = journey
    lx, ax = _seed_price_list(c, _THAR)
    _booking_form(c, {"vehicle_model": "THAR", "vehicle_variant": "AX", "ex_showroom_price": "1300000"})
    _set_journey_product(c, "THAR", "LX")
    _legacy_pin(c, lx)
    # The documents name AX: the reresolve run moves the pin, and says so on
    # the Journey.
    proposal = _recheck(c, commit=False)["proposal"]
    assert (proposal["changes"], proposal["proposedBasis"], proposal["oldBasis"]) == (
        True, "AUTO_MATCHED", "AUTO_LEGACY")
    _recheck(c)
    v = _vehicle(c)
    assert v["product_sku_id"] == ax and v["match_basis"] == "AUTO_MATCHED"
    activity = c.execute(text("SELECT details FROM auditcore.p2_activity_events WHERE tenant_id=:t AND journey_id=:j "
                              "AND event_type='SKU_RESOLUTION_CHANGED'"),
                         {"t": c.tenant_id, "j": c.journey_id}).scalars().all()
    assert len(activity) == 1 and activity[0]["toBasis"] == "AUTO_MATCHED"


def test_a_normal_sync_leaves_a_pre_rule_pin_the_documents_do_not_name(journey):
    """Only the reresolve run re-checks AUTO_LEGACY pins (after its dry run
    was reviewed); a normal sync neither flags nor clears them."""
    c = journey
    (lx,) = _seed_price_list(c, _THAR[:1])
    _booking_form(c, {"vehicle_model": "THAR", "vehicle_variant": "AX7", "ex_showroom_price": "1400000"})
    _set_journey_product(c, "THAR", "LX")
    _legacy_pin(c, lx)
    _sync(c)
    v = _vehicle(c)
    assert v["product_sku_id"] == lx and v["match_basis"] == "AUTO_LEGACY"
    assert "SKU_NOT_CONFIRMED" not in _open_tasks(c)


# ── S4: a stale automatic pin ─────────────────────────────────────────────────
def test_s8_a_stale_automatic_pin_is_kept_marked_and_put_to_the_pc(journey):
    c = journey
    lx, _ax = _seed_price_list(c, _THAR)
    doc = _booking_form(c, {"vehicle_model": "THAR", "vehicle_variant": "LX", "ex_showroom_price": "1400000"})
    _sync(c)
    _correct_field(c, doc, "vehicle_variant", "AX7")  # the form now names no SKU
    _sync(c)
    v = _vehicle(c)
    assert v["product_sku_id"] == lx and v["match_basis"] == "AUTO_UNCONFIRMED"
    assert v["match_evidence"]["previousBasis"] == "AUTO_MATCHED"
    assert "AX7" in _sku_task(c, "SKU_NOT_CONFIRMED")["description"]
    # Corrected back: the words name it again, the question closes.
    _correct_field(c, doc, "vehicle_variant", "LX")
    _sync(c)
    assert _vehicle(c)["match_basis"] == "AUTO_MATCHED"
    assert _sku_task(c, "SKU_NOT_CONFIRMED")["task_status"] == "VERIFIED_COMPLETE"


def test_s5_an_unreadable_invoice_price_task_stays_open(journey):
    c = journey
    _seed_price_list(c, _THAR)
    doc = uuid4()
    _set_invoice_field(c, "model_name_raw", "THAR", doc=doc)
    _set_invoice_field(c, "variant_raw", "LX", doc=doc)
    _sync(c)
    _sync(c)
    task = _sku_task(c, "SKU_PRICE_MISMATCH")
    assert _vehicle(c)["match_basis"] == "AUTO_PRICE_MISMATCH" and task["task_status"] == "READY"
    assert task["title"] == "SKU price could not be checked: the vehicle invoice's ex-showroom was not read"


# ── B3 ────────────────────────────────────────────────────────────────────────
def test_s9_keep_this_vehicle_confirms_the_sku_the_task_names(journey):
    c = journey
    lx, ax = _seed_price_list(c, _THAR)
    _booking_form(c, {"vehicle_model": "THAR", "vehicle_variant": "LX", "ex_showroom_price": "1400000"})
    doc = uuid4()
    _set_invoice_field(c, "model_name_raw", "THAR", doc=doc)
    _set_invoice_field(c, "variant_raw", "AX", doc=doc)
    _set_invoice_field(c, "invoice_number", "INV-AX", doc=doc)
    _set_invoice_price(c, "1290000", doc=doc)
    _sync(c)
    assert _vehicle(c)["product_sku_id"] == lx
    task = dict(_sku_task(c, "SKU_PRICE_MISMATCH"))
    assert task["title"].startswith("Vehicle invoice names a different vehicle from the booking: THAR AX")
    handle_sku_task_action(c, tenant_id=c.tenant_id, task=task, action="ACCEPT", actor_id="pc",
                           actor_role_code="PC", comment=None)
    v = _vehicle(c)
    assert v["product_sku_id"] == ax and v["match_basis"] == "PC_CONFIRMED"
    events = c.execute(text("SELECT count(*) FROM auditcore.p2_task_events WHERE tenant_id=:t AND task_id=:k "
                            "AND event_type='COMPLETED'"), {"t": c.tenant_id, "k": task["task_id"]}).scalar_one()
    assert events == 1  # closed once, by the PC's action (review N3)


# ── B4 ────────────────────────────────────────────────────────────────────────
def _two_invoices(c):
    docs = {}
    for variant, price in (("LX", "1400000"), ("AX", "1300000")):
        doc = docs[variant] = uuid4()
        _set_invoice_field(c, "model_name_raw", "THAR", doc=doc)
        _set_invoice_field(c, "variant_raw", variant, doc=doc)
        _set_invoice_field(c, "invoice_number", f"INV-{variant}", doc=doc)
        _set_invoice_price(c, price, doc=doc)
    return docs


def test_s4_the_invoices_disagree_task_stays_closed_after_the_pc_picks(journey):
    c = journey
    lx, ax = _seed_price_list(c, _THAR)
    _two_invoices(c)
    _sync(c)
    task = dict(_sku_task(c, "SKU_INVOICES_DISAGREE"))
    assert set(task["allowed_actions"]) >= {"ACCEPT", "REJECT"}
    assert {o["skuId"] for o in task["reference"]["skuOptions"]} == {str(lx), str(ax)}
    with pytest.raises(ValueError):
        handle_sku_task_action(c, tenant_id=c.tenant_id, task=task, action="ACCEPT", actor_id="pc",
                               actor_role_code="PC", comment=None, details={"skuId": str(uuid4())})
    handle_sku_task_action(c, tenant_id=c.tenant_id, task=task, action="ACCEPT", actor_id="pc",
                           actor_role_code="PC", comment=None, details={"skuId": str(lx)})
    assert _vehicle(c)["product_sku_id"] == lx
    for _ in range(2):
        _sync(c)
    (only,) = _tasks(c, "SKU_INVOICES_DISAGREE")
    assert only["task_status"] == "VERIFIED_COMPLETE" and only["round_number"] == 1


def test_a_new_disagreement_on_other_documents_is_a_new_task(journey):
    c = journey
    _seed_price_list(c, _THAR)
    _two_invoices(c)
    _sync(c)
    first = _sku_task(c, "SKU_INVOICES_DISAGREE")
    # Without a decision, a third invoice changes the question: a new task,
    # the old one closes.
    doc = uuid4()
    _set_invoice_field(c, "model_name_raw", "THAR", doc=doc)
    _set_invoice_field(c, "variant_raw", "LX", doc=doc)
    _set_invoice_price(c, "1400000", doc=doc)
    _sync(c)
    status = {t["task_id"]: t["task_status"] for t in _tasks(c, "SKU_INVOICES_DISAGREE")}
    assert len(status) == 2 and status.pop(first["task_id"]) == "VERIFIED_COMPLETE"
    assert list(status.values()) == ["READY"]


def test_s6_the_invoice_price_task_stays_closed_after_the_pc_accepts_the_booking_one(journey):
    c = journey
    _seed_price_list(c, _THAR)
    _booking_form(c, {"vehicle_model": "THAR", "vehicle_variant": "LX", "ex_showroom_price": "1350000"})
    doc = uuid4()
    _set_invoice_field(c, "model_name_raw", "THAR", doc=doc)
    _set_invoice_field(c, "variant_raw", "LX", doc=doc)
    _set_invoice_price(c, "1350000", doc=doc)
    _sync(c)
    tasks = _tasks(c, "SKU_PRICE_MISMATCH")
    assert {t["reference"]["source"] for t in tasks} == {"BOOKING", "INVOICE"}
    booking_task = next(t for t in tasks if t["reference"]["source"] == "BOOKING")
    assert booking_task["title"] == "Confirm the model and price: master Rs 14,00,000.00 vs booking Rs 13,50,000.00"
    handle_sku_task_action(c, tenant_id=c.tenant_id, task=booking_task, action="ACCEPT", actor_id="pc",
                           actor_role_code="PC", comment=None)
    for _ in range(2):
        _sync(c)
    after = _tasks(c, "SKU_PRICE_MISMATCH")
    assert [(t["task_status"], t["round_number"]) for t in after] == [("VERIFIED_COMPLETE", 1)] * 2
    assert _vehicle(c)["match_basis"] == "PC_CONFIRMED"


def test_a_price_task_is_keyed_by_its_prices(journey):
    """The same invoice at a new price is a new question (B4); the old one
    closes as superseded."""
    c = journey
    _seed_price_list(c, _THAR)
    doc = uuid4()
    _set_invoice_field(c, "model_name_raw", "THAR", doc=doc)
    _set_invoice_field(c, "variant_raw", "LX", doc=doc)
    _set_invoice_price(c, "1350000", doc=doc)
    _sync(c)
    first = _sku_task(c, "SKU_PRICE_MISMATCH")
    c.execute(text("UPDATE auditcore.journey_document_extracted_fields SET effective_value = to_jsonb('1150000'::text) "
                   "WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d AND field_key='taxable_amount'"),
              {"t": c.tenant_id, "j": c.journey_id, "d": doc})
    _sync(c)
    status = {t["task_id"]: t["task_status"] for t in _tasks(c, "SKU_PRICE_MISMATCH")}
    assert len(status) == 2 and status.pop(first["task_id"]) == "VERIFIED_COMPLETE"
    assert list(status.values()) == ["READY"]


# ── S6: several booking forms, registration records ───────────────────────────
def test_booking_forms_naming_different_vehicles_are_a_pc_task_not_newest_wins(journey):
    c = journey
    _lx, ax = _seed_price_list(c, _THAR)
    _booking_form(c, {"vehicle_model": "THAR", "vehicle_variant": "LX", "ex_showroom_price": "1400000"})
    _booking_form(c, {"vehicle_model": "THAR", "vehicle_variant": "AX", "ex_showroom_price": "1300000"})
    result = _sync(c)
    assert result.get("raised") is True and (_vehicle(c) or {}).get("product_sku_id") is None, result
    task = dict(_sku_task(c, "SKU_BOOKING_FORMS_DISAGREE"))
    handle_sku_task_action(c, tenant_id=c.tenant_id, task=task, action="ACCEPT", actor_id="pc",
                           actor_role_code="PC", comment=None, details={"skuId": str(ax)})
    assert _vehicle(c)["product_sku_id"] == ax
    _sync(c)
    assert _sku_task(c, "SKU_BOOKING_FORMS_DISAGREE")["task_status"] == "VERIFIED_COMPLETE"


def test_conflicting_registration_records_leave_the_category_unknown(journey):
    c = journey
    rows = [{"customer_type_code": None, "registration_type_code": "COMMERCIAL", "registration_category_code": None},
            {"customer_type_code": None, "registration_type_code": "PRIVATE", "registration_category_code": None}]

    class _Result:
        def mappings(self):
            return self

        def all(self):
            return rows

    class _Conn:
        def execute(self, *_a, **_k):
            return _Result()

    reg = mr._journey_registration(_Conn(), tenant_id=c.tenant_id, journey_id=c.journey_id)
    assert reg["category"] is None and reg["conflict"] is True
    rows.pop()
    assert mr._journey_registration(_Conn(), tenant_id=c.tenant_id, journey_id=c.journey_id)["category"] == "COMMERCIAL"


# ── 2026-10-21: a DMS / Tally invoice always re-runs the SKU ──────────────────
def test_a_dms_or_tally_invoice_materialised_reruns_the_sku(journey, monkeypatch):
    from audit_core import uc03_invoice_materialization as im

    calls: list[str] = []
    monkeypatch.setattr(im, "_confirm_sku_against_invoice",
                        lambda connection, **kw: calls.append(str(kw["journey_id"])) or {"basis": "AUTO_MATCHED"})

    def document(kind):
        return SimpleNamespace(documentTypeKey=kind, extractionState="READY", documentId=uuid4(), evidenceId=None,
                               fields=[SimpleNamespace(fieldKey="invoice_number", value="INV-1",
                                                       effectiveValue="INV-1", extractedValue="INV-1")])

    c = journey
    for kind, expected in (("accessory_invoice_dms", 0), ("tax_invoice_tally", 1), ("customer_invoice_dms", 1)):
        calls.clear()
        result = im.materialize_reviewed_invoices(c, tenant_id=c.tenant_id, journey_id=c.journey_id,
                                                  documents=[document(kind)], actor_id="pc")
        assert result["invoices"] == 1 and len(calls) == expected, kind
        if expected:
            assert result["skuResolution"] == "AUTO_MATCHED"


# ── B1: migration 0152 keeps the PC's decisions ──────────────────────────────
def _migration():
    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0152_sku_match_basis.py"
    spec = importlib.util.spec_from_file_location("m0152", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0152_keeps_a_pc_pick_saved_under_the_resolvers_method(journey):
    c = journey
    lx, ax = _seed_price_list(c, _THAR)
    _set_journey_product(c, "THAR", "LX")
    # Before #441 the PC's confirm saved MODEL_RESOLUTION_SYNC_V1; the trace
    # is the MODEL_NOT_IDENTIFIED finding the PC resolved.
    mr.confirm_model_resolution_sku(c, tenant_id=c.tenant_id, journey_id=c.journey_id, product_sku_id=lx,
                                    actor_id="pc-1", correlation_id="")
    finding = c.execute(text("SELECT audit_finding_id FROM auditcore.audit_findings WHERE tenant_id=:t "
                             "AND journey_id=:j AND finding_type_code='MODEL_NOT_IDENTIFIED'"),
                        {"t": c.tenant_id, "j": c.journey_id}).scalar_one_or_none()
    if finding is None:
        _sync_flag = mr._machine_flag  # a finding for the PC to resolve, as live
        finding = _sync_flag(c, tenant_id=c.tenant_id, journey_id=c.journey_id, stage_code="BOOKING",
                             rule_key="MODEL_NOT_IDENTIFIED:BOOKING", finding_type="MODEL_NOT_IDENTIFIED",
                             severity="MEDIUM", title="t", description="d", correlation_id="", safe_payload={},
                             task_payload_extra={})
        mr._resolve_open_flag(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
                              actor_id="pc-1")
    c.execute(text("UPDATE auditcore.journey_products SET selection_method='MODEL_RESOLUTION_SYNC_V1', "
                   "selection_status='CONFIRMED', match_basis=NULL, match_evidence=NULL "
                   "WHERE tenant_id=:t AND journey_id=:j"), {"t": c.tenant_id, "j": c.journey_id})
    _migration().classify_existing_pins(lambda sql: c.execute(text(sql)))
    v = _vehicle(c)
    assert (v["product_sku_id"], v["selection_status"], v["match_basis"]) == (lx, "CONFIRMED", "PC_CONFIRMED")
    assert v["match_evidence"]["trace"] == "MODEL_NOT_IDENTIFIED_RESOLVED"
    assert ax != lx


def test_0152_keeps_an_accepted_closest_match_and_labels_the_rest_legacy(journey):
    c = journey
    _lx, ax = _seed_price_list(c, _THAR)
    _set_journey_product(c, "THAR", "LX")
    c.execute(text("UPDATE auditcore.journey_products SET product_sku_id=:s, selection_method='MODEL_RESOLUTION_SYNC_V1', "
                   "selection_status='CONFIRMED', match_basis=NULL WHERE tenant_id=:t AND journey_id=:j"),
              {"s": ax, "t": c.tenant_id, "j": c.journey_id})
    c.execute(text("""
        INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, task_type, category, origin_kind, source_type,
            dedupe_key, title, description, reference, assigned_role_code, completion_protocol, task_status,
            completion_result, verified_at_utc)
        VALUES (:t, :j, 'SKU_CLOSEST_MATCH_CONFIRM', 'CORRECTION_APPROVAL', 'SYSTEM', 'RESOLVER', :k, 't', 'd',
                CAST(:ref AS jsonb), 'PC', 'MACHINE_VERIFIED', 'VERIFIED_COMPLETE',
                '{"outcome": "SKU_ACCEPTED", "by": "pc-2"}'::jsonb, now())
    """), {"t": c.tenant_id, "j": c.journey_id, "k": f"sku-closest:{c.journey_id}",
           "ref": f'{{"suggestedSkuId": "{ax}"}}'})
    _migration().classify_existing_pins(lambda sql: c.execute(text(sql)))
    v = _vehicle(c)
    assert (v["selection_status"], v["match_basis"]) == ("CONFIRMED", "PC_CONFIRMED")
    assert v["match_evidence"]["trace"] == "SKU_CLOSEST_MATCH_ACCEPTED"

    # Without a trace the resolver's pin is AUTO_LEGACY (TENTATIVE).
    c.execute(text("DELETE FROM auditcore.p2_task_events WHERE tenant_id=:t AND journey_id=:j"),
              {"t": c.tenant_id, "j": c.journey_id})
    c.execute(text("DELETE FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j"),
              {"t": c.tenant_id, "j": c.journey_id})
    c.execute(text("UPDATE auditcore.journey_products SET selection_status='CONFIRMED', match_basis=NULL "
                   "WHERE tenant_id=:t AND journey_id=:j"), {"t": c.tenant_id, "j": c.journey_id})
    _migration().classify_existing_pins(lambda sql: c.execute(text(sql)))
    v = _vehicle(c)
    assert (v["selection_status"], v["match_basis"]) == ("TENTATIVE", "AUTO_LEGACY")
    assert v["match_evidence"]["statusBefore"] == "CONFIRMED"
