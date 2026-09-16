from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

import audit_core.uc03_document_unrecognized as du


# ── unit ──────────────────────────────────────────────────────────────────────
def test_effect_key_is_namespaced_per_tenant_stage_and_document() -> None:
    doc = uuid4()
    assert du._effect_key("t1", "j1", "DELIVERY", doc) == f"task:document-unrecognized:t1:j1:DELIVERY:{doc}"
    assert du._effect_key("t1", "j1", "BOOKING", doc) == f"task:document-unrecognized:t1:j1:BOOKING:{doc}"


def test_friendly_filename_falls_back_to_short_document_id() -> None:
    doc = uuid4()
    assert du._friendly_filename("credit_note.pdf", doc) == "credit_note.pdf"
    assert du._friendly_filename(None, doc) == f"document {str(doc)[:8]}"
    assert du._friendly_filename("  ", doc) == f"document {str(doc)[:8]}"


# ── integration ───────────────────────────────────────────────────────────────
@pytest.fixture
def journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for document-unrecognized integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-du-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"DU-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"DU-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'DU', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"DU-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"DU-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"DU-O-{suffix}"},
        ).scalar_one()
        customer_id = c.execute(
            text("""INSERT INTO auditcore.customers
                (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = c.execute(
            text("""INSERT INTO auditcore.journeys
                (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"DU-J-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.journey_stage_states
                (tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                 first_started_at_utc, latest_activity_at_utc, version_no)
                VALUES (:t, :j, 'DELIVERY', 'DELIVERY_IN_PROGRESS', 'IN_PROGRESS', 'NOT_EVALUATED',
                        now(), now(), 1)"""),
            {"t": tenant_id, "j": journey_id},
        )
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_id = journey_id  # type: ignore[attr-defined]
        yield c
    engine.dispose()


def _unknown_document(filename: str) -> dict:
    return {
        "documentId": str(uuid4()),
        "clientUploadId": "client-1",
        "state": "UNKNOWN",
        "classifiedDocumentTypeKey": None,
        "originalFilename": filename,
        "contentUrl": f"https://example.test/{filename}",
        "processingStatus": None,
    }


def _open_task(c, *, tenant_id: str, task_type: str) -> dict | None:
    return c.execute(
        text(
            "SELECT workflow_task_id, task_status, related_finding_id, task_payload, "
            "assigned_role_code, process_area FROM auditcore.workflow_tasks "
            "WHERE tenant_id = :t AND task_type = :tt"
        ),
        {"t": tenant_id, "tt": task_type},
    ).mappings().one_or_none()


def test_producer_raises_one_standalone_task_per_unrecognized_document_no_finding(journey) -> None:
    """No Audit Finding at all -- DI not recognizing a document is a "please
    verify" job for PC, not a rule violation or a compliance gap."""
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    unknown = _unknown_document("gst_declaration.pdf")
    classified = {
        "documentId": str(uuid4()),
        "clientUploadId": "client-2",
        "state": "CLASSIFIED",
        "classifiedDocumentTypeKey": "payment_receipt",
        "originalFilename": "receipt.pdf",
        "contentUrl": None,
        "processingStatus": None,
    }

    result = du.sync_document_unrecognized_findings(
        journey, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
        di_documents=[unknown, classified], correlation_id="",
    )
    assert result["raised"] == 1  # only the UNKNOWN one

    assert journey.execute(
        text("SELECT count(*) FROM auditcore.audit_findings WHERE tenant_id = :t"), {"t": tenant_id}
    ).scalar_one() == 0

    task = _open_task(journey, tenant_id=tenant_id, task_type=du.TASK_TYPE)
    assert task is not None
    assert task["task_status"] == "READY"
    assert task["related_finding_id"] is None
    assert task["assigned_role_code"] == "PC"
    assert task["process_area"] == "DELIVERY"
    assert task["task_payload"]["diDocumentId"] == unknown["documentId"]
    assert "gst_declaration.pdf" in task["task_payload"]["comment"]

    # idempotent -- calling again with the same live document doesn't duplicate
    again = du.sync_document_unrecognized_findings(
        journey, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
        di_documents=[unknown, classified], correlation_id="",
    )
    assert again["raised"] == 0
    assert journey.execute(
        text("SELECT count(*) FROM auditcore.workflow_tasks WHERE tenant_id = :t AND task_type = :tt"),
        {"t": tenant_id, "tt": du.TASK_TYPE},
    ).scalar_one() == 1


def test_reclassified_document_cancels_its_task(journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    unknown = _unknown_document("mystery.pdf")

    du.sync_document_unrecognized_findings(
        journey, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
        di_documents=[unknown], correlation_id="",
    )
    assert _open_task(journey, tenant_id=tenant_id, task_type=du.TASK_TYPE)["task_status"] == "READY"

    # A later DI pass reclassifies it -- the document is no longer in the
    # UNKNOWN set on the next capture-screen read.
    reclassified = dict(unknown, state="CLASSIFIED", classifiedDocumentTypeKey="credit_note")
    result = du.sync_document_unrecognized_findings(
        journey, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
        di_documents=[reclassified], correlation_id="",
    )
    assert result["resolved"] == 1

    status = journey.execute(
        text("SELECT task_status FROM auditcore.workflow_tasks WHERE tenant_id = :t AND task_type = :tt"),
        {"t": tenant_id, "tt": du.TASK_TYPE},
    ).scalar_one()
    assert status == "CANCELLED"


def test_apply_verification_correct_outcome_leaves_document_untouched(journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    document_id = uuid4()
    journey.execute(
        text("""INSERT INTO auditcore.document_capture_v2_documents
            (tenant_id, journey_id, stage_code, di_document_id, client_upload_id, capture_status, created_by_actor_id)
            VALUES (:t, :j, 'DELIVERY', :doc_id, 'upload-1', 'UNKNOWN', 'test-actor')"""),
        {"t": tenant_id, "j": journey_id, "doc_id": document_id},
    )

    du.apply_unrecognized_document_verification(
        journey, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
        di_document_id=document_id, outcome="CORRECT",
    )

    status = journey.execute(
        text("SELECT capture_status FROM auditcore.document_capture_v2_documents "
             "WHERE tenant_id = :t AND di_document_id = :d"),
        {"t": tenant_id, "d": document_id},
    ).scalar_one()
    assert status == "UNKNOWN"


def test_apply_verification_incorrect_outcome_supersedes_the_document(journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    document_id = uuid4()
    journey.execute(
        text("""INSERT INTO auditcore.document_capture_v2_documents
            (tenant_id, journey_id, stage_code, di_document_id, client_upload_id, capture_status, created_by_actor_id)
            VALUES (:t, :j, 'DELIVERY', :doc_id, 'upload-1', 'UNKNOWN', 'test-actor')"""),
        {"t": tenant_id, "j": journey_id, "doc_id": document_id},
    )

    du.apply_unrecognized_document_verification(
        journey, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
        di_document_id=document_id, outcome="INCORRECT",
    )

    status = journey.execute(
        text("SELECT capture_status FROM auditcore.document_capture_v2_documents "
             "WHERE tenant_id = :t AND di_document_id = :d"),
        {"t": tenant_id, "d": document_id},
    ).scalar_one()
    assert status == "SUPERSEDED"
