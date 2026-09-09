from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core import uc03_confidence_review_policy as confidence_policy
from audit_core import uc03_post_extraction_materialization as post_extract
from audit_core.di_client import DiDocument, DiFact


class _FakeSecurityClient:
    def get_service_token(self, *, audience: str) -> str:
        return "fake-token"


class _FakeDiClient:
    def __init__(self, document: DiDocument, facts: list[DiFact]) -> None:
        self._document = document
        self._facts = facts

    def get_audit_document(self, **kwargs) -> DiDocument:
        return self._document

    def get_audit_document_facts(self, **kwargs) -> list[DiFact]:
        return self._facts


def _seed_booking_evidence(engine, *, tenant_id: str, suffix: str):
    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:c, 'V') RETURNING product_category_id"
            ),
            {"c": f"PEMCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"
            ),
            {"c": f"PEMOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date)
                VALUES (:t, :pc, 'PEM', :o, :cat, CURRENT_DATE)"""
            ),
            {"t": tenant_id, "pc": f"PEM-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:t, :c, 'D') RETURNING dealer_id"
            ),
            {"t": tenant_id, "c": f"PEM-D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"
            ),
            {"t": tenant_id, "d": dealer_id, "c": f"PEM-O-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """INSERT INTO auditcore.customers
                (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"""
            ),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text(
                """INSERT INTO auditcore.journeys
                (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""
            ),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"PEM-J-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """INSERT INTO auditcore.journey_stage_states
                (tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                 first_started_at_utc, latest_activity_at_utc, version_no)
                VALUES (:t, :j, 'BOOKING', 'BOOKING_IN_PROGRESS', 'IN_PROGRESS', 'NOT_EVALUATED',
                        now(), now(), 1)"""
            ),
            {"t": tenant_id, "j": journey_id},
        )
        document_id = uuid4()
        connection.execute(
            text(
                """INSERT INTO auditcore.evidence (
                    tenant_id, journey_id, customer_id,
                    di_subject_id, di_document_id,
                    document_type_key, evidence_purpose
                ) VALUES (
                    :t, :j, :cu, :subject, :doc,
                    'booking_form', 'BOOKING_CAPTURE'
                )"""
            ),
            {
                "t": tenant_id,
                "j": journey_id,
                "cu": customer_id,
                "subject": uuid4(),
                "doc": document_id,
            },
        )
    return journey_id, document_id


def test_successful_booking_sync_materializes_canonical_commercial_line() -> None:
    # Regression test for the Phase 0 monkeypatch removal: materialize_machine_
    # booking_values used to run via install_uc03_post_extraction_materialization
    # wrapping _sync_booking_document from the outside; it's now called directly
    # from inside _sync_booking_document itself. Drives the real function (not a
    # stub) with a fake DI client, and asserts the real canonical side effect --
    # a commercial_lines row -- rather than just that some function got called.
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-pem-{suffix}"
    journey_id, document_id = _seed_booking_evidence(engine, tenant_id=tenant_id, suffix=suffix)

    document = DiDocument(
        document_id=str(document_id),
        upload_status="COMPLETE",
        processing_status="COMPLETED",
        confirmation_status="CONFIRMED",
        document_type_key="booking_form",
        verification_state="NOT_VERIFIED",
    )
    fact = DiFact(
        canonical_field_id=f"ex_showroom_price-{suffix}",
        field_key="ex_showroom_price",
        value="996500",
        value_source="EXTRACTION",
        confidence_score=99.0,
        version_no=1,
    )

    with engine.begin() as connection:
        result = confidence_policy._sync_booking_document(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document_id,
            service_id="di-service",
            security_client=_FakeSecurityClient(),
            di_client=_FakeDiClient(document, [fact]),
            bump_version=True,
        )
        assert result == 1

    with engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT actual_amount
                FROM auditcore.commercial_lines
                WHERE tenant_id=:t AND journey_id=:j AND component_key='ex_showroom_price'
                """
            ),
            {"t": tenant_id, "j": journey_id},
        ).mappings().one_or_none()
    assert row is not None
    assert float(row["actual_amount"]) == 996500.0

    engine.dispose()


def test_pending_confirmation_does_not_run_canonical_materialization() -> None:
    # A document that hasn't reached DI's CONFIRMED status yet must not
    # materialize anything -- _sync_booking_document returns before ever
    # fetching facts.
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-pem-pending-{suffix}"
    journey_id, document_id = _seed_booking_evidence(engine, tenant_id=tenant_id, suffix=suffix)

    document = DiDocument(
        document_id=str(document_id),
        upload_status="COMPLETE",
        processing_status="PROCESSING",
        confirmation_status="PENDING",
        document_type_key="booking_form",
        verification_state="NOT_VERIFIED",
    )

    with engine.begin() as connection:
        result = confidence_policy._sync_booking_document(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document_id,
            service_id="di-service",
            security_client=_FakeSecurityClient(),
            di_client=_FakeDiClient(document, []),
            bump_version=True,
        )
        assert result == 0

    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT 1 FROM auditcore.commercial_lines WHERE tenant_id=:t AND journey_id=:j"
            ),
            {"t": tenant_id, "j": journey_id},
        ).one_or_none()
    assert row is None

    engine.dispose()


def test_low_confidence_does_not_remove_machine_effective_value() -> None:
    row = {
        "confidenceScore": 84.0,
        "confidenceScale": "PERCENT",
        "effectiveValue": "450000",
        "hasEffectiveValue": True,
    }
    assert post_extract._confidence_percent(row) == 84.0
    assert row["effectiveValue"] == "450000"
    assert row["hasEffectiveValue"] is True


def test_unit_interval_confidence_is_normalized_for_identity_gate() -> None:
    row = {"confidenceScore": 0.97, "confidenceScale": "UNIT_INTERVAL"}
    assert post_extract._confidence_percent(row) == 97.0
