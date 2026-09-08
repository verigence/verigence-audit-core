"""uc03_masters_alignment.py — one deterministic map between the OEM native
masters vocabulary and the Audit Core commercial / discount vocabulary.

The OEM native price / discount masters (ingested by ``oem_price_masters`` from
Mahindra's own documents, migration 0061) use their own component and benefit
keys:

  price_list_items.component_key      EX_SHOWROOM, TCS, INSURANCE, RSA_1YR, ...
  discount_scheme_benefits.benefit_key CASH_DISCOUNT, EXCHANGE_BONUS, ...

Audit Core's per-journey reconciliation tables use the booking-form field
vocabulary instead:

  commercial_lines.component_key      ex_showroom_price, insurance_amount, ...
  discount_applications.discount_key  (free text; historically SALES/BUFFER/...)

Nothing here is fuzzy: every entry is an explicit, reviewed 1:1 (or n:1) mapping.
Where a masters component has no Audit Core counterpart the value is still
carried on its own line (``keep as is``), never dropped and never merged into a
different component.

Used by:
  * ``uc03_model_resolution``       — resolve the SKU, then materialise standards
  * ``uc03_deal_reconciliation``    — fill commercial_lines.standard_amount /
                                       discount_applications.standard_eligible_amount
  * ``uc03_journey_overview_projection._sku_pricing_panel`` — master-vs-booking panel
"""
from __future__ import annotations

from typing import Literal

# ── price components ─────────────────────────────────────────────────────────
# OEM price_list_items.component_key  ->  Audit Core commercial_lines.component_key
# (which is the reviewed booking-form field key).  Two OEM warranty tiers and the
# two registration variants deliberately fold onto one Audit Core line each.
PRICE_COMPONENT_TO_COMMERCIAL_KEY: dict[str, str] = {
    "EX_SHOWROOM":             "ex_showroom_price",
    "TCS":                     "tcs_amount",
    "INSURANCE":               "insurance_amount",
    "EXT_WARRANTY_4TH_YR":     "additional_warranty_amount",
    "EXT_WARRANTY_4TH_5TH_YR": "additional_warranty_amount",
    "ACCESSORIES_KIT":         "accessories_cost",
    "RSA_1YR":                 "rsa_amount",
    "FASTAG":                  "fastag_amount",
    "REGISTRATION_INDIVIDUAL": "registration_charges",
    "REGISTRATION_CORPORATE":  "registration_charges",
}

# OEM registration components that must be picked apart by the buyer's
# registration basis before mapping.  Exactly one applies to any journey.
_REGISTRATION_COMPONENTS = ("REGISTRATION_INDIVIDUAL", "REGISTRATION_CORPORATE")

RegistrationBasis = Literal["INDIVIDUAL", "CORPORATE"]


def registration_component_for(basis: RegistrationBasis) -> str:
    """Return the single OEM registration component_key that applies."""
    return "REGISTRATION_CORPORATE" if basis == "CORPORATE" else "REGISTRATION_INDIVIDUAL"


# Buyer types that take the OEM's corporate registration rate. This is the
# *buyer* axis (individual vs company), not vehicle use — a commercial vehicle
# bought by an individual still uses the individual rate.
_CORPORATE_BUYER_TOKENS = frozenset(
    {"CORPORATE", "COMPANY", "INSTITUTIONAL", "PARTNERSHIP", "LLP", "TRUST", "SOCIETY"}
)


def registration_basis(
    *, customer_type_code: str | None, registration_type_code: str | None
) -> RegistrationBasis:
    """Decide whether the OEM corporate or individual registration rate applies.

    Corporate when the reviewed customer type is a company/institution, or the
    registration type is explicitly corporate; individual otherwise (the common
    case).
    """
    for value in (customer_type_code, registration_type_code):
        token = (value or "").strip().upper()
        if token in _CORPORATE_BUYER_TOKENS:
            return "CORPORATE"
    return "INDIVIDUAL"


def commercial_key_for_price_component(
    component_key: str, *, basis: RegistrationBasis | None = None
) -> str | None:
    """OEM price component_key -> Audit Core commercial_lines.component_key.

    Returns None for a component that has no Audit Core counterpart *and* whose
    raw key should be carried as-is (the caller keeps it on its own line under
    the upper-cased OEM key).  Registration components only map when they match
    ``basis`` — the other variant returns None so it is not double-counted.
    """
    upper = component_key.strip().upper()
    if upper in _REGISTRATION_COMPONENTS:
        if basis is None or upper == registration_component_for(basis):
            return "registration_charges"
        return None
    return PRICE_COMPONENT_TO_COMMERCIAL_KEY.get(upper)


def commercial_amounts_are_additive(commercial_key: str) -> bool:
    """True when several OEM components legitimately sum onto one Audit Core line
    (only the two extended-warranty tiers today)."""
    return commercial_key == "additional_warranty_amount"


# ── discount benefits ────────────────────────────────────────────────────────
# The canonical discount key IS the OEM benefit_key — discount_applications.
# discount_key is free varchar, so we keep the OEM vocabulary rather than force
# it into the legacy SALES/BUFFER/... set.
CANONICAL_DISCOUNT_KEYS: frozenset[str] = frozenset(
    {
        "CASH_DISCOUNT",
        "EXCHANGE_BONUS",
        "SCRAPPAGE_BONUS_DEALER",
        "SCRAPPAGE_BONUS_COD",
        "WELCOME_BONUS",
        "CORPORATE_PRIVILEGE",
        "ACCESSORIES_KIT",
        "EXT_WARRANTY_4TH_YR",
        "EXT_WARRANTY_4TH_5TH_YR",
        "INSURANCE",
        "OTHER_SCHEME",
        # discretionary dealer discount with no scheme entitlement (over-grant)
        "ADDITIONAL_DISCOUNT",
    }
)

