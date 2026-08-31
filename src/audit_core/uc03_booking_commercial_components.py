"""Extend UC03 Booking Review with detailed Booking Form commercial components.

The existing typed Core owners are reused:
- auditcore.booking_form_review_values keeps the reviewed Booking Form values;
- auditcore.commercial_lines keeps each monetary component by component_key.

There is no generic fallback table. Fields defined here are first-class UC03 fields;
a future populated DI field without an explicit Core owner continues to fail Review
Confirm instead of being silently ignored.
"""
from __future__ import annotations

from audit_core import uc03_attribute_mapping as attribute_mapping
from audit_core import uc03_v2_review_materialization as materialization

_BOOKING_COMMERCIAL_COMPONENT_FIELDS = (
    "sales_discount_amount",
    "buffer_discount_amount",
    "exchange_discount_amount",
    "corporate_discount_amount",
    "loyalty_discount_amount",
    "inhouse_insurance_discount_amount",
    "mr_discount_amount",
    "oem_referral_discount_amount",
    "other_discount_amount",
    "free_accessory_discount_amount",
    "essential_kit_amount",
    "genuine_accessories_amount",
    "non_genuine_accessories_amount",
    "fastag_amount",
    "extended_warranty_amount",
    "green_tax_amount",
    "service_package_amount",
)

_COMPONENT_SPECS = (
    ("booking_sales_discount_amount", "Sales Discount", "sales_discount_amount"),
    ("booking_buffer_discount_amount", "Buffer Discount", "buffer_discount_amount"),
    ("booking_exchange_discount_amount", "Exchange Discount", "exchange_discount_amount"),
    ("booking_corporate_discount_amount", "Corporate Discount", "corporate_discount_amount"),
    ("booking_loyalty_discount_amount", "Loyalty Discount", "loyalty_discount_amount"),
    ("booking_inhouse_insurance_discount_amount", "In-house Insurance Discount", "inhouse_insurance_discount_amount"),
    ("booking_mr_discount_amount", "MR Discount", "mr_discount_amount"),
    ("booking_oem_referral_discount_amount", "OEM Referral Discount", "oem_referral_discount_amount"),
    ("booking_other_discount_amount", "Other Discount", "other_discount_amount"),
    ("booking_free_accessory_discount_amount", "Free Accessory Discount", "free_accessory_discount_amount"),
    ("booking_essential_kit_amount", "Essential Kit", "essential_kit_amount"),
    ("booking_genuine_accessories_amount", "Genuine Accessories", "genuine_accessories_amount"),
    ("booking_non_genuine_accessories_amount", "Non-Genuine Accessories", "non_genuine_accessories_amount"),
    ("booking_fastag_amount", "FASTag", "fastag_amount"),
    ("booking_extended_warranty_amount", "Extended Warranty (EW)", "extended_warranty_amount"),
    ("booking_green_tax_amount", "Green Tax", "green_tax_amount"),
    ("booking_service_package_amount", "Service Package", "service_package_amount"),
)

_installed = False


def install_uc03_booking_commercial_components() -> None:
    """Register every detailed Booking Form commercial field with existing Core owners."""

    global _installed
    if _installed:
        return

    # Materialization and owner lookup read these module globals at request time.
    # Extend the existing typed Booking owner and commercial_lines projection.
    materialization._BOOKING_FORM_FIELDS = tuple(
        dict.fromkeys(
            (*materialization._BOOKING_FORM_FIELDS, *_BOOKING_COMMERCIAL_COMPONENT_FIELDS)
        )
    )
    materialization._BOOKING_DECIMAL_FIELDS = {
        *materialization._BOOKING_DECIMAL_FIELDS,
        *_BOOKING_COMMERCIAL_COMPONENT_FIELDS,
    }
    materialization._COMMERCIAL_LINE_FIELDS = {
        *materialization._COMMERCIAL_LINE_FIELDS,
        *_BOOKING_COMMERCIAL_COMPONENT_FIELDS,
    }

    # Make the fields normal Review attributes rather than anonymous/raw values.
    existing_attribute_keys = {
        spec.attribute_key for spec in attribute_mapping.ATTRIBUTE_SPECS
    }
    additions = tuple(
        attribute_mapping.AttributeSpec(
            attribute_key=attribute_key,
            excel_field_no=None,
            label=label,
            stages=("BOOKING", "DELIVERY"),
            field_keys=frozenset({field_key}),
            source_priority=("booking_form", "booking_docket"),
        )
        for attribute_key, label, field_key in _COMPONENT_SPECS
        if attribute_key not in existing_attribute_keys
    )
    if additions:
        attribute_mapping.ATTRIBUTE_SPECS = (
            *attribute_mapping.ATTRIBUTE_SPECS,
            *additions,
        )
        attribute_mapping._FIELD_INDEX = {
            field_key.casefold(): spec
            for spec in attribute_mapping.ATTRIBUTE_SPECS
            for field_key in spec.field_keys
        }

    _installed = True
