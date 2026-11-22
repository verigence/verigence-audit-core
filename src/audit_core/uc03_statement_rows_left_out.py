"""NDC rows the Deal does not take, told to the PC (product owner 2026-11-22).

"Since 7.2 doesn't add up to 60,000 then why no task was raised ... when NDC
[row] is not added, why it is not added to the Deal ... what PC can do in
this". The statement reader leaves a row out of the Deal when it cannot tell
which line it is: a wording no label reads (once its misspelt words are put
right), a handwritten working that does not give the row's amount, a scrappage
row that names no bonus. Until now that was noted only inside the reading:
live, 184 of 1,150 NDC bookings had a row left out and nobody was told
(ADI-RD-261112-021: the Rs 60,000 charger).

One task per booking, STATEMENT_ROWS_NOT_IN_DEAL, to the PC, listing each row
with its amount and why. The PC corrects the reading of the row (a misread
word or amount): the row is read again and, once read, leaves the list. A row
read right whose wording names no line is closed with a comment saying what it
is, which the Team Lead sees. The task closes by itself once no row is left.

Not listed: rows the reader leaves out on purpose and says why (the old
vehicle's value or a total read on a discount row, a charger settlement), and
the postings of a computer-printed statement filed as the NDC ("Journal_NonGST,
...", "Money Receipt (Bank), ...", "Bank Finance DO (BDO), ..."), which are the
payments, not the deal. Only for a Project whose Deal is priced by the NDC.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection

from audit_core.uc03_account_statement import (
    KIND_FORM,
    combine_statements,
    line_rows,
    load_ledger_documents,
    statement_kind,
)

logger = structlog.get_logger(__name__)

TASK_TYPE = "STATEMENT_ROWS_NOT_IN_DEAL"
# The reasons a row is left out for a person to say what it is.
_FOR_A_PERSON = (
    "label not in the mapping",
    "the working written beside it does not agree with the amount",
    "names the scrappage but no bonus: it may be the certificate of deposit bought for it",
)
_WHY = {
    "label not in the mapping": "its wording names no Deal line",
    "the working written beside it does not agree with the amount": "the working written beside it does not give the amount",
    "names the scrappage but no bonus: it may be the certificate of deposit bought for it":
        "it names the scrappage but no bonus (it may be the certificate of deposit)",
}
_POSTING = re.compile(
    r"(\d{4}-\d{2}-\d{2}, )?(.*\b(JOURNAL|MONEY RECEIPT|BANK FINANCE DO|VEHICLE SALES|SALES WORKSHOP).*"
    r"|PAYMENT RECEIVED.*|PAYMENT RECEIPT.*|ROUND OFF)")


def rows_left_out(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """The NDC rows the Deal does not take, for a person: {"label", "amount", "why"}."""
    documents = load_ledger_documents(connection, tenant_id=tenant_id, journey_id=journey_id)
    forms = [(d, rows) for d in documents
             if (rows := line_rows((d.get("fields") or {}).get("line_items"))) and statement_kind(rows) == KIND_FORM]
    statement = combine_statements(forms)[0] if forms else None
    if statement is None:
        return []
    out: list[dict[str, Any]] = []
    for row in statement.unmapped:
        label, why = str(row["label"]), str(row["why"])
        if why not in _FOR_A_PERSON or _POSTING.fullmatch(label):
            continue
        item = {"label": label, "amount": Decimal(str(row["amount"])), "why": _WHY[why]}
        if item not in out:
            out.append(item)
    return out


def _rupees(value: Decimal) -> str:
    whole = f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"Rs {whole}"


def describe(rows: list[dict[str, Any]]) -> str:
    return "; ".join(f"\"{r['label']}\" {_rupees(r['amount'])}: {r['why']}" for r in rows)


def _task_key(journey_id: UUID) -> str:
    return f"statement-rows-not-in-deal:{journey_id}"


def sync_rows_left_out(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """Raise, refresh or close the booking's one task. Never raises."""
    from audit_core.uc03_deal_source_policy import account_statement_is_source_of_truth
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh, resolve_if_open

    try:
        with connection.begin_nested():
            if not account_statement_is_source_of_truth(connection, tenant_id=tenant_id):
                return {"skipped": True}
            rows = rows_left_out(connection, tenant_id=tenant_id, journey_id=journey_id)
            if rows:
                raise_or_refresh(
                    connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=_task_key(journey_id),
                    task_type=TASK_TYPE, source_type="RESOLVER", source_code=TASK_TYPE,
                    title="NDC rows not used in the Deal",
                    description=(
                        "These rows of the NDC are not in the Deal, because the system cannot tell which line "
                        "they are: " + describe(rows) + ". Open the NDC and correct the reading of each row "
                        "(the wording or the amount): a corrected row is read again and, once read, leaves this "
                        "list. If a row was read right, close this task with a comment saying what the row is "
                        "(for example \"TR is the temporary registration\"): the Team Lead sees it."
                    ),
                    reference={"sourceCode": TASK_TYPE, "rows": [
                        {"label": r["label"], "amount": str(r["amount"]), "why": r["why"]} for r in rows]},
                    severity="MEDIUM", registry=get_registry(),
                )
            else:
                resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id,
                                dedupe_key=_task_key(journey_id), evidence={"rowsLeftOut": 0})
            return {"rows": len(rows)}
    except Exception:
        logger.warning("statement_rows_left_out_failed", tenant_id=tenant_id, journey_id=str(journey_id),
                       exc_info=True)
        return {"error": True}


__all__ = ["TASK_TYPE", "describe", "rows_left_out", "sync_rows_left_out"]
