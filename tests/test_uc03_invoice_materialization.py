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
    # 2026-10-20 (R1): ex-showroom = this invoice's selling price x (1 + its
    # own GST rate). Add-on lines on the same invoice are their own lines, and
    # then the vehicle is read from its own VEHICLE line.
    out = im.derive_commercials(
        {
            "invoice_purpose": "VEHICLE_SALE",
            "taxable_amount": "1655593.22",
            "cgst_amount": "149003.39",
            "sgst_amount": "149003.39",
            "tcs_amount": "16000",
            "line_items": [
                {"line_category": "ACCESSORY_GENUINE", "net_amount": "25000"},
                {"line_category": "EXTENDED_WARRANTY", "net_amount": "12000"},
                {"line_category": "VEHICLE", "gross_amount": "1600000", "taxable_amount": "1600000",
                 "tax_rate": "18"},
            ],
        }
    )
    assert out == {
        "ex_showroom_price": Decimal("1888000.00"),
        "tcs_amount": Decimal(16000),
        "accessories_cost": Decimal(25000),
        "additional_warranty_amount": Decimal(12000),
    }


def test_derive_commercials_vehicle_without_taxes_has_no_ex_showroom() -> None:
    # Taxes not read on this invoice: never borrowed, never guessed.
    out = im.read_invoice("tax_invoice_tally", {"invoice_purpose": "VEHICLE_SALE", "taxable_amount": "1600000"})
    assert "ex_showroom_price" not in out["lines"]
    assert [i["code"] for i in out["issues"]] == ["TAXES_NOT_READ"]


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


def test_derive_commercials_vehicle_line_fallback_when_no_header_figures() -> None:
    out = im.derive_commercials(
        {
            "invoice_purpose": "VEHICLE_SALE",
            # A line rate alone counts only when the line's own tax confirms it.
            "line_items": [{"line_category": "VEHICLE", "gross_amount": "1450000", "taxable_amount": "1450000",
                            "tax_rate": "28", "tax_amount": "406000"}],
        }
    )
    assert out == {"ex_showroom_price": Decimal("1856000.00")}


def test_derive_discounts_invoice_level_is_unclassified_and_gst_inclusive() -> None:
    # A header discount names no scheme: its own key, not the consumer offer.
    out = im.derive_discounts({"invoice_purpose": "VEHICLE_SALE", "taxable_amount": "1000000",
                               "igst_amount": "180000", "invoice_discount_amount": "20000"})
    assert out == {"UNCLASSIFIED_DISCOUNT": Decimal("23600.00")}


def test_derive_discounts_header_and_lines_are_one_discount_by_type() -> None:
    out = im.derive_discounts(
        {
            "invoice_purpose": "VEHICLE_SALE",
            "taxable_amount": "1000000",
            "igst_amount": "180000",
            "invoice_discount_amount": "15000",
            "line_items": [
                {"line_category": "DISCOUNT_LINE", "description_raw": "Exchange Bonus", "net_amount": "-10000"},
                {"line_category": "DISCOUNT_LINE", "description_raw": "Consumer Offer", "net_amount": "-5000"},
                {"line_category": "VEHICLE", "net_amount": "1600000"},
            ],
        }
    )
    assert out == {"EXCHANGE_BONUS": Decimal("11800.00"), "CASH_DISCOUNT": Decimal("5900.00")}


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


def test_line_item_rows_salvages_legacy_python_repr_string() -> None:
    # Confirmed live (2026-09-24): a since-fixed Gemini adapter bug
    # serialized array field values via str() instead of json.dumps,
    # landing exactly this single-quoted, non-JSON text in
    # journey_document_extracted_fields for every array field extracted
    # before the fix. ast.literal_eval salvages it without a DI reprocess.
    legacy_value = (
        "[{'description_raw': 'MV Tax(One Time)', 'amount': 100252}, "
        "{'description_raw': 'Hypothecation Addition', 'amount': 1500}]"
    )
    rows = im._line_item_rows(legacy_value)
    assert rows == [
        {"description_raw": "MV Tax(One Time)", "amount": 100252},
        {"description_raw": "Hypothecation Addition", "amount": 1500},
    ]


