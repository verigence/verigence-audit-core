"""The one-off Mahindra price-master re-load (audit_core.oem_master_reload).
Requires DATABASE_URL. Runs the job end to end on a tenant of its own: a dry
run changes nothing, a commit publishes the five uploads, a tag runs once,
and a failed check rolls everything back."""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core import oem_master_reload
from audit_core.oem_master_reload import run_master_reload


@pytest.fixture
def tenant():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the re-load job test")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-reload-{suffix}"
    with engine.begin() as conn:
        oem_id = conn.execute(text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'R') RETURNING oem_id"),
                              {"c": f"RELOAD{suffix}"}).scalar_one()
        category = conn.execute(text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                                     "VALUES (:c, 'V') RETURNING product_category_id"), {"c": f"RL-{suffix}"}).scalar_one()
        conn.execute(text("INSERT INTO auditcore.projects (tenant_id, project_code, project_name, oem_id, product_category_id, "
                          "effective_start_date) VALUES (:t, :pc, 'R', :o, :c, CURRENT_DATE)"),
                     {"t": tenant_id, "pc": f"RL-{suffix}", "o": oem_id, "c": category})
    yield engine, tenant_id
    engine.dispose()


def _versions(engine, tenant_id) -> int:
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        return conn.execute(text("SELECT count(*) FROM auditcore.price_list_versions WHERE tenant_id = :t"),
                            {"t": tenant_id}).scalar_one()


def test_the_reload_dry_runs_commits_once_and_checks_the_books(tenant) -> None:
    engine, tenant_id = tenant
    tag = f"test-{uuid4().hex[:6]}"
    (dry,) = run_master_reload(engine, tag, tenant_ids=[tenant_id])
    assert dry["status"] == "DRY_RUN_PASSED", [c for c in dry.get("checks", []) if not c["ok"]] or dry.get("error")
    assert dry["booksAfter"] == {"2026-09-01": 323, "2026-09-02": 323, "2026-09-03": 317, "2026-09-07": 319,
                                 "2026-09-08": 319, "2026-09-19": 319, "2026-10-03": 327}
    assert _versions(engine, tenant_id) == 0  # a dry run leaves nothing
    assert run_master_reload(engine, tag, tenant_ids=[tenant_id])[0]["status"] == "ALREADY_DONE"

    (done,) = run_master_reload(engine, f"{tag}-commit", tenant_ids=[tenant_id])
    assert done["status"] == "COMMITTED"
    assert _versions(engine, tenant_id) == 5
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        uploads = conn.execute(text("SELECT effective_from, status FROM auditcore.oem_master_uploads WHERE tenant_id = :t "
                                    "ORDER BY effective_from"), {"t": tenant_id}).all()
        runs = dict(conn.execute(text("SELECT reload_tag, status FROM auditcore.oem_master_reload_runs WHERE tenant_id = :t"),
                                 {"t": tenant_id}).all())
    assert [tuple(u) for u in uploads] == [(on, "PUBLISHED") for _, on in oem_master_reload.RELOAD_PLAN]
    assert runs == {tag: "DRY_RUN_PASSED", f"{tag}-commit": "COMMITTED"}
    assert run_master_reload(engine, f"{tag}-commit", tenant_ids=[tenant_id])[0]["status"] == "ALREADY_DONE"


def test_a_failed_check_rolls_the_commit_back(tenant, monkeypatch) -> None:
    engine, tenant_id = tenant
    monkeypatch.setitem(oem_master_reload.EXPECTED_SKUS, date(2026, 10, 3), 999)
    (result,) = run_master_reload(engine, f"test-{uuid4().hex[:6]}-commit", tenant_ids=[tenant_id])
    assert result["status"] == "FAILED"
    assert [c["check"] for c in result["checks"] if not c["ok"]] == ["SKUs in force on 2026-10-03"]
    assert _versions(engine, tenant_id) == 0


