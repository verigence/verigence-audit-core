from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.uc03_journey_overview_projection import _sku_pricing_panel


@pytest.fixture
def journey():
    """Self-contained -- deliberately not imported from another test module.
    A plain `pytest` invocation (as CI runs it) doesn't add the repo root to
    sys.path the way `python -m pytest` does locally, so `from tests.<mod>
    import <fixture>` resolves locally but raises `ModuleNotFoundError: No
    module named 'tests'` in real CI. Same shape as test_uc03_model_resolution
    .py's own fixture."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-op-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"OP-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"OP-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'OP', :o, :cat, CURRENT_DATE - 60, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"OP-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"OP-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"OP-O-{suffix}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"OP-J-{suffix}"},
        ).scalar_one()
        c.execute(
            text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date) "
                 "VALUES (:t, :j, CURRENT_DATE - 10)"),
            {"t": tenant_id, "j": journey_id},
        )
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_id = journey_id  # type: ignore[attr-defined]
        c.oem_id = oem_id  # type: ignore[attr-defined]
        yield c
    engine.dispose()


def _seed_price_list(c, skus: list[dict]) -> list:
    """One published price-list version holding every SKU in ``skus``.
    Each entry: {"model", "variant", "components": {key: amount}}."""
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
    sku_ids = []
    for entry in skus:
        model_id = c.execute(
            text("INSERT INTO auditcore.product_models (oem_id, model_code, model_name) "
                 "VALUES (:o, :mc, :mn) RETURNING model_id"),
            {"o": oem_id, "mc": f"M{uuid4().hex[:8]}", "mn": entry["model"]},
        ).scalar_one()
        variant_id = c.execute(
            text("INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name) "
                 "VALUES (:m, :vc, :vn) RETURNING variant_id"),
            {"m": model_id, "vc": f"V{uuid4().hex[:8]}", "vn": entry.get("variant") or "BASE"},
        ).scalar_one()
        sku_id = c.execute(
            text("INSERT INTO auditcore.product_skus (oem_id, model_id, variant_id, sku_code) "
                 "VALUES (:o, :m, :v, :sc) RETURNING product_sku_id"),
            {"o": oem_id, "m": model_id, "v": variant_id, "sc": f"SKU{uuid4().hex[:10]}"},
        ).scalar_one()
        sku_ids.append(sku_id)
        for key, amount in entry["components"].items():
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
    return sku_ids


def test_sku_pricing_panel_does_not_crash_when_sku_is_resolved(journey) -> None:
    """Regression: bookings.price_list_id is NULL for almost every real booking
    (nothing sets it explicitly), which is exactly the ``:price_list_id::uuid
    IS NULL`` branch -- SQLAlchemy's text() bind-parameter matcher refuses to
    substitute a ``:name`` immediately followed by another ``:`` (Postgres's
    cast operator), so ``:price_list_id::uuid`` was sent to psycopg as literal,
    unsubstituted text and raised ``SyntaxError: syntax error at or near ":"``
    live in production the moment any journey actually had a resolved SKU
    (uc03_model_resolution pins journey_products.product_sku_id) -- this path
    was previously untested and unreached for every journey that never
    resolved a SKU.
    """
    c = journey
    (sku_id,) = _seed_price_list(c, [{
        "model": "SCORPIO N", "variant": "Z8L",
        "components": {"EX_SHOWROOM": "1600000", "INSURANCE": "60000",
                       "REGISTRATION_INDIVIDUAL": "170000", "REGISTRATION_CORPORATE": "220000"},
    }])
    c.execute(
        text(
            "INSERT INTO auditcore.journey_products "
            "(tenant_id, journey_id, product_sku_id, model_name_snapshot, "
            " variant_name_snapshot, selection_status, selection_source) "
            "VALUES (:t, :j, :sku, 'SCORPIO N', 'Z8L', 'CONFIRMED', 'EVIDENCE')"
        ),
        {"t": c.tenant_id, "j": c.journey_id, "sku": sku_id},
    )

    panel = _sku_pricing_panel(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, reviewed_booking={},
    )

    assert panel is not None
    assert panel["skuCode"] is not None
    assert panel["modelName"] == "SCORPIO N"
    assert panel["masterTotalAmount"] > 0
    assert panel["masterComponents"]


def test_sku_pricing_panel_scopes_to_booking_price_list_id_when_set(journey) -> None:
    """The other branch of the same OR -- ``bookings.price_list_id`` explicitly
    set -- must also survive the CAST, not just the IS NULL side."""
    c = journey
    (sku_id,) = _seed_price_list(c, [{
        "model": "THAR", "variant": "LX",
        "components": {"EX_SHOWROOM": "1500000", "REGISTRATION_INDIVIDUAL": "150000",
                       "REGISTRATION_CORPORATE": "180000"},
    }])
    price_list_id = c.execute(
        text(
            "SELECT pl.price_list_id FROM auditcore.price_list_items pli "
            "JOIN auditcore.price_list_versions plv "
            "  ON plv.price_list_version_id = pli.price_list_version_id "
            "JOIN auditcore.price_lists pl ON pl.price_list_id = plv.price_list_id "
            "WHERE pli.product_sku_id = :sku LIMIT 1"
        ),
        {"sku": sku_id},
    ).scalar_one()
    c.execute(
        text(
            "INSERT INTO auditcore.journey_products "
            "(tenant_id, journey_id, product_sku_id, model_name_snapshot, "
            " variant_name_snapshot, selection_status, selection_source) "
            "VALUES (:t, :j, :sku, 'THAR', 'LX', 'CONFIRMED', 'EVIDENCE')"
        ),
        {"t": c.tenant_id, "j": c.journey_id, "sku": sku_id},
    )
    c.execute(
        text("UPDATE auditcore.bookings SET price_list_id=:plid WHERE tenant_id=:t AND journey_id=:j"),
        {"plid": price_list_id, "t": c.tenant_id, "j": c.journey_id},
    )

    panel = _sku_pricing_panel(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, reviewed_booking={},
    )

    assert panel is not None
    assert panel["modelName"] == "THAR"
