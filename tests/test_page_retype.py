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
    assert tenant == "t" and commit and items == [("UTK-SA-261027-002", 17, "supporting_document", None)]
    _, _, [item] = retype.parse_request("t:x", ["A-1:1:customer_ledger:booking_docket"])
    assert item == ("A-1", 1, "customer_ledger", "booking_docket")
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


def test_the_current_type_picks_the_page_when_two_uploads_share_its_number(journey):
    """Live 2026-11-03 (SHI-SS-261031-005): page 1 of the delivery file is the
    Offer Letter, page 1 of a second upload is the Tally ledger."""
    _, [letter] = add_batch_pages(journey, [("booking_form", "READY")], grouping_status="NOT_NEEDED")
    _, [ledger] = add_batch_pages(journey, [("customer_ledger", "READY")], grouping_status="NOT_NEEDED")
    reference = _reference(journey)
    plain = retype.run_page_retype(journey.engine, f"{journey.tenant_id}:look", [f"{reference}:1:customer_ledger"])
    assert plain["wouldSend"][0]["from"] == "customer_ledger"  # the newest upload's page 1
    named = retype.run_page_retype(journey.engine, f"{journey.tenant_id}:look",
                                   [f"{reference}:1:customer_ledger:booking_form"])
    assert named["wouldSend"][0]["from"] == "booking_form"
    assert _unit(journey, letter["queue_id"])["queue_status"] == "READY"
    assert _unit(journey, ledger["queue_id"])["queue_status"] == "READY"


def test_a_document_never_read_is_split_with_each_page_read_as_named(journey):
    """Live 2026-11-03 (SHI-SS-261027-007): a delivery-file cover sheet and the
    Offer Letter were joined into one document that was never read
    (CANCELLED). Each page is read alone: the Offer Letter as the NDC, the
    cover as the type the same job names for it."""
    batch_id, pages = add_batch_pages(journey, [("booking_form", "MERGED"), ("booking_form", "MERGED")],
                                      grouping_status="NOT_NEEDED")
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        group = c.execute(text(
            "INSERT INTO auditcore.p2_document_queue (tenant_id, batch_id, journey_id, page_number, page_numbers, "
            "page_sha256, page_object_key, client_upload_id, queue_status, unit_kind, group_source, "
            "classified_document_type, template_key, business_stage) VALUES (:t, :b, :j, 1, ARRAY[1,2], 'g2', "
            "'p2/g2.pdf', 'p2-group-y', 'CANCELLED', 'GROUP', 'SYSTEM', 'booking_form', 'booking_docket', "
            "'BOOKING') RETURNING queue_id"), {"t": journey.tenant_id, "b": batch_id, "j": journey.journey_id}).scalar_one()
        c.execute(text("UPDATE auditcore.p2_document_queue SET merged_into_queue_id=:g WHERE queue_id = ANY(:q)"),
                  {"g": group, "q": [p["queue_id"] for p in pages]})
    reference = _reference(journey)
    done = retype.run_page_retype(journey.engine, f"{journey.tenant_id}:split-commit",
                                  [f"{reference}:2:customer_ledger", f"{reference}:1:supporting_document"])
    assert [s["page"] for s in done["sent"]] == [2] and done["skipped"] == []
    sent = {p["page"]: p["to"] for p in done["sent"][0]["pages"]}
    assert sent == {2: "customer_ledger", 1: "supporting_document"}
    assert _unit(journey, group)["queue_status"] == "MERGED"


def test_a_page_kept_as_a_supporting_document_stays_on_file_and_counts_as_nothing(journey):
    """2026-11-05: the dealers' Transaction Regularization Form, read as a cost
    sheet, is kept as a supporting document under its printed title."""
    from uuid import uuid4

    from p2_support import add_evidence, add_extracted_field

    document_id = uuid4()
    _, [page] = add_batch_pages(journey, [("cost_sheet", "READY")], grouping_status="NOT_NEEDED")
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_document_queue SET di_document_id=:d, template_key='cost_sheet' "
                       "WHERE queue_id=:q"), {"d": document_id, "q": page["queue_id"]})
    add_evidence(journey, di_document_id=document_id, document_type_key="cost_sheet")
    add_extracted_field(journey, di_document_id=document_id, field_key="document_title",
                        value="TRANSACTION REGULARIZATION FORM", document_type="cost_sheet")
    number = _unit(journey, page["queue_id"])["page_number"]
    ask = [f"{_reference(journey)}:{number}:supporting_document:cost_sheet"]
    dry = retype.run_page_retype(journey.engine, f"{journey.tenant_id}:trf", ask)
    assert len(dry["wouldSend"]) == 1 and _unit(journey, page["queue_id"])["queue_status"] == "READY"
    out = retype.run_page_retype(journey.engine, f"{journey.tenant_id}:trf-commit", ask)
    assert out["sent"][0]["name"] == "Transaction Regularization Form"
    unit = _unit(journey, page["queue_id"])
    assert (unit["queue_status"], unit["template_key"], unit["display_name"]) == (
        "SUPPORTING", "supporting_document", "Transaction Regularization Form")
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        status = c.execute(text("SELECT association_status FROM auditcore.evidence WHERE di_document_id=:d"),
                           {"d": document_id}).scalar_one()
    assert status == "SUPERSEDED"
