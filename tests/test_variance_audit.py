"""The variance audit sets the Deal's figure beside the list's stored copy."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import audit_core.uc03_variance_audit as audit


def _deal(variance="-3000"):
    return {
        "standardUnavailable": None,
        "categories": [{"code": "VEHICLE", "components": [
            {"key": "ex_showroom_price", "standard": "1000000", "current": "1000050", "flags": []},
            {"key": "accessories_cost", "standard": None, "current": "5000", "flags": ["x"]}]}],
        "discounts": [{"key": "CORPORATE", "standard": "20000", "current": "17000", "flags": []}],
        "summary": {"variance": {"currentVsStandard": variance}, "net": {"standard": "980000", "current": "983000"},
                    "invoiceIsSourceOfTruth": True, "vehicleInvoiceOnFile": True},
    }


def test_only_components_off_the_price_list_by_more_than_100_are_listed(monkeypatch):
    monkeypatch.setattr(audit, "deal", lambda *a, **k: _deal())
    monkeypatch.setattr(audit, "_stored_copy", lambda *a, **k: (Decimal(-3000), True))
    item = audit.audit_journey(object(), tenant_id="t", journey_id=uuid4())
    assert [d["key"] for d in item["differing"]] == ["accessories_cost", "CORPORATE"]
    assert item["listAgreesWithDeal"] is True and item["dealVariance"] == Decimal(-3000)


def test_a_stale_or_different_stored_copy_is_reported(monkeypatch):
    monkeypatch.setattr(audit, "deal", lambda *a, **k: _deal("-3000"))
    monkeypatch.setattr(audit, "_stored_copy", lambda *a, **k: (Decimal(271181), False))
    item = audit.audit_journey(object(), tenant_id="t", journey_id=uuid4())
    assert item["listAgreesWithDeal"] is False and item["storedIsCurrent"] is False
