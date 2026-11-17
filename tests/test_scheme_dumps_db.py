"""The EW / RSA dump upload against a real database (synthetic data only): the upload,
the match, what Verigence shows per Journey and the four lists, as the runtime role."""
from __future__ import annotations

import io
import json
import os
from datetime import date
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from sqlalchemy import create_engine, text
from starlette.datastructures import UploadFile

import audit_core.uc03_p2_scheme_dumps as sd

HEAD = ("Customer ID,Scheme Invoice No,Scheme Reg Date,Model Family,Scheme desc,Vehicle Reg No,Chassis No,KM,"
        "Employee Designation,Sold By,Status,Model Desc,RSA Cancelled Date,Amount,Total RSA amount,Location")


def _line(number: str, chassis: str, *, code: str = "R270000000", sold_by: str = "SALS",
          designation: str = "Field Sales Consultant", cancelled: str = "", location: str = "Jajpur") -> str:
    return (f"{code},{number},14-10-2026,J46 - XUV 3XO,RSA Scheme for 4th Year,{chassis},{chassis},11,{designation},"
            f"{sold_by},Completed,XUV3XO AX5,{cancelled},1713,1713,{location}")


DUMP = "\n".join([
    HEAD,
    _line("RSA001", "T6H49888"),                                  # charged on the ledger: in sync
    _line("RSA002", "T2J78772"),                                  # ledger on file, no RSA line
    _line("RSA003", "T5J25790"),                                  # chassis known from the insurance cover only
    _line("RSA004", "T9Z99999", code="R270600851"),              # no chassis on file: the DMS customer id
    _line("RSA005", "T8X00001"),                                  # sold by sales, no Journey
    _line("RSA006", "P6J17679", sold_by="", designation="Service Manager"),  # the workshop's, an older vehicle
    _line("RSA007", "T6K00002", cancelled="20-10-2026"),         # registered and cancelled
]) + "\n"


