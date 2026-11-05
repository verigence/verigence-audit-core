"""A month's Mahindra masters (audit_core.oem_master_reload, a "-month" tag):
the 4 Nov 2026 book and grid on a tenant that has the live history (the
September and October books, the October carry, both grids). Requires
DATABASE_URL."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text

from audit_core.oem_master_reload import run_master_reload
from tests.test_oem_master_reload import (  # noqa: F401 - the fixture
    _versions,
    _with_mahindra_aliases,
    tenant,
)


def _history(engine, tenant_id) -> str:
    _with_mahindra_aliases(engine, tenant_id)
    tag = f"test-{uuid4().hex[:6]}"
    # The October book's 29 carried SKUs come with its ingest (standing rule of 2 Nov 2026).
    for suffix in ("-commit", "-grids-commit"):
        (result,) = run_master_reload(engine, f"{tag}{suffix}", tenant_ids=[tenant_id])
        assert result["status"] == "COMMITTED", [c for c in result.get("checks", []) if not c["ok"]] or result
    return tag


def test_november_loads_on_4_nov_keeps_what_it_does_not_print_and_takes_the_new_grid(tenant, monkeypatch) -> None:  # noqa: F811
    """Product owner, 5 Nov 2026: the new price list and discount grid from
    4 Nov. What November does not print keeps October's figure, not on sale
    (standing rule, 2 Nov); the one old-series Scorpio N row November prints
    is November's price but not on sale, so "SCORPIO N" still names the new
    car; the grid's new spellings all name a model, and XUV400 / the old
    Scorpio N take the line their last grid gave them."""
    from audit_core.oem_price_masters import (
        _models_in_book,
        find_book_sku,
        grid_row_for_model,
    )

    engine, tenant_id = tenant
    tag = _history(engine, tenant_id)
    before = _versions(engine, tenant_id)

    (dry,) = run_master_reload(engine, f"{tag}-month", tenant_ids=[tenant_id])
    assert dry["status"] == "DRY_RUN_PASSED", [c for c in dry.get("checks", []) if not c["ok"]] or dry.get("error")
    assert _versions(engine, tenant_id) == before  # a dry run leaves nothing
    # October's 29 carried rows less the old-series Z8 L D AT 2WD 7 STR ADAS November prints.
    assert dry["carriedNotOnSale"] == 28

    (done,) = run_master_reload(engine, f"{tag}-month-commit", tenant_ids=[tenant_id])
    assert done["status"] == "COMMITTED", [c for c in done.get("checks", []) if not c["ok"]] or done.get("error")
    assert _versions(engine, tenant_id) == before + 1
    on = date(2026, 11, 20)
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        uploads = conn.execute(text(
            "SELECT master_kind, effective_from, status FROM auditcore.oem_master_uploads "
            "WHERE tenant_id = :t AND effective_from = '2026-11-04' ORDER BY master_kind"), {"t": tenant_id}).all()
        old = conn.execute(text(
            "SELECT i.metadata FROM auditcore.price_list_items i JOIN auditcore.product_skus s "
            "ON s.product_sku_id = i.product_sku_id JOIN auditcore.product_models pm ON pm.model_id = s.model_id "
            "WHERE i.price_list_version_id = CAST(:v AS uuid) AND pm.model_name = 'SCORPIO N' "
            "AND i.component_key = 'EX_SHOWROOM' AND i.metadata -> 'printedAs' ->> 'variant' = "
            "'Z8 L D AT 2WD 7 STR BS6.2 - N - ADAS'"), {"v": done["priceListVersionId"]}).scalar_one()
        new_car = find_book_sku(conn, tenant_id=tenant_id, model_text="SCORPIO N",
                                variant_text="Z8 S D AT 2WD 7 STR BS6.2 - Refresh", effective_on=on)
        on_sale = set(conn.execute(text("SELECT model_name FROM auditcore.product_models WHERE model_id = ANY(:m)"),
                                   {"m": list(_models_in_book(conn, tenant_id=tenant_id, effective_on=on))}).scalars())
        thar = conn.execute(text(
            "SELECT pm.model_id FROM auditcore.product_models pm JOIN auditcore.projects p ON p.oem_id = pm.oem_id "
            "WHERE p.tenant_id = :t AND pm.model_name = 'NEW THAR 2WD & 4WD'"), {"t": tenant_id}).scalar_one()
        thar_nov = grid_row_for_model(conn, tenant_id=tenant_id, model_id=thar, effective_on=on)
        thar_oct = grid_row_for_model(conn, tenant_id=tenant_id, model_id=thar, effective_on=date(2026, 11, 3))
    assert [tuple(u) for u in uploads] == [("DISCOUNT_GRID", date(2026, 11, 4), "PUBLISHED"),
                                           ("PRICE_LIST", date(2026, 11, 4), "PUBLISHED")]
    assert old["oldSeries"] is True and "carriedReason" in old
    assert new_car["sku"]["model_name"] == "NEW SCORPIO N" and new_car["sku"]["ex_showroom"] == Decimal("1998996.00")
    assert "NEW SCORPIO N" in on_sale and "SCORPIO N" not in on_sale
    assert thar_nov["model_alias"] == "Thar OG" and thar_oct["model_alias"] == "Thar"
    # once per tag
    assert run_master_reload(engine, f"{tag}-month-commit", tenant_ids=[tenant_id])[0]["status"] == "ALREADY_DONE"
