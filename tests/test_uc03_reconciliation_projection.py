from __future__ import annotations

from uuid import uuid4

from audit_core import uc03_v2_review_materialization as materialization


def test_discount_and_addon_field_maps_are_disjoint_and_canonical() -> None:
    discount_fields = set(materialization._DISCOUNT_KEY_BY_FIELD)
    addon_fields = set(materialization._ADDON_TYPE_BY_FIELD)
    assert discount_fields.isdisjoint(addon_fields)
    # canonical values are upper snake case, no raw DI field names leak through
    for value in (
        *materialization._DISCOUNT_KEY_BY_FIELD.values(),
        *materialization._ADDON_TYPE_BY_FIELD.values(),
    ):
        assert value == value.upper()
        assert not value.endswith("_AMOUNT")


def test_projection_routes_reviewed_fields_to_canonical_keys(monkeypatch) -> None:
    tenant_id = "tenant-x"
    journey_id = uuid4()
    evidence_id = uuid4()
    discounts: list[tuple[str, object]] = []
    addons: list[tuple[str, object]] = []

    monkeypatch.setattr(
        materialization,
        "_upsert_evidence_discount_application",
        lambda connection, *, tenant_id, journey_id, discount_key, amount, evidence_id: (
            discounts.append((discount_key, amount))
        ),
    )
    monkeypatch.setattr(
        materialization,
        "_upsert_evidence_journey_addon",
        lambda connection, *, tenant_id, journey_id, addon_type_code, amount, evidence_id: (
            addons.append((addon_type_code, amount))
        ),
    )

    values = {
        "corporate_discount_amount": 40000,
        "loyalty_discount_amount": 5000,
        "extended_warranty_amount": 18000,
        "accessories_cost": 12000,
        # unmapped / absent fields must be ignored
        "ex_showroom_price": 900000,
        "other_discount_amount": None,
    }

    d, a = materialization._materialize_reconciliation_projections(
        connection=object(),
        tenant_id=tenant_id,
        journey_id=journey_id,
        evidence_id=evidence_id,
        values=values,
    )

    assert d == 2
    assert a == 2
    # 2026-10-20 product owner rules: the canonical keys the reconciliation
    # writes, never the legacy CORPORATE / LOYALTY twins.
    assert dict(discounts) == {"CORPORATE_PRIVILEGE": 40000, "WELCOME_BONUS": 5000}
    assert dict(addons) == {"EXTENDED_WARRANTY": 18000, "ACCESSORIES_TOTAL": 12000}


def test_projection_never_writes_the_declared_total_and_keeps_mr_apart(monkeypatch) -> None:
    """The booking form's declared total discount is not a discount row (it
    holds the components); MR is its own key; two fields that both mean
    "Other scheme" are written once, added."""
    discounts: list[tuple[str, object]] = []
    monkeypatch.setattr(
        materialization,
        "_upsert_evidence_discount_application",
        lambda connection, *, tenant_id, journey_id, discount_key, amount, evidence_id: (
            discounts.append((discount_key, amount))
        ),
    )
    monkeypatch.setattr(materialization, "_upsert_evidence_journey_addon", lambda *a, **k: None)
    d, _ = materialization._materialize_reconciliation_projections(
        connection=object(),
        tenant_id="tenant-x",
        journey_id=uuid4(),
        evidence_id=None,
        values={
            "discount_amount": 80000,
            "sales_discount_amount": 30000,
            "mr_discount_amount": 10000,
            "oem_referral_discount_amount": 2000,
            "other_discount_amount": 3000,
        },
    )
    assert d == 3
    assert dict(discounts) == {"CASH_DISCOUNT": 30000, "MANAGEMENT_REFERRAL": 10000, "OTHER_SCHEME": 5000}
    assert "discount_amount" not in materialization._DISCOUNT_KEY_BY_FIELD
