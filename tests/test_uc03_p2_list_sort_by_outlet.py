"""The TL's journey list, Outlet by Outlet (product owner, 2026-10-23)."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import AllowAllAuthorization, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import uc03_p2_api
from audit_core.db import set_tenant_context


@pytest.fixture
def tl_list():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2so")
    tl = f"tl-{uuid4().hex[:8]}"
    with engine.begin() as connection:
        set_tenant_context(connection, created.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code) "
                 "VALUES (:t, :a, 'TL')"),
            {"t": created.tenant_id, "a": tl},
        )
        # Outlet names sort "A Outlet" < "O" < "Z Outlet"; journeys alternate
        # between them with the most recently updated first overall.
        outlets = {}
        for name in ("A Outlet", "Z Outlet"):
            outlets[name] = connection.execute(
                text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                     "VALUES (:t, :d, :c, :n) RETURNING outlet_id"),
                {"t": created.tenant_id, "d": created.dealer_id, "c": f"C-{uuid4().hex[:6]}", "n": name},
            ).scalar_one()
        order = [("Z Outlet", "J1", 5), ("A Outlet", "J2", 4), ("Z Outlet", "J3", 3), ("A Outlet", "J4", 2)]
        for outlet, ref, days_ago in order:
            connection.execute(
                text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, "
                     "journey_reference, updated_at_utc) VALUES (:t, :d, :o, :cu, :r, now() - make_interval(days => :n))"),
                {"t": created.tenant_id, "d": created.dealer_id, "o": outlets[outlet], "cu": created.customer_id,
                 "r": ref, "n": days_ago},
            )
    try:
        yield SimpleNamespace(engine=engine, tenant_id=created.tenant_id, tl=tl)
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _page(tl_list, monkeypatch, **kwargs):
    monkeypatch.setattr(uc03_p2_api, "_authorize", lambda *args, **inner: None)
    with tl_list.engine.begin() as connection:
        set_tenant_context(connection, tl_list.tenant_id)
        return uc03_p2_api.list_p2_journeys(
            tenant_id=tl_list.tenant_id,
            human_principal=SimpleNamespace(subject=tl_list.tl),
            authorization_client=AllowAllAuthorization(role_key="TL"),
            connection=connection,
            view="all",
            limit=50,
            **kwargs,
        )


def test_sort_by_outlet_lists_one_outlet_after_another(tl_list, monkeypatch):
    by_outlet = _page(tl_list, monkeypatch, sort="outlet")["items"]
    refs = [(i["outlet_name"], i["journey_reference"]) for i in by_outlet if i["journey_reference"].startswith("J")]
    # A Outlet first, then Z Outlet; inside each, the most recently updated first.
    assert refs == [("A Outlet", "J4"), ("A Outlet", "J2"), ("Z Outlet", "J3"), ("Z Outlet", "J1")]
    names = [i["outlet_name"] for i in by_outlet]
    assert names == sorted(names, key=str.lower)


def test_the_default_order_is_unchanged(tl_list, monkeypatch):
    default = _page(tl_list, monkeypatch)["items"]
    refs = [i["journey_reference"] for i in default if i["journey_reference"].startswith("J")]
    assert refs == ["J4", "J3", "J2", "J1"]  # most recently updated first, outlets mixed
