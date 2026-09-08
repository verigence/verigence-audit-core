from __future__ import annotations

import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

import audit_core.uc03_deal_reconciliation as dr


# ── unit: reviewed booking discount fields -> OEM benefit keys ────────────────
def test_actual_discounts_collapse_onto_benefit_keys() -> None:
    out = dr._actual_discounts_by_benefit(
        {
            "discount_amount": "5000",
            "sales_discount_amount": "3000",   # also CASH_DISCOUNT — sums
            "bonus_amount": "10000",           # EXCHANGE_BONUS
            "corporate_discount_amount": "20000",
        }
    )
    assert out == {
        "CASH_DISCOUNT": Decimal(8000),
        "EXCHANGE_BONUS": Decimal(10000),
        "CORPORATE_PRIVILEGE": Decimal(20000),
    }


def test_actual_discounts_skip_zero_blank_and_missing() -> None:
    out = dr._actual_discounts_by_benefit(
        {"discount_amount": "0", "bonus_amount": "", "loyalty_discount_amount": None}
    )
    assert out == {}


def test_to_decimal_handles_none_and_garbage() -> None:
    assert dr._to_decimal(None) is None
    assert dr._to_decimal("") is None
    assert dr._to_decimal("not-a-number") is None
    assert dr._to_decimal("1234.50") == Decimal("1234.50")


# ── integration ──────────────────────────────────────────────────────────────
@pytest.fixture
def journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for deal-reconciliation integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-dr-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"DR-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"DR-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'DR', :o, :cat, CURRENT_DATE - 60, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"DR-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"DR-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"DR-O-{suffix}"},
        ).scalar_one()
        customer_id = c.execute(
            text("""INSERT INTO auditcore.customers
                (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = c.execute(
            text("""INSERT INTO auditcore.journeys
                (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"DR-J-{suffix}"},
        ).scalar_one()
        c.execute(
            text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date) "
                 "VALUES (:t, :j, CURRENT_DATE - 10)"),
            {"t": tenant_id, "j": journey_id},
        )
        c.execute(
            text("""INSERT INTO auditcore.journey_stage_states
                (tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                 first_started_at_utc, latest_activity_at_utc, version_no)
                VALUES (:t, :j, 'BOOKING', 'BOOKING_IN_PROGRESS', 'IN_PROGRESS', 'NOT_EVALUATED',
                        now(), now(), 1)"""),
            {"t": tenant_id, "j": journey_id},
        )
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_id = journey_id  # type: ignore[attr-defined]
        c.oem_id = oem_id  # type: ignore[attr-defined]
        c.customer_id = customer_id  # type: ignore[attr-defined]
        yield c
    engine.dispose()


def _seed_pinned_sku(c, *, model: str, variant: str, components: dict[str, str], pin: bool = True):
    """One published price-list version + one SKU, optionally pinned on the journey."""
    tenant_id, oem_id = c.tenant_id, c.oem_id
    pl_id = c.execute(
        text("INSERT INTO auditcore.price_lists (tenant_id, price_list_code, price_list_name) "
             "VALUES (:t, :c, 'OEM') RETURNING price_list_id"),
        {"t": tenant_id, "c": f"PL{uuid4().hex[:8]}"},
    ).scalar_one()
    plv_id = c.execute(
        text("INSERT INTO auditcore.price_list_versions "
             "(tenant_id, price_list_id, version_no, lifecycle_status, effective_from) "
             "VALUES (:t, :pl, 1, 'DRAFT', CURRENT_DATE - 45) RETURNING price_list_version_id"),
        {"t": tenant_id, "pl": pl_id},
    ).scalar_one()
    model_id = c.execute(
        text("INSERT INTO auditcore.product_models (oem_id, model_code, model_name) "
             "VALUES (:o, :mc, :mn) RETURNING model_id"),
        {"o": oem_id, "mc": f"M{uuid4().hex[:8]}", "mn": model},
    ).scalar_one()
    variant_id = c.execute(
        text("INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name) "
             "VALUES (:m, :vc, :vn) RETURNING variant_id"),
        {"m": model_id, "vc": f"V{uuid4().hex[:8]}", "vn": variant},
    ).scalar_one()
    sku_id = c.execute(
        text("INSERT INTO auditcore.product_skus (oem_id, model_id, variant_id, sku_code) "
             "VALUES (:o, :m, :v, :sc) RETURNING product_sku_id"),
        {"o": oem_id, "m": model_id, "v": variant_id, "sc": f"SKU{uuid4().hex[:10]}"},
    ).scalar_one()
    for key, amount in components.items():
        c.execute(
            text("INSERT INTO auditcore.price_list_items "
                 "(tenant_id, price_list_version_id, product_sku_id, component_key, standard_amount) "
                 "VALUES (:t, :plv, :sku, :k, :a)"),
            {"t": tenant_id, "plv": plv_id, "sku": sku_id, "k": key, "a": amount},
        )
    c.execute(
        text("UPDATE auditcore.price_list_versions SET lifecycle_status='PUBLISHED' "
             "WHERE tenant_id=:t AND price_list_version_id=:plv"),
        {"t": tenant_id, "plv": plv_id},
    )
    c.execute(
        text("INSERT INTO auditcore.journey_products "
             "(tenant_id, journey_id, product_sku_id, model_name_snapshot, variant_name_snapshot, "
             " selection_status, selection_method, selection_source) "
             "VALUES (:t, :j, :sku, :m, :v, :st, 'TEST', 'EVIDENCE')"),
        {"t": tenant_id, "j": c.journey_id, "sku": sku_id if pin else None,
         "m": model, "v": variant, "st": "CONFIRMED" if pin else None},
    )
    return {"model_id": model_id, "variant_id": variant_id, "sku_id": sku_id, "plv_id": plv_id}


