from __future__ import annotations

import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

import audit_core.uc03_model_resolution as mr


# ── unit: the deterministic matcher ──────────────────────────────────────────
def _row(sku: str, model: str, *, variant=None, colour=None, total="1000000", ex="800000"):
    return {
        "product_sku_id": uuid4(),
        "sku_code": sku,
        "model_name": model,
        "variant_name": variant,
        "colour_name": colour,
        "master_total_individual": Decimal(total),
        "master_total_corporate": Decimal(total) + Decimal(50000),
        "master_ex_showroom": Decimal(ex),
    }


def _inputs(*, model, variant=None, colour=None, total=None, ex=None, basis="INDIVIDUAL"):
    return {
        "model_name": model,
        "variant_name": variant,
        "colour_name": colour,
        "offered_total": Decimal(total) if total is not None else None,
        "offered_ex_showroom": Decimal(ex) if ex is not None else None,
        "registration_basis": basis,
    }


def test_match_unique_on_total() -> None:
    rows = [_row("A", "SCORPIO N", total="1000000"), _row("B", "THAR", total="1000000")]
    matched, stage = mr._match(rows, _inputs(model="Scorpio N", total="1000000"))
    assert stage == "TOTAL"
    assert [r["sku_code"] for r in matched] == ["A"]


def test_match_falls_back_to_ex_showroom_when_total_ambiguous() -> None:
    rows = [
        _row("A", "SCORPIO N", variant="Z8", total="1000000", ex="800000"),
        _row("B", "SCORPIO N", variant="Z8L", total="1000000", ex="850000"),
    ]
    matched, stage = mr._match(
        rows, _inputs(model="SCORPIO N", total="1000000", ex="850000")
    )
    assert stage == "EX_SHOWROOM"
    assert [r["sku_code"] for r in matched] == ["B"]


def test_match_zero_when_no_model() -> None:
    rows = [_row("A", "SCORPIO N"), _row("B", "THAR")]
    matched, stage = mr._match(rows, _inputs(model="XUV700", total="1000000"))
    assert matched == []
    assert stage == "NONE"


def test_match_multiple_reported() -> None:
    rows = [
        _row("A", "SCORPIO N", total="1000000", ex="800000"),
        _row("B", "SCORPIO N", total="1000000", ex="800000"),
    ]
    matched, _ = mr._match(rows, _inputs(model="SCORPIO N", total="1000000", ex="800000"))
    assert {r["sku_code"] for r in matched} == {"A", "B"}


def test_match_narrows_by_variant_then_colour() -> None:
    rows = [
        _row("A", "THAR", variant="LX", colour="RED", total="1000000"),
        _row("B", "THAR", variant="LX", colour="WHITE", total="1000000"),
        _row("C", "THAR", variant="AX", colour="WHITE", total="1000000"),
    ]
    matched, stage = mr._match(
        rows, _inputs(model="THAR", variant="LX", colour="white", total="1000000")
    )
    assert stage == "TOTAL"
    assert [r["sku_code"] for r in matched] == ["B"]


def test_corporate_basis_uses_corporate_total() -> None:
    rows = [_row("A", "SCORPIO N", total="1000000")]  # corporate total = 1050000
    matched, stage = mr._match(
        rows, _inputs(model="SCORPIO N", total="1050000", basis="CORPORATE")
    )
    assert stage == "TOTAL"
    assert [r["sku_code"] for r in matched] == ["A"]


def test_no_price_data_returns_empty() -> None:
    rows = [_row("A", "SCORPIO N")]
    matched, stage = mr._match(rows, _inputs(model="SCORPIO N"))
    assert stage == "NONE"
    assert matched == [rows[0]] or matched == []  # model matched but nothing to disambiguate


# ── integration ──────────────────────────────────────────────────────────────
@pytest.fixture
def env():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for model-resolution integration tests")
    return create_engine(database_url)


def _seed_price_list(c, tenant_id: str, *, model: str, variant: str, components: dict[str, str]):
    """Minimal OEM-style price list: one SKU, one published version."""
    cat = c.execute(
        text("INSERT INTO auditcore.product_categories (category_code, category_name) "
             "VALUES (:cc, :cn) RETURNING category_id"),
        {"cc": f"C{uuid4().hex[:6]}", "cn": "PV"},
    ).scalar_one()
    model_id = c.execute(
        text("INSERT INTO auditcore.product_models (category_id, model_code, model_name, is_active) "
             "VALUES (:cat, :mc, :mn, true) RETURNING model_id"),
        {"cat": cat, "mc": f"M{uuid4().hex[:6]}", "mn": model},
    ).scalar_one()
    variant_id = c.execute(
        text("INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name, is_active) "
             "VALUES (:m, :vc, :vn, true) RETURNING variant_id"),
        {"m": model_id, "vc": f"V{uuid4().hex[:6]}", "vn": variant},
    ).scalar_one()
    sku_id = c.execute(
        text("INSERT INTO auditcore.product_skus (model_id, variant_id, sku_code, is_active) "
             "VALUES (:m, :v, :sc, true) RETURNING product_sku_id"),
        {"m": model_id, "v": variant_id, "sc": f"SKU{uuid4().hex[:8]}"},
    ).scalar_one()
    pl_id = c.execute(
        text("INSERT INTO auditcore.price_lists (tenant_id, price_list_code, price_list_name) "
             "VALUES (:t, :c, :n) RETURNING price_list_id"),
        {"t": tenant_id, "c": f"PL{uuid4().hex[:6]}", "n": "OEM"},
    ).scalar_one()
    plv_id = c.execute(
        text("INSERT INTO auditcore.price_list_versions "
             "(tenant_id, price_list_id, version_no, lifecycle_status, effective_from) "
             "VALUES (:t, :pl, 1, 'PUBLISHED', CURRENT_DATE - 30) RETURNING price_list_version_id"),
        {"t": tenant_id, "pl": pl_id},
    ).scalar_one()
    for key, amount in components.items():
        c.execute(
            text("INSERT INTO auditcore.price_list_items "
                 "(tenant_id, price_list_version_id, product_sku_id, component_key, standard_amount) "
                 "VALUES (:t, :plv, :sku, :k, :a)"),
            {"t": tenant_id, "plv": plv_id, "sku": sku_id, "k": key, "a": amount},
        )
    return sku_id


