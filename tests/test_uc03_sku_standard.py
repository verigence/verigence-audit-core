"""The SKU standard API (decision 2026-09-30): every master line for one
vehicle on one date. Requires DATABASE_URL."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from p2_support import AllowAllAuthorization
from test_oem_price_masters import _price_bytes, connection  # noqa: F401  (fixture)

from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.oem_master_parsers import (
    CorporateBenefitRow,
    CorporateCompany,
    DiscountRow,
    GridRow,
    ParseResult,
    parse_price_list,
)
from audit_core.oem_price_masters import (
    _alias_map,
    _project_oem,
    ingest_corporate_policy,
    ingest_discount_document,
    ingest_discount_grid,
    ingest_price_list,
)
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_sku_standard import (
    catalogue_rows,
    price_version_on,
    search_sku,
    standard_for_sku,
)

ON = date(2026, 9, 15)


def _load_masters(conn) -> str:
    tenant_id = conn.tenant_id
    project = _project_oem(conn, tenant_id)
    common = {"connection": conn, "tenant_id": tenant_id, "oem_id": project["oem_id"], "actor_id": "admin"}
    ingest_price_list(
        effective_from=date(2026, 9, 1),
        parsed=parse_price_list(_price_bytes(("THAR ROXX", "MX1 PMT 2WD", 1_000_000), ("THAR ROXX", "MX5 DAT 4WD", 1_800_000),
                                             ("XUV 3XO", "MX1", 800_000))),
        **common,
    )
    consumer = ParseResult(kind="CONSUMER_SCHEME")
    consumer.discount_rows = [DiscountRow(1, "CONSUMER", "Thar Roxx", [], [("CASH_DISCOUNT", Decimal(30000)), ("ACCESSORIES_KIT", Decimal(5000))],
                                          Decimal(35000), {"section": "CONSUMER"})]
    ingest_discount_document(oem_code="MAHINDRA", master_kind="CONSUMER_SCHEME", effective_from=date(2026, 9, 1), parsed=consumer, **common)
    exchange = ParseResult(kind="EXCHANGE_SCHEME")
    exchange.discount_rows = [
        DiscountRow(1, "EXCHANGE", "Thar Roxx", [], [("EXCHANGE_BONUS", Decimal(25000))], Decimal(25000),
                    {"section": "EXCHANGE_PERSONAL", "mAndMContribution": "15000", "dealerContribution": "10000"}),
        DiscountRow(2, "SCRAPPAGE", "Thar Roxx", [], [("SCRAPPAGE_BONUS_DEALER", Decimal(35000))], Decimal(35000),
                    {"section": "SCRAPPAGE_DEALER"}),
    ]
    ingest_discount_document(oem_code="MAHINDRA", master_kind="EXCHANGE_SCHEME", effective_from=date(2026, 9, 1), parsed=exchange, **common)
    corporate = ParseResult(kind="CORPORATE_POLICY")
    corporate.corporate_benefits = [
        CorporateBenefitRow(1, "Z", "Thar Roxx", Decimal(10000), Decimal(5000), Decimal(15000)),
        CorporateBenefitRow(2, "A", "Thar Roxx", Decimal(4000), Decimal(2000), Decimal(6000)),
    ]
    corporate.corporate_companies = [CorporateCompany("CORP-0001", "Acme Steel Ltd", "PSU", "Z")]
    ingest_corporate_policy(oem_code="MAHINDRA", effective_from=date(2026, 9, 1), parsed=corporate, upload_id=None, **common)
    grid = ParseResult(kind="DISCOUNT_GRID")
    grid.grid_rows = [GridRow(3, "Thar Roxx", ["Thar Roxx"], True, 60, Decimal(5000), Decimal(60), Decimal(3000))]
    ingest_discount_grid(oem_code="MAHINDRA", effective_from=date(2026, 9, 1), parsed=grid, upload_id=None, **common)
    return tenant_id


def test_the_standard_answers_every_block_for_one_vehicle_on_a_date(connection) -> None:  # noqa: F811
    tenant_id = _load_masters(connection)
    version = price_version_on(connection, tenant_id=tenant_id, on=ON)
    rows = catalogue_rows(connection, tenant_id=tenant_id, price_list_version_id=version["priceListVersionId"])
    aliases = _alias_map(connection, "MAHINDRA")
    assert search_sku(rows, alias_map=aliases, model="Thar Roxx")["matched"] == "AMBIGUOUS"
    assert len(search_sku(rows, alias_map=aliases, model="Thar Roxx")["candidates"]) == 2
    assert search_sku(rows, alias_map=aliases, model="Nexon")["matched"] == "NONE"
    found = search_sku(rows, alias_map=aliases, model="Thar Roxx", variant="mx1 pmt 2wd")
    assert found["matched"] == "UNIQUE" and found["sku"]["skuCode"] == "THAR_ROXX::MX1 PMT 2WD"
    assert search_sku(rows, alias_map=aliases, sku_code="THAR_ROXX::MX1 PMT 2WD")["matched"] == "EXACT"

    standard = standard_for_sku(
        connection, tenant_id=tenant_id, on=ON, row=found["row"], version=version, basis="INDIVIDUAL",
        corporate_code="CORP-0001", exchange="EXCHANGE", quantity=2,
    )
    assert standard["unknown"] == []
    price = standard["priceList"]
    assert price["version"] == 1 and len(price["components"]) == 10
    ex = next(c for c in price["components"] if c["key"] == "EX_SHOWROOM")
    assert ex["amount"] == "1000000.00" and ex["priceSince"] == "2026-09-01" and ex["commercialKey"] == "ex_showroom_price"
    # decision 2026-10-06: one warranty tier at most; none is the default, so neither tier (300, 400) is in the total
    assert price["onRoad"] == {"individual": "1005900.00", "corporate": "1005900.00", "basis": "INDIVIDUAL", "ew": "NONE",
                               "amount": "1005900.00"}
    assert standard["consumerScheme"]["total"] == "35000.00"
    assert {b["key"] for b in standard["consumerScheme"]["benefits"]} == {"CASH_DISCOUNT", "ACCESSORIES_KIT"}
    exchange = standard["exchangeScheme"]
    assert {b["key"] for b in exchange["benefits"]} == {"EXCHANGE_BONUS", "SCRAPPAGE_BONUS_DEALER"}
    assert [b["key"] for b in exchange["applicable"]] == ["EXCHANGE_BONUS"] and exchange["applicableMax"] == "25000.00"
    corporate = standard["corporate"]
    assert corporate["range"] == {"min": "6000.00", "max": "15000.00"}
    assert corporate["corporate"]["found"] is True and corporate["corporate"]["privilegeCategory"] == "Z"
    assert corporate["exact"]["amount"] == "15000.00"
    assert standard["grid"]["bookingProtectionDays"] == 60 and standard["grid"]["insuranceOdPercentMax"] == "60.0000"
    assert standard["summary"] == {
        "onRoad": "1005900.00", "consumerBenefits": "35000.00", "exchangeBenefit": "25000.00",
        "corporateBenefit": "15000.00", "standardNet": "930900.00", "standardNetForQuantity": "1861800.00",
    }

    # No corporate named: the range stands and nothing is subtracted for it.
    plain = standard_for_sku(connection, tenant_id=tenant_id, on=ON, row=found["row"], version=version)
    assert plain["corporate"]["exact"] is None and plain["summary"]["corporateBenefit"] is None
    assert plain["summary"]["standardNet"] == "970900.00" and plain["exchangeScheme"]["applicable"] == []
    # A model no scheme or grid names: the blocks are named unknown, never guessed.
    xuv = search_sku(rows, alias_map=aliases, model="XUV 3XO", variant="MX1")["row"]
    assert standard_for_sku(connection, tenant_id=tenant_id, on=ON, row=xuv, version=version)["unknown"] == [
        "consumerScheme", "exchangeScheme", "corporate", "grid"]


def test_the_standard_routes_answer_over_http(connection) -> None:  # noqa: F811
    tenant_id = _load_masters(connection)
    connection.commit()  # the API answers over its own connection
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject="tl-1")
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    try:
        client = TestClient(app, raise_server_exceptions=False)
        base = f"/p2/v1/tenants/{tenant_id}/standard"
        catalogue = client.get(f"{base}/catalogue", params={"on": "2026-09-15"})
        assert catalogue.status_code == 200, catalogue.text
        assert [m["model"] for m in catalogue.json()["models"]] == ["THAR ROXX", "XUV 3XO"]
        answer = client.get(f"{base}/sku", params={"on": "2026-09-15", "model": "Thar Roxx", "variant": "MX1 PMT 2WD",
                                                    "registrationBasis": "CORPORATE"})
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["matched"] == "UNIQUE" and body["priceList"]["onRoad"]["basis"] == "CORPORATE"
        assert body["summary"]["standardNet"] == "970900.00"
        nothing = client.get(f"{base}/sku", params={"on": "2026-01-15", "model": "Thar Roxx"})
        assert nothing.status_code == 200 and nothing.json()["matched"] == "NONE" and "No price list" in nothing.json()["reason"]
        assert client.get(f"{base}/sku", params={"on": "2026-09-15"}).status_code == 422
        sheet = client.get(f"{base}/price-sheet", params={"on": "2026-09-15", "model": "thar"})
        assert sheet.status_code == 200, sheet.text
        listed = sheet.json()
        assert listed["total"] == 2 and listed["truncated"] is False and listed["sourceFiles"] == []
        assert sorted(v["standard"]["exShowroom"] for v in listed["vehicles"]) == ["1000000.00", "1800000.00"]
        assert all(v["standard"]["wefDate"] == "2026-09-01" for v in listed["vehicles"])
        assert client.get(f"{base}/price-sheet", params={"on": "2026-09-15", "model": "thar", "variant": "mx5"}).json()["total"] == 1
        assert client.get(f"{base}/price-sheet", params={"on": "2026-01-15"}).json()["vehicles"] == []
        versions = client.get(f"{base}/price-versions")
        assert versions.status_code == 200, versions.text
        listed_versions = versions.json()["versions"]
        assert [v["effectiveFrom"] for v in listed_versions] == ["2026-09-01"]  # newest first
        assert listed_versions[0]["version"] == 1 and listed_versions[0]["status"] in ("PUBLISHED", "RETIRED")
        assert set(listed_versions[0]) == {"priceListVersionId", "priceList", "version", "effectiveFrom", "effectiveTo",
                                           "status", "sourceFiles"}
    finally:
        app.dependency_overrides.clear()
