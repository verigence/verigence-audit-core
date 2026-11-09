"""Registration on a scrappage deal (product owner 2026-11-09).

Odisha's Motor Vehicles (Registration & Functions of Vehicle Scrapping
Facility) Policy 2022, following the Central Motor Vehicles (24th Amendment)
Rules 2021 (rule 51A): a new non-transport vehicle registered against a
Certificate of Deposit pays 25% less motor vehicle tax (15% for a transport
vehicle), and its registration fee is waived. On a scrappage deal the
"Road tax + registration" charged is therefore below the price master's
standard, and DEAL_UNDERCHARGED used to raise it as a shortfall.

Product owner: "check if the discount is 25% exactly (within +-100) then pls
do that and let PC know it". So, for each registration line of the deal:

- the shortfall is 25% of the line's standard, within Rs 100, and the deal
  has scrappage on record (a Certificate of Deposit on file, or a scrappage
  bonus given): the shortfall is the concession. The line's standard is taken
  as 75% of the master's, it is not a shortfall, and the PC gets one
  information task saying so (closed with a comment);
- the shortfall is 25% within Rs 100 but no scrappage is on record: it stays
  a shortfall, with the note that it matches the concession and no
  Certificate of Deposit or scrappage benefit is on file;
- the deal has scrappage but the shortfall is not 25%: it stays a
  shortfall, with what the concession would have been.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection

CONCESSION_RATE = Decimal("0.25")
TOLERANCE = Decimal(100)
TASK_TYPE = "SCRAPPAGE_REGISTRATION_CONCESSION"
POLICY = ("Odisha Motor Vehicles (Registration & Functions of Vehicle Scrapping Facility) Policy 2022: "
          "25% concession in motor vehicle tax for a non-transport vehicle registered against a Certificate "
          "of Deposit, registration fee waived")
# The deal lines that carry the road tax.
REGISTRATION_KEYS = frozenset({"road_tax_registration", "road_tax_amount", "registration_charges"})
COD_DOCUMENT_TYPES = ("scrappage_certificate_of_deposit",)


def scrappage_evidence(*, cod_documents: int, scrappage_discounts: list[dict[str, Any]]) -> str | None:
    """Why the deal counts as a scrappage deal, in words, or None."""
    reasons = []
    if cod_documents:
        reasons.append("a Certificate of Deposit is on file")
    given = [d for d in scrappage_discounts if d.get("label")]
    if given:
        reasons.append(" and ".join(str(d["label"]) for d in given) + " given")
    return "; ".join(reasons) or None


def assess_line(*, standard: Decimal, charged: Decimal, scrappage: str | None) -> dict[str, Any]:
    """The registration line against the concession: ``explained`` when the
    shortfall is the concession on a scrappage deal; a ``note`` otherwise."""
    short = standard - charged
    concession = (standard * CONCESSION_RATE).quantize(Decimal("0.01"))
    off_by = short - concession
    matches = abs(off_by) <= TOLERANCE
    result: dict[str, Any] = {"concession": concession, "offBy": off_by, "matches": matches, "explained": False}
    if matches and scrappage:
        result["explained"] = True
        result["note"] = (f"matches the 25% scrappage concession on the standard ({_rupees(concession)}; "
                          f"{scrappage})")
    elif matches:
        result["note"] = ("matches the 25% scrappage concession, but no Certificate of Deposit or scrappage "
                          "benefit is on file")
    elif scrappage:
        result["note"] = (f"scrappage deal ({scrappage}): the 25% concession would be {_rupees(concession)}; "
                          f"the shortfall is {_rupees(abs(off_by))} {'more' if off_by > 0 else 'less'} than that")
    return result


def _rupees(value: Decimal) -> str:
    from audit_core.uc03_p2_audit_rules import _rupees as fmt

    return fmt(value)


def _task_key(journey_id: UUID | str) -> str:
    return f"scrappage-registration:{journey_id}"


def refresh_pc_task(connection: Connection, *, tenant_id: str, journey_id: UUID,
                    explained: list[dict[str, Any]]) -> None:
    """Tell the PC (one information task per journey) that the registration
    was taken as lower by the scrappage concession; resolve it once no line
    is explained that way any more."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh, resolve_if_open

    if not explained:
        resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id,
                        dedupe_key=_task_key(journey_id), evidence={"scrappageConcession": []})
        return
    lines = "; ".join(
        f"{e['label']}: {_rupees(Decimal(e['charged']))} charged against the standard "
        f"{_rupees(Decimal(e['standard']))}, {_rupees(Decimal(e['short']))} lower = the 25% concession "
        f"{_rupees(Decimal(e['concession']))} (within Rs 100)"
        for e in explained
    )
    why = str(explained[0]["scrappage"] or "")
    why = why[:1].upper() + why[1:]
    raise_or_refresh(
        connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=_task_key(journey_id),
        task_type=TASK_TYPE, source_type="RESOLVER", source_code=TASK_TYPE,
        title="Registration lower by the scrappage concession",
        description=(
            f"For information: {lines}. {why}. Under the {POLICY}, so the "
            "difference is not counted as an undercharge. Check the registration was made against this "
            "Certificate of Deposit and close this task with a comment."
        ),
        reference={"sourceCode": TASK_TYPE,
                   "lines": [{k: str(v) for k, v in e.items()} for e in explained]},
        severity="MEDIUM", registry=get_registry(),
    )
