"""Review of #471 (2026-10-22): a SKU task the documents no longer call for
is closed by the next run, never left READY for a PC / Team Lead to accept.

* A -- SKU_INVOICE_REVIEW names the invoice's SKU; once no invoice names it
  (the invoice was corrected) the task closes.
* B -- SKU_CLOSEST_MATCH_CONFIRM; once the suggestion no longer stands
  (the booking text now fits several SKUs) the task closes.
* C -- the booking form's SKU_PRICE_MISMATCH closes when the booking form no
  longer names the pinned SKU (the KEEP path).
"""
from __future__ import annotations

import test_uc03_model_resolution as base
from sqlalchemy import text
from test_uc03_model_resolution import (
    _seed_price_list,
    _set_commercial,
    _set_invoice_field,
    _set_invoice_price,
    _set_journey_product,
    _vehicle,
)
from test_uc03_sku_ladder import _booking_form, _correct_field

import audit_core.uc03_model_resolution as mr

journey = base.journey
mahindra_journey = base.mahindra_journey

_THAR3 = [
    {"model": "THAR", "variant": "RX", "components": {"EX_SHOWROOM": "1200000"}},
    {"model": "THAR", "variant": "LX", "components": {"EX_SHOWROOM": "1400000"}},
    {"model": "THAR", "variant": "AX", "components": {"EX_SHOWROOM": "1300000"}},
]
_OPEN = "('READY','IN_PROGRESS','RETURNED','ACTION_COMPLETED','VERIFYING')"


def _open(c, task_type):
    return [dict(r) for r in c.execute(
        text(f"SELECT * FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j AND task_type=:k "
             f"AND task_status IN {_OPEN} ORDER BY created_at_utc"),
        {"t": c.tenant_id, "j": c.journey_id, "k": task_type}).mappings().all()]


def _all(c, task_type):
    return [dict(r) for r in c.execute(
        text("SELECT * FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j AND task_type=:k "
             "ORDER BY created_at_utc"),
        {"t": c.tenant_id, "j": c.journey_id, "k": task_type}).mappings().all()]


def _sync(c):
    return mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")


def _confirm(c, sku_id):
    c.execute(text("UPDATE auditcore.journey_products SET product_sku_id=:s, selection_status='CONFIRMED', "
                   "selection_method='PC_CONFIRMED', match_basis='PC_CONFIRMED' "
                   "WHERE tenant_id=:t AND journey_id=:j"), {"s": sku_id, "t": c.tenant_id, "j": c.journey_id})


def _invoice(c, variant, price):
    """Replace the Journey's one vehicle invoice's reading (a correction)."""
    doc = getattr(c, "invoice_doc", None)
    if doc is not None:
        c.execute(text("DELETE FROM auditcore.journey_document_extracted_fields WHERE tenant_id=:t "
                       "AND journey_id=:j AND di_document_id=:d"),
                  {"t": c.tenant_id, "j": c.journey_id, "d": doc})
    _set_invoice_field(c, "model_name_raw", "THAR")
    _set_invoice_field(c, "variant_raw", variant)
    _set_invoice_price(c, price)


# ── A ─────────────────────────────────────────────────────────────────────────
def test_a_corrected_invoice_closes_the_review_task_of_the_old_sku(journey):
    c = journey
    rx, lx, ax = _seed_price_list(c, _THAR3)
    _set_journey_product(c, "THAR", "RX")
    _confirm(c, rx)
    _invoice(c, "LX", "1400000")
    _sync(c)
    (lx_task,) = _open(c, "SKU_INVOICE_REVIEW")
    assert lx_task["reference"]["invoiceSkuId"] == str(lx)

    _invoice(c, "AX", "1300000")
    _sync(c)
    (ax_task,) = _open(c, "SKU_INVOICE_REVIEW")
    assert ax_task["reference"]["invoiceSkuId"] == str(ax)
    closed = next(t for t in _all(c, "SKU_INVOICE_REVIEW") if t["task_id"] == lx_task["task_id"])
    assert closed["task_status"] == "VERIFIED_COMPLETE"
    assert closed["completion_result"]["reason"] == "No vehicle invoice names this SKU at its master price any more"
    assert _vehicle(c)["product_sku_id"] == rx

    # The task of the SKU an invoice still names stays open on the next run.
    _sync(c)
    assert [t["task_id"] for t in _open(c, "SKU_INVOICE_REVIEW")] == [ax_task["task_id"]]


