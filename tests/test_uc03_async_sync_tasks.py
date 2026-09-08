from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

import audit_core.uc03_async_sync_tasks as ast_module


@pytest.fixture
def journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for async-sync-task integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-ast-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"AST-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"AST-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'AST', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"AST-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"AST-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"AST-O-{suffix}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"AST-J-{suffix}"},
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


def _open_finding(conn, *, tenant_id, journey_id, rule_key):
    return conn.execute(
        text(
            """
            SELECT audit_finding_id, finding_class, owner_role_code, finding_status
            FROM auditcore.audit_findings
            WHERE tenant_id=:t AND journey_id=:j AND rule_key=:r
              AND finding_status IN ('OPEN','ACKNOWLEDGED')
            """
        ),
        {"t": tenant_id, "j": journey_id, "r": rule_key},
    ).mappings().one_or_none()


def test_sku_resolution_failure_raises_data_gap_finding_for_pc(monkeypatch, journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    monkeypatch.setattr(
        ast_module, "sync_model_resolution", lambda *a, **k: {"error": True}
    )
    result = ast_module.sync_model_resolution_with_escalation(
        journey, tenant_id=tenant_id, journey_id=journey_id, correlation_id="corr-1"
    )
    assert result == {"error": True}
    rule_key = f"AUTOMATED_SYNC_FAILURE:SKU_RESOLUTION:{journey_id}"
    finding = _open_finding(journey, tenant_id=tenant_id, journey_id=journey_id, rule_key=rule_key)
    assert finding is not None
    assert finding["finding_class"] == "DATA_GAP"
    assert finding["owner_role_code"] == "PC"


def test_sku_resolution_success_after_failure_resolves_the_finding(monkeypatch, journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    monkeypatch.setattr(
        ast_module, "sync_model_resolution", lambda *a, **k: {"error": True}
    )
    ast_module.sync_model_resolution_with_escalation(
        journey, tenant_id=tenant_id, journey_id=journey_id, correlation_id="corr-1"
    )
    rule_key = f"AUTOMATED_SYNC_FAILURE:SKU_RESOLUTION:{journey_id}"
    assert _open_finding(journey, tenant_id=tenant_id, journey_id=journey_id, rule_key=rule_key) is not None

    monkeypatch.setattr(
        ast_module, "sync_model_resolution", lambda *a, **k: {"skipped": True}
    )
    ast_module.sync_model_resolution_with_escalation(
        journey, tenant_id=tenant_id, journey_id=journey_id, correlation_id="corr-2"
    )
    assert _open_finding(journey, tenant_id=tenant_id, journey_id=journey_id, rule_key=rule_key) is None


def test_payment_reconciliation_failure_raises_finding_under_its_own_stage(monkeypatch, journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    monkeypatch.setattr(
        ast_module, "reconcile_payments", lambda *a, **k: {"error": True}
    )
    result = ast_module.reconcile_payments_with_escalation(
        journey, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
        correlation_id="corr-1",
    )
    assert result == {"error": True}
    rule_key = f"AUTOMATED_SYNC_FAILURE:PAYMENT_RECONCILIATION:{journey_id}"
    finding = _open_finding(journey, tenant_id=tenant_id, journey_id=journey_id, rule_key=rule_key)
    assert finding is not None
    assert finding["finding_class"] == "DATA_GAP"
    assert finding["owner_role_code"] == "PC"


def test_not_confirmed_document_raises_document_missing_for_pc(journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    document_id = uuid4()
    ast_module.sync_document_confirmation_status(
        journey, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY",
        document_id=document_id, confirmation_status="NOT_CONFIRMED",
        document_label="Insurance Cover", correlation_id="corr-1",
    )
    rule_key = f"DOCUMENT_MISSING:DELIVERY:{document_id}"
    finding = _open_finding(journey, tenant_id=tenant_id, journey_id=journey_id, rule_key=rule_key)
    assert finding is not None
    assert finding["finding_class"] == "DATA_GAP"
    assert finding["owner_role_code"] == "PC"


def test_document_missing_resolves_when_the_same_document_later_confirms(journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    document_id = uuid4()
    ast_module.sync_document_confirmation_status(
        journey, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING",
        document_id=document_id, confirmation_status="NOT_CONFIRMED",
        document_label="Booking Form", correlation_id="corr-1",
    )
    rule_key = f"DOCUMENT_MISSING:BOOKING:{document_id}"
    assert _open_finding(journey, tenant_id=tenant_id, journey_id=journey_id, rule_key=rule_key) is not None

    ast_module.sync_document_confirmation_status(
        journey, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING",
        document_id=document_id, confirmation_status="CONFIRMED",
        document_label="Booking Form", correlation_id="corr-2",
    )
    assert _open_finding(journey, tenant_id=tenant_id, journey_id=journey_id, rule_key=rule_key) is None


def test_pending_confirmation_status_raises_nothing(journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    document_id = uuid4()
    ast_module.sync_document_confirmation_status(
        journey, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING",
        document_id=document_id, confirmation_status="PENDING",
        document_label="Booking Form", correlation_id="corr-1",
    )
    rule_key = f"DOCUMENT_MISSING:BOOKING:{document_id}"
    assert _open_finding(journey, tenant_id=tenant_id, journey_id=journey_id, rule_key=rule_key) is None


def test_expected_outcomes_never_raise_a_failure_finding(monkeypatch, journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    monkeypatch.setattr(
        ast_module, "sync_model_resolution", lambda *a, **k: {"skipped": True, "reason": "no_effective_price_list"}
    )
    ast_module.sync_model_resolution_with_escalation(
        journey, tenant_id=tenant_id, journey_id=journey_id, correlation_id="corr-1"
    )
    rule_key = f"AUTOMATED_SYNC_FAILURE:SKU_RESOLUTION:{journey_id}"
    assert _open_finding(journey, tenant_id=tenant_id, journey_id=journey_id, rule_key=rule_key) is None
