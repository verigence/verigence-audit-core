import os
from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from sqlalchemy import create_engine, text

from audit_core.uc03_booking_review_decisions import _raw_review_items
from audit_core.uc03_document_review_v2 import (
    ReviewV2Document,
    ReviewV2Field,
    ReviewV2UnmappedField,
)
from audit_core.uc03_strict_review_core_ownership import _DOCKET_ONLY_FIELDS
from audit_core.uc03_v2_review_materialization import (
    _AADHAAR_FIELDS,
    _BOOKING_FORM_FIELDS,
    _PAN_FIELDS,
    _RECEIPT_FIELDS,
    _payment_values,
    _reviewed_receipt_values,
    materialize_reviewed_di_business_values,
    receipt_document_ordinals,
    receipt_review_key,
)


def _field(field_key: str, value, *, confidence: float = 99.0) -> ReviewV2Field:
    return ReviewV2Field(
        canonicalFieldId=str(uuid4()),
        fieldKey=field_key,
        value=value,
        confidenceScore=confidence,
        sourceFactVersion=1,
        reviewState="READY" if confidence >= 92.0 else "NEEDS_REVIEW",
    )


def _receipt(document_id, *, amount: str, confidence: float = 99.0) -> ReviewV2Document:
    return ReviewV2Document(
        documentId=document_id,
        label="Dealer Receipt",
        documentTypeKey="dealer_receipt",
        originalFilename=f"{document_id}.pdf",
        processingStatus="PROCESSED",
        extractionState="READY",
        fields=[
            _field("receipt_number", f"R-{str(document_id)[:6]}"),
            _field("receipt_date", "2026-08-30"),
            _field("amount_paid", amount, confidence=confidence),
            _field("payment_mode", "UPI"),
            _field("payment_reference_no", "UTR-123"),
        ],
    )


def _receipt_unmapped(document_id, *, amount: str, confidence: float):
    return ReviewV2UnmappedField(
        canonicalFieldId=str(uuid4()),
        fieldKey="amount_paid",
        value=amount,
        confidenceScore=confidence,
        sourceFactVersion=1,
        documentId=document_id,
        documentTypeKey="dealer_receipt",
        documentLabel="Dealer Receipt",
        originalFilename=f"{document_id}.pdf",
    )


def test_booking_form_di_contract_has_core_review_owner_for_every_field() -> None:
    # Mirrors verigence-di booking_form schema v1.5 exactly. The strict Core-owner
    # installer reuses the same typed Booking owner for Booking Docket and therefore
    # extends the runtime owner field set with Docket-only keys. Those keys are not
    # part of the Booking Form DI contract and are excluded from this source-contract
    # assertion; they have separate Docket coverage in the strict-owner tests.
    booking_form_fields = set(_BOOKING_FORM_FIELDS) - set(_DOCKET_ONLY_FIELDS)
    assert booking_form_fields == {
        "dealer_name",
        "dealer_branch",
        "booking_reference_number",
        "booking_date",
        "customer_name",
        "customer_phone",
        "customer_email",
        "customer_address",
        "vehicle_model",
        "vehicle_variant",
        "vehicle_color",
        "sku_code",
        "sales_person",
        "registration_by",
        "registration_type",
        "insurance_by",
        "exchange_applicable",
        "exchange_value",
        "ex_showroom_price",
        "insurance_amount",
        "registration_charges",
        "road_tax_amount",
        "road_tax_registration",
        "tcs_amount",
        "rsa_amount",
        "additional_warranty_amount",
        "extended_warranty_amount",
        "accessories_cost",
        "essential_kit_amount",
        "genuine_accessories_amount",
        "non_genuine_accessories_amount",
        "fastag_amount",
        "green_tax_amount",
        "service_package_amount",
        "other_charges",
        "discount_amount",
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
        "scrappage_discount_amount",
        "bonus_amount",
        "total_price",
        "net_amount",
        "booking_amount_paid",
        "balance_amount",
        "mode_of_payment",
        "payment_reference_no",
        "expected_delivery",
        "expected_delivery_date",
    }


def test_pan_di_contract_has_core_review_owner_for_every_field() -> None:
    assert _PAN_FIELDS == {
        "pan_number",
        "pan_name",
        "pan_father_name",
        "pan_relationship_type",
        "pan_relationship_name",
        "date_of_birth",
    }


def test_aadhaar_di_contract_has_core_review_owner_for_every_field() -> None:
    # Mirrors verigence-di Aadhaar v1.2, including the three address components
    # that DI only emits when they are explicitly identifiable on the document.
    assert _AADHAAR_FIELDS == {
        "aadhaar_number",
        "aadhaar_name",
        "date_of_birth",
        "gender",
        "aadhaar_address",
        "address_pincode",
        "address_state",
        "address_district",
        "aadhaar_relationship_type",
        "aadhaar_relationship_name",
    }