def test_a_the_stale_task_can_no_longer_be_accepted_into_a_sku_no_invoice_names(journey):
    c = journey
    rx, lx, _ax = _seed_price_list(c, _THAR3)
    _set_journey_product(c, "THAR", "RX")
    _confirm(c, rx)
    _invoice(c, "LX", "1400000")
    _sync(c)
    _invoice(c, "AX", "1300000")
    _sync(c)
    assert all(t["reference"]["invoiceSkuId"] != str(lx) for t in _open(c, "SKU_INVOICE_REVIEW"))


# ── B ─────────────────────────────────────────────────────────────────────────
def _closest_setup(c):
    ax5l, ax7l = _seed_price_list(c, [
        {"model": "XUV 3XO EV", "variant": "AX5L", "fuel": "ELECTRIC", "trim": "AX5L",
         "components": {"EX_SHOWROOM": "1500000"}},
        {"model": "XUV 3XO EV", "variant": "AX7L", "fuel": "ELECTRIC", "trim": "AX7L",
         "components": {"EX_SHOWROOM": "1700000"}},
    ])
    _set_journey_product(c, "XUV300", "W7L EV")
    _set_commercial(c, "ex_showroom_price", "1700000")
    return ax5l, ax7l


def test_b_the_closest_match_task_closes_when_the_suggestion_no_longer_stands(mahindra_journey):
    c = mahindra_journey
    _ax5l, ax7l = _closest_setup(c)
    _sync(c)
    (task,) = _open(c, "SKU_CLOSEST_MATCH_CONFIRM")
    assert task["reference"]["suggestedSkuId"] == str(ax7l)

    c.execute(text("UPDATE auditcore.journey_products SET model_name_snapshot='XUV 3XO EV', "
                   "variant_name_snapshot=NULL WHERE tenant_id=:t AND journey_id=:j"),
              {"t": c.tenant_id, "j": c.journey_id})
    result = _sync(c)
    assert result.get("raised") is True and result.get("wordStatus") == "AMBIGUOUS", result
    assert _open(c, "SKU_CLOSEST_MATCH_CONFIRM") == []
    closed = _all(c, "SKU_CLOSEST_MATCH_CONFIRM")[0]
    assert closed["task_status"] == "VERIFIED_COMPLETE"
    assert _vehicle(c)["product_sku_id"] is None  # nothing was pinned


def test_b_the_closest_match_task_stays_while_the_same_suggestion_stands(mahindra_journey):
    c = mahindra_journey
    _closest_setup(c)
    _sync(c)
    (task,) = _open(c, "SKU_CLOSEST_MATCH_CONFIRM")
    _sync(c)
    assert [t["task_id"] for t in _open(c, "SKU_CLOSEST_MATCH_CONFIRM")] == [task["task_id"]]


# ── C ─────────────────────────────────────────────────────────────────────────
def test_c_the_booking_price_task_closes_when_only_an_invoice_names_the_sku(journey):
    c = journey
    (rx,) = _seed_price_list(c, [{"model": "THAR", "variant": "RX", "components": {"EX_SHOWROOM": "1300000"}}])
    doc = _booking_form(c, {"vehicle_model": "THAR", "vehicle_variant": "RX", "ex_showroom_price": "1250000"})
    _sync(c)
    v = _vehicle(c)
    assert (v["product_sku_id"], v["match_basis"]) == (rx, "AUTO_PRICE_MISMATCH")
    (booking_task,) = _open(c, "SKU_PRICE_MISMATCH")
    assert booking_task["severity"] == "HIGH"

    _correct_field(c, doc, "vehicle_model", "BOLERO UNKNOWN")  # the booking form no longer names the SKU
    _invoice(c, "RX", "1260000")
    _sync(c)
    assert _vehicle(c)["product_sku_id"] == rx
    open_price = _open(c, "SKU_PRICE_MISMATCH")
    assert [(t["severity"], t["reference"].get("source")) for t in open_price] == [("MEDIUM", "INVOICE")], open_price
    assert next(t for t in _all(c, "SKU_PRICE_MISMATCH") if t["task_id"] == booking_task["task_id"])[
        "task_status"] == "VERIFIED_COMPLETE"
