from __future__ import annotations

import os
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

import audit_core.uc03_invoice_materialization as im


# ── unit: deterministic derivation ──────────────────────────────────────────
def test_derive_commercials_vehicle_tax_invoice() -> None:
    out = im.derive_commercials(
        {
            "invoice_purpose": "VEHICLE_SALE",
            "taxable_amount": "1600000",
            "tcs_amount": "16000",
            "grand_total_amount": "1596000",
            "line_items": [
                {"line_category": "ACCESSORY_GENUINE", "net_amount": "25000"},
                {"line_category": "EXTENDED_WARRANTY", "net_amount": "12000"},
                {"line_category": "VEHICLE", "net_amount": "1600000"},
            ],
        }
    )
    assert out == {
        "ex_showroom_price": Decimal(1600000),
        "tcs_amount": Decimal(16000),
        "accessories_cost": Decimal(25000),
        "additional_warranty_amount": Decimal(12000),
    }


def test_derive_commercials_accessory_invoice_from_grand_total() -> None:
    out = im.derive_commercials({"invoice_purpose": "ACCESSORY", "grand_total_amount": "48000"})
    assert out == {"accessories_cost": Decimal(48000)}


def test_derive_commercials_ew_invoice() -> None:
    out = im.derive_commercials({"invoice_purpose": "EXTENDED_WARRANTY", "grand_total_amount": "18000"})
    assert out == {"additional_warranty_amount": Decimal(18000)}


def test_derive_commercials_lines_win_over_grand_total() -> None:
    out = im.derive_commercials(
        {
            "invoice_purpose": "ACCESSORY",
            "grand_total_amount": "40000",
            "line_items": [
                {"line_category": "ACCESSORY_GENUINE", "net_amount": "30000"},
                {"line_category": "ACCESSORY_NON_GENUINE", "net_amount": "10000"},
            ],
        }
    )
    assert out == {"accessories_cost": Decimal(40000)}


def test_derive_commercials_vehicle_line_fallback_when_no_taxable() -> None:
    out = im.derive_commercials(
        {
            "invoice_purpose": "VEHICLE_SALE",
            "line_items": [{"line_category": "VEHICLE", "net_amount": "1450000"}],
        }
    )
    assert out == {"ex_showroom_price": Decimal(1450000)}


def test_derive_discounts_invoice_level() -> None:
    assert im.derive_discounts({"invoice_discount_amount": "20000"}) == {"CASH_DISCOUNT": Decimal(20000)}


def test_derive_discounts_adds_discount_line_items() -> None:
    out = im.derive_discounts(
        {
            "invoice_discount_amount": "10000",
            "line_items": [
                {"line_category": "DISCOUNT_LINE", "net_amount": "-5000"},
                {"line_category": "VEHICLE", "net_amount": "1600000"},
            ],
        }
    )
    assert out == {"CASH_DISCOUNT": Decimal(15000)}


def test_derive_discounts_empty() -> None:
    assert im.derive_discounts({"grand_total_amount": "100"}) == {}


def test_derive_commercials_skips_a_credit_note_entirely() -> None:
    # A credit note reduces an earlier invoice -- it must never also land as
    # a fresh ex_showroom_price/commercial line (that would double-count the
    # original sale). derive_discounts below is where its value belongs.
    out = im.derive_commercials(
        {
            "invoice_nature": "CREDIT_NOTE",
            "invoice_purpose": "VEHICLE_SALE",
            "taxable_amount": "25000",
            "grand_total_amount": "25000",
            "line_items": [{"line_category": "VEHICLE", "net_amount": "25000"}],
        }
    )
    assert out == {}


def test_derive_discounts_routes_a_credit_note_to_additional_discount() -> None:
    out = im.derive_discounts(
        {"invoice_nature": "CREDIT_NOTE", "grand_total_amount": "25000"}
    )
    assert out == {"ADDITIONAL_DISCOUNT": Decimal(25000)}


def test_derive_discounts_credit_note_falls_back_to_taxable_amount() -> None:
    out = im.derive_discounts(
        {"invoice_nature": "credit_note", "taxable_amount": "17857.14"}
    )
    assert out == {"ADDITIONAL_DISCOUNT": Decimal("17857.14")}


def test_derive_discounts_credit_note_with_no_amount_is_empty() -> None:
    assert im.derive_discounts({"invoice_nature": "CREDIT_NOTE"}) == {}


def test_line_item_rows_parses_json_string() -> None:
    rows = im._line_item_rows('[{"line_category": "RSA", "net_amount": 2000}]')
    assert rows == [{"line_category": "RSA", "net_amount": 2000}]


def test_to_decimal_tolerant() -> None:
    assert im._to_decimal("N/A") is None
    assert im._to_decimal("") is None
    assert im._to_decimal("1,60,000") == Decimal(160000)
    assert im._to_decimal("18000.50") == Decimal("18000.50")


# ── integration ─────────────────────────────────────────────────────────────
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
        pytest.skip("DATABASE_URL is required for invoice-materialization integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-inv-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"INV-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"INV-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'INV', :o, :cat, CURRENT_DATE - 60, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"INV-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"INV-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"INV-O-{suffix}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"INV-J-{suffix}"},
        ).scalar_one()
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_id = journey_id  # type: ignore[attr-defined]
        yield c
    engine.dispose()


