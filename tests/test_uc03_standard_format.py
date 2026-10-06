"""The SKU standard's full format (decision 2026-10-06): one shape for every OEM, one warranty tier at most in
the on-road price, hypothecation as its own field, both insurances where a vehicle has them, minimum booking
and extra charges as rows, the WEF date. Requires DATABASE_URL."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook
from test_oem_master_parsers import (
    _THAR_NOTES,
    _dealer_ev_workbook,
    _dealer_pv_workbook,
    _dealer_trim_workbook,
    _ev_row,
    _pv_row,
    _trim_row,
)
from test_oem_price_masters import _HEADER, connection  # noqa: F401  (fixture)

from audit_core.oem_master_parsers import parse_price_list
from audit_core.oem_price_masters import _alias_map, _project_oem, ingest_price_list
from audit_core.uc03_sku_standard import (
    catalogue_rows,
    on_road_for,
    price_version_on,
    search_sku,
    standard_for_sku,
)

ON = date(2026, 9, 25)


def _ingest(conn, content: bytes, effective: date) -> None:
    project = _project_oem(conn, conn.tenant_id)
    ingest_price_list(
        connection=conn, tenant_id=conn.tenant_id, oem_id=project["oem_id"], effective_from=effective,
        parsed=parse_price_list(content), actor_id="admin",
    )


def _standard(conn, *, model: str, variant: str, on: date = ON, **kwargs):
    version = price_version_on(conn, tenant_id=conn.tenant_id, on=on)
    rows = catalogue_rows(conn, tenant_id=conn.tenant_id, price_list_version_id=version["priceListVersionId"])
    found = search_sku(rows, alias_map=_alias_map(conn, "MAHINDRA"), model=model, variant=variant,
                       insurance_type=kwargs.get("insurance_type"))
    assert found["matched"] == "UNIQUE", found
    return standard_for_sku(conn, tenant_id=conn.tenant_id, on=on, row=found["row"], version=version, catalogue=rows,
                            **{k: v for k, v in kwargs.items() if k != "insurance_type"})


def test_the_pure_on_road_takes_one_warranty_tier_at_most() -> None:
    amounts = {
        "EX_SHOWROOM": Decimal(1000), "TCS": Decimal(10), "INSURANCE": Decimal(50), "EXT_WARRANTY_4TH_YR": Decimal(20),
        "EXT_WARRANTY_4TH_5TH_YR": Decimal(35), "ACCESSORIES_KIT": Decimal(30), "RSA_1YR": Decimal(2), "FASTAG": Decimal(1),
        "REGISTRATION_INDIVIDUAL": Decimal(100), "REGISTRATION_CORPORATE": Decimal(100), "MIN_BOOKING_AMOUNT": Decimal(21000),
        "EXTRA_CHARGE_PERMIT": Decimal(1700),
    }
    assert on_road_for(amounts, ew="NONE")["withoutHypo"] == Decimal(1193)
    assert on_road_for(amounts, ew="4TH")["withoutHypo"] == Decimal(1213)
    assert on_road_for(amounts, ew="4TH_5TH")["withoutHypo"] == Decimal(1228)
    assert on_road_for(amounts, ew="NONE")["withHypo"] is None  # no charge known: never guessed
    amounts["HYPOTHECATION_CHARGE"] = Decimal(1500)
    assert on_road_for(amounts, ew="4TH")["withHypo"] == Decimal(2713)


def test_ev_sheet_gives_the_standard_format_with_hypothecation_from_its_columns(connection) -> None:  # noqa: F811
    _ingest(connection, _dealer_ev_workbook({"BE6": [_ev_row("BE 6 One B59 R18 NCH", 1_890_000)]}), date(2026, 9, 1))
    std = _standard(connection, model="BE6", variant="BE 6 One B59 R18 NCH")["standard"]
    base = Decimal(1_890_000) + round(1_890_000 * 0.01) + 80_000 + 25_000 + 500
    assert std["wefDate"] == date(2026, 9, 1) and std["ewOption"] == "NONE"
    assert std["exShowroom"] == "1890000.00" and std["essentialAccessories"] == "25000.00"
    assert std["accessoriesKit"] == "0.00" and std["ewFourthYear"] == "0.00" and std["rsa"] == "0.00"  # not offered: nil
    assert std["registrationWithoutHypo"] == "140.00" and std["registrationWithHypo"] == "1640.00"
    assert std["hypothecationCharge"] == "1500.00" and std["hypothecationSource"] == "PRICE_LIST"
    assert std["onRoadWithoutHypo"] == f"{base + 140:.2f}" and std["onRoadWithHypo"] == f"{base + 1640:.2f}"
    assert std["minimumBookingAmount"] is None and std["extraCharges"] == []


def test_a_sheet_with_both_warranty_tiers_prices_each_option_and_shows_its_notes(connection) -> None:  # noqa: F811
    _ingest(connection, _dealer_trim_workbook([
        _trim_row("AXT D MT 2WD 4S HT BS6.2", "AXT", 1_032_000),
    ], notes=_THAR_NOTES), date(2026, 9, 23))
    answer = _standard(connection, model="NEW THAR 2WD & 4WD", variant="AXT D MT 2WD 4S HT BS6.2", ew="4TH")
    std = answer["standard"]
    without_ew = 1_032_000 + 10_320 + 45_288 + 30_000 + 2_021 + 500 + 103_940
    assert std["ewFourthYear"] == "17999.00" and std["ewFourthAndFifthYear"] == "32999.00" and std["ewOption"] == "4TH"
    assert std["onRoadWithoutHypo"] == f"{without_ew + 17_999:.2f}" and std["onRoadWithHypo"] == f"{without_ew + 17_999 + 1_500:.2f}"
    assert std["onRoadByEw"]["NONE"]["withoutHypo"] == f"{without_ew:.2f}"
    assert std["onRoadByEw"]["4TH_5TH"]["withoutHypo"] == f"{without_ew + 32_999:.2f}"
    assert std["minimumBookingAmount"] == "21000.00" and std["wefDate"] == date(2026, 9, 23)
    assert [(e["amount"], "BELOW 7 STR" in e["label"]) for e in std["extraCharges"]] == [("1700.00", True)]
    # the price lines of the original block carry none of the notes, and its on-road is the same one-tier figure
    keys = {c["key"] for c in answer["priceList"]["components"]}
    assert not keys & {"HYPOTHECATION_CHARGE", "REGISTRATION_WITH_HYPO", "MIN_BOOKING_AMOUNT"}
    assert not any(k.startswith("EXTRA_CHARGE_") for k in keys)
    assert answer["priceList"]["onRoad"]["amount"] == f"{without_ew + 17_999:.2f}"


def test_a_vehicle_whose_sheet_is_silent_uses_the_versions_hypothecation_and_says_so(connection) -> None:  # noqa: F811
    _ingest(connection, _dealer_trim_workbook([_trim_row("AXT D MT 2WD 4S HT BS6.2", "AXT", 1_032_000)], notes=_THAR_NOTES),
            date(2026, 9, 23))
    _ingest(connection, _dealer_pv_workbook([_pv_row("1.6XXL HD V2", 859_501, 3_632, 3_632)]), date(2026, 9, 23))
    std = _standard(connection, model="VEERO", variant="1.6XXL HD V2")["standard"]
    assert std["hypothecationCharge"] == "1500.00" and std["hypothecationSource"] == "VERSION_DEFAULT"
    assert std["registrationWithHypo"] == "5132.00"


def _bolero_workbook() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Price List"
    ws.append(_HEADER)
    for sl, (insurance, sheet) in enumerate(((44_298, "NEW BOLERO NEO (PVT)"), (46_846, "NEW BOLERO NEO-COM")), start=1):
        ex, tcs, ew4, ew45, acc, rsa, fastag, reg = 999_000, 0, 8_999, 16_999, 20_000, 2_021, 500, 80_660
        onroad = ex + tcs + insurance + ew4 + ew45 + acc + rsa + fastag + reg
        ws.append([sl, "PV", "THE BOSS BOLERO NEO", "B8", "B8", "DIESEL", "MT", "2WD", "7",
                   ex, tcs, insurance, ew4, ew45, acc, rsa, fastag, reg, onroad, reg, onroad, sheet])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_a_vehicle_priced_for_private_and_commercial_insurance_shows_both_and_the_form_chooses(connection) -> None:  # noqa: F811
    _ingest(connection, _bolero_workbook(), date(2026, 9, 1))
    private = _standard(connection, model="THE BOSS BOLERO NEO", variant="B8")["standard"]  # not ambiguous: one vehicle
    options = {o["type"]: o for o in private["insurance"]["options"]}
    assert set(options) == {"PRIVATE", "COMMERCIAL"} and private["insurance"]["selectionNeeded"] is True
    assert options["PRIVATE"]["selected"] is True and private["insurance"]["inHouse"] == "44298.00"
    assert options["COMMERCIAL"]["amount"] == "46846.00"
    assert Decimal(options["COMMERCIAL"]["onRoadWithoutHypo"]) - Decimal(options["PRIVATE"]["onRoadWithoutHypo"]) == Decimal(2548)
    commercial = _standard(connection, model="THE BOSS BOLERO NEO", variant="B8", insurance_type="COMMERCIAL")["standard"]
    assert commercial["insurance"]["inHouse"] == "46846.00"
    assert {o["type"]: o["selected"] for o in commercial["insurance"]["options"]} == {"PRIVATE": False, "COMMERCIAL": True}
