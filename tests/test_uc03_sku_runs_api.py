"""Administration > Run SKU Resolution: Check (dry run) and Apply from the page (2026-10-21).

The product owner reviews every Journey's SKU and applies the changes himself,
so the route has a ``mode`` (RESOLVE as before, RERESOLVE_DRY_RUN = "Check
SKUs", RERESOLVE_COMMIT = "Apply"). Apply is refused with a 409 unless a
complete check started in the last 24 hours and no run has changed Journey
data since. A check writes no Journey data at all.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text
from test_uc03_model_resolution import _seed_price_list, _set_journey_product
from test_uc03_sku_ladder import _booking_form

import audit_core.uc03_backfill_document_sync_producers as backfill
from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_engine,
    require_super_admin_request,
)
from audit_core.main import app
from audit_core.security_integration import SecurityAdminContext

_THAR = [
    {"model": "THAR", "variant": "LX", "components": {"EX_SHOWROOM": "1400000"}},
    {"model": "THAR", "variant": "AX", "components": {"EX_SHOWROOM": "1300000"}},
]
_SNAPSHOT_TABLES = ("journeys", "journey_products", "p2_tasks", "audit_findings", "p2_activity_events",
                    "journey_stage_states")


def _add_journey(engine, base, label):
    with engine.begin() as c:
        journey_id = c.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, journey_reference) "
                 "VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"),
            {"t": base.tenant_id, "d": base.dealer_id, "o": base.outlet_id, "cu": base.customer_id,
             "r": f"{label}-{uuid4().hex[:6]}"},
        ).scalar_one()
        _booked(c, base.tenant_id, journey_id)
    return journey_id


def _booked(c, tenant_id, journey_id):
    """A booking date and an open Booking stage."""
    c.execute(text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date) "
                   "VALUES (:t, :j, CURRENT_DATE - 10)"), {"t": tenant_id, "j": journey_id})
    c.execute(
        text("INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code, business_status, "
             "audit_state, audit_status, first_started_at_utc, latest_activity_at_utc, version_no) "
             "VALUES (:t, :j, 'BOOKING', 'BOOKING_IN_PROGRESS', 'IN_PROGRESS', 'NOT_EVALUATED', now(), now(), 1)"),
        {"t": tenant_id, "j": journey_id},
    )


def _conn_for(c, tenant_id, journey_id, oem_id):
    """The shape the shared test helpers expect."""
    return SimpleNamespace(execute=c.execute, tenant_id=tenant_id, journey_id=journey_id, oem_id=oem_id)


@pytest.fixture
def project():
    """A Project with three Journeys, committed (the route opens its own connections):

    * ``legacy``  -- an automatic pin on LX; its booking form names AX (a SKU change);
    * ``human``   -- a PC-confirmed pin on LX; its booking form names AX (left alone);
    * ``done``    -- review completed (left alone).
    """
    engine = database_engine()
    base = create_p2_journey(engine, prefix="skuapi")
    legacy = base.journey_id
    human = _add_journey(engine, base, "human")
    done = _add_journey(engine, base, "done")
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": base.tenant_id})
        oem_id = c.execute(text("SELECT oem_id FROM auditcore.projects WHERE tenant_id=:t"),
                           {"t": base.tenant_id}).scalar_one()
        _booked(c, base.tenant_id, legacy)
        lx, ax = _seed_price_list(_conn_for(c, base.tenant_id, legacy, oem_id), _THAR)
        for jid, pin in ((legacy, "AUTO_LEGACY"), (human, "PC_CONFIRMED")):
            h = _conn_for(c, base.tenant_id, jid, oem_id)
            _booking_form(h, {"vehicle_model": "THAR", "vehicle_variant": "AX", "ex_showroom_price": "1300000"})
            _set_journey_product(h, "THAR", "LX")
            c.execute(
                text("UPDATE auditcore.journey_products SET product_sku_id=:s, match_basis=:b, "
                     "selection_status=:st, selection_method=:m WHERE tenant_id=:t AND journey_id=:j"),
                {"s": lx, "b": pin, "st": "TENTATIVE" if pin == "AUTO_LEGACY" else "CONFIRMED",
                 "m": "MODEL_RESOLUTION_SYNC_V1" if pin == "AUTO_LEGACY" else "MODEL_RESOLUTION_PC_PICK_V1",
                 "t": base.tenant_id, "j": jid},
            )
        c.execute(text("UPDATE auditcore.journeys SET review_completed_at_utc = now() "
                       "WHERE tenant_id=:t AND journey_id=:j"), {"t": base.tenant_id, "j": done})
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[require_super_admin_request] = lambda: HumanAdminRequest(
        user_id="superadmin-sku-api", bearer_token="t",
        admin_context=SecurityAdminContext(user_id="superadmin-sku-api", is_super_admin=True, admin_scopes=()),
    )
    try:
        yield SimpleNamespace(engine=engine, tenant_id=base.tenant_id, legacy=legacy, human=human, done=done,
                              lx=lx, ax=ax, client=TestClient(app, raise_server_exceptions=False))
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, base.tenant_id)
        engine.dispose()


def _url(p, suffix=""):
    return f"/v1/tenants/{p.tenant_id}/admin/backfill/sku-resolution{suffix}"


def _run(p, mode=None, **params):
    if mode:
        params["mode"] = mode
    return p.client.post(_url(p), params={"limit": 25, **params})


def _snapshot(p):
    with p.engine.begin() as c:
        set_tenant_context(c, p.tenant_id)
        return {
            table: sorted(json.dumps(r, sort_keys=True, default=str) for r in c.execute(
                text(f"SELECT to_jsonb(t) FROM auditcore.{table} t WHERE tenant_id=:t"), {"t": p.tenant_id}).scalars())
            for table in _SNAPSHOT_TABLES
        }


def _sku_of(p, journey_id):
    with p.engine.begin() as c:
        set_tenant_context(c, p.tenant_id)
        return c.execute(text("SELECT product_sku_id, match_basis FROM auditcore.journey_products "
                              "WHERE tenant_id=:t AND journey_id=:j"), {"t": p.tenant_id, "j": journey_id}).one()


def _age_run(p, run_id, hours):
    with p.engine.begin() as c:
        set_tenant_context(c, p.tenant_id)
        c.execute(text("UPDATE auditcore.sku_resolution_runs SET started_at_utc = started_at_utc - make_interval(hours => :h) "
                       "WHERE tenant_id=:t AND run_id=:r"), {"h": hours, "t": p.tenant_id, "r": run_id})


def _detail(p, run_id):
    response = p.client.get(_url(p, f"/runs/{run_id}"))
    assert response.status_code == 200, response.text
    return response.json()


# ── the mode switch ──────────────────────────────────────────────────────────
def test_no_mode_is_the_old_resolve_run(project):
    response = _run(project)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["runMode"] == "RESOLVE" and body["checkedByRunId"] is None
    assert _detail(project, body["runId"])["runMode"] == "RESOLVE"


def test_an_unknown_mode_is_refused(project):
    assert _run(project, "WIPE_EVERYTHING").status_code == 400


def test_a_check_run_writes_no_journey_data_at_all(project):
    before = _snapshot(project)
    response = _run(project, "RERESOLVE_DRY_RUN")
    assert response.status_code == 200, response.text
    assert response.json()["runMode"] == "RERESOLVE_DRY_RUN"
    assert _snapshot(project) == before  # journeys, pins, tasks, findings, activity: all unchanged
    # ... and the pin is still the old one
    assert _sku_of(project, project.legacy) == (project.lx, "AUTO_LEGACY")
    # Control: the snapshot does see what Apply writes (the pin, a task, the activity log).
    assert _run(project, "RERESOLVE_COMMIT").status_code == 200
    after = _snapshot(project)
    assert all(after[t] != before[t] for t in ("journey_products", "p2_tasks", "p2_activity_events")), \
        [t for t in after if after[t] == before[t]]


def test_a_check_run_records_the_full_proposal_for_every_journey(project):
    body = _run(project, "RERESOLVE_DRY_RUN").json()
    detail = _detail(project, body["runId"])
    assert detail["runMode"] == "RERESOLVE_DRY_RUN"
    assert detail["actionCounts"] == {"CHANGE": 1, "LEFT_ALONE": 2}
    items = {i["journeyId"]: i for i in detail["items"]}
    p = items[str(project.legacy)]["proposal"]
    assert (p["oldBasis"], p["proposedBasis"], p["action"], p["changes"], p["group"]) == (
        "AUTO_LEGACY", "AUTO_MATCHED", "BOOKING_PIN", True, "CHANGE")
    assert p["oldSku"] != p["proposedSku"] and p["words"] == "THAR / AX"
    assert (p["masterExShowroom"], p["documentExShowroom"], p["priceCheck"]) == ("1300000.00", "1300000", "MATCH") \
        or p["priceCheck"] == "MATCH"
    assert p["booking"]["words"] == "THAR / AX" and p["booking"]["exShowroom"] is not None
    assert p["booking"]["masterExShowroom"] is not None and p["invoice"] is None
    assert isinstance(p["tasks"], list) and p["dryRun"] is True and "actionTaken" not in p
    human = items[str(project.human)]["proposal"]
    assert human["action"] == "KEEP_HUMAN" and human["group"] == "LEFT_ALONE" and human["changes"] is False
    done = items[str(project.done)]["proposal"]
    assert done["group"] == "LEFT_ALONE" and done["skipped"] == "review_completed"


def test_the_runs_list_carries_the_mode_and_counts(project):
    run_id = _run(project, "RERESOLVE_DRY_RUN").json()["runId"]
    runs = project.client.get(_url(project, "/runs")).json()
    mine = next(r for r in runs if r["runId"] == run_id)
    assert mine["runMode"] == "RERESOLVE_DRY_RUN" and mine["actionCounts"]["CHANGE"] == 1


# ── the Apply gate ───────────────────────────────────────────────────────────
def test_apply_without_a_check_is_refused_and_changes_nothing(project):
    before = _snapshot(project)
    response = _run(project, "RERESOLVE_COMMIT")
    assert response.status_code == 409
    assert "no completed check" in response.json()["detail"]
    assert _snapshot(project) == before
    assert project.client.get(_url(project, "/runs")).json() == []  # no run was started
    gate = project.client.get(_url(project, "/apply-check")).json()
    assert gate["allowed"] is False and gate["code"] == "NO_CHECK_RUN"


def test_apply_on_a_stale_check_is_refused(project):
    check = _run(project, "RERESOLVE_DRY_RUN").json()
    _age_run(project, check["runId"], 25)
    response = _run(project, "RERESOLVE_COMMIT")
    assert response.status_code == 409 and "more than 24 hours ago" in response.json()["detail"]
    assert _sku_of(project, project.legacy) == (project.lx, "AUTO_LEGACY")


def test_apply_while_the_check_is_unfinished_is_refused(project):
    _run(project, "RERESOLVE_DRY_RUN", limit=1)  # one Journey of three: still RUNNING
    response = _run(project, "RERESOLVE_COMMIT")
    assert response.status_code == 409 and "has not finished" in response.json()["detail"]


def test_apply_on_a_fresh_check_changes_the_sku_and_says_what_it_applied(project):
    check = _run(project, "RERESOLVE_DRY_RUN").json()
    response = _run(project, "RERESOLVE_COMMIT")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["runMode"] == "RERESOLVE_COMMIT" and body["checkedByRunId"] == check["runId"]
    assert body["check"]["willChange"] == 1 and body["check"]["leftAlone"] == 2
    assert (body["check"]["journeysChecked"], body["check"]["failed"], body["check"]["notChecked"]) == (3, 0, 0)
    assert "3 of 3 journeys checked, 0 failed, 0 not checked" in body["checkNote"]
    assert _sku_of(project, project.legacy) == (project.ax, "AUTO_MATCHED")
    assert _sku_of(project, project.human) == (project.lx, "PC_CONFIRMED")  # a PC pick is never touched
    detail = _detail(project, body["runId"])
    assert detail["checkedByRunId"] == check["runId"]
    items = {i["journeyId"]: i["proposal"] for i in detail["items"]}
    assert items[str(project.legacy)]["actionTaken"] == "REPLACED"
    assert items[str(project.legacy)]["differsFromCheck"] is False
    assert items[str(project.legacy)]["skuAfter"] is not None and items[str(project.legacy)]["dryRun"] is False
    assert items[str(project.human)]["actionTaken"] == "LEFT_ALONE"
    assert items[str(project.done)]["actionTaken"] == "LEFT_ALONE"


def test_a_check_is_applied_once(project):
    _run(project, "RERESOLVE_DRY_RUN")
    assert _run(project, "RERESOLVE_COMMIT").status_code == 200
    again = _run(project, "RERESOLVE_COMMIT")
    assert again.status_code == 409 and "changed journey data since the check" in again.json()["detail"]


def test_another_resolve_run_after_the_check_blocks_apply(project):
    _run(project, "RERESOLVE_DRY_RUN")
    assert _run(project).status_code == 200  # the old Run SKU Resolution changes data
    assert _run(project, "RERESOLVE_COMMIT").status_code == 409


def test_a_second_check_after_the_first_does_not_block_apply(project):
    _run(project, "RERESOLVE_DRY_RUN")
    _run(project, "RERESOLVE_DRY_RUN")  # a check changes nothing
    assert _run(project, "RERESOLVE_COMMIT").status_code == 200


def test_errors_in_the_check_do_not_block_apply_but_the_response_states_them(project, monkeypatch):
    import audit_core.uc03_model_resolution as mr
    real = mr.recheck_model_resolution
    state = {"armed": True}

    def flaky(connection, *, tenant_id, journey_id, correlation_id, commit):
        if state["armed"] and str(journey_id) == str(project.done):  # never reached: kept for clarity
            raise RuntimeError("x")
        if state["armed"] and str(journey_id) == str(project.human):
            return {"error": True, "errorType": "RuntimeError", "errorMessage": "boom"}
        return real(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
                    commit=commit)

    monkeypatch.setattr(mr, "recheck_model_resolution", flaky)
    check = _run(project, "RERESOLVE_DRY_RUN").json()
    assert _detail(project, check["runId"])["actionCounts"]["FAILED"] == 1
    state["armed"] = False
    response = _run(project, "RERESOLVE_COMMIT")
    assert response.status_code == 200, response.text
    assert response.json()["check"]["failed"] == 1 and "1 failed" in response.json()["checkNote"]


def test_a_journey_added_after_the_check_is_stated_as_not_checked(project):
    _run(project, "RERESOLVE_DRY_RUN")
    base = SimpleNamespace(tenant_id=project.tenant_id)
    with project.engine.begin() as c:
        row = c.execute(text("SELECT dealer_id, outlet_id, customer_id FROM auditcore.journeys "
                             "WHERE journey_id=:j"), {"j": project.legacy}).one()
    base.dealer_id, base.outlet_id, base.customer_id = row
    _add_journey(project.engine, base, "late")
    gate = project.client.get(_url(project, "/apply-check")).json()
    assert gate["allowed"] is True and gate["check"]["notChecked"] == 1
    response = _run(project, "RERESOLVE_COMMIT")
    assert response.status_code == 200 and response.json()["check"]["notChecked"] == 1


def test_a_run_is_continued_in_its_own_mode(project):
    first = _run(project, "RERESOLVE_DRY_RUN", limit=1).json()
    assert first["nextOffset"] == 1
    before = _snapshot(project)
    # No mode on the next batches: the run's own mode applies, still a check.
    second = _run(project, runId=first["runId"], offset=1, limit=25).json()
    assert second["runMode"] == "RERESOLVE_DRY_RUN" and second["nextOffset"] is None
    assert _snapshot(project) == before
    # Asking for a different mode on an existing run is refused.
    clash = _run(project, "RERESOLVE_COMMIT", runId=first["runId"], offset=1)
    assert clash.status_code == 409
    assert _run(project, runId=uuid4(), offset=0).status_code == 404


def test_the_worker_commit_path_is_gated_like_apply(project):
    """AUDIT_CORE_SKU_RUN_TAG "...-commit" (``reresolve_all_skus(commit=True)``) needs
    the same completed check from the last 24 hours as the Apply button (product
    owner, 2026-10-22); the dry-run path needs none."""
    before = _snapshot(project)
    with pytest.raises(backfill.CommitNotAllowed) as refused:
        backfill.reresolve_all_skus(project.engine, project.tenant_id, commit=True, batch=25)
    assert refused.value.detail["code"] == "NO_CHECK_RUN"
    assert _snapshot(project) == before  # nothing written, no run row made
    with project.engine.begin() as c:
        set_tenant_context(c, project.tenant_id)
        assert c.execute(text("SELECT count(*) FROM auditcore.sku_resolution_runs WHERE tenant_id=:t"),
                         {"t": project.tenant_id}).scalar_one() == 0

    check = backfill.reresolve_all_skus(project.engine, project.tenant_id, batch=25)
    assert check["runMode"] == "RERESOLVE_DRY_RUN"
    result = backfill.reresolve_all_skus(project.engine, project.tenant_id, commit=True, batch=25)
    assert result["runMode"] == "RERESOLVE_COMMIT" and result["checkedByRunId"] == check["runId"]
    # The check is used up by the Apply, as for the button.
    with pytest.raises(backfill.CommitNotAllowed) as again:
        backfill.reresolve_all_skus(project.engine, project.tenant_id, commit=True, batch=25)
    assert again.value.detail["code"] == "DATA_CHANGED_SINCE_CHECK"


def test_only_a_super_admin_may_run_it():
    app.dependency_overrides.clear()
    response = TestClient(app, raise_server_exceptions=False).post(
        "/v1/tenants/anything/admin/backfill/sku-resolution", params={"mode": "RERESOLVE_COMMIT"})
    assert response.status_code in (401, 403)
