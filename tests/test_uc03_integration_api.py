"""The machine-facing Booking helpers.

The money rule is the point of these: a dealership application calls
``/minimum-payment`` to decide whether a customer takes a place in its queue,
so the answer has to be the same one the Booking flow would reach from the same
receipts -- duplicates counted once, the confirming receipt dated
chronologically, and no tolerance on the threshold.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from audit_core.dependencies import get_connection
from audit_core.main import app
from audit_core.security import ServiceIntegrationPrincipal
from audit_core.uc03_pc_booking_documents import require_audit_service_principal


class _StubConnection:
    """Enough of a connection for the stateless paths: the tenant context is set
    on it and nothing else is read, because the caller supplies the receipts."""

    def execute(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("the stateless paths must not query the database")


@pytest.fixture(autouse=True)
def _as_service():
    app.dependency_overrides[require_audit_service_principal] = lambda: ServiceIntegrationPrincipal(subject="vineeti")
    app.dependency_overrides[get_connection] = lambda: _StubConnection()
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _no_tenant_context(monkeypatch):
    monkeypatch.setattr("audit_core.uc03_integration_api.set_tenant_context", lambda *_a, **_k: None)


def _check(receipts: list[dict], minimum: str = "21000") -> dict:
    response = TestClient(app).post(
        "/v1/internal/booking/minimum-payment",
        json={"tenantId": "aditya", "minimumAmount": minimum, "receipts": receipts},
        headers={"Authorization": "Bearer x"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_a_single_receipt_for_the_whole_minimum_confirms_it():
    body = _check([{"reference": "R1", "amount": "21000", "receiptDate": "2026-10-01", "receiptNumber": "RC-1"}])
    assert body["met"] is True
    assert Decimal(body["totalCounted"]) == Decimal("21000")
    assert body["confirmingReference"] == "R1"
    assert body["confirmingDate"] == "2026-10-01"


def test_part_payments_add_up_and_the_one_that_crosses_the_line_confirms_it():
    body = _check(
        [
            {"reference": "R1", "amount": "5000", "receiptDate": "2026-10-01", "receiptNumber": "RC-1"},
            {"reference": "R2", "amount": "6000", "receiptDate": "2026-10-03", "receiptNumber": "RC-2"},
            {"reference": "R3", "amount": "10000", "receiptDate": "2026-10-07", "receiptNumber": "RC-3"},
        ]
    )
    assert body["met"] is True
    assert Decimal(body["totalCounted"]) == Decimal("21000")
    # The third one takes it over; that is the date the booking is confirmed on.
    assert body["confirmingReference"] == "R3"
    assert body["confirmingDate"] == "2026-10-07"


def test_receipts_are_counted_in_date_order_whatever_order_they_arrive_in():
    body = _check(
        [
            {"reference": "LATE", "amount": "20000", "receiptDate": "2026-10-09", "receiptNumber": "RC-9"},
            {"reference": "EARLY", "amount": "21000", "receiptDate": "2026-10-02", "receiptNumber": "RC-2"},
        ]
    )
    # The earlier receipt already met the minimum on its own, so it is the one
    # that confirms -- not whichever the caller happened to list first.
    assert body["confirmingReference"] == "EARLY"
    assert body["confirmingDate"] == "2026-10-02"


# The same physical receipt reaching us twice -- a consultant photographing the
# dealer receipt and then the payment confirmation for the same money -- must
# not count twice. That is the whole reason this is not a bare sum.
def test_the_same_receipt_sent_twice_is_counted_once():
    body = _check(
        [
            {"reference": "FIRST", "amount": "21000", "receiptDate": "2026-10-01", "receiptNumber": "RC-77"},
            {"reference": "AGAIN", "amount": "21000", "receiptDate": "2026-10-01", "receiptNumber": "RC-77"},
        ],
        minimum="30000",
    )
    assert body["met"] is False
    assert Decimal(body["totalCounted"]) == Decimal("21000")
    assert body["countedReferences"] == ["FIRST"]
    assert body["duplicateReferences"] == ["AGAIN"]


def test_receipts_without_a_number_are_matched_on_amount_and_date_instead():
    body = _check(
        [
            {"reference": "A", "amount": "21000", "receiptDate": "2026-10-01"},
            {"reference": "B", "amount": "21000", "receiptDate": "2026-10-01"},
        ],
        minimum="30000",
    )
    assert Decimal(body["totalCounted"]) == Decimal("21000")
    assert body["duplicateReferences"] == ["B"]


def test_two_genuine_payments_of_the_same_amount_on_different_days_both_count():
    body = _check(
        [
            {"reference": "A", "amount": "11000", "receiptDate": "2026-10-01", "receiptNumber": "RC-1"},
            {"reference": "B", "amount": "11000", "receiptDate": "2026-10-05", "receiptNumber": "RC-2"},
        ]
    )
    assert body["met"] is True
    assert Decimal(body["totalCounted"]) == Decimal("22000")
    assert body["duplicateReferences"] == []


# No tolerance: the Booking rule is a bare >=, and a rupee short is short.
def test_a_rupee_short_is_not_met():
    body = _check([{"reference": "R1", "amount": "20999", "receiptDate": "2026-10-01", "receiptNumber": "RC-1"}])
    assert body["met"] is False
    assert body["confirmingReference"] is None


def test_exactly_the_minimum_is_met():
    assert _check([{"reference": "R1", "amount": "21000", "receiptDate": "2026-10-01", "receiptNumber": "RC-1"}])["met"] is True


def test_only_a_registered_service_identity_may_ask():
    app.dependency_overrides.pop(require_audit_service_principal, None)
    response = TestClient(app).post(
        "/v1/internal/booking/minimum-payment",
        json={"tenantId": "aditya", "receipts": [{"reference": "R1", "amount": "21000"}]},
    )
    assert response.status_code == 401
    assert response.json()["status"] == 401


class _Resolved:
    """Shapes a price-master row the way the ladder hands it back."""

    @staticmethod
    def row(sku_code: str = "SKU-1", colour: str | None = "EVEREST WHITE") -> dict:
        from uuid import uuid4

        return {
            "product_sku_id": uuid4(),
            "sku_code": sku_code,
            "model_name": "SCORPIO N",
            "variant_name": "Z8L",
            "colour_name": colour,
            "master_ex_showroom": Decimal("1800000"),
            "master_total_individual": Decimal("2100000"),
        }


def _resolve(monkeypatch, result: dict) -> dict:
    monkeypatch.setattr("audit_core.uc03_integration_api.resolve_model_text", lambda *_a, **_k: result)
    response = TestClient(app).post(
        "/v1/internal/booking/sku-resolution",
        json={"tenantId": "aditya", "model": "SCORPIO N", "variant": "Z8L", "colour": "EVEREST WHITE"},
        headers={"Authorization": "Bearer x"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_one_matching_sku_comes_back_resolved(monkeypatch):
    body = _resolve(monkeypatch, {"matched": [_Resolved.row()], "matchStage": "ATTRIBUTE_DECOMPOSITION"})
    assert body["outcome"] == "RESOLVED"
    assert body["matchStage"] == "ATTRIBUTE_DECOMPOSITION"
    assert body["sku"]["skuCode"] == "SKU-1"


# Several matches is an answer, not a failure — and the caller is told which
# ones rather than being handed a guess.
def test_several_matching_skus_come_back_ambiguous_with_the_candidates(monkeypatch):
    body = _resolve(
        monkeypatch,
        {"matched": [_Resolved.row("SKU-1", "EVEREST WHITE"), _Resolved.row("SKU-2", "STEALTH BLACK")], "matchStage": "VARIANT_COLOUR"},
    )
    assert body["outcome"] == "AMBIGUOUS"
    assert body["sku"] is None
    assert [c["skuCode"] for c in body["candidates"]] == ["SKU-1", "SKU-2"]


def test_nothing_in_the_masters_is_reported_as_none_rather_than_an_error(monkeypatch):
    body = _resolve(monkeypatch, {"matched": [], "matchStage": "NONE"})
    assert body["outcome"] == "NONE"
    assert body["candidates"] == []


def test_a_tenant_with_no_price_list_is_told_so(monkeypatch):
    body = _resolve(monkeypatch, {"skipped": True, "reason": "no_effective_price_list"})
    assert body["outcome"] == "UNAVAILABLE"
    assert body["reason"] == "no_effective_price_list"


def test_a_long_shortlist_is_capped(monkeypatch):
    body = _resolve(monkeypatch, {"matched": [_Resolved.row(f"SKU-{i}") for i in range(25)], "matchStage": "CATALOGUE_SEARCH"})
    assert body["outcome"] == "AMBIGUOUS"
    assert len(body["candidates"]) == 10
