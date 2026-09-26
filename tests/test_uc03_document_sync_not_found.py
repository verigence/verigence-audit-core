"""Root-caused live (2026-09-26): the self-heal sweep (uc03_document_sync_
recovery.py) repeatedly retried syncing documents DI had authoritatively
confirmed do not exist (404 DOCUMENT_NOT_FOUND) -- once per 5-minute sweep
cycle, forever, since _sync_booking_document wrapped every DiClientError
identically as "temporarily unavailable", discarding DI's own retryable/
code classification. Same fixture/fake-client pattern as
test_uc03_document_synced_execution_log.py (which itself credits
test_uc03_sku_resolution_ordering.py as the established pattern for
exercising _sync_booking_document directly against a real DB).
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core import uc03_confidence_review_policy as confidence_policy
from audit_core.di_client import DiClientError
from audit_core.errors import DependencyUnavailableError


class _FakeSecurityClient:
    def get_service_token(self, *, audience: str) -> str:
        return "fake-token"


class _FailingDiClient:
    def __init__(self, error: DiClientError) -> None:
        self._error = error

    def get_audit_document(self, **kwargs):
        raise self._error


@pytest.fixture
def sync_not_found_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-dsnf-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"DSNF-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"DSNF-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date)
                VALUES (:t, :pc, 'DSNF', :o, :cat, CURRENT_DATE - 60)"""),
            {"t": tenant_id, "pc": f"DSNF-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"DSNF-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"DSNF-O-{suffix}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"DSNF-J-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.journey_stage_states
                (tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                 first_started_at_utc, latest_activity_at_utc, version_no)
                VALUES (:t, :j, 'BOOKING', 'BOOKING_IN_PROGRESS', 'IN_PROGRESS', 'NOT_EVALUATED',
                        now(), now(), 1)"""),
            {"t": tenant_id, "j": journey_id},
        )
    yield engine, tenant_id, journey_id, customer_id
    engine.dispose()


def _add_evidence(engine, tenant_id, journey_id, customer_id, document_id) -> None:
    with engine.begin() as c:
        c.execute(
            text("""INSERT INTO auditcore.evidence
                (tenant_id, journey_id, customer_id, di_subject_id, di_document_id,
                 document_type_key, evidence_purpose)
                VALUES (:t, :j, :cu, :s, :d, 'booking_form', 'BOOKING')"""),
            {"t": tenant_id, "j": journey_id, "cu": customer_id, "s": uuid4(), "d": document_id},
        )


def _evidence_row(engine, tenant_id, journey_id, document_id):
    with engine.begin() as c:
        return c.execute(
            text("""SELECT association_status, void_reason FROM auditcore.evidence
                WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d"""),
            {"t": tenant_id, "j": journey_id, "d": document_id},
        ).mappings().one()


def test_document_not_found_voids_evidence_instead_of_retrying_forever(
    sync_not_found_setup,
) -> None:
    engine, tenant_id, journey_id, customer_id = sync_not_found_setup
    document_id = uuid4()
    _add_evidence(engine, tenant_id, journey_id, customer_id, document_id)

    di_client = _FailingDiClient(
        DiClientError(status_code=404, code="DOCUMENT_NOT_FOUND", retryable=False)
    )
    with engine.begin() as c:
        result = confidence_policy._sync_booking_document(
            c, tenant_id=tenant_id, journey_id=journey_id, document_id=document_id,
            service_id="di-service", security_client=_FakeSecurityClient(),
            di_client=di_client, bump_version=True,
        )
    assert result == 0

    row = _evidence_row(engine, tenant_id, journey_id, document_id)
    assert row["association_status"] == "VOIDED"
    assert row["void_reason"] == "DI_DOCUMENT_NOT_FOUND"


def test_other_di_errors_still_raise_and_leave_evidence_active(
    sync_not_found_setup,
) -> None:
    # Confirms the fix is narrowly scoped to DOCUMENT_NOT_FOUND specifically
    # -- a different DI failure must not be silently voided, and must still
    # surface as a real, retryable-by-the-caller error.
    engine, tenant_id, journey_id, customer_id = sync_not_found_setup
    document_id = uuid4()
    _add_evidence(engine, tenant_id, journey_id, customer_id, document_id)

    di_client = _FailingDiClient(
        DiClientError(status_code=503, code="DI_UNAVAILABLE", retryable=True)
    )
    with pytest.raises(DependencyUnavailableError), engine.begin() as c:
        confidence_policy._sync_booking_document(
            c, tenant_id=tenant_id, journey_id=journey_id, document_id=document_id,
            service_id="di-service", security_client=_FakeSecurityClient(),
            di_client=di_client, bump_version=True,
        )

    row = _evidence_row(engine, tenant_id, journey_id, document_id)
    assert row["association_status"] == "ACTIVE"
    assert row["void_reason"] is None
