from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Self

import httpx
import structlog

from audit_core.telemetry import record_metric, trace_span

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DiSubject:
    subject_id: str
    status: str


@dataclass(frozen=True)
class DiDocument:
    document_id: str
    upload_status: str
    processing_status: str | None
    confirmation_status: str | None = None
    confidence_score: float | None = None
    document_type_key: str | None = None
    registered_at: str | None = None
    verification_state: str | None = None


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
    """Audit Core adapter for DI's D8 HTTP contract.

    Normal module-to-module operations use a Security-v2 ServiceIntegration token
    with aud=di. Human administrative proxy calls pass the initiating Security
    human token unchanged; the token policy is enforced by the DI route itself.
    """

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
        payload = self._request_data(
            "POST",
            f"/v1/tenants/{tenant_id}/integration/subjects",
            operation="create_subject",
            token=token,
            json={"subjectType": subject_type, "displayName": display_name},
        )
        return DiSubject(
            subject_id=_required_str(payload, "subjectId"),
            status=_required_str(payload, "status"),
        )

    def ensure_audit_storage_context(
        self,
        *,
        token: str,
        tenant_id: str,
        external_context_ref: str,
        subject_id: str,
        dealer_id: str,
        outlet_id: str,
        customer_id: str,
        project_name: str | None,
        dealer_name: str | None,
        outlet_name: str | None,
        customer_name: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_data(
            "PUT",
            f"/v1/tenants/{tenant_id}/audit-storage-contexts/{external_context_ref}",
            operation="ensure_audit_storage_context",
            token=token,
            headers={"Idempotency-Key": idempotency_key},
            json={
                "subjectId": subject_id,
                "dealerId": dealer_id,
                "dealerOutletId": outlet_id,
                "customerId": customer_id,
                "displayContext": {
                    "projectName": project_name,
                    "dealerName": dealer_name,
                    "dealerOutletName": outlet_name,
                    "customerName": customer_name,
                },
            },
        )

    def upload_audit_document(
        self,
        *,
        token: str,
        tenant_id: str,
        external_context_ref: str,
        filename: str,
        content: bytes,
        content_type: str,
        document_type_key: str | None = None,
    ) -> DiDocument:
        data: dict[str, str] = {}
        if document_type_key is not None:
            data["documentTypeKey"] = document_type_key
        payload = self._request_data(
            "POST",
            f"/v1/tenants/{tenant_id}/audit-storage-contexts/{external_context_ref}/documents",
            operation="upload_audit_document",
            token=token,
            allow_business_error_data=True,
            data=data,
            files={"file": (filename, content, content_type)},
        )
        return _document(payload)

    def get_audit_document(
        self,
        *,
        token: str,
        tenant_id: str,
        external_context_ref: str,
        document_id: str,
    ) -> DiDocument:
        payload = self._request_data(
            "GET",
            f"/v1/tenants/{tenant_id}/audit-storage-contexts/{external_context_ref}/documents/{document_id}",
            operation="get_audit_document",
            token=token,
        )
        return _document(payload)

    # Existing human/operational document reads are kept for current Audit Core
    # workflows. The adapter now unwraps D8 instead of expecting the payload at root.
    def get_document(
        self,
        *,
        token: str,
        tenant_id: str,
        subject_id: str,
        document_id: str,
    ) -> DiDocument:
        payload = self._request_data(
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
        payload = self._request_data(
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
        payload = self._request_data(
            "POST",
            f"/v1/tenants/{tenant_id}/subjects/{subject_id}/documents/{document_id}/verification",
            operation="verify_document",
            token=token,
            json={"remarks": remarks, "fieldCorrections": field_corrections or []},
        )
        return DiVerification(
            verification_id=_required_str(payload, "verificationId"),
            document_id=_required_str(payload, "documentId"),
            verified_at=_required_str(payload, "verifiedAt"),
            verified_by_actor_id=_required_str(payload, "verifiedByActorId"),
            field_correction_count=_required_int(payload, "fieldCorrectionCount"),
        )

    def ensure_project_provisioning(
        self,
        *,
        human_token: str,
        tenant_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_data(
            "PUT",
            f"/v1/tenants/{tenant_id}/admin/provisioning",
            operation="ensure_project_provisioning",
            token=human_token,
            headers={"Idempotency-Key": idempotency_key},
            json={},
        )

    def get_project_provisioning(
        self,
        *,
        human_token: str,
        tenant_id: str,
    ) -> dict[str, Any]:
        return self._request_data(
            "GET",
            f"/v1/tenants/{tenant_id}/admin/provisioning",
            operation="get_project_provisioning",
            token=human_token,
        )

    def list_project_masters(self, *, human_token: str, tenant_id: str) -> list[dict[str, Any]]:
        payload = self._request_data_any(
            "GET",
            f"/v1/tenants/{tenant_id}/project-masters",
            operation="list_project_masters",
            token=human_token,
        )
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise _contract_error()
        return [dict(item) for item in payload]

    def get_project_master_template(
        self, *, human_token: str, tenant_id: str, master_key: str
    ) -> tuple[bytes, str]:
        response = self._request_raw(
            "GET",
            f"/v1/tenants/{tenant_id}/project-masters/{master_key}/template",
            operation="get_project_master_template",
            token=human_token,
        )
        return response.content, response.headers.get(
            "content-type",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def upload_project_master_import(
        self,
        *,
        human_token: str,
        tenant_id: str,
        master_key: str,
        idempotency_key: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> dict[str, Any]:
        return self._request_data(
            "POST",
            f"/v1/tenants/{tenant_id}/project-masters/{master_key}/imports",
            operation="upload_project_master_import",
            token=human_token,
            headers={"Idempotency-Key": idempotency_key},
            files={"file": (filename, content, content_type)},
        )

    def get_project_master_import(
        self,
        *,
        human_token: str,
        tenant_id: str,
        master_key: str,
        import_id: str,
    ) -> dict[str, Any]:
        return self._request_data(
            "GET",
            f"/v1/tenants/{tenant_id}/project-masters/{master_key}/imports/{import_id}",
            operation="get_project_master_import",
            token=human_token,
        )

    def confirm_project_master_import(
        self,
        *,
        human_token: str,
        tenant_id: str,
        master_key: str,
        import_id: str,
    ) -> dict[str, Any]:
        return self._request_data(
            "POST",
            f"/v1/tenants/{tenant_id}/project-masters/{master_key}/imports/{import_id}/confirm",
            operation="confirm_project_master_import",
            token=human_token,
        )

    def list_project_master_versions(
        self, *, human_token: str, tenant_id: str, master_key: str
    ) -> dict[str, Any]:
        return self._request_data(
            "GET",
            f"/v1/tenants/{tenant_id}/project-masters/{master_key}/versions",
            operation="list_project_master_versions",
            token=human_token,
        )

    def publish_project_master_version(
        self,
        *,
        human_token: str,
        tenant_id: str,
        master_key: str,
        version_id: str,
    ) -> dict[str, Any]:
        return self._request_data(
            "POST",
            f"/v1/tenants/{tenant_id}/project-masters/{master_key}/versions/{version_id}/publish",
            operation="publish_project_master_version",
            token=human_token,
        )

    def _request_data(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        token: str,
        allow_business_error_data: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        data = self._request_data_any(
            method,
            path,
            operation=operation,
            token=token,
            allow_business_error_data=allow_business_error_data,
            **kwargs,
        )
        if not isinstance(data, dict):
            raise _contract_error()
        return data

    def _request_data_any(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        token: str,
        allow_business_error_data: bool = False,
        **kwargs: Any,
    ) -> Any:
        response = self._request_raw(
            method,
            path,
            operation=operation,
            token=token,
            **kwargs,
        )
        try:
            envelope = response.json()
        except ValueError as exc:
            raise _contract_error() from exc
        if not isinstance(envelope, dict):
            raise _contract_error()
        error_code = envelope.get("errorCode")
        error_message = envelope.get("errorMessage")
        if not isinstance(error_code, str) or not isinstance(error_message, str) or "data" not in envelope:
            raise _contract_error()
        if error_code != "000" and not allow_business_error_data:
            raise DiClientError(status_code=422, code=error_code, retryable=False)
        data = envelope.get("data")
        if data is None:
            raise _contract_error()
        return data

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        token: str,
        **kwargs: Any,
    ) -> httpx.Response:
        if not token:
            raise ValueError("DI bearer token is required")
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {token}"
        started = time.perf_counter()
        result = "SUCCESS"
        try:
            with trace_span(
                "audit_core.dependency.di",
                attributes={"dependency": "di", "operation": operation, "method": method},
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
                return response
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
                logger.warning(
                    "di_call_failed",
                    operation=operation,
                    error_code=result,
                    duration_ms=round(duration_ms, 2),
                )


def _document(payload: dict[str, Any]) -> DiDocument:
    return DiDocument(
        document_id=_required_str(payload, "documentId"),
        upload_status=_required_str(payload, "uploadStatus"),
        processing_status=_optional_str(payload, "processingStatus"),
        confirmation_status=_optional_str(payload, "confirmationStatus"),
        confidence_score=_optional_float(payload, "confidenceScore"),
        document_type_key=_optional_str(payload, "documentTypeKey"),
        registered_at=_optional_str(payload, "registeredAtUtc"),
        verification_state=_optional_str(payload, "verificationState"),
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
