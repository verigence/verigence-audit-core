"""uc03_nightly_reprocessing_reports.py — DI's Nightly Reprocessing status,
surfaced for the PMO/TL "Failed Extraction Reprocessing" tile.

DI's own processing_jobs/backout_jobs remain the source of truth for what
happened to any one document. This module is deliberately not that --
it's a thin log of "the nightly batch ran, here's roughly how much work it
queued", reported once per run by DI itself (scheduler/beat.py) via the
internal POST route below, and read back by the human-facing GET route
for display. Never in the extraction pipeline; a lost or delayed report
here has no effect on any document's own processing.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.uc03_pc_booking_documents import require_audit_service_principal

router = APIRouter(tags=["uc03-nightly-reprocessing-reports"])

_MAX_RECENT_RUNS = 30


class NightlyReprocessingRunReport(BaseModel):
    model_config = {"extra": "forbid"}

    ranAtUtc: datetime
    documentsQueued: int | None = None
    error: str | None = Field(default=None, max_length=2000)


class NightlyReprocessingRunReportResponse(BaseModel):
    nightlyReprocessingRunId: UUID


@router.post(
    "/v1/internal/di/nightly-reprocessing-runs",
    response_model=NightlyReprocessingRunReportResponse,
)
def report_nightly_reprocessing_run(
    payload: NightlyReprocessingRunReport,
    service_principal: Annotated[Any, Depends(require_audit_service_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> NightlyReprocessingRunReportResponse:
    del service_principal  # authentication only -- this route carries no tenant/journey scope
    run_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.nightly_reprocessing_runs
                (ran_at_utc, documents_queued, error)
            VALUES (:ran_at_utc, :documents_queued, :error)
            RETURNING nightly_reprocessing_run_id
            """
        ),
        {
            "ran_at_utc": payload.ranAtUtc,
            "documents_queued": payload.documentsQueued,
            "error": payload.error,
        },
    ).scalar_one()
    return NightlyReprocessingRunReportResponse(nightlyReprocessingRunId=run_id)


class NightlyReprocessingRunSummary(BaseModel):
    ranAtUtc: datetime
    documentsQueued: int | None
    error: str | None


class NightlyReprocessingStatusResponse(BaseModel):
    recentRuns: list[NightlyReprocessingRunSummary] = Field(default_factory=list)


@router.get(
    "/v1/admin/nightly-reprocessing-runs",
    response_model=NightlyReprocessingStatusResponse,
)
def get_nightly_reprocessing_status(
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> NightlyReprocessingStatusResponse:
    del human_principal  # any authenticated human -- the PMO/TL tile gates visibility by nav placement
    rows = connection.execute(
        text(
            """
            SELECT ran_at_utc, documents_queued, error
            FROM auditcore.nightly_reprocessing_runs
            ORDER BY ran_at_utc DESC
            LIMIT :limit
            """
        ),
        {"limit": _MAX_RECENT_RUNS},
    ).mappings().all()
    return NightlyReprocessingStatusResponse(
        recentRuns=[
            NightlyReprocessingRunSummary(
                ranAtUtc=row["ran_at_utc"],
                documentsQueued=row["documents_queued"],
                error=row["error"],
            )
            for row in rows
        ]
    )


__all__ = ["router"]
