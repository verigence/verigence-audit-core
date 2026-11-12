"""Classified but not read (product owner, 12 Nov 2026): UPI screenshots never, a page of a
document read once merged, and -- for bulk and Drive uploads -- a one-per-journey document the
journey already holds read. See audit_core.uc03_p2_read_skip."""
from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_batch_pages,
    add_ready_document,
    create_p2_journey,
    database_engine,
    one_page_pdf,
)
from sqlalchemy import text

from audit_core import uc03_p2_read_skip as rs
from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.di_capture_v2_client import DiCaptureV2Client
from audit_core.uc03_p2_registry import get_registry


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="rskip")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _skip(journey, batch_id, **kw) -> list[str]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return rs.read_skip_types(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                  batch_id=batch_id, registry=get_registry(), **kw)


def test_the_upi_screenshot_is_never_read_on_any_upload() -> None:
    """"UPI receipts can be ignored to be extracted and this should be standard rule across"."""
    sent: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"uploads": []})

    client = DiCaptureV2Client(base_url="http://di.test", transport=httpx.MockTransport(handler))
    common = {"token": "t", "tenant_id": "tenant-1", "external_context_ref": "ctx-1", "phase": "BOOKING",
              "files": [{"clientUploadId": "c", "filename": "a.pdf"}], "candidate_document_type_keys": ["pan_card"]}
    client.create_upload_intents(**common)
    client.create_upload_intents(skip_reading_document_type_keys=["pan_card"], **common)
    assert sent[0]["skipReadingDocumentTypeKeys"] == ["upi_screenshot"]
    assert sent[1]["skipReadingDocumentTypeKeys"] == ["pan_card", "upi_screenshot"]


def test_what_can_come_more_than_once_is_always_read() -> None:
    once = rs.one_per_journey_di_types(get_registry())
    assert {"booking_form", "pan_card", "aadhaar", "customer_invoice_dms", "insurance_cover"} <= once
    # "dealership receipt and payment receipt are one only": the dealer_receipt type, always read.
    assert not once & {"dealer_receipt", "accessory_invoice_dms", "accessory_invoice_tally", "customer_ledger",
                       "cost_sheet", "no_dues_certificate", "upi_screenshot"}


def test_a_bulk_or_drive_upload_does_not_read_again_what_the_journey_holds(journey, monkeypatch):
    add_ready_document(journey, "booking_form", customer_name="Akansh")
    pan = add_ready_document(journey, "pan_card", pan_number="ABCDE1234F")
    add_ready_document(journey, "dealer_receipt", amount_paid="21000")
    batch_id, _ = add_batch_pages(journey, [("pan_card", "QUEUED")])
    # A PC's own upload in the app: only the UPI rule.
    assert _skip(journey, batch_id) == ["upi_screenshot"]
    monkeypatch.setattr(rs, "from_bulk_upload", lambda connection, **kw: True)
    assert _skip(journey, batch_id) == ["booking_form", "pan_card", "upi_screenshot"]
    # An open verification task on the PAN read: it is read again.
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, task_type, category, origin_kind, "
                 "source_type, source_code, dedupe_key, title, description, reference, severity, priority, "
                 "assigned_role_code, allowed_actions, completion_protocol, task_status, completion_result) "
                 "VALUES (:t, :j, 'MANUAL_VERIFICATION_REVIEW', 'DOCUMENT_VERIFICATION', 'SYSTEM', "
                 "'DOCUMENT_FIELD', 'X', :k, 'x', 'x', CAST(:r AS jsonb), 'HIGH', 'HIGH', 'PC', '[]'::jsonb, "
                 "'MACHINE_VERIFIED', 'READY', '{}'::jsonb)"),
            {"t": journey.tenant_id, "j": journey.journey_id, "k": f"mv:{uuid4()}",
             "r": json.dumps({"documentId": str(pan), "fields": []})},
        )
    assert _skip(journey, batch_id) == ["booking_form", "upi_screenshot"]
    # A page of a document read once merged is only classified.
    assert "aadhaar" in _skip(journey, batch_id, classify_only=frozenset({"aadhaar"}))


