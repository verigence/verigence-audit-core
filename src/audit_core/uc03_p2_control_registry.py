"""Phase 2 control registry.

Audit Core P2 owns control identity/state. Native Audit Core checks and the
external Rule Engine remain executor adapters only. This registry represents
the verified 25 native + 47 enabled external controls at the Phase 2 baseline.

The registry is deliberately metadata-only: creating WAITING_FOR_FACTS state
never executes a rule. Existing execution paths continue to produce outcomes
that the P2 control ledger records.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Connection, text

Executor = Literal["NATIVE", "RULE_ENGINE"]


@dataclass(frozen=True)
class P2ControlDefinition:
    code: str
    executor: Executor
    phases: tuple[str, ...]
    left_operand: str | None = None
    right_operand: str | None = None


def _native(code: str, *phases: str) -> P2ControlDefinition:
    return P2ControlDefinition(code=code, executor="NATIVE", phases=tuple(phases))


def _external(
    code: str,
    phases: tuple[str, ...],
    left: str | None = None,
    right: str | None = None,
) -> P2ControlDefinition:
    return P2ControlDefinition(
        code=code,
        executor="RULE_ENGINE",
        phases=phases,
        left_operand=left,
        right_operand=right,
    )


# Phase membership is intentionally conservative. Rules triggered by generic
# DOCUMENT_SYNCED events and valid in both journey halves are listed in both.
P2_NATIVE_CONTROLS: dict[str, P2ControlDefinition] = {
    item.code: item
    for item in (
        _native("WRONG_DOCUMENT", "BOOKING", "DELIVERY"),
        _native("DUPLICATE_RECEIPT", "BOOKING", "DELIVERY"),
        _native("MANUAL_VERIFICATION", "BOOKING", "DELIVERY"),
        _native("MODEL_NOT_IDENTIFIED", "BOOKING", "DELIVERY"),
        _native("PAYMENT_BANK_UNMATCHED", "BOOKING", "DELIVERY"),
        _native("BK_DISCOUNT_EVIDENCE_MISSING", "BOOKING", "DELIVERY"),
        _native("BK_MIN_BOOKING_AMOUNT_NOT_MET", "BOOKING"),
        _native("WF_BOOKING_INCOMPLETE_AT_DELIVERY_START", "DELIVERY"),
        _native("DL_VIN_RECONCILIATION", "DELIVERY"),
        _native("DL_NOT_INTIMATED", "DELIVERY"),
        _native("DOC_REQUIRED_ANSWER_NO", "DELIVERY"),
        _native("WF_DELIVERY_COMPLETED_WITH_AUDIT_INCOMPLETE", "DELIVERY"),
        _native("DOCUMENT_UNRECOGNIZED", "BOOKING", "DELIVERY"),
        _native("DOCUMENT_MISSING", "BOOKING", "DELIVERY"),
        _native("AUTOMATED_SYNC_FAILURE", "BOOKING", "DELIVERY"),
        _native("BK_DOCKET_PRESENT", "BOOKING"),
        _native("BK_PAN_PRESENT", "BOOKING"),
        _native("BK_MIN_BOOKING_PROOF_PRESENT", "BOOKING"),
        _native("BK_CONDITIONAL_DOCS_ADDRESSED", "BOOKING"),
        _native("BK_REQUIRED_CAPTURE_COMPLETE", "BOOKING"),
        _native("DL_V2_REQUIRED_DOCUMENT_MISSING", "DELIVERY"),
        _native("DL_V2_DOCUMENT_PROCESSING_FAILED", "DELIVERY"),
        _native("FINANCE_HYPOTHECATION_MISSING", "DELIVERY", "FINANCE"),
        _native("UC03_DI_LOW_CONFIDENCE_POST_SUBMIT", "BOOKING"),
        _native("DUPLICATE_BOOKING", "BOOKING"),
    )
}


P2_EXTERNAL_CONTROLS: dict[str, P2ControlDefinition] = {
    item.code: item
    for item in (
        _external("ACCESSORY_BOOKING_VS_INVOICE", ("BOOKING", "DELIVERY"), "booking_form.accessories_cost", "accessory_invoice_dms.grand_total_amount"),
        _external("ACCESSORY_DATE_VS_DELIVERY", ("DELIVERY",), "accessory_invoice_dms.invoice_date", "gate_pass.delivery_date"),
        _external("ACCESSORY_DMS_VS_TALLY", ("DELIVERY",), "accessory_invoice_dms.grand_total_amount", "accessory_invoice_tally.grand_total_amount"),
        _external("ACCESSORY_VIN_VS_VEHICLE_VIN", ("DELIVERY",), "accessory_invoice_dms.chassis_number", "customer_invoice_dms.chassis_number"),
        _external("ACCESSORY_VS_COST_SHEET", ("DELIVERY",), "accessory_invoice_dms.grand_total_amount", "_reconciliation.addon:ACCESSORIES_TOTAL:standard"),
        _external("BOOKING_AMOUNT_ZERO", ("BOOKING",), "booking_docket.booking_amount_paid"),
        _external("BOOKING_RECEIPT_SUM_VS_BOOKING_FORM", ("BOOKING",), "dealer_receipt.amount_paid", "booking_form.booking_amount_paid"),
        _external("BOOKING_TO_DELIVERY_EXCESS", ("BOOKING", "DELIVERY"), "gate_pass.delivery_date", "booking_form.booking_date"),
        _external("CHASSIS_INVOICE_VS_DO", ("DELIVERY",), "customer_invoice_dms.chassis_number", "delivery_order_cover.chassis_number"),
        _external("CORPORATE_GSTIN_CERT_VS_INVOICE", ("DELIVERY", "CORPORATE"), "gst_certificate.gstin", "customer_invoice_dms.buyer_gstin"),
        _external("CORPORATE_NAME_CERT_VS_INVOICE", ("BOOKING", "DELIVERY", "CORPORATE"), "gst_certificate.legal_name", "customer_invoice_dms.buyer_name"),
        _external("CORPORATE_PO_AMOUNT_VS_INVOICE", ("DELIVERY", "CORPORATE"), "purchase_order.po_amount", "customer_invoice_dms.grand_total_amount"),
        _external("CORPORATE_PO_MISSING", ("BOOKING", "CORPORATE")),
        _external("CUSTOMER_LEDGER_MISSING", ("DELIVERY", "FINANCE")),
        _external("CUSTOMER_NAME_BOOKING_VS_INVOICE", ("BOOKING", "DELIVERY"), "booking_form.customer_name", "customer_invoice_dms.buyer_name"),
        _external("DEBIT_NOTE_INSURANCE_VS_COVER_NOTE", ("DELIVERY",), "debit_note.insurance_amount", "insurance_cover.premium_amount"),
        _external("DELIVERY_ORDER_MISSING", ("DELIVERY",)),
        _external("DELIVERY_RECEIPT_SUM_VS_INVOICE", ("DELIVERY", "FINANCE"), "payment_receipt.amount_paid", "customer_invoice_dms.grand_total_amount"),
        _external("DISCOUNT_BOOKING_EXCEEDS_APPROVAL", ("BOOKING",), "booking_form.discount_amount", "_reconciliation.discount:TOTAL:standard"),
        _external("DISCOUNT_EXCEEDS_POLICY", ("BOOKING",), "discount_approval_form.approved_discount"),
        _external("DISCOUNT_HIDDEN_IN_EXCHANGE", ("BOOKING", "EXCHANGE"), "customer_invoice_dms.invoice_discount_amount", "_reconciliation.discount:TOTAL:standard"),
        _external("DISCOUNT_INVOICE_DMS_VS_TALLY", ("DELIVERY",), "customer_invoice_dms.invoice_discount_amount", "tax_invoice_tally.invoice_discount_amount"),
        _external("DISCOUNT_INVOICE_VS_BOOKING", ("BOOKING", "DELIVERY"), "customer_invoice_dms.invoice_discount_amount", "booking_form.discount_amount"),
        _external("DO_DATE_AFTER_GATE", ("DELIVERY",), "delivery_order_cover.delivery_date", "gate_pass.delivery_date"),
        _external("EXCHANGE_VALUE_VS_COST_SHEET", ("DELIVERY", "EXCHANGE"), "valuation_report.final_offer_value", "_reconciliation.discount:EXCHANGE:standard"),
        _external("EXCHANGE_VALUE_VS_INVOICE_CREDIT", ("DELIVERY", "EXCHANGE"), "valuation_report.final_offer_value", "_reconciliation.discount:EXCHANGE:actual"),
        _external("GATE_DATE_BEFORE_INVOICE", ("DELIVERY",), "gate_pass.delivery_date", "customer_invoice_dms.invoice_date"),
        _external("GATE_PASS_CHASSIS_EMPTY", ("DELIVERY",), "gate_pass.chassis_number"),
        _external("GATE_PASS_MISSING", ("DELIVERY",)),
        _external("INSURANCE_COVER_NOTE_MISSING", ("DELIVERY",)),
        _external("INSURANCE_PREMIUM_COVER_VS_COST_SHEET", ("DELIVERY",), "insurance_cover.premium_amount", "_reconciliation.commercial:insurance_amount:standard"),
        _external("INSURANCE_PREMIUM_VS_DEBIT_NOTE", ("DELIVERY",), "insurance_cover.premium_amount", "debit_note.insurance_amount"),
        _external("INSURANCE_START_AFTER_DELIVERY", ("DELIVERY",), "insurance_cover.policy_start_date", "gate_pass.delivery_date"),
        _external("INVOICE_DATE_BEFORE_BOOKING", ("BOOKING",), "customer_invoice_dms.invoice_date", "booking_form.booking_date"),
        _external("INVOICE_MISSING", ("DELIVERY",)),
        _external("KYC_DOB_AADHAAR_VS_PAN", ("BOOKING",), "aadhaar.date_of_birth", "pan_card.date_of_birth"),
        _external("KYC_NAME_VS_BOOKING", ("BOOKING",), "aadhaar.aadhaar_name", "booking_form.customer_name"),
        _external("KYC_NAME_VS_INVOICE", ("DELIVERY",), "aadhaar.aadhaar_name", "customer_invoice_dms.buyer_name"),
        _external("MODEL_VARIANT_BOOKING_VS_INVOICE", ("BOOKING", "DELIVERY"), "booking_form.vehicle_variant", "customer_invoice_dms.variant_raw"),
        _external("PAYMENT_MODE_CASH_UNRECEIPTED", ("BOOKING", "DELIVERY"), "customer_ledger.cash_credit_total", "_derived.payments_total"),
        _external("PAYMENT_SUM_VS_LEDGER", ("DELIVERY", "FINANCE"), "_derived.payments_total", "customer_ledger.total_credited"),
        _external("PRICE_BOOKING_VS_INVOICE", ("BOOKING",), "booking_form.total_price", "customer_invoice_dms.grand_total_amount"),
        _external("PRICE_INVOICE_DMS_VS_TALLY", ("DELIVERY",), "customer_invoice_dms.grand_total_amount", "tax_invoice_tally.grand_total_amount"),
        _external("PRICE_INVOICE_VS_LEDGER", ("DELIVERY",), "customer_invoice_dms.grand_total_amount", "customer_ledger.total_debited"),
        _external("RTO_CHALLAN_MISSING", ("DELIVERY",)),
        _external("TALLY_ACCESSORY_INVOICE_VS_DMS", ("DELIVERY",), "accessory_invoice_tally.grand_total_amount", "accessory_invoice_dms.grand_total_amount"),
        _external("TALLY_VEHICLE_INVOICE_VS_DMS", ("DELIVERY",), "tax_invoice_tally.grand_total_amount", "customer_invoice_dms.grand_total_amount"),
    )
}

P2_CONTROL_REGISTRY: dict[str, P2ControlDefinition] = {
    **P2_NATIVE_CONTROLS,
    **P2_EXTERNAL_CONTROLS,
}


def ensure_p2_control_state_rows(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id,
) -> None:
    """Create explicit WAITING rows for every governed control.

    Existing outcomes are never overwritten. This closes the ambiguity where
    an absent row could otherwise be mistaken for a clean control.
    """
    for definition in P2_CONTROL_REGISTRY.values():
        connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_control_state (
                    tenant_id, journey_id, control_code, executor_type,
                    control_status
                ) VALUES (
                    :tenant_id, :journey_id, :control_code, :executor_type,
                    'WAITING_FOR_FACTS'
                )
                ON CONFLICT (tenant_id, journey_id, control_code) DO NOTHING
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "control_code": definition.code,
                "executor_type": definition.executor,
            },
        )


def control_codes_for_stage(stage: str) -> frozenset[str]:
    normalized = stage.strip().upper()
    if normalized == "BOOKING":
        relevant = {"BOOKING"}
    else:
        relevant = {"DELIVERY", "FINANCE", "CORPORATE", "EXCHANGE"}
    return frozenset(
        code
        for code, definition in P2_CONTROL_REGISTRY.items()
        if relevant.intersection(definition.phases)
    )
