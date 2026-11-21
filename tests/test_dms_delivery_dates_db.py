"""A delivery Journey's delivery day for the DMS comparison (product owner 2026-11-17):
the gate pass's date, else the NDC's printed delivery date, else the latest payment receipt,
for Journeys with delivery files only; a daily dump is compared with the deliveries from the
1st of its month to the last day it covers. Synthetic data only."""
from __future__ import annotations

import json
import os
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from sqlalchemy import create_engine, text

from audit_core import uc03_p2_delivery_dates as dd
from audit_core.uc03_p2_dms_outlet_report import verigence_figures

TODAY = date(2026, 11, 17)


@pytest.fixture
def dealer():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for delivery date integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-dd-{suffix}"
    journeys: dict[str, str] = {}
    with engine.begin() as c:
        category = c.execute(text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                                  "VALUES (:c, 'V') RETURNING product_category_id"), {"c": f"DD-C-{suffix}"}).scalar_one()
        oem = c.execute(text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
                        {"c": f"DD-O-{suffix}"}).scalar_one()
        c.execute(text("""INSERT INTO auditcore.projects (tenant_id, project_code, project_name, oem_id,
                product_category_id, effective_start_date, timezone_name, project_status)
                VALUES (:t, :p, 'DD', :o, :c, '2026-01-01', 'Asia/Kolkata', 'ACTIVE')"""),
                  {"t": tenant_id, "p": f"DD-{suffix}", "o": oem, "c": category})
        dealer_id = c.execute(text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                                   "VALUES (:t, :c, 'D') RETURNING dealer_id"), {"t": tenant_id, "c": f"DD-D-{suffix}"}
                              ).scalar_one()
        outlet_id = c.execute(text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                                   "VALUES (:t, :d, :c, 'Main') RETURNING outlet_id"),
                              {"t": tenant_id, "d": dealer_id, "c": f"DD-OU-{suffix}"}).scalar_one()

        def vin(jid, document, document_type: str) -> None:
            """A vehicle paper prints the VIN: a delivery in Verigence (2026-11-21)."""
            c.execute(text("""INSERT INTO auditcore.journey_document_extracted_fields (tenant_id, journey_id,
                    di_document_id, source_fact_version, field_key, stage_code, source_document_type_key,
                    extracted_value, effective_value)
                    VALUES (:t, :j, :d, 1, 'chassis_number', 'DELIVERY', :k, CAST(:v AS jsonb), CAST(:v AS jsonb))"""),
                      {"t": tenant_id, "j": jid, "d": document, "k": document_type,
                       "v": json.dumps(f"MA1TEST{str(jid).replace('-', '')[:10].upper()}")})

        def journey(name: str, *, delivery_stage: bool, gate_pass: str | None = None, ndc: str | None = None,
                    receipt: date | None = None, delivery_paper: str | None = None) -> None:
            customer = c.execute(text("""INSERT INTO auditcore.customers (tenant_id, dealer_id, outlet_id,
                    customer_type_code, display_name, legal_name) VALUES (:t, :d, :o, 'INDIVIDUAL', :n, :n)
                    RETURNING customer_id"""), {"t": tenant_id, "d": dealer_id, "o": outlet_id, "n": name}).scalar_one()
            jid = c.execute(text("""INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id,
                    journey_reference) VALUES (:t, :d, :o, :c, :r) RETURNING journey_id"""),
                            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "c": customer,
                             "r": f"DD-{suffix}-{name}"}).scalar_one()
            journeys[name] = str(jid)
            if delivery_stage:
                c.execute(text("INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code, "
                               "business_status, first_started_at_utc) VALUES (:t, :j, 'DELIVERY', 'IN_PROGRESS', now())"),
                          {"t": tenant_id, "j": jid})
            if delivery_paper is not None:  # a delivery document with no delivery date on it
                c.execute(text("""INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id,
                        di_document_id, document_type_key, evidence_purpose, process_area, association_status)
                        VALUES (:t, :j, :c, :s, :d, :k, 'JOURNEY_DOCUMENT', 'DELIVERY', 'ACTIVE')"""),
                          {"t": tenant_id, "j": jid, "c": customer, "s": uuid4(), "d": (paper := uuid4()), "k": delivery_paper})
                vin(jid, paper, delivery_paper)
            for document_type, value in (("gate_pass", gate_pass), ("customer_ledger", ndc)):
                if value is None:
                    continue
                document = uuid4()
                c.execute(text("""INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id,
                        di_document_id, document_type_key, evidence_purpose, process_area, association_status)
                        VALUES (:t, :j, :c, :s, :d, :k, 'JOURNEY_DOCUMENT', 'DELIVERY', 'ACTIVE')"""),
                          {"t": tenant_id, "j": jid, "c": customer, "s": uuid4(), "d": document, "k": document_type})
                c.execute(text("""INSERT INTO auditcore.journey_document_extracted_fields (tenant_id, journey_id,
                        di_document_id, source_fact_version, field_key, stage_code, source_document_type_key,
                        extracted_value, effective_value)
                        VALUES (:t, :j, :d, 1, 'delivery_date', 'DELIVERY', :k, CAST(:v AS jsonb), CAST(:v AS jsonb))"""),
                          {"t": tenant_id, "j": jid, "d": document, "k": document_type, "v": json.dumps(value)})
                vin(jid, document, document_type)
            if receipt is not None:
                document = uuid4()
                c.execute(text("""INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id,
                        di_document_id, document_type_key, evidence_purpose, process_area, association_status)
                        VALUES (:t, :j, :c, :s, :d, 'dealer_receipt', 'JOURNEY_DOCUMENT', 'BOOKING', 'ACTIVE')"""),
                          {"t": tenant_id, "j": jid, "c": customer, "s": uuid4(), "d": document})
                booking = c.execute(text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date) "
                                         "VALUES (:t, :j, '2026-10-20') RETURNING booking_id"),
                                    {"t": tenant_id, "j": jid}).scalar_one()
                c.execute(text("""INSERT INTO auditcore.payments (tenant_id, journey_id, amount, booking_id,
                        payment_stage, status_source, source_di_document_id, receipt_number, receipt_date,
                        payment_mode_code) VALUES (:t, :j, 100000, :b, 'BOOKING', 'EVIDENCE', :d, :n, :rd, 'NEFT')"""),
                          {"t": tenant_id, "j": jid, "b": booking, "d": document, "n": f"R-{name}", "rd": receipt})

        journey("GATEPASS", delivery_stage=True, gate_pass="2026-11-03", receipt=date(2026, 10, 25))
        journey("NDCONLY", delivery_stage=True, ndc="2026-11-04", receipt=date(2026, 10, 30))
        journey("RECEIPTONLY", delivery_stage=True, receipt=date(2026, 11, 2), delivery_paper="insurance_cover")
        # 2026-11-21: the Delivery stage row opens on its own minutes after a booking upload; with
        # no delivery document on file the car is not delivered in Verigence (uc03_delivery_start).
        journey("STAGEONLY", delivery_stage=True, receipt=date(2026, 11, 2))
        journey("BOOKINGONLY", delivery_stage=False, receipt=date(2026, 11, 2))
        journey("LATERGP", delivery_stage=True, gate_pass="2026-11-08")
        journey("OCTOBER", delivery_stage=True, gate_pass="2026-10-28")
        journey("MISREAD", delivery_stage=True, gate_pass="2026-12-30", receipt=date(2026, 11, 1))

        dump_id = uuid4()
        c.execute(text("""INSERT INTO auditcore.p2_dms_dumps (tenant_id, dump_id, dump_kind, dealer_id, business_date,
                period_month, file_name, file_format, row_count, uploaded_by)
                VALUES (:t, :id, 'DELIVERY', :d, '2026-11-06', '2026-11-01', 'd.xlsx', 'DMS', 2, 'u')"""),
                  {"t": tenant_id, "id": dump_id, "d": dealer_id})
        for n, (day, jid) in enumerate(((date(2026, 11, 3), journeys["GATEPASS"]), (date(2026, 11, 5), None)),
                                       start=1):
            c.execute(text("""INSERT INTO auditcore.p2_dms_dump_rows (tenant_id, dump_id, row_no, dump_kind, dealer_id,
                    event_date, journey_id, outcome) VALUES (:t, :id, :n, 'DELIVERY', :d, :e, CAST(:j AS uuid), :o)"""),
                      {"t": tenant_id, "id": dump_id, "n": n, "d": dealer_id, "e": day, "j": jid,
                       "o": "MATCHED" if jid else "MISSING"})
    try:
        yield SimpleNamespace(engine=engine, tenant_id=tenant_id, dealer_id=str(dealer_id), outlet_id=str(outlet_id),
                              journeys=journeys, dump_id=dump_id)
    finally:
        delete_tenant_data(engine, tenant_id)
        engine.dispose()


