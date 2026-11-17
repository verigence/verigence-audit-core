"""BANK_REMATCH (2026-11-17): the dealerships' current bank statements reconciled again
after a reconciliation rule changes -- the check window became the statement's own
transaction dates and a financier's credit is tied to the Journey whose loan it is
(product owner 2026-11-17). Statements uploaded before keep the old verdicts until
reconciled again; this is the Rematch button run for every dealership with a current
statement.

Worker job ``BANK_REMATCH``: tag ``<tenant_id>:<label>``. A DRY RUN (each dealership
reconciled inside a transaction that is rolled back: what it would change, nothing
written) unless the label ends with "-commit". ``reports`` may list dealer ids to limit
the run. Safe to run again: a run decides every receipt and credit afresh.

    INSERT INTO auditcore.p2_worker_jobs (job_kind, tag, reports, requested_by)
    VALUES ('BANK_REMATCH', '<tenant_id>:bank-rematch-2026-11-17', '{}', '<who>');
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

JOB = "BANK_REMATCH"
_KEYS = ("receipts", "matched", "notFound", "outsideDates", "ambiguous", "cash", "credits", "creditsMatched",
         "creditsLoan", "creditsLoanAmount", "creditsUnidentified", "creditsUnidentifiedAmount",
         "journeysVerified", "journeysPartial", "journeysNotVerified")


def _parse(tag: str) -> tuple[str, str, bool]:
    tenant_id, sep, label = (tag or "").partition(":")
    if not sep or not tenant_id.strip() or not label.strip():
        raise ValueError(f"{JOB}: tag must be '<tenant_id>:<label>'")
    return tenant_id.strip(), label.strip(), label.strip().endswith("-commit")


def _dealers(connection: Connection, tenant_id: str, only: list[str]) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("""
            SELECT DISTINCT CAST(s.dealer_id AS text) AS dealer_id, d.dealer_name
            FROM auditcore.p2_bank_statements s
            JOIN auditcore.dealers d ON d.tenant_id = s.tenant_id AND d.dealer_id = s.dealer_id
            WHERE s.tenant_id = :t AND s.status = 'ACTIVE'
            ORDER BY d.dealer_name
        """),
        {"t": tenant_id},
    ).mappings().all()
    return [dict(r) for r in rows if not only or r["dealer_id"] in only]


def _statements(connection: Connection, tenant_id: str, dealer_id: str) -> dict[str, dict[str, Any]]:
    return {str(r["statement_id"]): {"bank": r["bank_name"], "account": r["account_number"],
                                     "counts": {k: (r["counts"] or {}).get(k) for k in _KEYS}}
            for r in connection.execute(
                text("SELECT statement_id, bank_name, account_number, counts FROM auditcore.p2_bank_statements "
                     "WHERE tenant_id = :t AND dealer_id = CAST(:d AS uuid) AND status = 'ACTIVE'"),
                {"t": tenant_id, "d": dealer_id}).mappings()}


def _loans(connection: Connection, tenant_id: str, dealer_id: str) -> list[dict[str, Any]]:
    return [{"journey": r["journey_reference"], "date": r["txn_date"].isoformat() if r["txn_date"] else None,
             "amount": str(r["amount"]), "basis": r["match_basis"], "receiptOnFile": r["outcome"] == "MATCHED"}
            for r in connection.execute(
                text("""
                    SELECT j.journey_reference, x.txn_date, x.amount, x.match_basis, x.outcome
                    FROM auditcore.p2_bank_statement_txns x
                    JOIN auditcore.p2_bank_statements s
                      ON s.tenant_id = x.tenant_id AND s.statement_id = x.statement_id AND s.status = 'ACTIVE'
                    JOIN auditcore.journeys j ON j.tenant_id = x.tenant_id AND j.journey_id = x.journey_id
                    WHERE x.tenant_id = :t AND s.dealer_id = CAST(:d AS uuid) AND x.match_basis LIKE 'LOAN%'
                    ORDER BY x.txn_date, j.journey_reference
                """),
                {"t": tenant_id, "d": dealer_id}).mappings()]


def run_bank_rematch(engine: Engine, tag: str, reports: list[str]) -> dict[str, Any]:
    from audit_core.uc03_p2_bank_statements import reconcile_dealer

    tenant_id, _label, commit = _parse(tag)
    only = [str(r).strip() for r in reports or [] if str(r).strip()]
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        dealers = _dealers(connection, tenant_id, only)
    out, failed = [], 0
    for dealer in dealers:
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                set_tenant_context(connection, tenant_id)
                before = _statements(connection, tenant_id, dealer["dealer_id"])
                reconcile_dealer(connection, tenant_id=tenant_id, dealer_id=dealer["dealer_id"])
                after = _statements(connection, tenant_id, dealer["dealer_id"])
                loans = _loans(connection, tenant_id, dealer["dealer_id"])
                if commit:
                    transaction.commit()
                else:
                    transaction.rollback()
            out.append({
                "dealer": dealer["dealer_name"], "loans": loans,
                "statements": [{"bank": a["bank"], "account": a["account"], "before": before.get(sid, {}).get("counts"),
                                "after": a["counts"]} for sid, a in after.items()],
            })
            logger.info("bank_rematch_dealer", tag=tag, commit=commit, dealer=dealer["dealer_name"], loans=len(loans))
        except Exception:
            failed += 1
            logger.warning("bank_rematch_failed", tag=tag, dealer_id=dealer["dealer_id"], exc_info=True)
    return {"commit": commit, "dealers": len(dealers), "failed": failed, "items": out}


__all__ = ["JOB", "run_bank_rematch"]
