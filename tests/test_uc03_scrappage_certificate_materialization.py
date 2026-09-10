from __future__ import annotations

import os
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

import audit_core.uc03_scrappage_certificate_materialization as scm


# ── unit: small coercion helpers ─────────────────────────────────────────────
def test_to_decimal_tolerant() -> None:
    assert scm._to_decimal("N/A") is None
    assert scm._to_decimal("") is None
    assert scm._to_decimal("800.0") == Decimal("800.0")


def test_to_int_from_decimal() -> None:
    assert scm._to_int("4") == 4
    assert scm._to_int(None) is None


def test_to_date_parses_iso() -> None:
    assert str(scm._to_date("2026-08-11")) == "2026-08-11"
    assert scm._to_date(None) is None
    assert scm._to_date("not-a-date") is None


def test_clean_text_collapses_whitespace() -> None:
    assert scm._clean_text("  YGA  STAR   AUTO  ") == "YGA STAR AUTO"
    assert scm._clean_text(None) is None


# ── integration ───────────────────────────────────────────────────────────────
def _doc(document_type: str, fields: dict[str, object], *, state: str = "READY"):
    return SimpleNamespace(
        documentId=uuid4(),
        evidenceId=None,
        documentTypeKey=document_type,
        extractionState=state,
        fields=[SimpleNamespace(fieldKey=k, value=v) for k, v in fields.items()],
    )


@pytest.fixture
def journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for scrappage-certificate-materialization integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-scrap-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"SCRAP-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"SCRAP-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'SCRAP', :o, :cat, CURRENT_DATE - 60, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"SCRAP-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"SCRAP-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"SCRAP-O-{suffix}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"SCRAP-J-{suffix}"},
        ).scalar_one()
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_id = journey_id  # type: ignore[attr-defined]
        yield c
    engine.dispose()


def test_original_certificate_persists_old_vehicle_details(journey) -> None:
    c = journey
    written = scm.materialize_reviewed_scrappage_certificates(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_doc("scrappage_certificate_of_deposit", {
            "certificate_variant": "ORIGINAL",
            "certificate_number": "COD202608000DL7C5573",
            "old_vehicle_registration_number": "DL7C5573",
            "old_vehicle_make": "MARUTI SUZUKI INDIA LTD",
            "old_vehicle_model": "M800",
            "old_vehicle_category": "LMV",
            "old_vehicle_type": "Non - Transport",
            "old_vehicle_fuel_type": "PETROL",
            "old_vehicle_cubic_capacity": "800.0",
            "old_vehicle_seating_capacity": "4",
            "old_vehicle_year_of_manufacturing": "1996",
            "old_vehicle_unladen_weight_kg": "620",
            "old_vehicle_number_of_cylinders": "3",
            "current_holder_name": "IMRAN KHAN",
            "scrapping_facility_name": "YGA STAR AUTO SCRAPPING CENTRE PRIVATE LIMITED",
            "state_of_scrapping": "UTTAR PRADESH",
        })],
    )
    assert written == 1

    row = c.execute(
        text("""SELECT certificate_variant, certificate_number, old_vehicle_registration_number,
                       old_vehicle_make, old_vehicle_model, old_vehicle_cubic_capacity,
                       old_vehicle_seating_capacity, current_holder_name, scrapping_facility_name,
                       document_type_key
                FROM auditcore.scrappage_certificate_review_values
                WHERE tenant_id=:t AND journey_id=:j"""),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert row["certificate_variant"] == "ORIGINAL"
    assert row["certificate_number"] == "COD202608000DL7C5573"
    assert row["old_vehicle_registration_number"] == "DL7C5573"
    assert row["old_vehicle_make"] == "MARUTI SUZUKI INDIA LTD"
    assert row["old_vehicle_model"] == "M800"
    assert row["old_vehicle_cubic_capacity"] == Decimal("800.00")
    assert row["old_vehicle_seating_capacity"] == 4
    assert row["current_holder_name"] == "IMRAN KHAN"
    assert row["scrapping_facility_name"] == "YGA STAR AUTO SCRAPPING CENTRE PRIVATE LIMITED"
    assert row["document_type_key"] == "scrappage_certificate_of_deposit"


def test_transfer_certificate_captures_trade_details(journey) -> None:
    c = journey
    scm.materialize_reviewed_scrappage_certificates(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_doc("scrappage_certificate_of_deposit", {
            "certificate_variant": "TRANSFERRED",
            "certificate_number": "COD202608000DL7C5573",
            "old_vehicle_registration_number": "DL7C5573",
            "original_owner_name": "IMRAN KHAN",
            "current_holder_name": "SANJAYA KUMAR MOHANTY",
            "current_holder_mobile": "******9204",
            "current_holder_pan": "******582C",
            "trade_date": "2026-08-13",
            "trade_number": "33130826332229771262",
        })],
    )
    row = c.execute(
        text("""SELECT certificate_variant, original_owner_name, current_holder_name,
                       current_holder_mobile, trade_date, trade_number
                FROM auditcore.scrappage_certificate_review_values
                WHERE tenant_id=:t AND journey_id=:j"""),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert row["certificate_variant"] == "TRANSFERRED"
    assert row["original_owner_name"] == "IMRAN KHAN"
    assert row["current_holder_name"] == "SANJAYA KUMAR MOHANTY"
    assert str(row["trade_date"]) == "2026-08-13"
    assert row["trade_number"] == "33130826332229771262"


def test_ignores_documents_of_a_different_type(journey) -> None:
    c = journey
    written = scm.materialize_reviewed_scrappage_certificates(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_doc("corporate_id", {"customer_name": "Someone"})],
    )
    assert written == 0
    assert c.execute(
        text("SELECT count(*) FROM auditcore.scrappage_certificate_review_values "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one() == 0


def test_not_yet_extracted_document_is_skipped(journey) -> None:
    c = journey
    written = scm.materialize_reviewed_scrappage_certificates(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_doc("scrappage_certificate_of_deposit", {"certificate_number": "COD1"}, state="PENDING")],
    )
    assert written == 0


def test_idempotent_on_repeated_sync(journey) -> None:
    c = journey
    docs = [_doc("scrappage_certificate_of_deposit", {
        "certificate_variant": "ORIGINAL",
        "certificate_number": "COD1",
        "old_vehicle_registration_number": "DL1AB1234",
        "current_holder_name": "Test Owner",
    })]
    for _ in range(3):
        scm.materialize_reviewed_scrappage_certificates(
            c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester", documents=docs,
        )
    assert c.execute(
        text("SELECT count(*) FROM auditcore.scrappage_certificate_review_values "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one() == 1


def test_two_certificates_on_one_journey_both_persist(journey) -> None:
    # A journey can plausibly hold both the original Certificate of Deposit
    # and a Transfer Certificate of Deposit recording its resale.
    c = journey
    original = _doc("scrappage_certificate_of_deposit", {
        "certificate_variant": "ORIGINAL", "certificate_number": "COD1",
        "current_holder_name": "Imran Khan",
    })
    transfer = _doc("scrappage_certificate_of_deposit", {
        "certificate_variant": "TRANSFERRED", "certificate_number": "COD1",
        "current_holder_name": "Sanjaya Kumar Mohanty",
    })
    written = scm.materialize_reviewed_scrappage_certificates(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[original, transfer],
    )
    assert written == 2
    assert c.execute(
        text("SELECT count(*) FROM auditcore.scrappage_certificate_review_values "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one() == 2