def test_the_scheme_reload_dry_runs_then_commits_the_bulletins_on_their_dates(tenant) -> None:
    """Review of PR 472: the scheme masters re-load (a "-schemes" tag), after
    the price books. Every bulletin on the dates it prints, nothing applied
    model-wide for variants that did not resolve, and Utkal's XUV 3XO AX5 PM
    MT at 30,000 cash in September."""
    engine, tenant_id = tenant
    with engine.begin() as conn:
        oem_code = conn.execute(text("SELECT o.oem_code FROM auditcore.projects p JOIN auditcore.oems o "
                                     "ON o.oem_id = p.oem_id WHERE p.tenant_id = :t"), {"t": tenant_id}).scalar_one()
        # The tenant's OEM reads the bulletins with Mahindra's model aliases.
        conn.execute(text("INSERT INTO auditcore.oem_model_aliases (oem_code, alias_text, canonical_model_name) "
                          "SELECT :o, alias_text, canonical_model_name FROM auditcore.oem_model_aliases "
                          "WHERE oem_code = 'MAHINDRA' ON CONFLICT DO NOTHING"), {"o": oem_code})
    tag = f"test-{uuid4().hex[:6]}"
    assert run_master_reload(engine, f"{tag}-commit", tenant_ids=[tenant_id])[0]["status"] == "COMMITTED"

    (dry,) = run_master_reload(engine, f"{tag}-schemes", tenant_ids=[tenant_id])
    assert dry["status"] == "DRY_RUN_PASSED", [c for c in dry.get("checks", []) if not c["ok"]] or dry.get("error")
    assert [(u["kind"], u["effectiveFrom"], u["effectiveTo"]) for u in dry["uploads"]] == [
        (kind, on, until) for kind, _, on, until in oem_master_reload.SCHEME_RELOAD_PLAN]
    assert dry["appliedPerModel"]["2026-09-15"]["XUV3XO"] > 0

    (done,) = run_master_reload(engine, f"{tag}-schemes-commit", tenant_ids=[tenant_id])
    assert done["status"] == "COMMITTED", [c for c in done.get("checks", []) if not c["ok"]] or done.get("error")
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        ends = dict(conn.execute(text(
            "SELECT DISTINCT v.effective_from, v.effective_to FROM auditcore.discount_scheme_versions v "
            "JOIN auditcore.discount_scheme_benefits b ON b.discount_scheme_version_id = v.discount_scheme_version_id "
            "WHERE v.tenant_id = :t"), {"t": tenant_id}).all())
    assert ends == {date(2026, 9, 3): date(2026, 9, 30), date(2026, 10, 3): date(2026, 10, 31)}
    # The standing rows name their scheme, so a failed "model-wide rows of unresolved variants"
    # check can log which schemes and variant texts the rows are.
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        standing = oem_master_reload._standing_rows(conn, tenant_id, date(2026, 9, 15))
    assert standing and all(r["scheme_code"] and r["scheme_name"] for r in standing)


def test_the_real_october_grid_binds_scorpio_n_to_the_model_the_october_book_prices(tenant) -> None:
    """Review of PR 472: the October grid (dated 1 Oct) names "SCORPIO-N"; the
    October book (3 Oct) carries only NEW SCORPIO N. With the real files, the
    line is bound to it, a deal of 15 Oct finds its grid line, and the dry
    run's grid check is among the checks that pass."""
    from audit_core.oem_master_reload import reload_schemes_tenant, reload_tenant
    from audit_core.oem_price_masters import grid_row_for_model

    engine, tenant_id = tenant
    with engine.begin() as conn:
        oem_code = conn.execute(text("SELECT o.oem_code FROM auditcore.projects p JOIN auditcore.oems o "
                                     "ON o.oem_id = p.oem_id WHERE p.tenant_id = :t"), {"t": tenant_id}).scalar_one()
        conn.execute(text("INSERT INTO auditcore.oem_model_aliases (oem_code, alias_text, canonical_model_name) "
                          "SELECT :o, alias_text, canonical_model_name FROM auditcore.oem_model_aliases "
                          "WHERE oem_code = 'MAHINDRA' ON CONFLICT DO NOTHING"), {"o": oem_code})
    connection = engine.connect()
    transaction = connection.begin()
    try:
        tag = f"test-{uuid4().hex[:6]}"
        assert reload_tenant(connection, tenant_id=tenant_id, tag=tag, store=False)["passed"]
        outcome = reload_schemes_tenant(connection, tenant_id=tenant_id, tag=f"{tag}-schemes", store=False)
        failed = [c for c in outcome["checks"] if not c["ok"]]
        assert not failed, failed
        grid_checks = [c for c in outcome["checks"] if c["check"].startswith("discount grid of")]
        assert len(grid_checks) >= 2 and all(c["ok"] for c in grid_checks)
        new_scorpio = connection.execute(text(
            "SELECT model_id FROM auditcore.product_models pm JOIN auditcore.projects p ON p.oem_id = pm.oem_id "
            "WHERE p.tenant_id = :t AND pm.model_name = 'NEW SCORPIO N'"), {"t": tenant_id}).scalar_one()
        row = grid_row_for_model(connection, tenant_id=tenant_id, model_id=new_scorpio, effective_on=date(2026, 10, 15))
        assert row is not None and row["model_alias"].upper().startswith("SCORPIO")
    finally:
        transaction.rollback()
        connection.close()


