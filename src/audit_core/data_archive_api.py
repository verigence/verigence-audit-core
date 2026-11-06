"""SuperAdmin read of the data-archive job's run log (auditcore.archive_runs).

GET /v1/admin/archive-runs lists the run rows newest first: one row per table,
Project and run (``batchNo`` null), and with ``includeBatches=true`` also the
row each archived batch wrote with its delete. Read-only; the job itself is
started from GitHub Actions (audit_core.data_archive).
"""
from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection, text

from audit_core.db import set_platform_super_admin_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_super_admin_request,
)

router = APIRouter(tags=["data-archive"])


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


@router.get("/v1/admin/archive-runs")
def list_archive_runs(
    _admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    table: Annotated[str | None, Query(max_length=160)] = None,
    run_id: Annotated[UUID | None, Query(alias="runId")] = None,
    include_batches: Annotated[bool, Query(alias="includeBatches")] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    set_platform_super_admin_context(connection)
    rows = connection.execute(
        text(
            """
            SELECT entry_id, run_id, tenant_id, table_key, mode, batch_no, status, rule_description,
                   cutoff_utc, candidate_count, archived_count, object_keys, checksums, error,
                   started_by, started_at_utc, finished_at_utc
            FROM auditcore.archive_runs
            WHERE (CAST(:table AS varchar) IS NULL OR table_key = CAST(:table AS varchar))
              AND (CAST(:run_id AS uuid) IS NULL OR run_id = CAST(:run_id AS uuid))
              AND (CAST(:batches AS boolean) OR batch_no IS NULL)
            ORDER BY started_at_utc DESC, run_id, tenant_id NULLS FIRST, batch_no NULLS FIRST
            LIMIT :limit
            """
        ),
        {"table": table, "run_id": run_id, "batches": include_batches, "limit": limit},
    ).mappings().all()
    return {
        "items": [
            {
                "entryId": str(row["entry_id"]),
                "runId": str(row["run_id"]),
                "tenantId": row["tenant_id"],
                "table": row["table_key"],
                "mode": row["mode"],
                "batchNo": row["batch_no"],
                "status": row["status"],
                "rule": row["rule_description"],
                "cutoffUtc": _iso(row["cutoff_utc"]),
                "candidateCount": int(row["candidate_count"]),
                "archivedCount": int(row["archived_count"]),
                "objectKeys": row["object_keys"],
                "checksums": row["checksums"],
                "error": row["error"],
                "startedBy": row["started_by"],
                "startedAtUtc": _iso(row["started_at_utc"]),
                "finishedAtUtc": _iso(row["finished_at_utc"]),
            }
            for row in rows
        ]
    }
