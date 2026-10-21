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