@pytest.fixture
def dealer():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for scheme dump integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-sd-{suffix}"
    journeys: dict[str, str] = {}
    with engine.begin() as c:
        category = c.execute(text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                                  "VALUES (:c, 'V') RETURNING product_category_id"), {"c": f"SD-C-{suffix}"}).scalar_one()
        oem = c.execute(text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
                        {"c": f"SD-O-{suffix}"}).scalar_one()
        c.execute(text("""INSERT INTO auditcore.projects (tenant_id, project_code, project_name, oem_id,
                product_category_id, effective_start_date, timezone_name, project_status)
                VALUES (:t, :p, 'SD', :o, :c, '2026-01-01', 'Asia/Kolkata', 'ACTIVE')"""),
                  {"t": tenant_id, "p": f"SD-{suffix}", "o": oem, "c": category})
        dealer_id = c.execute(text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                                   "VALUES (:t, :c, 'Test Motors') RETURNING dealer_id"),
                              {"t": tenant_id, "c": f"SD-D-{suffix}"}).scalar_one()
        outlets = {}
        for name in ("Jajpur", "Cuttack"):
            outlets[name] = c.execute(
                text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                     "VALUES (:t, :d, :c, :n) RETURNING outlet_id"),
                {"t": tenant_id, "d": dealer_id, "c": f"SD-{name[:3]}-{suffix}", "n": f"Test Motors - {name}"}).scalar_one()

        def journey(name: str, *, outlet: str = "Jajpur", vin: str | None = None,
                    documents: dict[str, dict[str, object]] | None = None) -> None:
            customer = c.execute(text("""INSERT INTO auditcore.customers (tenant_id, dealer_id, outlet_id,
                    customer_type_code, display_name, legal_name) VALUES (:t, :d, :o, 'INDIVIDUAL', :n, :n)
                    RETURNING customer_id"""),
                                 {"t": tenant_id, "d": dealer_id, "o": outlets[outlet], "n": name}).scalar_one()
            jid = c.execute(text("""INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id,
                    journey_reference) VALUES (:t, :d, :o, :c, :r) RETURNING journey_id"""),
                            {"t": tenant_id, "d": dealer_id, "o": outlets[outlet], "c": customer,
                             "r": f"SD-{suffix}-{name}"}).scalar_one()
            if vin:
                c.execute(text("INSERT INTO auditcore.vehicle_records (tenant_id, journey_id, vin) VALUES (:t, :j, :v)"),
                          {"t": tenant_id, "j": jid, "v": vin})
            for document_type, fields in (documents or {}).items():
                document = uuid4()
                c.execute(text("""INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id,
                        di_document_id, document_type_key, evidence_purpose, process_area, association_status)
                        VALUES (:t, :j, :c, :s, :d, :k, 'JOURNEY_DOCUMENT', 'DELIVERY', 'ACTIVE')"""),
                          {"t": tenant_id, "j": jid, "c": customer, "s": uuid4(), "d": document, "k": document_type})
                for key, value in fields.items():
                    c.execute(text("""INSERT INTO auditcore.journey_document_extracted_fields (tenant_id, journey_id,
                            di_document_id, field_key, extracted_value, effective_value, source_document_type_key,
                            stage_code) VALUES (:t, :j, :d, :k, CAST(:v AS jsonb), CAST(:v AS jsonb), :dt, 'DELIVERY')"""),
                              {"t": tenant_id, "j": jid, "d": document, "k": key, "v": json.dumps(value),
                               "dt": document_type})
            journeys[name] = str(jid)

        journey("sync", vin="MA1NE2ZTFT6H49888", documents={
            "customer_ledger": {"rsa_amount": 2021}, "customer_invoice_dms": {"invoice_date": "2026-10-14"}})
        journey("ledger-no-line", vin="MA1NE2ZTFT2J78772", documents={"customer_ledger": {"net_amount": 1500000}})
        journey("papers-pending", documents={"insurance_cover": {"chassis_number": "MA1NE2ZTFT5J25790"}})
        # the booking form's offer is not a charge: the customer id finds the Journey, RSA stays "not bought"
        journey("by-code", documents={"customer_invoice_dms": {"buyer_customer_id": "R270600851"},
                                      "booking_form": {"rsa_amount": 2021}})
        journey("charged-not-registered", vin="MA1NE2ZTFT6K00001", documents={
            "customer_ledger": {"rsa_amount": 2021}, "customer_invoice_dms": {"invoice_date": "2026-10-20"}})
        journey("charged-in-november", vin="MA1NE2ZTFT6K00003", documents={
            "customer_ledger": {"rsa_amount": 2021}, "customer_invoice_dms": {"invoice_date": "2026-11-05"}})
        journey("charged-at-other-outlet", outlet="Cuttack", vin="MA1NE2ZTFT6K00004", documents={
            "customer_ledger": {"rsa_amount": 2021}, "customer_invoice_dms": {"invoice_date": "2026-10-21"}})
        journey("charged-then-cancelled", vin="MA1NE2ZTFT6K00002", documents={
            "customer_ledger": {"rsa_amount": 2021}, "customer_invoice_dms": {"invoice_date": "2026-10-19"}})
    try:
        yield SimpleNamespace(engine=engine, tenant_id=tenant_id, dealer_id=dealer_id, journeys=journeys)
    finally:
        delete_tenant_data(engine, tenant_id)
        engine.dispose()


def _as_runtime(c, tenant_id):
    c.execute(text("SET LOCAL ROLE audit_core_runtime"))
    c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})


def _patched(d):
    return (mock.patch.object(sd, "_access", return_value={"ADMIN"}),
            mock.patch.object(sd, "_dealers", return_value=[{"dealerId": str(d.dealer_id), "dealerName": "Test Motors"}]),
            mock.patch.object(sd, "_name_of", return_value="Tester"),
            mock.patch.object(sd, "_today", return_value=date(2026, 11, 17)))


