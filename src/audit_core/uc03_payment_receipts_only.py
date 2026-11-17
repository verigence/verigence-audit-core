"""PAYMENT_RECEIPTS_ONLY (2026-11-17): the bookings on file put right after the
payment receipts became the payment receipts only.

Product owner 2026-11-17: "payment receipts should contain only the payment
receipts.. it shouldn't have any data coming from NDC" -- and, the rule having
been built otherwise before, "we need to create a job which does this check,
updates journey and tasks accordingly".

From 2 to 16 Nov 2026 a line of the NDC or a posting of the customer ledger
with no receipt on file was counted as a payment and listed among the payment
receipts (uc03_ledger_receipts). The code no longer does that: the Payments
tab, the Deal and the Audit Summary read the receipts on file the moment it is
deployed. What stays behind from before, and this job puts right, per booking:

- its findings and tasks were decided on the old money. The booking's checks
  are run again, forced (as RULES_RERUN does): the receipts-equal-the-NDC and
  delivered-on-short-payment checks are judged on the receipts on file, and
  "Payment receipt missing" is decided again -- so a task asking for a receipt
  that is on file under the same running number (the NDC's handwriting misread)
  closes, and one still missing stays, refreshed;
- the dealership's bank reconciliation carried those lines as receipts to
  verify. Each dealership with a current bank statement is reconciled again,
  so they leave it and each Journey's bank status is worked out on its
  receipts.

Which bookings: every one with an NDC / customer ledger on file whose NDC or
ledger shows money with no receipt on file (the money that was counted), and
every one with a "Payment receipt missing" task still open. ``reports`` may
list journey references to limit the run.

Worker job ``PAYMENT_RECEIPTS_ONLY``: tag ``<tenant_id>:<label>``. A DRY RUN
(what it would do, nothing written) unless the label ends with "-commit". Safe
to run again: the checks decide, the usual way, and a second run finds the
same bookings and asks for the same.

    INSERT INTO auditcore.p2_worker_jobs (job_kind, tag, reports, requested_by)
    VALUES ('PAYMENT_RECEIPTS_ONLY', '<tenant_id>:receipts-only-2026-11-17-commit', '{}', '<who>');
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

JOB = "PAYMENT_RECEIPTS_ONLY"
CHECK = "PAYMENT_RECEIPT_MISSING"
# The bookings listed one by one in the job's result (every one is logged).
_LISTED = 300


def _parse(tag: str) -> tuple[str, str, bool]:
    tenant_id, sep, label = (tag or "").partition(":")
    if not sep or not tenant_id.strip() or not label.strip():
        raise ValueError(f"{JOB}: tag must be '<tenant_id>:<label>'")
    return tenant_id.strip(), label.strip(), label.strip().endswith("-commit")


def candidates(connection: Connection, tenant_id: str, references: list[str]) -> list[dict[str, Any]]:
    """The bookings to look at: an NDC / customer ledger on file, or a "Payment
    receipt missing" task still open."""
    from audit_core.uc03_deal_source_policy import ACCOUNT_STATEMENT_TYPE
    from audit_core.uc03_p2_task_producer import _OPEN

    return [dict(r) for r in connection.execute(
        text(
            """
            SELECT j.journey_id, j.journey_reference,
                   EXISTS (SELECT 1 FROM auditcore.p2_tasks t
                            WHERE t.tenant_id = j.tenant_id AND t.journey_id = j.journey_id
                              AND t.source_code = :check AND t.task_status = ANY(:open)) AS open_task
            FROM auditcore.journeys j
            WHERE j.tenant_id = :t
              AND (EXISTS (SELECT 1 FROM auditcore.evidence e
                            WHERE e.tenant_id = j.tenant_id AND e.journey_id = j.journey_id
                              AND e.association_status = 'ACTIVE' AND e.document_type_key = :type)
                   OR EXISTS (SELECT 1 FROM auditcore.p2_tasks t
                               WHERE t.tenant_id = j.tenant_id AND t.journey_id = j.journey_id
                                 AND t.source_code = :check AND t.task_status = ANY(:open)))
            """
            + ("AND j.journey_reference = ANY(:r) " if references else "")
            + "ORDER BY j.journey_reference"
        ),
        {"t": tenant_id, "check": CHECK, "open": list(_OPEN), "type": ACCOUNT_STATEMENT_TYPE, "r": references},
    ).mappings().all()]


