"""Insurance code repair (2026-10-27): Journeys whose agent / MISP code holds a
phone number are rebuilt from the newest live reading first, and only those
still wrong are read again by Document Intelligence; a dry run changes nothing."""
from __future__ import annotations

import pytest
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import uc03_insurance_code_repair as repair
from audit_core import uc03_statement_reread
from audit_core.db import set_tenant_context


@pytest.mark.parametrize("value", ["7873999100", "+91 78739 99101", "07873999101", "1800-220-233"])
def test_phone_numbers_are_caught(value: str) -> None:
    assert repair.looks_like_phone_number(value)


@pytest.mark.parametrize("value", [None, "", "MIBL/M&M/AAECA5786A/000", "BA0000123456", "A011351AD04"])
def test_real_codes_are_left_alone(value: str | None) -> None:
    assert not repair.looks_like_phone_number(value)


@pytest.fixture
def journey():
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="insrepair")
    with engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.insurance_records (tenant_id, journey_id, agent_intermediary_name, "
                 "agent_intermediary_code) VALUES (:t, :j, 'ADITYA MOTORS', '7873999100')"),
            {"t": journey.tenant_id, "j": journey.journey_id})
    yield journey
    engine.dispose()


def _code(journey) -> str:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT agent_intermediary_code FROM auditcore.insurance_records WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id}).scalar_one()


def test_a_dry_run_lists_and_changes_nothing(journey, monkeypatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(repair, "_rebuild", lambda *a, **k: called.append("rebuild"))
    monkeypatch.setattr(uc03_statement_reread, "_reread", lambda *a, **k: called.append("reread") or {})
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert [j["agent_intermediary_code"] for j in repair.affected_journeys(connection, journey.tenant_id)] == ["7873999100"]
    repair.run_insurance_code_repair(journey.engine, "t-dry", [journey.tenant_id])
    assert called == [] and _code(journey) == "7873999100"


def test_commit_rebuilds_first_and_rereads_only_what_is_still_wrong(journey, monkeypatch) -> None:
    def fixed_by_newest_reading(engine, tenant_id, journey_id):
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            connection.execute(
                text("UPDATE auditcore.insurance_records SET agent_intermediary_code='MIBL/M&M/AAECA5786A/000' "
                     "WHERE tenant_id=:t AND journey_id=:j"), {"t": tenant_id, "j": journey_id})

    reread: list[object] = []
    monkeypatch.setattr(repair, "_rebuild", fixed_by_newest_reading)
    monkeypatch.setattr(uc03_statement_reread, "_reread", lambda engine, t, items, tag, **k: reread.extend(items) or {})
    repair.run_insurance_code_repair(journey.engine, "t-commit", [journey.tenant_id])
    assert _code(journey) == "MIBL/M&M/AAECA5786A/000"
    assert reread == []  # fixed by the rebuild: Document Intelligence not asked


def test_the_job_kind_is_registered() -> None:
    from audit_core.uc03_p2_worker import _one_shot_target

    engine = database_engine()
    try:
        target, _args = _one_shot_target("INSURANCE_CODE_REPAIR", engine=engine, tag="x", reports=[])
    finally:
        engine.dispose()
    assert target is repair.run_insurance_code_repair


def _patch_materializers(monkeypatch, fill):
    from audit_core import uc03_delivery_post_extraction_materialization as mat

    monkeypatch.setattr(mat, "materialize_booking_documents_from_durable_store", lambda connection, **k: 0)
    monkeypatch.setattr(mat, "materialize_delivery_documents_from_durable_store",
                        lambda connection, *, tenant_id, journey_id: fill(connection, tenant_id, journey_id))


def _set_source(journey, kind: str) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.insurance_records SET source_kind=:k WHERE tenant_id=:t AND journey_id=:j"),
                           {"k": kind, "t": journey.tenant_id, "j": journey.journey_id})


def _fills_empty_code_with(value):
    # Like the real rebuild: fills an empty column only.
    def fill(connection, tenant_id, journey_id):
        connection.execute(
            text("UPDATE auditcore.insurance_records SET agent_intermediary_code=COALESCE(agent_intermediary_code, :v) "
                 "WHERE tenant_id=:t AND journey_id=:j"), {"v": value, "t": tenant_id, "j": journey_id})
        return 1
    return fill


def test_the_rebuild_clears_the_phone_number_so_the_newest_reading_can_replace_it(journey, monkeypatch) -> None:
    _set_source(journey, "EVIDENCE")
    _patch_materializers(monkeypatch, _fills_empty_code_with("MIBL/M&M/AAECA5786A/000"))
    assert repair._rebuild(journey.engine, journey.tenant_id, journey.journey_id) == ["agent_intermediary_code"]
    assert _code(journey) == "MIBL/M&M/AAECA5786A/000"


@pytest.mark.parametrize("reading", [None, "7873999101"])
def test_without_a_proper_reading_the_journey_is_left_as_it_was(journey, monkeypatch, reading) -> None:
    _set_source(journey, "EVIDENCE")
    _patch_materializers(monkeypatch, _fills_empty_code_with(reading))
    assert repair._rebuild(journey.engine, journey.tenant_id, journey.journey_id) == []
    assert _code(journey) == "7873999100"  # rolled back, not emptied


def test_a_value_a_person_entered_is_never_cleared(journey, monkeypatch) -> None:
    _set_source(journey, "OPERATIONAL_INPUT")  # entered by a person
    _patch_materializers(monkeypatch, _fills_empty_code_with("MIBL/M&M/AAECA5786A/000"))
    assert repair._rebuild(journey.engine, journey.tenant_id, journey.journey_id) == []
    assert _code(journey) == "7873999100"
