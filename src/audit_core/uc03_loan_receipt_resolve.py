"""Re-resolve the loan receipt of every financed booking (2026-11-02).

uc03_finance_disbursement_resolution now links a receipt as the loan only
when it is in the financier's name or equals the loan amount the dealer
statement / Bank Approval Letter states, and never guesses (the MEDIUM
"only eligible post-booking payment" link is gone: Bhikari Singh's
Rs 2,50,000 customer receipt was taken as the loan). This job runs the same
resolver over what is already stored, per active tenant: every booking with
a finance record naming a financier, except bookings cancelled, marked a
duplicate or closed without delivery. A PC's confirmation is never touched.

p2_worker_jobs kind LOAN_RECEIPT_RESOLVE. A DRY RUN -- nothing written, the
outcome worked out by plan_finance_disbursement -- unless the tag ends with
"-commit"; ``reports`` may list journey references to limit it to. Each
booking is logged (``loan_receipt_resolve_item``) and the job returns, per
tenant, the counts by old -> new confidence (``"MEDIUM->HIGH"``, ``"MEDIUM->
UNVERIFIED"``, ``"HIGH->HIGH"`` ...), how many links change receipt, how
many bookings the PC is asked about, and up to _SAMPLES sample bookings per
transition that changes (reference, before / after confidence, amount and
payment, reason, basis, the stated loan amounts), so a dry run can be read
off the job row. Safe to run again.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

_SAMPLES = 15

# The Booking is not cancelled, a duplicate, or closed with no delivery (the
# same test the nightly review and SKU retry use, uc03_p2_page_removal).
_BOOKING_OPEN = """
    NOT EXISTS (
      SELECT 1 FROM auditcore.journey_stage_states s
      WHERE s.tenant_id=j.tenant_id AND s.journey_id=j.journey_id AND s.stage_code='BOOKING'
        AND (s.business_status IN ('BOOKING_CANCELLED','DUPLICATE_BOOKING')
             OR s.closure_disposition='NO_DELIVERY'))
"""


def financed_bookings(connection, tenant_id: str, references: list[str] | None = None) -> list[dict[str, Any]]:
    """Every booking of the tenant whose newest finance record names a
    financier, with whether the booking is still open."""
    return [dict(r) for r in connection.execute(
        text(
            f"""
            SELECT j.journey_id, j.journey_reference, ({_BOOKING_OPEN}) AS booking_open
            FROM auditcore.journeys j
            WHERE j.tenant_id = :t
              AND (CAST(:refs AS text[]) IS NULL OR j.journey_reference = ANY(CAST(:refs AS text[])))
              AND EXISTS (
                SELECT 1 FROM auditcore.finance_records f
                WHERE f.tenant_id = j.tenant_id AND f.journey_id = j.journey_id
                  AND NULLIF(btrim(f.provider_name), '') IS NOT NULL)
            ORDER BY j.journey_reference
            """
        ),
        {"t": tenant_id, "refs": references or None},
    ).mappings().all()]


def _money(value: Any) -> str | None:
    return None if value is None else str(value)


def _payment_id(value: Any) -> str | None:
    return None if value is None else str(value)


def run_loan_receipt_resolve(
    engine: Engine, tag: str, tenant_ids: list[str], references: list[str] | None = None,
) -> dict[str, Any]:
    from audit_core.uc03_finance_disbursement_resolution import (
        plan_finance_disbursement,
        resolve_finance_disbursement,
    )

    commit = tag.endswith("-commit")
    report: dict[str, Any] = {"commit": commit, "tag": tag, "tenants": {}}
    for tenant_id in tenant_ids:
        totals: dict[str, Any] = {"bookings": 0, "closed_skipped": 0, "failed": 0, "transitions": {},
                                  "linkChanged": 0, "pcConfirmedKept": 0, "pcAsked": 0, "samples": {}}
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                bookings = financed_bookings(connection, tenant_id, references)
        except Exception:
            logger.warning("loan_receipt_resolve_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            totals["failed"] += 1
            report["tenants"][tenant_id] = totals
            continue
        for booking in bookings:
            totals["bookings"] += 1
            if not booking["booking_open"]:
                totals["closed_skipped"] += 1
                continue
            journey_id = UUID(str(booking["journey_id"]))
            try:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    plan = plan_finance_disbursement(connection, tenant_id=tenant_id, journey_id=journey_id)
                    if commit and not plan["skipped"]:
                        resolve_finance_disbursement(
                            connection, tenant_id=tenant_id, journey_id=journey_id,
                            correlation_id=f"loan-receipt-resolve:{tag}")
            except Exception:
                totals["failed"] += 1
                logger.warning("loan_receipt_resolve_item_failed", tag=tag, tenant_id=tenant_id,
                               journey=booking["journey_reference"], exc_info=True)
                continue
            before = plan.get("before") or {}
            if plan["skipped"]:
                if plan["reason"] == "pc_confirmed":
                    totals["pcConfirmedKept"] += 1
                transition = f"{before.get('confidence') or 'NONE'}->{before.get('confidence') or 'NONE'}"
                after_payment, after_amount, after_conf = before.get("paymentId"), before.get("amount"), \
                    before.get("confidence")
            else:
                after_conf = plan["confidence"]
                after_payment = plan["payment"].get("payment_id")
                after_amount = plan["payment"].get("amount")
                transition = f"{before.get('confidence') or 'NONE'}->{after_conf}"
            changed = _payment_id(before.get("paymentId")) != _payment_id(after_payment)
            if changed:
                totals["linkChanged"] += 1
            if not plan["skipped"] and not plan["resolved"]:
                totals["pcAsked"] += 1  # a PC task is raised, or already open
            totals["transitions"][transition] = totals["transitions"].get(transition, 0) + 1
            item = {
                "reference": booking["journey_reference"],
                "before": {"confidence": before.get("confidence"), "amount": _money(before.get("amount")),
                           "paymentId": _payment_id(before.get("paymentId"))},
                "after": {"confidence": after_conf, "amount": _money(after_amount),
                          "paymentId": _payment_id(after_payment)},
                "linkChanged": changed,
                "reason": plan.get("reason"),
                "basis": plan.get("basis"),
                "stated": plan.get("stated"),
            }
            samples = totals["samples"].setdefault(transition, [])
            if len(samples) < _SAMPLES and (changed or transition.split("->")[0] != transition.split("->")[1]):
                samples.append(item)
            logger.info("loan_receipt_resolve_item", tag=tag, tenant_id=tenant_id, commit=commit, **item)
        totals["samples"] = {k: v for k, v in totals["samples"].items() if v}
        logger.info("loan_receipt_resolve_finished", tag=tag, tenant_id=tenant_id, commit=commit,
                    **{k: v for k, v in totals.items() if k != "samples"})
        report["tenants"][tenant_id] = totals
    return report