def test_dealer_receipt_di_contract_has_core_review_owner_for_every_field() -> None:
    assert set(_RECEIPT_FIELDS) == {
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


def test_payment_values_sets_payment_at_utc_from_receipt_date() -> None:
    """Reported live: every auto-materialized Booking receipt showed
    'Not available' in the Payments ledger's own Date column, despite the
    receipt's own date being extracted correctly -- payment_at_utc (the
    ledger's canonical date column) was never written at all."""
    values = _payment_values({"receipt_date": "2026-09-11", "amount_paid": "21000"})
    assert values["payment_at_utc"] == "2026-09-11"


def test_payment_values_falls_back_to_payment_reference_date() -> None:
    values = _payment_values({"payment_reference_date": "2026-09-12", "amount_paid": "5000"})
    assert values["payment_at_utc"] == "2026-09-12"


def test_payment_values_classifies_payment_mode_code_from_the_raw_payment_mode() -> None:
    values = _payment_values({"payment_mode": "NEFT", "amount_paid": "21000"})
    assert values["payment_method_code"] == "NEFT"
    assert values["payment_mode_code"] == "NEFT"


def test_payment_values_falls_back_to_others_when_payment_mode_is_unrecognized_or_missing() -> None:
    assert _payment_values({"payment_mode": "UPI", "amount_paid": "5000"})["payment_mode_code"] == "OTHERS"
    assert _payment_values({"amount_paid": "5000"})["payment_mode_code"] == "OTHERS"


def test_receipt_review_key_is_receipt_scoped() -> None:
    assert receipt_review_key(1, "amount_paid") != receipt_review_key(
        2, "amount_paid"
    )


def test_receipt_ordinals_are_deterministic() -> None:
    first = uuid4()
    second = uuid4()
    forward = receipt_document_ordinals([first, second])
    reverse = receipt_document_ordinals([second, first])

    assert forward == reverse
    assert set(forward.values()) == {1, 2}


def test_two_receipts_with_different_amounts_are_not_cross_source_mismatch() -> None:
    first = uuid4()
    second = uuid4()
    ordinals = receipt_document_ordinals([first, second])

    items = _raw_review_items(
        [
            _receipt_unmapped(first, amount="20000", confidence=99.0),
            _receipt_unmapped(second, amount="30000", confidence=99.0),
        ]
    )

    assert len(items) == 2
    assert {item.review_key for item in items} == {
        receipt_review_key(ordinals[first], "amount_paid"),
        receipt_review_key(ordinals[second], "amount_paid"),
    }
    assert all(item.decision_required is False for item in items)


def test_low_confidence_receipt_field_requires_only_its_own_decision() -> None:
    first = uuid4()
    second = uuid4()
    ordinals = receipt_document_ordinals([first, second])

    items = {
        item.review_key: item
        for item in _raw_review_items(
            [
                _receipt_unmapped(first, amount="20000", confidence=80.0),
                _receipt_unmapped(second, amount="30000", confidence=99.0),
            ]
        )
    }

    assert items[
        receipt_review_key(ordinals[first], "amount_paid")
    ].decision_required is True
    assert items[
        receipt_review_key(ordinals[second], "amount_paid")
    ].decision_required is False


def test_rejected_receipt_amount_is_not_materialized_as_zero_payment() -> None:
    document_id = uuid4()
    document = _receipt(document_id, amount="50000")

    values = _reviewed_receipt_values(
        document,
        receipt_ordinal=1,
        rejected_review_keys={receipt_review_key(1, "amount_paid")},
    )

    assert "amount_paid" not in values
    assert values["receipt_number"] is not None


def test_reviewed_receipt_fields_are_collected_once_in_memory() -> None:
    document_id = uuid4()
    document = _receipt(document_id, amount="50000")

    values = _reviewed_receipt_values(
        document,
        receipt_ordinal=1,
        rejected_review_keys=set(),
    )

    assert str(values["amount_paid"]) == "50000"
    assert str(values["receipt_date"]) == "2026-08-30"
    assert values["payment_mode"] == "UPI"
    assert values["payment_reference_no"] == "UTR-123"


# ── integration: Confirm-time materialization covers insurance/finance too ──

@pytest.fixture
def journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for materialization integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-v2rm-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"V2RM-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"V2RM-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'V2RM', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"V2RM-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"V2RM-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"V2RM-O-{suffix}"},
        ).scalar_one()
        customer_id = c.execute(
            text("""INSERT INTO auditcore.customers
                (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = c.execute(
            text("""INSERT INTO auditcore.journeys
                (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"V2RM-J-{suffix}"},
        ).scalar_one()
    engine.dispose()
    engine = create_engine(database_url)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            c.tenant_id = tenant_id  # type: ignore[attr-defined]
            c.journey_id = journey_id  # type: ignore[attr-defined]
            yield c
    finally:
        delete_tenant_data(engine, tenant_id)
        engine.dispose()


def test_confirm_time_materialization_covers_insurance_and_finance(journey) -> None:
    # Regression: materialize_reviewed_di_business_values (the materializer
    # POST /booking/review/confirm runs the instant a PC confirms, including
    # any field corrections made via the review screen's date picker) never
    # called materialize_delivery_insurance/materialize_delivery_finance at
    # all -- both are genuinely stage-agnostic (they filter by document
    # TYPE, and auditcore.insurance_records/finance_records have no stage
    # column), but were only ever wired into Delivery's own confirm flow and
    # the async document-link webhook's Booking-side background sync. A PC
    # correcting an insurance or finance field here and clicking Confirm
    # needs that correction in the canonical table immediately, not only
    # once some later, unrelated document's background sync happens to run
    # this same materializer again.
    c = journey
    tenant_id, journey_id = c.tenant_id, c.journey_id
    insurance_doc = ReviewV2Document(
        documentId=uuid4(),
        label="Insurance Cover",
        documentTypeKey="insurance_cover",
        originalFilename="insurance.pdf",
        processingStatus="PROCESSED",
        extractionState="READY",
        fields=[
            _field("insurer_name", "Zurich Kotak General Insurance Company (Ind.) Ltd."),
            _field("agent_intermediary_name", "Aditya Motors"),
        ],
    )
    finance_doc = ReviewV2Document(
        documentId=uuid4(),
        label="Bank Approval Letter",
        documentTypeKey="bank_approval_letter",
        originalFilename="approval.pdf",
        processingStatus="PROCESSED",
        extractionState="READY",
        fields=[_field("financed_by", "UCO Bank")],
    )

    result = materialize_reviewed_di_business_values(
        c,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=[insurance_doc, finance_doc],
        rejected_review_keys=set(),
        actor_id="tester",
    )
    assert result["insuranceFieldsWritten"] > 0
    assert result["financeFieldsWritten"] > 0

    insurance_row = c.execute(
        text("SELECT insurer_name, agent_intermediary_name FROM auditcore.insurance_records "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    assert insurance_row["insurer_name"] == "Zurich Kotak General Insurance Company (Ind.) Ltd."
    assert insurance_row["agent_intermediary_name"] == "Aditya Motors"

    finance_row = c.execute(
        text("SELECT provider_name FROM auditcore.finance_records "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    assert finance_row["provider_name"] == "UCO Bank"


def test_extended_warranty_amount_aliases_onto_additional_warranty_amount(journey) -> None:
    """Direct user correction (2026-09-24): DI's booking_form schema has two
    overlapping fields for the same real-world Extended/Additional Warranty
    line -- additional_warranty_amount and a later, near-identically-aliased
    extended_warranty_amount -- so which one a given extraction lands on is
    model-dependent, not deterministic. Previously these wrote two
    independent commercial_lines rows: the price master only ever resolves
    a Standard amount for additional_warranty_amount, so whenever the real
    value landed under extended_warranty_amount instead, the Deal page
    showed one row with a real Standard/Actual pair and a second, always-
    empty "Extended Warranty Amount" row right next to it, as if they were
    different products. Both must now land on the single canonical
    additional_warranty_amount commercial line.
    """
    from audit_core.uc03_booking_commercial_components import (
        install_uc03_booking_commercial_components,
    )

    # extended_warranty_amount only exists as a recognized commercial-line
    # field once this installer has run (production runs it once at app
    # startup via uc03_document_capture_v2_rules's own import) -- without
    # it, the field is silently ignored rather than exercising the alias
    # this test is actually about.
    install_uc03_booking_commercial_components()

    c = journey
    tenant_id, journey_id = c.tenant_id, c.journey_id
    booking_form = ReviewV2Document(
        documentId=uuid4(),
        label="Booking Form",
        documentTypeKey="booking_form",
        originalFilename="booking.pdf",
        processingStatus="PROCESSED",
        extractionState="READY",
        fields=[_field("extended_warranty_amount", "17999")],
    )

    materialize_reviewed_di_business_values(
        c,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=[booking_form],
        rejected_review_keys=set(),
        actor_id="tester",
    )

    rows = c.execute(
        text("SELECT component_key, actual_amount FROM auditcore.commercial_lines "
             "WHERE tenant_id=:t AND journey_id=:j "
             "AND component_key IN ('additional_warranty_amount', 'extended_warranty_amount')"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    by_key = {row["component_key"]: row["actual_amount"] for row in rows}
    assert by_key == {"additional_warranty_amount": Decimal(17999)}
