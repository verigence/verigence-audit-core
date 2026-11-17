"""uc03_p2_bank_register.py -- each dealership bank account's running record of transactions,
as the statements are loaded (product owner 2026-11-17: "we need to keep a record of bank
statement transactions as and when they get loaded.. it could be daily or 2 times in a week").

A statement is uploaded daily or twice a week, and the periods often overlap (1-4 Nov, then
1-8 Nov). Every upload is kept; the register is the account's transactions over all its
current uploads, each transaction once -- the same date, amount, direction, narration and
running balance on two uploads is one transaction, shown as loaded by the first upload that
carried it. Per account it also says:

coverage        the days the uploads cover (each upload's period), the days of the month
                missing in between, and the days since the last one up to yesterday
balance check   uploads that follow one another (no day between) must continue the balance:
                the next upload's opening balance is the previous one's closing balance; a
                break means transactions are missing between them

``GET /p2/v1/tenants/{t}/bank-statements/accounts?month=YYYY-MM[&dealer_id=]``
``GET /p2/v1/tenants/{t}/bank-statements/accounts/register?month=&dealer_id=&account=``
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_bank_statements import BASIS_LABELS, _access, _money
from audit_core.uc03_p2_dms_dumps import _dealers, _month, _next_month, _today

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-bank-register"])

RESULT_LABELS = {"MATCHED": "Tied to receipt", "LOAN": "Loan of a Journey (no receipt)",
                 "UNIDENTIFIED": "No receipt", "OWN_TRANSFER": "Own transfer"}

_STATEMENTS_SQL = """
    SELECT s.statement_id, s.dealer_id, d.dealer_name, s.outlet_id, o.outlet_name, s.bank_name, s.account_number,
           s.account_holder, s.period_from, s.period_to, s.opening_balance, s.closing_balance, s.file_name,
           s.uploaded_at_utc, s.uploaded_by_name, s.uploaded_by
    FROM auditcore.p2_bank_statements s
    JOIN auditcore.dealers d ON d.tenant_id = s.tenant_id AND d.dealer_id = s.dealer_id
    LEFT JOIN auditcore.dealer_outlets o ON o.tenant_id = s.tenant_id AND o.outlet_id = s.outlet_id
    WHERE s.tenant_id = :t AND s.status = 'ACTIVE' AND s.period_to >= :f AND s.period_from < :l
      AND CAST(s.dealer_id AS text) = ANY(:ds)
    ORDER BY s.period_from, s.uploaded_at_utc
"""


def account_key(statement: dict[str, Any]) -> str:
    """One account of one dealership: its number (digits and letters only), else the upload."""
    number = re.sub(r"[^0-9A-Za-z]", "", str(statement.get("account_number") or ""))
    return f"{statement['dealer_id']}:{number or statement['statement_id']}"


def _identity(r: dict[str, Any]) -> tuple[Any, ...]:
    return (r["txn_date"], Decimal(str(r["amount"])).quantize(Decimal("0.01")), r["direction"],
            re.sub(r"\s+", "", str(r["narration"] or "").upper()),
            None if r["balance"] is None else Decimal(str(r["balance"])).quantize(Decimal("0.01")))


def coverage(statements: list[dict[str, Any]], first: date, last: date, yesterday: date) -> dict[str, Any]:
    """The days of first..last the uploads cover, the days missing between them, and those
    since the last covered day up to yesterday."""
    covered: set[date] = set()
    for s in statements:
        day = max(s["period_from"], first)
        while day <= min(s["period_to"], last):
            covered.add(day)
            day += timedelta(days=1)
    if not covered:
        return {"coveredDays": 0, "from": None, "to": None, "missing": [], "notYetLoaded": None}
    start, end = min(covered), max(covered)
    missing = [start + timedelta(days=n) for n in range((end - start).days + 1)
               if start + timedelta(days=n) not in covered]
    pending_to = min(last, yesterday)
    return {"coveredDays": len(covered), "from": start.isoformat(), "to": end.isoformat(),
            "missing": [d.isoformat() for d in missing],
            "notYetLoaded": ({"from": (end + timedelta(days=1)).isoformat(), "to": pending_to.isoformat()}
                             if pending_to > end else None)}


def balance_breaks(statements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Uploads that follow one another (no day between) whose balances do not continue."""
    out = []
    ordered = sorted(statements, key=lambda s: (s["period_from"], s["period_to"]))
    for before, after in pairwise(ordered):
        if after["period_from"] != before["period_to"] + timedelta(days=1):
            continue  # overlapping (the same days again) or a gap (shown as missing days)
        if before["closing_balance"] is None or after["opening_balance"] is None:
            continue
        closing, opening = Decimal(str(before["closing_balance"])), Decimal(str(after["opening_balance"]))
        if abs(closing - opening) > Decimal("1.00"):
            out.append({"after": before["period_to"].isoformat(), "closing": _money(closing),
                        "nextOpening": _money(opening), "difference": _money(opening - closing),
                        "fileBefore": before["file_name"], "fileAfter": after["file_name"]})
    return out


