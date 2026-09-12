from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core import uc03_duplicate_booking_detection as dbd


@pytest.fixture
def two_journeys():
    """Two journeys under one tenant -- the minimum shape this rule needs."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for duplicate-booking integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-dbd-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"DBD-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"DBD-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'DBD', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"DBD-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"DBD-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"DBD-O-{suffix}"},
        ).scalar_one()

        def _journey(ref_suffix: str):
            customer_id = c.execute(
                text("""INSERT INTO auditcore.customers
                    (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                    VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"""),
                {"t": tenant_id, "d": dealer_id, "o": outlet_id},
            ).scalar_one()
            return c.execute(
                text("""INSERT INTO auditcore.journeys
                    (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                    VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""),
                {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"DBD-J-{ref_suffix}"},
            ).scalar_one()

        journey_a = _journey(f"{suffix}-A")
        journey_b = _journey(f"{suffix}-B")
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_a = journey_a  # type: ignore[attr-defined]
        c.journey_b = journey_b  # type: ignore[attr-defined]
        yield c
    engine.dispose()


def _set_field(c, *, journey_id, document_type_key, field_key, value) -> None:
    # Replace, don't add a competing row -- two rows for the same
    # (journey_id, field_key) with identical confidence and a tied
    # updated_at_utc (same transaction) would make "latest" undefined.
    c.execute(
        text(
            "DELETE FROM auditcore.journey_document_extracted_fields "
            "WHERE tenant_id=:t AND journey_id=:j AND field_key=:fk"
        ),
        {"t": c.tenant_id, "j": journey_id, "fk": field_key},
    )
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
                NULL, 1, 'BOOKING',
                :dtk, NULL, :fk,
                CAST(:v AS jsonb), CAST(:v AS jsonb), 0.95, false
            )
            """
        ),
        {"t": c.tenant_id, "j": journey_id, "doc": uuid4(), "dtk": document_type_key,
         "fk": field_key, "v": json.dumps(value)},
    )


def _assert_exactly_one_flagged_as_duplicate_of_the_other(c) -> None:
    """Both journeys were created in the same transaction (Postgres's now()
    is transaction-start time), so this exercises the journey_id tiebreaker
    rather than assuming a fixed 'a is earlier' ordering -- run the check
    from both sides and confirm exactly one flags the other, consistently."""
    result_a = dbd.sync_duplicate_booking_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_a, correlation_id="",
    )
    result_b = dbd.sync_duplicate_booking_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_b, correlation_id="",
    )
    a_findings = _open_findings(c, c.journey_a)
    b_findings = _open_findings(c, c.journey_b)
    assert (result_a["raised"], result_b["raised"]) in {(0, 1), (1, 0)}
    if result_a["raised"] == 1:
        assert a_findings[0]["rule_key"] == f"DUPLICATE_BOOKING:{c.journey_b}"
        assert b_findings == []
    else:
        assert b_findings[0]["rule_key"] == f"DUPLICATE_BOOKING:{c.journey_a}"
        assert a_findings == []


def _open_findings(c, journey_id) -> list[dict]:
    return [
        dict(row)
        for row in c.execute(
            text("SELECT rule_key, severity, description FROM auditcore.audit_findings "
                 "WHERE tenant_id=:t AND journey_id=:j AND finding_type_code='DUPLICATE_BOOKING' "
                 "AND finding_status IN ('OPEN','ACKNOWLEDGED')"),
            {"t": c.tenant_id, "j": journey_id},
        ).mappings().all()
    ]


def test_no_identity_data_yet_is_a_noop(two_journeys) -> None:
    c = two_journeys
    result = dbd.sync_duplicate_booking_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_a, correlation_id="",
    )
    assert result == {"raised": 0, "resolved": 0, "examined": 0}


def test_exact_pan_match_flags_exactly_one_journey_as_the_others_duplicate(two_journeys) -> None:
    c = two_journeys
    _set_field(c, journey_id=c.journey_a, document_type_key="pan_card", field_key="pan_number", value="ABCDE1234F")
    _set_field(c, journey_id=c.journey_b, document_type_key="pan_card", field_key="pan_number", value="ABCDE1234F")

    # Neither has a confirmed minimum booking amount, and both journeys were
    # created in the same transaction (tied created_at_utc) -- exercises the
    # journey_id tiebreaker rather than assuming a fixed winner.
    _assert_exactly_one_flagged_as_duplicate_of_the_other(c)


def test_exact_aadhaar_match_flags_exactly_one_journey_as_the_others_duplicate(two_journeys) -> None:
    c = two_journeys
    _set_field(c, journey_id=c.journey_a, document_type_key="aadhaar", field_key="aadhaar_number", value="123456789012")
    _set_field(c, journey_id=c.journey_b, document_type_key="aadhaar", field_key="aadhaar_number", value="123456789012")

    _assert_exactly_one_flagged_as_duplicate_of_the_other(c)


def test_different_pan_and_dissimilar_name_does_not_flag(two_journeys) -> None:
    c = two_journeys
    _set_field(c, journey_id=c.journey_a, document_type_key="pan_card", field_key="pan_number", value="AAAAA1111A")
    _set_field(c, journey_id=c.journey_a, document_type_key="aadhaar", field_key="aadhaar_name", value="Sanjaya Kumar Mohanty")
    _set_field(c, journey_id=c.journey_b, document_type_key="pan_card", field_key="pan_number", value="BBBBB2222B")
    _set_field(c, journey_id=c.journey_b, document_type_key="aadhaar", field_key="aadhaar_name", value="Priya Nair")

    result = dbd.sync_duplicate_booking_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_b, correlation_id="",
    )
    assert result["raised"] == 0
    assert _open_findings(c, c.journey_b) == []


def test_fuzzy_name_and_matching_pincode_flags_a_duplicate(two_journeys) -> None:
    c = two_journeys
    _set_field(c, journey_id=c.journey_a, document_type_key="aadhaar", field_key="aadhaar_name", value="Sanjaya Kumar Mohanty")
    _set_field(c, journey_id=c.journey_a, document_type_key="aadhaar", field_key="address_pincode", value="751001")
    _set_field(c, journey_id=c.journey_b, document_type_key="aadhaar", field_key="aadhaar_name", value="Sanjay Kumar Mohanty")  # minor OCR variant
    _set_field(c, journey_id=c.journey_b, document_type_key="aadhaar", field_key="address_pincode", value="751001")

    _assert_exactly_one_flagged_as_duplicate_of_the_other(c)


def test_matching_name_without_matching_pincode_does_not_flag(two_journeys) -> None:
    c = two_journeys
    _set_field(c, journey_id=c.journey_a, document_type_key="aadhaar", field_key="aadhaar_name", value="Sanjaya Kumar Mohanty")
    _set_field(c, journey_id=c.journey_a, document_type_key="aadhaar", field_key="address_pincode", value="751001")
    _set_field(c, journey_id=c.journey_b, document_type_key="aadhaar", field_key="aadhaar_name", value="Sanjaya Kumar Mohanty")
    _set_field(c, journey_id=c.journey_b, document_type_key="aadhaar", field_key="address_pincode", value="560001")

    result = dbd.sync_duplicate_booking_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_b, correlation_id="",
    )
    assert result["raised"] == 0


def test_confirmed_minimum_booking_amount_wins_originality_over_creation_order(two_journeys) -> None:
    c = two_journeys
    _set_field(c, journey_id=c.journey_a, document_type_key="pan_card", field_key="pan_number", value="CCCCC3333C")
    _set_field(c, journey_id=c.journey_b, document_type_key="pan_card", field_key="pan_number", value="CCCCC3333C")

    # journey_a was created first, but journey_b has its minimum booking
    # amount confirmed (no open BK_MIN_BOOKING_AMOUNT_NOT_MET) while
    # journey_a's is still open -- journey_b should win originality despite
    # being created later.
    c.execute(
        text("""INSERT INTO auditcore.audit_findings
            (tenant_id, journey_id, finding_type_code, severity, finding_status,
             title, stage_code, origin_kind, origin_role_snapshot, rule_key, correlation_id)
            VALUES (:t, :j, 'COMMERCIAL_EXCEPTION', 'HIGH', 'OPEN', 'test',
                    'BOOKING', 'MACHINE', 'SYSTEM', 'BK_MIN_BOOKING_AMOUNT_NOT_MET', '')"""),
        {"t": c.tenant_id, "j": c.journey_a},
    )

    result_a = dbd.sync_duplicate_booking_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_a, correlation_id="",
    )
    assert result_a["raised"] == 1
    assert _open_findings(c, c.journey_a)[0]["rule_key"] == f"DUPLICATE_BOOKING:{c.journey_b}"

    result_b = dbd.sync_duplicate_booking_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_b, correlation_id="",
    )
    assert result_b["raised"] == 0
    assert _open_findings(c, c.journey_b) == []


def test_pairing_that_no_longer_matches_self_heals(two_journeys) -> None:
    c = two_journeys
    _set_field(c, journey_id=c.journey_a, document_type_key="pan_card", field_key="pan_number", value="DDDDD4444D")
    _set_field(c, journey_id=c.journey_b, document_type_key="pan_card", field_key="pan_number", value="DDDDD4444D")

    # Find whichever journey the tiebreaker actually flagged as the
    # duplicate -- both were created in the same transaction (tied
    # created_at_utc), so this isn't assumed to be a fixed side.
    dbd.sync_duplicate_booking_detection(c, tenant_id=c.tenant_id, journey_id=c.journey_a, correlation_id="")
    dbd.sync_duplicate_booking_detection(c, tenant_id=c.tenant_id, journey_id=c.journey_b, correlation_id="")
    duplicate_journey = c.journey_a if _open_findings(c, c.journey_a) else c.journey_b
    assert _open_findings(c, duplicate_journey)

    # A correction shows these are two different PANs after all.
    _set_field(c, journey_id=duplicate_journey, document_type_key="pan_card", field_key="pan_number", value="EEEEE5555E")
    result2 = dbd.sync_duplicate_booking_detection(
        c, tenant_id=c.tenant_id, journey_id=duplicate_journey, correlation_id="",
    )
    assert result2["resolved"] == 1
    assert _open_findings(c, duplicate_journey) == []
