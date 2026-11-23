"""DMS_REMATCH (2026-11-18): the current DMS dumps matched again after the matching changed
-- models compared spacing aside ("XEV9S" / "XEV 9S"), which left bookings on file reported
"not in Verigence". It is the Rematch button run for every current dump.

Worker job ``DMS_REMATCH``: tag ``<tenant_id>:<label>``; ``reports`` may list dump ids to
limit it. A DRY RUN (each dump matched inside a transaction that is rolled back: the counts
before and after and the rows newly found, nothing written) unless the label ends with
"-commit". Safe to run again.

    INSERT INTO auditcore.p2_worker_jobs (job_kind, tag, reports, requested_by)
    VALUES ('DMS_REMATCH', '<tenant_id>:dms-rematch-2026-11-18', '{}', '<who>');
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

JOB = "DMS_REMATCH"


def _parse(tag: str) -> tuple[str, bool]:
    tenant_id, sep, label = (tag or "").partition(":")
    if not sep or not tenant_id.strip() or not label.strip():
        raise ValueError(f"{JOB}: tag must be '<tenant_id>:<label>'")
    return tenant_id.strip(), label.strip().endswith("-commit")


def run_dms_rematch(engine: Engine, tag: str, reports: list[str]) -> dict[str, Any]:
    tenant_id, commit = _parse(tag)
    only = {str(r).strip() for r in reports or [] if str(r).strip()}
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        dumps = [dict(r) for r in connection.execute(
            text("""
                SELECT u.dump_id, u.dump_kind, u.business_date, d.dealer_name, u.matched_count, u.not_in_tool_count,
                       u.missing_count, u.cancelled_count
                FROM auditcore.p2_dms_dumps u
                JOIN auditcore.dealers d ON d.tenant_id = u.tenant_id AND d.dealer_id = u.dealer_id
                WHERE u.tenant_id = :t AND u.status = 'ACTIVE'
                ORDER BY u.business_date, d.dealer_name, u.dump_kind
            """),
            {"t": tenant_id},
        ).mappings() if not only or str(r["dump_id"]) in only]
    out, failed, errors = [], 0, []
    # A dump that fails is tried once more after the others, on a fresh connection: it can run
    # into a Journey whose checks another worker is settling at that moment.
    retry: list[dict[str, Any]] = []
    for attempt, todo in ((1, dumps), (2, retry)):
        for dump in todo:
            result = _rematch_one(engine, tenant_id, commit, dump)
            if isinstance(result, dict):
                out.append(result)
            elif attempt == 1:
                retry.append(dump)
            else:
                failed += 1
                errors.append({"dumpId": str(dump["dump_id"]), "dealer": dump["dealer_name"],
                               "kind": dump["dump_kind"], "day": dump["business_date"].isoformat(),
                               "error": result})
    logger.info("dms_rematch_done", tag=tag, commit=commit, dumps=len(dumps), failed=failed)
    return {"commit": commit, "dumps": len(dumps), "failed": failed, "retried": len(retry),
            "errors": errors, "items": out}


def _rematch_one(engine: Engine, tenant_id: str, commit: bool, dump: dict[str, Any]) -> dict[str, Any] | str:
    """One dump matched again, or the error (kept in the job's result: the worker's log lines
    can be dropped when it logs fast)."""
    from audit_core.uc03_p2_dms_dumps import match_dump

    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            set_tenant_context(connection, tenant_id)
            was = {r["row_no"]: r["outcome"] for r in connection.execute(
                text("SELECT row_no, outcome FROM auditcore.p2_dms_dump_rows WHERE tenant_id = :t AND dump_id = :id"),
                {"t": tenant_id, "id": str(dump["dump_id"])}).mappings()}
            counts = match_dump(connection, tenant_id=tenant_id, dump_id=dump["dump_id"])
            found = [f"{r['customer_name']} ({r['match_basis']})" for r in connection.execute(
                text("SELECT row_no, customer_name, match_basis, outcome FROM auditcore.p2_dms_dump_rows "
                     "WHERE tenant_id = :t AND dump_id = :id ORDER BY row_no"),
                {"t": tenant_id, "id": str(dump["dump_id"])}).mappings()
                if was.get(r["row_no"]) == "MISSING" and r["outcome"] != "MISSING"]
            if commit:
                transaction.commit()
            else:
                transaction.rollback()
        return {"dealer": dump["dealer_name"], "kind": dump["dump_kind"],
                "day": dump["business_date"].isoformat(),
                "before": {"matched": dump["matched_count"], "notInTool": dump["not_in_tool_count"],
                           "missing": dump["missing_count"], "cancelled": dump["cancelled_count"]},
                "after": counts, "newlyFound": len(found), "found": found[:40]}
    except Exception as error:
        logger.warning("dms_rematch_failed", dump_id=str(dump["dump_id"]), exc_info=True)
        return f"{type(error).__name__}: {str(error)[:400]}"


__all__ = ["JOB", "run_dms_rematch"]
