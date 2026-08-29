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


# This registry contains only mappings supported by an existing UC03 design,
# current DI schema, or an explicitly identified provisional source relationship.
# It is deliberately smaller than the 123-field inventory: unknown/provisional
# canonical relationships must remain visible as unmapped rather than being
# inferred from English labels.
ATTRIBUTE_SPECS: tuple[AttributeSpec, ...] = (
    AttributeSpec(
        attribute_key="customer_name",
        excel_field_no=2,
        label="Customer Name",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"customer_name", "pan_name", "aadhaar_name"}),
        # Identity amendment makes PAN/Aadhaar authoritative for Legal Name.
        # Booking-form customer_name remains useful source evidence but cannot
        # silently replace Entered Name or Legal Name.
        source_priority=("pan_card", "pan", "aadhaar", "booking_form", "booking_docket"),
    ),
    AttributeSpec(
        attribute_key="customer_number",
        excel_field_no=3,
        label="Customer Number",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"customer_phone"}),
        source_priority=("booking_form", "booking_docket"),
        operational_field="CUSTOMER_NUMBER",
    ),
    AttributeSpec(
        attribute_key="mail_id",
        excel_field_no=6,
        label="Mail ID",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"customer_email"}),
        source_priority=("booking_form", "booking_docket"),
        mapping_status="PROVISIONAL",
        operational_field="CUSTOMER_EMAIL",
    ),
    AttributeSpec(
        attribute_key="pan",
        excel_field_no=7,
        label="Pan",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"pan_number"}),
        source_priority=("pan_card", "pan"),
    ),
    AttributeSpec(
        attribute_key="sc_name",
        excel_field_no=9,
        label="SC Name",
        stages=("BOOKING",),
        field_keys=frozenset({"sales_person"}),
        source_priority=("booking_form", "booking_docket"),
        mapping_status="PROVISIONAL",
    ),
    AttributeSpec(
        attribute_key="model",
        excel_field_no=20,
        label="Model",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"vehicle_model"}),
        source_priority=("booking_form", "booking_docket", "tax_invoice_dms", "tax_invoice"),
    ),
    AttributeSpec(
        attribute_key="variant",
        excel_field_no=22,
        label="Variant",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"vehicle_variant"}),
        source_priority=("booking_form", "booking_docket", "tax_invoice_dms", "tax_invoice"),
    ),
    AttributeSpec(
        attribute_key="color",
        excel_field_no=23,
        label="Color",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"vehicle_color"}),
        source_priority=("booking_form", "booking_docket", "tax_invoice_dms", "tax_invoice"),
    ),
    AttributeSpec(
        attribute_key="ex_showroom",
        excel_field_no=35,
        label="Ex Showroom",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"ex_showroom_price"}),
        source_priority=("booking_form", "booking_docket", "cost_sheet", "tax_invoice_dms", "tax_invoice"),
        mapping_status="PROVISIONAL",
    ),
    AttributeSpec(
        attribute_key="registration_type_amount",
        excel_field_no=36,
        label="Registration Type (amount)",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"road_tax_registration"}),
        source_priority=("booking_form", "booking_docket"),
        mapping_status="PROVISIONAL",
    ),
    AttributeSpec(
        attribute_key="insurance",
        excel_field_no=44,
        label="Insurance",
        stages=("BOOKING", "DELIVERY"),
        field_keys=frozenset({"insurance_amount"}),
        source_priority=("insurance_cover_note", "insurance_policy", "booking_form", "booking_docket"),
        mapping_status="PROVISIONAL",
    ),
    # Operational Booking concepts already have approved typed-domain owners even
    # though they are not numbered extracted rows in the 123-field inventory.
    AttributeSpec(
        attribute_key="booking_reference",
        excel_field_no=None,
        label="Booking Reference",
        stages=("BOOKING",),
        field_keys=frozenset({"booking_reference_number"}),
        source_priority=("booking_form", "booking_docket"),
        operational_field="BOOKING_REFERENCE",
    ),
    AttributeSpec(
        attribute_key="actual_booking_date",
        excel_field_no=None,
        label="Actual Booking Date",
        stages=("BOOKING",),
        field_keys=frozenset({"booking_date"}),
        source_priority=("booking_form", "booking_docket"),
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
    """Select a deterministic current source without using fuzzy field matching.

    Source precedence wins first. Confidence only breaks ties within the same
    precedence level; document id is the stable final tie-breaker. Null values are
    retained as evidence candidates but never selected over a non-null value.
    """

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