def _seed_scheme(c, *, category: str, benefit_key: str, amount: str, model_id, variant_id=None,
                 customer_type_code=None, benefit_type: str = "AMOUNT"):
    tenant_id = c.tenant_id
    scheme_id = c.execute(
        text("INSERT INTO auditcore.discount_schemes "
             "(tenant_id, scheme_code, scheme_name, scheme_category) "
             "VALUES (:t, :c, 'S', :cat) RETURNING discount_scheme_id"),
        {"t": tenant_id, "c": f"SCH{uuid4().hex[:8]}", "cat": category},
    ).scalar_one()
    dsv_id = c.execute(
        text("INSERT INTO auditcore.discount_scheme_versions "
             "(tenant_id, discount_scheme_id, version_no, lifecycle_status, effective_from) "
             "VALUES (:t, :s, 1, 'DRAFT', CURRENT_DATE - 30) RETURNING discount_scheme_version_id"),
        {"t": tenant_id, "s": scheme_id},
    ).scalar_one()
    c.execute(
        text("INSERT INTO auditcore.discount_scheme_benefits "
             "(tenant_id, discount_scheme_version_id, benefit_key, benefit_type, amount_value) "
             "VALUES (:t, :v, :k, :bt, :a)"),
        {"t": tenant_id, "v": dsv_id, "k": benefit_key, "bt": benefit_type, "a": amount},
    )
    c.execute(
        text("INSERT INTO auditcore.discount_scheme_eligibility "
             "(tenant_id, discount_scheme_version_id, model_id, variant_id, customer_type_code) "
             "VALUES (:t, :v, :m, :vr, :ct)"),
        {"t": tenant_id, "v": dsv_id, "m": model_id, "vr": variant_id, "ct": customer_type_code},
    )
    c.execute(
        text("UPDATE auditcore.discount_scheme_versions SET lifecycle_status='PUBLISHED' "
             "WHERE tenant_id=:t AND discount_scheme_version_id=:v"),
        {"t": tenant_id, "v": dsv_id},
    )
    return dsv_id


def _seed_reviewed_booking(c, **fields):
    cols = ["tenant_id", "journey_id", "source_di_document_id", "reviewed_by_actor_id", *fields.keys()]
    vals = [":tenant_id", ":journey_id", ":doc", ":actor", *(f":{k}" for k in fields)]
    params = {"tenant_id": c.tenant_id, "journey_id": c.journey_id,
              "doc": uuid4(), "actor": "tester", **fields}
    c.execute(
        text(f"INSERT INTO auditcore.booking_form_review_values ({', '.join(cols)}) "
             f"VALUES ({', '.join(vals)})"),
        params,
    )


def _commercial(c, key):
    return c.execute(
        text("SELECT standard_amount FROM auditcore.commercial_lines "
             "WHERE tenant_id=:t AND journey_id=:j AND component_key=:k"),
        {"t": c.tenant_id, "j": c.journey_id, "k": key},
    ).scalar_one_or_none()


def _discount(c, key):
    return c.execute(
        text("SELECT standard_eligible_amount, actual_discount_amount, eligibility_result, "
             "       actual_source_kind "
             "FROM auditcore.discount_applications "
             "WHERE tenant_id=:t AND journey_id=:j AND discount_key=:k"),
        {"t": c.tenant_id, "j": c.journey_id, "k": key},
    ).mappings().one_or_none()


