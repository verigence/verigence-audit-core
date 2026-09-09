"""uc03_booking_confirmation_rules.py — data-driven Booking milestones,
independent of the PC's own Submit/Review workflow.

Three checks, all wired into the same per-document async sync pipeline that
already runs SKU resolution and payment reconciliation
(``uc03_confidence_review_policy._sync_booking_document``) -- they fire as
soon as DI confirms the relevant document, not gated behind PC Submit/Review:

1. ``record_booking_form_intimation_and_discount_evidence`` -- on the Booking
   Form's own confirmation: records its printed ``booking_date`` as the
   Booking's Intimation Date, and cross-checks any corporate/exchange/
   scrappage discount the form itself shows against required supporting
   evidence. This is deliberately a cross-check against the *extracted*
   value, not the PC's own applicability declaration (``exchangeTaken`` etc.)
   -- it catches a booking form showing a benefit the PC didn't declare, not
   just an undeclared checklist item.

2. ``evaluate_minimum_booking_payment`` -- on every payment receipt's
   confirmation: walks all durable Booking payments in receipt-date order,
   accumulates the running total, and stamps Booking Confirmed (its own date
   + timestamp) the moment that total reaches the tenant's minimum booking
   amount. Recomputed from scratch every time (durable-state-driven, safe to
   call redundantly) so a backdated or out-of-order receipt is handled
   correctly -- the confirming receipt is whichever one the *chronological*
   running total crosses the threshold on, not whichever arrived last.

Booking Confirmed is tracked as its own columns
(``booking_confirm_date`` / ``booking_confirmed_at_utc``), never written into
``business_status`` -- the PC's own Submit action
(``uc03_simplified_booking_flow.py``) unconditionally overwrites that column
based on document completeness; a second, uncoordinated writer on the same
column would silently stomp one or the other. KYC/other documents can still
be missing (visible separately) while Booking Confirmed stands.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_delivery_commands import _machine_flag
from audit_core.uc03_manual_verification import _resolve_finding
from audit_core.uc03_review_confidence import REVIEW_THRESHOLD_PERCENT

_DEFAULT_MINIMUM_BOOKING_AMOUNT = Decimal(11000)

# rule_key stems, DOCUMENT_GAP-classified via their finding_type alone
# (uc03_finding_routing._DOCUMENT_GAP_TYPES already contains DOCUMENT_EXCEPTION,
# so no change to the shared classification table is needed).
_FINDING_TYPE_DOCUMENT_EXCEPTION = "DOCUMENT_EXCEPTION"
# VIOLATION-classified the same way, via _VIOLATION_TYPES.
_FINDING_TYPE_COMMERCIAL_EXCEPTION = "COMMERCIAL_EXCEPTION"

# discount field on the Booking Form -> the document type that must be on file.
# Scrappage has no registered, classifiable document type in DI yet -- see the
# module docstring for _scrappage_documents_present's own commentary -- so it
# always raises rather than checking presence.
_DISCOUNT_EVIDENCE: dict[str, tuple[str, str]] = {
    "corporate_discount_amount": ("corporate_discount", "corporate_id"),
    "exchange_discount_amount": ("exchange_bonus", "vehicle_rc"),
}
_SCRAPPAGE_FIELD_KEY = "scrappage_discount_amount"


def _confidence_percent(row: dict[str, Any]) -> float | None:
    score = row.get("confidence_score")
    if score is None:
        return None
    value = float(score)
    return value * 100 if str(row.get("confidence_scale") or "") == "UNIT_INTERVAL" else value


def _extracted_amount(row: dict[str, Any] | None) -> Decimal | None:
    if row is None:
        return None
    confidence = _confidence_percent(row)
    if confidence is None or confidence < REVIEW_THRESHOLD_PERCENT:
        return None
    raw = row.get("effective_value")
    if raw is None:
        raw = row.get("extracted_value")
    if raw is None:
        return None
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def _extracted_date(row: dict[str, Any] | None) -> date | None:
    if row is None:
        return None
    raw = row.get("effective_value")
    if raw is None:
        raw = row.get("extracted_value")
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None


def _booking_form_fields(
    connection: Connection, *, tenant_id: str, journey_id: UUID, document_id: UUID
) -> dict[str, dict[str, Any]]:
    field_keys = (*_DISCOUNT_EVIDENCE.keys(), _SCRAPPAGE_FIELD_KEY, "booking_date")
    rows = connection.execute(
        text(
            """
            SELECT field_key, extracted_value, effective_value,
                   confidence_score, confidence_scale
            FROM auditcore.journey_document_extracted_fields
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING' AND di_document_id=:document_id
              AND field_key = ANY(:field_keys)
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": document_id,
            "field_keys": list(field_keys),
        },
    ).mappings().all()
    return {str(row["field_key"]): dict(row) for row in rows}


