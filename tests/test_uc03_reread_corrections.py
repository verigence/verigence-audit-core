"""A person's corrections survive a page being read again (2026-11-10).

Live, UTK-SU-261020-005: the Reviewer set the struck-out accessories Rs 7,000
to 0 on the cost sheet; the page was read again as the NDC and Rs 7,000 came
back. Real Postgres."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_batch_pages,
    add_evidence,
    add_extracted_field,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import uc03_cost_sheet_statements as statements
from audit_core import uc03_page_retype as retype
from audit_core import uc03_reread_corrections as carry
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_worker import _one_shot_target


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="rrc")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql: str, **params):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return c.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _reference(journey) -> str:
    return _sql(journey, "SELECT journey_reference FROM auditcore.journeys WHERE journey_id=:j").scalar_one()


def _cost_sheet_page(journey, *, accessories=7000):
    _, [page] = add_batch_pages(journey, [("cost_sheet", "READY")], grouping_status="NOT_NEEDED")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET template_key='cost_sheet', business_stage='DELIVERY' "
                  "WHERE queue_id=:q", q=page["queue_id"])
    add_evidence(journey, di_document_id=page["di_document_id"], document_type_key="cost_sheet",
                 process_area="DELIVERY")
    for key, value in (("accessories_amount", accessories), ("ex_showroom_price", 1011500)):
        add_extracted_field(journey, di_document_id=page["di_document_id"], field_key=key, value=value,
                            document_type="cost_sheet")
    return page


def _person_corrects(journey, document_id, field_key, value, *, role="REVIEWER", remarks="struck out"):
    _sql(journey, "UPDATE auditcore.journey_document_extracted_fields SET effective_value=CAST(:v AS jsonb), "
                  "modified_value=CAST(:v AS jsonb), modified_by_actor_id='priyanka', modified_at_utc=now(), "
                  "is_modified=true WHERE di_document_id=:d AND field_key=:k",
         v=json.dumps(value), d=document_id, k=field_key)
    _sql(journey, "INSERT INTO auditcore.journey_field_change_log (tenant_id, journey_id, stage_code, di_document_id, "
                  "field_key, previous_value, new_value, change_reason, actor_id, actor_role_code, remarks) "
                  "VALUES (:t, :j, 'DELIVERY', :d, :k, '7000', CAST(:v AS jsonb), 'CORRECTION', 'priyanka', :r, :m)",
         d=document_id, k=field_key, v=json.dumps(value), r=role, m=remarks)


def _send_again(journey, page, template: str) -> str:
    done = retype.run_page_retype(journey.engine, f"{journey.tenant_id}:again-commit",
                                  [f"{_reference(journey)}:{page['page_number']}:{template}:cost_sheet"])
    return done["sent"][0]["newQueueId"]


def _read(journey, queue_id, document_type: str, **fields):
    """The new unit's reading lands: READY, with its document and fields."""
    document_id = uuid4()
    _sql(journey, "UPDATE auditcore.p2_document_queue SET queue_status='READY', di_document_id=:d WHERE queue_id=:q",
         d=document_id, q=queue_id)
    add_evidence(journey, di_document_id=document_id, document_type_key=document_type, process_area="DELIVERY")
    for key, value in fields.items():
        add_extracted_field(journey, di_document_id=document_id, field_key=key, value=value,
                            document_type=document_type)
    return document_id


def _value(journey, document_id, field_key):
    return _sql(journey, "SELECT effective_value FROM auditcore.journey_document_extracted_fields "
                         "WHERE di_document_id=:d AND field_key=:k ORDER BY source_fact_version DESC, "
                         "updated_at_utc DESC LIMIT 1", d=document_id, k=field_key).scalar_one()


def test_a_correction_is_applied_again_when_the_page_is_read_again_as_the_same_type(journey):
    page = _cost_sheet_page(journey)
    _person_corrects(journey, page["di_document_id"], "accessories_amount", 0)
    new_queue = _send_again(journey, page, "cost_sheet")
    assert carry.settle(journey.engine, [journey.tenant_id])["settled"] == 0  # still being read

    document = _read(journey, new_queue, "cost_sheet", accessories_amount=7000, ex_showroom_price=1011500)
    totals = carry.settle(journey.engine, [journey.tenant_id])
    assert totals["settled"] == 1 and totals["carried"] == 1 and totals["listed"] == 0
    assert _value(journey, document, "accessories_amount") == 0
    remark = _sql(journey, "SELECT remarks FROM auditcore.journey_field_change_log WHERE di_document_id=:d "
                           "AND actor_id=:a", d=document, a=carry.ACTOR).scalar_one()
    assert "the Reviewer" in remark and "struck out" in remark
    assert carry.settle(journey.engine, [journey.tenant_id])["settled"] == 0  # once only


def test_a_correction_the_new_reading_has_no_field_for_is_listed_for_the_pc(journey):
    page = _cost_sheet_page(journey)
    _person_corrects(journey, page["di_document_id"], "accessories_amount", 0)
    new_queue = _send_again(journey, page, "customer_ledger")
    _read(journey, new_queue, "customer_ledger", line_items=[{"particulars_raw": "Accessories", "debit_amount": 7000}])

    totals = carry.settle(journey.engine, [journey.tenant_id])
    assert totals["listed"] == 1 and totals["carried"] == 0
    task = _sql(journey, "SELECT task_type, description FROM auditcore.p2_tasks WHERE journey_id=:j "
                         "AND task_type='CORRECTIONS_RECHECK'").mappings().one()
    assert "accessories_amount: 7000 → 0 by the Reviewer" in task["description"]