def test_line_item_rows_rejects_unparseable_garbage() -> None:
    assert im._line_item_rows("not a list at all") == []
    assert im._line_item_rows("{'not': 'a list'}") == []


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
            "cgst_rate": "9", "cgst_amount": "144000", "sgst_rate": "9", "sgst_amount": "144000",
            "tcs_amount": "16000",
            "invoice_discount_amount": "20000",
            "grand_total_amount": "1904000",
            # With an add-on line the vehicle is read from its own line.
            "line_items": [{"line_category": "VEHICLE", "gross_amount": "1620000", "taxable_amount": "1600000"},
                           {"line_category": "EXTENDED_WARRANTY", "net_amount": "12000"}],
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

    # 2026-10-20 (R1): the vehicle line's selling price 16,20,000 x 1.18.
    ex = _commercial(c, "ex_showroom_price")
    assert ex["actual_amount"] == Decimal("1911600.00")
    assert ex["source_reference"].startswith("tax_invoice_tally:")
    assert ex["actual_source_kind"] == "EVIDENCE"
    assert _commercial(c, "tcs_amount")["actual_amount"] == Decimal(16000)
    assert _commercial(c, "additional_warranty_amount")["actual_amount"] == Decimal(12000)

    # A header discount names no scheme: its own key, GST-inclusive, the
    # printed pre-GST figure kept.
    disc = c.execute(
        text("SELECT actual_discount_amount, actual_source_kind, details "
             "FROM auditcore.discount_applications "
             "WHERE tenant_id=:t AND journey_id=:j AND discount_key='UNCLASSIFIED_DISCOUNT'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert disc["actual_discount_amount"] == Decimal(23600)
    assert disc["details"]["origin"] == "INVOICE_MATERIALIZATION"
    assert disc["details"]["printedBeforeGst"] == "20000.00"


def test_accessory_invoice_line_items_persist_and_round_trip(journey) -> None:
    """Regression check: reported live as accessory line items visible in the
    document's own raw extracted fields (confirmed non-empty, real
    ACCESSORY_GENUINE rows) but missing from the Journey 360 Accessories
    panel, which reads invoice_review_values.line_items via _invoices() in
    uc03_journey_overview_projection.py. derive_commercials/the ACCESSORY-
    purpose total path (test_accessory_invoice_projects_accessories_cost)
    doesn't touch this column at all -- it derives the total independently
    from grand_total_amount -- so a correct total there proves nothing about
    whether line_items itself survives the DB round trip. This asserts the
    actual persisted column directly, with the real shape from the live
    report (6 ACCESSORY_GENUINE rows: seat cover, wheel covers, roof rail,
    rear guard, chrome kit, spoiler)."""
    c = journey
    line_items = [
        {"item_code": "AC00819", "description_raw": "Dark theme- Seat cover with Armrest-7SF",
         "hsn_sac": "87089900", "quantity": 1, "unit_rate": 7885.59, "gross_amount": 7885.59,
         "discount_amount": 0.0, "taxable_amount": 7885.59, "tax_rate": 18.0, "tax_amount": 1419.42,
         "net_amount": 9305.0, "line_category": "ACCESSORY_GENUINE"},
        {"item_code": "AZ00111", "description_raw": "OE Wheel Cover - Z101",
         "hsn_sac": "87089900", "quantity": 4, "unit_rate": 807.63, "gross_amount": 3230.52,
         "discount_amount": 0.0, "taxable_amount": 3230.52, "tax_rate": 18.0, "tax_amount": 581.5,
         "net_amount": 3812.01, "line_category": "ACCESSORY_GENUINE"},
        {"item_code": "AC00625", "description_raw": "Roof Rail Set - Scorpio",
         "hsn_sac": "87089900", "quantity": 1, "unit_rate": 4166.95, "gross_amount": 4166.95,
         "discount_amount": 0.0, "taxable_amount": 4166.95, "tax_rate": 18.0, "tax_amount": 750.06,
         "net_amount": 4917.0, "line_category": "ACCESSORY_GENUINE"},
    ]
    im.materialize_reviewed_invoices(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_doc("accessory_invoice_dms", {
            "invoice_purpose": "ACCESSORY",
            "grand_total_amount": "17870",
            "line_items": line_items,
        })],
    )

    row = c.execute(
        text("SELECT line_items FROM auditcore.invoice_review_values "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    persisted = row["line_items"]
    assert persisted is not None
    assert len(persisted) == 3
    assert {item["item_code"] for item in persisted} == {"AC00819", "AZ00111", "AC00625"}
    assert all(item["line_category"] == "ACCESSORY_GENUINE" for item in persisted)

    from audit_core import uc03_journey_overview_projection as projection
    projected = projection._invoices(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    assert len(projected) == 1
    assert len(projected[0]["lineItems"]) == 3
    assert {item["item_code"] for item in projected[0]["lineItems"]} == {"AC00819", "AZ00111", "AC00625"}


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


def test_accessory_invoice_replaces_booking_form_accessories_cost(journey) -> None:
    """Regression: accessory/EW/RSA invoices were missing from the priority
    list used to rank commercial-line sources, so once a booking-form value
    existed they could never win — silently losing to a weaker source."""
    c = journey
    c.execute(
        text("INSERT INTO auditcore.commercial_lines "
             "(tenant_id, journey_id, component_key, actual_amount, actual_source_kind, source_reference) "
             "VALUES (:t, :j, 'accessories_cost', 30000, 'EVIDENCE', :ref)"),
        {"t": c.tenant_id, "j": c.journey_id, "ref": f"booking_form:{uuid4()}"},
    )
    im.materialize_reviewed_invoices(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_doc("accessory_invoice_dms", {
            "invoice_purpose": "ACCESSORY", "grand_total_amount": "25000",
        })],
    )
    accessories = _commercial(c, "accessories_cost")
    assert accessories["actual_amount"] == Decimal(25000)
    assert accessories["source_reference"].startswith("accessory_invoice_dms:")


def test_ew_invoice_replaces_booking_form_warranty_amount(journey) -> None:
    c = journey
    c.execute(
        text("INSERT INTO auditcore.commercial_lines "
             "(tenant_id, journey_id, component_key, actual_amount, actual_source_kind, source_reference) "
             "VALUES (:t, :j, 'additional_warranty_amount', 10000, 'EVIDENCE', :ref)"),
        {"t": c.tenant_id, "j": c.journey_id, "ref": f"booking_form:{uuid4()}"},
    )
    im.materialize_reviewed_invoices(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_doc("ew_invoice", {
            "invoice_purpose": "EXTENDED_WARRANTY", "grand_total_amount": "18000",
        })],
    )
    warranty = _commercial(c, "additional_warranty_amount")
    assert warranty["actual_amount"] == Decimal(18000)
    assert warranty["source_reference"].startswith("ew_invoice:")


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
            "invoice_purpose": "VEHICLE_SALE", "taxable_amount": "1600000", "cgst_amount": "144000", "sgst_amount": "144000",
        })],
    )
    ex = _commercial(c, "ex_showroom_price")
    assert ex["actual_amount"] == Decimal("1888000.00")
    assert ex["source_reference"].startswith("tax_invoice_tally:")


