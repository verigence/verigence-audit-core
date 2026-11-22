"""DMS dumps on a sure match: the DMS date taken, the mobile and name left, the model listed
(product owner 2026-11-21).

"if OTF matches then can we try to match other records and if verigence has some discrepancy ...
if it is for date can you update those date fields as per the DMS provided record matches ..
and can close the corresponding manual tasks .. dont update mobile number .. if you find a
difference in the model ... make a list of the cases and provide it to the person who uploaded
as an alert and downloadable as Excel sheet". Name differences are ignored; the DMS date wins
over a hand correction, the earlier value kept in the change log; the delivery dump does the
same on the chassis.
"""
from __future__ import annotations

import io
import json
from uuid import uuid4

from openpyxl import load_workbook
from p2_support import add_evidence, add_extracted_field, add_page
from sqlalchemy import text
from test_dms_intimations import _dump, _today, setup  # noqa: F401 -- the fixture

from audit_core import uc03_p2_dms_dumps as dumps
from audit_core.db import set_tenant_context


def _document(s, document_type: str, stage: str = "BOOKING", **fields) -> str:
    """A live document of the Journey (page, capture, ACTIVE evidence) with values read."""
    _, _, document = add_page(s.journey, page_number=int(uuid4().int % 900) + 10, status="READY")
    add_evidence(s.journey, di_document_id=document, document_type_key=document_type, process_area=stage)
    for key, value in fields.items():
        add_extracted_field(s.journey, di_document_id=document, field_key=key, value=value,
                            document_type=document_type, confidence=97.0)
    return str(document)


def _value(s, document: str, field_key: str):
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        return connection.execute(text(
            "SELECT effective_value #>> '{}' FROM auditcore.journey_document_extracted_fields "
            "WHERE tenant_id = :t AND di_document_id = CAST(:d AS uuid) AND field_key = :f"),
            {"t": s.journey.tenant_id, "d": document, "f": field_key}).scalar_one()


def _log(s) -> list[dict]:
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        return [dict(r) for r in connection.execute(text(
            "SELECT field_key, change_reason, actor_id, actor_role_code, previous_value, new_value, remarks "
            "FROM auditcore.journey_field_change_log WHERE tenant_id = :t AND journey_id = :j "
            "ORDER BY changed_at_utc"), {"t": s.journey.tenant_id, "j": s.journey.journey_id}).mappings()]


def _booking_date_task(s) -> dict:
    from audit_core.uc03_p2_task_producer import sync_booking_date_task

    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        return sync_booking_date_task(connection, tenant_id=s.journey.tenant_id, journey_id=s.journey.journey_id)


def _row(s, dump_id: str) -> dict:
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        return dict(connection.execute(text(
            "SELECT match_basis, journey_model, model_differs, date_sync FROM auditcore.p2_dms_dump_rows "
            "WHERE tenant_id = :t AND dump_id = :d AND row_no = 1"),
            {"t": s.journey.tenant_id, "d": dump_id}).mappings().one())


def test_a_booking_number_match_takes_the_dms_booking_date_and_leaves_the_mobile(setup) -> None:  # noqa: F811
    s = setup
    today = _today()
    # Sujit Kumar Bastia's case: the form's date read as 2020, the mobile a digit off.
    form = _document(s, "booking_form", booking_date="2020-11-07", booking_reference_number="B-77700011",
                     customer_phone="+919337354361")
    assert _booking_date_task(s) == {"RAISED": 1}
    dump_id = _dump(s, dumps.BOOKING, [{"booking_number": "B-77700011", "otf": "OTF27A099999",
                                       "name": "Another Spelling", "mobile": "9337359361", "model": "XEV 9S"}])
    row = _row(s, dump_id)
    assert row["match_basis"] == "BOOKING_NUMBER"
    assert row["date_sync"]["status"] == "APPLIED" and row["date_sync"]["dms"] == today.isoformat()
    assert _value(s, form, "booking_date") == today.isoformat()
    # the mobile is never changed
    assert _value(s, form, "customer_phone") == "+919337354361"
    logged = [e for e in _log(s) if e["change_reason"] == "DMS_SYNC"]
    assert len(logged) == 1
    assert logged[0]["field_key"] == "booking_date" and logged[0]["actor_role_code"] == "DMS_SYNC"
    assert logged[0]["actor_id"] == "rv" and "As per the DMS booking dump" in logged[0]["remarks"]
    assert json.loads(json.dumps(logged[0]["previous_value"])) == "2020-11-07"
    # The journey's check runs again (the worker, on the facts noted as changed): the booking
    # date task closes itself.
    assert _booking_date_task(s) == {"VERIFIED": 1}
    # Matched again: nothing more to take.
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        dumps.match_dump(connection, tenant_id=s.journey.tenant_id, dump_id=dump_id)
    assert len([e for e in _log(s) if e["change_reason"] == "DMS_SYNC"]) == 1