# reviewed *actual* discount field (from an invoice / accounts statement /
# booking form) -> canonical discount key.  Invoice/accounts-statement fields
# are preferred; booking-form fields are the fallback (see the invoice-first
# source_priority in uc03_attribute_mapping).
DISCOUNT_ACTUAL_FIELD_TO_CANONICAL_KEY: dict[str, str] = {
    # dealer accounts statement
    "consumer_offer_amount":  "CASH_DISCOUNT",
    "corporate_offer_amount": "CORPORATE_PRIVILEGE",
    "exchange_claim_amount":  "EXCHANGE_BONUS",
    "cash_discount_amount":   "ADDITIONAL_DISCOUNT",
    # vehicle tax / retail invoice
    "oem_discount_amount":    "CASH_DISCOUNT",
    # booking form (fallback)
    "discount_amount":        "CASH_DISCOUNT",
    "bonus_amount":           "EXCHANGE_BONUS",
    "exchange_discount_amount": "EXCHANGE_BONUS",
    "corporate_discount_amount": "CORPORATE_PRIVILEGE",
    "loyalty_discount_amount": "WELCOME_BONUS",
}

# legacy discount_applications.discount_key (materialised by the older
# _DISCOUNT_KEY_BY_FIELD map) -> canonical, so an existing row is reconciled
# against the right scheme benefit.
LEGACY_DISCOUNT_KEY_TO_CANONICAL: dict[str, str] = {
    "TOTAL":             "CASH_DISCOUNT",
    "SALES":             "CASH_DISCOUNT",
    "BUFFER":            "ADDITIONAL_DISCOUNT",
    "EXCHANGE":          "EXCHANGE_BONUS",
    "CORPORATE":         "CORPORATE_PRIVILEGE",
    "LOYALTY":           "WELCOME_BONUS",
    "INHOUSE_INSURANCE": "INSURANCE",
    "FREE_ACCESSORY":    "ACCESSORIES_KIT",
    "OTHER":             "OTHER_SCHEME",
    "MR":               "OTHER_SCHEME",
    "OEM_REFERRAL":      "OTHER_SCHEME",
}

# reviewed *actual* discount field -> the OEM benefit_key it corresponds to.
# The full set of booking-form discount fields that the DI schema extracts.
DISCOUNT_ACTUAL_FIELD_TO_BENEFIT_KEY: dict[str, str] = {
    "discount_amount":                  "CASH_DISCOUNT",
    "sales_discount_amount":            "CASH_DISCOUNT",
    "consumer_offer_amount":            "CASH_DISCOUNT",
    "oem_discount_amount":              "CASH_DISCOUNT",
    "exchange_discount_amount":         "EXCHANGE_BONUS",
    "exchange_claim_amount":            "EXCHANGE_BONUS",
    "bonus_amount":                     "EXCHANGE_BONUS",
    "corporate_discount_amount":        "CORPORATE_PRIVILEGE",
    "corporate_offer_amount":           "CORPORATE_PRIVILEGE",
    "loyalty_discount_amount":          "WELCOME_BONUS",
    "inhouse_insurance_discount_amount": "INSURANCE",
    "free_accessory_discount_amount":   "ACCESSORIES_KIT",
    "buffer_discount_amount":           "ADDITIONAL_DISCOUNT",
    "cash_discount_amount":             "ADDITIONAL_DISCOUNT",
    "mr_discount_amount":               "OTHER_SCHEME",
    "oem_referral_discount_amount":     "OTHER_SCHEME",
    "other_discount_amount":            "OTHER_SCHEME",
}

# OEM scheme_category -> the canonical discount keys it can grant.  Used to pick
# which applicable scheme's benefit backs a given canonical discount key.
SCHEME_CATEGORY_DISCOUNT_KEYS: dict[str, frozenset[str]] = {
    "CONSUMER":  frozenset({"CASH_DISCOUNT", "ACCESSORIES_KIT", "EXT_WARRANTY_4TH_YR",
                            "EXT_WARRANTY_4TH_5TH_YR", "INSURANCE", "OTHER_SCHEME"}),
    "EXCHANGE":  frozenset({"EXCHANGE_BONUS"}),
    "SCRAPPAGE": frozenset({"SCRAPPAGE_BONUS_DEALER", "SCRAPPAGE_BONUS_COD"}),
    "WELCOME":   frozenset({"WELCOME_BONUS"}),
    "CORPORATE": frozenset({"CORPORATE_PRIVILEGE"}),
}


def canonical_discount_key(raw_key: str) -> str:
    """Normalise any discount key (OEM benefit, legacy, or already-canonical)."""
    upper = raw_key.strip().upper()
    if upper in CANONICAL_DISCOUNT_KEYS:
        return upper
    return LEGACY_DISCOUNT_KEY_TO_CANONICAL.get(upper, upper)


__all__ = [
    "CANONICAL_DISCOUNT_KEYS",
    "DISCOUNT_ACTUAL_FIELD_TO_BENEFIT_KEY",
    "DISCOUNT_ACTUAL_FIELD_TO_CANONICAL_KEY",
    "LEGACY_DISCOUNT_KEY_TO_CANONICAL",
    "PRICE_COMPONENT_TO_COMMERCIAL_KEY",
    "SCHEME_CATEGORY_DISCOUNT_KEYS",
    "RegistrationBasis",
    "canonical_discount_key",
    "commercial_amounts_are_additive",
    "commercial_key_for_price_component",
    "registration_basis",
    "registration_component_for",
]
