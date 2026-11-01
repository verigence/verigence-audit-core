"""The one-off Mahindra price-master re-load (audit_core.oem_master_reload).
Requires DATABASE_URL. Runs the job end to end on a tenant of its own: a dry
run changes nothing, a commit publishes the five uploads, a tag runs once,
and a failed check rolls everything back."""
from __future__ import annotations

import os
from datetime import date
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
    for them pls get the price of September". The 3 Oct book is published
    again with every October row unchanged plus September's rows for the
    SKUs October does not print -- of the models October still prices. The
    old Scorpio N series is not brought back: its words would name it again
    on an October invoice instead of the new Scorpio N."""
    from audit_core.oem_master_reload import complete_october_tenant
    from audit_core.oem_price_masters import find_book_sku

    engine, tenant_id = tenant
    _with_mahindra_aliases(engine, tenant_id)
    tag = f"test-{uuid4().hex[:6]}"
    assert run_master_reload(engine, f"{tag}-commit", tenant_ids=[tenant_id])[0]["status"] == "COMMITTED"
    # The five re-load files only (no live 19 Sep Veero LNT sheet): BE 6 One x2,
    # BE 6 SPORTEQ THREE x2 and Veero 1.5XXL CBC V2 are September-only here.
    monkeypatch.setattr(oem_master_reload, "OCTOBER_EXPECTED_CARRIED", 5)

    (dry,) = run_master_reload(engine, f"{tag}-octcarry", tenant_ids=[tenant_id])
    assert dry["status"] == "DRY_RUN_PASSED", [c for c in dry.get("checks", []) if not c["ok"]] or dry.get("error")
    assert sorted(r["variant"] for r in dry["carried"]) == [
        "1.5XXL CBC V2", "BE 6 One B59 R18 C11", "BE 6 One B59 R18 NCH",
        "BE 6 SPORTEQ THREE B79 R19 C11", "BE 6 SPORTEQ THREE B79 R19 NCH"]
    assert dry["notCarried"] and {r["model"] for r in dry["notCarried"]} == {"SCORPIO N"}
    assert _versions(engine, tenant_id) == 5  # a dry run leaves nothing

    (done,) = run_master_reload(engine, f"{tag}-octcarry-commit", tenant_ids=[tenant_id])
    assert done["status"] == "COMMITTED", [c for c in done.get("checks", []) if not c["ok"]] or done.get("error")
    assert _versions(engine, tenant_id) == 6
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        veero = find_book_sku(conn, tenant_id=tenant_id, model_text="VEERO", variant_text="1.5XXL CBC V2",
                              effective_on=date(2026, 10, 20))
        scorpio = find_book_sku(conn, tenant_id=tenant_id, model_text="SCORPIO N",
                                variant_text="Z8 S D AT 2WD 7 STR BS6.2 - N", effective_on=date(2026, 10, 20))
        marked = conn.execute(text(
            "SELECT count(*) FROM auditcore.price_list_items WHERE tenant_id = :t AND price_list_version_id = :v "
            "AND metadata ? 'carriedForwardFrom'"), {"t": tenant_id, "v": done["newVersionId"]}).scalar_one()
    assert veero["sku"] and veero["sku"]["model_name"] == "VEERO"
    assert scorpio["sku"]["model_name"] == "NEW SCORPIO N"  # October's new Scorpio N, at October's price
    assert marked > 0
    # Nothing left to carry: a second run publishes nothing.
    with engine.connect() as connection, connection.begin():
        again = complete_october_tenant(connection, tenant_id=tenant_id, tag=f"{tag}-octcarry-again", store=False)
    assert again["passed"] and again["carried"] == []
