"""Journey export to Excel for Team Leads and above (product owner 2026-11-06)."""
from __future__ import annotations

import io
from datetime import date

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from p2_support import AllowAllAuthorization, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_p2_journey_export import (
    ExportRequest,
    columns,
    deal_amounts,
    run_pending_exports,
)


def test_the_columns_are_the_deal_audit_export_s_in_its_order() -> None:
    cols = columns()
    # The Deal Audit export's 192 columns in its order, then the Booking Date
    # Source (2026-11-07) after them so that layout is unchanged.
    assert len(cols) == 193 and cols[-1] == "Booking Date Source"
    cols = cols[:192]
    assert cols[:4] == ["Dealership", "Location", "Booking No", "Booking Date"]
    assert cols[34] == "Pincode" and cols[35] == "Std: Ex Showroom"
    assert cols[62] == "Total Standard Deal" and cols[63] == "Std Disc: Sales Discount"
    assert cols[-7:] == ["Deal Variance", "Discount Variance", "Net Variance", "Total Amt To Be Received",
                         "Total Amt Received", "Payment Variance", "Payment Status"]
    assert len(set(cols)) == len(cols)


def _deal(*, delivery_started: bool, payable: str | None = "1100000", paid: str = "1100005") -> dict:
    return {
        "categories": [
            {"components": [
                {"key": "ex_showroom_price", "standard": "1000000", "current": "1005000"},
                {"key": "road_tax_registration", "standard": "80000", "current": "80000"},
                {"key": "road_tax_amount", "standard": "70000", "current": "70000", "partOf": "road_tax_registration"},
                {"key": "handling_charges", "standard": None, "current": "2000"},
                {"key": "other_charges", "standard": None, "current": "500", "excluded": True},
                {"key": "insurance_amount", "standard": "30000", "current": "31000"},
            ]},
        ],
        "discounts": [
            {"key": "CASH_DISCOUNT", "entitled": "20000", "current": "25000", "inStandard": True},
            {"key": "OTHER_SCHEME", "entitled": None, "current": "1000", "takenAsGiven": True},
        ],
        "summary": {
            "deliveryStarted": delivery_started,
            "standardTakenAsCharged": ["handling_charges"],
            "gross": {"standard": "1112000", "current": "1118000"},
            "discounts": {"standard": "21000", "current": "26000"},
            "net": {"standard": "1091000", "current": "1092000"},
            "variance": {"currentVsStandard": "1000"},
            "payable": payable,
            "paid": {"total": paid},
        },
    }


def test_each_deal_line_lands_in_its_head_and_the_totals_are_the_deal_s_own() -> None:
    row = deal_amounts(_deal(delivery_started=True))
    assert row["Std: Ex Showroom"] == 1000000 and row["Act: Ex Showroom"] == 1005000
    assert row["Var: Ex Showroom"] == 5000
    # Road tax inside "road tax + registration" is not counted twice.
    assert row["Act: Registration Type"] == 80000
    # A line with no head goes to Other Charges; an excluded line nowhere; a line
    # with no standard on record is taken at what is charged, as the Deal does.
    assert row["Std: Other Charges"] == 2000 and row["Act: Other Charges"] == 2000
    assert row["Std Disc: Sales Discount"] == 20000 and row["Act Disc: Sales Discount"] == 25000
    assert row["Var Disc: Sales Discount"] == -5000  # standard less actual: excess discount
    assert row["Std Disc: Other Discount"] == 1000  # taken as given
    # Not captured by Verigence yet: blank, not 0.
    assert row["Std: OBD / Body Décor Charges"] is None and row["Act Disc: Spin the wheel"] is None
    assert row["Total Standard Deal"] == 1112000 and row["Net Actual Deal"] == 1092000
    assert row["Std: Net Amount (On-Road)"] == 1112000
    assert row["Deal Variance"] == 6000 and row["Discount Variance"] == -5000 and row["Net Variance"] == 1000
    assert row["Total Amt To Be Received"] == 1100000 and row["Payment Variance"] == 5
    assert row["Payment Status"] == "Matched"  # within Rs 10


def test_no_variance_until_delivery_has_started_and_no_payable_is_no_amount_data() -> None:
    row = deal_amounts(_deal(delivery_started=False, payable=None, paid="5000"))
    assert row["Std: Ex Showroom"] == 1000000 and row["Var: Ex Showroom"] is None
    assert row["Var Disc: Sales Discount"] is None and row["Net Variance"] is None
    assert row["Payment Status"] == "No amount data" and row["Total Amt Received"] == 5000
    assert deal_amounts(_deal(delivery_started=True, paid="1000000"))["Payment Status"] == "Short"