def registers(connection: Connection, *, tenant_id: str, dealer_ids: list[str], first: date, today: date,
              account: str | None = None, with_rows: bool = False) -> list[dict[str, Any]]:
    """Every account of the dealerships with an upload touching the month: its summary
    (and, with_rows, its transactions of the month, each once)."""
    last = _next_month(first) - timedelta(days=1)
    statements = [dict(r) for r in connection.execute(
        text(_STATEMENTS_SQL), {"t": tenant_id, "f": first, "l": _next_month(first), "ds": dealer_ids}).mappings()]
    by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in statements:
        by_account[account_key(s)].append(s)
    if account is not None:
        by_account = {k: v for k, v in by_account.items() if k == account}
    if not by_account:
        return []
    ids = [str(s["statement_id"]) for group in by_account.values() for s in group]
    rows = connection.execute(
        text("""
            SELECT x.statement_id, x.row_no, x.txn_date, x.value_date, x.narration, x.reference, x.cheque_number,
                   x.amount, x.direction, x.balance, x.credit_kind, x.outcome, x.match_basis,
                   CAST(x.journey_id AS text) AS journey_id, j.journey_reference
            FROM auditcore.p2_bank_statement_txns x
            LEFT JOIN auditcore.journeys j ON j.tenant_id = x.tenant_id AND j.journey_id = x.journey_id
            WHERE x.tenant_id = :t AND CAST(x.statement_id AS text) = ANY(:ids)
              AND x.txn_date BETWEEN :f AND :l
        """),
        {"t": tenant_id, "ids": ids, "f": first, "l": last},
    ).mappings().all()
    by_statement: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_statement[str(r["statement_id"])].append(dict(r))
    yesterday = today - timedelta(days=1)
    out = []
    for key, group in by_account.items():
        group = sorted(group, key=lambda s: s["uploaded_at_utc"])
        seen: dict[tuple[Any, ...], int] = {}
        txns: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for s in group:  # the first upload that carried a transaction is the one it was loaded by
            for r in sorted(by_statement.get(str(s["statement_id"]), []), key=lambda x: x["row_no"]):
                identity = _identity(r)
                if identity not in seen:
                    seen[identity] = len(txns)
                    txns.append((s, r))
                    continue
                # the reconciliation's verdict sits on the copy it did not set aside as printed again
                kept = txns[seen[identity]][1]
                if kept["outcome"] == "DUPLICATE" and r["outcome"] != "DUPLICATE":
                    for field in ("outcome", "match_basis", "journey_id", "journey_reference", "credit_kind"):
                        kept[field] = r[field]
        txns.sort(key=lambda sr: (sr[1]["txn_date"] or date.max, sr[0]["uploaded_at_utc"], sr[1]["row_no"]))
        credits = [r for _, r in txns if r["direction"] == "CR"]
        debits = [r for _, r in txns if r["direction"] == "DR"]
        latest = max(group, key=lambda s: (s["period_to"], s["uploaded_at_utc"]))
        earliest = min(group, key=lambda s: (s["period_from"], s["uploaded_at_utc"]))
        head = group[0]
        summary = {
            "account": key, "dealerId": str(head["dealer_id"]), "dealerName": head["dealer_name"],
            "outletName": head["outlet_name"], "bankName": head["bank_name"], "accountNumber": head["account_number"],
            "accountHolder": head["account_holder"], "uploads": len(group),
            "lastUploadedAtUtc": max(s["uploaded_at_utc"] for s in group).isoformat(),
            "openingBalance": _money(earliest["opening_balance"]) if earliest["opening_balance"] is not None else None,
            "closingBalance": _money(latest["closing_balance"]) if latest["closing_balance"] is not None else None,
            "transactions": len(txns), "credits": len(credits),
            "creditTotal": _money(sum((Decimal(str(r["amount"])) for r in credits), Decimal(0))),
            "debits": len(debits), "debitTotal": _money(sum((Decimal(str(r["amount"])) for r in debits), Decimal(0))),
            "creditsNoReceipt": sum(1 for r in credits if r["outcome"] == "UNIDENTIFIED"),
            "coverage": coverage(group, first, last, yesterday), "balanceBreaks": balance_breaks(group),
        }
        if with_rows:
            summary["rows"] = [{
                "date": r["txn_date"].isoformat() if r["txn_date"] else None,
                "valueDate": r["value_date"].isoformat() if r["value_date"] else None,
                "narration": r["narration"], "reference": r["reference"] or r["cheque_number"],
                "debit": _money(r["amount"]) if r["direction"] == "DR" else None,
                "credit": _money(r["amount"]) if r["direction"] == "CR" else None,
                "balance": _money(r["balance"]) if r["balance"] is not None else None,
                "result": RESULT_LABELS.get(r["outcome"] or "") if r["direction"] == "CR" else None,
                "basisLabel": BASIS_LABELS.get(r["match_basis"] or ""),
                "journeyId": r["journey_id"], "journeyReference": r["journey_reference"],
                "loadedFrom": s["file_name"], "loadedAtUtc": s["uploaded_at_utc"].isoformat(),
            } for s, r in txns]
        out.append(summary)
    return sorted(out, key=lambda a: (str(a["dealerName"]).lower(), str(a["bankName"]), str(a["accountNumber"])))


