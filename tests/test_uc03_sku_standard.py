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
        corporate_code="CORP-0001", quantity=2,
    )
    assert standard["unknown"] == []
    price = standard["priceList"]
    assert price["version"] == 1 and len(price["components"]) == 10
    ex = next(c for c in price["components"] if c["key"] == "EX_SHOWROOM")
    assert ex["amount"] == "1000000.00" and ex["priceSince"] == "2026-09-01" and ex["commercialKey"] == "ex_showroom_price"
    assert price["onRoad"] == {"individual": "1006600.00", "corporate": "1006600.00", "basis": "INDIVIDUAL", "amount": "1006600.00"}
    assert standard["consumerScheme"]["total"] == "35000.00"
    assert {b["key"] for b in standard["consumerScheme"]["benefits"]} == {"CASH_DISCOUNT", "ACCESSORIES_KIT"}

    # Every scenario is answered, not one the caller picked: scrappage is
    # visible beside exchange without being asked for.
    exchange = standard["exchangeScheme"]
    assert {b["key"] for b in exchange["benefits"]} == {"EXCHANGE_BONUS", "SCRAPPAGE_BONUS_DEALER"}
    by_scenario = exchange["byScenario"]
    assert set(by_scenario) == {"EXCHANGE", "SCRAPPAGE_DEALER", "SCRAPPAGE_COD", "WELCOME"}
    assert [b["key"] for b in by_scenario["EXCHANGE"]["benefits"]] == ["EXCHANGE_BONUS"]
    assert by_scenario["EXCHANGE"]["best"] == "25000.00"
    assert [b["key"] for b in by_scenario["SCRAPPAGE_DEALER"]["benefits"]] == ["SCRAPPAGE_BONUS_DEALER"]
    assert by_scenario["SCRAPPAGE_DEALER"]["best"] == "35000.00"
    assert by_scenario["WELCOME"] == {"benefits": [], "best": None}

    corporate = standard["corporate"]
    assert corporate["range"] == {"min": "6000.00", "max": "15000.00"}
    assert corporate["corporate"]["found"] is True and corporate["corporate"]["privilegeCategory"] == "Z"
    assert corporate["exact"]["amount"] == "15000.00"
    assert standard["grid"]["bookingProtectionDays"] == 60 and standard["grid"]["insuranceOdPercentMax"] == "60.0000"

    # The price stands exactly as the price list states it: no benefit is
    # netted off and there is no "net" figure to mistake for one.
    assert standard["summary"] == {"onRoad": "1006600.00"}

    # No corporate named: the range still stands, and the price is unchanged.
    plain = standard_for_sku(connection, tenant_id=tenant_id, on=ON, row=found["row"], version=version)
    assert plain["corporate"]["exact"] is None and plain["corporate"]["range"] == {"min": "6000.00", "max": "15000.00"}
    assert plain["summary"] == {"onRoad": "1006600.00"}
    # A model no scheme or grid names: the blocks are named unknown, never guessed.
    xuv = search_sku(rows, alias_map=aliases, model="XUV 3XO", variant="MX1")["row"]
    assert standard_for_sku(connection, tenant_id=tenant_id, on=ON, row=xuv, version=version)["unknown"] == [
        "consumerScheme", "exchangeScheme", "corporate", "grid"]


def test_a_new_months_bulletin_replaces_the_last_one(connection) -> None:  # noqa: F811
    """The dealer is offered what the current bulletin says and nothing
    else: October's consumer scheme replaces September's outright, and a
    scheme withdrawn in October stops being on offer.

    Regression. A withdrawal is recorded by publishing a version carrying
    no benefit rows (``_tombstone_absent_schemes``). Every still-open
    published version used to be read, and an empty version shadows
    nothing, so the withdrawn scheme's September benefits kept answering
    for October dates -- for ever. September itself must keep answering
    with September's figures.
    """
    tenant_id = _load_masters(connection)
    project = _project_oem(connection, tenant_id)
    common = {"connection": connection, "tenant_id": tenant_id, "oem_id": project["oem_id"], "actor_id": "admin"}

    # September: Thar Roxx has a consumer scheme worth 35,000 (see _load_masters).
    version = price_version_on(connection, tenant_id=tenant_id, on=ON)
    rows = catalogue_rows(connection, tenant_id=tenant_id, price_list_version_id=version["priceListVersionId"])
    aliases = _alias_map(connection, "MAHINDRA")
    thar = search_sku(rows, alias_map=aliases, model="Thar Roxx", variant="mx1 pmt 2wd")["row"]
    sep = standard_for_sku(connection, tenant_id=tenant_id, on=ON, row=thar, version=version)
    assert sep["consumerScheme"]["total"] == "35000.00"

    # October's bulletin prices the same vehicle lower: it REPLACES
    # September's figure rather than standing beside it.
    october = ParseResult(kind="CONSUMER_SCHEME")
    october.discount_rows = [DiscountRow(1, "CONSUMER", "Thar Roxx", [], [("CASH_DISCOUNT", Decimal(20000))],
                                         Decimal(20000), {"section": "CONSUMER"})]
    ingest_discount_document(oem_code="MAHINDRA", master_kind="CONSUMER_SCHEME",
                             effective_from=date(2026, 10, 1), parsed=october, **common)

    on_october = date(2026, 10, 15)
    oct_version = price_version_on(connection, tenant_id=tenant_id, on=on_october)
    replaced = standard_for_sku(connection, tenant_id=tenant_id, on=on_october, row=thar, version=oct_version)
    assert replaced["consumerScheme"]["total"] == "20000.00"
    assert {b["key"] for b in replaced["consumerScheme"]["benefits"]} == {"CASH_DISCOUNT"}
    # September's accessories benefit is gone, not carried alongside.
    assert standard_for_sku(connection, tenant_id=tenant_id, on=ON, row=thar, version=version)["consumerScheme"]["total"] == "35000.00"

    # November's bulletin names the vehicle nowhere: the scheme is withdrawn.
    november = ParseResult(kind="CONSUMER_SCHEME")
    november.discount_rows = []
    ingest_discount_document(oem_code="MAHINDRA", master_kind="CONSUMER_SCHEME",
                             effective_from=date(2026, 11, 1), parsed=november, **common)

    on_november = date(2026, 11, 15)
    nov_version = price_version_on(connection, tenant_id=tenant_id, on=on_november)
    withdrawn = standard_for_sku(connection, tenant_id=tenant_id, on=on_november, row=thar, version=nov_version)
    assert withdrawn["consumerScheme"] is None
    assert "consumerScheme" in withdrawn["unknown"]

    # Every earlier month still answers with its own figure.
    assert standard_for_sku(connection, tenant_id=tenant_id, on=on_october, row=thar, version=oct_version)["consumerScheme"]["total"] == "20000.00"
    assert standard_for_sku(connection, tenant_id=tenant_id, on=ON, row=thar, version=version)["consumerScheme"]["total"] == "35000.00"


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
        # The price list's own corporate on-road, with nothing netted off it.
        assert body["summary"] == {"onRoad": "1006600.00"}
        # Scrappage is answered without the caller naming a scenario.
        assert body["exchangeScheme"]["byScenario"]["SCRAPPAGE_DEALER"]["best"] == "35000.00"
        nothing = client.get(f"{base}/sku", params={"on": "2026-01-15", "model": "Thar Roxx"})
        assert nothing.status_code == 200 and nothing.json()["matched"] == "NONE" and "No price list" in nothing.json()["reason"]
        assert client.get(f"{base}/sku", params={"on": "2026-09-15"}).status_code == 422
    finally:
        app.dependency_overrides.clear()