def test_price_standards_materialized_with_registration_basis(journey) -> None:
    c = journey
    _seed_pinned_sku(c, model="SCORPIO N", variant="Z8L", components={
        "EX_SHOWROOM": "1600000",
        "INSURANCE": "60000",
        "EXT_WARRANTY_4TH_YR": "12000",
        "EXT_WARRANTY_4TH_5TH_YR": "18000",
        "REGISTRATION_INDIVIDUAL": "170000",
        "REGISTRATION_CORPORATE": "220000",
    })

    result = dr.sync_deal_reconciliation(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("priceLines", 0) >= 4

    assert _commercial(c, "ex_showroom_price") == Decimal(1600000)
    assert _commercial(c, "insurance_amount") == Decimal(60000)
    # the two extended-warranty tiers sum onto one line
    assert _commercial(c, "additional_warranty_amount") == Decimal(30000)
    # individual buyer -> individual registration rate, corporate variant not double counted
    assert _commercial(c, "registration_charges") == Decimal(170000)


def test_registration_basis_corporate_picks_corporate_rate(journey) -> None:
    c = journey
    c.execute(
        text("UPDATE auditcore.customers SET customer_type_code='CORPORATE' WHERE customer_id=:id"),
        {"id": c.customer_id},
    )
    _seed_pinned_sku(c, model="XUV700", variant="AX7", components={
        "EX_SHOWROOM": "2000000",
        "REGISTRATION_INDIVIDUAL": "200000",
        "REGISTRATION_CORPORATE": "260000",
    })

    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _commercial(c, "registration_charges") == Decimal(260000)


def test_discount_standard_and_actual_on_one_row(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="THAR", variant="LX", components={"EX_SHOWROOM": "1500000"})
    _seed_scheme(c, category="CONSUMER", benefit_key="CASH_DISCOUNT", amount="20000",
                 model_id=seeded["model_id"])
    _seed_reviewed_booking(c, discount_amount="15000")

    result = dr.sync_deal_reconciliation(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("eligible") == 1

    row = _discount(c, "CASH_DISCOUNT")
    assert row is not None
    assert row["standard_eligible_amount"] == Decimal(20000)
    assert row["actual_discount_amount"] == Decimal(15000)
    assert row["eligibility_result"] == "ELIGIBLE"
    assert row["actual_source_kind"] == "CALCULATED"


def test_unclaimed_entitlement_recorded(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="BOLERO", variant="B4", components={"EX_SHOWROOM": "1000000"})
    _seed_scheme(c, category="EXCHANGE", benefit_key="EXCHANGE_BONUS", amount="25000",
                 model_id=seeded["model_id"])
    # no reviewed booking discount values at all

    result = dr.sync_deal_reconciliation(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("unclaimed") == 1

    row = _discount(c, "EXCHANGE_BONUS")
    assert row is not None
    assert row["standard_eligible_amount"] == Decimal(25000)
    assert row["actual_discount_amount"] is None
    assert row["eligibility_result"] == "ELIGIBLE_UNCLAIMED"


def test_over_grant_flagged_not_eligible(journey) -> None:
    c = journey
    _seed_pinned_sku(c, model="XUV400", variant="EL", components={"EX_SHOWROOM": "1700000"})
    # a corporate discount was given but no corporate scheme makes the customer eligible
    _seed_reviewed_booking(c, corporate_discount_amount="30000")

    result = dr.sync_deal_reconciliation(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("overGranted") == 1

    row = _discount(c, "CORPORATE_PRIVILEGE")
    assert row is not None
    assert row["standard_eligible_amount"] is None
    assert row["actual_discount_amount"] == Decimal(30000)
    assert row["eligibility_result"] == "NOT_ELIGIBLE"


def test_idempotent(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="MARAZZO", variant="M2", components={
        "EX_SHOWROOM": "1400000", "INSURANCE": "50000",
    })
    _seed_scheme(c, category="CONSUMER", benefit_key="CASH_DISCOUNT", amount="10000",
                 model_id=seeded["model_id"])
    _seed_reviewed_booking(c, discount_amount="8000")

    for _ in range(3):
        dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    lines = c.execute(
        text("SELECT count(*) FROM auditcore.commercial_lines WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    apps = c.execute(
        text("SELECT count(*) FROM auditcore.discount_applications WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert lines == 2
    assert apps == 1


def test_skipped_when_sku_not_pinned(journey) -> None:
    c = journey
    _seed_pinned_sku(c, model="THAR ROXX", variant="MX", components={"EX_SHOWROOM": "1600000"},
                     pin=False)

    result = dr.sync_deal_reconciliation(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("skipped") is True
