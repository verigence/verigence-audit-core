from __future__ import annotations

import os
from datetime import date
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from sqlalchemy import create_engine, text

from audit_core.uc03_journey_overview_projection import (
    _deal_price_point_options,
    _primary_invoice_date,
)


@pytest.fixture
def journey(request: pytest.FixtureRequest):
    """Self-contained -- deliberately not imported from another test module
    (see test_uc03_model_resolution.py's own fixture for why: a plain
    `pytest` invocation, as CI runs it, doesn't add the repo root to
    sys.path the way `python -m pytest` does locally)."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-ppo-{suffix}"
    request.addfinalizer(lambda: (delete_tenant_data(engine, tenant_id), engine.dispose()))
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"PPO-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"PPO-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'PPO', :o, :cat, CURRENT_DATE - 60, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"PPO-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"PPO-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"PPO-O-{suffix}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"PPO-J-{suffix}"},
        ).scalar_one()
        model_id = c.execute(
            text("INSERT INTO auditcore.product_models (oem_id, model_code, model_name) "
                 "VALUES (:o, :mc, 'SCORPIO') RETURNING model_id"),
            {"o": oem_id, "mc": f"M{suffix}"},
        ).scalar_one()
        variant_id = c.execute(
            text("INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name) "
                 "VALUES (:m, :vc, 'Z8L') RETURNING variant_id"),
            {"m": model_id, "vc": f"V{suffix}"},
        ).scalar_one()
        sku_id = c.execute(
            text("INSERT INTO auditcore.product_skus (oem_id, model_id, variant_id, sku_code) "
                 "VALUES (:o, :m, :v, :sc) RETURNING product_sku_id"),
            {"o": oem_id, "m": model_id, "v": variant_id, "sc": f"SKU{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.journey_products
                (tenant_id, journey_id, product_sku_id, model_name_snapshot, variant_name_snapshot,
                 selection_status, selection_source)
                VALUES (:t, :j, :sku, 'SCORPIO', 'Z8L', 'CONFIRMED', 'EVIDENCE')"""),
            {"t": tenant_id, "j": journey_id, "sku": sku_id},
        )
        # Booking made 03/09 -- resolves to the 01/09 price list by default.
        c.execute(
            text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date) "
                 "VALUES (:t, :j, :d)"),
            {"t": tenant_id, "j": journey_id, "d": date(2026, 9, 3)},
        )
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_id = journey_id  # type: ignore[attr-defined]
        c.oem_id = oem_id  # type: ignore[attr-defined]
        c.model_id = model_id  # type: ignore[attr-defined]
        c.variant_id = variant_id  # type: ignore[attr-defined]
        c.sku_id = sku_id  # type: ignore[attr-defined]
        yield c


def _seed_price_list_version(c, *, effective_from: date, ex_showroom: str):
    pl_id = c.execute(
        text("INSERT INTO auditcore.price_lists (tenant_id, price_list_code, price_list_name) "
             "VALUES (:t, :c, 'OEM') RETURNING price_list_id"),
        {"t": c.tenant_id, "c": f"PL{uuid4().hex[:8]}"},
    ).scalar_one()
    plv_id = c.execute(
        text("INSERT INTO auditcore.price_list_versions "
             "(tenant_id, price_list_id, version_no, lifecycle_status, effective_from) "
             "VALUES (:t, :pl, 1, 'DRAFT', :ef) RETURNING price_list_version_id"),
        {"t": c.tenant_id, "pl": pl_id, "ef": effective_from},
    ).scalar_one()
    c.execute(
        text("INSERT INTO auditcore.price_list_items "
             "(tenant_id, price_list_version_id, product_sku_id, component_key, standard_amount) "
             "VALUES (:t, :plv, :sku, 'EX_SHOWROOM', :amt)"),
        {"t": c.tenant_id, "plv": plv_id, "sku": c.sku_id, "amt": ex_showroom},
    )
    c.execute(
        text("UPDATE auditcore.price_list_versions SET lifecycle_status='PUBLISHED' "
             "WHERE tenant_id=:t AND price_list_version_id=:plv"),
        {"t": c.tenant_id, "plv": plv_id},
    )
    return plv_id


