from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from audit_core.uc03_nightly_reprocessing_reports import (
    NightlyReprocessingRunReport,
    get_nightly_reprocessing_status,
    report_nightly_reprocessing_run,
)


@pytest.fixture
def engine():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    yield engine
    engine.dispose()


def test_report_then_read_back_a_run(engine) -> None:
    ran_at = datetime.now(UTC).replace(microsecond=0)
    with engine.begin() as connection:
        response = report_nightly_reprocessing_run(
            NightlyReprocessingRunReport(ranAtUtc=ran_at, documentsQueued=7, error=None),
            service_principal=None,
            connection=connection,
        )
        assert response.nightlyReprocessingRunId is not None

        status = get_nightly_reprocessing_status(human_principal=None, connection=connection)

    matching = [run for run in status.recentRuns if run.ranAtUtc == ran_at]
    assert len(matching) == 1
    assert matching[0].documentsQueued == 7
    assert matching[0].error is None


def test_a_failed_run_is_reported_with_its_error_and_no_count(engine) -> None:
    ran_at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
    with engine.begin() as connection:
        report_nightly_reprocessing_run(
            NightlyReprocessingRunReport(
                ranAtUtc=ran_at, documentsQueued=None, error="database unavailable",
            ),
            service_principal=None,
            connection=connection,
        )
        status = get_nightly_reprocessing_status(human_principal=None, connection=connection)

    matching = [run for run in status.recentRuns if run.ranAtUtc == ran_at]
    assert len(matching) == 1
    assert matching[0].documentsQueued is None
    assert matching[0].error == "database unavailable"


def test_recent_runs_are_ordered_most_recent_first(engine) -> None:
    base = datetime.now(UTC).replace(microsecond=0)
    with engine.begin() as connection:
        for offset_minutes in (10, 5, 0):
            report_nightly_reprocessing_run(
                NightlyReprocessingRunReport(
                    ranAtUtc=base - timedelta(minutes=offset_minutes),
                    documentsQueued=offset_minutes,
                    error=None,
                ),
                service_principal=None,
                connection=connection,
            )
        status = get_nightly_reprocessing_status(human_principal=None, connection=connection)

    # Every prior test in this module also inserts rows around "now" -- just
    # confirm THESE three come back in the right relative order, not that
    # they're the only rows.
    ours = [
        run for run in status.recentRuns
        if run.ranAtUtc in (base, base - timedelta(minutes=5), base - timedelta(minutes=10))
    ]
    assert ours == sorted(ours, key=lambda run: run.ranAtUtc, reverse=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM auditcore.nightly_reprocessing_runs "
                "WHERE ran_at_utc = ANY(:times)"
            ),
            {"times": [base, base - timedelta(minutes=5), base - timedelta(minutes=10)]},
        )