def test_the_date_range_is_checked() -> None:
    with pytest.raises(ValueError):
        ExportRequest(dateFrom=date(2026, 11, 6), dateTo=date(2026, 11, 1))
    with pytest.raises(ValueError):
        ExportRequest(dateFrom=date(2026, 1, 1), dateTo=date(2026, 11, 1))
    with pytest.raises(ValueError):
        ExportRequest(dateFrom=date(2026, 11, 1))


class _Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, key: str, data: bytes, *, content_type: str) -> None:
        self.objects[key] = data

    def presign_get(self, key: str, *, expires_seconds: int = 1800) -> str:
        return f"https://storage.test/get/{key}"


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2exp")
    try:
        yield created
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _assign(journey, actor: str, role: str, *, outlet: bool) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
                 "dealer_id, outlet_id) SELECT CAST(:t AS varchar), :a, :r, CASE WHEN :o THEN dealer_id END, "
                 "CASE WHEN :o THEN outlet_id END FROM auditcore.journeys WHERE tenant_id=:t LIMIT 1"),
            {"t": journey.tenant_id, "a": actor, "r": role, "o": outlet},
        )


def _client(actor: str) -> TestClient:
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=actor)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    return TestClient(app, raise_server_exceptions=False)


def test_a_project_manager_exports_the_journeys_and_downloads_the_workbook(journey, monkeypatch) -> None:
    storage = _Storage()
    monkeypatch.setattr("audit_core.uc03_p2_storage.get_p2_document_storage", lambda: storage)
    base = f"/p2/v1/tenants/{journey.tenant_id}/reports/journey-exports"

    # The journey's own PC does not export.
    assert _client(journey.actor_id).post(base, json={}).status_code == 403

    _assign(journey, "pm-project-wide", "PM", outlet=False)
    client = _client("pm-project-wide")
    asked = client.post(base, json={"dateBasis": "UPLOADED"})
    assert asked.status_code == 202, asked.text
    assert asked.json()["status"] == "PENDING"

    assert run_pending_exports(journey.engine, [journey.tenant_id], "test-worker") == 1
    [done] = client.get(base).json()["exports"]
    assert done["status"] == "READY", done
    assert done["total"] == done["rows"] == 1 and done["done"] == 1
    link = client.get(f"{base}/{done['exportId']}/file").json()
    key = link["url"].split("https://storage.test/get/", 1)[1]
    assert key.endswith(".xlsx") and link["fileName"].startswith("Journey-Export_all-dates")

    book = load_workbook(io.BytesIO(storage.objects[key]))
    sheet = book["Deal Audit"]
    header = [c.value for c in next(sheet.iter_rows(max_row=1))]
    assert header == columns()
    [row] = list(sheet.iter_rows(min_row=2, values_only=True))
    values = dict(zip(header, row, strict=True))
    assert values["Dealership"] and values["Review Status"] == "Not reviewed"
    assert values["Payment Status"] == "No amount data"
    assert {"Columns", "About"} <= set(book.sheetnames)

    # Someone else cannot download it.
    _assign(journey, "another-pm", "PM", outlet=False)
    assert _client("another-pm").get(f"{base}/{done['exportId']}/file").status_code == 404


def test_a_team_lead_exports_the_outlets_they_lead_and_no_other(journey, monkeypatch) -> None:
    storage = _Storage()
    monkeypatch.setattr("audit_core.uc03_p2_storage.get_p2_document_storage", lambda: storage)
    base = f"/p2/v1/tenants/{journey.tenant_id}/reports/journey-exports"
    _assign(journey, "tl-of-the-outlet", "TL", outlet=True)
    client = _client("tl-of-the-outlet")
    scope = client.get(f"{base}/scope").json()
    assert scope["role"] == "TL" and len(scope["outlets"]) == 1 and scope["maxRangeDays"] == 92
    assert client.post(base, json={}).status_code == 202
    other_outlet = "00000000-0000-0000-0000-000000000001"
    assert client.post(base, json={"outletId": other_outlet}).status_code == 404
    run_pending_exports(journey.engine, [journey.tenant_id], "test-worker")
    [done] = client.get(base).json()["exports"]
    assert done["status"] == "READY" and done["rows"] == 1