def test_copies_of_one_invoice_count_once_and_each_keeps_its_own_source_row(journey) -> None:
    """2026-10-20 (R2): the Tally and a generic copy of one invoice (same
    number) are one invoice; the Tally copy stands for it (canonical type
    priority). Each copy's own figure is still recorded per document."""
    c = journey
    strong = _doc("tax_invoice_tally", {"invoice_purpose": "VEHICLE_SALE", "invoice_number": "INV-9",
                                        "taxable_amount": "1600000", "cgst_amount": "144000", "sgst_amount": "144000"})
    weak = _doc("invoice_generic", {"invoice_purpose": "VEHICLE_SALE", "invoice_number": "INV-9",
                                    "taxable_amount": "999999", "igst_amount": "179999.82"})
    im.materialize_reviewed_invoices(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester", documents=[strong, weak],
    )
    assert _commercial(c, "ex_showroom_price")["actual_amount"] == Decimal("1888000.00")

    rows = c.execute(
        text("SELECT source_document_type, amount FROM auditcore.commercial_line_source_values "
             "WHERE tenant_id=:t AND journey_id=:j AND line_kind='COMMERCIAL' AND component_key='ex_showroom_price'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().all()
    by_source = {r["source_document_type"]: r["amount"] for r in rows}
    assert by_source == {
        "tax_invoice_tally": Decimal("1888000.00"),
        "invoice_generic": Decimal("1179998.82"),
    }


def _finding(c, rule_key):
    return c.execute(
        text("SELECT audit_finding_id, finding_class, severity, finding_status "
             "FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j AND rule_key=:k "
             "ORDER BY created_at_utc DESC LIMIT 1"),
        {"t": c.tenant_id, "j": c.journey_id, "k": rule_key},
    ).mappings().one_or_none()


def _vehicle(document_type: str, taxable: str, number: str = "INV-9"):
    gst = str(Decimal(taxable) * Decimal("0.09"))
    return _doc(document_type, {"invoice_purpose": "VEHICLE_SALE", "invoice_number": number,
                                "taxable_amount": taxable, "cgst_amount": gst, "sgst_amount": gst})


def test_copies_of_one_invoice_disagreeing_raise_a_high_finding_and_tl_task(journey) -> None:
    """Two copies of the dealer's own invoice disagreeing is a red flag --
    explicit instruction: never silently pick a winner, always raise a HIGH
    finding + a Team Lead task to validate. 2026-10-20 (R2): copies are
    compared with each other (same invoice number), never two different
    invoices as if they were one."""
    c = journey
    im.materialize_reviewed_invoices(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_vehicle("tax_invoice_tally", "1600000"), _vehicle("invoice_generic", "999999")],
    )
    rule_key = "INVOICE_COPIES_DISAGREE:VEHICLE|N|1NV9"
    finding = _finding(c, rule_key)
    assert finding is not None
    assert finding["finding_class"] == "VIOLATION"
    assert finding["severity"] == "HIGH"
    assert finding["finding_status"] == "OPEN"

    task = c.execute(
        text("SELECT task_type, assigned_role_code, severity, related_finding_id "
             "FROM auditcore.workflow_tasks WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert task["task_type"] == "INVOICE_DISCREPANCY_REVIEW"
    assert task["assigned_role_code"] == "TL"
    assert task["severity"] == "HIGH"
    assert task["related_finding_id"] == finding["audit_finding_id"]


def test_two_different_vehicle_invoices_are_flagged_not_compared(journey) -> None:
    c = journey
    im.materialize_reviewed_invoices(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_vehicle("tax_invoice_tally", "1600000", "INV-1"), _vehicle("tax_invoice_tally", "1500000", "INV-2")],
    )
    assert _finding(c, "MULTIPLE_VEHICLE_INVOICES")["finding_status"] == "OPEN"
    assert _finding(c, "INVOICE_COPIES_DISAGREE:VEHICLE|N|1NV1") is None


def test_an_invoice_that_does_not_add_up_is_a_pc_review_task(journey) -> None:
    c = journey
    im.materialize_reviewed_invoices(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_doc("customer_invoice_dms", {"invoice_purpose": "VEHICLE_SALE", "invoice_number": "D-1",
                                                 "taxable_amount": "1600000"})],
    )
    task = c.execute(
        text("SELECT task_type, assigned_role_code FROM auditcore.workflow_tasks WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert task["task_type"] == "INVOICE_FIELD_REVIEW" and task["assigned_role_code"] == "PC"
    assert _commercial(c, "ex_showroom_price") is None  # taxes not read: never guessed


def test_agreeing_invoices_do_not_raise_a_discrepancy(journey) -> None:
    c = journey
    im.materialize_reviewed_invoices(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_vehicle("tax_invoice_tally", "1600000"), _vehicle("customer_invoice_dms", "1600000")],
    )
    assert _finding(c, "INVOICE_COPIES_DISAGREE:VEHICLE|N|1NV9") is None


def test_wholesale_invoice_excluded_from_discrepancy_check(journey) -> None:
    """Wholesale (dealer<->OEM) invoices are a different transaction entirely
    -- comparing their figures against a customer-facing invoice would just
    manufacture a false discrepancy, and they never bill the customer."""
    c = journey
    im.materialize_reviewed_invoices(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        documents=[_vehicle("tax_invoice_tally", "1600000"), _vehicle("wholesale_invoice", "1450000")],
    )
    assert _finding(c, "INVOICE_COPIES_DISAGREE:VEHICLE|N|1NV9") is None
    assert _finding(c, "MULTIPLE_VEHICLE_INVOICES") is None
    assert _commercial(c, "ex_showroom_price")["source_reference"].startswith("tax_invoice_tally:")


def test_discrepancy_resolves_once_invoices_agree(journey) -> None:
    c = journey
    tally, generic = _vehicle("tax_invoice_tally", "1600000"), _vehicle("invoice_generic", "999999")
    im.materialize_reviewed_invoices(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester", documents=[tally, generic],
    )
    rule_key = "INVOICE_COPIES_DISAGREE:VEHICLE|N|1NV9"
    assert _finding(c, rule_key)["finding_status"] == "OPEN"

    # The corrected generic copy now reports the same value.
    corrected = _vehicle("invoice_generic", "1600000")
    corrected.documentId = generic.documentId
    im.materialize_reviewed_invoices(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester", documents=[corrected],
    )
    assert _finding(c, rule_key)["finding_status"] == "RESOLVED"


def test_idempotent(journey) -> None:
    c = journey
    docs = [_doc("tax_invoice_tally", {
        "invoice_purpose": "VEHICLE_SALE", "taxable_amount": "1600000", "cgst_amount": "144000", "sgst_amount": "144000",
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
