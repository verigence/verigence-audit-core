from pathlib import Path


def test_uc03_stage_linkage_migration_keeps_explicit_ids_and_stage_rules() -> None:
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0040_uc03_journey_stage_linkage.py"
    ).read_text(encoding="utf-8")

    required_fragments = (
        "ADD COLUMN journey_id uuid",
        "ADD COLUMN booking_id uuid",
        "ADD COLUMN delivery_id uuid",
        "ADD COLUMN payment_stage varchar(20) NOT NULL DEFAULT 'BOOKING'",
        "ensure_booking_for_journey",
        "sync_booking_reverse_links",
        "prepare_delivery_booking_link",
        "sync_delivery_reverse_link",
        "prepare_payment_stage_link",
        "payment_stage = 'BOOKING' AND delivery_id IS NULL",
        "payment_stage = 'DELIVERY' AND delivery_id IS NOT NULL",
    )
    for fragment in required_fragments:
        assert fragment in migration


def test_customer_name_design_keeps_entered_and_document_names_separate() -> None:
    design = (
        Path(__file__).parents[1]
        / "docs"
        / "uc-003-booking-delivery-audit"
        / "UC03_JOURNEY_STAGE_LINKAGE_2026-08-30.md"
    ).read_text(encoding="utf-8")

    assert "customers.display_name" in design
    assert "PC-entered name" in design
    assert "customers.legal_name" in design
    assert "PAN/Aadhaar" in design
    assert "Booking Form `customer_name`" in design
    assert "does not overwrite" in design
