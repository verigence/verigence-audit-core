"""Every Journey's Deal reviewed for scrappage, RTO, FASTag and RSA (2026-11-17).

Product owner 2026-11-17: "for all the journey the variance amount coming is
quite high... lot of wrong calculations for RTO where scrappage discount is
given but it is not handled in the Deal ... fastag / RSA is being purchased by
the customer and it is NDC but it doesnt reflect in deal ... scrappage discount
on RTO amount is given but scrappage document is missing or not update in the
deal ... review all journey and check specifically for these issues".

Read-only. For every Journey it builds the Deal exactly as Journey 360 does and
reports, per Journey with anything to say:

* scrappage: the rebate the RTO challan prints, the Certificates of Deposit on
  file, the concession the Deal took off the RTO standard, and a certificate
  missing where the RTO took the concession or a scrappage benefit was given;
* RTO: the line's standard (the master's, and after the concession) against
  what was charged;
* FASTag and RSA: what the NDC charges as read now, what the Deal counts, and
  what the dealer's customer ledger / Tally posts for them that the NDC does
  not charge (shown for a person to check: the NDC alone prices the deal,
  product owner 2026-11-15);
* the Deal's variance.

Started as worker job ``DEAL_SCRAPPAGE_REVIEW`` (tag ``<tenant_id>:<label>``,
``reports`` optionally limiting it to some journey references). Its report is
the job's detail.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

_FASTAG = re.compile(r"\bFAST[ -]?T?T?AGE?\b|\bFT/", re.IGNORECASE)
_RSA = re.compile(r"\bRSA\b|ROAD ?SIDE", re.IGNORECASE)
_ITEMS_CAP = 1500


def _dec(value: Any) -> Decimal | None:
    try:
        return None if value in (None, "") else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _ledger_postings(documents: list[dict[str, Any]]) -> dict[str, Decimal]:
    """What the Tally / DMS customer ledgers (and the Tally rows of the NDC
    slot) debit the customer for FASTag and RSA, the largest copy's."""
    from audit_core.uc03_account_statement import KIND_TALLY, line_rows, statement_kind

    out: dict[str, Decimal] = {}
    for document in documents:
        rows = line_rows((document.get("fields") or {}).get("line_items"))
        if statement_kind(rows) != KIND_TALLY:
            continue
        found: dict[str, Decimal] = {}
        for row in rows:
            label = str(row.get("particulars_raw") or "")
            debit = _dec(row.get("debit_amount"))
            if not debit or debit <= 0:
                continue
            if _FASTAG.search(label):
                found["fastag"] = found.get("fastag", Decimal(0)) + debit
            elif _RSA.search(label):
                found["rsa"] = found.get("rsa", Decimal(0)) + debit
        for key, amount in found.items():
            out[key] = max(out.get(key, Decimal(0)), amount)
    return out


def _row(view: dict[str, Any], key: str) -> dict[str, Any] | None:
    return next((r for g in view.get("categories") or [] for r in g.get("components") or [] if r.get("key") == key),
                None)


