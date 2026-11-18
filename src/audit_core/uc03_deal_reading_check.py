"""A Deal line far from its standard is checked by hand (product owner 2026-11-18).

"Fasttag cost wont be more than Rs 600, ...and if some other line item is
coming up or being written wrong, lets raise manual verification task".

Live 2026-11-18: 23 of the 448 journeys more than Rs 10,000 off their standard
carried Rs 7.26 crore of the Rs 8.06 crore positive variance, each from one
line read many times its standard: an ex-showroom of 1,03,81,999 for
10,38,999 (a digit too many), accessories of 4,95,34,918 for 5,000, RTO of
13,27,701 for 1,32,520, insurance of 4,21,282 for 42,475. Such a line is a
misreading or a figure written on the wrong row far more often than a price.

A line is to be checked when, with a standard of Rs 1,000 or more:
* it reads more than three times its standard, or
* it is the ex-showroom and differs from the price list by more than 25% (an
  invoice ex-showroom cannot differ from the price list's, 2026-10-24).

The PC gets one HIGH task per journey (DEAL_LINE_VERIFY) naming the lines,
closed by correcting the reading or with a comment; it resolves itself once no
line is so far off. The journey's observation severity is MEDIUM for it
(uc03_observation_severity). Raised from the Deal each time the list's
variance is worked out (uc03_p2_price_variance), so every journey is covered.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import Connection

TASK_TYPE = "DEAL_LINE_VERIFY"
FACTOR = Decimal(3)
EX_SHOWROOM_SHARE = Decimal("0.25")
MIN_STANDARD = Decimal(1000)
_CHECKED = ("ex_showroom_price", "tcs_amount", "insurance_amount", "road_tax_registration", "road_tax_amount",
            "registration_charges", "accessories_cost", "additional_warranty_amount", "rsa_amount")


def _dec(value: Any) -> Decimal | None:
    try:
        return None if value in (None, "") else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def lines_to_verify(view: dict[str, Any]) -> list[dict[str, Any]]:
    """The Deal lines far enough from their standard to be checked by hand."""
    out = []
    for group in view.get("categories") or []:
        for row in group.get("components") or []:
            if row.get("key") not in _CHECKED or row.get("partOf") or row.get("excluded") or row.get("notTaken"):
                continue
            standard, current = _dec(row.get("standardMaster") or row.get("standard")), _dec(row.get("current"))
            if standard is None or current is None or standard < MIN_STANDARD:
                continue
            if current > FACTOR * standard:
                why = f"reads {current / standard:.1f} times its standard"
            elif row["key"] == "ex_showroom_price" and abs(current - standard) > EX_SHOWROOM_SHARE * standard:
                why = "differs from the price list by more than 25%"
            else:
                continue
            out.append({"key": row["key"], "label": row.get("label") or row["key"], "standard": str(standard),
                        "current": str(current), "why": why})
    return out


def refresh_task(connection: Connection, *, tenant_id: str, journey_id: UUID, view: dict[str, Any]) -> None:
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh, resolve_if_open

    found = lines_to_verify(view)
    key = f"deal-line-verify:{journey_id}"
    if not found:
        resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                        evidence={"linesToVerify": 0})
        return
    text_ = "; ".join(f"{f['label']} Rs {Decimal(f['current']):,.0f} against the standard "
                      f"Rs {Decimal(f['standard']):,.0f} ({f['why']})" for f in found)
    raise_or_refresh(
        connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key, task_type=TASK_TYPE,
        source_type="RESOLVER", source_code=TASK_TYPE, title="Check these Deal lines against the papers",
        description=(f"{text_}. A figure this far from its standard is usually misread or written on another "
                     "row. Check it on the NDC / document: correct the reading, or close this task with what the "
                     "charge is."),
        reference={"sourceCode": TASK_TYPE, "lines": found}, severity="HIGH", registry=get_registry(),
    )


__all__ = ["TASK_TYPE", "lines_to_verify", "refresh_task"]
