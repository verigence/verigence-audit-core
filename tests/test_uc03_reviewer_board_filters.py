"""The Reviewer's board by Outlet and by upload date (product owner, 2026-10-23).

Runs against real Postgres, calling the endpoint function directly the way the
other Phase 2 reviewer tests do.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi import HTTPException
from p2_support import AllowAllAuthorization, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_reviewer import list_reviewer_journeys


@pytest.fixture
def board():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2bd")
    reviewer = f"reviewer-{uuid4().hex[:8]}"
    with engine.begin() as connection:
        set_tenant_context(connection, created.tenant_id)
        # A second Outlet of the same Dealer, and a Reviewer mapped to the Dealer.
        second_outlet = connection.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'Second Outlet') RETURNING outlet_id"),
            {"t": created.tenant_id, "d": created.dealer_id, "c": f"P2-O2-{uuid4().hex[:6]}"},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
                 "dealer_id) VALUES (:t, :a, 'REVIEWER', :d)"),
            {"t": created.tenant_id, "a": reviewer, "d": created.dealer_id},
        )

        def add(outlet_id, reference: str, created_at: str) -> None:
            connection.execute(
                text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, "
                     "journey_reference, created_at_utc) VALUES (:t, :d, :o, :cu, :r, CAST(:at AS timestamptz))"),
                {"t": created.tenant_id, "d": created.dealer_id, "o": outlet_id, "cu": created.customer_id,
                 "r": reference, "at": created_at},
            )

        # The fixture's own journey is on the first outlet, created now.
        # Times are UTC; the Project is Asia/Kolkata (UTC+5:30), so 19:00 UTC on
        # the 10th is already the 11th there.
        add(created.outlet_id, "A-10", "2026-10-10 06:00:00+00")
        add(created.outlet_id, "A-11-late-utc", "2026-10-10 19:00:00+00")
        add(created.outlet_id, "A-12", "2026-10-12 06:00:00+00")
        add(second_outlet, "B-10", "2026-10-10 06:00:00+00")
    try:
        yield SimpleNamespace(engine=engine, tenant_id=created.tenant_id, reviewer=reviewer,
                              outlet_a=created.outlet_id, outlet_b=second_outlet, fixture_ref=None)
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _list(board, **filters):
    with board.engine.begin() as connection:
        return list_reviewer_journeys(
            tenant_id=board.tenant_id,
            human_principal=SimpleNamespace(subject=board.reviewer),
            authorization_client=AllowAllAuthorization(role_key="REVIEWER"),
            connection=connection,
            state="open",
            limit=200,
            outlet_id=filters.get("outlet_id"),
            date_from=filters.get("date_from"),
            date_to=filters.get("date_to"),
        )


def _refs(result, outlet_id) -> list[tuple[str, str]]:
    (group,) = [g for g in result["outlets"] if g["outletId"] == str(outlet_id)]
    return [(j["uploadDate"], j["journeyReference"]) for j in group["journeys"]]


def test_the_board_lists_every_outlet_with_the_oldest_upload_day_first(board):
    result = _list(board)
    a = _refs(result, board.outlet_a)
    # The late-UTC journey belongs to the 11th in the Project's timezone.
    assert [r for r in a if r[1].startswith("A-")] == [
        ("2026-10-10", "A-10"), ("2026-10-11", "A-11-late-utc"), ("2026-10-12", "A-12")]
    assert a == sorted(a, key=lambda r: r[0])
    assert _refs(result, board.outlet_b) == [("2026-10-10", "B-10")]
    assert result["timezone"] == "Asia/Kolkata"
    assert result["totalMatching"] == result["journeyCount"] == 5  # four above plus the fixture's own


def test_an_outlet_and_a_date_range_narrow_the_list_but_not_the_picker(board):
    result = _list(board, outlet_id=board.outlet_b)
    assert [g["outletId"] for g in result["outlets"]] == [str(board.outlet_b)]
    assert result["filters"]["outletId"] == str(board.outlet_b)
    # The picker still offers both outlets, with their own counts.
    counts = {o["outletId"]: o["journeyCount"] for o in result["outletOptions"]}
    assert counts == {str(board.outlet_a): 4, str(board.outlet_b): 1}

    ranged = _list(board, date_from=date(2026, 10, 11), date_to=date(2026, 10, 11))
    assert [(r[1]) for g in ranged["outlets"] for r in _refs(ranged, g["outletId"])] == ["A-11-late-utc"]
    assert ranged["totalMatching"] == 1


def test_a_reversed_date_range_is_refused(board):
    with pytest.raises(HTTPException) as caught:
        _list(board, date_from=date(2026, 10, 12), date_to=date(2026, 10, 10))
    assert caught.value.status_code == 422