def _with_mahindra_aliases(engine, tenant_id) -> None:
    with engine.begin() as conn:
        oem_code = conn.execute(text("SELECT o.oem_code FROM auditcore.projects p JOIN auditcore.oems o "
                                     "ON o.oem_id = p.oem_id WHERE p.tenant_id = :t"), {"t": tenant_id}).scalar_one()
        conn.execute(text("INSERT INTO auditcore.oem_model_aliases (oem_code, alias_text, canonical_model_name) "
                          "SELECT :o, alias_text, canonical_model_name FROM auditcore.oem_model_aliases "
                          "WHERE oem_code = 'MAHINDRA' ON CONFLICT DO NOTHING"), {"o": oem_code})


def test_the_two_grids_load_alone_with_both_scorpio_n_lines_bound(tenant) -> None:
    """2026-11-01: a "-grids" tag loads the September grid (the "Aug 2026"
    file, effective 1 Sep) and the October grid (1 Oct) and nothing else.
    Every line names a model -- the September "SCORPIO N Old" / "SCORPIO N
    New" are the book's SCORPIO N and NEW SCORPIO N -- and each model gets
    the line the file prints for it on each month."""
    from audit_core.oem_price_masters import grid_row_for_model

    engine, tenant_id = tenant
    _with_mahindra_aliases(engine, tenant_id)
    tag = f"test-{uuid4().hex[:6]}"
    assert run_master_reload(engine, f"{tag}-commit", tenant_ids=[tenant_id])[0]["status"] == "COMMITTED"

    (dry,) = run_master_reload(engine, f"{tag}-grids", tenant_ids=[tenant_id])
    assert dry["status"] == "DRY_RUN_PASSED", [c for c in dry.get("checks", []) if not c["ok"]] or dry.get("error")
    assert [(u["file"], u["effectiveFrom"]) for u in dry["uploads"]] == [
        ("Odisha PV & CV Discount Grid-1 Oct 2026.xlsx", date(2026, 10, 1)),
        ("Odisha PV & CV Discount Grid-1 Aug 2026.xlsx", date(2026, 9, 1))]
    september = {r["model"]: r for r in dry["gridByModel"]["2026-09-15"]}
    october = {r["model"]: r for r in dry["gridByModel"]["2026-10-15"]}
    assert september["NEW SCORPIO N"]["line"] == "SCORPIO N New" and september["NEW SCORPIO N"]["inScope"]
    assert september["SCORPIO N"]["line"] == "SCORPIO N Old" and not september["SCORPIO N"]["inScope"]
    assert october["NEW SCORPIO N"]["line"] == "SCORPIO-N"
    assert october["NEW SCORPIO N"]["tiers"] == {"BASE": "0.00", "WITH_INSURANCE": "0.00", "WITH_EW_INSURANCE": "5000.00"}
    assert september["XUV400"]["line"] == "XUV400 EV" and october["XUV400"]["line"] is None  # not in October's grid

    (done,) = run_master_reload(engine, f"{tag}-grids-commit", tenant_ids=[tenant_id])
    assert done["status"] == "COMMITTED", [c for c in done.get("checks", []) if not c["ok"]] or done.get("error")
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        starts = conn.execute(text("SELECT effective_from FROM auditcore.dealer_discount_grid_versions "
                                   "WHERE tenant_id = :t ORDER BY effective_from"), {"t": tenant_id}).scalars().all()
        schemes = conn.execute(text("SELECT count(*) FROM auditcore.discount_scheme_versions WHERE tenant_id = :t"),
                               {"t": tenant_id}).scalar_one()
        model_id = conn.execute(text(
            "SELECT model_id FROM auditcore.product_models pm JOIN auditcore.projects p ON p.oem_id = pm.oem_id "
            "WHERE p.tenant_id = :t AND pm.model_name = 'NEW SCORPIO N'"), {"t": tenant_id}).scalar_one()
        line = grid_row_for_model(conn, tenant_id=tenant_id, model_id=model_id, effective_on=date(2026, 9, 20))
    assert starts == [date(2026, 9, 1), date(2026, 10, 1)]
    assert schemes == 0  # the bulletins are not touched
    assert line["model_alias"] == "SCORPIO N New" and line["in_scope"]


