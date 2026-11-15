"""How registration and EW / RSA invoice tasks may be closed (product owner 2026-11-15).

The product owner's answers to the rule review of 15 Nov:

- The RTO challan is mandatory. A delivery registered on a **temporary
  registration** has no challan yet: a Team Lead or Reviewer (never the PC)
  closes the missing-challan task with a comment that carries the temporary
  registration number.
- A **BH-series** registration pays the road tax for two years, far below a
  state registration. When the registration charged is quite low -- below half
  of the price master's registration for the model -- and nothing on file says
  BH yet, the PC is asked whether it is a BH registration. Only a "BH
  registration" answer makes it one; the price master's RTO figure is then not
  compared (Journey 360 ``_bh_series``).
- The EW / RSA invoice is required when the customer bought EW / RSA. Some
  dealerships do not issue one: the PC closes that task as "Dealer not
  provided".

Each of these is recorded in the journey's System remarks
(uc03_system_remarks), read from the closed task: nothing is stored twice.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

# Missing-document close choices (the button's words are the choice).
DEALER_NOT_PROVIDED = "Dealer not provided"
TEMPORARY_REGISTRATION = "Temporary registration"
DEALER_NOT_PROVIDED_REQUIREMENTS = frozenset({"ew_invoice", "rsa_invoice"})
TEMPORARY_REGISTRATION_REQUIREMENTS = frozenset({"rto_challan"})
TEMPORARY_REGISTRATION_ROLES = frozenset({"TL", "PM", "REVIEWER"})

# The BH question.
BH_TASK_TYPE = "REGISTRATION_BH_CONFIRM"
BH_CHOICE = "BH registration"
NOT_BH_CHOICE = "Not a BH registration"
BH_CHOICES = (BH_CHOICE, NOT_BH_CHOICE)
# "In case the amount of RTO is quite low" (product owner 2026-11-15; the
# threshold proposed and accepted the same day): below half the standard.
LOW_SHARE = Decimal("0.5")

# A registration number in the comment: one word with letters and at least
# four digits ("OD02T1234", "T0925OD1234A"), or the spaced form ("OD 02 TR 1234").
_SPACED_NUMBER = re.compile(r"\b[A-Z]{2}[ \-]?\d{2}[ \-]?([A-Z]{0,3})[ \-]?\d{4}\b")
_MONTHS = frozenset({"JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"})


def _requirement(task: dict[str, Any]) -> str:
    reference = task.get("reference") or {}
    if isinstance(reference, str):
        import json

        reference = json.loads(reference or "{}")
    return str(reference.get("requirementKey") or "")


def check_close_choice(task: dict[str, Any], *, choice: str, actor_role_code: str, comment: str | None) -> None:
    """Refuse a close choice the task or the person cannot use (ValueError)."""
    requirement = _requirement(task)
    if choice == DEALER_NOT_PROVIDED and requirement not in DEALER_NOT_PROVIDED_REQUIREMENTS:
        raise ValueError("\"Dealer not provided\" closes only the extended warranty or RSA invoice task.")
    if choice == TEMPORARY_REGISTRATION:
        if requirement not in TEMPORARY_REGISTRATION_REQUIREMENTS:
            raise ValueError("\"Temporary registration\" closes only the RTO challan task.")
        if str(actor_role_code or "").upper() not in TEMPORARY_REGISTRATION_ROLES:
            raise PermissionError("Only a Team Lead or Reviewer closes the RTO challan task as a temporary "
                                  "registration.")
        if temporary_number(comment) is None:
            raise ValueError("Write the temporary registration number in the comment to close this task.")


def is_temporary_registration_close(task: dict[str, Any], choice: str | None) -> bool:
    return (choice or "") == TEMPORARY_REGISTRATION and _requirement(task) in TEMPORARY_REGISTRATION_REQUIREMENTS


def temporary_number(comment: str | None) -> str | None:
    """The temporary registration number written in the comment, or None."""
    body = (comment or "").upper()
    if body.startswith(TEMPORARY_REGISTRATION.upper()):
        body = body[len(TEMPORARY_REGISTRATION):]
    for token in body.split():
        value = token.strip(".,;:()[]\"'")
        if sum(ch.isdigit() for ch in value) >= 4 and any(ch.isalpha() for ch in value):
            return value
    for spaced in _SPACED_NUMBER.finditer(body):
        if spaced.group(1) not in _MONTHS:  # "on 12 Nov 2026" is a date, not a number
            return re.sub(r"[\s\-]+", "", spaced.group(0))
    return None


# ── BH ────────────────────────────────────────────────────────────────────────


def confirmed_bh(connection: Connection, *, tenant_id: str, journey_id: UUID) -> bool:
    """The PC answered the BH question "BH registration"."""
    return bool(connection.execute(
        text(
            """
            SELECT EXISTS (SELECT 1 FROM auditcore.p2_tasks
                           WHERE tenant_id=:t AND journey_id=:j AND task_type=:type
                             AND task_status='VERIFIED_COMPLETE' AND completion_result->>'choice' = :choice)
            """
        ),
        {"t": tenant_id, "j": journey_id, "type": BH_TASK_TYPE, "choice": BH_CHOICE},
    ).scalar_one())


def is_low(*, standard: Decimal, charged: Decimal) -> bool:
    return standard > 0 and charged < standard * LOW_SHARE


def _task_key(journey_id: UUID | str) -> str:
    return f"registration-bh:{journey_id}"


def refresh_bh_task(connection: Connection, *, tenant_id: str, journey_id: UUID,
                    lines: list[dict[str, Any]]) -> None:
    """Ask the PC (one task per journey) whether a registration charged far
    below the standard is a BH registration; resolve it once no line is."""
    from audit_core.uc03_p2_audit_rules import _rupees
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh, resolve_if_open

    if not lines:
        resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id,
                        dedupe_key=_task_key(journey_id), evidence={"lowRegistration": []})
        return
    said = "; ".join(f"{line['label']}: {_rupees(Decimal(str(line['charged'])))} charged against the standard "
                     f"{_rupees(Decimal(str(line['standard'])))}" for line in lines)
    raise_or_refresh(
        connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=_task_key(journey_id),
        task_type=BH_TASK_TYPE, source_type="RESOLVER", source_code=BH_TASK_TYPE,
        title="Is this a BH registration?",
        description=(f"The registration is less than half the standard: {said}. A BH-series registration "
                     "pays the road tax for two years, so it is lower. Answer \"BH registration\" or "
                     "\"Not a BH registration\"."),
        reference={"sourceCode": BH_TASK_TYPE,
                   "lines": [{k: str(v) for k, v in line.items()} for line in lines]},
        severity="MEDIUM", registry=get_registry(),
    )
