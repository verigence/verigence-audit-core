from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.di_client import DiSubject
from audit_core.uc03_document_capture_v2 import _ensure_di_context


class _FakeSecurityClient:
    def get_service_token(self, *, audience: str) -> str:
        return "fake-token"


class _FakeDiClient:
    def __init__(self) -> None:
        self.context_calls = 0

    def create_subject(self, **kwargs: object) -> DiSubject:
        return DiSubject(subject_id=str(uuid4()), status="ACTIVE")

    def ensure_audit_storage_context(self, **kwargs: object) -> dict[str, str]:
        self.context_calls += 1
        return {}


@pytest.fixture
def di_context_journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-dicache-{suffix}"

    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"DIC-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"DIC-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'DIC', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"DIC-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"DIC-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets "
                 "(tenant_id, dealer_id, outlet_code, outlet_name, outlet_classification, status) "
                 "VALUES (:t, :d, :c, 'O', 'ONSITE', 'ACTIVE') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"DIC-O-{suffix}"},
        ).scalar_one()
        customer_id = c.execute(
            text("INSERT INTO auditcore.customers (tenant_id, dealer_id, outlet_id, customer_type_code, display_name) "
                 "VALUES (:t, :d, :o, 'INDIVIDUAL', 'DI Cache Customer') RETURNING customer_id"),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = c.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id) "
                 "VALUES (:t, :d, :o, :cu) RETURNING journey_id"),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id},
        ).scalar_one()
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        yield tenant_id, journey_id, c, engine
    engine.dispose()


def test_ensure_di_context_skips_the_redundant_ensure_call_on_repeat(di_context_journey) -> None:
    """Regression: _ensure_di_context previously re-issued the DI
    ensure_audit_storage_context PUT on every single call, even though the
    dealer/outlet/customer context info for a journey doesn't change between
    calls -- a second full DI round trip stacked in front of every Booking/
    Delivery capture read, upload-intents call, Review fetch and work-item
    enrichment pass. Two calls for the same journey within the reuse window
    must issue exactly one DI ensure-context call, not two."""
    tenant_id, journey_id, connection, engine = di_context_journey
    di_client = _FakeDiClient()
    security_client = _FakeSecurityClient()

    _ensure_di_context(
        connection=connection, engine=engine, tenant_id=tenant_id, journey_id=journey_id,
        security_client=security_client, di_client=di_client,
    )
    _ensure_di_context(
        connection=connection, engine=engine, tenant_id=tenant_id, journey_id=journey_id,
        security_client=security_client, di_client=di_client,
    )

    assert di_client.context_calls == 1