def test_pages_only_classified_are_still_merged_into_their_document(journey, monkeypatch):
    """A page DI did not read is settled as supporting; grouping must still use it."""
    from p2_support import MemoryStorage

    storage = MemoryStorage()
    monkeypatch.setattr(worker, "get_p2_document_storage", lambda: storage)
    batch_id, pages = add_batch_pages(journey, [("aadhaar", "SUPPORTING"), ("aadhaar", "SUPPORTING"),
                                                ("pan_card", "EXTRACTING")])
    for index, page in enumerate(pages):
        storage.objects[page["object_key"]] = one_page_pdf(100 * (index + 1))
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        worker._enqueue(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                        work_type="BATCH_GROUP", work_key=str(batch_id), payload={}, correlation_id=None)
    [item] = [i for i in worker._claim_for_tenant(journey.engine, journey.tenant_id, 20)
              if i.work_type == "BATCH_GROUP"]
    worker._group_batch(journey.engine, item)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        groups = connection.execute(
            text("SELECT page_numbers, candidate_override FROM auditcore.p2_document_queue "
                 "WHERE tenant_id=:t AND batch_id=:b AND unit_kind='GROUP'"),
            {"t": journey.tenant_id, "b": batch_id},
        ).mappings().all()
    assert [(g["page_numbers"], g["candidate_override"]) for g in groups] == [([1, 2], ["aadhaar"])]


def _bulk(journey, batch_id, source: str) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        bulk_id = connection.execute(
            text("INSERT INTO auditcore.p2_bulk_uploads (tenant_id, dealer_id, outlet_id, uploaded_by_actor_id, "
                 "folder, source) VALUES (:t, :d, :o, :a, 'f', :s) RETURNING bulk_id"),
            {"t": journey.tenant_id, "d": journey.dealer_id, "o": journey.outlet_id, "a": journey.actor_id,
             "s": source},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.p2_bulk_files (tenant_id, bulk_id, original_filename, content_type, "
                 "object_key, batch_id) VALUES (:t, :b, 'a.pdf', 'application/pdf', :key, :batch)"),
            {"t": journey.tenant_id, "b": bulk_id, "batch": str(batch_id), "key": f"k-{batch_id}"},
        )


def test_drive_pages_are_read_on_the_flex_tier_and_get_an_hour(journey):
    """"Used as batch ... lower the cost": Gemini's Flex tier, half price, answered in minutes."""
    from datetime import UTC, datetime, timedelta

    app_batch, _ = add_batch_pages(journey, [("pan_card", "CLASSIFYING")])
    web_bulk, _ = add_batch_pages(journey, [("pan_card", "CLASSIFYING")])
    drive_batch, _ = add_batch_pages(journey, [("pan_card", "CLASSIFYING")])
    _bulk(journey, web_bulk, "WEB")
    _bulk(journey, drive_batch, "DRIVE")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        tiers = [rs.gemini_tier(connection, tenant_id=journey.tenant_id, batch_id=b)
                 for b in (app_batch, web_bulk, drive_batch)]
        # A bulk upload from the web is a plain bulk upload: "already on the journey" applies.
        assert rs.from_bulk_upload(connection, tenant_id=journey.tenant_id, batch_id=web_bulk)
        connection.execute(text("UPDATE auditcore.p2_document_queue SET read_clock_started_at_utc=:c "
                                "WHERE tenant_id=:t"),
                           {"t": journey.tenant_id, "c": datetime.now(UTC) - timedelta(minutes=45)})
        worker.fail_unsettled_pages(connection, tenant_id=journey.tenant_id)
        status = dict(connection.execute(
            text("SELECT batch_id::text, queue_status FROM auditcore.p2_document_queue WHERE tenant_id=:t"),
            {"t": journey.tenant_id},
        ).all())
    assert tiers == [None, None, "FLEX"]
    # 45 minutes: the app and web pages are failed at 30, the Drive page waits to 60.
    assert (status[str(app_batch)], status[str(web_bulk)], status[str(drive_batch)]) == ("FAILED", "FAILED",
                                                                                           "CLASSIFYING")


def test_the_flex_tier_is_asked_for_only_when_set() -> None:
    sent: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"uploads": []})

    client = DiCaptureV2Client(base_url="http://di.test", transport=httpx.MockTransport(handler))
    common = {"token": "t", "tenant_id": "tenant-1", "external_context_ref": "ctx-1", "phase": "BOOKING",
              "files": [{"clientUploadId": "c", "filename": "a.pdf"}], "candidate_document_type_keys": ["pan_card"]}
    client.create_upload_intents(**common)
    client.create_upload_intents(gemini_tier="FLEX", **common)
    assert "geminiTier" not in sent[0] and sent[1]["geminiTier"] == "FLEX"
