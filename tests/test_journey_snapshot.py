"""JOURNEY_SNAPSHOT / JOURNEY_SNAPSHOT_DIFF (2026-11-02): every booking's
Journey 360 money tabs kept per tag, read only, and compared between tags."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import add_receipt_payment, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import uc03_journey_snapshot as snap
from audit_core import uc03_p2_journey360 as j360
from audit_core.db import set_tenant_context


def test_diff_paths_align_keyed_items_and_ignore_build_time():
    before = {"generatedAtUtc": "a", "categories": [
        {"code": "VEHICLE", "components": [{"key": "ex_showroom_price", "current": "100"},
                                           {"key": "tcs_amount", "current": None}]}]}
    after = {"generatedAtUtc": "b", "categories": [
        {"code": "VEHICLE", "components": [{"key": "insurance_amount", "current": "5"},
                                           {"key": "ex_showroom_price", "current": "100"},
                                           {"key": "tcs_amount", "current": "1"}]}]}
    assert snap.diff_paths(before, after) == [
        ("categories[code=VEHICLE].components[key=insurance_amount]", None, {"key": "insurance_amount",
                                                                            "current": "5"}),
        ("categories[code=VEHICLE].components[key=tcs_amount].current", None, "1"),
    ]
    assert snap.diff_paths([1, 2], [1, 2, 3]) == [("[]", "2 items", "3 items")]
    assert snap.strip_volatile({"generatedAtUtc": "x", "a": [{"generatedAtUtc": "y", "b": 1}]}) == {"a": [{"b": 1}]}


@pytest.fixture
def journey():
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="snap")
    try:
        yield journey
    finally:
        delete_tenant_data(engine, journey.tenant_id)
        engine.dispose()


def _rows(journey, tag):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [dict(r) for r in connection.execute(
            text("SELECT section, payload IS NOT NULL AS built, error FROM auditcore.p2_journey_snapshots "
                 "WHERE tenant_id=:t AND tag=:g ORDER BY section"), {"t": journey.tenant_id, "g": tag}).mappings()]


def test_every_section_is_kept_and_an_unchanged_booking_shows_no_difference(journey):
    out = snap.run_journey_snapshot(journey.engine, f"{journey.tenant_id}:before", ["all"])
    assert out["bookings"] == 1 and out["built"] + out["failed"] == len(snap.SECTIONS)
    rows = _rows(journey, "before")
    assert sorted(r["section"] for r in rows) == sorted(snap.SECTIONS)
    assert all(r["built"] or r["error"] for r in rows)
    snap.run_journey_snapshot(journey.engine, f"{journey.tenant_id}:after", ["all"])
    same = snap.run_journey_snapshot_diff(journey.engine, f"{journey.tenant_id}:diff", ["before", "after"])
    assert all(s["changedBookings"] == 0 and s["newErrors"] == 0 for s in same["sections"].values())

    # A receipt added between the two runs shows up as a Payments change.
    add_receipt_payment(journey, amount="21000", receipt_number="R-1", receipt_date="2026-10-01")
    snap.run_journey_snapshot(journey.engine, f"{journey.tenant_id}:later", ["all", "section:payments"])
    moved = snap.run_journey_snapshot_diff(journey.engine, f"{journey.tenant_id}:diff", ["after", "later"])
    payments = moved["sections"]["payments"]
    assert payments["changedBookings"] == 1
    assert any(path.startswith("items") or path == "receiptsTotal" for path in payments["fields"])
    assert moved["sections"]["deal"]["changedBookings"] == 0


def test_a_section_that_tries_to_write_fails_and_writes_nothing(journey, monkeypatch):
    def writer(connection, *, tenant_id, journey_id):
        connection.execute(text("UPDATE auditcore.journeys SET journey_reference='CHANGED' "
                                "WHERE tenant_id=:t AND journey_id=:j"), {"t": tenant_id, "j": journey_id})
        return {}

    monkeypatch.setitem(j360.BUILDERS, "deal", writer)
    out = snap.run_journey_snapshot(journey.engine, f"{journey.tenant_id}:ro", ["all", "section:deal"])
    assert out["failed"] == 1 and "read-only" in out["errors"][0]["error"].lower()
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert connection.execute(text("SELECT journey_reference FROM auditcore.journeys WHERE tenant_id=:t"),
                                  {"t": journey.tenant_id}).scalar_one() != "CHANGED"


def test_only_the_newest_tags_are_kept(journey, monkeypatch):
    monkeypatch.setattr(snap, "_KEEP_TAGS", 2)
    for label in ("one", "two", "three"):
        out = snap.run_journey_snapshot(journey.engine, f"{journey.tenant_id}:{label}", ["all", "section:tradein"])
    assert out["prunedTags"] == ["one"]
    assert _rows(journey, "one") == [] and _rows(journey, "three")


def test_a_bad_tag_or_section_is_refused(journey):
    with pytest.raises(ValueError):
        snap.run_journey_snapshot(journey.engine, "no-label", ["all"])
    with pytest.raises(ValueError):
        snap.run_journey_snapshot(journey.engine, f"{journey.tenant_id}:x", ["section:nope"])
    with pytest.raises(ValueError):
        snap.run_journey_snapshot_diff(journey.engine, f"{journey.tenant_id}:x", ["only-one"])