def test_delivery_day_is_gate_pass_then_ndc_then_latest_receipt(dealer):
    with dealer.engine.begin() as c:
        found = dd.deliveries(c, tenant_id=dealer.tenant_id, today=TODAY, dealer_id=dealer.dealer_id)
    by_name = {name: found.get(jid) for name, jid in dealer.journeys.items()}
    assert (by_name["GATEPASS"].source, by_name["GATEPASS"].days) == (dd.GATE_PASS, [date(2026, 11, 3)])
    assert (by_name["NDCONLY"].source, by_name["NDCONLY"].days) == (dd.NDC, [date(2026, 11, 4)])
    assert (by_name["RECEIPTONLY"].source, by_name["RECEIPTONLY"].days) == (dd.RECEIPT, [date(2026, 11, 2)])
    assert by_name["STAGEONLY"] is None
    assert by_name["BOOKINGONLY"] is None  # receipts, but no delivery files: not a delivery
    # a gate pass dated after today is a misreading: the receipt decides
    assert (by_name["MISREAD"].source, by_name["MISREAD"].days) == (dd.RECEIPT, [date(2026, 11, 1)])


def test_a_daily_dump_is_compared_with_the_days_it_covers_only(dealer):
    with dealer.engine.begin() as c:
        dump = dict(c.execute(text("SELECT dump_id, dealer_id, period_month, business_date FROM auditcore.p2_dms_dumps "
                                   "WHERE tenant_id = :t AND dump_id = :id"),
                              {"t": dealer.tenant_id, "id": dealer.dump_id}).mappings().one())
        rows = dd.verigence_not_in_dump(c, tenant_id=dealer.tenant_id, dump=dump, today=TODAY)
        figures = verigence_figures(c, dealer.tenant_id, [{"outlet_id": dealer.outlet_id}], date(2026, 11, 1),
                                    date(2026, 11, 5), date(2026, 11, 5), TODAY,
                                    {dealer.journeys["GATEPASS"]})[dealer.outlet_id]
    names = {v: k for k, v in dealer.journeys.items()}
    # the dump of the 6th covers the 1st to the 5th: the 8th's gate pass and October's are out
    assert sorted(names[r["journeyId"]] for r in rows) == ["MISREAD", "NDCONLY", "RECEIPTONLY"]
    assert {names[r["journeyId"]]: r["matchBasis"] for r in rows}["NDCONLY"] == dd.NDC
    assert figures["deliveryInVerigence"] == 4
    assert figures["deliveryDatedWithoutGatePass"] == 3
    assert figures["deliveryNotInDms"] == 3


