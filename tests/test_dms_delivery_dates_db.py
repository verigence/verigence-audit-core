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

        def journey(name: str, *, delivery_stage: bool, gate_pass: str | None = None, ndc: str | None = None,
                    receipt: date | None = None) -> None:
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
        journey("RECEIPTONLY", delivery_stage=True, receipt=date(2026, 11, 2))
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