def _upload(d, c, data: str = DUMP, name: str = "Test_Jajpur_RSA_Dump.csv"):
    return sd.upload_scheme_dump(d.tenant_id, SimpleNamespace(subject="user-1"), None, c,
                                 UploadFile(io.BytesIO(data.encode()), filename=name), d.dealer_id, None)


def test_upload_matches_and_reports_the_four_lists(dealer):
    a, b, c_, e = _patched(dealer)
    with dealer.engine.begin() as c, a, b, c_, e:
        _as_runtime(c, dealer.tenant_id)
        dump = _upload(dealer, c)
        assert (dump["kind"], dump["periodMonth"], dump["rows"]) == ("RSA", "2026-10", 7)
        assert (dump["sales"], dump["service"], dump["cancelled"], dump["matched"]) == (6, 1, 1, 5)
        # "Jajpur" is the outlet's own name: nothing unmapped
        assert dump["locations"] == ["Jajpur"] and dump["unmappedLocations"] == []

        report = sd.scheme_report(dealer.tenant_id, SimpleNamespace(subject="user-1"), None, c, "2026-10", "RSA")
        by = {key: {r["journeyReference"].rsplit("-", 1)[-1] if r.get("journeyReference") else r["schemeNumber"]: r
                    for r in report[key]}
              for key in ("inSync", "notBought", "notInDump", "notInVerigence", "serviceOnly", "cancelled")}
        ref = {name: jid for name, jid in dealer.journeys.items()}

        assert [r["journeyId"] for r in report["inSync"]] == [ref["sync"]]
        assert report["inSync"][0]["matchBasis"] == "CHASSIS"
        assert "rsa amount" in report["inSync"][0]["verigenceReason"]

        not_bought = {r["journeyId"]: r for r in report["notBought"]}
        assert set(not_bought) == {ref["ledger-no-line"], ref["papers-pending"], ref["by-code"]}
        assert not_bought[ref["ledger-no-line"]]["papersOnFile"] is True
        assert not_bought[ref["papers-pending"]]["papersOnFile"] is False
        assert not_bought[ref["papers-pending"]]["matchBasis"] == "CHASSIS"  # from the insurance cover
        assert not_bought[ref["by-code"]]["matchBasis"] == "CUSTOMER_ID"

        # charged in Verigence, the vehicle in no dump: October's, at the outlet the dump covers. The one
        # registered and cancelled is listed too; November's and the other outlet's are not.
        not_in_dump = {r["journeyId"]: r for r in report["notInDump"]}
        assert set(not_in_dump) == {ref["charged-not-registered"], ref["charged-then-cancelled"]}
        assert not_in_dump[ref["charged-then-cancelled"]]["cancelledInDump"] is True
        assert not_in_dump[ref["charged-not-registered"]]["journeyDate"] == "2026-10-20"

        assert list(by["notInVerigence"]) == ["RSA005"]
        assert list(by["serviceOnly"]) == ["RSA006"]
        assert [r["schemeNumber"] for r in report["cancelled"]] == ["RSA007"]

        stats = report["dealers"][0]
        assert (stats["rows"], stats["inSync"], stats["notBought"], stats["papersPending"], stats["notInDump"],
                stats["notInVerigence"], stats["serviceOnly"], stats["cancelled"]) == (7, 1, 3, 2, 2, 1, 1, 1)
        assert stats["coverage"] == ["Test Motors - Jajpur"]

        # the extended warranty report of the same month: nothing uploaded, nothing listed
        ew = sd.scheme_report(dealer.tenant_id, SimpleNamespace(subject="user-1"), None, c, "2026-10", "EW")
        assert ew["dealers"][0]["uploaded"] is False and not ew["notInDump"] and not ew["inSync"]

        workbook = sd.build_scheme_excel([ew, report])
        assert workbook[:2] == b"PK"


