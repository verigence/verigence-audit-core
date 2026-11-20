"""DMS_REPORT_REBUILD (2026-11-20): DMS reports already generated, counted again by the current
rules and corrected in place. Product owner, 20 Nov 2026: "correct the already generated
report", after the DMS's date was made the source of truth (19 Nov: "you have to stick to the
DMS date ... DMS will always be source of truth.. no matter").

Each stored report keeps its id, its date, who generated it and when; its figures, Statement,
lists and sheets are made again from the same day's dumps for the same dealerships (those the
person who generated it covers). The dumps are not matched again: their rows stay as they are.
What Verigence holds is read as it is now, as for any report. The report records that it was
corrected and when (``rebuilt``).

Worker job ``DMS_REPORT_REBUILD``: tag ``<tenant_id>:<label>``; ``reports`` may list report ids
to limit it. A DRY RUN (the figures before and after, nothing written) unless the label ends
with "-commit". Safe to run again.

    INSERT INTO auditcore.p2_worker_jobs (job_kind, tag, reports, requested_by)
    VALUES ('DMS_REPORT_REBUILD', '<tenant_id>:dms-report-rebuild-2026-11-20', '{}', '<who>');
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

JOB = "DMS_REPORT_REBUILD"
WHY = "Counted again with the DMS's date as the source of truth (product owner, 19-20 Nov 2026)."
_SHOWN = ("bookingDms", "bookingSynced", "bookingNotInVerigence", "bookingInVerigence", "bookingNotInDms",
          "deliveryDump", "deliverySynced", "deliveryNotUploaded", "deliveryNotInVerigence", "deliveryInVerigence",
          "deliveryNotInDms")


def _parse(tag: str) -> tuple[str, bool]:
    tenant_id, sep, label = (tag or "").partition(":")
    if not sep or not tenant_id.strip() or not label.strip():
        raise ValueError(f"{JOB}: tag must be '<tenant_id>:<label>'")
    return tenant_id.strip(), label.strip().endswith("-commit")


def run_dms_report_rebuild(engine: Engine, tag: str, reports: list[str]) -> dict[str, Any]:
    from audit_core.uc03_p2_dms_dumps import _dealers
    from audit_core.uc03_p2_dms_reports import _day_dumps, compute_report

    tenant_id, commit = _parse(tag)
    only = {str(r).strip() for r in reports or [] if str(r).strip()}
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        stored = [dict(r) for r in connection.execute(
            text("SELECT report_id, report_date, dealer_ids, generated_by, figures, report "
                 "FROM auditcore.p2_dms_reports WHERE tenant_id = :t ORDER BY report_date, generated_at_utc"),
            {"t": tenant_id},
        ).mappings() if not only or str(r["report_id"]) in only]
    out, failed = [], 0
    for row in stored:
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                set_tenant_context(connection, tenant_id)
                by = str(row["generated_by"] or "")
                dealers = _dealers(connection, tenant_id, by, False) or _dealers(connection, tenant_id, by, True)
                dumps = _day_dumps(connection, tenant_id, [d["dealerId"] for d in dealers], row["report_date"])
                if not dumps:
                    raise ValueError(f"no current dump for {row['report_date']}")
                report = compute_report(connection, tenant_id=tenant_id, dealers=dealers, day=row["report_date"],
                                        dumps=dumps)
                was = row["figures"] or {}
                report["rebuilt"] = {"atUtc": datetime.now(UTC).isoformat(), "why": WHY,
                                     "figuresBefore": {k: was.get(k) for k in _SHOWN if k in was}}
                connection.execute(
                    text("UPDATE auditcore.p2_dms_reports SET figures = CAST(:f AS jsonb), report = CAST(:r AS jsonb) "
                         "WHERE tenant_id = :t AND report_id = :id"),
                    {"t": tenant_id, "id": str(row["report_id"]), "f": json.dumps(report["figures"]),
                     "r": json.dumps(report, default=str)},
                )
                if commit:
                    transaction.commit()
                else:
                    transaction.rollback()
            now = report["figures"]
            out.append({"reportId": str(row["report_id"]), "date": row["report_date"].isoformat(),
                        "before": {k: was.get(k) for k in _SHOWN},
                        "after": {k: now.get(k) for k in _SHOWN},
                        "dealers": [{"dealer": d["dealerName"],
                                     **{k: d["figures"].get(k) for k in ("bookingDms", "bookingSynced",
                                                                         "bookingInVerigence", "deliveryDump",
                                                                         "deliverySynced", "deliveryInVerigence")}}
                                    for d in report["dealers"]]})
        except Exception:
            failed += 1
            logger.warning("dms_report_rebuild_failed", tag=tag, report_id=str(row["report_id"]), exc_info=True)
    logger.info("dms_report_rebuild_done", tag=tag, commit=commit, reports=len(stored), failed=failed)
    return {"commit": commit, "reports": len(stored), "failed": failed, "items": out}


__all__ = ["JOB", "run_dms_report_rebuild"]