def _commercial(c, key):
    return c.execute(
        text("SELECT actual_amount, source_reference, actual_source_kind "
             "FROM auditcore.commercial_lines "
             "WHERE tenant_id=:t AND journey_id=:j AND component_key=:k"),
        {"t": c.tenant_id, "j": c.journey_id, "k": key},
    ).mappings().one_or_none()


def test_tax_invoice_persisted_and_projected(journey) -> None:
    c = journey
    result = im.materialize_reviewed_invoices(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_doc("tax_invoice_tally", {
            "invoice_purpose": "VEHICLE_SALE",
            "invoice_nature": "TAX_INVOICE",
            "invoice_number": "TI/2026/0042",
            "invoice_date": "2026-09-01",
            "seller_name": "Aditya Motors",
            "taxable_amount": "1600000",
            "tcs_amount": "16000",
            "invoice_discount_amount": "20000",
            "grand_total_amount": "1596000",
            "line_items": [{"line_category": "EXTENDED_WARRANTY", "net_amount": "12000"}],
        })],
    )
    assert result["invoices"] == 1

    row = c.execute(
        text("SELECT invoice_purpose, invoice_number, taxable_amount, document_type_key "
             "FROM auditcore.invoice_review_values "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert row["invoice_purpose"] == "VEHICLE_SALE"
    assert row["invoice_number"] == "TI/2026/0042"
    assert row["taxable_amount"] == Decimal(1600000)
    assert row["document_type_key"] == "tax_invoice_tally"

    ex = _commercial(c, "ex_showroom_price")
    assert ex["actual_amount"] == Decimal(1600000)
    assert ex["source_reference"].startswith("tax_invoice_tally:")
    assert ex["actual_source_kind"] == "EVIDENCE"
    assert _commercial(c, "tcs_amount")["actual_amount"] == Decimal(16000)
    assert _commercial(c, "additional_warranty_amount")["actual_amount"] == Decimal(12000)

    disc = c.execute(
        text("SELECT actual_discount_amount, actual_source_kind, details "
             "FROM auditcore.discount_applications "
             "WHERE tenant_id=:t AND journey_id=:j AND discount_key='CASH_DISCOUNT'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert disc["actual_discount_amount"] == Decimal(20000)
    assert disc["details"]["origin"] == "INVOICE_MATERIALIZATION"


def test_accessory_invoice_projects_accessories_cost(journey) -> None:
    c = journey
    im.materialize_reviewed_invoices(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_doc("accessory_invoice_dms", {
            "invoice_purpose": "ACCESSORY",
            "grand_total_amount": "48000",
        })],
    )
    assert _commercial(c, "accessories_cost")["actual_amount"] == Decimal(48000)
    addon = c.execute(
        text("SELECT actual_amount FROM auditcore.journey_addons "
             "WHERE tenant_id=:t AND journey_id=:j AND addon_type_code='ACCESSORIES_TOTAL'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert addon == Decimal(48000)


def test_invoice_replaces_booking_form_commercial(journey) -> None:
    c = journey
    c.execute(
        text("INSERT INTO auditcore.commercial_lines "
             "(tenant_id, journey_id, component_key, actual_amount, actual_source_kind, source_reference) "
             "VALUES (:t, :j, 'ex_showroom_price', 1550000, 'EVIDENCE', :ref)"),
        {"t": c.tenant_id, "j": c.journey_id, "ref": f"booking_form:{uuid4()}"},
    )
    im.materialize_reviewed_invoices(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_doc("tax_invoice_tally", {
            "invoice_purpose": "VEHICLE_SALE", "taxable_amount": "1600000",
        })],
    )
    ex = _commercial(c, "ex_showroom_price")
    assert ex["actual_amount"] == Decimal(1600000)
    assert ex["source_reference"].startswith("tax_invoice_tally:")


def test_weaker_invoice_does_not_override_stronger(journey) -> None:
    c = journey
    strong = _doc("tax_invoice_tally", {"invoice_purpose": "VEHICLE_SALE", "taxable_amount": "1600000"})
    weak = _doc("invoice_generic", {"invoice_purpose": "VEHICLE_SALE", "taxable_amount": "999999"})
    im.materialize_reviewed_invoices(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester", documents=[strong, weak],
    )
    assert _commercial(c, "ex_showroom_price")["actual_amount"] == Decimal(1600000)


def test_idempotent(journey) -> None:
    c = journey
    docs = [_doc("tax_invoice_tally", {
        "invoice_purpose": "VEHICLE_SALE", "taxable_amount": "1600000",
        "invoice_discount_amount": "20000",
    })]
    for _ in range(3):
        im.materialize_reviewed_invoices(
            c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester", documents=docs,
        )
    assert c.execute(
        text("SELECT count(*) FROM auditcore.invoice_review_values WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one() == 1
    assert c.execute(
        text("SELECT count(*) FROM auditcore.commercial_lines WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one() == 1
    assert c.execute(
        text("SELECT count(*) FROM auditcore.discount_applications WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one() == 1
