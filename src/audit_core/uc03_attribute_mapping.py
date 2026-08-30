from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

MappingStatus = Literal["SUPPORTED", "PROVISIONAL"]
ComparisonState = Literal["MATCH", "MISMATCH", "SINGLE_SOURCE", "NOT_AVAILABLE"]


@dataclass(frozen=True)
class AttributeSpec:
    """Explicit UC03 business/Excel attribute mapping shared by V1 and V2.

    The registry is intentionally explicit. A DI field that is not listed here is
    not guessed from a similar-looking label; callers must surface it as UNMAPPED.
    Commercial facts may be SUPPORTED for review/provenance while intentionally
    having no Audit Core value column because DI remains the machine-fact source.
    """

    attribute_key: str
    excel_field_no: int | None
    label: str
    stages: tuple[str, ...]
    field_keys: frozenset[str]
    source_priority: tuple[str, ...]
    mapping_status: MappingStatus = "SUPPORTED"
    operational_field: str | None = None


@dataclass(frozen=True)
class AttributeCandidate:
    field_key: str
    value: Any
    confidence_score: float | None
    document_id: str
    document_type_key: str | None
    document_label: str
    original_filename: str
    content_url: str | None
    page_no: int | None
    evidence_region: dict[str, Any] | None
    evidence_id: str | None = None
    canonical_field_id: str | None = None
    source_fact_version: int | None = None


_B = ("booking_form", "booking_docket")
_ID = ("pan_card", "pan", "aadhaar")
_INVOICE = ("customer_invoice_dms", "tax_invoice_tally", "tax_invoice_dms", "tax_invoice")


