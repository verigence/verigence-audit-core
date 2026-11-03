"""PAGE_RETYPE (2026-11-02): stored pages read again as a named type, through
the same path a PC's "Set the document type" takes."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import add_batch_pages, create_p2_journey, database_engine, queue_row
from sqlalchemy import text

from audit_core import uc03_page_retype as retype
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_worker import _one_shot_target


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="rtyp")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _reference(journey) -> str:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return c.execute(text("SELECT journey_reference FROM auditcore.journeys WHERE journey_id=:j"),
                         {"j": journey.journey_id}).scalar_one()


def _unit(journey, queue_id) -> dict:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return dict(c.execute(text("SELECT * FROM auditcore.p2_document_queue WHERE queue_id=:q"),
                              {"q": queue_id}).mappings().one())


def test_a_request_names_the_tenant_and_each_page_with_its_type():
    tenant, commit, items = retype.parse_request("t:try-commit", ["utk-sa-261027-002:17:supporting_document"])
    assert tenant == "t" and commit and items == [("UTK-SA-261027-002", 17, "supporting_document")]
    for tag, reports in (("no-label", ["A-1:1:x_y"]), ("t:x", []), ("t:x", ["A-1:1"]), ("t:x", ["A-1:0:abc"]),
                         ("t:x", ["A-1:1:Bad-Key"])):
        with pytest.raises(ValueError):
            retype.parse_request(tag, reports)


def test_the_worker_knows_the_job():
    target, args = _one_shot_target("PAGE_RETYPE", None, "t:x", ["A-1:1:customer_kyc"])
    assert target is retype.run_page_retype and args == (None, "t:x", ["A-1:1:customer_kyc"])


def test_a_dry_run_lists_and_a_commit_sends_the_page_as_the_type(journey):
    _, [page] = add_batch_pages(journey, [(None, "SUPPORTING")], grouping_status="NOT_NEEDED")
    number = _unit(journey, page["queue_id"])["page_number"]
    ask = [f"{_reference(journey)}:{number}:customer_kyc", f"{_reference(journey)}:99:customer_kyc"]

    dry = retype.run_page_retype(journey.engine, f"{journey.tenant_id}:look", ask)
    assert [i["to"] for i in dry["wouldSend"]] == ["customer_kyc"] and dry["sent"] == []
    assert [s["why"] for s in dry["skipped"]] == ["no live page"]
    assert _unit(journey, page["queue_id"])["queue_status"] == "SUPPORTING"

    done = retype.run_page_retype(journey.engine, f"{journey.tenant_id}:go-commit", ask[:1])
    new_id = done["sent"][0]["newQueueId"]
    new_unit = _unit(journey, new_id)
    assert new_unit["candidate_override"] == ["customer_kyc"] and new_unit["type_overridden_by_actor_id"] == retype._ACTOR
    assert _unit(journey, page["queue_id"])["queue_status"] == "MERGED"
    assert queue_row(journey, "DOCUMENT_INGEST", new_id) is not None


def test_a_page_read_with_others_is_split_and_each_page_read_alone(journey):
    """Live 2026-11-03 (SHI-SS-261031-008): Shivnath's Offer Letter (page 1) was
    read together with the Mahindra booking form (page 3) as one booking
    docket. Asked as the NDC, page 1 is read alone as the NDC, page 3 alone as
    the booking docket, and the joined document is retired."""
    batch_id, pages = add_batch_pages(journey, [("booking_form", "MERGED"), ("aadhaar", "READY"),
                                                ("booking_form", "MERGED")], grouping_status="NOT_NEEDED")
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        group = c.execute(text(
            "INSERT INTO auditcore.p2_document_queue (tenant_id, batch_id, journey_id, page_number, page_numbers, "
            "page_sha256, page_object_key, client_upload_id, queue_status, unit_kind, group_source, "
            "classified_document_type, template_key, business_stage) VALUES (:t, :b, :j, 1, ARRAY[1,3], 'g', "
            "'p2/g.pdf', 'p2-group-x', 'READY', 'GROUP', 'SYSTEM', 'booking_form', 'booking_docket', 'BOOKING') "
            "RETURNING queue_id"), {"t": journey.tenant_id, "b": batch_id, "j": journey.journey_id}).scalar_one()
        c.execute(text("UPDATE auditcore.p2_document_queue SET merged_into_queue_id=:g WHERE queue_id = ANY(:q)"),
                  {"g": group, "q": [pages[0]["queue_id"], pages[2]["queue_id"]]})
    ask = [f"{_reference(journey)}:1:customer_ledger"]

    dry = retype.run_page_retype(journey.engine, f"{journey.tenant_id}:look", ask)
    assert dry["wouldSend"][0]["splitFrom"] == [1, 3] and _unit(journey, group)["queue_status"] == "READY"

    done = retype.run_page_retype(journey.engine, f"{journey.tenant_id}:split-commit", ask)
    sent = {p["page"]: p for p in done["sent"][0]["pages"]}
    assert {k: v["to"] for k, v in sent.items()} == {1: "customer_ledger", 3: "booking_docket"}
    for number, own in ((1, pages[0]), (3, pages[2])):
        unit = _unit(journey, sent[number]["newQueueId"])
        assert unit["page_numbers"] == [number] and unit["page_object_key"] == own["object_key"]
        assert unit["template_key"] == sent[number]["to"] and unit["queue_status"] == "QUEUED"
        assert _unit(journey, own["queue_id"])["merged_into_queue_id"] == unit["queue_id"]
        assert queue_row(journey, "DOCUMENT_INGEST", sent[number]["newQueueId"]) is not None
    retired = _unit(journey, group)
    assert retired["queue_status"] == "MERGED" and str(retired["merged_into_queue_id"]) == sent[1]["newQueueId"]
    assert _unit(journey, pages[1]["queue_id"])["queue_status"] == "READY"  # the page outside the group