def test_october_gets_septembers_price_for_the_variants_it_no_longer_lists(tenant, monkeypatch) -> None:
    """2026-11-01, product owner: "the model which are not available in Oct,
    for them pls get the price of September"; "old scorpio to be priced at sep
    price". The 3 Oct book is published again with every October row
    unchanged plus September's rows for the SKUs October does not print,
    marked carried. October's words still name what October sells: "SCORPIO
    N Z8 S D AT ... - N" is the new Scorpio N at October's price; only an
    old-only variant (ADAS) reaches the old series, at September's price."""
    from audit_core.oem_master_reload import complete_october_tenant
    from audit_core.oem_price_masters import _models_in_book, find_book_sku

    engine, tenant_id = tenant
    _with_mahindra_aliases(engine, tenant_id)
    tag = f"test-{uuid4().hex[:6]}"
    assert run_master_reload(engine, f"{tag}-commit", tenant_ids=[tenant_id])[0]["status"] == "COMMITTED"
    # The five re-load files only (no live 19 Sep Veero LNT sheet): the 24 old
    # Scorpio N, BE 6 One x2, BE 6 SPORTEQ THREE x2 and Veero 1.5XXL CBC V2.
    monkeypatch.setattr(oem_master_reload, "OCTOBER_EXPECTED_CARRIED", 29)

    (dry,) = run_master_reload(engine, f"{tag}-octcarry", tenant_ids=[tenant_id])
    assert dry["status"] == "DRY_RUN_PASSED", [c for c in dry.get("checks", []) if not c["ok"]] or dry.get("error")
    models = {}
    for row in dry["carried"]:
        models[row["model"]] = models.get(row["model"], 0) + 1
    assert models == {"SCORPIO N": 24, "BE 6": 2, "BE6 SPORTEQ": 2, "VEERO": 1}
    assert _versions(engine, tenant_id) == 5  # a dry run leaves nothing

    (done,) = run_master_reload(engine, f"{tag}-octcarry-commit", tenant_ids=[tenant_id])
    assert done["status"] == "COMMITTED", [c for c in done.get("checks", []) if not c["ok"]] or done.get("error")
    assert _versions(engine, tenant_id) == 6
    on = date(2026, 10, 20)
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        new_car = find_book_sku(conn, tenant_id=tenant_id, model_text="SCORPIO N",
                                variant_text="Z8 S D AT 2WD 7 STR BS6.2 - N", effective_on=on)
        old_car = find_book_sku(conn, tenant_id=tenant_id, model_text="SCORPIO N",
                                variant_text="Z8 L D AT 2WD 7 STR BS6.2 - N - ADAS", effective_on=on)
        veero = find_book_sku(conn, tenant_id=tenant_id, model_text="VEERO", variant_text="1.5XXL CBC V2",
                              effective_on=on)
        on_sale = conn.execute(text(
            "SELECT pm.model_name FROM auditcore.product_models pm WHERE pm.model_id = ANY(:m)"),
            {"m": list(_models_in_book(conn, tenant_id=tenant_id, effective_on=on))}).scalars().all()
        # The 7 Sep EV sheet's version carries the rest of the book
        # ("carriedForwardFrom"): those rows are on sale, every model of it.
        book_7_sep = conn.execute(text(
            "SELECT DISTINCT s.model_id FROM auditcore.price_list_items i JOIN auditcore.product_skus s "
            "ON s.product_sku_id = i.product_sku_id WHERE i.tenant_id = :t AND i.price_list_version_id = ("
            "SELECT price_list_version_id FROM auditcore.price_list_versions WHERE tenant_id = :t "
            "AND effective_from = '2026-09-07' ORDER BY version_no DESC LIMIT 1)"), {"t": tenant_id}).scalars().all()
        sheet_carried = conn.execute(text(
            "SELECT count(*) FROM auditcore.price_list_items i JOIN auditcore.price_list_versions v "
            "ON v.price_list_version_id = i.price_list_version_id WHERE i.tenant_id = :t "
            "AND v.effective_from = '2026-09-07' AND i.metadata ? 'carriedForwardFrom'"), {"t": tenant_id}).scalar_one()
        on_sale_7_sep = _models_in_book(conn, tenant_id=tenant_id, effective_on=date(2026, 9, 7))
        z8l = find_book_sku(conn, tenant_id=tenant_id, model_text="NEW SCORPIO N",
                            variant_text="Z8 L D AT 2WD 7 STR BS6.2 - Refresh", effective_on=date(2026, 9, 7))
        marked = conn.execute(text(
            "SELECT count(DISTINCT product_sku_id) FROM auditcore.price_list_items WHERE tenant_id = :t "
            "AND price_list_version_id = :v AND metadata ? 'carriedForwardFrom'"),
            {"t": tenant_id, "v": done["newVersionId"]}).scalar_one()
    assert new_car["sku"]["model_name"] == "NEW SCORPIO N"
    assert old_car["sku"]["model_name"] == "SCORPIO N" and old_car["sku"]["ex_showroom"] == Decimal("2276499.58")
    assert veero["sku"] and veero["sku"]["model_name"] == "VEERO"
    assert "NEW SCORPIO N" in on_sale and "SCORPIO N" not in on_sale  # the old series is priced, not on sale
    assert marked == 29
    assert sheet_carried > 0 and on_sale_7_sep == set(book_7_sep)
    assert z8l["sku"] and z8l["sku"]["model_name"] == "NEW SCORPIO N"
    # Nothing left to carry: a second run publishes nothing.
    with engine.connect() as connection, connection.begin():
        again = complete_october_tenant(connection, tenant_id=tenant_id, tag=f"{tag}-octcarry-again", store=False)
    assert again["passed"] and again["carried"] == []


