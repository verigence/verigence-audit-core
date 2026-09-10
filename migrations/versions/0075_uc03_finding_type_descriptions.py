"""Add PC/TL-facing descriptions to the 24 finding types 0060 seeded without one.

Revision ID: 0075_uc03_finding_descriptions
Revises: 0074_uc03_document_unrecognized
Create Date: 2026-09-10

0060 built the finding-type registry and seeded the platform's known
vocabulary, but only populated finding_class/owner/mode -- description was
left NULL for every one of those 24 types. Every finding type added *since*
0060 (MANUAL_VERIFICATION, MODEL_NOT_IDENTIFIED, AUTOMATED_SYNC_FAILURE,
DOCUMENT_MISSING, DOCUMENT_UNRECOGNIZED) shipped with a real description from
the start -- this closes the gap for the rest of the catalogue so a PC or TL
looking at any flag has somewhere to understand what it means and what to do
about it, not just the ones built most recently.

Where a finding type's own raise sites already carry rich, specific
per-instance text (the 85-rule Booking/Delivery checkpoint engine and the
external rule-engine's anomaly categories both do -- each instance explains
its own specific violation), the description here documents the category
honestly rather than duplicating instance-level detail it can't know in
advance.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0075_uc03_finding_descriptions"
down_revision = "0074_uc03_document_unrecognized"
branch_labels = None
depends_on = None

_DESCRIPTIONS: dict[str, str] = {
    # ── document gaps (PC self-serve) ────────────────────────────────────────
    "DOCUMENT_EXCEPTION": (
        "A submitted document could not be processed successfully, or a "
        "checkpoint rule found a problem with it. Business progression is "
        "never blocked by this alone -- open the document, resolve the "
        "underlying issue (re-upload, correct, or confirm it's fine)."
    ),
    "DELIVERY_DOCUMENT_MISSING": (
        "Delivery was submitted without a configured mandatory document. "
        "The PC needs to upload it, or confirm with a TL that it's genuinely "
        "not applicable to this deal."
    ),
    "REQUIRED_DOCUMENT_ANSWER_NO": (
        "The PC explicitly answered 'No' for a required document (it "
        "doesn't exist/apply for this deal) rather than leaving it "
        "unanswered. Recorded for TL/PM visibility -- confirm this is "
        "genuinely correct, not a shortcut past a document that does exist."
    ),
    # ── data gaps (PC self-serve) ─────────────────────────────────────────────
    "PAYMENT_EXCEPTION": (
        "A recorded payment doesn't reconcile cleanly against its supporting "
        "evidence (receipt, bank statement) -- an amount, date, or reference "
        "mismatch. The PC needs to check the payment against its evidence "
        "and correct whichever side is wrong."
    ),
    "PAYMENT_UNVERIFIED": (
        "A payment on this journey has no verification event confirming it "
        "against a receipt or bank statement. The PC needs to complete "
        "verification before this can be considered resolved."
    ),
    "DELIVERY_NOT_INTIMATED": (
        "The PC explicitly recorded that the customer was NOT intimated "
        "before vehicle delivery. The description carries the stated reason "
        "-- a TL should review whether this is acceptable for this deal."
    ),
    # ── violations (TL / PM adjudicate) ───────────────────────────────────────
    "VIN_RECONCILIATION_MISMATCH": (
        "Two or more documents on this journey show conflicting full "
        "vehicle identifiers (VIN/chassis) that should match. A TL/PM needs "
        "to determine which source is correct and how the conflict arose."
    ),
    "DELIVERY_COMPLETED_WITH_AUDIT_INCOMPLETE": (
        "The vehicle was physically delivered and recorded as such while "
        "configured audit work (documents, payments, or other checks) was "
        "still outstanding. Delivery itself is never blocked by this -- a "
        "TL/PM needs to review and close the outstanding audit work."
    ),
    "BOOKING_PREREQUISITES_INCOMPLETE_AT_DELIVERY": (
        "Delivery was started while the Booking stage still had incomplete "
        "prerequisites. Delivery progression is never blocked by this -- a "
        "TL/PM needs to review what was left incomplete on the Booking side."
    ),
    "DUPLICATE_BOOKING": (
        "This Booking was marked as a duplicate of another booking for the "
        "same customer/vehicle. A TL/PM needs to confirm which booking is "
        "the real one and how the duplicate should be resolved."
    ),
    "COMMERCIAL_EXCEPTION": (
        "A checkpoint rule found a commercial-value discrepancy (pricing, "
        "discount, or a component of the deal's commercial terms) that "
        "needs a TL/PM's judgment call, not a PC self-fix."
    ),
    "PROCESS_NON_COMPLIANCE": (
        "A checkpoint rule found the journey didn't follow an expected "
        "process step or sequencing rule (a date/timing rule, an approval "
        "step, or similar). A TL/PM needs to review what happened and why."
    ),
    "CUSTOMER_IDENTITY_CONCERN": (
        "A checkpoint rule or the external rule engine's KYC checks found "
        "something inconsistent about the customer's identity evidence "
        "across documents. A TL/PM needs to review the identity documents "
        "directly."
    ),
    "PHYSICAL_OBSERVATION": (
        "A PC recorded a physical observation about the vehicle or delivery "
        "that needs a TL/PM's attention -- see the finding's own description "
        "for what was observed."
    ),
    "DELIVERY_EXCEPTION": (
        "A checkpoint rule found a Delivery-stage problem that doesn't fit "
        "a more specific category. A TL/PM needs to review the finding's "
        "own description for the specific issue."
    ),
    "OTHER": (
        "A checkpoint rule or manual flag didn't fit any of the platform's "
        "more specific categories. See the finding's own title/description "
        "for what was actually found."
    ),
    # ── external rule-engine anomaly categories (each instance already
    #    carries the specific rule-engine anomaly text; these describe the
    #    category, not any one instance) ────────────────────────────────────
    "PRICING_ANOMALY": (
        "The external rule engine found a pricing discrepancy against the "
        "OEM/dealer price master for this deal. A TL/PM needs to review the "
        "specific comparison in the finding's own description."
    ),
    "DISCOUNT_ANOMALY": (
        "The external rule engine found a discount applied outside the "
        "approved discount/scheme master for this deal. A TL/PM needs to "
        "review the specific comparison in the finding's own description."
    ),
    "ACCESSORY_ANOMALY": (
        "The external rule engine found an accessory pricing or fitment "
        "discrepancy for this deal. A TL/PM needs to review the specific "
        "comparison in the finding's own description."
    ),
    "INSURANCE_ANOMALY": (
        "The external rule engine found an insurance-value or coverage "
        "discrepancy for this deal. A TL/PM needs to review the specific "
        "comparison in the finding's own description."
    ),
    "RTO_ANOMALY": (
        "The external rule engine found an RTO (registration) fee or "
        "process discrepancy for this deal. A TL/PM needs to review the "
        "specific comparison in the finding's own description."
    ),
    "VEHICLE_IDENTITY_ANOMALY": (
        "The external rule engine found a vehicle-identity discrepancy "
        "(VIN/chassis/engine number) across this deal's documents. A TL/PM "
        "needs to review the specific comparison in the finding's own "
        "description."
    ),
    "CROSS_CASE_DUPLICATE": (
        "The external rule engine found this deal shares an identifier "
        "(vehicle, customer document, or similar) with another case in a "
        "way that suggests duplication. A TL/PM needs to review both cases."
    ),
    "RULE_ENGINE_ANOMALY": (
        "The external rule engine found an anomaly that didn't fit one of "
        "its more specific categories. See the finding's own description "
        "for the specific comparison."
    ),
}


def upgrade() -> None:
    conn = op.get_bind()
    for code, description in _DESCRIPTIONS.items():
        conn.execute(
            text(
                """
                UPDATE auditcore.finding_types
                SET description = :description, updated_at_utc = now()
                WHERE finding_type_code = :code
                """
            ),
            {"code": code, "description": description},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for code in _DESCRIPTIONS:
        conn.execute(
            text(
                """
                UPDATE auditcore.finding_types
                SET description = NULL, updated_at_utc = now()
                WHERE finding_type_code = :code
                """
            ),
            {"code": code},
        )