def _seed_invoice(c, *, document_type_key: str, invoice_date: date):
    c.execute(
        text("""INSERT INTO auditcore.invoice_review_values
            (tenant_id, journey_id, source_di_document_id, document_type_key,
             invoice_date, reviewed_by_actor_id)
            VALUES (:t, :j, :doc, :dtk, :dt, 'tester')"""),
        {"t": c.tenant_id, "j": c.journey_id, "doc": uuid4(), "dtk": document_type_key, "dt": invoice_date},
    )


def test_no_invoice_yet_returns_none(journey) -> None:
    c = journey
    _seed_price_list_version(c, effective_from=date(2026, 9, 1), ex_showroom="1600000")
    assert _primary_invoice_date(c, tenant_id=c.tenant_id, journey_id=c.journey_id) is None
    assert _deal_price_point_options(c, tenant_id=c.tenant_id, journey_id=c.journey_id) is None


def test_invoice_same_date_as_booking_returns_none(journey) -> None:
    c = journey
    _seed_price_list_version(c, effective_from=date(2026, 9, 1), ex_showroom="1600000")
    _seed_invoice(c, document_type_key="customer_invoice_dms", invoice_date=date(2026, 9, 3))
    assert _deal_price_point_options(c, tenant_id=c.tenant_id, journey_id=c.journey_id) is None


def test_invoice_date_with_newer_price_list_is_detected(journey) -> None:
    """Direct user scenario (2026-09-24): booking 03/09 resolves to the
    01/09 price list by default; delivery/invoice lands 05/10, by which
    time a newer price list (wef 01/10) has been published. Must detect
    the two dates resolve to genuinely different price-list versions.
    """
    c = journey
    _seed_price_list_version(c, effective_from=date(2026, 9, 1), ex_showroom="1600000")
    _seed_price_list_version(c, effective_from=date(2026, 10, 1), ex_showroom="1650000")
    _seed_invoice(c, document_type_key="customer_invoice_dms", invoice_date=date(2026, 10, 5))

    inv_date = _primary_invoice_date(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    assert inv_date == date(2026, 10, 5)

    options = _deal_price_point_options(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    assert options is not None
    assert options["bookingDate"] == "2026-09-03"
    assert options["invoiceDate"] == "2026-10-05"
    assert options["priceListDiffers"] is True
    assert options["discountsDiffer"] is False


def test_non_vehicle_sale_invoice_is_ignored(journey) -> None:
    """An accessory/EW/RSA/credit-note invoice is not the vehicle sale --
    its date must not be treated as the deal's own invoice date."""
    c = journey
    _seed_price_list_version(c, effective_from=date(2026, 9, 1), ex_showroom="1600000")
    _seed_price_list_version(c, effective_from=date(2026, 10, 1), ex_showroom="1650000")
    _seed_invoice(c, document_type_key="accessory_invoice_dms", invoice_date=date(2026, 10, 5))

    assert _primary_invoice_date(c, tenant_id=c.tenant_id, journey_id=c.journey_id) is None
    assert _deal_price_point_options(c, tenant_id=c.tenant_id, journey_id=c.journey_id) is None


def test_vehicle_sale_invoice_ranked_over_wholesale_when_both_exist(journey) -> None:
    c = journey
    _seed_price_list_version(c, effective_from=date(2026, 9, 1), ex_showroom="1600000")
    _seed_invoice(c, document_type_key="wholesale_invoice", invoice_date=date(2026, 9, 20))
    _seed_invoice(c, document_type_key="customer_invoice_dms", invoice_date=date(2026, 10, 5))

    # customer_invoice_dms outranks wholesale_invoice in _VEHICLE_SALE_INVOICE_TYPES.
    inv_date = _primary_invoice_date(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    assert inv_date == date(2026, 10, 5)