def test_a_page_that_was_not_read_again_settles_with_nothing_done(journey):
    page = _cost_sheet_page(journey)
    _person_corrects(journey, page["di_document_id"], "accessories_amount", 0)
    new_queue = _send_again(journey, page, "cost_sheet")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET queue_status='FAILED' WHERE queue_id=:q", q=new_queue)
    assert carry.settle(journey.engine, [journey.tenant_id])["notRead"] == 1


def test_a_page_nobody_corrected_records_nothing(journey):
    page = _cost_sheet_page(journey)
    _send_again(journey, page, "cost_sheet")
    assert _sql(journey, "SELECT count(*) FROM auditcore.p2_reread_corrections WHERE journey_id=:j").scalar_one() == 0


def test_a_cost_sheet_read_before_its_title_was_is_read_again_and_counted(journey):
    """The 5 Nov job judged only titled pages: the 93 read before were never judged."""
    untitled = _cost_sheet_page(journey)
    titled = _cost_sheet_page(journey)
    add_extracted_field(journey, di_document_id=titled["di_document_id"], field_key="document_title",
                        value="Quotation", document_type="cost_sheet")
    found = statements.untitled_pages(journey.engine, journey.tenant_id)
    assert [p["di_document_id"] for p in found] == [untitled["di_document_id"]]

    dry = statements.run_cost_sheet_statements(journey.engine, f"{journey.tenant_id}:look", [])
    # The untitled page is read again as a cost sheet; the quotation is left alone.
    assert dry["remaining"] == 1 and dry["found"] == 0 and dry["wouldSend"] == 1 and dry["sent"] == 0


def test_field_set_puts_a_value_back_with_the_trail(journey):
    page = _cost_sheet_page(journey)
    ask = [json.dumps({"journey": _reference(journey), "documentId": str(page["di_document_id"]),
                       "fieldKey": "accessories_amount", "value": 0, "remarks": "Struck out on the NDC"})]
    target, args = _one_shot_target("FIELD_SET", journey.engine, f"{journey.tenant_id}:look", ask)
    dry = target(*args)
    assert dry["set"] == [{"journey": _reference(journey), "fieldKey": "accessories_amount", "from": 7000, "to": 0}]
    assert _value(journey, page["di_document_id"], "accessories_amount") == 7000

    carry.run_field_set(journey.engine, f"{journey.tenant_id}:fix-commit", ask)
    assert _value(journey, page["di_document_id"], "accessories_amount") == 0
    no_remark = [json.dumps({**json.loads(ask[0]), "remarks": ""})]
    assert carry.run_field_set(journey.engine, f"{journey.tenant_id}:fix-commit", no_remark)["skipped"]


def test_a_page_read_again_once_is_never_read_again_by_the_sweep(journey):
    """The sweep runs hourly: a page already read again with no title is left alone."""
    page = _cost_sheet_page(journey)
    new_queue = _send_again(journey, page, "cost_sheet")
    _read(journey, new_queue, "cost_sheet", ex_showroom_price=1011500)  # still no title
    _sql(journey, "UPDATE auditcore.evidence SET association_status='VOIDED' WHERE di_document_id=:d",
         d=page["di_document_id"])
    found = statements.untitled_pages(journey.engine, journey.tenant_id)
    assert len(found) == 1 and found[0]["page_number"] is None


def test_the_worker_sweeps_cost_sheet_statements_every_hour():
    from audit_core import uc03_p2_worker as worker

    assert worker._COST_SHEET_SWEEP_SECONDS == 3600 and callable(worker._sweep_cost_sheet_statements_every)


def test_each_page_is_sent_by_its_own_upload_when_two_uploads_share_a_page_number(journey):
    """Live 2026-11-10, UTK-BI-261020-005: page 1 of one upload was the
    statement, page 1 of another an untitled cost sheet. By page number the job
    sent the newest page 1 as a cost sheet, four times; the statement never moved."""
    statement = _cost_sheet_page(journey)
    add_extracted_field(journey, di_document_id=statement["di_document_id"], field_key="document_title",
                        value="VEHICLE ACCOUNT STATEMENT", document_type="cost_sheet")
    untitled = _cost_sheet_page(journey)  # the newer upload's page 1
    assert statement["page_number"] == untitled["page_number"] == 1

    done = statements.run_cost_sheet_statements(journey.engine, f"{journey.tenant_id}:go-commit", [])
    assert done["sent"] == 2 and done["skipped"] == []
    moved = {r["queue_id"]: r for r in _sql(
        journey, "SELECT o.queue_id, n.template_key FROM auditcore.p2_document_queue o "
                 "JOIN auditcore.p2_document_queue n ON n.queue_id = o.merged_into_queue_id "
                 "WHERE o.journey_id = :j").mappings().all()}
    assert moved[statement["queue_id"]]["template_key"] == "customer_ledger"
    assert moved[untitled["queue_id"]]["template_key"] == "cost_sheet"