# Explicit UC03 mappings. Nothing here relies on fuzzy English-label matching.
ATTRIBUTE_SPECS: tuple[AttributeSpec, ...] = (
    AttributeSpec(
        attribute_key="customer_name",
        excel_field_no=2,
        label="Customer Name",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"customer_name", "pan_name", "aadhaar_name"}),
        # PAN/Aadhaar can establish Legal Name. Booking Form customer_name remains
        # genuine evidence for comparison but cannot overwrite Entered/Legal Name.
        source_priority=("pan_card", "pan", "aadhaar", "booking_form", "booking_docket"),
    ),
    AttributeSpec(
        attribute_key="customer_number",
        excel_field_no=3,
        label="Customer Number",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"customer_phone"}),
        source_priority=_B,
        operational_field="CUSTOMER_NUMBER",
    ),
    AttributeSpec(
        attribute_key="mail_id",
        excel_field_no=6,
        label="Mail ID",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"customer_email"}),
        source_priority=_B,
        operational_field="CUSTOMER_EMAIL",
    ),
    AttributeSpec(
        attribute_key="pan",
        excel_field_no=7,
        label="PAN",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"pan_number"}),
        source_priority=("pan_card", "pan"),
    ),
    AttributeSpec(
        attribute_key="pan_father_name",
        excel_field_no=None,
        label="PAN Father Name",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"pan_father_name"}),
        source_priority=("pan_card", "pan"),
    ),
    AttributeSpec(
        attribute_key="customer_relationship_type",
        excel_field_no=None,
        label="Relationship Type (S/O, W/O, D/O)",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"pan_relationship_type", "aadhaar_relationship_type"}),
        source_priority=_ID,
    ),
    AttributeSpec(
        attribute_key="customer_relationship_name",
        excel_field_no=None,
        label="Relationship Name",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"pan_relationship_name", "aadhaar_relationship_name"}),
        source_priority=_ID,
    ),
    AttributeSpec(
        attribute_key="sc_name",
        excel_field_no=9,
        label="SC Name",
        stages=("BOOKING",),
        field_keys=frozenset({"sales_person"}),
        source_priority=_B,
        mapping_status="PROVISIONAL",
    ),
    AttributeSpec(
        attribute_key="model",
        excel_field_no=20,
        label="Model",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"vehicle_model"}),
        source_priority=_B + _INVOICE,
    ),
    AttributeSpec(
        attribute_key="variant",
        excel_field_no=22,
        label="Variant",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"vehicle_variant"}),
        source_priority=_B + _INVOICE,
    ),
    AttributeSpec(
        attribute_key="color",
        excel_field_no=23,
        label="Color",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"vehicle_color"}),
        source_priority=_B + _INVOICE,
    ),
    AttributeSpec(
        attribute_key="booking_registration_by",
        excel_field_no=None,
        label="Registration By",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"registration_by"}),
        source_priority=_B,
    ),
    AttributeSpec(
        attribute_key="booking_registration_type",
        excel_field_no=None,
        label="Registration Type",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"registration_type"}),
        source_priority=_B,
        operational_field="REGISTRATION_TYPE",
    ),
    AttributeSpec(
        attribute_key="booking_insurance_by",
        excel_field_no=None,
        label="Insurance By",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"insurance_by"}),
        source_priority=_B,
    ),
    AttributeSpec(
        attribute_key="booking_exchange_applicable",
        excel_field_no=None,
        label="Exchange",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"exchange_applicable"}),
        source_priority=_B,
        operational_field="EXCHANGE_TAKEN",
    ),
    AttributeSpec(
        attribute_key="booking_exchange_value",
        excel_field_no=None,
        label="Exchange Value",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"exchange_value"}),
        source_priority=_B,
        operational_field="TRADE_IN_ACTUAL_VALUE",
    ),
    # Commercial values are supported audit facts but stay in DI as machine facts.
    # Audit Core records only the selected source/provenance until a rule/business
    # action requires an approved typed business value.
    AttributeSpec(
        attribute_key="booking_ex_showroom_price",
        excel_field_no=35,
        label="Ex-Showroom Price",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"ex_showroom_price"}),
        source_priority=_B + ("cost_sheet",) + _INVOICE,
    ),
    AttributeSpec(
        attribute_key="booking_tcs_amount",
        excel_field_no=None,
        label="TCS",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"tcs_amount"}),
        source_priority=_B + _INVOICE,
    ),
    AttributeSpec(
        attribute_key="booking_registration_charges",
        excel_field_no=None,
        label="Registration Charges",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"registration_charges"}),
        source_priority=_B,
    ),
    AttributeSpec(
        attribute_key="booking_road_tax_amount",
        excel_field_no=None,
        label="Road Tax",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"road_tax_amount"}),
        source_priority=_B,
    ),
    AttributeSpec(
        attribute_key="booking_road_tax_registration_combined",
        excel_field_no=36,
        label="Road Tax / Registration (combined source value)",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"road_tax_registration"}),
        source_priority=_B,
    ),
    AttributeSpec(
        attribute_key="booking_insurance_amount",
        excel_field_no=44,
        label="Insurance Amount",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"insurance_amount"}),
        source_priority=("insurance_cover_note", "insurance_policy") + _B,
    ),
    AttributeSpec(
        attribute_key="booking_rsa_amount",
        excel_field_no=None,
        label="RSA Amount",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"rsa_amount"}),
        source_priority=_B,
    ),
    AttributeSpec(
        attribute_key="booking_accessories_cost",
        excel_field_no=None,
        label="Accessories",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"accessories_cost"}),
        source_priority=_B,
    ),
    AttributeSpec(
        attribute_key="booking_additional_warranty_amount",
        excel_field_no=None,
        label="Additional Warranty",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"additional_warranty_amount"}),
        source_priority=_B,
    ),
    AttributeSpec(
        attribute_key="booking_other_charges",
        excel_field_no=None,
        label="Other Charges",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"other_charges"}),
        source_priority=_B,
    ),
    AttributeSpec(
        attribute_key="booking_total_price",
        excel_field_no=None,
        label="Total Price",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"total_price"}),
        source_priority=_B + _INVOICE,
    ),
    AttributeSpec(
        attribute_key="booking_discount_amount",
        excel_field_no=None,
        label="Discount",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"discount_amount"}),
        source_priority=_B + _INVOICE,
    ),
    AttributeSpec(
        attribute_key="booking_bonus_amount",
        excel_field_no=None,
        label="Bonus",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"bonus_amount"}),
        source_priority=_B,
    ),
    AttributeSpec(
        attribute_key="booking_net_amount",
        excel_field_no=None,
        label="Net Amount",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"net_amount"}),
        source_priority=_B + _INVOICE,
    ),
    AttributeSpec(
        attribute_key="booking_amount_paid",
        excel_field_no=None,
        label="Booking Amount",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"booking_amount_paid"}),
        source_priority=_B,
    ),
    AttributeSpec(
        attribute_key="booking_balance_amount",
        excel_field_no=None,
        label="Balance Amount",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"balance_amount"}),
        source_priority=_B,
    ),
    AttributeSpec(
        attribute_key="booking_payment_mode",
        excel_field_no=None,
        label="Booking Payment Mode",
        stages=("BOOKING",),
        field_keys=frozenset({"mode_of_payment"}),
        source_priority=_B,
    ),
    AttributeSpec(
        attribute_key="booking_payment_reference",
        excel_field_no=None,
        label="Booking Payment Reference",
        stages=("BOOKING",),
        field_keys=frozenset({"payment_reference_no"}),
        source_priority=_B,
    ),
    AttributeSpec(
        attribute_key="expected_delivery_text",
        excel_field_no=None,
        label="Expected Delivery",
        stages=("BOOKING",),
        field_keys=frozenset({"expected_delivery"}),
        source_priority=_B,
    ),
    AttributeSpec(
        attribute_key="expected_delivery_date",
        excel_field_no=None,
        label="Expected Delivery Date",
        stages=("BOOKING",),
        field_keys=frozenset({"expected_delivery_date"}),
        source_priority=_B,
    ),
    # Operational Booking concepts already have approved typed-domain owners.
    AttributeSpec(
        attribute_key="booking_reference",
        excel_field_no=None,
        label="Booking Reference",
        stages=("BOOKING",),
        field_keys=frozenset({"booking_reference_number"}),
        source_priority=_B,
        operational_field="BOOKING_REFERENCE",
    ),
    AttributeSpec(
        attribute_key="actual_booking_date",
        excel_field_no=None,
        label="Actual Booking Date",
        stages=("BOOKING",),
        field_keys=frozenset({"booking_date"}),
        source_priority=_B,
        operational_field="BOOKING_DATE",
    ),
)

