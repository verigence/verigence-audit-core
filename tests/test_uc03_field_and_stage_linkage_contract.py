from pathlib import Path

from audit_core.uc03_attribute_mapping import spec_for_field


def test_completed_booking_fields_have_explicit_non_fuzzy_mappings() -> None:
    expected = {
        "customer_email": ("mail_id", "CUSTOMER_EMAIL"),
        "registration_by": ("booking_registration_by", None),
        "registration_type": ("booking_registration_type", "REGISTRATION_TYPE"),
        "insurance_by": ("booking_insurance_by", None),
        "exchange_applicable": ("booking_exchange_applicable", "EXCHANGE_TAKEN"),
        "exchange_value": ("booking_exchange_value", "TRADE_IN_ACTUAL_VALUE"),
        "ex_showroom_price": ("booking_ex_showroom_price", None),
        "tcs_amount": ("booking_tcs_amount", None),
        "registration_charges": ("booking_registration_charges", None),
        "road_tax_amount": ("booking_road_tax_amount", None),
        "road_tax_registration": ("booking_road_tax_registration_combined", None),
        "insurance_amount": ("booking_insurance_amount", None),
        "rsa_amount": ("booking_rsa_amount", None),
        "accessories_cost": ("booking_accessories_cost", None),
        "additional_warranty_amount": ("booking_additional_warranty_amount", None),
        "other_charges": ("booking_other_charges", None),
        "total_price": ("booking_total_price", None),
        "discount_amount": ("booking_discount_amount", None),
        "bonus_amount": ("booking_bonus_amount", None),
        "net_amount": ("booking_net_amount", None),
        "booking_amount_paid": ("booking_amount_paid", None),
        "balance_amount": ("booking_balance_amount", None),
        "mode_of_payment": ("booking_payment_mode", None),
        "payment_reference_no": ("booking_payment_reference", None),
        "expected_delivery": ("expected_delivery_text", None),
        "expected_delivery_date": ("expected_delivery_date", None),
    }
    for field_key, (attribute_key, operational_field) in expected.items():
        spec = spec_for_field(field_key)
        assert spec is not None, field_key
        assert spec.mapping_status == "SUPPORTED", field_key
        assert spec.attribute_key == attribute_key, field_key
        assert spec.operational_field == operational_field, field_key


def test_pan_and_aadhaar_relationships_are_mapped_source_specifically() -> None:
    pan_type = spec_for_field("pan_relationship_type")
    aadhaar_type = spec_for_field("aadhaar_relationship_type")
    pan_name = spec_for_field("pan_relationship_name")
    aadhaar_name = spec_for_field("aadhaar_relationship_name")
    father = spec_for_field("pan_father_name")

    assert pan_type is not None and pan_type.attribute_key == "customer_relationship_type"
    assert aadhaar_type is not None and aadhaar_type.attribute_key == "customer_relationship_type"
    assert pan_name is not None and pan_name.attribute_key == "customer_relationship_name"
    assert aadhaar_name is not None and aadhaar_name.attribute_key == "customer_relationship_name"
    assert father is not None and father.attribute_key == "pan_father_name"


def test_stage_linkage_migrations_are_scoped_and_do_not_guess_payment_stage() -> None:
    root = Path(__file__).parents[1] / "migrations" / "versions"
    linkage = (root / "0041_uc03_journey_stage_linkage.py").read_text(encoding="utf-8")
    relationship = (root / "0042_uc03_customer_relationship.py").read_text(encoding="utf-8")
    stage = (root / "0043_uc03_booking_stage_link.py").read_text(encoding="utf-8")

    for fragment in (
        "ADD COLUMN journey_id uuid",
        "ADD COLUMN booking_id uuid",
        "ADD COLUMN delivery_id uuid",
        "DEFAULT 'UNSPECIFIED'",
        "payment_stage IN ('UNSPECIFIED','BOOKING','DELIVERY')",
        "sync_booking_reverse_links",
        "prepare_delivery_booking_link",
        "prepare_payment_stage_link",
        "target_journeys",
    ):
        assert fragment in linkage

    assert "INSERT INTO auditcore.bookings (tenant_id, journey_id)\n        SELECT j.tenant_id, j.journey_id\n        FROM auditcore.journeys j\n        WHERE NOT EXISTS" not in linkage
    assert "relationship_type" in relationship
    assert "relationship_name" in relationship
    assert "S/O" in relationship and "W/O" in relationship and "D/O" in relationship
    assert "NEW.stage_code = 'BOOKING'" in stage
    assert "AFTER INSERT OR UPDATE OF stage_code ON auditcore.journey_stage_states" in stage
