from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Self

import httpx

from audit_core.telemetry import record_metric, trace_span


@dataclass(frozen=True)
class DiSubject:
    subject_id: str
    status: str


@dataclass(frozen=True)
class DiDocument:
    document_id: str
    subject_id: str | None
    upload_status: str
    processing_status: str
    confirmation_status: str
    verification_state: str
    human_verification_status: str | None
    confidence_score: float | None
    correlation_id: str


@dataclass(frozen=True)
class DiFact:
    canonical_field_id: str
    field_key: str
    value: Any
    value_source: str
    confidence_score: float | None
    version_no: int


@dataclass(frozen=True)
class DiVerification:
    verification_id: str
    document_id: str
    verified_at: str
    verified_by_actor_id: str
    field_correction_count: int


class DiClientError(RuntimeError):
    def __init__(self, *, status_code: int, code: str, retryable: bool) -> None:
        super().__init__(f"DI request failed: {code}")
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


class DiClient:
    """Maps DI HTTP contracts into stable Audit Core integration objects."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("DI base URL is required")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def create_subject(
        self,
        *,
        token: str,
        tenant_id: str,
        subject_type: str,
        display_name: str | None = None,
    ) -> DiSubject:
        payload = self._request_json(
            "POST",
            f"/v1/tenants/{tenant_id}/subjects",
            operation="create_subject",
            token=token,
            json={"subjectType": subject_type, "displayName": display_name},
        )
        return DiSubject(
            subject_id=_required_str(payload, "subjectId"),
            status=_required_str(payload, "status"),
        )

    def upload_document(
        self,
        *,
        token: str,
        tenant_id: str,
        subject_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        source_channel: str,
        document_type_key: str | None = None,
        captured_at: str | None = None,
        source_reference: str | None = None,
        replaces_document_id: str | None = None,
    ) -> DiDocument:
        form = {
            "sourceChannel": source_channel,
            "documentTypeKey": document_type_key,
            "capturedAt": captured_at,
            "sourceReference": source_reference,
            "replacesDocumentId": replaces_document_id,
        }
        response = self._request_json(
            "POST",
            f"/v1/tenants/{tenant_id}/subjects/{subject_id}/documents",
            operation="upload_document",
            token=token,
            data={key: value for key, value in form.items() if value is not None},
            files={"file": (filename, content, content_type)},
        )
        return _document(response)

    def get_document(
        self,
        *,
        token: str,
        tenant_id: str,
        subject_id: str,
        document_id: str,
    ) -> DiDocument:
        payload = self._request_json(
            "GET",
            f"/v1/tenants/{tenant_id}/subjects/{subject_id}/documents/{document_id}",
            operation="get_document",
            token=token,
        )
        return _document(payload)

    def get_document_facts(
        self,
        *,
        token: str,
        tenant_id: str,
        subject_id: str,
        document_id: str,
    ) -> tuple[DiFact, ...]:
        payload = self._request_json(
            "GET",
            f"/v1/tenants/{tenant_id}/subjects/{subject_id}/documents/{document_id}/fields",
            operation="get_document_facts",
            token=token,
        )
        fields = payload.get("fields")
        if not isinstance(fields, list):
            raise _contract_error()
        return tuple(_fact(item) for item in fields)

    def verify_document(
        self,
        *,
        token: str,
        tenant_id: str,
        subject_id: str,
        document_id: str,
        remarks: str | None = None,
        field_corrections: list[dict[str, Any]] | None = None,
    ) -> DiVerification:
        payload = self._request_json(
            "POST",
            f"/v1/tenants/{tenant_id}/subjects/{subject_id}/documents/{document_id}/verification",
            operation="verify_document",
            token=token,
            json={
                "remarks": remarks,
                "fieldCorrections": field_corrections or [],
            },
        )
        return DiVerification(
            verification_id=_required_str(payload, "verificationId"),
            document_id=_required_str(payload, "documentId"),
            verified_at=_required_str(payload, "verifiedAt"),
            verified_by_actor_id=_required_str(payload, "verifiedByActorId"),
            field_correction_count=_required_int(payload, "fieldCorrectionCount"),
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        token: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not token:
            raise ValueError("DI bearer token is required")
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {token}"
        started = time.perf_counter()
        result = "SUCCESS"
        try:
            with trace_span(
                "audit_core.dependency.di",
                attributes={
                    "dependency": "di",
                    "operation": operation,
                    "method": method,
                },
            ):
                try:
                    response = self._client.request(method, path, headers=headers, **kwargs)
                except httpx.HTTPError as exc:
                    result = "UNAVAILABLE"
                    raise DiClientError(
                        status_code=503,
                        code="DI_UNAVAILABLE",
                        retryable=True,
                    ) from exc

                if response.status_code < 200 or response.status_code >= 300:
                    result = "FAILURE"
                    raise _http_error(response)
                try:
                    payload = response.json()
                except ValueError as exc:
                    result = "CONTRACT_ERROR"
                    raise _contract_error() from exc
                if not isinstance(payload, dict):
                    result = "CONTRACT_ERROR"
                    raise _contract_error()
                return payload
        finally:
            duration_ms = (time.perf_counter() - started) * 1000.0
            labels = {
                "dependency": "di",
                "operation": operation,
                "method": method,
                "result": result,
            }
            record_metric("audit_core.dependency.calls", labels=labels)
            record_metric(
                "audit_core.dependency.duration_ms",
                duration_ms,
                kind="histogram",
                labels=labels,
            )
            if result != "SUCCESS":
                record_metric("audit_core.dependency.errors", labels=labels)


def _document(payload: dict[str, Any]) -> DiDocument:
    return DiDocument(
        document_id=_required_str(payload, "documentId"),
        subject_id=_optional_str(payload, "subjectId"),
        upload_status=_required_str(payload, "uploadStatus"),
        processing_status=_required_str(payload, "processingStatus"),
        confirmation_status=_required_str(payload, "confirmationStatus"),
        verification_state=_required_str(payload, "verificationState"),
        human_verification_status=_optional_str(payload, "humanVerificationStatus"),
        confidence_score=_optional_float(payload, "confidenceScore"),
        correlation_id=_required_str(payload, "correlationId"),
    )


def _fact(payload: Any) -> DiFact:
    if not isinstance(payload, dict):
        raise _contract_error()
    return DiFact(
        canonical_field_id=_required_str(payload, "canonicalFieldId"),
        field_key=_required_str(payload, "fieldKey"),
        value=payload.get("currentValue"),
        value_source=_required_str(payload, "valueSource"),
        confidence_score=_optional_float(payload, "confidenceScore"),
        version_no=_required_int(payload, "versionNo"),
    )


def _http_error(response: httpx.Response) -> DiClientError:
    code = f"DI_HTTP_{response.status_code}"
    retryable = response.status_code >= 500
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        candidate = payload.get("code")
        if isinstance(candidate, str) and candidate:
            code = candidate
        retryable_value = payload.get("retryable")
        if isinstance(retryable_value, bool):
            retryable = retryable_value
    return DiClientError(status_code=response.status_code, code=code, retryable=retryable)


def _contract_error() -> DiClientError:
    return DiClientError(status_code=502, code="DI_CONTRACT_ERROR", retryable=False)


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise _contract_error()
    return value


def _optional_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _contract_error()
    return value


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _contract_error()
    return value


def _optional_float(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise _contract_error()
    return float(value)
