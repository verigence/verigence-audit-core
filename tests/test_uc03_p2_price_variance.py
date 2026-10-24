"""The Journey list's price variance is the Deal tab's own number, stored by
the worker on the Journey's runtime row (real Postgres)."""
from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import AllowAllAuthorization, create_p2_journey, database_engine
from sqlalchemy import text
from test_uc03_p2_journey360 import _seed_deal

from audit_core import uc03_p2_price_variance as price_variance
from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_p2_journey360 import deal
from audit_core.uc03_p2_runtime import note_facts_changed


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2pv")
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=created.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    price_variance._backfill_tried.clear()
    try:
        yield created
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _stored(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT price_variance FROM auditcore.p2_journey_runtime WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one_or_none()


def _listed(journey):
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(f"/p2/v1/tenants/{journey.tenant_id}/journeys", params={"state": "open"})
    assert response.status_code == 200, response.text
    return next(i for i in response.json()["items"] if i["journey_id"] == str(journey.journey_id))["price_variance"]


def _runtime_row(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        note_facts_changed(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, reason="TEST")


def _set(journey, value, *, fresh: bool = True):
    """Put a value in the cache. ``fresh`` stamps the Journey's current fact
    version alongside it, as a real refresh does; without it the value is a
    copy left behind by a change that never recomputed it."""
    version = "fact_version" if fresh else "price_variance_fact_version"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text(f"UPDATE auditcore.p2_journey_runtime "
                 f"SET price_variance=:v, price_variance_fact_version={version} "
                 f"WHERE tenant_id=:t AND journey_id=:j"),
            {"v": value, "t": journey.tenant_id, "j": journey.journey_id},
        )


def _stored_version(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT fact_version, price_variance_fact_version FROM auditcore.p2_journey_runtime "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).one()


def test_the_worker_stores_the_deals_own_variance_and_the_list_shows_it(journey):
    _seed_deal(journey)
    _runtime_row(journey)  # a fact changed: a STAGE_RECOMPUTE is queued
    [item] = [w for w in worker._claim_for_tenant(journey.engine, journey.tenant_id, 10)
              if w.work_type == "STAGE_RECOMPUTE"]
    worker.process_work(journey.engine, item)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        expected = Decimal(deal(connection, tenant_id=journey.tenant_id,
                                journey_id=journey.journey_id)["summary"]["variance"]["currentVsStandard"])
    assert expected == 5000  # the cash discount given 5,000 under its entitlement
    assert _stored(journey) == expected
    assert Decimal(_listed(journey)) == expected


def test_the_list_reads_the_stored_value_and_falls_back_while_it_is_not_computed(journey):
    _seed_deal(journey)
    _runtime_row(journey)
    _set(journey, Decimal("123.00"))
    assert Decimal(_listed(journey)) == 123
    _set(journey, None)  # not computed yet: the list's previous calculation
    assert Decimal(_listed(journey)) == 5000


def test_the_list_never_shows_a_cached_variance_older_than_the_facts(journey):
    """The bug this guards (2026-10-24): a Journey's deal changed -- a SKU
    re-pinned by a resolution run -- and the list went on showing the figure
    from before, while Journey 360 recomputed live and showed another. One
    customer read +2,71,181 on the list against -9,407 on the Deal tab."""
    _seed_deal(journey)
    _runtime_row(journey)
    _set(journey, Decimal("271181.00"), fresh=False)
    assert Decimal(_listed(journey)) == 5000, "a stale copy must not reach the list"

    # and the sweep restores the Deal's own figure, stamped at these facts
    assert price_variance.backfill_price_variance(journey.engine, tenant_id=journey.tenant_id) == 1
    assert _stored(journey) == 5000
    current, cached = _stored_version(journey)
    assert cached == current
    assert Decimal(_listed(journey)) == 5000


def test_a_fact_change_makes_the_cached_variance_stale_and_the_sweep_picks_it_up(journey):
    _seed_deal(journey)
    _runtime_row(journey)
    assert price_variance.backfill_price_variance(journey.engine, tenant_id=journey.tenant_id) == 1
    current, cached = _stored_version(journey)
    assert cached == current
    # nothing has moved, so there is nothing to recompute
    assert price_variance.backfill_price_variance(journey.engine, tenant_id=journey.tenant_id) == 0

    _runtime_row(journey)  # a fact changed: the cached copy is now behind
    current, cached = _stored_version(journey)
    assert cached < current
    assert price_variance.backfill_price_variance(journey.engine, tenant_id=journey.tenant_id) == 1
    current, cached = _stored_version(journey)
    assert cached == current


def test_a_deal_that_cannot_be_built_never_breaks_the_recompute_or_overwrites(journey, monkeypatch):
    _seed_deal(journey)
    _runtime_row(journey)
    _set(journey, Decimal("77.00"))

    def boom(*args, **kwargs):
        raise RuntimeError("deal exploded")

    monkeypatch.setattr(price_variance, "deal", boom)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert price_variance.refresh_price_variance(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id) is None
        # the surrounding transaction is still usable
        assert connection.execute(text("SELECT 1")).scalar_one() == 1
    assert _stored(journey) == 77


def test_nothing_to_compare_is_zero_and_unchanged_values_are_not_rewritten(journey):
    _runtime_row(journey)  # no commercial data at all
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert price_variance.refresh_price_variance(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id) == 0
    assert _stored(journey) == 0


def test_backfill_fills_journeys_without_a_value_once_per_process(journey):
    _seed_deal(journey)
    _runtime_row(journey)
    assert _stored(journey) is None
    assert price_variance.backfill_price_variance(journey.engine, tenant_id=journey.tenant_id) == 1
    assert _stored(journey) == 5000
    # nothing left to fill: the value it wrote is stamped at the Journey's facts
    assert price_variance.backfill_price_variance(journey.engine, tenant_id=journey.tenant_id) == 0


def test_a_deal_that_cannot_be_built_is_not_retried_until_its_facts_move(journey, monkeypatch):
    _seed_deal(journey)
    _runtime_row(journey)

    def boom(*args, **kwargs):
        raise RuntimeError("deal exploded")

    monkeypatch.setattr(price_variance, "deal", boom)
    assert price_variance.backfill_price_variance(journey.engine, tenant_id=journey.tenant_id) == 0
    assert _stored(journey) is None
    # the same broken Deal is not rebuilt every sweep
    assert len(price_variance._backfill_tried) == 1
    assert price_variance.backfill_price_variance(journey.engine, tenant_id=journey.tenant_id) == 0

    _runtime_row(journey)  # its facts moved: worth another attempt
    monkeypatch.undo()
    assert price_variance.backfill_price_variance(journey.engine, tenant_id=journey.tenant_id) == 1
    assert _stored(journey) == 5000