def test_the_dms_date_wins_over_a_hand_correction_and_keeps_it_in_the_log(setup) -> None:  # noqa: F811
    s = setup
    today = _today()
    form = _document(s, "booking_form", booking_date="2026-01-05", booking_reference_number="B-77700012")
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        from audit_core.uc03_p2_documents import apply_p2_field_correction

        apply_p2_field_correction(connection, tenant_id=s.journey.tenant_id, journey_id=s.journey.journey_id,
                                  document_id=form, field_key="booking_date", canonical_field_id="cf-booking_date",
                                  source_fact_version=1, new_value="2026-01-06", actor_id=s.journey.actor_id,
                                  actor_role_code="PC", remarks=None)
    _dump(s, dumps.BOOKING, [{"booking_number": "B-77700012", "name": "X", "model": "THAR"}])
    assert _value(s, form, "booking_date") == today.isoformat()
    reasons = [(e["change_reason"], json.loads(json.dumps(e["new_value"]))) for e in _log(s)]
    assert reasons == [("CORRECTION", "2026-01-06"), ("DMS_SYNC", today.isoformat())]


def test_a_mobile_or_name_match_changes_no_date(setup) -> None:  # noqa: F811
    s = setup
    form = _document(s, "booking_form", booking_date="2020-11-07", customer_phone="9000000077")
    dump_id = _dump(s, dumps.BOOKING, [{"booking_number": "B-99999999", "name": "X", "mobile": "9000000077",
                                       "model": "THAR"}])
    row = _row(s, dump_id)
    assert row["match_basis"] == "MOBILE"
    assert row["date_sync"] is None and row["model_differs"] is None
    assert _value(s, form, "booking_date") == "2020-11-07"
    assert not [e for e in _log(s) if e["change_reason"] == "DMS_SYNC"]


def test_a_chassis_match_takes_the_dms_delivery_date_on_the_gate_pass(setup) -> None:  # noqa: F811
    s = setup
    today = _today()
    gate = _document(s, "gate_pass", stage="DELIVERY", delivery_date="2025-11-06", vin_number="MA1XX2T2K55617")
    ndc = _document(s, "customer_ledger", stage="DELIVERY", delivery_date="2025-11-01")
    dump_id = _dump(s, dumps.DELIVERY, [{"chassis": "T2K55617", "name": "X", "model": "XUV 3XO"}])
    assert _row(s, dump_id)["match_basis"] == "CHASSIS"
    assert _value(s, gate, "delivery_date") == today.isoformat()
    # the NDC's date is the fallback when there is no gate pass: left as it is here
    assert _value(s, ndc, "delivery_date") == "2025-11-01"


def test_a_different_model_is_listed_for_the_uploader_with_the_excel(setup) -> None:  # noqa: F811
    s = setup
    _document(s, "booking_form", booking_date=_today().isoformat(), booking_reference_number="B-77700013")
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(text(
            "UPDATE auditcore.journey_products SET model_name_snapshot = 'XUV-9S' "
            "WHERE tenant_id = :t AND journey_id = :j"), {"t": s.journey.tenant_id, "j": s.journey.journey_id})
    dump_id = _dump(s, dumps.BOOKING, [{"booking_number": "B-77700013", "name": "X", "model": "XEV 9S"}])
    row = _row(s, dump_id)
    assert row["model_differs"] is True
    assert row["date_sync"] is None  # the date already agreed
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        dumps.publish_model_alert(connection, tenant_id=s.journey.tenant_id, dump_id=dump_id, uploader=s.reviewer,
                                  dealer_name="Dealer", kind=dumps.BOOKING, business_date=_today())
        alert = connection.execute(text(
            "SELECT title, payload FROM auditcore.p2_tenant_alerts WHERE tenant_id = :t AND alert_kind = :k"),
            {"t": s.journey.tenant_id, "k": dumps.MODEL_ALERT_KIND}).mappings().one()
    assert alert["title"] == "Model differs on 1 booking"
    assert alert["payload"]["audienceActorIds"] == [s.reviewer]
    s.acting["subject"] = s.reviewer
    listed = s.client.get(f"{s.base}/dms-dumps").json()["items"]
    assert next(d for d in listed if d["dumpId"] == dump_id)["modelDiffers"] == 1
    got = s.client.get(f"{s.base}/dms-dumps/{dump_id}/model-differences/file")
    assert got.status_code == 200, got.text
    sheet = load_workbook(io.BytesIO(got.content)).active
    header = [c.value for c in sheet[1]]
    line = dict(zip(header, [c.value for c in sheet[2]], strict=True))
    assert line["Model as per DMS"] == "XEV 9S" and line["Booking no."] == "B-77700013"