def test_the_dms_date_decides_whatever_verigence_reads(dealer):
    """Product owner, 19 Nov 2026: "you have to stick to the DMS date.. check DMS Dump date and
    compare the data accordingly.. DMS will always be source of truth.. no matter"."""
    from audit_core.uc03_p2_dms_dumps import dms_dates
    from audit_core.uc03_p2_dms_outlet_report import build_outlet_report

    j = dealer.journeys
    with dealer.engine.begin() as c:
        def dump(kind: str, business_date: date, period: date, rows: list[tuple[date, str | None, str]]) -> None:
            dump_id = uuid4()
            c.execute(text("""INSERT INTO auditcore.p2_dms_dumps (tenant_id, dump_id, dump_kind, dealer_id,
                    business_date, period_month, file_name, file_format, row_count, uploaded_by)
                    VALUES (:t, :id, :k, :d, :b, :p, 'x.xlsx', 'DMS', :n, 'u')"""),
                      {"t": dealer.tenant_id, "id": dump_id, "k": kind, "d": dealer.dealer_id, "b": business_date,
                       "p": period, "n": len(rows)})
            for n, (day, jid, outcome) in enumerate(rows, start=1):
                c.execute(text("""INSERT INTO auditcore.p2_dms_dump_rows (tenant_id, dump_id, row_no, dump_kind,
                        dealer_id, event_date, journey_id, outcome, outlet_label)
                        VALUES (:t, :id, :n, :k, :d, :e, CAST(:j AS uuid), :o, 'Main')"""),
                          {"t": dealer.tenant_id, "id": dump_id, "n": n, "k": kind, "d": dealer.dealer_id, "e": day,
                           "j": jid, "o": outcome})

        # October's dump dates NDCONLY's delivery on 31 Oct; Verigence reads 4 Nov from its NDC.
        dump("DELIVERY", date(2026, 10, 31), date(2026, 10, 1), [(date(2026, 10, 31), j["NDCONLY"], "MATCHED")])
        # The fixture's dump of 6 Nov (GATEPASS on the 3rd, one row not in Verigence) gets two more rows:
        # LATERGP on the 4th (Verigence reads the 8th from its gate pass) and NODATE on the 5th (a
        # delivery file with no delivery date read at all).
        c.execute(text("""INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code,
                business_status, first_started_at_utc) VALUES (:t, :j, 'DELIVERY', 'IN_PROGRESS', now())"""),
                  {"t": dealer.tenant_id, "j": j["BOOKINGONLY"]})
        for n, (day, name) in enumerate(((date(2026, 11, 4), "LATERGP"), (date(2026, 11, 5), "BOOKINGONLY")), start=3):
            c.execute(text("""INSERT INTO auditcore.p2_dms_dump_rows (tenant_id, dump_id, row_no, dump_kind, dealer_id,
                    event_date, journey_id, outcome, outlet_label)
                    VALUES (:t, :id, :n, 'DELIVERY', :d, :e, CAST(:j AS uuid), 'MATCHED', 'Main')"""),
                      {"t": dealer.tenant_id, "id": dealer.dump_id, "n": n, "d": dealer.dealer_id, "e": day,
                       "j": j[name]})
        c.execute(text("UPDATE auditcore.p2_dms_dump_rows SET outlet_label = 'Main' WHERE tenant_id = :t"),
                  {"t": dealer.tenant_id})
        # The booking dump of 6 Nov: GATEPASS booked on 3 Nov per the DMS; Verigence reads 20 Oct
        # from its receipt's booking. One more row is not in Verigence.
        dump("BOOKING", date(2026, 11, 6), date(2026, 11, 1),
             [(date(2026, 11, 3), j["GATEPASS"], "MATCHED"), (date(2026, 11, 2), None, "MISSING")])

        dated = dms_dates(c, dealer.tenant_id, dealer.dealer_id, date(2026, 11, 6))
        assert dated["DELIVERY"][j["NDCONLY"]] == date(2026, 10, 31)
        assert dated["DELIVERY"][j["LATERGP"]] == date(2026, 11, 4)
        assert dated["BOOKING"][j["GATEPASS"]] == date(2026, 11, 3)
        # A dump uploaded after the report's day is not read.
        assert dms_dates(c, dealer.tenant_id, dealer.dealer_id, date(2026, 11, 5))["BOOKING"] == {}

        report = build_outlet_report(c, tenant_id=dealer.tenant_id,
                                     dealers=[{"dealerId": dealer.dealer_id, "dealerName": "D"}],
                                     day=date(2026, 11, 6), today=TODAY)
        dump_row = dict(c.execute(text("SELECT dump_id, dealer_id, period_month, business_date FROM auditcore.p2_dms_dumps "
                                       "WHERE tenant_id = :t AND dump_id = :id"),
                                  {"t": dealer.tenant_id, "id": dealer.dump_id}).mappings().one())
        not_in_dump = dd.verigence_not_in_dump(c, tenant_id=dealer.tenant_id, dump=dump_row, today=TODAY)
    f = report["dealers"][0]["figures"]
    # Bookings: the DMS's 3 Nov, not Verigence's 20 Oct -- synced and in Verigence.
    assert (f["bookingDms"], f["bookingSynced"], f["bookingNotInVerigence"]) == (2, 1, 1)
    assert f["bookingInVerigence"] == f["bookingSynced"] + f["bookingNotInDms"]
    # Deliveries: GATEPASS, LATERGP (DMS 4 Nov, Verigence 8 Nov) and BOOKINGONLY (no date read) synced;
    # NDCONLY is October's per the DMS, so not counted in November though Verigence reads 4 Nov.
    assert (f["deliveryDump"], f["deliverySynced"], f["deliveryNotInVerigence"]) == (4, 3, 1)
    assert f["deliveryInVerigence"] == f["deliverySynced"] + f["deliveryNotInDms"]
    assert "deliveryDatedElsewhere" not in f and "datedElsewhere" not in report
    names = {v: k for k, v in j.items()}
    assert sorted(names[r["journeyId"]] for r in not_in_dump) == ["MISREAD", "RECEIPTONLY"]


