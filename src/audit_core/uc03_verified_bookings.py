"""Verified bookings (product owner, 2026-11-17): bookings a Team Lead or Reviewer has
checked against their papers, and the system's own check that it still shows them the
same.

Why: "why so many mistakes ... can we do introspection". A change made for one reported
booking kept moving the figures of others, and nothing showed it before the PCs did.
The product owner chose: Team Leads and Reviewers mark bookings verified; a change that
moves a verified booking's figures goes live and is reported (it does not block).

What is kept, as verified (booking_figures): the Deal (net payable, gross, discounts,
each category's current total, each discount), the money received as the Deal counts it
(receipts, loan, old vehicle, refunds), the NDC against the payment receipts
(uc03_ledger_receipts.receipts_against_ndc: the three sums), the Audit Summary's
settlement and verdict, and the open tasks by check. Built by the same functions the
Journey 360 screens are served by, in a READ ONLY transaction.

``papers`` is a fingerprint of what the figures rest on: the documents on file and every
value read or corrected on them, the finance records. A booking whose figures changed
and whose papers did not was moved by the system (a code change, a job): CHANGED. Its
papers changed too: PAPERS_CHANGED -- the person verifies it again.

Compared (check_verified): every night at the Jobs page time (VERIFIED_CHECK, 9:40 pm),
once after each deploy of the worker (its code version), and on demand from the
Verified bookings page. Each comparison is one row of p2_verified_booking_checks.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-verified-bookings"])

# Who marks a booking verified (product owner 2026-11-17: "TL and Reviewer").
MARK_ROLES = ("TL", "REVIEWER")
# Who sees the list.
READ_ROLES = ("TL", "REVIEWER", "PM", "EXECUTIVE")
TRIGGERS = ("DAILY", "DEPLOY", "ON_DEMAND", "MARKED")
DEPLOY_RUN = "P2_VERIFIED_DEPLOY"
DAILY_RUN = "P2_VERIFIED_CHECK"
# A figure that moves by less than a rupee has not moved.
_SAME = Decimal("0.5")
_MAX_CHANGES = 40


def code_version() -> str | None:
    return (os.environ.get("VERIGENCE_GIT_SHA") or os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "")[:12] or None


# ── the figures ───────────────────────────────────────────────────────────────
def _money(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
    except ArithmeticError:
        return str(value)


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def booking_figures(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """The figures a verified booking is kept with, from the functions the screens use."""
    from audit_core.uc03_ledger_receipts import receipts_against_ndc
    from audit_core.uc03_p2_journey360 import BUILDERS

    deal = BUILDERS["deal"](connection, tenant_id=tenant_id, journey_id=journey_id)
    summary = deal.get("summary") or {}
    paid = summary.get("paid") or {}
    audit = BUILDERS["audit-summary"](connection, tenant_id=tenant_id, journey_id=journey_id)
    settlement = audit.get("settlement") or {}
    verdict = audit.get("verdict") or {}
    ndc = receipts_against_ndc(connection, tenant_id=tenant_id, journey_id=journey_id)
    tasks = connection.execute(
        text(
            """
            SELECT coalesce(nullif(source_code, ''), task_type) AS code, count(*) AS n
            FROM auditcore.p2_tasks
            WHERE tenant_id = :t AND journey_id = :j
              AND task_status NOT IN ('VERIFIED_COMPLETE', 'CANCELLED')
            GROUP BY 1
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    return _plain({
        "deal": {
            "payable": _money(summary.get("payable")),
            "grossCurrent": _money((summary.get("gross") or {}).get("current")),
            "discountsCurrent": _money((summary.get("discounts") or {}).get("current")),
            "netCurrent": _money((summary.get("net") or {}).get("current")),
            "balanceDue": _money(summary.get("balanceDue")),
            "categories": {str(c.get("code")): _money((c.get("totals") or {}).get("current"))
                           for c in deal.get("categories") or []},
            "discounts": {str(d.get("key")): _money(d.get("current") if d.get("current") is not None
                                                    else d.get("billed"))
                          for d in deal.get("discounts") or []},
        },
        "money": {
            "receipts": _money(paid.get("receipts")), "loan": _money(paid.get("loan")),
            "loanSource": paid.get("loanSource"), "oldVehicle": _money(paid.get("oldVehicle")),
            "refunds": _money(paid.get("refunds")), "total": _money(paid.get("total")),
        },
        "ndcPayments": {
            "status": ndc.get("status"), "netValue": _money(ndc.get("net")),
            "ndcReceived": _money(ndc.get("money")), "receiptsOnFile": _money(ndc.get("onFile")),
            "receiptsShort": _money(ndc.get("short")), "loanWithoutReceipt": _money(ndc.get("loanWithoutReceipt")),
        },
        "auditSummary": {
            "settlement": settlement.get("status"), "balance": _money(settlement.get("balance")),
            "verdict": verdict.get("status"),
            "customerOvercharged": _money((verdict.get("amountAtRisk") or {}).get("customerOvercharged")),
            "companyLeakage": _money((verdict.get("amountAtRisk") or {}).get("companyLeakage")),
        },
        "openTasks": {str(r["code"]): int(r["n"]) for r in sorted(tasks, key=lambda r: str(r["code"]))},
    })


