"""Project history of a person: folding of repeated rows (pure) and the SuperAdmin-only read."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from audit_core.main import app
from audit_core.user_project_history import collapse_assignments

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)


def _row(tenant="t1", role="PC", outlet="o1", status="ACTIVE", start=0, end=None, project="Hyundai Orissa"):
    return {
        "tenant_id": tenant, "business_role_code": role, "dealer_id": "d1", "outlet_id": outlet,
        "assignment_status": status, "effective_from": NOW + timedelta(days=start),
        "effective_to": None if end is None else NOW + timedelta(days=end),
        "project_name": project, "business_code": "JBR-02", "dealer_name": "Utkal Hyundai",
        "outlet_name": "Utkal Hyundai - Pahal",
    }


def test_repeated_rows_from_edits_fold_into_one_line_with_the_first_start():
    rows = [
        _row(status="INACTIVE", start=-30, end=-10),  # closed when the mapping was edited
        _row(status="INACTIVE", start=-10, end=-2),   # closed again
        _row(start=-2),                               # the live one
    ]
    [line] = collapse_assignments(rows, NOW)
    assert line["current"] is True and line["until"] is None
    assert line["since"] == NOW - timedelta(days=30)


def test_an_ended_assignment_keeps_its_last_end_and_is_not_current():
    rows = [_row(status="INACTIVE", start=-60, end=-40), _row(status="INACTIVE", start=-40, end=-20)]
    [line] = collapse_assignments(rows, NOW)
    assert line["current"] is False and line["until"] == NOW - timedelta(days=20)


def test_projects_roles_and_outlets_stay_separate_and_current_comes_first():
    rows = [
        _row(tenant="t0", project="Mahindra Orissa", status="INACTIVE", start=-90, end=-30),
        _row(),
        _row(role="TL", outlet=None, start=-1),
    ]
    lines = collapse_assignments(rows, NOW)
    assert [(l["projectName"], l["roleCode"], l["current"]) for l in lines] == [
        ("Hyundai Orissa", "PC", True), ("Hyundai Orissa", "TL", True), ("Mahindra Orissa", "PC", False)]


def test_a_future_dated_row_is_not_current():
    [line] = collapse_assignments([_row(start=3)], NOW)
    assert line["current"] is False


def test_only_a_super_admin_may_read_it():
    response = TestClient(app).get("/v1/admin/users/someone/project-assignments")
    assert response.status_code in (401, 403)


# ---- against the database (runs where DATABASE_URL is set, as in CI) --------------------------

import os
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from sqlalchemy import create_engine, text

from audit_core.user_project_history import load_user_projects


@pytest.fixture()
def two_projects():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is required")
    engine = create_engine(url)
    suffix = uuid4().hex[:10]
    actor = f"hist-user-{suffix}"
    tenants = [f"tenant-hist-a-{suffix}", f"tenant-hist-b-{suffix}"]
    with engine.begin() as c:
        cat = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"HX-{suffix}"},
        ).scalar_one()
        oem = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"HY-{suffix}"},
        ).scalar_one()
        for index, tenant in enumerate(tenants):
            c.execute(
                text(
                    "INSERT INTO auditcore.projects (tenant_id, project_code, project_name, oem_id,"
                    " product_category_id, effective_start_date) VALUES (:t, :c, :n, :o, :k, CURRENT_DATE)"
                ),
                {"t": tenant, "c": f"HQ{index}-{suffix}", "n": f"History Project {'AB'[index]}", "o": oem, "k": cat},
            )
            dealer = c.execute(
                text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) VALUES (:t, :c, 'Hist Dealer') RETURNING dealer_id"),
                {"t": tenant, "c": f"HD{index}-{suffix}"},
            ).scalar_one()
            outlet = c.execute(
                text(
                    "INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name)"
                    " VALUES (:t, :d, :c, 'Hist Outlet') RETURNING outlet_id"
                ),
                {"t": tenant, "d": dealer, "c": f"HO{index}-{suffix}"},
            ).scalar_one()
            rows = (
                # project A: closed by an edit, then live again; project B: worked there, ended
                [("INACTIVE", "now() - interval '30 days'", "now() - interval '5 days'"), ("ACTIVE", "now() - interval '5 days'", None)]
                if index == 0
                else [("INACTIVE", "now() - interval '90 days'", "now() - interval '40 days'")]
            )
            for status, start, end in rows:
                c.execute(
                    text(
                        "INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code,"
                        f" dealer_id, outlet_id, assignment_status, effective_from, effective_to) VALUES (:t, :a, 'PC', :d, :o, :s, {start}, "
                        + (end or "NULL")
                        + ")"
                    ),
                    {"t": tenant, "a": actor, "d": dealer, "o": outlet, "s": status},
                )
    yield engine, actor, tenants
    for tenant in tenants:
        delete_tenant_data(engine, tenant)
    engine.dispose()


def test_a_person_tagged_in_two_projects_shows_the_current_one_first_and_the_ended_one_after(two_projects):
    engine, actor, _ = two_projects
    lines = load_user_projects(engine, actor)
    assert [(l["projectName"], l["current"]) for l in lines] == [("History Project A", True), ("History Project B", False)]
    current, ended = lines
    assert current["until"] is None and current["outletName"] == "Hist Outlet" and current["dealerName"] == "Hist Dealer"
    assert (current["since"].date() - ended["until"].date()).days > 0
    assert load_user_projects(engine, f"nobody-{uuid4().hex}") == []
