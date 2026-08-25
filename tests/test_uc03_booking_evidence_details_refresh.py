from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import audit_core.uc03_booking_evidence_details as details
from audit_core.errors import DependencyUnavailableError


def _dependencies(monkeypatch):
    tenant_id = "tenant-test"
    journey_id = uuid4()
    evidence_id = uuid4()
    customer_id = uuid4()
    document_id = uuid4()
    connection = object()

    monkeypatch.setattr(details, "_scope", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        details,
        "_evidence_row",
        lambda *args, **kwargs: {
            "customer_id": customer_id,
            "di_subject_id": uuid4(),
            "di_document_id": document_id,
        },
    )

    security_client = MagicMock()
    security_client.get_service_token.return_value = "service-token"
    di_client = MagicMock()

    return {
        "tenant_id": tenant_id,
        "journey_id": journey_id,
        "evidence_id": evidence_id,
        "customer_id": customer_id,
        "document_id": document_id,
        "connection": connection,
        "security_client": security_client,
        "di_client": di_client,
    }


def test_refresh_pending_document_does_not_request_fields(monkeypatch) -> None:
    deps = _dependencies(monkeypatch)
    deps["di_client"].get_audit_document.return_value = SimpleNamespace(
        processing_status="PROCESSING",
        verification_state="OPTIONAL",
        confirmation_status="PENDING",
    )

    update_cache = MagicMock()
    monkeypatch.setattr(details, "_update_evidence_cache", update_cache)
    monkeypatch.setattr(details, "_fact_rows", lambda *args, **kwargs: [])

    result = details.refresh_booking_evidence_details(
        tenant_id=deps["tenant_id"],
        journey_id=deps["journey_id"],
        evidence_id=deps["evidence_id"],
        human_principal=object(),
        authorization_client=object(),
        security_client=deps["security_client"],
        di_client=deps["di_client"],
        connection=deps["connection"],
    )

    assert result == []
    deps["di_client"].get_audit_document.assert_called_once_with(
        token="service-token",
        tenant_id=deps["tenant_id"],
        external_context_ref=f"audit-{deps['journey_id']}-{deps['customer_id']}",
        document_id=str(deps["document_id"]),
    )
    deps["di_client"].get_audit_document_facts.assert_not_called()
    update_cache.assert_called_once_with(
        deps["connection"],
        tenant_id=deps["tenant_id"],
        journey_id=deps["journey_id"],
        evidence_id=deps["evidence_id"],
        processing_status="PROCESSING",
        verification_status="OPTIONAL",
        confirmation_status="PENDING",
    )


def test_refresh_confirmed_document_requests_and_persists_fields(monkeypatch) -> None:
    deps = _dependencies(monkeypatch)
    document = SimpleNamespace(
        processing_status="COMPLETED",
        verification_state="OPTIONAL",
        confirmation_status="CONFIRMED",
    )
    facts = (SimpleNamespace(field_key="customer_name"),)
    deps["di_client"].get_audit_document.return_value = document
    deps["di_client"].get_audit_document_facts.return_value = facts

    persist = MagicMock(return_value=[{"fieldKey": "customer_name"}])
    monkeypatch.setattr(details, "_persist_facts", persist)

    result = details.refresh_booking_evidence_details(
        tenant_id=deps["tenant_id"],
        journey_id=deps["journey_id"],
        evidence_id=deps["evidence_id"],
        human_principal=object(),
        authorization_client=object(),
        security_client=deps["security_client"],
        di_client=deps["di_client"],
        connection=deps["connection"],
    )

    assert result == [{"fieldKey": "customer_name"}]
    deps["di_client"].get_audit_document_facts.assert_called_once_with(
        token="service-token",
        tenant_id=deps["tenant_id"],
        external_context_ref=f"audit-{deps['journey_id']}-{deps['customer_id']}",
        document_id=str(deps["document_id"]),
    )
    persist.assert_called_once_with(
        deps["connection"],
        tenant_id=deps["tenant_id"],
        journey_id=deps["journey_id"],
        evidence_id=deps["evidence_id"],
        processing_status="COMPLETED",
        verification_status="OPTIONAL",
        confirmation_status="CONFIRMED",
        facts=facts,
    )


def test_refresh_e008_fields_not_ready_returns_cached_facts(monkeypatch) -> None:
    deps = _dependencies(monkeypatch)
    deps["di_client"].get_audit_document.return_value = SimpleNamespace(
        processing_status="COMPLETED",
        verification_state="OPTIONAL",
        confirmation_status="CONFIRMED",
    )
    deps["di_client"].get_audit_document_facts.side_effect = details.DiClientError(
        status_code=422,
        code="E008",
        retryable=False,
    )

    update_cache = MagicMock()
    monkeypatch.setattr(details, "_update_evidence_cache", update_cache)
    monkeypatch.setattr(
        details,
        "_fact_rows",
        lambda *args, **kwargs: [{"fieldKey": "cached"}],
    )

    result = details.refresh_booking_evidence_details(
        tenant_id=deps["tenant_id"],
        journey_id=deps["journey_id"],
        evidence_id=deps["evidence_id"],
        human_principal=object(),
        authorization_client=object(),
        security_client=deps["security_client"],
        di_client=deps["di_client"],
        connection=deps["connection"],
    )

    assert result == [{"fieldKey": "cached"}]
    update_cache.assert_called_once_with(
        deps["connection"],
        tenant_id=deps["tenant_id"],
        journey_id=deps["journey_id"],
        evidence_id=deps["evidence_id"],
        processing_status="COMPLETED",
        verification_status="OPTIONAL",
        confirmation_status="CONFIRMED",
    )


def test_refresh_non_e008_fields_error_remains_dependency_error(monkeypatch) -> None:
    deps = _dependencies(monkeypatch)
    deps["di_client"].get_audit_document.return_value = SimpleNamespace(
        processing_status="COMPLETED",
        verification_state="OPTIONAL",
        confirmation_status="CONFIRMED",
    )
    deps["di_client"].get_audit_document_facts.side_effect = details.DiClientError(
        status_code=502,
        code="DI_CONTRACT_ERROR",
        retryable=False,
    )

    with pytest.raises(DependencyUnavailableError):
        details.refresh_booking_evidence_details(
            tenant_id=deps["tenant_id"],
            journey_id=deps["journey_id"],
            evidence_id=deps["evidence_id"],
            human_principal=object(),
            authorization_client=object(),
            security_client=deps["security_client"],
            di_client=deps["di_client"],
            connection=deps["connection"],
        )
