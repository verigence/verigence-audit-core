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
    assert dict(discounts) == {"CORPORATE": 40000, "LOYALTY": 5000}
    assert dict(addons) == {"EXTENDED_WARRANTY": 18000, "ACCESSORIES_TOTAL": 12000}