_FIELD_INDEX: dict[str, AttributeSpec] = {
    field_key.casefold(): spec
    for spec in ATTRIBUTE_SPECS
    for field_key in spec.field_keys
}


def spec_for_field(field_key: str) -> AttributeSpec | None:
    return _FIELD_INDEX.get(field_key.strip().casefold())


def specs_for_stage(stage: str) -> tuple[AttributeSpec, ...]:
    normalized = stage.strip().upper()
    return tuple(spec for spec in ATTRIBUTE_SPECS if normalized in spec.stages)


def _source_rank(spec: AttributeSpec, document_type_key: str | None) -> int:
    key = (document_type_key or "").strip().casefold()
    for index, source in enumerate(spec.source_priority):
        if key == source.casefold():
            return index
    return len(spec.source_priority) + 1


def _confidence_rank(candidate: AttributeCandidate) -> float:
    return candidate.confidence_score if candidate.confidence_score is not None else -1.0


def resolve_candidate(spec: AttributeSpec, candidates: list[AttributeCandidate]) -> AttributeCandidate | None:
    """Select a deterministic current source without using fuzzy field matching."""

    usable = [candidate for candidate in candidates if candidate.value is not None]
    if not usable:
        return None
    return min(
        usable,
        key=lambda candidate: (
            _source_rank(spec, candidate.document_type_key),
            -_confidence_rank(candidate),
            candidate.document_id,
            candidate.field_key,
        ),
    )


def _comparable(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return str(value).strip().casefold()


def comparison_state(candidates: list[AttributeCandidate]) -> ComparisonState:
    values = [candidate.value for candidate in candidates if candidate.value is not None]
    if not values:
        return "NOT_AVAILABLE"
    if len(values) == 1:
        return "SINGLE_SOURCE"
    normalized = {_comparable(value) for value in values}
    return "MATCH" if len(normalized) == 1 else "MISMATCH"