def look(connection: Connection, tenant_id: str, journey: dict[str, Any]) -> dict[str, Any] | None:
    """What one booking carried: the NDC / ledger lines with no receipt on file
    (the money that was counted as paid and is not now) and whether its
    missing-receipt task is open. None when it carried neither."""
    from audit_core.uc03_ledger_receipts import missing_receipts

    lines = missing_receipts(connection, tenant_id=tenant_id, journey_id=UUID(str(journey["journey_id"])))
    if not lines and not journey["open_task"]:
        return None
    return {"reference": journey["journey_reference"], "journeyId": str(journey["journey_id"]),
            "lines": len(lines), "amount": sum((Decimal(p["amount"]) for p in lines), Decimal(0)),
            "openTask": bool(journey["open_task"]),
            # its task asks for receipts that are all on file: it closes when the checks run
            "taskSettles": bool(journey["open_task"]) and not lines}


def _bank_dealers(connection: Connection, tenant_id: str) -> list[str]:
    return [str(d) for d in connection.execute(
        text("SELECT DISTINCT dealer_id FROM auditcore.p2_bank_statements WHERE tenant_id = :t AND status = 'ACTIVE'"),
        {"t": tenant_id},
    ).scalars().all()]


def _bank_lines(connection: Connection, tenant_id: str) -> int:
    """NDC / ledger lines the bank reconciliation still carries as receipts."""
    return int(connection.execute(
        text("SELECT count(*) FROM auditcore.p2_bank_receipt_matches "
             "WHERE tenant_id = :t AND is_current AND source = 'LEDGER'"),
        {"t": tenant_id},
    ).scalar_one())


def run_payment_receipts_only(engine: Engine, tag: str, reports: list[str]) -> dict[str, Any]:
    from audit_core.uc03_audit_task_cleanup import _rerun_native
    from audit_core.uc03_p2_bank_statements import reconcile_dealer

    tenant_id, label, commit = _parse(tag)
    references = [str(r).strip() for r in reports or [] if str(r).strip()]
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        on_file = candidates(connection, tenant_id, references)
        bank_lines = _bank_lines(connection, tenant_id)
        dealers = [] if references else _bank_dealers(connection, tenant_id)
    found: list[dict[str, Any]] = []
    queued = units = failed = 0
    for journey in on_file:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                item = look(connection, tenant_id, journey)
                if item is None:
                    continue
                if commit:
                    units += len(_rerun_native(connection, tenant_id, UUID(item["journeyId"]), label))
                    queued += 1
            found.append(item)
            logger.info("payment_receipts_only_item", tag=tag, commit=commit, reference=item["reference"],
                        lines=item["lines"], amount=str(item["amount"]), open_task=item["openTask"],
                        task_settles=item["taskSettles"])
        except Exception:
            failed += 1
            logger.warning("payment_receipts_only_failed", tag=tag, reference=journey["journey_reference"],
                           exc_info=True)
    reconciled = 0
    if commit:
        for dealer_id in dealers:
            try:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    reconcile_dealer(connection, tenant_id=tenant_id, dealer_id=dealer_id)
                reconciled += 1
            except Exception:
                failed += 1
                logger.warning("payment_receipts_only_bank_failed", tag=tag, dealer_id=dealer_id, exc_info=True)
    seen = {str(j["journey_reference"]) for j in on_file}
    result = {
        "commit": commit, "looked": len(on_file), "bookings": len(found),
        # the NDC / ledger money that was counted as paid and is no longer
        "withLines": sum(1 for f in found if f["lines"]), "lines": sum(f["lines"] for f in found),
        "amount": str(sum((f["amount"] for f in found), Decimal(0))),
        "openTasks": sum(1 for f in found if f["openTask"]),
        "tasksSettling": sum(1 for f in found if f["taskSettles"]),
        "queued": queued, "units": units,
        "bankLines": bank_lines, "bankDealers": len(dealers), "bankReconciled": reconciled, "failed": failed,
        "notFound": [r for r in references if r not in seen],
        "items": [{**f, "amount": str(f["amount"])} for f in found[:_LISTED]],
    }
    logger.info("payment_receipts_only_finished", tag=tag,
                **{k: v for k, v in result.items() if k not in ("items", "notFound")})
    return result


__all__ = ["JOB", "candidates", "look", "run_payment_receipts_only"]