def test_a_carried_row_answers_only_when_the_words_fit_nothing_on_sale() -> None:
    """The resolver (2026-11-01): October prints the new Scorpio N with the old
    series' names. With the old series carried, every October spelling names
    what it named before -- the new car, also when the words add "ADAS" (the
    new Z8 L has it too: an old-stock car is set by the PC, and its price
    mismatch asks for it) -- and only words that fit no car on sale (the old
    Z8 G AT) name a carried one."""
    from audit_core.oem_price_masters import _SUCCESSOR_IDENTITY, price_list_model_key
    from audit_core.uc03_sku_words import identify

    def row(model, variant, carried=False):
        return {"product_sku_id": f"{model}::{variant}", "sku_code": f"{model}::{variant}", "model_name": model,
                "variant_name": variant, "colour_name": None, "fuel_powertrain": "DIESEL", "transmission": None,
                "drive": None, "seater": None, "trim": None, "category": "PV", "segment_code": "PASSENGER_VEHICLE",
                "carried": carried}

    book = [row("NEW SCORPIO N", "Z8 S D AT 2WD 7 STR BS6.2 - Refresh"),
            row("NEW SCORPIO N", "Z8 L D AT 2WD 7 STR BS6.2 - Refresh"),
            row("SCORPIO N", "Z8 S D AT 2WD 7 STR BS6.2 - N", carried=True),
            row("SCORPIO N", "Z8 L D AT 2WD 7 STR BS6.2 - N - ADAS", carried=True),
            {**row("SCORPIO N", "Z8 G AT 2WD 7 STR BS6.2 - N", carried=True), "fuel_powertrain": "PETROL"}]

    def named(model, variant, rows=book):
        result = identify(rows=rows, model_text=model, variant_text=variant, identity=price_list_model_key,
                          successors=dict(_SUCCESSOR_IDENTITY))
        return result.status, (result.row or {}).get("sku_code")

    for model in ("SCORPIO N", "SCORPIO-N"):
        assert named(model, "Z8 S D AT 2WD 7 STR BS6.2 - N") == (
            "IDENTIFIED", "NEW SCORPIO N::Z8 S D AT 2WD 7 STR BS6.2 - Refresh")
        assert named(model, "Z8 S D AT 2WD 7 STR BS6.2 - N") == named(model, "Z8 S D AT 2WD 7 STR BS6.2 - N",
                                                                     [r for r in book if not r["carried"]])
    on_sale = [r for r in book if not r["carried"]]
    adas = "Z8 L D AT 2WD 7 STR BS6.2 - N - ADAS"
    assert named("SCORPIO N", adas) == named("SCORPIO N", adas, on_sale)
    assert named("SCORPIO N", "Z8 G AT 2WD 7 STR BS6.2 - N", on_sale)[0] != "IDENTIFIED"
    assert named("SCORPIO N", "Z8 G AT 2WD 7 STR BS6.2 - N") == ("IDENTIFIED", "SCORPIO N::Z8 G AT 2WD 7 STR BS6.2 - N")
    # Live trial, 1 Nov: "VEERO 1.6 XL HD V2" names no October Veero, and never
    # the carried 1.6XL SD V2 - LNT (HD is not SD): the words are read against
    # the whole book, not the carried rows alone.
    veero = [row("VEERO", v) for v in ("1.6XL SD V2", "1.6XXL HD V2", "1.6XXL HD V2 - LNT", "1.5XXL HD V2",
                                        "1.5XL CBC V2", "1.5XXL CBC V2 - LNT")]
    veero += [row("VEERO", "1.6XL SD V2 - LNT", carried=True), row("VEERO", "1.5XXL CBC V2", carried=True)]
    assert named("VEERO", "1.6 XL HD V2", veero)[0] != "IDENTIFIED"
    assert named("VEERO", "1.6XL SD V2 LNT", veero) == ("IDENTIFIED", "VEERO::1.6XL SD V2 - LNT")
