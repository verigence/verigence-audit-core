from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text

import audit_core.uc03_document_unrecognized as du


# ── unit ──────────────────────────────────────────────────────────────────────
def test_rule_key_is_namespaced_per_stage_and_document() -> None:
    doc = uuid4()
    assert du._rule_key("DELIVERY", doc) == f"DOCUMENT_UNRECOGNIZED:DELIVERY:{doc}"
    assert du._rule_key("BOOKING", doc) == f"DOCUMENT_UNRECOGNIZED:BOOKING:{doc}"


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


def test_producer_raises_one_finding_per_unrecognized_document(journey) -> None:
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

    rows = journey.execute(
        text("SELECT audit_finding_id, rule_key, finding_type_code, finding_class, owner_role_code, title "
             "FROM auditcore.audit_findings WHERE tenant_id = :t AND journey_id = :j"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["finding_type_code"] == "DOCUMENT_UNRECOGNIZED"
    assert row["finding_class"] == "DATA_GAP"
    assert row["owner_role_code"] == "PC"
    assert row["rule_key"] == du._rule_key("DELIVERY", UUID(unknown["documentId"]))
    assert "gst_declaration.pdf" in row["title"]

    event_payload = journey.execute(
        text("SELECT safe_payload FROM auditcore.audit_finding_events "
             "WHERE tenant_id = :t AND audit_finding_id = :f AND event_type = 'RAISED'"),
        {"t": tenant_id, "f": row["audit_finding_id"]},
    ).scalar_one()
    assert event_payload["contentUrl"] == unknown["contentUrl"]

    # idempotent -- calling again with the same live document doesn't duplicate
    again = du.sync_document_unrecognized_findings(
        journey, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
        di_documents=[unknown, classified], correlation_id="",
    )
    assert again["raised"] == 1
    assert journey.execute(
        text("SELECT count(*) FROM auditcore.audit_findings WHERE tenant_id = :t"), {"t": tenant_id}
    ).scalar_one() == 1


def test_reclassified_document_resolves_its_finding(journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    unknown = _unknown_document("mystery.pdf")

    du.sync_document_unrecognized_findings(
        journey, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
        di_documents=[unknown], correlation_id="",
    )
    open_status = journey.execute(
        text("SELECT finding_status FROM auditcore.audit_findings WHERE tenant_id = :t"),
        {"t": tenant_id},
    ).scalar_one()
    assert open_status == "OPEN"

    # A later DI pass reclassifies it -- the document is no longer in the
    # UNKNOWN set on the next capture-screen read.
    reclassified = dict(unknown, state="CLASSIFIED", classifiedDocumentTypeKey="credit_note")
    result = du.sync_document_unrecognized_findings(
        journey, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
        di_documents=[reclassified], correlation_id="",
    )
    assert result["resolved"] == 1

    closed = journey.execute(
        text("SELECT finding_status, disposition FROM auditcore.audit_findings WHERE tenant_id = :t"),
        {"t": tenant_id},
    ).mappings().one()
    assert closed["finding_status"] == "RESOLVED"
    assert closed["disposition"] == "FIXED"
