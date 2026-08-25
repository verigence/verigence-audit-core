import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.uc03_booking_part1 import _normalized
from audit_core.uc03_booking_receipt_capture import _RECEIPT_CAPTURE_MAP


def test_part1_master_normalization_is_exact_but_format_tolerant() -> None:
    assert _normalized("Scorpio N") == "scorpion"
    assert _normalized("  Z8-L  ") == "z8l"
    assert _normalized(None) is None


def test_dealer_receipt_fields_use_generic_pc_review_contract() -> None:
    assert set(_RECEIPT_CAPTURE_MAP) == {
        "dealer_name",
        "dealer_gstin",
        "customer_name",
        "customer_phone",
        "receipt_number",
        "receipt_date",
        "amount_paid",
        "payment_mode",
        "payment_reference_no",
        "payment_reference_date",
        "bank_name",
        "bank_location",
        "booking_reference_number",
        "remarks",
        "amount_in_words",
    }
    assert _RECEIPT_CAPTURE_MAP["amount_paid"] == "RECEIPT_AMOUNT"
    assert _RECEIPT_CAPTURE_MAP["customer_phone"] == "RECEIPT_CUSTOMER_PHONE"
    assert (
        _RECEIPT_CAPTURE_MAP["booking_reference_number"]
        == "RECEIPT_BOOKING_REFERENCE"
    )


def test_new_project_gets_part1_evidence_defaults() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 Part-1 profile integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-uc03-part1-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, 'Vehicle')
                RETURNING product_category_id
                """
            ),
            {"code": f"P1-CAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, 'Part1 OEM')
                RETURNING oem_id
                """
            ),
            {"code": f"P1-OEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date,
                    timezone_name, project_status
                ) VALUES (
                    :tenant_id, :code, 'Part1 Project', :oem_id,
                    :category_id, CURRENT_DATE, 'Asia/Kolkata', 'ACTIVE'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"P1-P-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )

        rows = connection.execute(
            text(
                """
                SELECT i.requirement_key, i.document_type_key, i.requirement_level
                FROM auditcore.document_requirement_profiles p
                JOIN auditcore.document_requirement_profile_versions v
                  ON v.tenant_id=p.tenant_id
                 AND v.document_requirement_profile_id=p.document_requirement_profile_id
                JOIN auditcore.document_requirement_items i
                  ON i.tenant_id=v.tenant_id
                 AND i.document_requirement_profile_version_id=
                    v.document_requirement_profile_version_id
                WHERE p.tenant_id=:tenant_id
                  AND p.profile_code='UC03_DEFAULT_VEHICLE_SALES'
                  AND v.lifecycle_status='PUBLISHED'
                  AND upper(i.process_area)='BOOKING'
                """
            ),
            {"tenant_id": tenant_id},
        ).mappings().all()

    by_key = {row["requirement_key"]: row for row in rows}
    assert by_key["booking_docket"]["requirement_level"] == "REQUIRED"
    assert by_key["pan_card"]["requirement_level"] == "OPTIONAL"
    assert by_key["aadhaar"]["requirement_level"] == "OPTIONAL"
    assert by_key["booking_payment_receipt"]["document_type_key"] == "dealer_receipt"
    assert by_key["booking_payment_receipt"]["requirement_level"] == "REQUIRED"
