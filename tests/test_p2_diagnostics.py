"""The read-only diagnostics reports run against the real schema."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_diagnostics import REPORTS, run_diagnostics


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="diag")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


@pytest.mark.parametrize("name", sorted(REPORTS))
def test_every_report_runs_read_only(journey, name):
    with journey.engine.begin() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text(REPORTS[name]), {"t": journey.tenant_id}).mappings().all()


def test_run_logs_without_failing(journey, monkeypatch):
    from audit_core import uc03_p2_diagnostics as diag

    monkeypatch.setattr(diag, "_projects", lambda engine: [
        {"tenant_id": journey.tenant_id, "project_code": "X", "project_name": "X", "project_status": "ACTIVE"}])
    run_diagnostics(journey.engine, ["all"])
