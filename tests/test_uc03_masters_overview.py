"""What has been loaded, and every offer standing on a date (2026-10-23).
Requires DATABASE_URL."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from p2_support import AllowAllAuthorization
from sqlalchemy import text
from test_oem_price_masters import _price_bytes, connection  # noqa: F401  (fixture)
from test_uc03_sku_standard import _load_masters

from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.oem_master_parsers import DiscountRow, ParseResult, parse_price_list
from audit_core.oem_price_masters import (
    _project_oem,
    ingest_discount_document,
    ingest_price_list,
)
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_masters_overview import masters_loaded_payload, offers_on

ON = date(2026, 9, 15)


def _upload(conn, tenant_id: str, kind: str, name: str, effective_from: date, *, stored: bool = True,
            price_list_version_id: str | None = None) -> str:
    return str(conn.execute(
        text("""INSERT INTO auditcore.oem_master_uploads
                  (tenant_id, oem_code, master_kind, source_filename, source_sha256, effective_from, status,
                   row_counts, uploaded_by_actor_id, published_at_utc, source_object_key, price_list_version_id)
                VALUES (:t, 'MAHINDRA', :k, :n, :sha, :eff, 'PUBLISHED', '{}'::jsonb, 'admin', now(), :key,
                        CAST(:plv AS uuid))
                RETURNING upload_id"""),
        {"t": tenant_id, "k": kind, "n": name, "sha": f"{abs(hash(name)):064x}"[:64], "eff": effective_from,
         "key": f"masters/{name}" if stored else None, "plv": price_list_version_id},
    ).scalar_one())


def test_every_offer_standing_on_a_date_for_every_model(connection) -> None:  # noqa: F811
    tenant_id = _load_masters(connection)
    book = offers_on(connection, tenant_id=tenant_id, on=ON)

    (consumer,) = book["consumer"]
    assert consumer["model"] == "THAR ROXX" and consumer["variant"] is None
    assert {b["key"]: b["amount"] for b in consumer["benefits"]} == {
        "CASH_DISCOUNT": "30000.00", "ACCESSORIES_KIT": "5000.00"}
    assert consumer["total"] == "35000.00"

    # Exchange and scrappage lines both listed, each with its own section;
    # alternatives are never summed.
    assert sorted((e["key"], e["section"], e["amount"]) for e in book["exchange"]) == [
        ("EXCHANGE_BONUS", "EXCHANGE_PERSONAL", "25000.00"),
        ("SCRAPPAGE_BONUS_DEALER", "SCRAPPAGE_DEALER", "35000.00"),
    ]
    exchange = next(e for e in book["exchange"] if e["key"] == "EXCHANGE_BONUS")
    assert exchange["contributions"] == {"mAndMContribution": "15000", "dealerContribution": "10000"}

    (corporate,) = book["corporate"]
    assert corporate["model"] == "THAR ROXX"
    assert corporate["byCategory"] == {"Z": "15000.00", "A": "6000.00"}
    assert book["corporateCategories"] == ["A", "Z"]

    grid = book["grid"]
    assert grid["effectiveFrom"] == date(2026, 9, 1)
    (line,) = grid["lines"]
    assert line["modelAsWritten"] == "Thar Roxx" and line["bookingProtectionDays"] == 60
    assert line["agreedBuffer"] == "5000.00" and line["insuranceOdPercentMax"] == "60.0000"
    assert book["notApplied"] == []

    # Before any master: nothing, and no grid -- never a later book.
    empty = offers_on(connection, tenant_id=tenant_id, on=date(2026, 8, 1))
    assert empty["consumer"] == [] and empty["exchange"] == [] and empty["grid"] is None


def test_a_line_for_variants_the_book_does_not_carry_is_named_not_offered(connection) -> None:  # noqa: F811
    """Review of PR 472, S9: a model-level line stored for variants the price
    book did not carry is not an offer on any variant. The deal skips it; this
    view lists it under notApplied with the variant texts instead of hiding it."""
    tenant_id = _load_masters(connection)
    project = _project_oem(connection, tenant_id)
    october = ParseResult(kind="CONSUMER_SCHEME")
    october.discount_rows = [DiscountRow(1, "CONSUMER", "Thar Roxx", ["AX9 NOT IN THE BOOK"],
                                         [("CASH_DISCOUNT", Decimal(12000))], Decimal(12000),
                                         {"section": "CONSUMER"})]
    ingest_discount_document(connection=connection, tenant_id=tenant_id, oem_id=project["oem_id"],
                             actor_id="admin", oem_code="MAHINDRA", master_kind="CONSUMER_SCHEME",
                             effective_from=date(2026, 10, 3), parsed=october)

    book = offers_on(connection, tenant_id=tenant_id, on=date(2026, 10, 15))
    # October's bulletin replaced September's for the scheme it names; the
    # unmatched line is not offered on any variant.
    assert all(b["amount"] != "12000.00" for c in book["consumer"] for b in c["benefits"])
    assert any("AX9 NOT IN THE BOOK" in str(n["variantTexts"]) for n in book["notApplied"])


def test_masters_loaded_lists_every_version_with_its_document(connection) -> None:  # noqa: F811
    tenant_id = _load_masters(connection)
    project = _project_oem(connection, tenant_id)
    ingest_price_list(connection=connection, tenant_id=tenant_id, oem_id=project["oem_id"], actor_id="admin",
                      effective_from=date(2026, 10, 3),
                      parsed=parse_price_list(_price_bytes(("THAR ROXX", "MX1 PMT 2WD", 1_050_000))))
    loaded = masters_loaded_payload(connection, tenant_id=tenant_id)
    september, october = loaded["priceLists"]
    # A version answers until the day before the next one starts.
    assert september["effectiveFrom"] == date(2026, 9, 1) and september["inForceTo"] == date(2026, 10, 2)
    assert october["inForceTo"] is None and september["answers"] and october["answers"]
    assert september["skus"] == 3 and september["document"] is None

    _upload(connection, tenant_id, "PRICE_LIST", "Mahindra_Consolidated_Price_List_01Sep2026.xlsx",
            date(2026, 9, 1), price_list_version_id=september["priceListVersionId"])
    _upload(connection, tenant_id, "CONSUMER_SCHEME", "Consumer Scheme Sep'26.pdf", date(2026, 9, 1))
    _upload(connection, tenant_id, "DISCOUNT_GRID", "Odisha PV & CV Discount Grid-1 Aug 2026.xlsx",
            date(2026, 9, 1), stored=False)
    loaded = masters_loaded_payload(connection, tenant_id=tenant_id)

    assert loaded["priceLists"][0]["document"]["fileName"] == "Mahindra_Consolidated_Price_List_01Sep2026.xlsx"
    assert loaded["priceLists"][0]["document"]["openable"] is True
    kinds = {(s["kind"], s["effectiveFrom"]): s for s in loaded["schemes"]}
    assert set(kinds) == {("CONSUMER_SCHEME", date(2026, 9, 1)), ("EXCHANGE_SCHEME", date(2026, 9, 1)),
                          ("CORPORATE_POLICY", date(2026, 9, 1))}
    assert kinds[("CONSUMER_SCHEME", date(2026, 9, 1))]["document"]["fileName"] == "Consumer Scheme Sep'26.pdf"
    # A master loaded with no document on record says so rather than borrowing one.
    assert kinds[("CORPORATE_POLICY", date(2026, 9, 1))]["document"] is None
    (grid,) = loaded["grids"]
    assert grid["lines"] == 1 and grid["document"]["openable"] is False


def test_the_overview_routes_answer_over_http(connection) -> None:  # noqa: F811
    tenant_id = _load_masters(connection)
    connection.commit()  # the API answers over its own connection
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject="pc-1")
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    try:
        client = TestClient(app, raise_server_exceptions=False)
        base = f"/p2/v1/tenants/{tenant_id}/standard"
        offers = client.get(f"{base}/offers", params={"on": "2026-09-15"})
        assert offers.status_code == 200, offers.text
        assert offers.json()["consumer"][0]["total"] == "35000.00"
        loaded = client.get(f"{base}/masters-loaded")
        assert loaded.status_code == 200, loaded.text
        assert [p["effectiveFrom"] for p in loaded.json()["priceLists"]] == ["2026-09-01"]
        # A date is required (the API answers a missing field 400, VAC-VAL-001).
        assert client.get(f"{base}/offers").status_code == 400
    finally:
        app.dependency_overrides.clear()