def review_journey(connection: Any, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    from audit_core.uc03_account_statement import (
        KIND_FORM,
        combine_statements,
        line_rows,
        load_ledger_documents,
        statement_kind,
    )
    from audit_core.uc03_p2_journey360 import deal

    view = deal(connection, tenant_id=tenant_id, journey_id=journey_id)
    summary = view.get("summary") or {}
    documents = load_ledger_documents(connection, tenant_id=tenant_id, journey_id=journey_id)
    forms = [(d, rows) for d in documents if statement_kind(rows := line_rows(d["fields"].get("line_items"))) == KIND_FORM]
    statement, _copies = combine_statements(forms) if forms else (None, None)
    ndc = statement.components if statement else {}
    ledger = _ledger_postings(documents)
    rto = next((r for k in ("road_tax_registration", "road_tax_amount", "registration_charges")
                if (r := _row(view, k)) is not None and r.get("standard") is not None and not r.get("partOf")), None)
    scrappage = view.get("scrappage") or {}
    products: dict[str, Any] = {}
    for name, key in (("fastag", "fastag_amount"), ("rsa", "rsa_amount")):
        line = _row(view, key)
        on_ndc = ndc.get(key)
        counted = _dec(line.get("current")) if line else None
        products[name] = {
            "ndc": str(on_ndc) if on_ndc is not None else None,
            "deal": str(counted) if counted is not None else None,
            "notTaken": bool(line and line.get("notTaken")),
            "ledger": str(ledger[name]) if name in ledger else None,
            # charged on the NDC, yet not counted on the Deal
            "missingOnDeal": on_ndc is not None and on_ndc > 0 and (counted is None or abs(counted - on_ndc) > 1),
            # posted on the customer ledger / Tally, not charged on the NDC
            "ledgerOnly": name in ledger and (on_ndc is None or on_ndc <= 0) and bool(statement),
        }
    return {
        "variance": (summary.get("variance") or {}).get("currentVsStandard"),
        "deliveryStarted": bool(summary.get("deliveryStarted")),
        "ndcRead": bool(statement),
        "rto": {"masterStandard": (rto or {}).get("standardMaster") or (rto or {}).get("standard"),
                "standard": (rto or {}).get("standard"), "charged": (rto or {}).get("current"),
                "variance": (rto or {}).get("variance")} if rto else None,
        "scrappage": {
            "challanRebate": scrappage.get("challanRebate"),
            "certificates": [c.get("certificateNumber") for c in scrappage.get("certificates") or []],
            "concession": (scrappage.get("concession") or {}).get("concession"),
            "concessionSource": (scrappage.get("concession") or {}).get("source"),
            "benefits": [{"key": b.get("key"), "amount": b.get("amount")} for b in scrappage.get("benefits") or []],
            "codMissing": bool(scrappage.get("codMissing")),
        } if scrappage else None,
        **products,
    }


def run_deal_scrappage_review(engine: Engine, tag: str, reports: list[str]) -> dict[str, Any]:
    tenant_id = tag.split(":", 1)[0]
    references = [str(r).strip() for r in reports or [] if str(r).strip()]
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        journeys = connection.execute(
            text("SELECT journey_id, journey_reference FROM auditcore.journeys WHERE tenant_id=:t "
                 + ("AND journey_reference = ANY(:r) " if references else "") + "ORDER BY journey_reference"),
            {"t": tenant_id, "r": references},
        ).mappings().all()
    totals = {"journeys": len(journeys), "failed": 0, "rebateOnChallan": 0, "certificateOnFile": 0,
              "concessionApplied": 0, "codMissing": 0, "scrappageNoConcession": 0,
              "fastagMissingOnDeal": 0, "rsaMissingOnDeal": 0, "fastagLedgerOnly": 0, "rsaLedgerOnly": 0,
              "varianceOver10000": 0}
    items: list[dict[str, Any]] = []
    for journey in journeys:
        try:
            with engine.begin() as connection:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                set_tenant_context(connection, tenant_id)
                item = review_journey(connection, tenant_id=tenant_id, journey_id=UUID(str(journey["journey_id"])))
        except Exception as exc:
            totals["failed"] += 1
            logger.warning("deal_scrappage_review_failed", tag=tag, reference=journey["journey_reference"],
                           exc_info=True)
            if len(items) < _ITEMS_CAP:
                items.append({"ref": journey["journey_reference"], "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        s = item["scrappage"] or {}
        totals["rebateOnChallan"] += bool(s.get("challanRebate"))
        totals["certificateOnFile"] += bool(s.get("certificates"))
        totals["concessionApplied"] += bool(s.get("concession"))
        totals["codMissing"] += bool(s.get("codMissing"))
        totals["scrappageNoConcession"] += bool(s) and not s.get("concession")
        for name in ("fastag", "rsa"):
            totals[f"{name}MissingOnDeal"] += item[name]["missingOnDeal"]
            totals[f"{name}LedgerOnly"] += item[name]["ledgerOnly"]
        variance = _dec(item["variance"])
        totals["varianceOver10000"] += variance is not None and abs(variance) > 10000
        worth = bool(s) or any(item[n]["missingOnDeal"] or item[n]["ledgerOnly"] for n in ("fastag", "rsa"))
        if worth and len(items) < _ITEMS_CAP:
            items.append({"ref": journey["journey_reference"], **item})
    result = {"tag": tag, "totals": totals, "items": items}
    logger.info("deal_scrappage_review_finished", tag=tag, **totals)
    return result


__all__ = ["review_journey", "run_deal_scrappage_review"]