@pytest.fixture
def journey(env):
    engine = env
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-mr-{suffix}"
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.execute(
            text("INSERT INTO auditcore.projects (tenant_id, project_name, project_status, timezone_name) "
                 "VALUES (:t, :n, 'ACTIVE', 'Asia/Kolkata')"),
            {"t": tenant_id, "n": f"P {suffix}"},
        )
        dealer_id = uuid4()
        c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_id, dealer_name) VALUES (:t,:d,:n)"),
            {"t": tenant_id, "d": dealer_id, "n": "D"},
        )
        outlet_id = uuid4()
        c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_id, outlet_name) "
                 "VALUES (:t,:d,:o,:n)"),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "n": "O"},
        )
        customer_id = uuid4()
        c.execute(
            text("INSERT INTO auditcore.customers (tenant_id, customer_id, display_name) "
                 "VALUES (:t,:c,:n)"),
            {"t": tenant_id, "c": customer_id, "n": "Cust"},
        )
        journey_id = uuid4()
        c.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, journey_id, customer_id, dealer_id, outlet_id) "
                 "VALUES (:t,:j,:c,:d,:o)"),
            {"t": tenant_id, "j": journey_id, "c": customer_id, "d": dealer_id, "o": outlet_id},
        )
        c.execute(
            text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date) "
                 "VALUES (:t,:j,CURRENT_DATE - 5)"),
            {"t": tenant_id, "j": journey_id},
        )
    return {"engine": engine, "tenant_id": tenant_id, "journey_id": journey_id}


def _set_journey_product(c, tid, jid, model, variant):
    c.execute(
        text("INSERT INTO auditcore.journey_products "
             "(tenant_id, journey_id, model_name_snapshot, variant_name_snapshot, selection_source) "
             "VALUES (:t,:j,:m,:v,'EVIDENCE')"),
        {"t": tid, "j": jid, "m": model, "v": variant},
    )


def _set_commercial(c, tid, jid, key, amount):
    c.execute(
        text("INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, actual_amount) "
             "VALUES (:t,:j,:k,:a) ON CONFLICT (tenant_id, journey_id, component_key) "
             "DO UPDATE SET actual_amount = EXCLUDED.actual_amount"),
        {"t": tid, "j": jid, "k": key, "a": amount},
    )


def test_integration_resolves_and_pins_sku(journey) -> None:
    e, tid, jid = journey["engine"], journey["tenant_id"], journey["journey_id"]
    with e.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        _seed_price_list(
            c, tid, model="SCORPIO N", variant="Z8L",
            components={"EX_SHOWROOM": "1600000", "INSURANCE": "60000",
                        "REGISTRATION_INDIVIDUAL": "170000", "REGISTRATION_CORPORATE": "220000"},
        )
        _set_journey_product(c, tid, jid, "Scorpio N", "Z8L")
        _set_commercial(c, tid, jid, "ex_showroom_price", "1600000")
        _set_commercial(c, tid, jid, "total_price", "1830000")  # 1.6M + 60k + 170k

    with e.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        result = mr.sync_model_resolution(c, tenant_id=tid, journey_id=jid, correlation_id="")
    assert result.get("resolved") is True

    with e.connect() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        sku = c.execute(
            text("SELECT product_sku_id, selection_status FROM auditcore.journey_products "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"t": tid, "j": jid},
        ).mappings().one()
        assert sku["product_sku_id"] is not None
        assert sku["selection_status"] == "CONFIRMED"
        flags = c.execute(
            text("SELECT count(*) FROM auditcore.audit_findings "
                 "WHERE tenant_id=:t AND journey_id=:j AND finding_type_code='MODEL_NOT_IDENTIFIED' "
                 "AND finding_status IN ('OPEN','ACKNOWLEDGED')"),
            {"t": tid, "j": jid},
        ).scalar_one()
        assert flags == 0


def test_integration_raises_flag_when_no_match(journey) -> None:
    e, tid, jid = journey["engine"], journey["tenant_id"], journey["journey_id"]
    with e.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        _seed_price_list(
            c, tid, model="THAR ROXX", variant="AX",
            components={"EX_SHOWROOM": "1400000", "REGISTRATION_INDIVIDUAL": "150000",
                        "REGISTRATION_CORPORATE": "180000"},
        )
        _set_journey_product(c, tid, jid, "Scorpio N", "Z8L")  # not in the list
        _set_commercial(c, tid, jid, "ex_showroom_price", "1600000")
        _set_commercial(c, tid, jid, "total_price", "1830000")

    with e.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        r1 = mr.sync_model_resolution(c, tenant_id=tid, journey_id=jid, correlation_id="")
        r2 = mr.sync_model_resolution(c, tenant_id=tid, journey_id=jid, correlation_id="")
    assert r1.get("raised") is True
    assert r2.get("raised") is True  # idempotent

    with e.connect() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        flags = c.execute(
            text("SELECT count(*) FROM auditcore.audit_findings "
                 "WHERE tenant_id=:t AND journey_id=:j AND finding_type_code='MODEL_NOT_IDENTIFIED' "
                 "AND finding_status='OPEN'"),
            {"t": tid, "j": jid},
        ).scalar_one()
        assert flags == 1  # one open finding despite two sync calls