@router.get("/bank-statements/accounts")
def list_bank_accounts(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    month: str | None = None,
    dealer_id: UUID | None = None,
) -> dict[str, Any]:
    """The month's bank accounts: transactions on record, days covered and missing, balance check."""
    held = _access(connection, tenant_id, principal, client)
    today = _today(connection, tenant_id)
    first = _month(month, today)
    dealer_ids = [d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)
                  if dealer_id is None or d["dealerId"] == str(dealer_id)]
    return {"month": first.strftime("%Y-%m"),
            "items": registers(connection, tenant_id=tenant_id, dealer_ids=dealer_ids, first=first, today=today)}


@router.get("/bank-statements/accounts/register")
def read_bank_account_register(
    tenant_id: str,
    account: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    month: str | None = None,
) -> dict[str, Any]:
    """One account's transactions of the month, each once, with the upload that loaded it."""
    held = _access(connection, tenant_id, principal, client)
    today = _today(connection, tenant_id)
    first = _month(month, today)
    dealer_id = account.split(":", 1)[0]
    if dealer_id not in {d["dealerId"] for d in _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)}:
        raise HTTPException(status_code=403, detail="That dealership is not one you cover.")
    found = registers(connection, tenant_id=tenant_id, dealer_ids=[dealer_id], first=first, today=today,
                      account=account, with_rows=True)
    if not found:
        raise HTTPException(status_code=404, detail="No statement of that account in the month.")
    return {"month": first.strftime("%Y-%m"), **found[0]}


__all__ = ["account_key", "balance_breaks", "coverage", "registers", "router"]
