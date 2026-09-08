from __future__ import annotations

import audit_core.uc03_masters_alignment as align


def test_price_component_maps_to_booking_field() -> None:
    assert align.commercial_key_for_price_component("EX_SHOWROOM") == "ex_showroom_price"
    assert align.commercial_key_for_price_component("INSURANCE") == "insurance_amount"
    assert align.commercial_key_for_price_component("TCS") == "tcs_amount"
    assert align.commercial_key_for_price_component("RSA_1YR") == "rsa_amount"
    assert align.commercial_key_for_price_component("ACCESSORIES_KIT") == "accessories_cost"
    assert align.commercial_key_for_price_component("FASTAG") == "fastag_amount"


def test_both_warranty_tiers_fold_onto_one_line() -> None:
    assert align.commercial_key_for_price_component("EXT_WARRANTY_4TH_YR") == "additional_warranty_amount"
    assert (
        align.commercial_key_for_price_component("EXT_WARRANTY_4TH_5TH_YR")
        == "additional_warranty_amount"
    )
    assert align.commercial_amounts_are_additive("additional_warranty_amount") is True
    assert align.commercial_amounts_are_additive("ex_showroom_price") is False


def test_registration_variant_selected_by_basis() -> None:
    assert (
        align.commercial_key_for_price_component("REGISTRATION_INDIVIDUAL", basis="INDIVIDUAL")
        == "registration_charges"
    )
    # the non-applicable variant returns None so it is not double-counted
    assert align.commercial_key_for_price_component("REGISTRATION_CORPORATE", basis="INDIVIDUAL") is None
    assert (
        align.commercial_key_for_price_component("REGISTRATION_CORPORATE", basis="CORPORATE")
        == "registration_charges"
    )
    assert align.commercial_key_for_price_component("REGISTRATION_INDIVIDUAL", basis="CORPORATE") is None


def test_registration_basis_defaults_individual() -> None:
    assert align.registration_basis(customer_type_code=None, registration_type_code=None) == "INDIVIDUAL"
    assert (
        align.registration_basis(customer_type_code="INDIVIDUAL", registration_type_code="PVT")
        == "INDIVIDUAL"
    )
    assert (
        align.registration_basis(customer_type_code="CORPORATE", registration_type_code=None)
        == "CORPORATE"
    )
    assert (
        align.registration_basis(customer_type_code="Company", registration_type_code=None)
        == "CORPORATE"
    )
    # vehicle use (commercial goods carrier) does not force the corporate rate
    assert (
        align.registration_basis(customer_type_code=None, registration_type_code="commercial")
        == "INDIVIDUAL"
    )


def test_unknown_price_component_kept_as_is() -> None:
    assert align.commercial_key_for_price_component("SOME_NEW_OEM_COMPONENT") is None


def test_canonical_discount_key_normalises_legacy_and_oem() -> None:
    assert align.canonical_discount_key("EXCHANGE") == "EXCHANGE_BONUS"
    assert align.canonical_discount_key("CORPORATE") == "CORPORATE_PRIVILEGE"
    assert align.canonical_discount_key("LOYALTY") == "WELCOME_BONUS"
    assert align.canonical_discount_key("CASH_DISCOUNT") == "CASH_DISCOUNT"
    assert align.canonical_discount_key("cash_discount") == "CASH_DISCOUNT"
    # unknown stays as-is, upper-cased
    assert align.canonical_discount_key("brand_new_scheme") == "BRAND_NEW_SCHEME"


def test_discount_actual_field_mapping() -> None:
    m = align.DISCOUNT_ACTUAL_FIELD_TO_CANONICAL_KEY
    assert m["consumer_offer_amount"] == "CASH_DISCOUNT"
    assert m["corporate_offer_amount"] == "CORPORATE_PRIVILEGE"
    assert m["exchange_claim_amount"] == "EXCHANGE_BONUS"
    assert m["cash_discount_amount"] == "ADDITIONAL_DISCOUNT"
    for canonical in m.values():
        assert canonical in align.CANONICAL_DISCOUNT_KEYS


def test_scheme_category_discount_keys_are_canonical() -> None:
    for keys in align.SCHEME_CATEGORY_DISCOUNT_KEYS.values():
        assert keys <= align.CANONICAL_DISCOUNT_KEYS