def _has_active_document(
    connection: Connection, *, tenant_id: str, journey_id: UUID, document_type_key: str
) -> bool:
    return (
        connection.execute(
            text(
                """
                SELECT 1 FROM auditcore.evidence
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND document_type_key=:document_type_key
                  AND association_status='ACTIVE'
                LIMIT 1
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "document_type_key": document_type_key,
            },
        ).scalar_one_or_none()
        is not None
    )


def record_booking_form_intimation_and_discount_evidence(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    correlation_id: str,
) -> None:
    """Runs once the Booking Form itself confirms. Never raises."""

    fields = _booking_form_fields(
        connection, tenant_id=tenant_id, journey_id=journey_id, document_id=document_id
    )

    booking_date = _extracted_date(fields.get("booking_date"))
    if booking_date is not None:
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET intimation_date=:intimation_date,
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "intimation_date": booking_date,
            },
        )

    for field_key, (discount_label, required_document_type) in _DISCOUNT_EVIDENCE.items():
        amount = _extracted_amount(fields.get(field_key))
        rule_key = f"BK_DISCOUNT_EVIDENCE_MISSING:{discount_label}"
        if amount is None:
            _resolve_if_open(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                rule_key=rule_key,
                correlation_id=correlation_id,
            )
            continue
        if _has_active_document(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_type_key=required_document_type,
        ):
            _resolve_if_open(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                rule_key=rule_key,
                correlation_id=correlation_id,
            )
            continue
        _machine_flag(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code="BOOKING",
            rule_key=rule_key,
            finding_type=_FINDING_TYPE_DOCUMENT_EXCEPTION,
            severity="HIGH",
            title=f"{discount_label.replace('_', ' ').title()} evidence is missing",
            description=(
                f"The Booking Form shows a {discount_label.replace('_', ' ')} of "
                f"₹{amount:,.2f}, but no supporting document is on file for this Booking."
            ),
            correlation_id=correlation_id,
            safe_payload={
                "trigger": "BOOKING_FORM_CONFIRMED",
                "discountField": field_key,
                "amount": str(amount),
                "requiredDocumentType": required_document_type,
            },
            blocking_completion=False,
        )

    # Scrappage has no registered, classifiable document type in DI yet, so
    # presence can never be verified -- this always raises when the amount is
    # present and stays open until a PC/TL clears it by hand. Deliberately
    # not self-healing like the two checks above.
    scrappage_amount = _extracted_amount(fields.get(_SCRAPPAGE_FIELD_KEY))
    if scrappage_amount is not None:
        _machine_flag(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code="BOOKING",
            rule_key="BK_SCRAPPAGE_DOCUMENT_UNCLASSIFIED",
            finding_type=_FINDING_TYPE_DOCUMENT_EXCEPTION,
            severity="HIGH",
            title="Scrappage Documents evidence is missing",
            description=(
                f"The Booking Form shows a scrappage discount/bonus of "
                f"₹{scrappage_amount:,.2f}. Scrappage evidence has no dedicated "
                "document type in Verigence yet, so it cannot be verified "
                "automatically -- confirm the scrappage certificate is on file."
            ),
            correlation_id=correlation_id,
            safe_payload={
                "trigger": "BOOKING_FORM_CONFIRMED",
                "discountField": _SCRAPPAGE_FIELD_KEY,
                "amount": str(scrappage_amount),
                "requiredDocumentType": None,
            },
            blocking_completion=False,
        )


def _resolve_if_open(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    rule_key: str,
    correlation_id: str,
) -> None:
    finding_id = connection.execute(
        text(
            """
            SELECT audit_finding_id
            FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND rule_key=:rule_key AND finding_status IN ('OPEN','ACKNOWLEDGED')
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "rule_key": rule_key},
    ).scalar_one_or_none()
    if finding_id is None:
        return
    _resolve_finding(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code="BOOKING",
        finding_id=finding_id,
        actor_id=None,
        correlation_id=correlation_id,
        note="Supporting document is now on file.",
    )


def _minimum_booking_amount(connection: Connection, *, tenant_id: str) -> Decimal:
    value = connection.execute(
        text(
            "SELECT minimum_booking_amount FROM auditcore.tenant_rule_config "
            "WHERE tenant_id=:tenant_id"
        ),
        {"tenant_id": tenant_id},
    ).scalar_one_or_none()
    return Decimal(str(value)) if value is not None else _DEFAULT_MINIMUM_BOOKING_AMOUNT


def evaluate_minimum_booking_payment(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    correlation_id: str,
) -> None:
    """Runs once any Booking payment receipt confirms. Never raises.

    Recomputes from every currently-durable Booking payment every time it
    runs -- safe to call redundantly, and correctly handles a receipt that
    arrives out of chronological order (a backdated receipt inserted earlier
    in the sequence can move the confirming receipt to an earlier date).
    """

    payments = connection.execute(
        text(
            """
            SELECT amount, receipt_date
            FROM auditcore.payments
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND payment_stage='BOOKING'
              AND amount IS NOT NULL AND receipt_date IS NOT NULL
            ORDER BY receipt_date ASC, payment_id ASC
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()

    minimum = _minimum_booking_amount(connection, tenant_id=tenant_id)
    running_total = Decimal(0)
    confirming_date = None
    for payment in payments:
        running_total += Decimal(str(payment["amount"]))
        if confirming_date is None and running_total >= minimum:
            confirming_date = payment["receipt_date"]

    rule_key = "BK_MIN_BOOKING_AMOUNT_NOT_MET"
    if confirming_date is not None:
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET booking_confirm_date=:confirm_date,
                    booking_confirmed_at_utc=COALESCE(booking_confirmed_at_utc, now()),
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "confirm_date": confirming_date,
            },
        )
        _resolve_if_open(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            rule_key=rule_key,
            correlation_id=correlation_id,
        )
        return

    if not payments:
        # Nothing to evaluate yet -- this trigger only fires from a receipt's
        # own confirmation, so an empty set means the payment materializer
        # hasn't caught up in this same pass yet, not a genuine shortfall.
        return

    connection.execute(
        text(
            """
            UPDATE auditcore.journey_stage_states
            SET booking_confirm_date=NULL, booking_confirmed_at_utc=NULL,
                updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    )
    _machine_flag(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code="BOOKING",
        rule_key=rule_key,
        finding_type=_FINDING_TYPE_COMMERCIAL_EXCEPTION,
        severity="HIGH",
        title="Booking without minimum payment",
        description=(
            f"Total Booking payments received (₹{running_total:,.2f}) are below "
            f"the minimum booking amount (₹{minimum:,.2f})."
        ),
        correlation_id=correlation_id,
        safe_payload={
            "trigger": "PAYMENT_RECEIPT_CONFIRMED",
            "totalReceived": str(running_total),
            "minimumBookingAmount": str(minimum),
        },
        blocking_completion=False,
    )
