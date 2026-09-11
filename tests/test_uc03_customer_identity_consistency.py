from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core import uc03_customer_identity_consistency as cic


# ── unit: name normalization / fuzzy match ──────────────────────────────────
def test_identical_names_match() -> None:
    assert cic._names_match("Sanjaya Kumar Mohanty", "Sanjaya Kumar Mohanty")


def test_case_and_whitespace_variants_match() -> None:
    assert cic._names_match("SANJAYA KUMAR MOHANTY", "  sanjaya   kumar mohanty ")


def test_title_prefix_is_ignored() -> None:
    assert cic._names_match("Mr. Sanjaya Kumar Mohanty", "Sanjaya Kumar Mohanty")


def test_minor_typo_still_matches() -> None:
    # A single missing letter across an independently-scanned document is a
    # formatting/OCR variant, not a different person.
    assert cic._names_match("Sanjaya Kumar Mohanty", "Sanjay Kumar Mohanty")


def test_clearly_different_names_do_not_match() -> None:
    assert not cic._names_match("Sanjaya Kumar Mohanty", "Priya Nair")


def test_blank_value_is_not_this_checks_job() -> None:
    # Nothing to compare -- a missing/unparseable name is a different finding
    # type's problem (MANUAL_VERIFICATION), not a mismatch.
    assert cic._names_match("Sanjaya Kumar Mohanty", "")
    assert cic._names_match("", "")


# ── DB integration ───────────────────────────────────────────────────────────
@pytest.fixture
def journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for identity-consistency integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-cic-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"CIC-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"CIC-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'CIC', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"CIC-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"CIC-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"CIC-O-{suffix}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"CIC-J-{suffix}"},
        ).scalar_one()
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_id = journey_id  # type: ignore[attr-defined]
        yield c
    engine.dispose()


def _set_named_field(
    c, *, stage_code, document_type_key, field_key, value, confidence=0.95, document_id=None
):
    document_id = document_id or uuid4()
    c.execute(
        text(
            """
            INSERT INTO auditcore.journey_document_extracted_fields (
                tenant_id, journey_id, evidence_id, di_document_id,
                source_fact_ref, source_fact_version, stage_code,
                source_document_type_key, source_canonical_field_id, field_key,
                extracted_value, effective_value, confidence_score, is_modified
            ) VALUES (
                :t, :j, NULL, :doc,
                NULL, 1, :stage,
                :dtk, NULL, :fk,
                CAST(:v AS jsonb), CAST(:v AS jsonb), :conf, false
            )
            """
        ),
        {"t": c.tenant_id, "j": c.journey_id, "doc": document_id, "stage": stage_code,
         "dtk": document_type_key, "fk": field_key, "v": json.dumps(value), "conf": confidence},
    )
    return document_id


def _open_wrong_document_findings(c) -> list[dict]:
    return [
        dict(row)
        for row in c.execute(
            text("SELECT rule_key, severity, finding_status FROM auditcore.audit_findings "
                 "WHERE tenant_id=:t AND journey_id=:j AND finding_type_code='WRONG_DOCUMENT' "
                 "AND finding_status IN ('OPEN','ACKNOWLEDGED')"),
            {"t": c.tenant_id, "j": c.journey_id},
        ).mappings().all()
    ]


def test_skips_when_no_kyc_document_yet(journey) -> None:
    c = journey
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="booking_form",
        field_key="customer_name", value="Sanjaya Kumar Mohanty",
    )

    result = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result == {"skipped": True, "reason": "no_kyc_name_yet"}
    assert _open_wrong_document_findings(c) == []


def test_matching_names_raise_nothing(journey) -> None:
    c = journey
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="aadhaar",
        field_key="aadhaar_name", value="Sanjaya Kumar Mohanty",
    )
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="booking_form",
        field_key="customer_name", value="Sanjaya Kumar Mohanty",
    )
    _set_named_field(
        c, stage_code="DELIVERY", document_type_key="insurance_cover",
        field_key="insured_name", value="Sanjaya K Mohanty",
    )

    result = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["raised"] == 0
    assert _open_wrong_document_findings(c) == []


def test_mismatched_invoice_raises_high_severity_wrong_document(journey) -> None:
    c = journey
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="aadhaar",
        field_key="aadhaar_name", value="Sanjaya Kumar Mohanty",
    )
    invoice_doc = _set_named_field(
        c, stage_code="DELIVERY", document_type_key="customer_invoice_dms",
        field_key="buyer_name", value="Priya Nair",
    )

    result = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["raised"] == 1
    findings = _open_wrong_document_findings(c)
    assert len(findings) == 1
    assert findings[0]["severity"] == "HIGH"
    assert findings[0]["rule_key"] == f"WRONG_DOCUMENT:{invoice_doc}"

    # finding_types registry classification actually applied (migration 0081).
    classified = c.execute(
        text("SELECT finding_class, owner_role_code FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j AND finding_type_code='WRONG_DOCUMENT'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert classified["finding_class"] == "VIOLATION"
    assert classified["owner_role_code"] == "TL"


def test_kyc_uploaded_after_a_mismatching_document_still_catches_it(journey) -> None:
    # Order does not matter -- a non-KYC document confirmed first, then KYC
    # arrives later and re-checks everything already on file.
    c = journey
    _set_named_field(
        c, stage_code="DELIVERY", document_type_key="insurance_cover",
        field_key="insured_name", value="Priya Nair",
    )
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="pan_card",
        field_key="pan_name", value="Sanjaya Kumar Mohanty",
    )

    result = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["raised"] == 1
    assert result["referenceName"] == "Sanjaya Kumar Mohanty"


def test_correction_to_a_matching_name_self_heals(journey) -> None:
    c = journey
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="aadhaar",
        field_key="aadhaar_name", value="Sanjaya Kumar Mohanty",
    )
    invoice_doc = _set_named_field(
        c, stage_code="DELIVERY", document_type_key="customer_invoice_dms",
        field_key="buyer_name", value="Priya Nair",
    )
    cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert len(_open_wrong_document_findings(c)) == 1

    # The PC re-uploads / DI re-extracts the same document with the correct name.
    c.execute(
        text("UPDATE auditcore.journey_document_extracted_fields "
             "SET effective_value = CAST(:v AS jsonb) "
             "WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:doc"),
        {"v": json.dumps("Sanjaya Kumar Mohanty"), "t": c.tenant_id, "j": c.journey_id, "doc": invoice_doc},
    )

    result = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["resolved"] == 1
    assert _open_wrong_document_findings(c) == []


def test_kyc_document_itself_is_not_checked_against_its_own_name(journey) -> None:
    c = journey
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="aadhaar",
        field_key="aadhaar_name", value="Sanjaya Kumar Mohanty",
    )

    result = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["raised"] == 0
    assert result["resolved"] == 0