def test_a_ledger_that_arrives_later_moves_the_journey_on_check_again(dealer):
    a, b, c_, e = _patched(dealer)
    with dealer.engine.begin() as c, a, b, c_, e:
        _as_runtime(c, dealer.tenant_id)
        _upload(dealer, c)
    with dealer.engine.begin() as c:
        # the NDC comes in for the Journey that had no papers, charging the RSA
        jid = dealer.journeys["papers-pending"]
        customer = c.execute(text("SELECT customer_id FROM auditcore.journeys WHERE tenant_id = :t AND journey_id = :j"),
                             {"t": dealer.tenant_id, "j": jid}).scalar_one()
        document = uuid4()
        c.execute(text("""INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id,
                di_document_id, document_type_key, evidence_purpose, process_area, association_status)
                VALUES (:t, :j, :c, :s, :d, 'customer_ledger', 'JOURNEY_DOCUMENT', 'DELIVERY', 'ACTIVE')"""),
                  {"t": dealer.tenant_id, "j": jid, "c": customer, "s": uuid4(), "d": document})
        c.execute(text("""INSERT INTO auditcore.journey_document_extracted_fields (tenant_id, journey_id, di_document_id,
                field_key, extracted_value, effective_value, source_document_type_key, stage_code)
                VALUES (:t, :j, :d, 'rsa_amount', '2021', '2021', 'customer_ledger', 'DELIVERY')"""),
                  {"t": dealer.tenant_id, "j": jid, "d": document})
    a, b, c_, e = _patched(dealer)
    with dealer.engine.begin() as c, a, b, c_, e:
        _as_runtime(c, dealer.tenant_id)
        before = sd.scheme_report(dealer.tenant_id, SimpleNamespace(subject="user-1"), None, c, "2026-10", "RSA")
        assert jid in {r["journeyId"] for r in before["notBought"]}  # as decided at the upload
        done = sd.recheck_scheme_dumps(dealer.tenant_id, SimpleNamespace(subject="user-1"), None, c,
                                       {"month": "2026-10"})
        assert done["dumps"] == 1 and done["journeys"] >= 5
        after = sd.scheme_report(dealer.tenant_id, SimpleNamespace(subject="user-1"), None, c, "2026-10", "RSA")
        assert jid in {r["journeyId"] for r in after["inSync"]}
        assert jid not in {r["journeyId"] for r in after["notBought"]}
        # the nightly look does the same for every dealership with a recent dump
        assert sd.refresh_recent(c, tenant_id=dealer.tenant_id)["dealers"] == 1


def test_a_second_upload_replaces_the_same_location_and_keeps_another(dealer):
    a, b, c_, e = _patched(dealer)
    with dealer.engine.begin() as c, a, b, c_, e:
        _as_runtime(c, dealer.tenant_id)
        first = _upload(dealer, c)
        other = _upload(dealer, c, "\n".join([HEAD, _line("RSA101", "T6K00004", location="Cuttack")]) + "\n",
                        "Test_Rest_RSA_Dump.csv")
        again = _upload(dealer, c)
        listed = {u["dumpId"]: u["status"] for u in sd.list_scheme_dumps(
            dealer.tenant_id, SimpleNamespace(subject="user-1"), None, c, "2026-10")["uploads"]}
        assert listed == {first["dumpId"]: "SUPERSEDED", other["dumpId"]: "ACTIVE", again["dumpId"]: "ACTIVE"}
        report = sd.scheme_report(dealer.tenant_id, SimpleNamespace(subject="user-1"), None, c, "2026-10", "RSA")
        stats = report["dealers"][0]
        # both locations are covered now: the other outlet's Journey is in the Cuttack dump, so in sync
        assert stats["rows"] == 8 and stats["coverage"] == ["Test Motors - Cuttack", "Test Motors - Jajpur"]
        assert dealer.journeys["charged-at-other-outlet"] in {r["journeyId"] for r in report["inSync"]}
        assert dealer.journeys["charged-at-other-outlet"] not in {r["journeyId"] for r in report["notInDump"]}
