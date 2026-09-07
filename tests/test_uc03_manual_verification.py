from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

import audit_core.uc03_manual_verification as mv


# ── unit ──────────────────────────────────────────────────────────────────────
def test_rule_key_round_trips_document_id() -> None:
    doc = uuid4()
    key = mv._rule_key("BOOKING", doc)
    assert key == f"MANUAL_VERIFICATION:BOOKING:{doc}"
    assert mv._document_from_rule(key) == doc


def test_friendly_label() -> None:
    assert mv._friendly_label("aadhaar_card", uuid4()) == "Aadhaar Card"
    doc = uuid4()
    assert mv._friendly_label(None, doc) == f"document {str(doc)[:8]}"


def test_by_document_groups() -> None:
    a, b = uuid4(), uuid4()
    rows = [
        {"di_document_id": a, "field_key": "x"},
        {"di_document_id": a, "field_key": "y"},
        {"di_document_id": b, "field_key": "z"},
    ]
    grouped = mv._by_document(rows)
    assert len(grouped[a]) == 2
    assert len(grouped[b]) == 1


# ── integration ───────────────────────────────────────────────────────────────
@pytest.fixture
def journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for manual-verification integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-mv-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"MV-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"MV-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'MV', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"MV-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"MV-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"MV-O-{suffix}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"MV-J-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.journey_stage_states
                (tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                 first_started_at_utc, latest_activity_at_utc, version_no)
                VALUES (:t, :j, 'BOOKING', 'BOOKING_IN_PROGRESS', 'IN_PROGRESS', 'NOT_EVALUATED',
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


def _add_field(conn, *, tenant_id, journey_id, document_id, field_key, value, confidence):
    conn.execute(
        text("""
            INSERT INTO auditcore.journey_document_extracted_fields (
                tenant_id, journey_id, di_document_id, source_fact_ref, source_fact_version,
                stage_code, field_key, source_canonical_field_id,
                extracted_value, confidence_score, confidence_scale
            ) VALUES (
                :t, :j, :doc, :ref, 1, 'BOOKING', :fk, :fk,
                CAST(:val AS jsonb), :conf, 'UNIT_INTERVAL'
            ) RETURNING extracted_field_id
        """),
        {"t": tenant_id, "j": journey_id, "doc": document_id, "ref": uuid4(),
         "fk": field_key, "val": json.dumps(value), "conf": confidence},
    )


def test_producer_raises_one_finding_per_document(journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    doc_a, doc_b = uuid4(), uuid4()
    _add_field(journey, tenant_id=tenant_id, journey_id=journey_id, document_id=doc_a,
               field_key="pan_number", value="ABCDE1234F", confidence=0.55)
    _add_field(journey, tenant_id=tenant_id, journey_id=journey_id, document_id=doc_a,
               field_key="name", value="J Doe", confidence=0.80)
    _add_field(journey, tenant_id=tenant_id, journey_id=journey_id, document_id=doc_b,
               field_key="dob", value="1990-01-01", confidence=0.99)  # high conf -> no finding

    result = mv.sync_manual_verification_findings(
        journey, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING", correlation_id="",
    )
    assert result["raised"] == 1  # only doc_a

    rows = journey.execute(
        text("SELECT rule_key, finding_type_code, finding_class, owner_role_code "
             "FROM auditcore.audit_findings WHERE tenant_id = :t AND journey_id = :j"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    assert len(rows) == 1
    assert rows[0]["finding_type_code"] == "MANUAL_VERIFICATION"
    assert rows[0]["finding_class"] == "DATA_GAP"
    assert rows[0]["owner_role_code"] == "PC"
    assert rows[0]["rule_key"] == f"MANUAL_VERIFICATION:BOOKING:{doc_a}"

    # idempotent
    again = mv.sync_manual_verification_findings(
        journey, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING", correlation_id="",
    )
    assert again["raised"] == 1
    assert journey.execute(
        text("SELECT count(*) FROM auditcore.audit_findings WHERE tenant_id = :t"), {"t": tenant_id}
    ).scalar_one() == 1


def test_resolving_confirms_and_corrects_then_closes_finding(journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    doc = uuid4()
    _add_field(journey, tenant_id=tenant_id, journey_id=journey_id, document_id=doc,
               field_key="pan_number", value="ABCDE1234F", confidence=0.55)
    _add_field(journey, tenant_id=tenant_id, journey_id=journey_id, document_id=doc,
               field_key="address", value="old addr", confidence=0.40)
    mv.sync_manual_verification_findings(
        journey, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING", correlation_id="",
    )
    finding_id = journey.execute(
        text("SELECT audit_finding_id FROM auditcore.audit_findings WHERE tenant_id = :t"),
        {"t": tenant_id},
    ).scalar_one()
    fields = journey.execute(
        text("SELECT extracted_field_id, field_key FROM auditcore.journey_document_extracted_fields "
             "WHERE tenant_id = :t ORDER BY field_key"),
        {"t": tenant_id},
    ).mappings().all()
    by_key = {r["field_key"]: r["extracted_field_id"] for r in fields}

    # resolve one field at a time — finding stays open until the last one
    _resolve(journey, tenant_id, journey_id, finding_id,
             [{"extractedFieldId": by_key["address"], "action": "CORRECT", "effectiveValue": "new addr"}])
    still_open = journey.execute(
        text("SELECT finding_status FROM auditcore.audit_findings WHERE audit_finding_id = :f AND tenant_id = :t"),
        {"f": finding_id, "t": tenant_id},
    ).scalar_one()
    assert still_open == "OPEN"

    _resolve(journey, tenant_id, journey_id, finding_id,
             [{"extractedFieldId": by_key["pan_number"], "action": "CONFIRM"}])
    closed = journey.execute(
        text("SELECT finding_status, disposition FROM auditcore.audit_findings "
             "WHERE audit_finding_id = :f AND tenant_id = :t"),
        {"f": finding_id, "t": tenant_id},
    ).mappings().one()
    assert closed["finding_status"] == "RESOLVED"
    assert closed["disposition"] == "FIXED"

    stored = journey.execute(
        text("SELECT field_key, effective_value, is_modified, reviewed_at_utc IS NOT NULL AS reviewed "
             "FROM auditcore.journey_document_extracted_fields WHERE tenant_id = :t ORDER BY field_key"),
        {"t": tenant_id},
    ).mappings().all()
    addr = next(r for r in stored if r["field_key"] == "address")
    pan = next(r for r in stored if r["field_key"] == "pan_number")
    assert addr["effective_value"] == "new addr"
    assert addr["is_modified"] is True
    assert addr["reviewed"] is True
    assert pan["effective_value"] == "ABCDE1234F"
    assert pan["is_modified"] is False
    assert pan["reviewed"] is True


def _resolve(conn, tenant_id, journey_id, finding_id, decisions):
    """Call the resolve handler directly (bypassing FastAPI DI)."""
    from audit_core.uc03_manual_verification import (
        ResolveManualVerificationCommand,
        resolve_manual_verification,
    )

    class _State:
        correlation_id = "test-corr"

    class _Req:
        def __init__(self) -> None:
            self.headers: dict = {}
            self.state = _State()

    class _Principal:
        subject = "pc-mv-test"

    class _Auth:
        def check_user_permission(self, **_):
            class _D:
                allowed = True

            return _D()

    return resolve_manual_verification(
        tenant_id=tenant_id,
        journey_id=journey_id,
        finding_id=finding_id,
        command=ResolveManualVerificationCommand(decisions=decisions),
        request=_Req(),
        human_principal=_Principal(),
        authorization_client=_Auth(),
        connection=conn,
    )