def test_a_held_or_replaced_form_gives_its_numbers_to_no_match(setup) -> None:  # noqa: F811
    """2026-11-22: another customer's booking form, held as FOREIGN on a Journey, still gave the
    Journey that customer's OTF and mobile -- the DMS dump gave it their row by booking number.
    Only the papers on file now count."""
    s = setup
    held = _document(s, "booking_form", booking_reference_number="B-55500077", customer_phone="+919000011122")
    replaced = _document(s, "booking_form", booking_reference_number="B-55500088", customer_phone="+919000033344")
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        for document, status in ((held, "FOREIGN"), (replaced, "SUPERSEDED")):
            connection.execute(text(
                "UPDATE auditcore.evidence SET association_status = :s "
                "WHERE tenant_id = :t AND di_document_id = CAST(:d AS uuid)"),
                {"s": status, "t": s.journey.tenant_id, "d": document})
        index = dumps.build_index(connection, tenant_id=s.journey.tenant_id, dealer_id=s.journey.dealer_id)
    refs = index.journeys.get(str(s.journey.journey_id), {}).get("refs") or set()
    mobiles = index.journeys.get(str(s.journey.journey_id), {}).get("mobiles") or set()
    assert not any("55500077" in r or "55500088" in r for r in refs)
    assert not any("9000011122" in m or "9000033344" in m for m in mobiles)
    dump_id = _dump(s, dumps.BOOKING, [{"booking_number": "B-55500077", "otf": "OTF27A055555",
                                       "name": "Someone Else", "mobile": "9000011122", "model": "XEV 9S"}])
    assert _row(s, dump_id)["match_basis"] is None


def test_a_booking_cancelled_in_verigence_takes_no_booking_row_but_its_chassis_still_finds_it(setup) -> None:  # noqa: F811
    """2026-11-22: a journey the PC closed as "document upload failed" (a fresh one started for
    the same sale) is no booking: no booking row goes to it by OTF or mobile. Journeys closed that
    way also held complete deliveries: the chassis, the vehicle itself, still finds them."""
    s = setup
    _document(s, "booking_form", booking_reference_number="B-66600011", customer_phone="+919000044455")
    _document(s, "customer_invoice_dms", "DELIVERY", vin_number="MA1TESTCANCEL0001")
    with s.journey.engine.begin() as connection:
        set_tenant_context(connection, s.journey.tenant_id)
        connection.execute(text("""
            INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code, business_status)
            VALUES (:t, :j, 'BOOKING', 'BOOKING_CANCELLED')
            ON CONFLICT (tenant_id, journey_id, stage_code) DO UPDATE SET business_status = 'BOOKING_CANCELLED'
        """), {"t": s.journey.tenant_id, "j": s.journey.journey_id})
        index = dumps.build_index(connection, tenant_id=s.journey.tenant_id, dealer_id=s.journey.dealer_id)
    mine = str(s.journey.journey_id)
    assert index.journeys[mine]["cancelled"]
    booking = {"booking_number": "B-66600011", "mobile": "9000044455", "customer_name": "Someone"}
    assert dumps.match_row(index, dumps.BOOKING, booking) == (None, None)
    assert dumps.match_row(index, dumps.DELIVERY, {"chassis": "MA1TESTCANCEL0001"}) == (mine, "CHASSIS")