def papers(connection: Connection, *, tenant_id: str, journey_id: UUID) -> str:
    """A fingerprint of the papers the figures rest on: each document on file with every
    value read or corrected on it, and the finance records."""
    row = connection.execute(
        text(
            """
            SELECT
              (SELECT string_agg(e.di_document_id::text || ':' || e.document_type_key || ':' || coalesce(f.h, ''),
                                 ',' ORDER BY e.di_document_id)
                 FROM auditcore.evidence e
                 LEFT JOIN (
                   SELECT di_document_id,
                          md5(string_agg(field_key || '=' || coalesce(effective_value::text, ''), '|'
                                         ORDER BY field_key, source_fact_version)) AS h
                   FROM auditcore.journey_document_extracted_fields
                   WHERE tenant_id = :t AND journey_id = :j
                   GROUP BY di_document_id) f ON f.di_document_id = e.di_document_id
                WHERE e.tenant_id = :t AND e.journey_id = :j AND e.association_status = 'ACTIVE') AS documents,
              (SELECT string_agg(coalesce(loan_disbursement_amount::text, '') || '/'
                                 || coalesce(loan_disbursement_payment_id::text, ''), ',' ORDER BY finance_record_id)
                 FROM auditcore.finance_records WHERE tenant_id = :t AND journey_id = :j) AS finance
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    return hashlib.sha256(f"{row['documents'] or ''}#{row['finance'] or ''}".encode()).hexdigest()


def _read_only(engine: Engine, tenant_id: str, journey_id: UUID) -> tuple[dict[str, Any], str]:
    with engine.begin() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        set_tenant_context(connection, tenant_id)
        return (booking_figures(connection, tenant_id=tenant_id, journey_id=journey_id),
                papers(connection, tenant_id=tenant_id, journey_id=journey_id))


def compare(verified: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """The figures that moved: path, as verified, now. Amounts within a rupee are the same."""
    from audit_core.uc03_journey_snapshot import diff_paths

    out = []
    for path, before, after in diff_paths(verified, current):
        try:
            if before is not None and after is not None and \
                    abs(Decimal(str(before)) - Decimal(str(after))) < _SAME:
                continue
        except ArithmeticError:
            pass
        out.append({"field": path, "verified": before, "now": after})
    return out


# ── marking, withdrawing, comparing ───────────────────────────────────────────
def mark_verified(connection: Connection, *, tenant_id: str, journey_id: UUID, actor_id: str,
                  actor_name: str | None, role: str | None, comment: str | None) -> dict[str, Any]:
    """Keep the booking's figures as verified, with the fingerprint of its papers."""
    figures = booking_figures(connection, tenant_id=tenant_id, journey_id=journey_id)
    fingerprint = papers(connection, tenant_id=tenant_id, journey_id=journey_id)
    reference = connection.execute(
        text("SELECT journey_reference FROM auditcore.journeys WHERE tenant_id = :t AND journey_id = :j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_verified_bookings
              (tenant_id, journey_id, journey_reference, status, verified_by, verified_by_name, verified_role,
               verified_at_utc, comment, figures, papers, code_version,
               withdrawn_by, withdrawn_at_utc, withdrawn_reason)
            VALUES (:t, :j, :ref, 'ACTIVE', :by, :name, :role, now(), :comment, CAST(:figures AS jsonb), :papers,
                    :version, NULL, NULL, NULL)
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
              journey_reference = EXCLUDED.journey_reference, status = 'ACTIVE',
              verified_by = EXCLUDED.verified_by, verified_by_name = EXCLUDED.verified_by_name,
              verified_role = EXCLUDED.verified_role, verified_at_utc = now(), comment = EXCLUDED.comment,
              figures = EXCLUDED.figures, papers = EXCLUDED.papers, code_version = EXCLUDED.code_version,
              withdrawn_by = NULL, withdrawn_at_utc = NULL, withdrawn_reason = NULL
            """
        ),
        {"t": tenant_id, "j": journey_id, "ref": reference, "by": actor_id, "name": actor_name, "role": role,
         "comment": (comment or "").strip() or None, "figures": json.dumps(figures), "papers": fingerprint,
         "version": code_version()},
    )
    _record(connection, tenant_id, journey_id, "MARKED", "MATCHES", False, [], None)
    logger.info("verified_booking_marked", tenant_id=tenant_id, reference=reference, by=actor_id)
    return {"journeyId": str(journey_id), "reference": reference, "figures": figures}


def withdraw(connection: Connection, *, tenant_id: str, journey_id: UUID, actor_id: str, reason: str | None) -> bool:
    return bool(connection.execute(
        text(
            """
            UPDATE auditcore.p2_verified_bookings
            SET status = 'WITHDRAWN', withdrawn_by = :by, withdrawn_at_utc = now(), withdrawn_reason = :reason
            WHERE tenant_id = :t AND journey_id = :j AND status = 'ACTIVE'
            """
        ),
        {"t": tenant_id, "j": journey_id, "by": actor_id, "reason": (reason or "").strip() or None},
    ).rowcount)


def _record(connection: Connection, tenant_id: str, journey_id: UUID, trigger: str, result: str,
            papers_changed: bool, changes: list[dict[str, Any]], error: str | None) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_verified_booking_checks
              (tenant_id, journey_id, trigger, code_version, result, papers_changed, changes, error)
            VALUES (:t, :j, :trigger, :version, :result, :papers, CAST(:changes AS jsonb), :error)
            """
        ),
        {"t": tenant_id, "j": journey_id, "trigger": trigger, "version": code_version(), "result": result,
         "papers": papers_changed, "changes": json.dumps(_plain(changes[:_MAX_CHANGES])), "error": error},
    )


def check_verified(engine: Engine, tenant_id: str, trigger: str) -> dict[str, Any]:
    """Compare every verified booking of the Project with what the system shows now; one
    row each in p2_verified_booking_checks. Read only, apart from that row."""
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        bookings = [dict(r) for r in connection.execute(
            text("SELECT journey_id, journey_reference, figures, papers FROM auditcore.p2_verified_bookings "
                 "WHERE tenant_id = :t AND status = 'ACTIVE' ORDER BY journey_reference"),
            {"t": tenant_id},
        ).mappings().all()]
    counts = {"MATCHES": 0, "CHANGED": 0, "PAPERS_CHANGED": 0, "ERROR": 0}
    moved: list[dict[str, Any]] = []
    for booking in bookings:
        journey_id = UUID(str(booking["journey_id"]))
        try:
            figures, fingerprint = _read_only(engine, tenant_id, journey_id)
            changes = compare(booking["figures"], figures)
            papers_changed = fingerprint != booking["papers"]
            result = "MATCHES" if not changes else "PAPERS_CHANGED" if papers_changed else "CHANGED"
            error = None
        except Exception as exc:
            changes, papers_changed, result, error = [], False, "ERROR", f"{type(exc).__name__}: {exc}"[:1000]
            logger.warning("verified_booking_check_failed", tenant_id=tenant_id,
                           reference=booking["journey_reference"], exc_info=True)
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            _record(connection, tenant_id, journey_id, trigger, result, papers_changed, changes, error)
        counts[result] += 1
        if result != "MATCHES":
            moved.append({"booking": booking["journey_reference"], "result": result, "changes": changes[:5]})
    summary = {"trigger": trigger, "codeVersion": code_version(), "bookings": len(bookings), **counts,
               "moved": moved[:30]}
    logger.info("verified_bookings_checked", tenant_id=tenant_id,
                **{k: v for k, v in summary.items() if k != "moved"})
    return summary


def run_verified_check(engine: Engine, tag: str, reports: list[str]) -> dict[str, Any]:
    """Worker job VERIFIED_BOOKINGS_CHECK (tag ``<tenant_id>:<label>``): compare now."""
    tenant_id = (tag or "").partition(":")[0].strip()
    if not tenant_id:
        raise ValueError("VERIFIED_BOOKINGS_CHECK needs the tag '<tenant_id>:<label>'")
    return check_verified(engine, tenant_id, "ON_DEMAND")


def listing(connection: Connection, *, tenant_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT v.journey_id, v.journey_reference, v.verified_by_name, v.verified_by, v.verified_role,
                   v.verified_at_utc, v.comment, v.figures, v.code_version AS verified_code,
                   d.dealer_name, o.outlet_name, auditcore.shown_name(NULL, c.display_name) AS customer_name,
                   k.checked_at_utc, k.trigger, k.result, k.papers_changed, k.changes, k.error,
                   k.code_version AS checked_code
            FROM auditcore.p2_verified_bookings v
            JOIN auditcore.journeys j ON j.tenant_id = v.tenant_id AND j.journey_id = v.journey_id
            JOIN auditcore.dealers d ON d.tenant_id = j.tenant_id AND d.dealer_id = j.dealer_id
            JOIN auditcore.dealer_outlets o ON o.tenant_id = j.tenant_id AND o.outlet_id = j.outlet_id
            JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
            LEFT JOIN LATERAL (
              SELECT * FROM auditcore.p2_verified_booking_checks k
              WHERE k.tenant_id = v.tenant_id AND k.journey_id = v.journey_id
              ORDER BY k.checked_at_utc DESC LIMIT 1) k ON true
            WHERE v.tenant_id = :t AND v.status = 'ACTIVE'
            ORDER BY (k.result IS DISTINCT FROM 'MATCHES') DESC, v.journey_reference
            """
        ),
        {"t": tenant_id},
    ).mappings().all()
    return [{
        "journeyId": str(r["journey_id"]), "reference": r["journey_reference"], "customer": r["customer_name"],
        "dealer": r["dealer_name"], "outlet": r["outlet_name"],
        "verifiedBy": r["verified_by_name"] or r["verified_by"], "verifiedRole": r["verified_role"],
        "verifiedAt": r["verified_at_utc"].isoformat() if r["verified_at_utc"] else None, "comment": r["comment"],
        "payable": ((r["figures"] or {}).get("deal") or {}).get("payable"),
        "received": ((r["figures"] or {}).get("money") or {}).get("total"),
        "lastCheck": None if r["result"] is None else {
            "at": r["checked_at_utc"].isoformat(), "trigger": r["trigger"], "result": r["result"],
            "papersChanged": r["papers_changed"], "changes": r["changes"] or [], "error": r["error"],
            "codeVersion": r["checked_code"]},
    } for r in rows]


# ── the API ───────────────────────────────────────────────────────────────────
def _who(connection: Connection, tenant_id: str, principal: HumanPrincipal, client: SecurityAuthorizationClient,
         roles: tuple[str, ...], journey_id: UUID | None = None) -> tuple[set[str], str | None]:
    from audit_core.uc03_p2_dms_dumps import _READ, _UPDATE, _access, _dealers, _name_of

    held = _access(connection, tenant_id, principal, client, _UPDATE if roles == MARK_ROLES else _READ, roles)
    if journey_id is not None:
        dealer = connection.execute(
            text("SELECT dealer_id FROM auditcore.journeys WHERE tenant_id = :t AND journey_id = :j"),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one_or_none()
        if dealer is None:
            raise HTTPException(status_code=404, detail="Booking not found.")
        covered = {d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)}
        if str(dealer) not in covered:
            raise HTTPException(status_code=403, detail="That booking is not in a dealership you cover.")
    return held, _name_of(connection, principal.subject)


def _state(connection: Connection, tenant_id: str, journey_id: UUID) -> dict[str, Any] | None:
    row = connection.execute(
        text(
            """
            SELECT v.status, v.verified_by_name, v.verified_by, v.verified_role, v.verified_at_utc, v.comment,
                   k.result, k.checked_at_utc, k.changes, k.papers_changed
            FROM auditcore.p2_verified_bookings v
            LEFT JOIN LATERAL (
              SELECT * FROM auditcore.p2_verified_booking_checks k
              WHERE k.tenant_id = v.tenant_id AND k.journey_id = v.journey_id
              ORDER BY k.checked_at_utc DESC LIMIT 1) k ON true
            WHERE v.tenant_id = :t AND v.journey_id = :j AND v.status = 'ACTIVE'
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one_or_none()
    if row is None:
        return None
    return {"verifiedBy": row["verified_by_name"] or row["verified_by"], "verifiedRole": row["verified_role"],
            "verifiedAt": row["verified_at_utc"].isoformat(), "comment": row["comment"],
            "lastResult": row["result"], "lastCheckedAt": row["checked_at_utc"].isoformat()
            if row["checked_at_utc"] else None, "papersChanged": row["papers_changed"],
            "changes": row["changes"] or []}


@router.get("/journeys/{journey_id}/verified")
def get_verified(
    tenant_id: str, journey_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Whether the booking is verified, and whether the caller may mark it (Journey 360)."""
    from audit_core.uc03_p2_dms_dumps import _roles

    try:
        held, _ = _who(connection, tenant_id, principal, client, READ_ROLES, journey_id)
    except HTTPException as refused:
        if refused.status_code == 403:
            return {"verified": None, "canMark": False, "canSee": False}
        raise
    can_mark = "ADMIN" in held or bool(_roles(connection, tenant_id, principal.subject) & set(MARK_ROLES))
    return {"verified": _state(connection, tenant_id, journey_id), "canMark": can_mark, "canSee": True}


@router.post("/journeys/{journey_id}/verified")
def post_verified(
    tenant_id: str, journey_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Mark the booking verified: its figures were checked against its papers. The comment
    says what was checked (required)."""
    held, name = _who(connection, tenant_id, principal, client, MARK_ROLES, journey_id)
    comment = str((body or {}).get("comment") or "").strip()
    if len(comment) < 5:
        raise HTTPException(status_code=422, detail="Say what you checked against the papers (a comment).")
    role = "ADMIN" if "ADMIN" in held else next((r for r in MARK_ROLES if r in held), None)
    result = mark_verified(connection, tenant_id=tenant_id, journey_id=journey_id, actor_id=principal.subject,
                           actor_name=name, role=role, comment=comment)
    return {"reference": result["reference"], "verified": True}


@router.post("/journeys/{journey_id}/verified/withdraw")
def post_withdraw(
    tenant_id: str, journey_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> dict[str, Any]:
    _who(connection, tenant_id, principal, client, MARK_ROLES, journey_id)
    if not withdraw(connection, tenant_id=tenant_id, journey_id=journey_id, actor_id=principal.subject,
                    reason=(body or {}).get("reason")):
        raise HTTPException(status_code=404, detail="The booking is not verified.")
    return {"verified": False}


@router.get("/verified-bookings")
def get_verified_bookings(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    from audit_core.uc03_p2_dms_dumps import _dealers, _roles

    held, _ = _who(connection, tenant_id, principal, client, READ_ROLES)
    covered = {d["dealerName"] for d in _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)}
    items = [i for i in listing(connection, tenant_id=tenant_id) if i["dealer"] in covered]
    counts = {k: sum(1 for i in items if (i["lastCheck"] or {}).get("result") == k)
              for k in ("MATCHES", "CHANGED", "PAPERS_CHANGED", "ERROR")}
    can_check = "ADMIN" in held or bool(_roles(connection, tenant_id, principal.subject) & set(MARK_ROLES))
    return {"items": items, "counts": counts, "canCheck": can_check}


@router.post("/verified-bookings/check")
def post_check(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Compare every verified booking now (queued as a worker job: it takes a minute)."""
    _who(connection, tenant_id, principal, client, MARK_ROLES)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    job_id = connection.execute(
        text("INSERT INTO auditcore.p2_worker_jobs (job_kind, tag, reports, requested_by) "
             "VALUES ('VERIFIED_BOOKINGS_CHECK', :tag, '{}', :by) RETURNING job_id"),
        {"tag": f"{tenant_id}:on-demand-{stamp}", "by": principal.subject},
    ).scalar_one()
    return {"queued": True, "jobId": str(job_id)}


__all__ = [
    "MARK_ROLES",
    "booking_figures",
    "check_verified",
    "compare",
    "listing",
    "mark_verified",
    "papers",
    "router",
    "run_verified_check",
    "withdraw",
]