def test_dms_rematch_job_dry_run_then_commit(dealer):
    """DMS_REMATCH: the current dumps matched again; a dry run writes nothing."""
    from audit_core.uc03_dms_rematch import run_dms_rematch

    with dealer.engine.begin() as c:  # the dump's synced row as if never matched
        c.execute(text("UPDATE auditcore.p2_dms_dump_rows SET outcome = 'MISSING', journey_id = NULL "
                       "WHERE tenant_id = :t"), {"t": dealer.tenant_id})
    dry = run_dms_rematch(dealer.engine, f"{dealer.tenant_id}:dms-rematch-test", [])
    assert (dry["commit"], dry["dumps"], dry["failed"]) == (False, 1, 0)
    with dealer.engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM auditcore.p2_dms_dump_rows WHERE tenant_id = :t "
                              "AND outcome <> 'MISSING'"), {"t": dealer.tenant_id}).scalar_one() == 0
    done = run_dms_rematch(dealer.engine, f"{dealer.tenant_id}:dms-rematch-test-commit", [])
    assert done["commit"] is True and done["failed"] == 0


def test_a_generated_report_is_corrected_in_place(dealer):
    """DMS_REPORT_REBUILD (product owner, 20 Nov 2026: "correct the already generated report"):
    the stored report keeps its id, date and author; its figures are counted again; a dry run
    writes nothing."""
    from uuid import uuid4 as new_id

    from audit_core.uc03_dms_report_rebuild import run_dms_report_rebuild

    report_id = new_id()
    old = {"bookingDms": 0, "bookingSynced": 0, "bookingDatedElsewhere": 0, "deliveryDump": 2, "deliverySynced": 5,
           "deliveryInVerigence": 4, "deliveryDatedElsewhere": 1}
    with dealer.engine.begin() as c:
        c.execute(text("""INSERT INTO auditcore.p2_dms_reports (tenant_id, report_id, report_date, period_month,
                dealer_ids, generated_by, generated_by_name, figures, report)
                VALUES (:t, :id, '2026-11-06', '2026-11-01', :ds, 'reviewer-1', 'Reviewer One',
                        CAST(:f AS jsonb), CAST(:r AS jsonb))"""),
                  {"t": dealer.tenant_id, "id": report_id, "ds": [dealer.dealer_id], "f": json.dumps(old),
                   "r": json.dumps({"figures": old, "datedElsewhere": [{"customerName": "X"}], "statementLines": []})})

    def stored() -> dict:
        with dealer.engine.begin() as c:
            return dict(c.execute(text("SELECT report_date, generated_by_name, figures, report FROM auditcore.p2_dms_reports "
                                       "WHERE tenant_id = :t AND report_id = :id"),
                                  {"t": dealer.tenant_id, "id": report_id}).mappings().one())

    dry = run_dms_report_rebuild(dealer.engine, f"{dealer.tenant_id}:dms-report-rebuild-test", [])
    assert (dry["commit"], dry["reports"], dry["failed"]) == (False, 1, 0)
    [item] = dry["items"]
    assert item["before"]["deliverySynced"] == 5
    # GATEPASS synced on the DMS's 3 Nov; the second row is not in Verigence.
    assert (item["after"]["deliveryDump"], item["after"]["deliverySynced"]) == (2, 1)
    assert stored()["figures"] == old  # nothing written

    done = run_dms_report_rebuild(dealer.engine, f"{dealer.tenant_id}:dms-report-rebuild-test-commit",
                                  [str(report_id)])
    assert (done["commit"], done["failed"]) == (True, 0)
    now = stored()
    assert (now["report_date"], now["generated_by_name"]) == (date(2026, 11, 6), "Reviewer One")
    f = now["figures"]
    assert (f["deliveryDump"], f["deliverySynced"]) == (2, 1)
    assert "deliveryDatedElsewhere" not in f and "datedElsewhere" not in now["report"]
    assert f["deliveryInVerigence"] == f["deliverySynced"] + f["deliveryNotInDms"]
    assert now["report"]["rebuilt"]["figuresBefore"]["deliverySynced"] == 5
    # Again: nothing changes but the time it was corrected.
    again = run_dms_report_rebuild(dealer.engine, f"{dealer.tenant_id}:dms-report-rebuild-test-commit", [])
    assert again["items"][0]["after"] == {k: f.get(k) for k in again["items"][0]["after"]}

