"""Verified bookings (product owner 2026-11-17): a booking a Team Lead or Reviewer
checked against its papers is kept with its figures; the system says when it no
longer shows the same, and whether the papers changed (verify again) or the
system moved it. Real Postgres."""
from __future__ import annotations

from decimal import Decimal as D

import pytest
from conftest import delete_tenant_data
from p2_support import add_receipt_payment, create_p2_journey, database_engine
from sqlalchemy import text
from test_ndc_three_sums import PRINTED, _ndc
from test_uc03_account_statement import _set_policy

from audit_core.db import set_tenant_context
from audit_core.uc03_deal_source_policy import POLICY_ACCOUNT_STATEMENT
from audit_core.uc03_verified_bookings import (
    booking_figures,
    check_verified,
    compare,
    listing,
    mark_verified,
    papers,
    withdraw,
)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="vfb")
    _set_policy(created, POLICY_ACCOUNT_STATEMENT)
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _mark(journey) -> dict:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return mark_verified(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id, actor_id="tl-1",
                             actor_name="Team Lead One", role="TL", comment="NDC, receipts and loan checked")


def _list(journey) -> list[dict]:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return listing(c, tenant_id=journey.tenant_id)


def test_the_figures_kept_are_the_screens_figures(journey) -> None:
    _ndc(journey, **PRINTED)
    add_receipt_payment(journey, amount="11000", receipt_number="KRM/2627/3592", receipt_date="2026-10-01")
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        figures = booking_figures(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert D(figures["money"]["receipts"]) == D(11000)
    assert figures["ndcPayments"]["status"] == "SHORT" and D(figures["ndcPayments"]["receiptsShort"]) == D(100000)
    assert set(figures) == {"deal", "money", "ndcPayments", "auditSummary", "openTasks"}


def test_a_verified_booking_matches_until_something_moves_it(journey) -> None:
    _ndc(journey, **PRINTED)
    add_receipt_payment(journey, amount="11000", receipt_number="KRM/2627/3592", receipt_date="2026-10-01")
    _mark(journey)
    [item] = _list(journey)
    assert item["verifiedBy"] == "Team Lead One" and item["lastCheck"]["result"] == "MATCHES"

    first = check_verified(journey.engine, journey.tenant_id, "DAILY")
    assert (first["bookings"], first["MATCHES"], first["CHANGED"]) == (1, 1, 0)

    # A receipt uploaded: the papers changed, and the figures with them.
    add_receipt_payment(journey, amount="100000", receipt_number="KRM/2627/3677", receipt_date="2026-10-02")
    second = check_verified(journey.engine, journey.tenant_id, "DAILY")
    assert second["PAPERS_CHANGED"] == 1
    [item] = _list(journey)
    assert item["lastCheck"]["result"] == "PAPERS_CHANGED" and item["lastCheck"]["papersChanged"] is True
    fields = {c["field"] for c in item["lastCheck"]["changes"]}
    assert "money.receipts" in fields and "ndcPayments.receiptsShort" in fields


def test_the_system_moving_a_figure_on_the_same_papers_is_changed(journey, monkeypatch) -> None:
    from audit_core import uc03_verified_bookings as module

    _ndc(journey, **PRINTED)
    add_receipt_payment(journey, amount="11000", receipt_number="KRM/2627/3592", receipt_date="2026-10-01")
    _mark(journey)
    real = module.booking_figures

    def moved(connection, *, tenant_id, journey_id):  # a code change that moves the payable
        figures = real(connection, tenant_id=tenant_id, journey_id=journey_id)
        figures["deal"]["payable"] = "1.00"
        return figures

    monkeypatch.setattr(module, "booking_figures", moved)
    result = check_verified(journey.engine, journey.tenant_id, "DEPLOY")
    assert result["CHANGED"] == 1 and result["moved"][0]["changes"][0]["field"] == "deal.payable"


def test_a_withdrawn_booking_is_no_longer_compared(journey) -> None:
    _mark(journey)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        assert withdraw(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id, actor_id="tl-1",
                        reason="wrong booking") is True
        assert withdraw(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id, actor_id="tl-1",
                        reason=None) is False
    assert check_verified(journey.engine, journey.tenant_id, "DAILY")["bookings"] == 0
    assert _list(journey) == []
    # marked again, it is verified again with today's figures
    _mark(journey)
    assert len(_list(journey)) == 1


def test_the_papers_fingerprint_follows_a_correction(journey) -> None:
    from p2_support import add_extracted_field

    _ndc(journey, **PRINTED)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        before = papers(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        document_id = c.execute(text("SELECT di_document_id FROM auditcore.evidence WHERE tenant_id=:t AND "
                                     "journey_id=:j"), {"t": journey.tenant_id, "j": journey.journey_id}).scalar_one()
    add_extracted_field(journey, di_document_id=document_id, field_key="net_value", value="852090",
                        document_type="customer_ledger")
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        assert papers(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id) != before


def test_amounts_within_a_rupee_have_not_moved() -> None:
    assert compare({"a": {"b": "100.00"}}, {"a": {"b": "100.40"}}) == []
    assert compare({"a": {"b": "100.00"}}, {"a": {"b": "101.00"}}) == [
        {"field": "a.b", "verified": "100.00", "now": "101.00"}]
    assert compare({"t": {"X": 1}}, {"t": {}}) == [{"field": "t.X", "verified": 1, "now": None}]


def test_the_check_is_a_daily_job_on_the_jobs_page_and_runs_after_each_deploy(monkeypatch) -> None:
    from datetime import UTC, datetime

    from audit_core import job_schedule
    from audit_core import uc03_p2_worker as worker

    assert job_schedule.DAILY_JOBS["VERIFIED_CHECK"].default_ist == "21:40"
    monkeypatch.setattr(worker, "_VERIFIED_CHECK_UTC", "16:10")
    worker._last_verified_check.pop("t-x", None)
    assert worker._verified_check_due("t-x", datetime(2026, 11, 17, 16, 0, tzinfo=UTC)) is None
    assert worker._verified_check_due("t-x", datetime(2026, 11, 17, 16, 11, tzinfo=UTC)) == "2026-11-17"
    target, _ = worker._one_shot_target("VERIFIED_BOOKINGS_CHECK", None, "t-x:now", [])
    assert target.__name__ == "run_verified_check"


def test_a_commit_job_runs_only_after_its_dry_run() -> None:
    """Product owner 2026-11-17 (introspection): dry-run on live data and check a sample
    before applying. A '-commit' job with no dry run of the same kind for the Project in the
    last 24 hours is refused."""
    from sqlalchemy import text

    from audit_core.db import set_platform_super_admin_context
    from audit_core.uc03_p2_worker import dry_run_missing

    engine = database_engine()
    tenant = "tenant-dryrun-test"
    try:
        with engine.begin() as c:
            set_platform_super_admin_context(c)
            job = {"job_kind": "RULES_RERUN", "tag": f"{tenant}:fix-commit"}
            assert "Refused" in dry_run_missing(c, job)
            assert dry_run_missing(c, {"job_kind": "RULES_RERUN", "tag": f"{tenant}:fix"}) is None
            c.execute(text("INSERT INTO auditcore.p2_worker_jobs (job_kind, tag, status, finished_at_utc) "
                           "VALUES ('RULES_RERUN', :t, 'DONE', now())"), {"t": f"{tenant}:fix"})
            assert dry_run_missing(c, job) is None
            # another kind's dry run does not count
            assert "Refused" in dry_run_missing(c, {"job_kind": "PAYMENT_RECEIPTS_ONLY", "tag": f"{tenant}:x-commit"})
            c.execute(text("DELETE FROM auditcore.p2_worker_jobs WHERE tag LIKE :t"), {"t": f"{tenant}:%"})
    finally:
        engine.dispose()


def test_the_code_version_is_the_deployed_commit(monkeypatch) -> None:
    """The deploy workflow uploads the code with a BUILD_SHA file and no git metadata: the
    after-deploy comparison found no version and never ran (live 17 Nov)."""
    from audit_core import uc03_verified_bookings as module

    monkeypatch.setenv("VERIGENCE_GIT_SHA", "4c2da234d09064be")
    assert module.code_version() == "4c2da234d090"
