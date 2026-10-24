"""The invoice rescan re-reads every Journey's stored invoices: a dry run
writes nothing, a commit re-sets the figures and tells downstream."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import audit_core.uc03_invoice_rescan as rescan
from audit_core.uc03_invoice_materialization import read_invoice

_TENANT = "t1"


def _reading() -> dict:
    # GST only inside the total: the shape that used to lose the price.
    return read_invoice("tax_invoice_tally", {
        "invoice_purpose": "VEHICLE_SALE", "gross_amount_before_discount": "1050000",
        "invoice_discount_amount": "50000", "tcs_amount": "10000", "grand_total_amount": "1290000"},
        document_id=uuid4(), evidence_id=uuid4())


def _wire(monkeypatch, *, before, phase_two=True):
    calls: dict[str, list] = {"source": [], "sync": [], "notes": []}
    monkeypatch.setattr(rescan, "load_invoice_readings", lambda *a, **k: [_reading()])
    monkeypatch.setattr(rescan, "plausible_from", lambda *a, **k: None)
    monkeypatch.setattr(rescan, "_ex_showroom_now", lambda *a, **k: before)
    monkeypatch.setattr(rescan, "_on_phase_two", lambda *a, **k: phase_two)
    monkeypatch.setattr(rescan, "record_source_value", lambda *a, **k: calls["source"].append(k["component_key"]))
    monkeypatch.setattr(rescan, "sync_invoice_actuals", lambda *a, **k: calls["sync"].append(k["journey_id"]))
    import audit_core.uc03_p2_runtime as runtime
    monkeypatch.setattr(runtime, "note_facts_changed", lambda *a, **k: calls["notes"].append(k["reason"]))
    return calls


def test_dry_run_reports_the_figure_and_writes_nothing(monkeypatch):
    calls = _wire(monkeypatch, before=None)
    item = rescan.rescan_journey(object(), tenant_id=_TENANT, journey_id=uuid4(), tag="t", commit=False)
    assert item["before"] is None and item["after"] == Decimal("1344000.00") and item["changed"] is True
    assert calls == {"source": [], "sync": [], "notes": []}


def test_commit_resets_the_figures_and_tells_downstream(monkeypatch):
    calls = _wire(monkeypatch, before=None)
    rescan.rescan_journey(object(), tenant_id=_TENANT, journey_id=uuid4(), tag="t-commit", commit=True)
    assert "ex_showroom_price" in calls["source"] and len(calls["sync"]) == 1
    assert calls["notes"] == ["invoice_rescan"]


def test_a_figure_already_right_is_not_a_change(monkeypatch):
    _wire(monkeypatch, before=Decimal("1344000.00"))
    item = rescan.rescan_journey(object(), tenant_id=_TENANT, journey_id=uuid4(), tag="t", commit=False)
    assert item["changed"] is False


def test_a_phase_one_journey_is_not_given_a_runtime_row(monkeypatch):
    calls = _wire(monkeypatch, before=None, phase_two=False)
    rescan.rescan_journey(object(), tenant_id=_TENANT, journey_id=uuid4(), tag="t-commit", commit=True)
    assert calls["notes"] == []
